"""iTunes Search API scanner.

Apple Music URLs do not expose any per-variant manifest until the wrapper
authenticates and fetches the m3u8. Because we want to give the user a
quality preview *before* committing to a download, we fall back to Apple's
public iTunes Lookup API. It's unauthenticated, has no rate limits worth
worrying about for human-paced use, and exposes every signal we need:

    audioTraits  : ["lossless-audio", "hi-res-lossless", "atmos",
                    "spatial-audio", "lossy-stereo", ...]
    artworkUrl100, artistName, collectionName, trackCount, kind, ...

`audioTraits` lets us infer the maximum lossless sample rate Apple has on
file for that asset *without* having to start a download:

    hi-res-lossless  -> 192000 Hz max (24-bit)
    lossless-audio   -> 48000 Hz max  (24-bit, "Apple Lossless")
    lossy-stereo     -> AAC only

The picker we expose then walks the user-defined preference chain and
returns the highest-priority entry that the asset actually supports, so
"if 48 kHz is available pick it, else fall back to 44.1, else 96, else
192" works exactly as the user described.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
ITUNES_SEARCH = "https://itunes.apple.com/search"

# Apple sometimes 403s the default urllib UA, so use a real one.
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# All quality tiers users can ask for. Sample-rate-keyed because that's the
# axis of `alac-max` in upstream config.
ALAC_MAX_TIERS = (44100, 48000, 96000, 192000)


@dataclass
class ScanResult:
    """Per-URL scan output. Everything we know before downloading."""

    url: str
    ok: bool
    kind: str = "unknown"          # 'album' | 'song' | 'playlist' | 'artist' | 'music-video'
    artist: str = ""
    title: str = ""
    track_count: int = 0
    duration_ms: int = 0
    storefront: str = ""
    apple_id: str = ""
    artwork_url: str = ""
    audio_traits: list = field(default_factory=list)
    has_lossless: bool = False
    has_hi_res: bool = False
    has_atmos: bool = False
    has_lossy_only: bool = False
    max_alac_hz: int = 0           # 0 if no lossless at all
    available_tiers: list = field(default_factory=list)  # e.g. [44100, 48000, 96000, 192000]
    # Per-track breakdown for albums/playlists. Lets the UI show per-tier
    # storage estimates (`bandwidth_bps × seconds / 8`) and an expandable
    # track list under each item without needing a second iTunes call.
    tracks: list = field(default_factory=list)  # [{"title":..., "duration_ms":..., "track_number":...}, ...]
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "ok": self.ok,
            "kind": self.kind,
            "artist": self.artist,
            "title": self.title,
            "track_count": self.track_count,
            "duration_ms": self.duration_ms,
            "storefront": self.storefront,
            "apple_id": self.apple_id,
            "artwork_url": self.artwork_url,
            "audio_traits": self.audio_traits,
            "has_lossless": self.has_lossless,
            "has_hi_res": self.has_hi_res,
            "has_atmos": self.has_atmos,
            "has_lossy_only": self.has_lossy_only,
            "max_alac_hz": self.max_alac_hz,
            "available_tiers": list(self.available_tiers),
            "tracks": list(self.tracks),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------
_URL_RE = re.compile(
    r"music\.apple\.com/(?P<storefront>[a-z]{2,3})/(?P<kind>album|song|playlist|artist|music-video|mv)"
    r"/[^/]+/(?P<id>[^/?#]+)",
    re.IGNORECASE,
)


def parse_apple_url(url: str) -> Optional[dict]:
    """Pull (kind, storefront, id, song-id-if-any) out of an Apple Music URL.

    Returns None for unrecognised URLs so callers can short-circuit cleanly.
    """
    m = _URL_RE.search(url or "")
    if not m:
        return None
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query or "")
    song_id = (qs.get("i") or [""])[0]
    kind = m.group("kind").lower()
    if kind == "mv":
        kind = "music-video"
    # An album URL with `?i=<song-id>` actually points at a single song.
    if kind == "album" and song_id:
        kind = "song"
        apple_id = song_id
    else:
        apple_id = m.group("id")
    return {
        "kind": kind,
        "storefront": m.group("storefront").lower(),
        "id": apple_id,
    }


# ---------------------------------------------------------------------------
# iTunes API
# ---------------------------------------------------------------------------
def _http_json(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8") or "{}")


def _lookup(apple_id: str, storefront: str, entity: str = "") -> dict:
    """Hit the public iTunes Lookup endpoint. `entity=song` is required to
    get audioTraits per song; without it we only get album-level traits."""
    params = {"id": apple_id, "country": storefront or "us"}
    if entity:
        params["entity"] = entity
    qs = urllib.parse.urlencode(params)
    return _http_json(f"{ITUNES_LOOKUP}?{qs}")


def _coalesce_traits(*trait_lists) -> list:
    """Merge audioTraits from several results, keeping unique values."""
    seen = []
    for traits in trait_lists:
        if not traits:
            continue
        for t in traits:
            if t not in seen:
                seen.append(t)
    return seen


def _max_alac_from_traits(traits) -> int:
    """Map iTunes audioTraits → maximum ALAC sample rate Apple has on file.

    `hi-res-lossless` officially means up to 24/192. `lossless-audio`
    means at least CD (16/44.1) and up to 24/48. If neither is present
    (only `lossy-stereo`), there's no ALAC variant at all → 0.
    """
    t = set(traits or [])
    if "hi-res-lossless" in t:
        return 192000
    if "lossless-audio" in t or "lossless" in t:
        return 48000
    return 0


def _available_tiers_from_max(max_hz: int) -> list:
    """All ALAC sample-rate variants Apple makes available for this asset.

    Apple's m3u8 manifests carry a strict hierarchy: a hi-res-lossless asset
    contains 192/96/48/44.1 kHz variants; a lossless-audio asset contains
    48/44.1; lossy-only contains nothing. The downloader picks the highest
    variant that does not exceed `alac-max`, so listing them here gives the
    user a faithful preview of every cap they can pick.
    """
    if max_hz >= 192000:
        return [44100, 48000, 96000, 192000]
    if max_hz >= 96000:
        return [44100, 48000, 96000]
    if max_hz >= 48000:
        return [44100, 48000]
    if max_hz >= 44100:
        return [44100]
    return []


# ---------------------------------------------------------------------------
# Public scan API
# ---------------------------------------------------------------------------
def scan_url(url: str) -> ScanResult:
    parsed = parse_apple_url(url)
    if not parsed:
        return ScanResult(url=url, ok=False, error="Unrecognised Apple Music URL format")

    res = ScanResult(
        url=url,
        ok=False,
        kind=parsed["kind"],
        storefront=parsed["storefront"],
        apple_id=parsed["id"],
    )

    try:
        if parsed["kind"] == "album":
            data = _lookup(parsed["id"], parsed["storefront"], entity="song")
            results = data.get("results") or []
            album = next((r for r in results if r.get("wrapperType") == "collection"), None)
            tracks = [r for r in results if r.get("wrapperType") == "track"]
            if not album:
                res.error = "Album not found in iTunes catalog"
                return res
            res.artist = album.get("artistName", "")
            res.title = album.get("collectionName", "") or album.get("collectionCensoredName", "")
            res.track_count = album.get("trackCount", len(tracks))
            res.artwork_url = (album.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
            res.audio_traits = _coalesce_traits(
                album.get("audioTraits"),
                *(t.get("audioTraits") for t in tracks),
            )
            # Per-track durations let the UI compute storage estimates and
            # render an expandable track list. We sum into duration_ms so
            # the album-level total is consistent with the tracks list.
            sum_ms = 0
            for t in tracks:
                ms = int(t.get("trackTimeMillis") or 0)
                sum_ms += ms
                res.tracks.append({
                    "id": str(t.get("trackId") or ""),
                    "url": t.get("trackViewUrl") or "",
                    "kind": t.get("kind") or "",
                    "artist": t.get("artistName") or res.artist,
                    "title": t.get("trackName") or "",
                    "duration_ms": ms,
                    "track_number": t.get("trackNumber") or 0,
                    "disc_number": t.get("discNumber") or 0,
                    "audio_traits": list(t.get("audioTraits") or []),
                })
            res.duration_ms = sum_ms
        elif parsed["kind"] == "song":
            data = _lookup(parsed["id"], parsed["storefront"])
            results = data.get("results") or []
            track = next((r for r in results if r.get("wrapperType") == "track"), None)
            if not track:
                res.error = "Song not found in iTunes catalog"
                return res
            res.artist = track.get("artistName", "")
            res.title = track.get("trackName", "")
            res.track_count = 1
            res.duration_ms = track.get("trackTimeMillis", 0) or 0
            res.artwork_url = (track.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
            res.audio_traits = list(track.get("audioTraits") or [])
            res.tracks.append({
                "id": str(track.get("trackId") or parsed["id"]),
                "url": track.get("trackViewUrl") or url,
                "kind": track.get("kind") or "song",
                "artist": track.get("artistName") or res.artist,
                "title": res.title,
                "duration_ms": res.duration_ms,
                "track_number": track.get("trackNumber") or 1,
                "disc_number": track.get("discNumber") or 1,
                "audio_traits": list(track.get("audioTraits") or []),
            })
        elif parsed["kind"] == "playlist":
            # Playlists aren't in the lookup API; we can only mark the URL
            # as "valid playlist, scan-during-download". Keep traits empty
            # so the caller knows quality is unknown until runtime.
            res.title = "Playlist (preview unavailable)"
            res.kind = "playlist"
        elif parsed["kind"] == "artist":
            data = _lookup(parsed["id"], parsed["storefront"])
            results = data.get("results") or []
            artist = next((r for r in results if r.get("wrapperType") == "artist"), None)
            if artist:
                res.artist = artist.get("artistName", "")
                res.title = "(entire artist catalog)"
        else:  # music-video etc.
            data = _lookup(parsed["id"], parsed["storefront"])
            results = data.get("results") or []
            mv = results[0] if results else None
            if mv:
                res.artist = mv.get("artistName", "")
                res.title = mv.get("trackName", "")
                res.audio_traits = list(mv.get("audioTraits") or [])
        res.ok = True
    except Exception as e:  # noqa: BLE001
        res.error = f"iTunes lookup failed: {e}"
        return res

    res.has_hi_res = "hi-res-lossless" in res.audio_traits
    res.has_lossless = res.has_hi_res or "lossless-audio" in res.audio_traits or "lossless" in res.audio_traits
    res.has_atmos = "atmos" in res.audio_traits or "spatial-audio" in res.audio_traits
    res.has_lossy_only = (not res.has_lossless) and ("lossy-stereo" in res.audio_traits or not res.audio_traits)
    res.max_alac_hz = _max_alac_from_traits(res.audio_traits)
    res.available_tiers = _available_tiers_from_max(res.max_alac_hz)
    return res


def pick_alac_target(scan: ScanResult, fallback_chain) -> int:
    """Walk the user's preference chain and return the highest-priority
    sample rate the asset actually supports.

    `fallback_chain` is an ordered list, e.g. [48000, 44100, 96000, 192000].
    The first entry the asset can satisfy wins. If the asset is lossy-only
    (`max_alac_hz == 0`) we return 0 to signal "fall back to AAC".
    """
    if not scan.max_alac_hz:
        return 0
    chain = [int(x) for x in (fallback_chain or ALAC_MAX_TIERS) if int(x) > 0]
    for tier in chain:
        # We can clamp `alac-max` to anything ≤ what's available; the
        # downloader will then pick the highest variant ≤ that cap. So a
        # pref of 48 kHz on an asset that goes to 192 still works.
        if tier <= scan.max_alac_hz:
            return tier
        # If the user listed a tier above the asset's ceiling, skip it.
    # Nothing in the chain matched (e.g. user only listed 96 / 192 but
    # asset is lossless-only at 48). Fall back to the asset's own max.
    return scan.max_alac_hz
