# Contributing

Thanks for considering a contribution.

## Project scope

This repository is a Web UI, launcher, queue, diagnostics, and setup layer around upstream Apple Music downloader tools.

Issues in the core downloader or wrapper may need to be reported upstream:

- `zhaarey/apple-music-downloader`
- `WorldObservationLog/wrapper`

## Development setup

Recommended development environment:

- Linux.
- Ubuntu/Debian or another apt-based distribution.
- Python 3.9+.

Install and run:

```bash
sudo python3 main.py
```

For dependency refresh:

```bash
sudo python3 main.py --update-python-deps
```

## Before opening a pull request

Please check:

- The app still starts with `sudo python3 main.py`.
- Python files compile.
- No credentials, tokens, logs, downloads, archives, or generated tools are committed.
- Existing HTML IDs and JavaScript hooks are preserved unless intentionally changed.
- UI changes are tested in the browser.

Compile check:

```bash
python3 -m py_compile main.py app/routes.py app/queue_engine.py app/scanner.py app/probe.py
```

## What not to commit

Do not commit:

- `.venv/`
- `apple-music-downloader/`
- `wrapper/`
- `bento4/`
- `.credentials`
- `firstrun`
- `.instance.lock`
- `config-backups/`
- downloaded music
- personal config files containing tokens

## Bug reports

Useful bug reports include:

- Linux distribution and version.
- Python version.
- Browser.
- Exact command used to start the app.
- Whether this is VM, WSL2, or native Linux.
- Redacted logs or diagnostics bundle.
- The Apple Music URL type, for example song, album, or playlist.

Never include Apple credentials, tokens, or unredacted config.
