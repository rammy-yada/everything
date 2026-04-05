import os
import threading
import uuid
import time
import tempfile
import atexit
import http.cookiejar
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

# Auto-cleanup files older than 1 hour
MAX_FILE_AGE = 3600

# YouTube player clients to try (bypass age restrictions without cookies)
YT_PLAYER_CLIENTS = ["android_creator", "mediaconnect", "web_creator", "android", "ios"]

# In-memory task store {task_id: {status, progress, ...}}
tasks: dict[str, dict] = {}

# Browser-like headers shared by all requests
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Mode": "navigate",
}

# ---------------------------------------------------------------------------
# Per-platform cookie definitions (non-authenticated, consent/visitor only)
# ---------------------------------------------------------------------------
_PLATFORM_COOKIES: dict[str, list[tuple[str, str, str]]] = {
    # (name, value, domain)
    ".youtube.com": [
        ("SOCS", "CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjQwNDIxLjA3X3AxGgJlbiADGgYIgJy2sgY", ".youtube.com"),
        ("CONSENT", "PENDING+987", ".youtube.com"),
        ("GPS", "1", ".youtube.com"),
        ("VISITOR_INFO1_LIVE", "OmxCGPeCF98", ".youtube.com"),
        ("YSC", "DsLg2m1xJQo", ".youtube.com"),
        ("PREF", "f4=4000000&tz=America.New_York&f6=40000000", ".youtube.com"),
    ],
    ".tiktok.com": [
        ("tt_csrf_token", "auto", ".tiktok.com"),
        ("tt_webid_v2", "7355000000000000000", ".tiktok.com"),
        ("ttwid", "1%7Cauto%7C1700000000%7Cab1234567890abcdef1234567890abcdef1234567890abcdef1234567890ab", ".tiktok.com"),
        ("tt_chain_token", "auto", ".tiktok.com"),
        ("cookie-consent", "{%22ga%22:true,%22af%22:true,%22fbp%22:true,%22lip%22:true%2C%22bing%22:true}", ".tiktok.com"),
    ],
    ".instagram.com": [
        ("ig_did", "A0000000-0000-0000-0000-000000000000", ".instagram.com"),
        ("ig_nrcb", "1", ".instagram.com"),
        ("csrftoken", "auto", ".instagram.com"),
        ("mid", "Zm0AAAAAAAAAAAAAAAAAAA", ".instagram.com"),
        ("datr", "auto", ".instagram.com"),
    ],
    ".twitter.com": [
        ("guest_id", "v1%3A170000000000000000", ".twitter.com"),
        ("gt", "1700000000000000000", ".twitter.com"),
        ("d_prefs", "MjoxLGNvbnNlbnRfdmVyc2lvbjoyLHRleHRfdmVyc2lvbjoxMDAw", ".twitter.com"),
        ("guest_id_ads", "v1%3A170000000000000000", ".twitter.com"),
        ("guest_id_marketing", "v1%3A170000000000000000", ".twitter.com"),
    ],
    ".x.com": [
        ("guest_id", "v1%3A170000000000000000", ".x.com"),
        ("gt", "1700000000000000000", ".x.com"),
        ("d_prefs", "MjoxLGNvbnNlbnRfdmVyc2lvbjoyLHRleHRfdmVyc2lvbjoxMDAw", ".x.com"),
    ],
    ".facebook.com": [
        ("datr", "auto", ".facebook.com"),
        ("sb", "auto", ".facebook.com"),
        ("locale", "en_US", ".facebook.com"),
        ("wd", "1920x1080", ".facebook.com"),
    ],
    ".reddit.com": [
        ("csv", "2", ".reddit.com"),
        ("edgebucket", "auto", ".reddit.com"),
    ],
}


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


# Cached cookie file path
_COOKIE_JAR_PATH: str | None = None


def _get_cookie_file() -> str:
    """Build a Netscape cookie file containing consent cookies for all platforms."""
    global _COOKIE_JAR_PATH
    if _COOKIE_JAR_PATH and os.path.isfile(_COOKIE_JAR_PATH):
        return _COOKIE_JAR_PATH

    jar = http.cookiejar.MozillaCookieJar()
    expires = int(time.time()) + 365 * 24 * 3600

    for domain, cookies in _PLATFORM_COOKIES.items():
        for name, value, cookie_domain in cookies:
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name=name, value=value,
                port=None, port_specified=False,
                domain=cookie_domain, domain_specified=True,
                domain_initial_dot=cookie_domain.startswith("."),
                path="/", path_specified=True,
                secure=True, expires=expires, discard=False,
                comment=None, comment_url=None,
                rest={"HttpOnly": ""},
            ))

    fd, path = tempfile.mkstemp(suffix=".txt", prefix="dl_cookies_")
    os.close(fd)
    jar.save(path, ignore_discard=True, ignore_expires=True)
    _COOKIE_JAR_PATH = path
    atexit.register(lambda: os.unlink(path) if os.path.isfile(path) else None)
    return path


def _detect_platform(url: str) -> str:
    """Return a platform key from the URL for logging/detection."""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return "unknown"
    host = host.lower()
    for key in ("youtube", "youtu.be", "tiktok", "instagram", "twitter",
                "x.com", "facebook", "fb.watch", "reddit", "vimeo",
                "soundcloud", "twitch", "dailymotion", "bandcamp", "threads"):
        if key in host:
            return key.replace(".com", "").replace(".be", "")
    return "other"


def _base_opts() -> dict:
    """Base yt-dlp options: cookies, headers, speed optimisations."""
    return {
        "cookiefile": _get_cookie_file(),
        "http_headers": _HEADERS,
        "extractor_args": {
            "youtube": {"player_client": YT_PLAYER_CLIENTS},
        },
        # Speed optimisations
        "concurrent_fragment_downloads": 8,
        "buffersize": 1024 * 64,       # 64 KB buffer
        "http_chunk_size": 10485760,    # 10 MB chunks
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "noprogress": True,
    }


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
        elif fmt == "video":
            quality_map = {
                "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "1080": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]",
                "720": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]",
                "480": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]",
                "360": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]",
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
                        filenames.append(fname)
                tasks[task_id].update(
                    status="done",
                    progress=100,
                    filenames=filenames,
                    title=info.get("title", "Playlist"),
                )
            else:
                fname = _resolve_filename(ydl, info, fmt)

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


@app.route("/api/status")
def server_status():
    """Health check endpoint."""
    return jsonify(
        ok=True,
        bypass_clients=YT_PLAYER_CLIENTS,
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
