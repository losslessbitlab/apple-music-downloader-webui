import subprocess
import threading
import time
import socket
import shutil
from flask import render_template, request, jsonify, Response, stream_with_context, send_file
import shlex
import yaml
import os
import io
import zipfile
import re
import json
import base64
from . import app
from concurrent.futures import ThreadPoolExecutor
from .scanner import scan_url as itunes_scan, pick_alac_target, ScanResult, ALAC_MAX_TIERS
from .queue_engine import JobQueue, Job, build_job_from_scan
from .probe import deep_probe, ensure_probe_binary

# ---------------------------------------------------------------------------
# Path constants — resolved ONCE at import. Anything that needs to find
# apple-music-downloader, wrapper, or config files MUST go through these.
# Printing them on import makes layout problems (e.g. a nested
# `alac-rip/alac-rip/` extract) immediately visible in the Flask console.
# ---------------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AMD_DIR = os.path.join(PROJECT_DIR, "apple-music-downloader")
CONFIG_PATH = os.path.join(AMD_DIR, "config.yaml")
CONFIG_EXAMPLE_PATH = os.path.join(AMD_DIR, "config.yaml.example")
BUNDLED_DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "default_config.yaml")
WRAPPER_BIN_DIR = os.path.join(PROJECT_DIR, "wrapper")

print(f"[routes] PROJECT_DIR = {PROJECT_DIR}")
print(f"[routes] AMD_DIR     = {AMD_DIR} (exists={os.path.isdir(AMD_DIR)})")
print(f"[routes] CONFIG_PATH = {CONFIG_PATH} (exists={os.path.isfile(CONFIG_PATH)})")


def _ensure_config_yaml():
    """Make sure `config.yaml` exists at CONFIG_PATH.

    Upstream `zhaarey/apple-music-downloader` only ships `config.yaml.example`;
    a fresh `git clone` therefore never produces `config.yaml` on its own.
    To stop the Settings page from breaking on a fresh install we bootstrap
    the file ourselves, in this priority order:

      1. Use the upstream example shipped with the cloned repo.
      2. Use our bundled fallback (so Settings even works before the AMD
         clone has finished or if the user nuked the example).

    If the parent directory doesn't exist yet (AMD repo not cloned), we
    create it and drop the bundled default in there so /get_config can
    still serve a sensible default. Returns True if a config now exists.
    """
    if os.path.isfile(CONFIG_PATH):
        return True
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    except OSError as e:
        print(f"[routes] Could not create AMD dir: {e}")
        return False
    src = None
    if os.path.isfile(CONFIG_EXAMPLE_PATH):
        src = CONFIG_EXAMPLE_PATH
    elif os.path.isfile(BUNDLED_DEFAULT_CONFIG):
        src = BUNDLED_DEFAULT_CONFIG
    if not src:
        return False
    try:
        import shutil as _sh
        _sh.copyfile(src, CONFIG_PATH)
        print(f"[routes] Bootstrapped config.yaml from {src}")
        return True
    except OSError as e:
        print(f"[routes] Failed to bootstrap config.yaml: {e}")
        return False


# Run once at import so a fresh install lands with a working config.yaml.
_ensure_config_yaml()

wrapper_process = None
wrapper_running = False
wrapper_needs_2fa = False


# ---------------------------------------------------------------------------
# Post-download quality verification
# ---------------------------------------------------------------------------
_AUDIO_EXTS = {".m4a", ".mp4", ".alac", ".flac", ".mp3", ".opus", ".wav", ".aac", ".ogg", ".dts", ".ec3"}


def _resolve_save_folder(format_choice, special_audio):
    """Return the absolute save-folder path for this download, based on the
    user's format selection and the current config.yaml."""
    cfg = {}
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}
    if special_audio and format_choice == "ATMOS":
        rel = cfg.get("atmos-save-folder") or "AM-DL-Atmos downloads"
    elif special_audio and format_choice == "AAC":
        rel = cfg.get("aac-save-folder") or "AM-DL-AAC downloads"
    else:
        rel = cfg.get("alac-save-folder") or "AM-DL downloads"
    rel = str(rel)
    return rel if os.path.isabs(rel) else os.path.join(AMD_DIR, rel)


def _human_size(n):
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if f < 1024:
            return f"{f:.2f} {unit}"
        f /= 1024
    return f"{f:.2f} TiB"


