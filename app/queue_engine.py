"""Job queue + background worker for sequential downloads.

This replaces the old single-global-`download_running` lock with a real
FIFO queue so the user can paste an arbitrary number of links and walk
away. One worker processes jobs sequentially (apple-music-downloader is
single-tenant against the wrapper's TCP ports, so parallelism here would
just race), but the queue itself supports cancel, retry, reorder, and
SSE event subscribers for live UI updates.

Persistence is in-memory only for now; queue state survives page reloads
but not Flask restarts. Adding SQLite persistence is a one-line swap of
`_save()` / `_load()` if you want it later.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from .scanner import ScanResult, pick_alac_target

# ---------------------------------------------------------------------------
# Job model
# ---------------------------------------------------------------------------
JOB_STATES = ("queued", "running", "done", "failed", "canceled", "skipped")


@dataclass
class Job:
    id: str
    url: str
    format: str                  # 'ALAC' | 'ATMOS' | 'AAC'
    # Optional siblings appended to the same AMD invocation. Used when
    # one logical "album with N audio tracks + 1 music video" is split
    # into per-track /song/ URLs but presented as a single queue card.
    # AMD's main loop iterates over all positional URL args in one Go
    # process, so this is the most efficient way to do the MV skip
    # without touching AMD source.
    extra_urls: list = field(default_factory=list)
    requested_max: int = 0       # alac sample rate cap (Hz); ignored for ATMOS/AAC
    requested_atmos_max: int = 0 # atmos bitrate cap; e.g. 2768 or 2448; 0 = use config.yaml
    requested_aac_type: str = "" # aac-lc | aac | aac-binaural | aac-downmix; "" = use config.yaml
    lyrics_mode: str = ""        # "" = leave config.yaml alone, "standard" = lyrics, "karaoke" = syllable-lyrics+ttml
    fallback_chain: list = field(default_factory=list)
    strict: bool = False
    skip_if_exists: bool = False
    metadata: dict = field(default_factory=dict)
    status: str = "queued"
    log_lines: list = field(default_factory=list)
    quality_summary: list = field(default_factory=list)
    # Structured per-track ffprobe output. Each entry is a dict with
    # keys filename, path, codec, container, sample_rate_hz, bits_per_sample,
    # channels, bit_rate_kbps, duration_s, size_bytes. Drives the home-page
    # Library panel.
    tracks: list = field(default_factory=list)
    save_folder: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    exit_code: Optional[int] = None
    error: str = ""
    progress: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        # Trim log lines for JSON payloads
        d["log_lines"] = self.log_lines[-200:]
        return d


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------
class JobQueue:
    """Thread-safe job queue with one background worker."""

    def __init__(
        self,
        amd_dir: str,
        resolve_save_folder: Callable[[str, bool], str],
        ffprobe_summary: Callable[[str, list], None],
        env_extras: Optional[dict] = None,
    ):
        self.amd_dir = amd_dir
        self._resolve_save_folder = resolve_save_folder
        self._ffprobe_summary = ffprobe_summary
        self._env_extras = env_extras or {}

        self._lock = threading.RLock()
        self._jobs: dict = {}             # id -> Job
        self._order: list = []            # ordered list of job ids
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._current_id: Optional[str] = None
        self._current_proc: Optional[subprocess.Popen] = None
        # Set by cancel() so the readline loop and finaliser can tell a
        # user-initiated kill apart from a natural exit. Reset at the
        # start of every _run_job.
        self._cancel_requested: bool = False
        self._subscribers: list = []       # list[queue.Queue]
        self._completion_webhook: str = ""
        self._library_scan_webhook: str = ""

        self._worker = threading.Thread(target=self._loop, name="JobQueue", daemon=True)
        self._worker.start()

    # ----- subscriber interface (SSE) ----------------------------------
    def subscribe(self) -> "queue.Queue":
        q: "queue.Queue" = queue.Queue(maxsize=1024)
        with self._lock:
            self._subscribers.append(q)
        # Send a snapshot so a freshly-connected client sees current state.
        q.put({"type": "snapshot", "jobs": self._snapshot()})
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def _emit(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for s in subs:
            try:
                s.put_nowait(event)
            except queue.Full:
                # Drop events on slow consumers rather than block the worker.
                pass

    def _terminal_logs_enabled(self) -> bool:
        value = os.environ.get("ALAC_RIP_TERMINAL_LOGS", "1").strip().lower()
        return value not in ("0", "false", "no", "off")

    def _print_job_lines(self, job: Job, lines: list) -> None:
        if not self._terminal_logs_enabled():
            return
        prefix = f"[AMD {job.id[:8]}]"
        for line in lines:
            print(f"{prefix} {line}", flush=True)

    # ----- runtime config (set from routes) ----------------------------
    def set_webhooks(self, completion: str, library_scan: str) -> None:
        with self._lock:
            self._completion_webhook = completion or ""
            self._library_scan_webhook = library_scan or ""

    # ----- lifecycle ---------------------------------------------------
    def shutdown(self) -> None:
        self._shutdown.set()
        self._wake.set()
        # Kill the running download if any; the worker will mark it canceled.
        with self._lock:
            proc = self._current_proc
        self._terminate_proc(proc)

    @staticmethod
    def _terminate_proc(proc: Optional[subprocess.Popen]) -> None:
        """Kill a running download AND any child processes it forked.

        `go run` spawns a compiled binary as a separate process; on
        POSIX that child survives a plain proc.terminate() because it
        was reparented out of the shell stub. To actually stop the
        download we have to signal the whole process group, which only
        works because we Popen with start_new_session=True below.
        Escalates SIGTERM -> SIGKILL after 3s in case the child is
        blocked in a syscall (DRM negotiation, network fetch, etc.).
        """
        if not proc or proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.terminate()
            else:
                proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        proc.kill()
                else:
                    proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # ----- enqueue -----------------------------------------------------
    def enqueue(self, jobs: list) -> list:
        """Append validated `Job`s to the queue and emit events. Returns ids."""
        ids = []
        with self._lock:
            for j in jobs:
                if not isinstance(j, Job):
                    continue
                self._jobs[j.id] = j
                self._order.append(j.id)
                ids.append(j.id)
                self._emit({"type": "job_added", "job": j.to_dict()})
        self._wake.set()
        return ids

    # ----- cancel / retry ---------------------------------------------
    def cancel(self, job_id: str) -> bool:
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return False
            if j.status == "queued":
                j.status = "canceled"
                j.finished_at = time.time()
                j.error = "Canceled before start"
                self._emit({"type": "job_updated", "job": j.to_dict()})
                return True
            if j.status == "running" and job_id == self._current_id and self._current_proc:
                # Mark intent so the worker loop knows the readline EOF
                # is our doing, then kill the entire AMD process group
                # (start_new_session=True at Popen). The reader loop
                # also checks _cancel_requested so it exits promptly
                # without waiting for the OS to flush the pipe.
                j.error = "Canceled by user"
                self._cancel_requested = True
                proc_to_kill = self._current_proc
                self._emit({"type": "job_updated", "job": j.to_dict()})
            else:
                proc_to_kill = None
        # Do the actual kill outside the lock since _terminate_proc
        # may block on wait(3s).
        if proc_to_kill is not None:
            self._terminate_proc(proc_to_kill)
            return True
        return False

    def retry(self, job_id: str) -> Optional[str]:
        """Clone a failed/canceled job back to the tail of the queue."""
        with self._lock:
            old = self._jobs.get(job_id)
            if not old or old.status not in ("failed", "canceled", "skipped"):
                return None
            new = Job(
                id=str(uuid.uuid4()),
                url=old.url,
                format=old.format,
                extra_urls=list(old.extra_urls),
                requested_max=old.requested_max,
                requested_atmos_max=old.requested_atmos_max,
                requested_aac_type=old.requested_aac_type,
                lyrics_mode=old.lyrics_mode,
                fallback_chain=list(old.fallback_chain),
                strict=old.strict,
                skip_if_exists=old.skip_if_exists,
                metadata=dict(old.metadata),
            )
            self._jobs[new.id] = new
            self._order.append(new.id)
            self._emit({"type": "job_added", "job": new.to_dict()})
        self._wake.set()
        return new.id

    def remove(self, job_id: str) -> bool:
        """Permanently remove a finished job from the visible list."""
        with self._lock:
            j = self._jobs.get(job_id)
            if not j or j.status in ("running", "queued"):
                return False
            self._jobs.pop(job_id, None)
            try:
                self._order.remove(job_id)
            except ValueError:
                pass
            self._emit({"type": "job_removed", "id": job_id})
            return True

    def reprobe(self, job_id: str) -> Optional[list]:
        """Re-run ffprobe over a finished job's save folder. Returns new
        summary lines, or None if not applicable."""
        with self._lock:
            j = self._jobs.get(job_id)
            if not j or j.status != "done" or not j.save_folder:
                return None
        summary = []
        tracks = []
        try:
            self._ffprobe_summary(j.save_folder, summary, since=j.started_at, tracks_out=tracks)
        except Exception as e:  # noqa: BLE001
            summary.append(f"ffprobe re-probe failed: {e}")
        with self._lock:
            j.quality_summary = summary
            j.tracks = tracks
            self._emit({"type": "job_updated", "job": j.to_dict()})
        return summary

    # ----- introspection ----------------------------------------------
    def _snapshot(self) -> list:
        return [self._jobs[i].to_dict() for i in self._order if i in self._jobs]

    def list_jobs(self) -> list:
        with self._lock:
            return self._snapshot()

    def get(self, job_id: str) -> Optional[dict]:
        with self._lock:
            j = self._jobs.get(job_id)
            return j.to_dict() if j else None

    def is_busy(self) -> bool:
        with self._lock:
            return self._current_id is not None

    # ----- worker loop -------------------------------------------------
    def _loop(self) -> None:
        while not self._shutdown.is_set():
            job = self._dequeue_next()
            if job is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            try:
                self._run_job(job)
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    job.status = "failed"
                    job.error = f"Worker exception: {e}"
                    job.finished_at = time.time()
                    self._emit({"type": "job_updated", "job": job.to_dict()})
            finally:
                with self._lock:
                    self._current_id = None
                    self._current_proc = None

    def _dequeue_next(self) -> Optional[Job]:
        with self._lock:
            for jid in self._order:
                j = self._jobs.get(jid)
                if j and j.status == "queued":
                    j.status = "running"
                    j.started_at = time.time()
                    self._current_id = j.id
                    self._emit({"type": "job_updated", "job": j.to_dict()})
                    return j
        return None

    def _run_job(self, job: Job) -> None:
        # Fresh run — clear any cancel intent left over from a previous job.
        with self._lock:
            self._cancel_requested = False

        # ----- 1. Build downloader command -----
        # Upstream apple-music-downloader exposes --alac-max, --atmos-max,
        # and --aac-type as CLI flags that override the values in
        # config.yaml (see main.go). Passing them per-job is the only way
        # to make the UI's quality cap deterministic; otherwise the
        # downloader silently falls back to whatever sits in config.yaml
        # and the user gets 192 kHz when they asked for 48.
        cmd = ["go", "run", "main.go"]
        if job.format == "ATMOS":
            cmd.append("--atmos")
            if job.requested_atmos_max > 0:
                cmd.extend(["--atmos-max", str(job.requested_atmos_max)])
        elif job.format == "AAC":
            cmd.append("--aac")
            if job.requested_aac_type:
                cmd.extend(["--aac-type", job.requested_aac_type])
        else:  # ALAC
            if job.requested_max > 0:
                cmd.extend(["--alac-max", str(job.requested_max)])
        # Primary URL + any siblings. AMD's main loop walks every
        # positional URL arg inside a single Go process: no recompile,
        # no re-auth, no re-init between tracks. This is what makes
        # the "one album card, N audio tracks" UI honest under the
        # hood.
        cmd.append(job.url)
        if job.extra_urls:
            cmd.extend(job.extra_urls)

        # ----- 2. Resolve save folder -----
        special = job.format in ("ATMOS", "AAC")
        save_folder = self._resolve_save_folder(job.format, special)
        with self._lock:
            job.save_folder = save_folder

        # ----- 3. Skip-if-exists short-circuit -----
        # Heuristic: if the album folder already contains audio files, skip.
        if job.skip_if_exists and save_folder and os.path.isdir(save_folder):
            audio_exts = {".m4a", ".mp4", ".alac", ".flac", ".mp3"}
            existing = []
            for root, _, files in os.walk(save_folder):
                for f in files:
                    if os.path.splitext(f)[1].lower() in audio_exts:
                        existing.append(os.path.join(root, f))
                        if len(existing) >= 1:
                            break
                if existing:
                    break
            if existing:
                with self._lock:
                    job.status = "skipped"
                    job.finished_at = time.time()
                    job.log_lines.append(
                        f"⏭ Skip-if-exists: found {len(existing)} existing file(s) under {save_folder}"
                    )
                    self._print_job_lines(job, job.log_lines[-1:])
                    self._emit({"type": "job_updated", "job": job.to_dict()})
                return

        # ----- 4. Disk space pre-check -----
        try:
            free = shutil.disk_usage(save_folder if os.path.isdir(save_folder) else self.amd_dir).free
            min_required = 500 * 1024 * 1024  # 500 MB safety floor; album-sized
            if free < min_required:
                with self._lock:
                    job.status = "failed"
                    job.error = f"Less than 500 MB free on {save_folder}"
                    job.finished_at = time.time()
                    self._emit({"type": "job_updated", "job": job.to_dict()})
                return
        except Exception:  # noqa: BLE001
            pass

        # ----- 5. Lyrics-mode patch (best-effort) -----
        # The downloader has no CLI flag for lrc-type, so karaoke / standard
        # lyrics get applied by patching config.yaml in-place around the
        # job. The worker is single-threaded, so this is race-free.
        lyrics_undo = self._apply_lyrics_patch(job)

        with self._lock:
            job.log_lines.append(f"› Working directory: {self.amd_dir}")
            job.log_lines.append(f"› Save folder: {save_folder}")
            job.log_lines.append(f"› Format: {job.format}")
            if job.format == "ALAC" and job.requested_max:
                job.log_lines.append(
                    f"› ALAC cap: --alac-max {job.requested_max} "
                    f"({job.requested_max // 1000} kHz; chain {job.fallback_chain})"
                )
            if job.format == "ATMOS" and job.requested_atmos_max:
                job.log_lines.append(f"› Atmos cap: --atmos-max {job.requested_atmos_max}")
            if job.format == "AAC" and job.requested_aac_type:
                job.log_lines.append(f"› AAC type: --aac-type {job.requested_aac_type}")
            if job.lyrics_mode == "karaoke":
                job.log_lines.append("› Lyrics: karaoke (syllable-synced TTML)")
            elif job.lyrics_mode == "standard":
                job.log_lines.append("› Lyrics: standard LRC")
            redacted_cmd = " ".join(cmd)
            job.log_lines.append(f"› Executing: {redacted_cmd}")
            self._emit({"type": "job_log", "id": job.id, "lines": job.log_lines[-6:]})
            self._print_job_lines(job, job.log_lines[-6:])

        # ----- 6. Spawn process -----
        # stdin=DEVNULL: AMD's `Error detected, press Enter to try
        # again...` prompt would otherwise block indefinitely on our
        # inherited stdin. With DEVNULL the prompt's Scanln gets EOF
        # immediately. We *also* watch for the prompt in the readline
        # loop and kill the run on match so the queue moves on to the
        # next album instead of waiting for the prompt to keep
        # repeating.
        #
        # start_new_session=True (POSIX): puts AMD and its `go run`
        # child in a fresh process group so cancel() can
        # os.killpg(SIGTERM) the whole tree. Without it, terminating
        # the shell stub leaves the compiled Go binary still running.
        env = os.environ.copy()
        env.update(self._env_extras)
        popen_kwargs = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=1,
            universal_newlines=True,
            cwd=self.amd_dir,
            env=env,
        )
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except Exception as e:  # noqa: BLE001
            with self._lock:
                job.status = "failed"
                job.error = f"Failed to spawn downloader: {e}"
                job.finished_at = time.time()
                self._emit({"type": "job_updated", "job": job.to_dict()})
            return

        with self._lock:
            self._current_proc = proc

        # ----- 7. Stream logs with hang detector + retry-prompt skip -----
        last_line_at = time.time()
        HANG_TIMEOUT = 600  # 10 min of silence → kill
        hung = False
        saw_retry_prompt = False

        def kill_if_hung():
            nonlocal hung
            while proc.poll() is None and not self._shutdown.is_set():
                time.sleep(15)
                if time.time() - last_line_at > HANG_TIMEOUT:
                    with self._lock:
                        job.log_lines.append(
                            f"⚠ No output for {HANG_TIMEOUT}s — killing process"
                        )
                        self._print_job_lines(job, job.log_lines[-1:])
                        self._emit({"type": "job_log", "id": job.id, "lines": job.log_lines[-1:]})
                    self._terminate_proc(proc)
                    hung = True
                    break

        threading.Thread(target=kill_if_hung, daemon=True).start()

        try:
            for raw in iter(proc.stdout.readline, ""):
                if self._shutdown.is_set() or self._cancel_requested:
                    break
                line = raw.rstrip()
                if not line:
                    continue
                last_line_at = time.time()
                # Watch for AMD's per-track unrecoverable-error prompt.
                # When this fires for a region-locked / DRM-failed track
                # AMD repeats it forever waiting for stdin, freezing
                # the whole queue. Kill the process group instead so
                # the next album in the queue gets a turn.
                if "Error detected, press Enter to try again" in line:
                    saw_retry_prompt = True
                    with self._lock:
                        job.log_lines.append(line)
                        job.log_lines.append(
                            "⚠ AMD hit an unrecoverable per-track error — stopping this job and moving to the next one."
                        )
                        self._print_job_lines(job, job.log_lines[-2:])
                        self._emit({"type": "job_log", "id": job.id, "lines": job.log_lines[-2:]})
                    self._terminate_proc(proc)
                    break
                with self._lock:
                    job.log_lines.append(line)
                    self._print_job_lines(job, [line])
                    self._emit({"type": "job_log", "id": job.id, "lines": [line]})
        except Exception as e:  # noqa: BLE001
            with self._lock:
                job.log_lines.append(f"Error reading log stream: {e}")
                self._print_job_lines(job, job.log_lines[-1:])

        # If we're cancelling, the reader exits early but AMD might
        # still be alive; idempotent re-kill.
        if self._cancel_requested:
            self._terminate_proc(proc)

        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        # Bounded wait so a stuck zombie can't deadlock the worker.
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._terminate_proc(proc)
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                rc = -1

        # ----- 8. Finalise job -----
        with self._lock:
            job.exit_code = rc
            job.finished_at = time.time()
            if self._cancel_requested or job.error == "Canceled by user":
                job.status = "canceled"
            elif hung:
                job.status = "failed"
                job.error = job.error or "Process hung (no output for 10 min)"
            elif saw_retry_prompt:
                # Treat a per-track unrecoverable error as a partial
                # success: AMD likely already wrote earlier tracks to
                # disk before hitting the broken one. ffprobe summary
                # still runs below.
                job.status = "done"
                job.error = ""
                summary = []
                tracks = []
                try:
                    self._ffprobe_summary(save_folder, summary, since=job.started_at, tracks_out=tracks)
                except Exception as e:  # noqa: BLE001
                    summary.append(f"ffprobe failed: {e}")
                job.quality_summary = summary
                job.tracks = tracks
                if summary:
                    self._print_job_lines(job, summary)
                    self._emit({"type": "job_log", "id": job.id, "lines": summary})
            elif rc == 0:
                job.status = "done"
                # ffprobe summary (log lines + structured per-track dicts)
                summary = []
                tracks = []
                try:
                    self._ffprobe_summary(save_folder, summary, since=job.started_at, tracks_out=tracks)
                except Exception as e:  # noqa: BLE001
                    summary.append(f"ffprobe failed: {e}")
                job.quality_summary = summary
                job.tracks = tracks
                if summary:
                    self._print_job_lines(job, summary)
                    self._emit({"type": "job_log", "id": job.id, "lines": summary})
            else:
                job.status = "failed"
                if not job.error:
                    job.error = f"Downloader exited with code {rc}"
            self._emit({"type": "job_updated", "job": job.to_dict()})

        # ----- 9. Restore lyrics patch (always, even on failure) -----
        if lyrics_undo:
            try:
                lyrics_undo()
            except Exception:  # noqa: BLE001
                pass

        # ----- 10. Webhooks (best-effort, non-blocking-ish) -----
        if job.status == "done":
            self._fire_webhooks(job)

    # ----- lyrics-mode helpers -------------------------------------------
    def _apply_lyrics_patch(self, job: Job) -> Optional[Callable[[], None]]:
        """Patch config.yaml to enable the requested lyrics mode for the
        duration of this job. Returns an undo callable, or None if no
        patch was applied.

        We do a minimal in-place edit (not a YAML round-trip) so the user
        keeps their comments and field ordering. If the lines are absent
        we append them.
        """
        if not job.lyrics_mode:
            return None
        cfg = os.path.join(self.amd_dir, "config.yaml")
        if not os.path.isfile(cfg):
            return None

        if job.lyrics_mode == "karaoke":
            patches = {
                "lrc-type": "syllable-lyrics",
                "lrc-format": "ttml",
                "embed-lrc": "true",
                "save-lrc-file": "true",
            }
        elif job.lyrics_mode == "standard":
            patches = {
                "lrc-type": "lyrics",
                "lrc-format": "lrc",
                "embed-lrc": "true",
                "save-lrc-file": "false",
            }
        else:
            return None

        try:
            with open(cfg, "r", encoding="utf-8") as fh:
                original = fh.read()
        except Exception:  # noqa: BLE001
            return None

        new_text = original
        for key, value in patches.items():
            # Match a top-level key (no leading whitespace) followed by ':'.
            # The replacement preserves any inline comment on the line.
            pattern = re.compile(
                rf"^({re.escape(key)})\s*:\s*\"?[^#\n\r\"]*\"?(\s*(?:#.*)?)$",
                re.MULTILINE,
            )
            if pattern.search(new_text):
                new_text = pattern.sub(rf'\1: "{value}"\2', new_text)
            else:
                # Append at end if the key wasn't there.
                if not new_text.endswith("\n"):
                    new_text += "\n"
                new_text += f'{key}: "{value}"\n'

        if new_text == original:
            return None

        try:
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write(new_text)
        except Exception:  # noqa: BLE001
            return None

        def _undo() -> None:
            try:
                with open(cfg, "w", encoding="utf-8") as fh:
                    fh.write(original)
            except Exception:  # noqa: BLE001
                pass

        return _undo

    def _fire_webhooks(self, job: Job) -> None:
        completion = self._completion_webhook
        scan_url = self._library_scan_webhook

        def _post(url: str, payload: dict) -> None:
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    r.read()
            except Exception:  # noqa: BLE001
                pass

        if completion:
            payload = {
                "id": job.id,
                "url": job.url,
                "format": job.format,
                "status": job.status,
                "save_folder": job.save_folder,
                "metadata": job.metadata,
                "quality_summary": job.quality_summary,
                "duration_s": round(job.finished_at - job.started_at, 1),
            }
            threading.Thread(target=_post, args=(completion, payload), daemon=True).start()

        if scan_url:
            # Plex/Jellyfin-style refresh URL; usually GET, but POST is fine
            # for HASS shortcuts. Try GET first via urlopen; the body POST
            # variant above is also harmless.
            def _get():
                try:
                    req = urllib.request.Request(scan_url, headers={"User-Agent": "alac-rip"})
                    with urllib.request.urlopen(req, timeout=8) as r:
                        r.read()
                except Exception:  # noqa: BLE001
                    pass

            threading.Thread(target=_get, daemon=True).start()


# ---------------------------------------------------------------------------
# Helpers exposed to routes.py
# ---------------------------------------------------------------------------
def build_job_from_scan(
    scan: ScanResult,
    desired_format: str,            # 'ALAC' | 'ATMOS' | 'AAC'
    fallback_chain: list,
    strict: bool,
    skip_if_exists: bool,
    override_max_hz: int = 0,
    override_atmos_max: int = 0,
    override_aac_type: str = "",
    lyrics_mode: str = "",
) -> Job:
    """Materialise a Job from a ScanResult and the user's preferences.

    `override_max_hz` lets the UI pin a specific ALAC tier per item
    (overrides the auto-pick from the fallback chain).
    `override_atmos_max` / `override_aac_type` do the same for the other
    formats. `lyrics_mode` triggers a config.yaml patch around the job.
    """
    requested = 0
    if desired_format == "ALAC":
        requested = override_max_hz or pick_alac_target(scan, fallback_chain)
        if requested == 0:
            # No lossless available → silently fall back to AAC.
            desired_format = "AAC"
    return Job(
        id=str(uuid.uuid4()),
        url=scan.url,
        format=desired_format,
        requested_max=requested,
        requested_atmos_max=int(override_atmos_max or 0),
        requested_aac_type=str(override_aac_type or ""),
        lyrics_mode=str(lyrics_mode or ""),
        fallback_chain=list(fallback_chain or []),
        strict=bool(strict),
        skip_if_exists=bool(skip_if_exists),
        metadata={
            "title": scan.title,
            "artist": scan.artist,
            "track_count": scan.track_count,
            "artwork_url": scan.artwork_url,
            "audio_traits": scan.audio_traits,
            "max_alac_hz": scan.max_alac_hz,
            "available_tiers": list(scan.available_tiers),
            "kind": scan.kind,
        },
    )
