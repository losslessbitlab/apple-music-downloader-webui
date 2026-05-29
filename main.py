import argparse
import hashlib
import os
import shutil
import signal
import socket
import subprocess
import threading
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
import sys
import tarfile

PROJECT_DIR = Path(__file__).resolve().parent
BENTO4_DIR = PROJECT_DIR / "bento4"
WRAPPER_DIR = PROJECT_DIR / "wrapper"
AMD_DIR = PROJECT_DIR / "apple-music-downloader"

# Project-local Python virtualenv. Required because modern Debian/Ubuntu mark
# the system Python as PEP 668 "externally-managed" and refuse pip installs.
VENV_DIR = PROJECT_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"

WRAPPER_URL = "https://github.com/WorldObservationLog/wrapper/releases/download/wrapper.x86_64.latest/Wrapper.x86_64.latest.zip"
AMD_REPO_URL = "https://github.com/zhaarey/apple-music-downloader"

# --- Go toolchain --------------------------------------------------------
# Pinned to the current Go stable release. Distro-shipped 'golang-go' on
# Debian 12 / Ubuntu 22.04 is too old for apple-music-downloader's go.mod
# (which requires go 1.23.1+), so we install Go directly from go.dev with
# SHA-256 verification.
GO_VERSION = "1.26.3"
GO_TARBALL = f"go{GO_VERSION}.linux-amd64.tar.gz"
GO_URL = f"https://go.dev/dl/{GO_TARBALL}"
# SHA-256 from https://go.dev/dl/?mode=json (verified at pin time)
GO_SHA256 = "2b2cfc7148493da5e73981bffbf3353af381d5f93e789c82c79aff64962eb556"
GO_INSTALL_DIR = Path("/usr/local/go")


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# Some hosts (notably bok.net, which serves Bento4 binaries) return HTTP 403
# to the default Python-urllib User-Agent. Send a real browser UA instead.
_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _download(url, dest, expected_sha256=None, timeout=60):
    """Download `url` to `dest`, optionally verifying SHA-256.
    Streams to a .part temp file and atomically renames on success so a
    partial download can never poison subsequent runs."""
    print(f"Downloading {url} ...")
    dest = Path(dest)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": _DEFAULT_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f, length=1024 * 1024)
        os.replace(tmp, dest)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
    if expected_sha256:
        actual = _sha256_file(dest)
        if actual.lower() != expected_sha256.lower():
            try:
                os.remove(dest)
            except OSError:
                pass
            raise RuntimeError(
                f"SHA-256 mismatch for {url}\n  expected: {expected_sha256}\n  got:      {actual}"
            )
        print(f"SHA-256 verified: {expected_sha256}")


def install_go(force=False):
    """Install Go toolchain from the official tarball with SHA-256
    verification. If force=True or the installed version differs, replace it."""
    go_bin = GO_INSTALL_DIR / "bin" / "go"
    needs_install = True

    if go_bin.exists():
        try:
            installed = subprocess.check_output([str(go_bin), "version"], text=True).strip()
            print(f"Existing Go install: {installed}")
            if GO_VERSION in installed and not force:
                print("INFO: Go already at target version, skipping install")
                needs_install = False
        except Exception as e:
            print(f"WARN: could not read existing Go version: {e}")

    if not needs_install:
        return

    if GO_INSTALL_DIR.exists():
        print(f"Removing existing Go install at {GO_INSTALL_DIR}")
        shutil.rmtree(GO_INSTALL_DIR)

    tarball = PROJECT_DIR / GO_TARBALL
    _download(GO_URL, tarball, expected_sha256=GO_SHA256)
    print("Extracting Go to /usr/local ...")
    GO_INSTALL_DIR.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as tar:
        # Tarball ships a top-level 'go/' directory which extracts to /usr/local/go
        tar.extractall("/usr/local")
    os.remove(tarball)

    # Symlink go + gofmt into /usr/local/bin so 'go' is on PATH for any shell
    for tool in ("go", "gofmt"):
        link = Path("/usr/local/bin") / tool
        target = GO_INSTALL_DIR / "bin" / tool
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            os.symlink(target, link)
            print(f"Symlinked {link} -> {target}")
        except Exception as e:
            print(f"WARN: failed to symlink {tool}: {e}")

    print(f"Go {GO_VERSION} installed")


