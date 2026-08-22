import os
import shutil
import subprocess
import threading
import uuid
import time
import base64
import re
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_from_directory,
)
import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = Path(__file__).parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

COOKIES_FILE = Path(__file__).parent / "cookies.txt"

# Auto-cleanup files older than 1 hour
MAX_FILE_AGE = 3600

# YouTube player clients to try (bypass age restrictions)
YT_PLAYER_CLIENTS = ["android_creator", "mediaconnect", "web_creator", "android", "ios"]

# In-memory task store {task_id: {status, progress, ...}}
tasks: dict[str, dict] = {}

# Browser-like headers
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ---------------------------------------------------------------------------
# Cookie management — real cookies required for YouTube on datacenter IPs
# ---------------------------------------------------------------------------

def _init_cookies_from_env():
    """On startup, write YT_COOKIES env var (base64 of cookies.txt) to disk."""
    env_cookies = os.environ.get("YT_COOKIES", "").strip()
    if env_cookies and not COOKIES_FILE.is_file():
        try:
            raw = base64.b64decode(env_cookies)
            COOKIES_FILE.write_bytes(raw)
        except Exception:
            pass


_init_cookies_from_env()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cleanup_old_files():
    """Remove downloaded files older than MAX_FILE_AGE."""
    now = time.time()
    for f in DOWNLOAD_DIR.iterdir():
        if f.name == ".gitkeep":
            continue
        try:
            if now - f.stat().st_mtime > MAX_FILE_AGE:
                f.unlink()
        except OSError:
            pass


def _base_opts() -> dict:
    """Base yt-dlp options: cookies, headers, speed optimisations."""
    opts: dict = {
        "http_headers": _HEADERS,
        "extractor_args": {
            "youtube": {"player_client": YT_PLAYER_CLIENTS},
        },
        # Speed optimisations
        "concurrent_fragment_downloads": 8,
        "buffersize": 1024 * 64,
        "http_chunk_size": 10485760,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "noprogress": True,
    }
    # Only pass cookiefile if we have one — avoids yt-dlp creating empty file
    if COOKIES_FILE.is_file() and COOKIES_FILE.stat().st_size > 10:
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def _progress_hook(task_id):
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total * 100) if total else 0
            tasks[task_id].update(
                status="downloading",
                progress=round(pct, 1),
                speed=d.get("_speed_str", ""),
                eta=d.get("_eta_str", ""),
            )
        elif d["status"] == "finished":
            tasks[task_id].update(status="processing", progress=100)
    return hook


def _run_download(task_id: str, url: str, fmt: str, quality: str):
    try:
        _cleanup_old_files()

        if fmt in ("audio", "video", "gif", "best") and not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg is required for compatible media downloads. Install ffmpeg and try again.")

        ydl_opts: dict = {
            "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
            "progress_hooks": [_progress_hook(task_id)],
            "noplaylist": False,
            "restrictfilenames": True,
            "quiet": True,
            "no_warnings": True,
            **_base_opts(),
        }

        if fmt == "audio":
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
        elif fmt == "image":
            # Download the thumbnail / image at best quality
            ydl_opts["skip_download"] = True
            ydl_opts["writethumbnail"] = True
            ydl_opts["outtmpl"] = str(DOWNLOAD_DIR / "%(title)s.%(ext)s")
            ydl_opts["postprocessors"] = []
        elif fmt == "gif":
            # Download video and convert to GIF via ffmpeg
            ydl_opts["format"] = "bestvideo[height<=480][ext=mp4]/bestvideo[height<=480]/best[height<=480]/best"
            ydl_opts["merge_output_format"] = "mp4"
            # We'll convert to GIF after download in a post-step
        elif fmt in ("video", "best"):
            quality_map = {
                "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
                "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
                "720": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
                "480": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/best",
                "360": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best[height<=360]/best",
            }
            ydl_opts["format"] = quality_map.get(quality, quality_map["best"])
            ydl_opts["merge_output_format"] = "mp4"
        else:
            ydl_opts["format"] = "best"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise RuntimeError("Could not extract info from URL")

            entries = info.get("entries")
            if entries:
                filenames = []
                for entry in entries:
                    if entry:
                        fname = _resolve_filename(ydl, entry, fmt)
                        if fmt in ("video", "best"):
                            fname = _normalize_video(task_id, fname)
                        filenames.append(fname)
                tasks[task_id].update(
                    status="done",
                    progress=100,
                    filenames=filenames,
                    title=info.get("title", "Playlist"),
                )
            else:
                fname = _resolve_filename(ydl, info, fmt)

                if fmt in ("video", "best"):
                    fname = _normalize_video(task_id, fname)

                # GIF conversion: ffmpeg mp4 → gif (first 15s, max 480px wide)
                if fmt == "gif":
                    tasks[task_id].update(status="processing", progress=100)
                    fname = _convert_to_gif(fname)

                tasks[task_id].update(
                    status="done",
                    progress=100,
                    filename=fname,
                    title=info.get("title", url),
                )
    except Exception as exc:
        tasks[task_id].update(status="error", error=str(exc))


