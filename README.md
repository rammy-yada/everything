# Everything Downloader

A web-based media downloader powered by **yt-dlp** that supports 1000+ websites.

## Features

- **Universal Downloads** — YouTube, TikTok, Instagram, Twitter/X, Facebook, Reddit, Vimeo, SoundCloud, and 1000+ more
- **Format Selection** — Video (MP4), Audio (MP3), or Best Available
- **Quality Control** — Choose from Best, 1080p, 720p, 480p, 360p
- **Playlist Support** — Download entire playlists
- **Live Progress** — Real-time download progress bar
- **URL Preview** — See video title, thumbnail, and available formats before downloading

## Requirements

- **Python 3.10+**
- **ffmpeg** (for audio extraction and video merging)

## Setup

### 1. Install ffmpeg

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg

# Windows (via chocolatey)
choco install ffmpeg
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Run the app

```bash
python app.py
```

Open **http://localhost:5000** in your browser.

## Project Structure

```
Everything/
├── app.py                 # Flask backend + yt-dlp integration
├── requirements.txt       # Python dependencies
├── templates/
│   └── index.html         # Frontend (HTML + CSS + JS)
├── downloads/             # Downloaded files stored here
└── README.md
```

## How It Works

1. Paste any supported URL
2. Click **Fetch** → sees title, thumbnail, formats
3. Choose format (Video/Audio) and quality
4. Click **Download** → progress bar shows real-time status
5. Click **Save File** to download to your device

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Main page |
| POST | `/api/info` | Get URL metadata (title, formats, etc.) |
| POST | `/api/download` | Start a download task |
| GET | `/api/task/<id>` | Poll download progress |
| GET | `/api/file/<name>` | Download a completed file |