def _ffprobe_info(path):
    """Run ffprobe on `path` and return a structured dict (or None on error).

    Used both to build human-readable log lines AND to feed the Library
    panel on the home page (per-track table). Keeping a single source of
    truth means the kbps/sample-rate/etc. shown in the log and in the
    Library are guaranteed identical.
    """
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format", "-show_streams",
                path,
            ],
            stderr=subprocess.STDOUT,
            timeout=15,
        )
        data = json.loads(out)
    except Exception:
        return None

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {}) or {}

    try:
        duration = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    try:
        bit_rate_kbps = int(int(fmt.get("bit_rate") or 0) / 1000)
    except (TypeError, ValueError):
        bit_rate_kbps = 0
    try:
        size_bytes = int(fmt.get("size") or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    try:
        sample_rate = int(audio.get("sample_rate") or 0)
    except (TypeError, ValueError):
        sample_rate = 0
    try:
        bits_per_sample = int(audio.get("bits_per_raw_sample") or audio.get("bits_per_sample") or 0)
    except (TypeError, ValueError):
        bits_per_sample = 0

    return {
        "filename": os.path.basename(path),
        "path": path,
        "codec": audio.get("codec_name") or audio.get("codec_long_name") or "?",
        "container": fmt.get("format_name") or "?",
        "sample_rate_hz": sample_rate,
        "bits_per_sample": bits_per_sample,
        "channels": audio.get("channels") or 0,
        "bit_rate_kbps": bit_rate_kbps,
        "duration_s": duration,
        "size_bytes": size_bytes,
    }


def _ffprobe_summary_lines(path):
    """Backwards-compat shim: returns the legacy 5-line block, or None."""
    info = _ffprobe_info(path)
    if not info:
        return None
    minutes, seconds = divmod(int(info["duration_s"]), 60)
    chan_label = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(info["channels"], f"{info['channels']}ch") if info["channels"] else "?"
    bits_part = f", {info['bits_per_sample']}-bit" if info["bits_per_sample"] else ""
    sr_label = f"{info['sample_rate_hz']} Hz" if info["sample_rate_hz"] else "?"
    return [
        f"\U0001F3BC {info['filename']}",
        f"   Codec: {info['codec']}{bits_part}  \u00b7  Container: {info['container']}",
        f"   Sample rate: {sr_label}  \u00b7  Channels: {chan_label}  \u00b7  Bit rate: {info['bit_rate_kbps']} kbps",
        f"   Duration: {minutes}:{seconds:02d}  \u00b7  Size: {_human_size(info['size_bytes'])}",
        f"   Path: {info['path']}",
    ]


def summarize_files_since(folder, target_list, since=0.0, tracks_out=None):
    """Walk `folder` for audio files newer than `since` (mtime epoch) and
    append an ffprobe summary for each into `target_list`.

    If `tracks_out` (list) is provided, also append per-track structured
    dicts (the same data the log lines are derived from). The Library
    panel on the home page uses this to render album/track tables.
    """
    if not folder or not os.path.isdir(folder):
        target_list.append(f"\u2139 Save folder not found for verification: {folder}")
        return
    found = []
    for root, _, files in os.walk(folder):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _AUDIO_EXTS:
                continue
            fp = os.path.join(root, fname)
            try:
                if os.path.getmtime(fp) >= since - 1:
                    found.append(fp)
            except OSError:
                pass
    found.sort()
    if not found:
        target_list.append(
            f"\u2139 No new audio files detected under {folder}; verification skipped."
        )
        return
    target_list.append(f"\U0001F3BC Verified {len(found)} file(s) in {folder}:")
    for fp in found:
        info = _ffprobe_info(fp)
        if info is None:
            target_list.append(f"\U0001F3BC {os.path.basename(fp)} (ffprobe unavailable)")
            continue
        if tracks_out is not None:
            tracks_out.append(info)
        # Log lines: cap to first 25 to avoid log spam on huge albums.
        if len([t for t in target_list if isinstance(t, str) and t.startswith("\U0001F3BC ")]) <= 26:
            minutes, seconds = divmod(int(info["duration_s"]), 60)
            chan_label = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(info["channels"], f"{info['channels']}ch") if info["channels"] else "?"
            bits_part = f", {info['bits_per_sample']}-bit" if info["bits_per_sample"] else ""
            sr_label = f"{info['sample_rate_hz']} Hz" if info["sample_rate_hz"] else "?"
            target_list.append(f"\U0001F3BC {info['filename']}")
            target_list.append(f"   Codec: {info['codec']}{bits_part}  \u00b7  Container: {info['container']}")
            target_list.append(f"   Sample rate: {sr_label}  \u00b7  Channels: {chan_label}  \u00b7  Bit rate: {info['bit_rate_kbps']} kbps")
            target_list.append(f"   Duration: {minutes}:{seconds:02d}  \u00b7  Size: {_human_size(info['size_bytes'])}")
            target_list.append(f"   Path: {info['path']}")

def stream_wrapper_logs(pipe, target_list, email=None, password=None, auto_login=False):
    """Thread target to read logs from wrapper process and store them."""
    global wrapper_running, wrapper_process, wrapper_needs_2fa
    login_successful = False
    
    try:
        for line in iter(pipe.readline, ''):
            line = line.strip()
            if line:
                target_list.append(line)
                print(f"[WRAPPER LOG] {line}", flush=True)
                
                # Check for 2FA requirement
                if "credentialHandler:" in line and "2FA: true" in line:
                    wrapper_needs_2fa = True
                    target_list.append("2FA required - please enter your code")
                    
                # Check for successful login.
                # Older wrapper builds printed "[.] response type 6"; newer
                # builds print three "[!] listening ... on 127.0.0.1:NNNNN"
                # lines once all local TCP ports are up. The "account info"
                # listener is the last one started, so it's the safest single
                # marker for "ready to accept download requests".
                if (
                    "[.] response type 6" in line
                    or "listening account info request" in line
                ):
                    wrapper_running = True
                    wrapper_needs_2fa = False
                    login_successful = True
                    if auto_login:
                        target_list.append("Auto-login successful. Ready for downloads.")
                    else:
                        target_list.append("Wrapper login successful. Ready for downloads.")
                        # Save credentials on successful manual login
                        if email and password:
                            if save_credentials(email, password):
                                target_list.append("Credentials saved for auto-login")
                            else:
                                target_list.append("Failed to save credentials")
                    
    except Exception as e:
        target_list.append(f"Error reading wrapper logs: {str(e)}")
    finally:
        # Check if process ended
        if wrapper_process and wrapper_process.poll() is not None:
            exit_code = wrapper_process.poll()
            if not login_successful:
                # Process ended before successful login
                target_list.append(f"Login failed - wrapper process exited with code: {exit_code}")
                wrapper_running = False
                wrapper_needs_2fa = False
                # Delete credentials on failed auto-login
                if auto_login:
                    target_list.append("Auto-login failed, deleting saved credentials")
                    delete_credentials()
            elif exit_code != 0:
                target_list.append(f"Wrapper process ended unexpectedly with exit code: {exit_code}")
                wrapper_running = False
                wrapper_needs_2fa = False
            else:
                target_list.append("Wrapper process ended normally")
                wrapper_running = False
                wrapper_needs_2fa = False
        pipe.close()

wrapper_logs = []


# ---------------------------------------------------------------------------
# Job queue singleton
# ---------------------------------------------------------------------------
def _queue_save_folder(format_choice, special_audio):
    """Adapter so JobQueue can call our save-folder resolver."""
    return _resolve_save_folder(format_choice, special_audio)


def _queue_ffprobe(folder, target_list, since=0.0, tracks_out=None):
    """Adapter so JobQueue can call our ffprobe summarizer with a folder
    + since cutoff. `tracks_out` (optional) collects structured per-track
    dicts that drive the home-page Library panel."""
    summarize_files_since(folder, target_list, since=since, tracks_out=tracks_out)


JOB_QUEUE = JobQueue(
    amd_dir=AMD_DIR,
    resolve_save_folder=_queue_save_folder,
    ffprobe_summary=_queue_ffprobe,
)


def _refresh_queue_webhooks():
    """Read completion + library-scan webhooks from config.yaml and push
    them into the queue. Cheap; called on every save."""
    cfg = {}
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}
    JOB_QUEUE.set_webhooks(
        completion=cfg.get("completion-webhook") or "",
        library_scan=cfg.get("library-scan-webhook") or "",
    )


# Pull current webhook config on import so jobs queued before any save
# still hit the configured endpoints.
_refresh_queue_webhooks()

def get_credentials_path():
    """Get the path to the credentials file"""
    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(script_dir, ".credentials")

def save_credentials(email, password):
    """Save credentials to file (base64 encoded for basic obfuscation)"""
    try:
        credentials = {
            "email": base64.b64encode(email.encode()).decode(),
            "password": base64.b64encode(password.encode()).decode()
        }
        with open(get_credentials_path(), 'w') as f:
            json.dump(credentials, f)
        return True
    except Exception as e:
        print(f"Error saving credentials: {e}")
        return False