def ensure_venv():
    """Create a project-local venv at .venv/ if it doesn't already exist.
    Required on PEP 668 systems (modern Debian/Ubuntu) where the system
    Python refuses pip installs."""
    if VENV_PYTHON.exists():
        print(f"INFO: venv already exists at {VENV_DIR}, reusing")
        return
    print(f"Creating Python venv at {VENV_DIR} ...")
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", "--upgrade-deps", str(VENV_DIR)],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        # Most common cause: 'python3-venv' apt package missing.
        raise RuntimeError(
            f"Failed to create venv: {e}. On Debian/Ubuntu install 'python3-venv' first."
        )
    print("venv created.")


def install_python_deps():
    """Install pinned Python deps from requirements.txt into the project venv."""
    req = PROJECT_DIR / "requirements.txt"
    if not req.exists():
        print("WARN: requirements.txt not found, skipping pip install")
        return
    ensure_venv()
    cmd = [str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "-r", str(req)]
    print(f"Installing Python dependencies into venv: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def install_wrapper(force=False):
    """Download and extract the wrapper binary. If force=True, remove the
    existing directory first so the latest release is always re-fetched."""
    wrapper_zip = PROJECT_DIR / "wrapper.x86_64.zip"

    if WRAPPER_DIR.exists():
        if force:
            print(f"Force update: removing existing wrapper at {WRAPPER_DIR}")
            shutil.rmtree(WRAPPER_DIR)
        else:
            print("INFO: Wrapper already exists, skipping download")
            return

    print(f"Downloading wrapper from {WRAPPER_URL}...")
    _download(WRAPPER_URL, wrapper_zip)
    print("Extracting wrapper...")

    WRAPPER_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wrapper_zip, "r") as zip_ref:
        zip_ref.extractall(WRAPPER_DIR)
    os.remove(wrapper_zip)

    # Ensure the wrapper binary is executable (running as root, so no sudo needed)
    wrapper_bin = WRAPPER_DIR / "wrapper"
    try:
        if wrapper_bin.exists():
            current_mode = wrapper_bin.stat().st_mode
            wrapper_bin.chmod(current_mode | 0o755)
            print("Set execute permission on wrapper binary")
        else:
            print("WARN: Wrapper binary not found after extraction")
    except Exception as e:
        print(f"WARN: Failed to chmod wrapper binary: {e}")

    print("Wrapper extracted inside project folder")


def install_downloader(force=False):
    """Clone the apple-music-downloader repo. If force=True and the repo
    already exists, fetch and hard-reset to the latest upstream commit."""
    if AMD_DIR.exists():
        if force:
            print(f"Force update: pulling latest changes for downloader at {AMD_DIR}")
            try:
                subprocess.run(["git", "-C", str(AMD_DIR), "fetch", "--all", "--prune"], check=True)
                # Reset to whatever the remote default branch is
                subprocess.run(
                    ["git", "-C", str(AMD_DIR), "reset", "--hard", "origin/HEAD"],
                    check=True,
                )
                print("Apple Music Downloader updated to latest")
                return
            except subprocess.CalledProcessError as e:
                print(f"WARN: git update failed ({e}); falling back to fresh clone")
                shutil.rmtree(AMD_DIR)
                # fall through to clone below
        else:
            print("INFO: Apple Music Downloader already exists, skipping clone")
            return

    print("Cloning Apple Music Downloader...")
    subprocess.run(["git", "clone", AMD_REPO_URL, str(AMD_DIR)], check=True)
    print("Apple Music Downloader cloned inside project folder")

    # Upstream ships only `config.yaml.example`; the Go binary expects
    # `config.yaml`. Materialise it now so the Settings page is usable
    # immediately after first install.
    _bootstrap_amd_config_yaml()


def _bootstrap_amd_config_yaml():
    """Make sure `apple-music-downloader/config.yaml` exists.

    Strategy: copy from `config.yaml.example` (shipped by upstream) if
    available, otherwise from our bundled `app/default_config.yaml` (which
    mirrors the upstream example). Existing `config.yaml` is never
    overwritten.
    """
    target = AMD_DIR / "config.yaml"
    if target.is_file():
        return
    AMD_DIR.mkdir(parents=True, exist_ok=True)
    upstream_example = AMD_DIR / "config.yaml.example"
    bundled_default = PROJECT_DIR / "app" / "default_config.yaml"
    src = None
    if upstream_example.is_file():
        src = upstream_example
    elif bundled_default.is_file():
        src = bundled_default
    if src is None:
        print("WARN: no config.yaml.example or bundled default found; "
              "Settings page will require manual config")
        return
    shutil.copyfile(src, target)
    print(f"Created {target} from {src.name}")