def _resolve_filename(ydl, info: dict, fmt: str) -> str:
    """Work out the final on-disk filename based on format."""
    fname = ydl.prepare_filename(info)
    if fmt == "audio":
        return Path(fname).with_suffix(".mp3").name
    if fmt == "image":
        # yt-dlp writes thumbnail next to video; find the image file
        base = Path(fname).stem
        for ext in (".jpg", ".png", ".webp", ".jpeg"):
            candidate = DOWNLOAD_DIR / (base + ext)
            if candidate.is_file():
                return candidate.name
        # fallback: return original name (thumbnail may have .webp etc)
        return Path(fname).name
    return Path(fname).name


def _normalize_video(task_id: str, video_filename: str) -> str:
    """Transcode video to the H.264/AAC MP4 profile supported by editors."""
    source = DOWNLOAD_DIR / video_filename
    final_name = Path(video_filename).with_suffix(".mp4").name
    destination = DOWNLOAD_DIR / (Path(final_name).stem + ".compatible.mp4")
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,pix_fmt",
            "-of", "default=noprint_wrappers=1:nokey=1", str(source),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.splitlines()
    audio_probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=noprint_wrappers=1:nokey=1", str(source),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    compatible = probe[:2] == ["h264", "yuv420p"] and audio_probe in ("", "aac")
    video_args = ["-c", "copy"] if compatible else [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
    ]
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?",
            *video_args,
            "-movflags", "+faststart", str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    source.unlink(missing_ok=True)
    final_path = DOWNLOAD_DIR / final_name
    destination.replace(final_path)
    return final_path.name


def _convert_to_gif(video_filename: str) -> str:
    """Convert a downloaded video to a GIF (first 15 seconds, 480px wide, 12fps)."""
    import subprocess
    src = DOWNLOAD_DIR / video_filename
    gif_name = Path(video_filename).with_suffix(".gif").name
    dst = DOWNLOAD_DIR / gif_name
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(src),
                "-t", "15",
                "-vf", "fps=12,scale=480:-1:flags=lanczos",
                "-loop", "0",
                str(dst),
            ],
            capture_output=True, timeout=120,
        )
        # Remove source video after conversion
        if dst.is_file():
            src.unlink(missing_ok=True)
            return gif_name
    except Exception:
        pass
    # If conversion fails, return the original video
    return video_filename


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    if not url:
        return jsonify(error="URL is required"), 400

    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, **_base_opts()}) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                return jsonify(error="Could not extract info"), 400

            entries = info.get("entries")
            if entries:
                items = []
                for e in entries:
                    if e:
                        items.append({
                            "title": e.get("title"),
                            "duration": e.get("duration"),
                            "thumbnail": e.get("thumbnail"),
                        })
                return jsonify(
                    is_playlist=True,
                    title=info.get("title"),
                    count=len(items),
                    items=items[:50],
                    extractor=info.get("extractor_key", ""),
                )
            else:
                formats_available = []
                for f in (info.get("formats") or []):
                    h = f.get("height")
                    if h and h not in [x["height"] for x in formats_available]:
                        formats_available.append({"height": h, "ext": f.get("ext", "mp4")})
                formats_available.sort(key=lambda x: x["height"], reverse=True)
                return jsonify(
                    is_playlist=False,
                    title=info.get("title"),
                    duration=info.get("duration"),
                    thumbnail=info.get("thumbnail"),
                    uploader=info.get("uploader"),
                    view_count=info.get("view_count"),
                    formats=formats_available,
                    extractor=info.get("extractor_key", ""),
                )
    except Exception as exc:
        return jsonify(error=str(exc)), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    fmt = data.get("format", "video")
    quality = data.get("quality", "best")

    if not url:
        return jsonify(error="URL is required"), 400

    task_id = str(uuid.uuid4())
    tasks[task_id] = {"status": "queued", "progress": 0}

    t = threading.Thread(target=_run_download, args=(task_id, url, fmt, quality), daemon=True)
    t.start()

    return jsonify(task_id=task_id)


@app.route("/api/task/<task_id>")
def task_status(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify(error="Unknown task"), 404
    return jsonify(**task)


@app.route("/api/file/<path:filename>")
def serve_file(filename):
    safe_name = Path(filename).name
    return send_from_directory(DOWNLOAD_DIR, safe_name, as_attachment=True)


@app.route("/api/cookies", methods=["GET"])
def cookies_status():
    """Check if cookies are configured."""
    has_env = bool(os.environ.get("YT_COOKIES", "").strip())
    has_file = COOKIES_FILE.is_file() and COOKIES_FILE.stat().st_size > 10
    return jsonify(
        has_cookies=has_file,
        source="env" if has_env else ("file" if has_file else "none"),
    )


@app.route("/api/cookies", methods=["POST"])
def upload_cookies():
    """Upload a cookies.txt file (Netscape format)."""
    f = request.files.get("file")
    if not f:
        return jsonify(error="No file uploaded"), 400
    content = f.read().decode("utf-8", errors="replace")
    if not content.strip():
        return jsonify(error="File is empty"), 400
    # Basic sanity check
    if "# Netscape HTTP Cookie" not in content and "\t" not in content:
        return jsonify(error="This doesn't look like a Netscape cookies.txt file. Export using the 'Get cookies.txt LOCALLY' extension."), 400
    COOKIES_FILE.write_text(content)
    return jsonify(ok=True, message="Cookies saved successfully")


@app.route("/api/cookies", methods=["DELETE"])
def delete_cookies():
    """Remove the cookies.txt file."""
    if COOKIES_FILE.is_file():
        COOKIES_FILE.unlink()
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