def load_credentials():
    """Load and decode saved credentials"""
    try:
        credentials_path = get_credentials_path()
        if os.path.exists(credentials_path):
            with open(credentials_path, 'r') as f:
                credentials = json.load(f)
            email = base64.b64decode(credentials["email"]).decode()
            password = base64.b64decode(credentials["password"]).decode()
            return email, password
    except Exception as e:
        print(f"Error loading credentials: {e}")
    return None, None

def delete_credentials():
    """Delete saved credentials"""
    try:
        credentials_path = get_credentials_path()
        if os.path.exists(credentials_path):
            os.remove(credentials_path)
        return True
    except Exception as e:
        print(f"Error deleting credentials: {e}")
        return False

def attempt_auto_login():
    """Try to automatically login with saved credentials"""
    email, password = load_credentials()
    if email and password:
        wrapper_logs.append("Found saved credentials, attempting auto-login...")
        return start_wrapper_login(email, password, auto_login=True)
    return False

def start_wrapper_login(email, password, auto_login=False):
    """Start wrapper login process"""
    global wrapper_process, wrapper_running, wrapper_logs
    
    if wrapper_process and wrapper_process.poll() is None:
        if not auto_login:
            wrapper_logs.append("Wrapper already running")
        return False

    if not auto_login:
        wrapper_logs = []  # reset logs only for manual login
    
    prefix = "Auto-login: " if auto_login else ""
    wrapper_logs.append(f"{prefix}Starting wrapper login for {email}...")
    
    # Use absolute path and proper command format
    script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    wrapper_dir = os.path.join(script_dir, "wrapper")
    wrapper_path = os.path.join(wrapper_dir, "wrapper")
    
    cmd = [wrapper_path, "-L", f"{email}:{password}"]
    # Redact password before adding to user-visible logs (the logs are
    # surfaced over the /get_logs endpoint, so we must never write the
    # password there in plaintext).
    safe_cmd_str = f"{wrapper_path} -L {email}:***"
    wrapper_logs.append(f"{prefix}Executing: {safe_cmd_str}")
    wrapper_logs.append(f"{prefix}Working directory: {wrapper_dir}")
    
    try:
        wrapper_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            bufsize=1,
            universal_newlines=True,
            cwd=wrapper_dir  # Run from wrapper directory
        )
        
        # Don't set wrapper_running=True yet, wait for the success message
        threading.Thread(target=stream_wrapper_logs, args=(wrapper_process.stdout, wrapper_logs, email, password, auto_login), daemon=True).start()
        
        wrapper_logs.append(f"{prefix}Wrapper process started, waiting for login confirmation...")
        return True
        
    except Exception as e:
        wrapper_logs.append(f"{prefix}Error starting wrapper: {str(e)}")
        if auto_login:
            wrapper_logs.append("Auto-login failed, deleting saved credentials")
            delete_credentials()
        return False


@app.route("/")
def index():
    # Check for saved credentials and attempt auto-login on first load
    email, password = load_credentials()
    if email and password and not wrapper_running and (not wrapper_process or wrapper_process.poll() is not None):
        # Attempt auto-login in a separate thread to not block page load
        threading.Thread(target=attempt_auto_login, daemon=True).start()
    
    return render_template("index.html", wrapper_running=wrapper_running, has_saved_credentials=email is not None, saved_email=email if email else "")


@app.route("/login_wrapper", methods=["POST"])
def login_wrapper():
    email = request.form.get("email")
    password = request.form.get("password")

    if wrapper_process and wrapper_process.poll() is None:
        return jsonify({"status": "error", "msg": "Wrapper already running"})

    if start_wrapper_login(email, password, auto_login=False):
        return jsonify({"status": "ok", "msg": "Wrapper process started, waiting for login..."})
    else:
        return jsonify({"status": "error", "msg": "Failed to start wrapper"})

@app.route("/submit_2fa", methods=["POST"])
def submit_2fa():
    global wrapper_process, wrapper_needs_2fa, wrapper_logs
    
    two_fa_code = request.form.get("twofa_code")
    
    if not wrapper_needs_2fa:
        return jsonify({"status": "error", "msg": "2FA not required"})
    
    if not wrapper_process or wrapper_process.poll() is not None:
        return jsonify({"status": "error", "msg": "Wrapper not running"})
    
    if not two_fa_code:
        return jsonify({"status": "error", "msg": "2FA code required"})
    
    try:
        # Send 2FA code to wrapper process
        wrapper_process.stdin.write(f"{two_fa_code}\n")
        wrapper_process.stdin.flush()
        wrapper_logs.append("Submitted 2FA code")
        wrapper_needs_2fa = False
        return jsonify({"status": "ok", "msg": "2FA code submitted"})
    except Exception as e:
        wrapper_logs.append(f"❌ Error submitting 2FA code: {str(e)}")
        return jsonify({"status": "error", "msg": f"Failed to submit 2FA code: {str(e)}"})


def _read_user_quality_prefs():
    """Pull the user's quality fallback chain + behavior toggles from
    config.yaml. Returns (chain, strict, skip_if_exists)."""
    cfg = {}
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}
    raw_chain = cfg.get("quality-preference-chain") or [48000, 44100, 96000, 192000]
    chain = []
    for v in raw_chain:
        try:
            chain.append(int(v))
        except (TypeError, ValueError):
            continue
    if not chain:
        chain = [48000, 44100, 96000, 192000]
    strict = bool(cfg.get("quality-strict-mode", False))
    skip = bool(cfg.get("skip-if-exists", False))
    return chain, strict, skip


@app.route("/scan", methods=["POST"])
def scan():
    """Scan a list of Apple Music URLs and return per-URL metadata +
    auto-picked quality. Used by the home page to populate the
    confirm-before-download modal."""
    payload = request.get_json(silent=True) or {}
    raw_urls = payload.get("urls") or []
    if isinstance(raw_urls, str):
        raw_urls = [raw_urls]
    urls = []
    seen = set()
    for u in raw_urls:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        urls.append(u)
    if not urls:
        return jsonify({"status": "error", "msg": "No URLs provided"})

    chain, strict, skip = _read_user_quality_prefs()
    results = []
    # Scan in parallel-ish; iTunes is fast but we don't want a 50-URL
    # request to take 50× the latency. Threadpool with a small cap.
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(itunes_scan, urls):
            d = r.to_dict()
            d["auto_alac_hz"] = pick_alac_target(r, chain) if r.has_lossless else 0
            d["needs_user_attention"] = bool(strict and r.ok and r.max_alac_hz and chain[0] > r.max_alac_hz)
            results.append(d)

    return jsonify({
        "status": "ok",
        "results": results,
        "fallback_chain": chain,
        "strict": strict,
        "skip_if_exists": skip,
    })


