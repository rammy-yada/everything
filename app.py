import os
import threading
import uuid
import time
from pathlib import Path

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

# In-memory task store {task_id: {status, progress, ...}}
tasks: dict[str, dict] = {}


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
                        fname = ydl.prepare_filename(entry)
                        if fmt == "audio":
                            fname = Path(fname).with_suffix(".mp3").name
                        else:
                            fname = Path(fname).name
                        filenames.append(fname)
                tasks[task_id].update(
                    status="done",
                    progress=100,
                    filenames=filenames,
                    title=info.get("title", "Playlist"),
                )
            else:
                fname = ydl.prepare_filename(info)
                if fmt == "audio":
                    fname = Path(fname).with_suffix(".mp3").name
                else:
                    fname = Path(fname).name
                tasks[task_id].update(
                    status="done",
                    progress=100,
                    filename=fname,
                    title=info.get("title", url),
                )
    except Exception as exc:
        tasks[task_id].update(status="error", error=str(exc))


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
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
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


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