def _apt_pkg_has_candidate(pkg_name: str) -> bool:
    """Return True if apt knows a concrete install candidate for `pkg_name`.

    On newer Ubuntu releases some packages (e.g. gpac) may exist in docs or
    older guides but have Candidate: (none) in the enabled repos.
    """
    try:
        out = subprocess.check_output(
            ["apt-cache", "policy", pkg_name],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except Exception as e:
        print(f"WARN: failed to query apt metadata for {pkg_name}: {e}")
        return False

    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Candidate:"):
            candidate = line.split(":", 1)[1].strip()
            return bool(candidate and candidate != "(none)")
    return False


def _try_enable_universe() -> bool:
    """Best-effort enable of Ubuntu's 'universe' apt component (home of gpac).

    Installs software-properties-common if add-apt-repository is missing, then
    enables universe and refreshes apt. Returns True only if the repo step ran.
    """
    if shutil.which("add-apt-repository") is None:
        try:
            subprocess.run(["apt-get", "install", "-y", "software-properties-common"], check=True)
        except subprocess.CalledProcessError as e:
            print(f"WARN: could not install software-properties-common: {e}")
            return False
    if shutil.which("add-apt-repository") is None:
        return False
    try:
        subprocess.run(["add-apt-repository", "-y", "universe"], check=True)
        subprocess.run(["apt-get", "update"], check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"WARN: could not enable 'universe' repo: {e}")
        return False


def _install_system_packages():
    """Install system deps with release-aware optional-package handling.

    Required packages must resolve and install; optional packages are installed
    when available and otherwise skipped with a warning.
    """
    if shutil.which("apt-get") is None or shutil.which("apt-cache") is None:
        raise RuntimeError(
            "Automatic dependency install currently supports apt-based systems "
            "(apt-get + apt-cache) only. Install dependencies manually and rerun."
        )

    required_pkgs = [
        "git", "ffmpeg", "wget", "ca-certificates", "python3-pip", "python3-venv",
    ]
    optional_pkgs = [
        # Not shipped in some newer Ubuntu releases/repo combinations.
        "gpac",
    ]

    missing_required = [p for p in required_pkgs if not _apt_pkg_has_candidate(p)]
    if missing_required:
        raise RuntimeError(
            "Required package(s) unavailable in current apt repos: "
            f"{', '.join(missing_required)}. "
            "Check your distro repositories and run apt-get update."
        )

    available_optional = [p for p in optional_pkgs if _apt_pkg_has_candidate(p)]
    skipped_optional = [p for p in optional_pkgs if p not in available_optional]

    print(f"Installing required system packages: {', '.join(required_pkgs)}")
    subprocess.run(["apt-get", "install", "-y", *required_pkgs], check=True)

    # gpac provides MP4Box, used by the downloader to embed/mux some outputs
    # (music videos, animated artwork). It lives in Ubuntu's 'universe'
    # component, which may be disabled. If it's not currently installable, try
    # enabling 'universe' and re-checking before giving up so embedding works
    # out of the box.
    if "gpac" in skipped_optional:
        print("gpac (MP4Box) not found in enabled repos; trying to enable 'universe'...")
        if _try_enable_universe() and _apt_pkg_has_candidate("gpac"):
            available_optional.append("gpac")
            skipped_optional.remove("gpac")

    if available_optional:
        print(f"Installing optional system packages: {', '.join(available_optional)}")
        try:
            subprocess.run(["apt-get", "install", "-y", *available_optional], check=True)
        except subprocess.CalledProcessError as e:
            print(
                "WARN: optional package installation failed "
                f"({', '.join(available_optional)}): {e}. Continuing."
            )

    if skipped_optional:
        print(
            "WARN: optional package(s) not available in enabled repos: "
            f"{', '.join(skipped_optional)}. Continuing without them."
        )
        if "gpac" in skipped_optional:
            print(
                "  NOTE: gpac provides 'MP4Box', used to embed/mux some downloads "
                "(music videos, animated artwork). Plain audio downloads still work.\n"
                "  To enable it later: 'sudo apt install -y gpac', or get an official "
                "build from https://gpac.io/downloads/"
            )

    print("System packages installed successfully.")

def firstsetup():
    # --- Check for root ---
    if os.geteuid() != 0:
        print("ERROR: This script must be run as root. Exiting.")
        sys.exit(1)

    try:
        # Step 1: Install required system packages.
        # Note: 'golang-go' / 'python3-flask' / 'python3-yaml' are intentionally
        # excluded — distro versions are too old. We install Go from the
        # official tarball (install_go) and Python deps via pip
        # (install_python_deps) below.
        _install_system_packages()

        # Step 1b: Modern Go toolchain (apt's golang-go is too old for
        # apple-music-downloader's go.mod).
        install_go(force=False)

        # Step 1c: Pinned Python dependencies
        install_python_deps()

        # Step 2: Download and set up Bento4
        BENTO4_URL = "https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-641.x86_64-unknown-linux.zip"
        zip_path = PROJECT_DIR / "bento4.zip"

        if not BENTO4_DIR.exists():
            print(f"Downloading Bento4 from {BENTO4_URL}...")
            _download(BENTO4_URL, zip_path)
            print("Extracting Bento4...")

            BENTO4_DIR.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(BENTO4_DIR)
            os.remove(zip_path)

            print("Bento4 installed inside project folder.")
            
            # Create symbolic links to Bento4 tools in /usr/local/bin
            bin_candidates = list(BENTO4_DIR.glob("Bento4*"))
            if bin_candidates:
                bin_dir = bin_candidates[0] / "bin"
                print(f"DEBUG: Creating symbolic links for Bento4 tools from: {bin_dir}")
                print(f"DEBUG: Bin directory exists: {bin_dir.exists()}")
                
                if not bin_dir.exists():
                    print(f"ERROR: Bin directory does not exist: {bin_dir}")
                    return
                
                # List all files for debugging
                all_files = list(bin_dir.glob("*"))
                print(f"DEBUG: All files in bin: {[f.name for f in all_files]}")
                
                # First, make all files executable (ZIP extraction doesn't preserve execute permissions)
                print("Setting execute permissions on all Bento4 tools...")
                for exe_file in all_files:
                    if exe_file.is_file():
                        try:
                            # Add execute permission for owner, group, and others
                            current_mode = exe_file.stat().st_mode
                            new_mode = current_mode | 0o755  # rwxr-xr-x
                            exe_file.chmod(new_mode)
                            print(f"  CHMOD: Set execute permission on {exe_file.name}")
                        except Exception as e:
                            print(f"  ERROR: Failed to set execute permission on {exe_file.name}: {e}")
                
                # Now check for executable files again
                executable_files = [f for f in all_files if f.is_file() and os.access(f, os.X_OK)]
                print(f"DEBUG: Executable files after chmod: {[f.name for f in executable_files]}")
                
                # Add to current session PATH as well
                os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
                
                # Create symbolic links with detailed error reporting
                success_count = 0
                error_count = 0
                
                for exe_file in executable_files:
                    try:
                        link_path = Path("/usr/local/bin") / exe_file.name
                        print(f"DEBUG: Attempting to create symlink: {exe_file.name}")
                        print(f"DEBUG: Source: {exe_file.absolute()}")
                        print(f"DEBUG: Target: {link_path}")
                        
                        if link_path.exists():
                            print(f"  INFO: Already exists: {exe_file.name}")
                        else:
                            os.symlink(str(exe_file.absolute()), str(link_path))
                            print(f"  SUCCESS: Created symlink for {exe_file.name}")
                            success_count += 1
                            
                    except Exception as e:
                        print(f"  ERROR: Failed to create symlink for {exe_file.name}: {e}")
                        error_count += 1
                
                print(f"SUMMARY: {success_count} symlinks created, {error_count} errors")
                
                # Verify what actually got created
                print("Verifying /usr/local/bin contents...")
                usr_local_bin = Path("/usr/local/bin")
                if usr_local_bin.exists():
                    bento4_links = [f for f in usr_local_bin.glob("*") if f.is_symlink()]
                    print(f"Found {len(bento4_links)} symlinks in /usr/local/bin")
                    for link in bento4_links:
                        if any(exe.name == link.name for exe in executable_files):
                            print(f"  VERIFIED: {link.name} -> {link.readlink()}")
                else:
                    print("ERROR: /usr/local/bin does not exist")
            else:
                print("WARN: Could not find Bento4 extracted folder")
                
        else:
            print("INFO: Bento4 already exists, skipping download")
            
            # Ensure Bento4 tools are available even if already downloaded
            bin_candidates = list(BENTO4_DIR.glob("Bento4*"))
            if bin_candidates:
                bin_dir = bin_candidates[0] / "bin"
                os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
                
                # Check if symbolic links need to be created
                try:
                    missing_links = []
                    for exe_file in bin_dir.glob("*"):
                        if exe_file.is_file() and os.access(exe_file, os.X_OK):
                            link_path = Path("/usr/local/bin") / exe_file.name
                            if not link_path.exists():
                                missing_links.append((exe_file, link_path))
                    
                    if missing_links:
                        print("Creating missing Bento4 symbolic links...")
                        for exe_file, link_path in missing_links:
                            os.symlink(exe_file, link_path)
                            print(f"  Created symlink: {exe_file.name}")
                    else:
                        print("✅ Bento4 tools already available system-wide")
                        
                except Exception as e:
                    print(f"WARN: Could not verify/create symbolic links: {e}")
                    print(f"Added existing Bento4 bin to current session PATH: {bin_dir}")

        # Step 3: Download and extract wrapper
        install_wrapper(force=False)

        # Step 4: Clone Apple Music Downloader repo
        install_downloader(force=False)

        print("First setup complete!")

    except subprocess.CalledProcessError as e:
        print(f"ERROR: Failed during setup: {e}")
        sys.exit(1)

def verify_and_repair_install():
    """Self-heal a partial / corrupted install.

    The `firstrun` marker only records that the *first* setup attempt
    completed; it does NOT prove every component is still on disk. If a
    network hiccup deleted `apple-music-downloader/` (or the user wiped a
    folder by hand), the Flask UI would happily start and then crash on
    'config.yaml not found'. This routine inspects each required component
    and re-installs anything missing, so 'Settings' always works after a
    successful launch.
    """
    issues = []
    # config.yaml: cheap repair first — if AMD repo exists but only the
    # example is there, just copy it. Re-cloning is expensive and unneeded.
    if not (AMD_DIR / "config.yaml").is_file():
        if AMD_DIR.is_dir() and (
            (AMD_DIR / "config.yaml.example").is_file()
            or (PROJECT_DIR / "app" / "default_config.yaml").is_file()
        ):
            issues.append(("config.yaml", _bootstrap_amd_config_yaml))
        else:
            issues.append(("apple-music-downloader", lambda: install_downloader(force=True)))
    if not (WRAPPER_DIR / "wrapper").is_file():
        issues.append(("wrapper", lambda: install_wrapper(force=True)))
    if not BENTO4_DIR.is_dir() or not list(BENTO4_DIR.glob("Bento4*")):
        # Bento4 has no targeted re-installer, so signal a full firstsetup.
        issues.append(("bento4", None))
    if not VENV_PYTHON.exists():
        issues.append(("python venv", install_python_deps))

    if not issues:
        return

    print("WARN: install integrity check failed for: "
          + ", ".join(name for name, _ in issues))

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print(
            "ERROR: missing components require root to repair. Re-run with:\n"
            f"  sudo python3 {Path(__file__).name}"
        )
        sys.exit(1)

    # If Bento4 is missing we need the full firstsetup path (it also handles
    # symlinks and PATH wiring). Drop the marker so it re-runs cleanly.
    if any(name == "bento4" for name, _ in issues):
        marker = PROJECT_DIR / "firstrun"
        if marker.exists():
            print(f"Removing {marker} to force a full re-setup")
            try:
                marker.unlink()
            except OSError as e:
                print(f"WARN: could not remove firstrun marker: {e}")
        firstsetup()
        with open(PROJECT_DIR / "firstrun", "w") as f:
            f.write("This file marks that first setup has been completed.\n")
        return

    for name, fn in issues:
        print(f"Repairing missing component: {name}")
        try:
            fn()
        except Exception as e:
            print(f"ERROR: failed to repair {name}: {e}")
            sys.exit(1)
    print("Install integrity check: all components repaired.")


def _maybe_reexec_in_venv():
    """If a project venv exists and we're not running under it, re-exec the
    current script under the venv's Python so flask/waitress imports work."""
    if not VENV_PYTHON.exists():
        return
    try:
        # Do NOT compare resolved interpreter paths here. On some distros,
        # `.venv/bin/python` is a symlink to the system interpreter binary,
        # so both resolve to e.g. `/usr/bin/python3.14` even when we're *not*
        # running with the venv's site-packages.
        in_target_venv = Path(sys.prefix).resolve() == VENV_DIR.resolve()
    except OSError:
        return
    if in_target_venv:
        return

    target = str(VENV_PYTHON)
    print(f"Switching to venv Python: {target}")
    script = str(Path(__file__).resolve())
    os.execv(target, [target, script, *sys.argv[1:]])


def _wire_paths_for_subprocs():
    """Add Bento4 / wrapper / Go to PATH so the apple-music-downloader Go
    subprocess can find them. Idempotent."""
    bin_candidates = list(BENTO4_DIR.glob("Bento4*"))
    if bin_candidates:
        bin_dir = bin_candidates[0] / "bin"
        os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    os.environ["PATH"] = f"{WRAPPER_DIR}:{os.environ['PATH']}"
    os.environ["PATH"] = f"{GO_INSTALL_DIR / 'bin'}:{os.environ['PATH']}"


def _browser_url_for_host(host: str, port: int) -> str:
    if host in ("", "0.0.0.0", "::"):
        return f"http://127.0.0.1:{port}/"
    if ":" in host and not host.startswith("["):
        return f"http://[{host}]:{port}/"
    return f"http://{host}:{port}/"


def _detect_lan_ip() -> str:
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Startup welcome banner
# ---------------------------------------------------------------------------
# Block-letter cells for the ASCII wordmark. Each glyph is 6 rows of equal
# width so they concatenate into perfectly aligned art on any terminal.
_WORDMARK_LETTERS = {
    "A": [" █████╗ ", "██╔══██╗", "███████║", "██╔══██║", "██║  ██║", "╚═╝  ╚═╝"],
    "L": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
    "C": [" ██████╗", "██╔════╝", "██║     ", "██║     ", "╚██████╗", " ╚═════╝"],
    "R": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██║  ██║", "╚═╝  ╚═╝"],
    "I": ["██╗", "██║", "██║", "██║", "██║", "╚═╝"],
    "P": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔═══╝ ", "██║     ", "╚═╝     "],
    " ": ["  ", "  ", "  ", "  ", "  ", "  "],
}


def _render_wordmark(text: str) -> list:
    """Build aligned ASCII-art rows for `text` from the per-letter cells."""
    rows = ["", "", "", "", "", ""]
    for ch in text:
        cell = _WORDMARK_LETTERS.get(ch.upper(), _WORDMARK_LETTERS[" "])
        for i in range(6):
            rows[i] += cell[i] + " "
    return rows


def _banner_enabled() -> bool:
    return os.environ.get("ALAC_RIP_BANNER", "1").strip().lower() not in ("0", "false", "no", "off")


def _banner_color_enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("ALAC_RIP_BANNER", "").strip().lower() == "plain":
        return False
    try:
        return sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


def _render_banner(local_url: str, lan_url: str) -> None:
    color = _banner_color_enabled()

    def paint(s: str, code: str) -> str:
        return f"\033[{code}m{s}\033[0m" if color else s

    MAGENTA, CYAN, GREEN, YELLOW, DIM, BOLD = "95", "96", "92", "93", "90", "1"

    print()
    for line in _render_wordmark("ALAC RIP"):
        print("  " + paint(line, MAGENTA))
    print("  " + paint("Apple Music Downloader  ·  Web UI", DIM))
    print()
    print("  " + paint("▸ Open in your browser:", BOLD) + "  " + paint(local_url, f"{CYAN};1"))
    if lan_url:
        print("  " + paint("▸ On your network:", BOLD) + "     " + paint(lan_url, CYAN))
        print("  " + paint("⚠  Reachable on your LAN with no login — only on networks you trust.", YELLOW))
    else:
        print("  " + paint("•  Local access only (no login needed on this machine).", DIM))
        print("  " + paint("   For other devices, restart with FLASK_HOST=0.0.0.0", DIM))
    print()
    print("  " + paint("Getting started", BOLD))
    print("    " + paint("1.", GREEN) + " Log in to Apple Music in the web UI")
    print("    " + paint("2.", GREEN) + " Add your Media User Token in Settings (lyrics / AAC-LC)")
    print("    " + paint("3.", GREEN) + " Paste Apple Music links, choose a format, and download")
    print()
    print("  " + paint("Formats", DIM) + ": ALAC · AAC · Dolby Atmos      "
          + paint("Stop", DIM) + ": Ctrl+C      " + paint("Hide banner", DIM) + ": ALAC_RIP_BANNER=0")
    print()


def _print_welcome_banner(host: str, port: int) -> str:
    """Print the startup banner and return the local browser URL.

    Falls back to plain URL lines when the banner is disabled
    (ALAC_RIP_BANNER=0) or if anything goes wrong — cosmetics must never
    block server startup.
    """
    local_url = _browser_url_for_host(host, port)
    lan_url = ""
    if host in ("0.0.0.0", "::"):
        lan_ip = _detect_lan_ip()
        if lan_ip:
            lan_url = f"http://{lan_ip}:{port}/"

    if not _banner_enabled():
        print(f"Web UI: {local_url}", flush=True)
        if lan_url:
            print(f"LAN URL: {lan_url}", flush=True)
        return local_url

    try:
        _render_banner(local_url, lan_url)
    except Exception:  # noqa: BLE001
        print(f"Web UI: {local_url}", flush=True)
        if lan_url:
            print(f"LAN URL: {lan_url}", flush=True)
    return local_url


def _should_open_browser() -> bool:
    value = os.environ.get("ALAC_RIP_OPEN_BROWSER", "1").strip().lower()
    if value in ("0", "false", "no", "off"):
        return False
    force = value in ("force", "always")
    if sys.platform.startswith("linux"):
        if not force and (os.environ.get("SUDO_USER") or (hasattr(os, "geteuid") and os.geteuid() == 0)):
            return False
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or os.environ.get("WSL_DISTRO_NAME")):
            return False
    return True