# ---------------------------------------------------------------------------
# Deep probe: ask Apple's CDN (via apple-music-downloader's --debug mode)
# for the actual m3u8 variant list per asset. The wrapper / AMD pipeline
# is single-tenant, so probes serialise behind this lock.
# ---------------------------------------------------------------------------
_PROBE_LOCK = threading.Lock()


@app.route("/probe", methods=["POST"])
def probe_endpoint():
    """Deep-probe one or more URLs and return the actual variants Apple
    will deliver (codec, channels, sample rate, bit depth, kbps).

    Body: {"urls": ["https://music.apple.com/..."]}

    The probe spawns AMD with `--debug`, which fetches the master m3u8
    from Apple's CDN, prints every variant, and aborts before any audio
    is downloaded (extractMedia returns nil streamUrl when more_mode is
    set). The wrapper must be running and authenticated.
    """
    if not wrapper_running:
        return jsonify({"status": "error", "msg": "Wrapper not running — login first."})
    payload = request.get_json(silent=True) or {}
    urls = payload.get("urls") or []
    if isinstance(urls, str):
        urls = [urls]
    urls = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
    if not urls:
        return jsonify({"status": "error", "msg": "No URLs provided"})

    results = []
    # Serialise probes — AMD/wrapper handshake is single-tenant per process.
    with _PROBE_LOCK:
        # Build the binary once up-front so the per-URL latency is just the
        # round-trip to Apple, not Go's compile time.
        ensure_probe_binary(AMD_DIR)
        for url in urls:
            res = deep_probe(url, AMD_DIR)
            results.append(res)

    return jsonify({"status": "ok", "results": results})


