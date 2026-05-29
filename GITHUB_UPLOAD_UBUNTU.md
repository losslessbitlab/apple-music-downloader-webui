# Uploading this project to GitHub from Ubuntu

This is a concise maintainer cheat-sheet for publishing the project for the first time.

> For a complete, beginner-friendly walkthrough (setup, usage, customization, screenshots, Windows/WSL2, and upload with Personal Access Tokens), see the **[Full Guide](docs/GUIDE.md)**.

## 1. Choose the repository name

Recommended name:

```text
apple-music-downloader-webui
```

Short GitHub description:

```text
A modern Apple Music Downloader Web UI with queue management, ALAC/AAC/Atmos options, lyrics controls, Skip-MV handling, health checks, diagnostics, and safer Linux setup.
```

## 2. Create the empty GitHub repository

1. Open https://github.com/new
2. Repository name: `apple-music-downloader-webui`
3. Description: paste the description above.
4. Visibility: choose Public or Private.
5. Do not initialize with README, .gitignore, or license if you are pushing this existing folder.
6. Click Create repository.

## 3. Prepare Ubuntu

```bash
sudo apt update
sudo apt install -y git
```

Set your Git identity:

```bash
git config --global user.name "Your Name"
git config --global user.email "your-email@example.com"
```

## 4. Move into the project folder

```bash
cd ~/alac-rip
```

Use the actual path where the project lives. Examples:

```bash
cd ~/Downloads/alac-rip
cd ~/Desktop/alac-rip
```

## 5. Check what will be uploaded

```bash
git status
```

If this is not yet a Git repository:

```bash
git init
```

Check ignored files:

```bash
git status --ignored
```

Make sure these are ignored and not staged:

```text
.venv/
apple-music-downloader/
wrapper/
bento4/
firstrun
.credentials
.instance.lock
config-backups/
*.tar.gz
*.zip
```

## 6. Add files and commit

```bash
git add .
git status
git commit -m "Initial public release"
```

## 7. Connect to GitHub

Replace `YOUR_USERNAME` with your GitHub username.

HTTPS remote:

```bash
git remote add origin https://github.com/YOUR_USERNAME/apple-music-downloader-webui.git
```

Or SSH remote:

```bash
git remote add origin git@github.com:YOUR_USERNAME/apple-music-downloader-webui.git
```

## 8. Push

```bash
git branch -M main
git push -u origin main
```

If GitHub asks for a password over HTTPS, use a Personal Access Token instead of your account password.

## 9. Add GitHub topics

In the GitHub repository page:

1. Click the gear icon next to About.
2. Paste relevant topics.
3. Save changes.

Recommended topics:

```text
apple-music
apple-music-downloader
apple-music-webui
alac
lossless-audio
aac
atmos
spatial-audio
music-downloader
downloader
web-ui
flask
python
self-hosted
lyrics
metadata
music-library
linux
ubuntu
wsl2
```

## 10. Update the star button URL

The in-app **Star this project** button is already set to:

```text
https://github.com/losslessbitlab/apple-music-downloader-webui
```

If your username or repo name differs, edit `app/templates/index.html`, find the line with `id="github-star-link"`, and set the `href`:

```html
href="https://github.com/YOUR_USERNAME/apple-music-downloader-webui"
```

Then commit and push:

```bash
git add app/templates/index.html
git commit -m "Update GitHub project link"
git push
```

## 11. Add screenshots later

Create this folder:

```bash
mkdir -p docs/screenshots
```

Add your screenshots (PNG files) to `docs/screenshots/`. For example:

```text
docs/screenshots/Download.png
docs/screenshots/Home.png
docs/screenshots/Settings.png
```

Make sure the image links in `README.md` point at these files. Full steps are in the [Full Guide](docs/GUIDE.md#9-add-screenshots).

Commit and push:

```bash
git add docs/screenshots README.md
git commit -m "Add screenshots"
git push
```