def _open_browser_later(url: str, enabled: bool) -> None:
    if not enabled:
        return
    if not _should_open_browser():
        return

    def _open():
        try:
            opened = bool(webbrowser.open(url, new=2))
        except Exception as e:
            print(f"WARN: could not auto-open browser: {e}", flush=True)
            return
        if not opened:
            print(f"WARN: browser did not open automatically; use the Web UI URL above.", flush=True)

    threading.Timer(1.0, _open).start()


def _access_logs_enabled() -> bool:
    value = os.environ.get("ALAC_RIP_ACCESS_LOGS", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def _enable_access_logs(flask_app) -> None:
    if not _access_logs_enabled() or getattr(flask_app, "_alac_rip_access_logs", False):
        return
    from flask import request

    @flask_app.after_request
    def _alac_rip_access_log(response):
        path = request.full_path.rstrip("?")
        print(f"[HTTP] {request.remote_addr or '-'} {request.method} {path} -> {response.status_code}", flush=True)
        return response

    flask_app._alac_rip_access_logs = True


def _acquire_lockfile():
    """Create `~/.alac-rip-instance.lock` (or PROJECT_DIR/.lock) and refuse
    to start a second instance against the same project. Cooperative; the
    file holds our PID and the staleness check uses os.kill(pid, 0)."""
    lock_path = PROJECT_DIR / ".instance.lock"
    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text().strip() or "0")
        except Exception:  # noqa: BLE001
            existing_pid = 0
        if existing_pid > 0:
            try:
                os.kill(existing_pid, 0)
                # Process is alive. Refuse to start.
                print(
                    f"ERROR: another alac-rip instance (pid={existing_pid}) is "
                    f"already running against this project. If that's wrong, "
                    f"delete {lock_path} and try again."
                )
                sys.exit(2)
            except OSError:
                # Stale lock; reclaim.
                pass
    try:
        lock_path.write_text(str(os.getpid()))
    except OSError as e:
        print(f"WARN: could not write lockfile {lock_path}: {e}")
        return None

    def _cleanup(*_):
        try:
            if lock_path.exists() and lock_path.read_text().strip() == str(os.getpid()):
                lock_path.unlink()
        except OSError:
            pass

    import atexit
    atexit.register(_cleanup)
    # On SIGTERM/SIGINT, release the JobQueue cleanly first then exit.
    def _on_signal(signum, _frame):
        print(f"Received signal {signum}; shutting down…")
        try:
            from app.routes import JOB_QUEUE  # type: ignore
            JOB_QUEUE.shutdown()
        except Exception:  # noqa: BLE001
            pass
        _cleanup()
        sys.exit(0)
    try:
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)
    except Exception:  # noqa: BLE001
        pass
    return lock_path