@app.route("/queue/start", methods=["POST"])
def queue_start():
    """Materialise scan results into Job objects and enqueue them.

    Body: {
      "items":[{"url":..., "format":"ALAC|ATMOS|AAC",
                "override_max_hz":48000 (optional),
                "override_atmos_max":2768 (optional),
                "override_aac_type":"aac-lc" (optional)},...],
      "skip_if_exists": bool,
      "strict": bool,
      "global_override_hz": 0|44100|48000|96000|192000,  # apply to ALL ALAC items
      "lyrics_mode": ""|"standard"|"karaoke",            # apply to ALL items
    }
    """
    if not wrapper_running:
        return jsonify({"status": "error", "msg": "Wrapper not running"})
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    if not items:
        return jsonify({"status": "error", "msg": "No items"})

    chain, strict_default, skip_default = _read_user_quality_prefs()
    skip = payload.get("skip_if_exists", skip_default)
    strict = payload.get("strict", strict_default)
    global_override = int(payload.get("global_override_hz") or 0)
    lyrics_mode = (payload.get("lyrics_mode") or "").strip().lower()
    if lyrics_mode not in ("", "standard", "karaoke"):
        lyrics_mode = ""
    # Skip-MV: when ON, exclude any item whose iTunes `kind` is
    # music-video, and split albums that contain MV tracks into per-song
    # jobs that drop the MVs. Done entirely in Python before enqueue —
    # we never touch AMD's Go source. Default-off so existing flows
    # behave exactly as before.
    skip_mv = bool(payload.get("skip_mv"))

    # User-defined priority chain from the scan modal. When supplied it
    # overrides the chain pulled from config.yaml so the auto-pick
    # respects what the user actually wants. Only positive integers in
    # `ALAC_MAX_TIERS` are honoured; everything else is dropped silently.
    raw_chain = payload.get("global_chain") or []
    user_chain = []
    for v in raw_chain:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv in ALAC_MAX_TIERS and iv not in user_chain:
            user_chain.append(iv)
    if user_chain:
        chain = user_chain

    # User-defined format priority chain. Same idea as the kHz chain
    # but for codec: e.g. ["ALAC", "AAC", "ATMOS"] means "try ALAC first,
    # otherwise AAC, otherwise Atmos". Only used as a fallback when a
    # requested per-item format isn't viable for that specific asset.
    raw_fmt_chain = payload.get("global_format_chain") or []
    fmt_chain: list = []
    for v in raw_fmt_chain:
        if not isinstance(v, str):
            continue
        u = v.upper()
        if u in ("ALAC", "ATMOS", "AAC") and u not in fmt_chain:
            fmt_chain.append(u)
    if not fmt_chain:
        fmt_chain = ["ALAC", "AAC", "ATMOS"]

    def _format_viable(scan_obj, fmt: str) -> bool:
        """Can we realistically deliver this format for this asset?"""
        if fmt == "ALAC":
            return getattr(scan_obj, "max_alac_hz", 0) > 0
        if fmt == "ATMOS":
            return bool(getattr(scan_obj, "has_atmos", False))
        if fmt == "AAC":
            # AAC is essentially always available on Apple's catalog and
            # is also our last-resort for lossy-only assets.
            return True
        return False

    def _pick_format(scan_obj, requested: str) -> str:
        """If `requested` works for this asset, keep it. Otherwise walk
        the user's chain. Last resort: AAC."""
        if requested and _format_viable(scan_obj, requested):
            return requested
        for f in fmt_chain:
            if _format_viable(scan_obj, f):
                return f
        return "AAC"

    def _max_alac_from_traits(traits) -> int:
        t = set(traits or [])
        if "hi-res-lossless" in t:
            return 192000
        if "lossless-audio" in t or "lossless" in t:
            return 48000
        return 0

    def _song_scan_from_album_track(album_scan: ScanResult, track: dict, album_url: str) -> ScanResult:
        """Synthesise a single-song ScanResult from one entry of an
        album's `tracks` list so we can build a per-song Job that
        downloads just that track. Used when Skip-MV is on and the
        album contains a mix of audio + music-video tracks.

        IMPORTANT: AMD's `?i=<trackId>` filter is only honored when
        `dl_song == true`, which only flips for a canonical `/song/`
        URL. iTunes' `trackViewUrl` is the album-with-`?i=` form, which
        AMD treats as `/album/`-kind and silently downloads the *whole*
        album — N split tracks would re-download the album N times.
        Build the `/song/<storefront>/song/<trackId>` form ourselves
        so AMD dispatches via `ripSong` and only fetches that one
        track."""
        track_id = str(track.get("id") or "")
        storefront = (album_scan.storefront or "us").lower()
        if track_id:
            track_url = f"https://music.apple.com/{storefront}/song/{track_id}"
        else:
            # Fallback: keep whatever the scanner gave us, then the
            # album URL as a last-ditch.
            track_url = (track.get("url") or "").strip() or album_url
        traits = list(track.get("audio_traits") or album_scan.audio_traits or [])
        max_hz = _max_alac_from_traits(traits)
        return ScanResult(
            url=track_url or album_url,
            ok=True,
            kind="song",
            artist=track.get("artist") or album_scan.artist,
            title=track.get("title") or album_scan.title,
            track_count=1,
            duration_ms=int(track.get("duration_ms") or 0),
            storefront=album_scan.storefront,
            apple_id=track_id,
            artwork_url=album_scan.artwork_url,
            audio_traits=traits,
            has_lossless=max_hz > 0,
            has_hi_res="hi-res-lossless" in set(traits),
            has_atmos=("atmos" in set(traits) or "spatial-audio" in set(traits)),
            has_lossy_only=(max_hz == 0),
            max_alac_hz=max_hz,
            available_tiers=[44100, 48000, 96000, 192000][: max(0, [44100, 48000, 96000, 192000].index(max_hz) + 1)] if max_hz in (44100, 48000, 96000, 192000) else [],
            tracks=[track],
        )

    jobs = []
    skipped_mv_items = 0
    for it in items:
        url = (it.get("url") or "").strip()
        fmt = (it.get("format") or "ALAC").upper()
        if fmt not in ("ALAC", "ATMOS", "AAC"):
            fmt = "ALAC"
        if not url:
            continue
        # Re-scan inline so the queue always has fresh metadata; cheap.
        scan_res = itunes_scan(url)
        if not scan_res.ok and fmt == "ALAC":
            # We can still try the download without metadata, but warn.
            scan_res.error = scan_res.error or "Scan failed; queueing anyway"
        # Apply the format chain only if the requested format isn't viable.
        # When the scan failed we fall back to the requested format as-is
        # (the AMD binary will figure it out at download time).
        if scan_res.ok:
            fmt = _pick_format(scan_res, fmt)

        # ---- Skip-MV: drop standalone music-video URLs entirely. ----
        if skip_mv and scan_res.ok and scan_res.kind == "music-video":
            skipped_mv_items += 1
            continue

        # ---- Skip-MV: albums that mix audio + MV tracks. ----
        # We don't split into N jobs anymore — the UI looked noisy and
        # AMD's per-album auth handshake / cover-art fetch ran N times.
        # Instead: one Job whose primary URL is the first audio track's
        # canonical /song/<id> URL and whose extra_urls list carries the
        # rest. AMD's main loop walks all positional args in a single
        # Go process, so it's one queue card, one auth, N tracks in
        # sequence, MVs excluded by construction.
        if skip_mv and scan_res.ok and scan_res.kind == "album" and scan_res.tracks:
            audio_tracks = [
                t for t in scan_res.tracks
                if (t.get("kind") or "").lower() != "music-video"
            ]
            skipped_mvs = len(scan_res.tracks) - len(audio_tracks)
            if skipped_mvs > 0 and audio_tracks:
                per_item_override = int(it.get("override_max_hz") or 0)
                override = per_item_override or global_override
                # Build a synthetic ScanResult from the first audio track
                # so build_job_from_scan picks its format chain off real
                # per-track audio traits. The job's metadata still
                # reflects the album-level identity for the UI card.
                first = audio_tracks[0]
                first_scan = _song_scan_from_album_track(scan_res, first, url)
                first_fmt = _pick_format(first_scan, fmt)
                job = build_job_from_scan(
                    first_scan,
                    desired_format=first_fmt,
                    fallback_chain=chain,
                    strict=strict,
                    skip_if_exists=False,
                    override_max_hz=override,
                    override_atmos_max=int(it.get("override_atmos_max") or 0),
                    override_aac_type=str(it.get("override_aac_type") or ""),
                    lyrics_mode=lyrics_mode,
                )
                # Append the remaining audio tracks as sibling URLs so
                # AMD downloads them all in one invocation.
                job.extra_urls = [
                    _song_scan_from_album_track(scan_res, t, url).url
                    for t in audio_tracks[1:]
                ]
                # Re-stamp metadata to the album identity. The UI card
                # then says "Artist — Album · album · N tracks" exactly
                # like a normal MV-free album.
                job.metadata.update({
                    "title":        scan_res.title,
                    "artist":       scan_res.artist,
                    "kind":         "album",
                    "track_count":  len(audio_tracks),
                    "artwork_url":  scan_res.artwork_url,
                    "audio_traits": scan_res.audio_traits,
                    "split_from_album": True,
                    "skipped_mvs_from_album": skipped_mvs,
                })
                job.log_lines.append(
                    f"› Album has {skipped_mvs} music video(s) — skipping them, downloading {len(audio_tracks)} audio track(s)."
                )
                jobs.append(job)
                skipped_mv_items += skipped_mvs
                continue

        # Per-item override beats the global override; global overrides the
        # auto-pick from the fallback chain. Either way the cap is now
        # passed as `--alac-max <Hz>` on the CLI so it actually takes effect.
        per_item_override = int(it.get("override_max_hz") or 0)
        override = per_item_override or global_override
        job = build_job_from_scan(
            scan_res,
            desired_format=fmt,
            fallback_chain=chain,
            strict=strict,
            skip_if_exists=skip,
            override_max_hz=override,
            override_atmos_max=int(it.get("override_atmos_max") or 0),
            override_aac_type=str(it.get("override_aac_type") or ""),
            lyrics_mode=lyrics_mode,
        )
        jobs.append(job)

    if not jobs:
        if skipped_mv_items:
            return jsonify({"status": "error", "msg": "Nothing queued: selected item(s) were music videos and Skip music videos is enabled."})
        return jsonify({"status": "error", "msg": "No valid jobs to queue"})
    ids = JOB_QUEUE.enqueue(jobs)
    return jsonify({"status": "ok", "queued": len(ids), "ids": ids, "skipped_mv": skipped_mv_items})


@app.route("/jobs", methods=["GET"])
def jobs_list():
    return jsonify({"status": "ok", "jobs": JOB_QUEUE.list_jobs()})


@app.route("/jobs/<job_id>", methods=["GET"])
def job_detail(job_id):
    j = JOB_QUEUE.get(job_id)
    if not j:
        return jsonify({"status": "error", "msg": "Job not found"}), 404
    return jsonify({"status": "ok", "job": j})


@app.route("/jobs/<job_id>/cancel", methods=["POST"])
def job_cancel(job_id):
    ok = JOB_QUEUE.cancel(job_id)
    return jsonify({"status": "ok" if ok else "error",
                    "msg": "Canceled" if ok else "Cannot cancel"})


