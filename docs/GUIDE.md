# Full Guide — Setup, Usage, and Uploading to GitHub

This is a complete, beginner-friendly guide. If you have never used Linux or GitHub before, you can still follow every step. Copy the commands exactly as shown.

> **Two important notes before you start**
>
> - This app is for **personal, educational use** with your **own** Apple Music account and an **active subscription**.
> - The Web UI has **no password/login of its own**. By default it is only reachable from the same computer (localhost). Do not put it on the public internet.

## Table of contents

- [1. What you need before you start](#1-what-you-need-before-you-start)
- [2. Get the project onto your computer](#2-get-the-project-onto-your-computer)
- [3. Install and run on Linux (Ubuntu recommended)](#3-install-and-run-on-linux-ubuntu-recommended)
- [4. Install and run on Windows (WSL2)](#4-install-and-run-on-windows-wsl2)
- [5. Open and use the Web UI](#5-open-and-use-the-web-ui)
- [6. Downloading music step by step](#6-downloading-music-step-by-step)
- [7. Choose your repository name](#7-choose-your-repository-name)
- [8. Customize the star button](#8-customize-the-star-button)
- [9. Add screenshots](#9-add-screenshots)
- [10. Upload to GitHub](#10-upload-to-github)
- [11. After uploading](#11-after-uploading)
- [12. Update your project later](#12-update-your-project-later)
- [13. Troubleshooting](#13-troubleshooting)

---

## 1. What you need before you start

To **run the app**, you need:

- A Linux system. **Ubuntu is recommended** (22.04 LTS or 24.04 LTS), but any Debian/Ubuntu-based, x86_64 system works.
- `python3` installed.
- The ability to use `sudo` (administrator rights).
- An internet connection.
- A valid Apple Music account with an active subscription.

To **upload to GitHub**, you also need:

- A free GitHub account: https://github.com/signup
- `git` installed (the steps below show how).

> If your main computer is Windows or macOS, jump to [section 4](#4-install-and-run-on-windows-wsl2) for the easiest path (WSL2 on Windows), or run Ubuntu inside VirtualBox/VMware.

---

## 2. Get the project onto your computer

You have two options.

### Option A — Download a ZIP (simplest)

1. On the GitHub page of the project, click the green **Code** button.
2. Click **Download ZIP**.
3. Extract it. You now have a folder like `apple-music-downloader-webui`.

### Option B — Clone with git (recommended)

```bash
git clone https://github.com/losslessbitlab/apple-music-downloader-webui.git
cd apple-music-downloader-webui
```

> Replace the URL with the real repository address once it exists. If you have not uploaded it yet, just use the folder you already have.

---

## 3. Install and run on Linux (Ubuntu recommended)

### Step 1 — Update your system

```bash
sudo apt update
sudo apt upgrade -y
```

### Step 2 — Make sure Python is installed

```bash
python3 --version
```

If it prints a version (e.g. `Python 3.12.x`), you are good. If not:

```bash
sudo apt install -y python3
```

### Step 3 — Go into the project folder

```bash
cd ~/apple-music-downloader-webui
```

(Adjust the path to wherever you put the folder.)

### Step 4 — Run it

```bash
sudo python3 main.py
```

**What happens on the first run:**

- It installs required system packages (`git`, `ffmpeg`, `wget`, etc.).
- It installs a pinned Go toolchain and Python dependencies into a local `.venv`.
- It downloads Bento4 and the wrapper, and clones the core downloader.
- It then starts the Web UI.

This first run can take a few minutes. Later runs are fast.

### Step 5 — Open the Web UI

When it starts you will see something like:

```text
Web UI: http://127.0.0.1:5000/
Local access only (no authentication). To allow other devices on your LAN, restart with FLASK_HOST=0.0.0.0.
Running waitress WSGI server on 127.0.0.1:5000
```

Open this address in your browser:

```text
http://127.0.0.1:5000/
```

### Optional — Access from another device on your network

By default the app only listens on the local machine. To reach it from your phone or another PC on the **same network you trust**:

```bash
sudo FLASK_HOST=0.0.0.0 python3 main.py
```

Then use the printed `LAN URL` (for example `http://192.168.1.20:5000/`).

> Never expose this to the public internet. Use a VPN or SSH tunnel for remote access.

### Optional — Do not auto-open the browser

```bash
sudo python3 main.py --no-browser
```

### How to stop the app

Press `Ctrl + C` in the terminal where it is running.

---

## 4. Install and run on Windows (WSL2)

The app is built for Linux. On Windows, the cleanest way is **WSL2** (a real Ubuntu inside Windows).

### Step 1 — Install WSL2 with Ubuntu

Open **PowerShell as Administrator** and run:

```powershell
wsl --install -d Ubuntu
```

Restart your PC if it asks. After reboot, Ubuntu opens and asks you to create a Linux username and password. This password is your Linux `sudo` password.

### Step 2 — Open Ubuntu

Open the **Ubuntu** app from the Start menu. You now have a Linux terminal.

### Step 3 — Follow the Linux steps

Inside the Ubuntu terminal, follow [section 3](#3-install-and-run-on-linux-ubuntu-recommended) exactly:

```bash
sudo apt update
sudo apt upgrade -y
cd ~
git clone https://github.com/losslessbitlab/apple-music-downloader-webui.git
cd apple-music-downloader-webui
sudo python3 main.py
```

### Step 4 — Open the Web UI from Windows

In your normal Windows browser (Chrome, Edge, etc.), go to:

```text
http://127.0.0.1:5000/
```

WSL2 forwards localhost automatically, so this just works.

### Where do downloads go?

You can set save folders in **Settings**. To save into a Windows folder, use a path like:

```text
/mnt/c/Users/YourName/Music
```

`/mnt/c/` is your Windows `C:` drive seen from inside Ubuntu.

> **Pure Windows (no WSL) is not supported** for running the app, because it relies on Linux tools. WSL2 or a Linux VM is the way.

---

## 5. Open and use the Web UI

When you open `http://127.0.0.1:5000/`, here is the layout:

- **Header** — theme toggle, **Star this project** button, and **Settings**.
- **Login / authentication panel** — sign in to Apple Music.
- **Download box** — paste Apple Music links here.
- **Queue** — shows progress of each download.
- **System Health** — green checks confirm everything is ready.
- **Storage / Library** — shows where files are saved and disk usage.

### Step 1 — Log in to Apple Music

1. Find the login panel.
2. Enter your Apple ID email and password.
3. If Apple asks for a **two-factor code**, enter it when prompted and watch the log.
4. Wait for the log to say login succeeded.

> Tip: use a **dedicated Apple Music account**, not your main personal Apple ID.

### Step 2 — Add your Media User Token (needed for lyrics and AAC-LC)

1. Open **Settings**.
2. Find **Media User Token**.
3. Follow the on-screen helper to copy it from the Apple Music web player, then paste and save.

### Step 3 — Configure your save folders and quality (optional)

In **Settings** you can set:

- Where ALAC, AAC, and Atmos files are saved.
- Your quality preference chain (e.g. prefer 48 kHz, then 44.1, then 96, then 192).
- Lyrics behavior, cover art, conversion options, and more.

Click **Save** when done.

---

## 6. Downloading music step by step

> **Two things that trip people up most:**
>
> 1. **You need an active Apple Music subscription.** This tool downloads from your own subscription's streams; it does not bypass payment. Without an active plan, downloads fail.
> 2. **The country code in the link must match your subscription's country.** Apple Music links have a storefront/country code right after the domain (the `us` in `https://music.apple.com/us/album/...`). After login your links often default to `/us/`. If your subscription is in another country, change that code to **your** storefront, e.g. `gb` (UK), `de` (Germany), `jp` (Japan), `in` (India):
>
>    ```text
>    https://music.apple.com/us/album/...   ->   https://music.apple.com/gb/album/...
>    ```
>
>    A mismatched storefront is the most common cause of "not available" / region errors.

1. Copy an Apple Music link from the Apple Music app or website. Examples:
   - A song: `https://music.apple.com/us/song/...`
   - An album: `https://music.apple.com/us/album/...`
   - A playlist: `https://music.apple.com/us/playlist/...`
   - Remember to swap `us` for your own country code if needed (see the note above).
2. Paste one or more links into the download box on the home page.
3. The app **scans** each link and shows the available quality.
4. Pick your format: **ALAC** (lossless), **AAC** (smaller, lossy), or **Atmos** (spatial), and adjust options if you want.
5. Click **Download**.
6. Watch the **queue** for progress. Finished files appear in your configured save folder.

### Headless / command-line downloads (advanced, optional)

You can download without opening the browser:

```bash
sudo python3 main.py download "https://music.apple.com/us/album/xxxx" --format ALAC
```

Allowed formats: `ALAC`, `AAC`, `ATMOS`.

---

## 7. Choose your repository name

A good name is short, searchable, and describes what the project is.

**Recommended:**

```text
apple-music-downloader-webui
```

Good alternatives if that name is taken:

```text
apple-music-dl-webui
apple-music-downloader-studio
amdl-web-ui
```

The rest of this guide assumes `apple-music-downloader-webui`.

---

## 8. Customize the star button

The app header has a **Star this project** button. It must point to **your** repository.

It currently points to:

```text
https://github.com/losslessbitlab/apple-music-downloader-webui
```

If your username or repo name is different, change it:

1. Open the file:

   ```text
   app/templates/index.html
   ```

2. Find the line containing `id="github-star-link"`:

   ```html
   <a id="github-star-link" href="https://github.com/losslessbitlab/apple-music-downloader-webui" target="_blank" rel="noopener"
   ```

3. Change the `href` to your real repository URL:

   ```html
   <a id="github-star-link" href="https://github.com/YOUR_USERNAME/YOUR_REPO" target="_blank" rel="noopener"
   ```

4. Save the file.

**One-line way to do it from the terminal** (replace `YOUR_USERNAME` and `YOUR_REPO`):

```bash
sed -i 's#https://github.com/losslessbitlab/apple-music-downloader-webui#https://github.com/YOUR_USERNAME/YOUR_REPO#g' app/templates/index.html
```

---

## 9. Add screenshots

The README shows three images, which live in `docs/screenshots/`:

- `Download.png`
- `Home.png`
- `Settings.png`

To replace them with real screenshots:

1. Open the app in your browser and take screenshots of:
   - the home page / queue,
   - the scan-and-confirm step,
   - the Settings page,
   - the System Health panel.
2. Save each screenshot as a PNG (for example `Download.png`, `Home.png`, `Settings.png`).
3. Put them in the `docs/screenshots/` folder.
4. Open `README.md` and verify the image links match your filenames, for example:

   ```markdown
   ![Home screen](docs/screenshots/Home.png)
   ```

That's it — GitHub will show your real screenshots.

---

## 10. Upload to GitHub

### Step 1 — Install and configure git

```bash
sudo apt install -y git
git config --global user.name "Your Name"
git config --global user.email "your-email@example.com"
```

### Step 2 — Create an empty repository on GitHub

1. Go to https://github.com/new
2. **Repository name:** `apple-music-downloader-webui`
3. **Description** (paste this):

   ```text
   A modern Apple Music Downloader Web UI with queue management, ALAC/AAC/Atmos options, lyrics controls, Skip-MV handling, health checks, diagnostics, and safer Linux setup.
   ```

4. Choose **Public** or **Private**.
5. **Do NOT** tick "Add a README", ".gitignore", or "license" — this project already has them.
6. Click **Create repository**.

### Step 3 — Go to your project folder

```bash
cd ~/apple-music-downloader-webui
```

### Step 4 — Check the existing remote (important!)

Your local copy may still point at the original project. Check:

```bash
git remote -v
```

If it shows something like `origin  https://github.com/lalit22km/alac-rip.git`, rename it so you don't push to the wrong place:

```bash
git remote rename origin upstream
```

### Step 5 — Connect your new repository

Replace `YOUR_USERNAME` with your GitHub username (for you: `losslessbitlab`):

```bash
git remote add origin https://github.com/YOUR_USERNAME/apple-music-downloader-webui.git
```

If `origin` already exists and you just want to repoint it:

```bash
git remote set-url origin https://github.com/YOUR_USERNAME/apple-music-downloader-webui.git
```

Confirm:

```bash
git remote -v
```

### Step 6 — Double-check what will be uploaded

```bash
git status --ignored
```

Make sure these are **ignored** (not staged): `.venv/`, `apple-music-downloader/`, `wrapper/`, `bento4/`, `.credentials`, `firstrun`, `.instance.lock`, downloaded music, and `*.zip`/`*.tar.gz`. The included `.gitignore` already handles this.

### Step 7 — Stage, commit, and push

```bash
git add .
git commit -m "Initial public release"
git branch -M main
git push -u origin main
```

### About the password prompt (Personal Access Token)

When pushing over HTTPS, GitHub will ask for a username and password. **Your normal account password will not work.** You must use a **Personal Access Token (PAT)**:

1. Go to https://github.com/settings/tokens
2. Click **Generate new token** → **Fine-grained** (or **classic**).
3. Give it access to your repositories with the **`repo`** scope/permission.
4. Copy the token (you only see it once).
5. When git asks for your password, paste the **token** instead.

> Prefer SSH? Use `git remote add origin git@github.com:YOUR_USERNAME/apple-music-downloader-webui.git` after adding an SSH key to GitHub.

---

## 11. After uploading

### Add topics (helps people find it)

On your repository page, click the gear icon next to **About**, then add these topics:

```text
apple-music
apple-music-downloader
apple-music-webui
alac
aac
atmos
lossless-audio
music-downloader
downloader
web-ui
flask
python
ubuntu
debian
linux
wsl2
self-hosted
music-library
lyrics
metadata
audio-tools
```

### Verify the page looks right

- README renders with screenshots (placeholders until you replace them).
- License shows as **MIT**.
- The description and topics are set.

### Test the star button

Open your app, click **Star this project** in the header, and confirm it opens your repository.

---

## 12. Update your project later

### Push changes you make locally

```bash
git add .
git commit -m "Describe what you changed"
git push
```

### Update the downloader tools (not your code)

The app can refresh the upstream tools it uses:

```bash
sudo python3 main.py --update-all
```

Useful single updates:

```bash
sudo python3 main.py --update-downloader
sudo python3 main.py --update-wrapper
sudo python3 main.py --update-go
sudo python3 main.py --update-python-deps
```

Add `--no-start` to update without launching the UI:

```bash
sudo python3 main.py --update-all --no-start
```

---

## 13. Troubleshooting

### "This script must be run as root"

Run it with `sudo`:

```bash
sudo python3 main.py
```

### `python3` not found

```bash
sudo apt install -y python3
```

### The browser did not open automatically

This is normal when running with `sudo`. Just open the printed URL yourself:

```text
http://127.0.0.1:5000/
```

### `Embed failed: MP4Box ... not found in $PATH`

`MP4Box` comes from the **gpac** suite and is used to embed/mux some downloads (music videos, animated artwork). **Your ALAC/AAC audio still downloads fine** — this message is non-fatal.

Setup now installs `gpac` automatically (and tries to enable Ubuntu's `universe` repo). If you still see this, install it manually and restart the app:

```bash
sudo apt update
sudo apt install -y gpac
MP4Box -version
```

If `gpac` is not in your distro's repos, get an official build from https://gpac.io/downloads/.

### Downloads say "not available" or fail with region errors

Check the **country code** in your link (the `us` in `https://music.apple.com/us/...`) and change it to the country where your subscription is active. Also confirm your subscription is active and your Media User Token is set.

### Settings says `config.yaml not found`

Run the app once with `sudo python3 main.py` from the project folder so it can finish installing the downloader. The app self-repairs missing pieces on start.

### Another instance is already running

The app refuses to start twice. Stop the other one (`Ctrl + C` in its terminal), or delete the lock file if it is stale:

```bash
rm .instance.lock
```

### I need to report a bug

In the Web UI, open **System Health** and download the diagnostics bundle. It redacts your tokens and webhooks. Review it before sharing, and never post your Apple password, Media User Token, or unredacted config.

---

Need anything else explained? Open an issue using the templates in `.github/ISSUE_TEMPLATE/`.