def start(open_browser=True):
    # Re-exec under the venv interpreter so 'flask' / 'waitress' imports resolve.
    _maybe_reexec_in_venv()
    print("Starting Apple Music Downloader Web UI...")

    _wire_paths_for_subprocs()
    _acquire_lockfile()

    from app import app  # noqa: WPS433
    _enable_access_logs(app)

    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    # Default to loopback-only. The Web UI has no authentication and the
    # wrapper holds an authenticated Apple session, so binding to all
    # interfaces by default would expose it to the whole LAN. Users who
    # want LAN access can opt in explicitly with FLASK_HOST=0.0.0.0.
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "5000"))
    browser_url = _print_welcome_banner(host, port)
    _open_browser_later(browser_url, open_browser)

    if debug:
        print(f"Running Flask dev server on {host}:{port} (DEBUG=on)", flush=True)
        app.run(host=host, port=port, debug=True)
        return

    # Production: use waitress (pure-Python, cross-platform WSGI server).
    # Fall back to Flask's built-in server if waitress is unavailable.
    try:
        from waitress import serve  # type: ignore
        print(f"Running waitress WSGI server on {host}:{port}", flush=True)
        serve(app, host=host, port=port, threads=16)
    except ImportError:
        print(
            "WARN: waitress not installed; falling back to Flask dev server. "
            "Install it via 'pip install -r requirements.txt'."
        )
        app.run(host=host, port=port, debug=False)


