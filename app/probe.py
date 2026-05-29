"""Real availability probe via apple-music-downloader's --debug mode.

The iTunes Lookup API exposes only a coarse classification (`hi-res-lossless`,
`lossless-audio`, `lossy-stereo`). It does not tell us which specific
variants Apple has on file for a given asset — and Apple does occasionally
publish hi-res-lossless metadata for albums that only have lossless
variants in the actual m3u8, or vice versa.

For an authoritative answer we have to ask Apple's CDN. The downstream
`apple-music-downloader` already does this — when invoked with `--debug`
its `extractMedia()` function fetches the master m3u8, prints a table of
every variant (codec, audio descriptor, bandwidth) and a per-format
summary, then **returns without downloading** (extractMedia returns
streamUrl=nil so the rest of the pipeline aborts). That gives us a
zero-cost authoritative probe.

We just spawn that binary, capture stdout, and parse the summary block.

Output of `go run main.go --debug <url>` (relevant section):

    Debug: All Available Variants:
    +-------+----------------+-----------+
    | CODEC | AUDIO          | BANDWIDTH |
    +-------+----------------+-----------+
    | alac  | audio-alac-... | 1411000   |
    +-------+----------------+-----------+
    Available Audio Formats:
    ------------------------
    AAC             : AAC | 2 Channel | 256 Kbps
    Lossless        : ALAC | 2 Channel | 16-bit/44 kHz
    Hi-Res Lossless : ALAC | 2 Channel | 24-bit/192 kHz
    Dolby Atmos     : E-AC-3 | 16 Channel | 768 Kbps
    Dolby Audio     : Not Available
    ------------------------

We parse:
  - the per-format summary (most reliable; one line per format)
  - the underlying variant table (kept for `verbose` output)

and return a structured dict the UI can consume directly.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from typing import Optional

# --------------------------------------------------------------------------
# Stdout parsing
# --------------------------------------------------------------------------
_SUMMARY_KEYS = {
    "AAC": "aac",
    "Lossless": "lossless",
    "Hi-Res Lossless": "hi_res",
    "Dolby Atmos": "atmos",
    "Dolby Audio": "dolby_audio",
}

# Examples this matches:
#   "ALAC | 2 Channel | 24-bit/192 kHz"
#   "ALAC | 2 Channel | 16-bit/44 kHz"
_ALAC_RE = re.compile(r"ALAC\s*\|\s*(?P<channels>\d+)\s*Channel\s*\|\s*(?P<bits>\d+)-bit/(?P<khz>\d+)\s*kHz", re.IGNORECASE)
# "AAC | 2 Channel | 256 Kbps"
_AAC_RE = re.compile(r"AAC\s*\|\s*(?P<channels>\d+)\s*Channel\s*\|\s*(?P<kbps>\d+)\s*Kbps", re.IGNORECASE)
# "E-AC-3 | 16 Channel | 768 Kbps"  /  "AC-3 | 16 Channel | 384 Kbps"
_DOLBY_RE = re.compile(r"(E-AC-3|AC-3)\s*\|\s*(?P<channels>\d+)\s*Channel\s*\|\s*(?P<kbps>\d+)\s*Kbps", re.IGNORECASE)
_SUMMARY_LINE_RE = re.compile(r"^(AAC|Lossless|Hi-Res Lossless|Dolby Atmos|Dolby Audio)\s*:\s*(.+)$")


def parse_amd_debug(stdout: str) -> dict:
    """Parse AMD --debug stdout into a structured availability report.

    Always returns a dict with the same shape, even if parsing finds
    nothing — the UI can treat absent keys as "not available".
    """
    report = {
        "aac": {"available": False, "label": "", "kbps": 0, "channels": 0, "bandwidth_bps": 0},
        "lossless": {"available": False, "label": "", "sample_rate_hz": 0, "bit_depth": 0, "channels": 0},
        "hi_res": {"available": False, "label": "", "sample_rate_hz": 0, "bit_depth": 0, "channels": 0},
        "atmos": {"available": False, "label": "", "kbps": 0, "channels": 0, "codec": "", "bandwidth_bps": 0},
        "dolby_audio": {"available": False, "label": "", "kbps": 0, "channels": 0, "codec": "", "bandwidth_bps": 0},
        # Cumulative ALAC sample-rate ceiling derived from the summary
        # block — handy for the auto-picker.
        "alac_sample_rates_hz": [],
        # Per-tier ALAC variant data parsed from the variant table:
        #   { 44100: {bandwidth_bps, bit_depth, channels}, 192000: {...} }
        # The UI uses this to compute realistic per-tier storage estimates
        # (bandwidth_bps × duration_seconds / 8). When the table is missing
        # a tier, the UI falls back to a typical-bitrate heuristic.
        "alac_variants_by_hz": {},
        # Raw rows from the variant table (`Codec | Audio | Bandwidth`).
        # Useful for verbose UI display.
        "variants": [],
        # Echo of the `--debug` raw output (trimmed) for debugging.
        "raw_summary": "",
    }

    in_summary = False
    summary_lines: list[str] = []

    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Available Audio Formats:"):
            in_summary = True
            continue
        if in_summary:
            if line.startswith("---") or line.startswith("==="):
                # End of summary block.
                if any("kbps" in s.lower() or "kHz" in s for s in summary_lines):
                    in_summary = False
                else:
                    # Header rule on the way IN. Keep collecting.
                    continue
            else:
                m = _SUMMARY_LINE_RE.match(line)
                if m:
                    summary_lines.append(line)
                    label = m.group(1)
                    value = m.group(2).strip()
                    if value.lower() == "not available":
                        continue
                    key = _SUMMARY_KEYS.get(label)
                    if not key:
                        continue
                    if key in ("lossless", "hi_res"):
                        am = _ALAC_RE.search(value)
                        if am:
                            khz = int(am.group("khz"))
                            sample_rate_hz = khz * 1000
                            # The summary collapses 44 kHz to "44" but the
                            # actual variant is 44.1 kHz.
                            if khz == 44:
                                sample_rate_hz = 44100
                            elif khz == 48:
                                sample_rate_hz = 48000
                            elif khz == 88:
                                sample_rate_hz = 88200
                            report[key].update({
                                "available": True,
                                "label": value,
                                "channels": int(am.group("channels")),
                                "bit_depth": int(am.group("bits")),
                                "sample_rate_hz": sample_rate_hz,
                            })
                            report["alac_sample_rates_hz"].append(sample_rate_hz)
                    elif key == "aac":
                        am = _AAC_RE.search(value)
                        if am:
                            report["aac"].update({
                                "available": True,
                                "label": value,
                                "channels": int(am.group("channels")),
                                "kbps": int(am.group("kbps")),
                            })
                    elif key in ("atmos", "dolby_audio"):
                        am = _DOLBY_RE.search(value)
                        if am:
                            report[key].update({
                                "available": True,
                                "label": value,
                                "codec": am.group(1).upper(),
                                "channels": int(am.group("channels")),
                                "kbps": int(am.group("kbps")),
                            })

        # Variant table rows like "| alac  | audio-alac-stereo-44100-16 | 1411000  |"
        if line.startswith("|") and not line.startswith("+") and "BANDWIDTH" not in line.upper():
            cols = [c.strip() for c in line.strip("|").split("|")]
            if len(cols) == 3 and cols[0] and cols[1] and cols[2].isdigit():
                codec = cols[0]
                audio = cols[1]
                bandwidth = int(cols[2])
                report["variants"].append({
                    "codec": codec,
                    "audio": audio,
                    "bandwidth": bandwidth,
                })
                # Decode the AMD `audio` descriptor to extract per-tier
                # info. ALAC rows look like:
                #   audio-alac-stereo-<sample_rate>-<bit_depth>
                # When multiple bandwidths are listed for the same tier
                # (rare; AMD usually picks one) we keep the highest, which
                # corresponds to the best sub-stream.
                if codec == "alac":
                    parts = audio.split("-")
                    if len(parts) >= 4:
                        try:
                            sr = int(parts[-2])
                            bd = int(parts[-1])
                        except ValueError:
                            sr = 0
                            bd = 0
                        if sr > 0:
                            cur = report["alac_variants_by_hz"].get(sr)
                            if not cur or bandwidth > cur.get("bandwidth_bps", 0):
                                report["alac_variants_by_hz"][sr] = {
                                    "bandwidth_bps": bandwidth,
                                    "bit_depth": bd,
                                    "channels": 2,
                                }
                elif codec.startswith("mp4a"):
                    # AAC stereo. Keep the highest-bandwidth entry.
                    cur = report["aac"].get("bandwidth_bps", 0)
                    if bandwidth > cur:
                        report["aac"]["bandwidth_bps"] = bandwidth
                elif codec == "ec-3":
                    cur = report["atmos"].get("bandwidth_bps", 0)
                    if bandwidth > cur:
                        report["atmos"]["bandwidth_bps"] = bandwidth
                elif codec == "ac-3":
                    cur = report["dolby_audio"].get("bandwidth_bps", 0)
                    if bandwidth > cur:
                        report["dolby_audio"]["bandwidth_bps"] = bandwidth

    # Deduplicate + sort sample rates. Prefer the variant-table list when
    # available (more accurate than the summary); fall back to the summary.
    if report["alac_variants_by_hz"]:
        rates = sorted(report["alac_variants_by_hz"].keys())
    else:
        rates = sorted(set(report["alac_sample_rates_hz"]))
    report["alac_sample_rates_hz"] = rates
    # Backfill bandwidth_bps from kbps if the variant table didn't provide it.
    for k in ("aac", "atmos", "dolby_audio"):
        if not report[k].get("bandwidth_bps") and report[k].get("kbps"):
            report[k]["bandwidth_bps"] = report[k]["kbps"] * 1000
    report["raw_summary"] = "\n".join(summary_lines)
    return report


# --------------------------------------------------------------------------
# Binary management
# --------------------------------------------------------------------------
# `go run main.go` recompiles every invocation, which is several seconds
# of overhead per probe. For a 10-URL queue that's a minute of waiting.
# We cache a built binary under apple-music-downloader/.alac-rip-probe.bin
# and rebuild only when main.go (or anything in apple-music-downloader/)
# is newer than the cached binary.

_BINARY_NAME = ".alac-rip-probe.bin" + (".exe" if os.name == "nt" else "")
_BUILD_LOCK = threading.Lock()


def _binary_path(amd_dir: str) -> str:
    return os.path.join(amd_dir, _BINARY_NAME)


def _newest_source_mtime(amd_dir: str) -> float:
    newest = 0.0
    for root, _, files in os.walk(amd_dir):
        # Skip our own cached binary so it doesn't trigger rebuilds of itself.
        if _BINARY_NAME in files:
            files = [f for f in files if f != _BINARY_NAME]
        for f in files:
            if not f.endswith((".go", ".mod", ".sum", ".yaml")):
                continue
            try:
                m = os.path.getmtime(os.path.join(root, f))
                if m > newest:
                    newest = m
            except OSError:
                continue
    return newest


def ensure_probe_binary(amd_dir: str, env: Optional[dict] = None) -> Optional[str]:
    """Build (or reuse) the probe binary. Returns its absolute path, or
    None if the build failed.

    Subsequent probes invoke the binary directly, skipping `go run`'s
    ~3 s recompile-per-invocation overhead.
    """
    if not os.path.isdir(amd_dir):
        return None
    binary = _binary_path(amd_dir)
    with _BUILD_LOCK:
        try:
            bin_mtime = os.path.getmtime(binary) if os.path.isfile(binary) else 0.0
        except OSError:
            bin_mtime = 0.0
        src_mtime = _newest_source_mtime(amd_dir)
        if bin_mtime > 0 and bin_mtime >= src_mtime:
            return binary
        env_full = os.environ.copy()
        if env:
            env_full.update(env)
        try:
            proc = subprocess.run(
                ["go", "build", "-o", _BINARY_NAME, "main.go"],
                cwd=amd_dir,
                env=env_full,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=180,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0 or not os.path.isfile(binary):
            return None
        return binary


# --------------------------------------------------------------------------
# Public probe API
# --------------------------------------------------------------------------
def deep_probe(
    url: str,
    amd_dir: str,
    env: Optional[dict] = None,
    timeout_s: float = 45.0,
) -> dict:
    """Ask Apple's CDN (via AMD --debug) for the actual variant list.

    Returns a dict with:
      ok: bool
      url: echoed back
      duration_s: wallclock time
      report: parsed availability (see parse_amd_debug)
      stdout: trimmed raw stdout (for the UI debug panel)
      error: human-readable failure string ("" on success)

    Network/auth failures degrade gracefully — we always return a dict.
    Caller decides what to do with `ok=False`.
    """
    started = time.time()
    out = {
        "ok": False,
        "url": url,
        "duration_s": 0.0,
        "report": {},
        "stdout": "",
        "error": "",
    }

    binary = ensure_probe_binary(amd_dir, env=env)
    if binary:
        cmd = [binary, "--debug", url]
        cwd = amd_dir
    else:
        # Fall back to `go run` if the build cache is unavailable.
        cmd = ["go", "run", "main.go", "--debug", url]
        cwd = amd_dir

    env_full = os.environ.copy()
    if env:
        env_full.update(env)

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env_full,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            text=True,
        )
    except FileNotFoundError as e:
        out["error"] = f"binary not found: {e}"
        out["duration_s"] = round(time.time() - started, 2)
        return out
    except subprocess.TimeoutExpired:
        out["error"] = f"probe timed out after {timeout_s}s"
        out["duration_s"] = round(time.time() - started, 2)
        return out

    raw = proc.stdout or ""
    report = parse_amd_debug(raw)

    # Trim stdout to the relevant block; the rest is wrapper handshake noise.
    trimmed_lines: list[str] = []
    capture = False
    for line in raw.splitlines():
        if "All Available Variants:" in line or "Available Audio Formats:" in line:
            capture = True
        if capture:
            trimmed_lines.append(line)
    out["stdout"] = "\n".join(trimmed_lines[-100:])

    # We consider the probe successful if at least one summary line was
    # populated — even one positive entry tells the UI something useful.
    any_format = any(
        report.get(k, {}).get("available")
        for k in ("aac", "lossless", "hi_res", "atmos", "dolby_audio")
    )
    out["report"] = report
    out["ok"] = any_format
    out["duration_s"] = round(time.time() - started, 2)
    if not any_format and not out["error"]:
        # No variants parsed — most likely the wrapper isn't running or
        # the URL was rejected. Surface the last few lines of stdout.
        tail = "\n".join(raw.splitlines()[-8:])
        out["error"] = "no variants returned (is the wrapper running and authenticated?)\n" + tail
    return out