@app.route("/jobs/<job_id>/retry", methods=["POST"])
def job_retry(job_id):
    new_id = JOB_QUEUE.retry(job_id)
    if not new_id:
        return jsonify({"status": "error", "msg": "Cannot retry"})
    return jsonify({"status": "ok", "id": new_id})


@app.route("/jobs/<job_id>/remove", methods=["POST"])
def job_remove(job_id):
    ok = JOB_QUEUE.remove(job_id)
    return jsonify({"status": "ok" if ok else "error"})


@app.route("/jobs/<job_id>/reprobe", methods=["POST"])
def job_reprobe(job_id):
    summary = JOB_QUEUE.reprobe(job_id)
    if summary is None:
        return jsonify({"status": "error", "msg": "Cannot reprobe"})
    return jsonify({"status": "ok", "summary": summary})


@app.route("/jobs/stream")
def jobs_stream():
    """Server-Sent Events stream of queue updates. Replaces 1Hz polling."""
    sub = JOB_QUEUE.subscribe()

    def gen():
        try:
            # Heartbeat every 25 s to keep proxies (waitress, nginx) from
            # closing the connection during quiet periods.
            last_beat = time.time()
            while True:
                try:
                    ev = sub.get(timeout=5.0)
                except Exception:
                    ev = None
                if ev is None:
                    if time.time() - last_beat > 25:
                        yield ":heartbeat\n\n"
                        last_beat = time.time()
                    continue
                yield "data: " + json.dumps(ev) + "\n\n"
                last_beat = time.time()
        finally:
            JOB_QUEUE.unsubscribe(sub)

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Backwards-compat: the old single-URL /download endpoint.
# It now just enqueues ONE job into the queue so existing scripts/UIs
# that POST `link` / `format` / `special_audio` keep working.
# ---------------------------------------------------------------------------
@app.route("/download", methods=["POST"])
def download():
    link = (request.form.get("link") or "").strip()
    format_choice = (request.form.get("format") or "ALAC").upper()
    special_audio = request.form.get("special_audio") == "true"

    if not wrapper_running:
        return jsonify({"status": "error", "msg": "Wrapper not running"})
    if not link:
        return jsonify({"status": "error", "msg": "No URL provided"})
    if not os.path.isdir(AMD_DIR):
        return jsonify({
            "status": "error",
            "msg": (
                f"apple-music-downloader is not installed at {AMD_DIR}. "
                f"Run 'sudo python3 main.py' from {PROJECT_DIR} first."
            ),
        })

    if not special_audio:
        format_choice = "ALAC"
    elif format_choice not in ("ATMOS", "AAC"):
        return jsonify({"status": "error", "msg": "Invalid format selected"})

    chain, strict, skip = _read_user_quality_prefs()
    scan_res = itunes_scan(link)
    job = build_job_from_scan(
        scan_res,
        desired_format=format_choice,
        fallback_chain=chain,
        strict=strict,
        skip_if_exists=skip,
    )
    JOB_QUEUE.enqueue([job])
    return jsonify({"status": "ok", "msg": "Queued", "id": job.id})


@app.route("/get_logs")
def get_logs():
    """Legacy poll endpoint. Returns the most recent running/queued job's
    log lines so the old single-pane UI keeps working. New UIs should
    subscribe to /jobs/stream instead."""
    global wrapper_running, wrapper_process, wrapper_needs_2fa
    if wrapper_process and wrapper_process.poll() is not None and wrapper_running:
        wrapper_running = False

    jobs = JOB_QUEUE.list_jobs()
    running = next((j for j in jobs if j["status"] == "running"), None)
    last_done = next((j for j in reversed(jobs) if j["status"] in ("done", "failed", "canceled", "skipped")), None)
    pivot = running or last_done
    downloader_lines = pivot["log_lines"][-200:] if pivot else []
    if pivot and pivot["quality_summary"]:
        downloader_lines = downloader_lines + pivot["quality_summary"]

    return jsonify({
        "wrapper": wrapper_logs[-200:],
        "downloader": downloader_lines,
        "wrapper_running": wrapper_running,
        "download_running": running is not None,
        "wrapper_needs_2fa": wrapper_needs_2fa,
        "queue_summary": {
            "queued": sum(1 for j in jobs if j["status"] == "queued"),
            "running": 1 if running else 0,
            "done": sum(1 for j in jobs if j["status"] == "done"),
            "failed": sum(1 for j in jobs if j["status"] == "failed"),
        },
    })