def cli_download(urls, fmt):
    """Headless one-shot downloader for cron / scripts.

    Talks to a running Web UI's `/queue/start` if one is alive (so the
    visible queue stays in sync); otherwise spawns a tiny synchronous
    download via subprocess directly. Either path respects the user's
    Settings (quality-preference-chain, etc.) because both ultimately
    consult the same config.yaml.
    """
    _maybe_reexec_in_venv()
    _wire_paths_for_subprocs()

    fmt = (fmt or "ALAC").upper()
    if fmt not in ("ALAC", "ATMOS", "AAC"):
        print(f"ERROR: --format must be ALAC, ATMOS, or AAC (got {fmt!r})")
        sys.exit(2)

    # Try to reach a running web UI on FLASK_HOST/FLASK_PORT first.
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    port = int(os.environ.get("FLASK_PORT", "5000"))
    api_base = f"http://{host}:{port}"

    import json as _json
    items = [{"url": u, "format": fmt} for u in urls]
    body = _json.dumps({"items": items}).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{api_base}/queue/start",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        if data.get("status") == "ok":
            print(f"✓ Queued {data.get('queued')} job(s) into running web UI at {api_base}")
            return
        print(f"WARN: web UI responded: {data}")
    except Exception as e:  # noqa: BLE001
        print(f"INFO: no running web UI at {api_base} ({e}); falling back to direct mode")

    # Direct mode: invoke `go run main.go ...` once per URL, sequentially.
    if not AMD_DIR.exists():
        print(f"ERROR: apple-music-downloader missing at {AMD_DIR}; run setup first.")
        sys.exit(1)
    overall = 0
    for url in urls:
        cmd = ["go", "run", "main.go"]
        if fmt == "ATMOS":
            cmd.append("--atmos")
        elif fmt == "AAC":
            cmd.append("--aac")
        cmd.append(url)
        print(f"\n=== {url} ({fmt}) ===")
        try:
            rc = subprocess.call(cmd, cwd=str(AMD_DIR))
        except Exception as e:  # noqa: BLE001
            print(f"ERROR: spawn failed: {e}")
            rc = 1
        if rc != 0:
            overall = rc
            print(f"FAIL exit={rc}")
        else:
            print("OK")
    sys.exit(overall)

def parse_args():
    parser = argparse.ArgumentParser(
        description="Apple Music Downloader Web UI launcher",
    )
    parser.add_argument(
        "--update-wrapper",
        action="store_true",
        help="Force re-download the wrapper binary to the latest release",
    )
    parser.add_argument(
        "--update-downloader",
        action="store_true",
        help="Force update the apple-music-downloader repo to the latest commit",
    )
    parser.add_argument(
        "--update-go",
        action="store_true",
        help=f"Force re-install the pinned Go toolchain ({GO_VERSION})",
    )
    parser.add_argument(
        "--update-python-deps",
        action="store_true",
        help="Re-run pip install -r requirements.txt to refresh Python deps",
    )
    parser.add_argument(
        "--update-all",
        action="store_true",
        help="Update wrapper, downloader, Go toolchain, and Python deps",
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Skip starting the Flask web UI after setup/update completes",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open the Web UI in a browser",
    )

    sub = parser.add_subparsers(dest="cmd")
    dl = sub.add_parser("download",
                        help="Headless one-shot download(s); cron/script-friendly")
    dl.add_argument("urls", nargs="+", help="One or more Apple Music URLs")
    dl.add_argument("--format", default="ALAC", choices=["ALAC", "ATMOS", "AAC", "alac", "atmos", "aac"],
                    help="Format (default: ALAC)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    update_wrapper = args.update_wrapper or args.update_all
    update_downloader = args.update_downloader or args.update_all
    update_go = args.update_go or args.update_all
    update_python = args.update_python_deps or args.update_all

    # === First run check ===
    marker_file = PROJECT_DIR / "firstrun"

    if not marker_file.exists():
        firstsetup()
        with open(marker_file, "w") as f:
            f.write("This file marks that first setup has been completed.\n")
    else:
        # Self-heal any missing component from a previous partial install
        # before we honour explicit --update-* flags below. This is what
        # prevents 'config.yaml not found' on Settings after a flaky first run.
        verify_and_repair_install()

    if marker_file.exists() and (update_wrapper or update_downloader or update_go or update_python):
        # Targeted update on an already-initialised install
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            print("ERROR: Updates must be run as root. Exiting.")
            sys.exit(1)
        try:
            if update_go:
                install_go(force=True)
            if update_python:
                install_python_deps()
            if update_wrapper:
                install_wrapper(force=True)
            if update_downloader:
                install_downloader(force=True)
        except (subprocess.CalledProcessError, RuntimeError) as e:
            print(f"ERROR: Update failed: {e}")
            sys.exit(1)

    if args.cmd == "download":
        cli_download(args.urls, args.format)
    elif not args.no_start:
        start(open_browser=not args.no_browser)