# ---------------------------------------------------------------------------
# Health / storage / open-folder
# ---------------------------------------------------------------------------
def _wrapper_port_alive(port=10020, host="127.0.0.1", timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@app.route("/health")
def health():
    """Liveness/readiness JSON. Cheap to call from monitoring."""
    folders_check = {}
    save_folders = _read_save_folders()
    for k, v in save_folders.items():
        try:
            usage = shutil.disk_usage(v) if os.path.isdir(v) else None
        except Exception:
            usage = None
        folders_check[k] = {
            "path": v,
            "exists": os.path.isdir(v),
            "free_bytes": usage.free if usage else None,
            "total_bytes": usage.total if usage else None,
        }
    ffprobe_ok = shutil.which("ffprobe") is not None
    go_ok = shutil.which("go") is not None
    # MP4Box (from the gpac suite) is optional: the downloader uses it to
    # embed/mux some outputs (music videos, animated artwork). Plain audio
    # downloads work without it.
    mp4box_ok = shutil.which("MP4Box") is not None
    # Token presence (length only — never the value).
    token_len = 0
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            token_len = len(str(cfg.get("media-user-token") or ""))
        except Exception:
            token_len = 0
    return jsonify({
        "status": "ok",
        "project_dir": PROJECT_DIR,
        "amd_dir_exists": os.path.isdir(AMD_DIR),
        "config_yaml_exists": os.path.isfile(CONFIG_PATH),
        "wrapper_bin_exists": os.path.isfile(os.path.join(WRAPPER_BIN_DIR, "wrapper")),
        "wrapper_process_running": bool(wrapper_process and wrapper_process.poll() is None),
        "wrapper_login_ok": wrapper_running,
        "wrapper_port_10020_alive": _wrapper_port_alive(10020),
        "wrapper_port_20020_alive": _wrapper_port_alive(20020),
        "ffprobe_available": ffprobe_ok,
        "go_available": go_ok,
        "mp4box_available": mp4box_ok,
        "media_user_token_len": token_len,
        "queue_busy": JOB_QUEUE.is_busy(),
        "save_folders": folders_check,
    })


# ---------------------------------------------------------------------------
# Diagnostics bundle
# ---------------------------------------------------------------------------
# Sensitive keys whose values we mask before shipping config.yaml inside
# the diagnostics zip. The pattern matches "name:" prefixes and replaces
# everything to the EOL with a fixed placeholder; we intentionally don't
# try to be clever about quoting because the redacted file is only for
# human inspection, not for round-tripping.
_REDACT_KEYS = (
    "media-user-token", "authorization-token",
    "completion-webhook", "library-scan-webhook",
)
_REDACT_RE = re.compile(
    r"^(?P<k>(?:" + "|".join(re.escape(k) for k in _REDACT_KEYS) + r"))\s*:\s*.*$",
    re.MULTILINE,
)


def _redact_config_yaml_text(text: str) -> str:
    return _REDACT_RE.sub(lambda m: f"{m.group('k')}: <redacted>", text)


@app.route("/diagnostics.zip")
def diagnostics_zip():
    """Bundle health.json + redacted config + recent queue/job logs into
    one zip the user can attach when reporting an issue.

    Tokens, webhooks and password-like values are masked before write."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. Health snapshot — call our own /health to keep one source of truth.
        try:
            with app.test_request_context("/health"):
                resp = health()
                health_body = resp.get_data(as_text=True) if hasattr(resp, "get_data") else json.dumps(resp.get_json())
        except Exception as e:  # noqa: BLE001
            health_body = json.dumps({"status": "error", "msg": str(e)})
        zf.writestr("health.json", health_body)

        # 2. Redacted config.yaml.
        if os.path.isfile(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg_text = f.read()
                zf.writestr("config.redacted.yaml", _redact_config_yaml_text(cfg_text))
            except Exception as e:  # noqa: BLE001
                zf.writestr("config.redacted.yaml", f"# read error: {e}\n")
        else:
            zf.writestr("config.redacted.yaml", "# config.yaml not present\n")

        # 3. Queue / job logs — last 200 lines per job, redacted of URLs only
        #    by length (we keep them; they're not secrets, but they ARE
        #    privacy-sensitive, so trim to first 80 chars).
        try:
            snap = JOB_QUEUE.list_jobs()
        except Exception as e:  # noqa: BLE001
            snap = [{"error": str(e)}]
        zf.writestr("queue.json", json.dumps(snap, indent=2, default=str))

        # 4. Wrapper logs (in-memory ring buffer).
        try:
            zf.writestr("wrapper.log", "\n".join(wrapper_logs[-500:]))
        except Exception:  # noqa: BLE001
            pass

        # 5. Recent stdout from the routes module — environment fingerprint.
        env_fp = {
            "PROJECT_DIR": PROJECT_DIR,
            "AMD_DIR": AMD_DIR,
            "CONFIG_PATH": CONFIG_PATH,
            "amd_dir_exists": os.path.isdir(AMD_DIR),
            "config_exists": os.path.isfile(CONFIG_PATH),
            "go": shutil.which("go") or "",
            "ffprobe": shutil.which("ffprobe") or "",
            "os.name": os.name,
        }
        zf.writestr("env.json", json.dumps(env_fp, indent=2))

    buf.seek(0)
    return send_file(
        buf, mimetype="application/zip",
        as_attachment=True, download_name="alac-rip-diagnostics.zip",
    )


def _read_save_folders():
    cfg = {}
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            cfg = {}
    out = {}
    for key, default in (
        ("alac", "AM-DL downloads"),
        ("atmos", "AM-DL-Atmos downloads"),
        ("aac", "AM-DL-AAC downloads"),
        ("mv", "AM-DL-MV downloads"),
    ):
        rel = cfg.get(f"{key}-save-folder") or default
        out[key] = rel if os.path.isabs(rel) else os.path.join(AMD_DIR, rel)
    return out


@app.route("/storage")
def storage():
    """Per-folder size + free-disk gauge data for the home-page widget."""
    folders = _read_save_folders()
    out = {}
    for key, path in folders.items():
        used = 0
        file_count = 0
        if os.path.isdir(path):
            try:
                for root, _, files in os.walk(path):
                    for f in files:
                        try:
                            used += os.path.getsize(os.path.join(root, f))
                            file_count += 1
                        except OSError:
                            pass
            except Exception:
                pass
        try:
            usage = shutil.disk_usage(path) if os.path.isdir(path) else shutil.disk_usage(AMD_DIR)
        except Exception:
            usage = None
        out[key] = {
            "path": path,
            "exists": os.path.isdir(path),
            "used_bytes": used,
            "file_count": file_count,
            "free_bytes": usage.free if usage else None,
            "total_bytes": usage.total if usage else None,
        }
    return jsonify({"status": "ok", "folders": out})


@app.route("/open_folder", methods=["POST"])
def open_folder():
    """Open a save folder server-side via xdg-open / equivalents.

    Only allowed for paths inside one of the configured save folders, to
    avoid turning this into a generic file-manager exploit primitive.
    """
    payload = request.get_json(silent=True) or {}
    target = (payload.get("path") or "").strip()
    if not target:
        return jsonify({"status": "error", "msg": "No path"})
    target = os.path.realpath(target)
    allowed_roots = [os.path.realpath(p) for p in _read_save_folders().values()]
    allowed_roots.append(os.path.realpath(AMD_DIR))
    if not any(target == r or target.startswith(r + os.sep) for r in allowed_roots):
        return jsonify({"status": "error", "msg": "Path outside allowed roots"})
    if not os.path.isdir(target):
        return jsonify({"status": "error", "msg": "Folder does not exist"})
    opener = shutil.which("xdg-open") or shutil.which("open")
    if not opener:
        return jsonify({"status": "error", "msg": "No graphical opener (xdg-open) available on server"})
    try:
        subprocess.Popen([opener, target], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
    return jsonify({"status": "ok"})

@app.route("/stop_wrapper", methods=["POST"])
def stop_wrapper():
    global wrapper_process, wrapper_running, wrapper_logs, wrapper_needs_2fa
    
    if wrapper_process and wrapper_process.poll() is None:
        wrapper_process.terminate()
        wrapper_logs.append("Wrapper process terminated by user")
        wrapper_running = False
        wrapper_needs_2fa = False
        return jsonify({"status": "ok", "msg": "Wrapper stopped"})
    else:
        return jsonify({"status": "error", "msg": "Wrapper not running"})

@app.route("/settings")
def settings():
    return render_template("settings.html")

def _config_missing_msg():
    return (
        f"config.yaml not found at {CONFIG_PATH}. "
        f"Looking from PROJECT_DIR={PROJECT_DIR}. "
        f"Run 'sudo python3 main.py' from {PROJECT_DIR} to install "
        f"apple-music-downloader, or fix the directory layout if it is nested."
    )


@app.route("/get_config")
def get_config():
    # Last-chance bootstrap so the Settings page never sees a bare 404.
    _ensure_config_yaml()
    if not os.path.isfile(CONFIG_PATH):
        return jsonify({"status": "error", "msg": _config_missing_msg()})
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as file:
            config = yaml.safe_load(file) or {}
        return jsonify({"status": "ok", "config": config})
    except Exception as e:
        return jsonify({"status": "error", "msg": f"Failed to read config: {e}"})

@app.route("/save_config", methods=["POST"])
def save_config():
    _ensure_config_yaml()
    if not os.path.isfile(CONFIG_PATH):
        return jsonify({"status": "error", "msg": _config_missing_msg()})
    try:
        config_path = CONFIG_PATH
        # Merge incoming changes over whatever is already on disk so any
        # config keys we don't expose in the UI (e.g. new upstream additions)
        # are preserved across saves instead of silently dropped.
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}
        except Exception:
            existing = {}
        incoming = request.json or {}

        # Defensive guard: never let an empty form submission wipe a
        # token / password / secret that already lives on disk.
        # collectFormData() in settings.html iterates every <input> on
        # save, so if the Media User Token field happened to be empty
        # at submit time the request body carries `media-user-token: ""`.
        # The blind dict.update below would then merge that empty string
        # into config.yaml and brick every token-gated AMD feature
        # (lyrics, AAC-LC, music videos). Drop empty secrets when an
        # existing value is present so the user can never accidentally
        # nuke the token by clicking Save.
        def _looks_secret(k: str) -> bool:
            lk = str(k).lower()
            return any(part in lk for part in ("token", "password", "secret"))

        for key in list(incoming.keys()):
            if not _looks_secret(key):
                continue
            new_val = incoming.get(key)
            if isinstance(new_val, str) and not new_val.strip() and existing.get(key):
                incoming.pop(key, None)

        config_data = dict(existing)
        config_data.update(incoming)

        # Define fields that should be integers
        integer_fields = {
            'alac-max', 'atmos-max', 'limit-max', 'max-memory-limit', 'mv-max'
        }
        
        # Define fields that should be booleans
        boolean_fields = {
            'embed-lrc', 'save-lrc-file', 'save-artist-cover', 'save-animated-artwork',
            'emby-animated-artwork', 'embed-cover', 'get-m3u8-from-device',
            'use-songinfo-for-playlist', 'dl-albumcover-for-playlist',
            'convert-after-download', 'convert-keep-original', 'convert-skip-if-source-matches',
            'tag-sort-order', 'tag-itunes-id', 'alac-fix', 'convert-with-metadata',
            'convert-warn-lossy-to-lossless', 'convert-skip-lossy-to-lossless',
            'convert-check-bad-alac', 'convert-delete-bad-alac',
            'quality-strict-mode', 'skip-if-exists',
        }

        # List-typed fields. The UI sends them as comma-separated strings;
        # we coerce back to a list of ints (or strings) here.
        list_int_fields = {'quality-preference-chain'}
        
        # Define fields that are folder paths and need Windows to WSL translation
        path_fields = {
            'alac-save-folder', 'atmos-save-folder', 'aac-save-folder'
        }
        
        def translate_path_to_wsl(path):
            """Translate Windows paths to WSL paths when saving config"""
            if not path:
                return path
            # Check if it's a Windows-style path (e.g., C:/, D:/)
            if len(path) >= 3 and path[1:3] == ':\\':
                # Convert C:\ to /mnt/c/
                drive = path[0].lower()
                rest = path[3:].replace('\\', '/')
                return f"/mnt/{drive}/{rest}"
            elif len(path) >= 3 and path[1:3] == ':/':
                # Convert C:/ to /mnt/c/
                drive = path[0].lower()
                rest = path[3:]
                return f"/mnt/{drive}/{rest}"
            return path
        
        # Convert data types properly
        for key, value in config_data.items():
            if key in integer_fields:
                try:
                    config_data[key] = int(value) if value else 0
                except (ValueError, TypeError):
                    config_data[key] = 0
            elif key in boolean_fields:
                # Handle boolean conversion
                if isinstance(value, str):
                    config_data[key] = value.lower() in ('true', '1', 'yes', 'on')
                else:
                    config_data[key] = bool(value)
            elif key in list_int_fields:
                # Accept either a real list (already typed by JS) or a
                # comma/space-separated string from a text input.
                if isinstance(value, list):
                    items = value
                else:
                    items = [s for s in str(value or "").replace(",", " ").split() if s]
                cleaned = []
                for s in items:
                    try:
                        cleaned.append(int(s))
                    except (TypeError, ValueError):
                        continue
                config_data[key] = cleaned or [48000, 44100, 96000, 192000]
            elif key in path_fields:
                # Translate Windows paths to WSL format
                config_data[key] = translate_path_to_wsl(str(value))
            # Strings remain as strings (default)
        
        with open(config_path, 'w', encoding='utf-8') as file:
            yaml.dump(config_data, file, default_flow_style=False, allow_unicode=True)

        # Webhook URLs may have changed; push the new values into the queue.
        _refresh_queue_webhooks()
        return jsonify({"status": "ok", "msg": "Configuration saved successfully"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})

@app.route("/check_saved_credentials")
def check_saved_credentials():
    """Check if saved credentials exist"""
    email, password = load_credentials()
    return jsonify({"has_credentials": email is not None, "email": email if email else ""})

@app.route("/delete_saved_credentials", methods=["POST"])
def delete_saved_credentials():
    """Delete saved credentials"""
    if delete_credentials():
        return jsonify({"status": "ok", "msg": "Saved credentials deleted"})
    else:
        return jsonify({"status": "error", "msg": "Failed to delete credentials"})

@app.route("/auto_login", methods=["POST"])
def auto_login():
    """Attempt auto-login with saved credentials"""
    if attempt_auto_login():
        return jsonify({"status": "ok", "msg": "Auto-login started"})
    else:
        return jsonify({"status": "error", "msg": "No saved credentials or login failed"})

@app.route("/get_download_folders")
def get_download_folders():
    """Get download folder paths from config with Windows to WSL path translation"""
    _ensure_config_yaml()
    if not os.path.isfile(CONFIG_PATH):
        return jsonify({"status": "error", "msg": _config_missing_msg()})
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as file:
            config = yaml.safe_load(file)
            
        # Paths are now already in correct format in config file, no need to translate
        folders = {
            "alac": config.get("alac-save-folder", "AM-DL downloads"),
            "atmos": config.get("atmos-save-folder", "AM-DL-Atmos downloads"),
            "aac": config.get("aac-save-folder", "AM-DL-AAC downloads")
        }
        
        return jsonify({"status": "ok", "folders": folders})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)})
