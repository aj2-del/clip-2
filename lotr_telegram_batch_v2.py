#!/usr/bin/env python3
"""
LOTR Short Clips -> Telegram | One-Time Batch Automation v2
=============================================================

Searches YouTube with yt-dlp ONLY (no YouTube Data API key),
downloads candidate videos, trims exactly the middle 10 seconds
(or less if the source is shorter), re-encodes to Telegram-friendly
MP4 (H.264 + AAC + faststart), and uploads to Telegram via
Bot API sendVideo.

Execution model: ONE-TIME, sequential, exits cleanly.
No schedule / APScheduler / cron loop.

Categories (10 clips each, 30 total by default):
  1. Landscape & Cinematography  -> #Cinematography
  2. Battle & Action Scenes      -> #BattleScene
  3. Iconic Dialogue & Moments   -> #IconicMoment

What changed in v2 (vs the 2026-10-26 v1):
  - HD is now ENFORCED, not just requested: format asks for
    height >= 720, and the downloaded file's actual height is
    checked with ffprobe. Anything < 720p is rejected, and the
    height is re-checked after any player-client fallback.
  - No subtitles: subtitle tracks are never downloaded
    (--no-write-subs) and are stripped on encode (-sn, with an
    explicit video+audio map so no subtitle stream can survive).
    Burned-in subtitles cannot be stripped, so every planned
    10s window is screened at 1 fps across its FULL window
    before upload (see screen_full_window()).
  - Title check: the candidate title must name the requested
    LOTR work. A title naming a different work is rejected
    even if search returned it.
  - Language exclusion applies to title AND uploader (Thai /
    Thai-channel candidates are skipped - a burned-in Thai
    subtitle cannot be removed).
  - yt-dlp is invoked as `python -m yt_dlp` when no `yt-dlp`
    binary is on PATH (the usual case on this VM).
  - Separate records file for this batch variant, so this run
    never borrows / repeats segments from an earlier variant.
  - YouTube cookies (v2.1, 2026-11-09): `--cookies-file` /
    `YOUTUBE_COOKIES_FILE` passes a Netscape-format cookies.txt
    to every yt-dlp search and download (`--cookies <file>`).
    Cookies remove the "Sign in to confirm you're not a bot"
    page, but they do NOT change the runner's IP reputation:
    on a flagged datacenter IP YouTube has returned only
    storyboard thumbnails even with valid cookies, so HD is
    still not guaranteed. Never put cookie values in this file,
    in the repo, or in a workflow file - pass the file path only,
    and store its contents in a Secret (GitHub) / a 600-mode
    file (local) only.

Requirements:
  - Python 3.9+
  - yt-dlp:  pip install yt-dlp
  - ffmpeg + ffprobe on PATH
  - pip install requests
  - Optional, for automatic burned-subtitle screening:
    tesseract-ocr on PATH (`apt install tesseract-ocr`).
    Without it, 1-fps frames are still saved for manual review
    and the clip is marked UNVERIFIED in the log / records file.

Usage:
  export TELEGRAM_BOT_TOKEN="your_bot_token_here"
  export TELEGRAM_CHAT_ID="your_chat_or_channel_id_here"

  python3 lotr_telegram_batch_v2.py
  python3 lotr_telegram_batch_v2.py --max-per-category 10 --dry-run
  python3 lotr_telegram_batch_v2.py --max-per-category 1   # pilot: 1 per category

Notes:
  - Credentials are NEVER hard-coded. They come only from env vars.
    Do not paste your token into this file.
  - Duplicate prevention: sent_clips.json next to this script stores
    every successfully uploaded YouTube video ID (shared with v1).
    Detailed per-segment records go to sent_lotr_3cat_10s.json.
  - A quota is left unfilled if no candidate passes every check.
    The script reports the shortfall instead of filling it from
    another category / movie.
  - Only upload clips you have the right to share. Respect YouTube's
    Terms of Service and copyright law. Short excerpts are not
    automatically fair use in every jurisdiction / channel context.
"""

import argparse
import html
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
SENT_FILE = SCRIPT_DIR / "sent_clips.json"
RECORDS_FILE = SCRIPT_DIR / "sent_lotr_3cat_10s.json"
SCREENING_DIR = SCRIPT_DIR / "screening_lotr_3cat_10s"

CLIP_DURATION = 10.0          # seconds, STRICT MAX
MIN_HEIGHT = 720              # HD minimum, enforced on the real file
TELEGRAM_MAX_BYTES = 50 * 1024 * 1024
TELEGRAM_SAFE_BYTES = 48 * 1024 * 1024  # stay well under the limit

NEGATIVE_KEYWORDS = [
    "reaction", "review", "analysis", "gameplay", "lego", "parody",
    "trailer", "vlog", "interview", "ranking", "shorts", "meme",
]

# Title must contain at least one of these (case-insensitive).
# Rejects a candidate naming a different work even if search returned it.
TITLE_MUST_CONTAIN = [
    "lord of the rings", "fellowship", "two towers", "return of the king",
    "middle-earth", "middle earth", "shire", "rivendell", "rohan",
    "gondor", "helm", "pelennor", "minas tirith", "gandalf", "frodo",
    "samwise", "aragorn", "legolas", "gimli", "gollum", "sauron",
    "mordor", "argonath", "isengard", "rohirrim",
]

# Language exclusion: title AND uploader. Thai burned-in subtitles
# cannot be removed by stripping a subtitle track.
LANGUAGE_EXCLUDE = ["thai", "thailand", "ไทย"]

CATEGORIES = {
    "landscape": {
        "name": "Landscape & Cinematography",
        "tag": "Cinematography",
        "queries": [
            "Lord of the Rings Shire landscape cinematography",
            "Lord of the Rings Rohan scenery",
            "Lord of the Rings Rivendell beautiful scenery",
            "Fellowship of the Ring New Zealand landscape scenes",
            "Lord of the Rings Argonath scenery",
        ],
    },
    "battle": {
        "name": "Battle & Action Scenes",
        "tag": "BattleScene",
        "queries": [
            "Helm's Deep battle Lord of the Rings",
            "Pelennor Fields battle Lord of the Rings",
            "Black Gate battle Return of the King",
            "Battle of Minas Tirith Lord of the Rings",
            "Lord of the Rings battle scenes",
        ],
    },
    "iconic": {
        "name": "Iconic Dialogue & Moments",
        "tag": "IconicMoment",
        "queries": [
            "You shall not pass Gandalf Lord of the Rings",
            "Gollum monologue Lord of the Rings my precious",
            "Ride of the Rohirrim Return of the King",
            "Lord of the Rings iconic moments dialogue",
            "Frodo Sam Lord of the Rings emotional scene",
        ],
    },
}

SEARCH_RESULTS_PER_QUERY = 25

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lotr-batch-v2")


# ---------------------------------------------------------------------------
# Helpers: sent history, filtering
# ---------------------------------------------------------------------------

def load_sent_ids(path: Path) -> set:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(data)
        if isinstance(data, dict):
            return set(data.keys())
        return set()
    except Exception as exc:
        log.warning("Could not read %s (%s) - starting with empty history", path, exc)
        return set()


def save_sent_ids(path: Path, ids: set) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")
    tmp.replace(path)


def load_records(path: Path) -> list:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_records(path: Path, records: list) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def contains_negative_keyword(title: str, uploader: str = "") -> str | None:
    lowered = f"{title or ''} {uploader or ''}".lower()
    for word in NEGATIVE_KEYWORDS:
        if word in lowered:
            return word
    return None


def language_excluded(title: str, uploader: str = "") -> str | None:
    lowered = f"{title or ''} {uploader or ''}".lower()
    for word in LANGUAGE_EXCLUDE:
        if word in lowered:
            return word
    return None


def title_names_lotr(title: str) -> bool:
    lowered = (title or "").lower()
    return any(k in lowered for k in TITLE_MUST_CONTAIN)


def random_delay(min_s: float = 2.0, max_s: float = 5.0) -> None:
    delay = random.uniform(min_s, max_s)
    log.info("Sleeping %.1fs to avoid rate limits...", delay)
    time.sleep(delay)


def ytdlp_base_cmd() -> list[str]:
    """Prefer a `yt-dlp` binary, fall back to `python -m yt_dlp`."""
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    return [sys.executable, "-m", "yt_dlp"]


# YouTube cookies file (Netscape format), set in main() from
# --cookies-file / YOUTUBE_COOKIES_FILE. None = anonymous.
COOKIES_FILE: Path | None = None


def cookies_args() -> list[str]:
    """--cookies <file> for every yt-dlp call, or [] if anonymous."""
    if COOKIES_FILE is not None and COOKIES_FILE.exists():
        return ["--cookies", str(COOKIES_FILE)]
    return []


# ---------------------------------------------------------------------------
# yt-dlp: search + download (no YouTube API key anywhere)
# ---------------------------------------------------------------------------

def search_youtube(query: str, max_results: int = SEARCH_RESULTS_PER_QUERY) -> list[dict]:
    """Returns [{id, title, uploader, duration, url}] via ytsearch."""
    cmd = ytdlp_base_cmd() + cookies_args() + [
        f"ytsearch{max_results}:{query}",
        "--dump-json",
        "--skip-download",
        "--no-playlist",
        "--no-warnings",
        "--ignore-errors",
    ]
    log.info('Searching YouTube via yt-dlp: "%s"', query)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        log.warning("Search timed out for query: %s", query)
        return []

    results = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = info.get("id")
        if not vid:
            continue
        results.append({
            "id": vid,
            "title": info.get("title") or "Untitled",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": info.get("duration"),
            "url": f"https://www.youtube.com/watch?v={vid}",
        })
    log.info("  -> %d candidates from this query", len(results))
    return results


def download_video(video_id: str, output_path: Path) -> bool:
    """
    Download best >=720p. Merged to MP4 if needed.
    Retries with android / ios player clients on bot-check failure.
    Returns True on success AND actual height >= 720 (checked by caller
    via probe_height, but a first-pass check happens here too).
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    for client in (None, "android", "ios"):
        cmd = ytdlp_base_cmd() + cookies_args() + [
            "-f", "bv*[height>=720]+ba/b[height>=720]/bv*+ba/b",
            "--merge-output-format", "mp4",
            "--no-write-subs", "--no-write-auto-subs",
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "-o", str(output_path),
            url,
        ]
        if client:
            cmd[1:1] = ["--extractor-args", f"youtube:player_client={client}"]
            log.info("  Retrying download with player_client=%s", client)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
            log.info("  Downloaded raw: %s (%.1f MB)",
                     output_path.name, output_path.stat().st_size / 1024 / 1024)
            return True
        if output_path.exists():
            output_path.unlink(missing_ok=True)
        for part in output_path.parent.glob(output_path.name + ".part*"):
            part.unlink(missing_ok=True)
        if proc.stderr:
            log.error("Download failed for %s (client=%s): %s",
                      video_id, client or "web", proc.stderr[-300:])
    return False


# ---------------------------------------------------------------------------
# FFmpeg / ffprobe: duration, height, screening, trim + encode
# ---------------------------------------------------------------------------

def probe_duration(path: Path) -> float | None:
    cmd = ["ffprobe", "-v", "error",
           "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return float(proc.stdout.strip())
    except Exception:
        return None


def probe_height(path: Path) -> int | None:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=height",
           "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return int(proc.stdout.strip())
    except Exception:
        return None


def probe_has_subtitle_stream(path: Path) -> bool:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "s",
           "-show_entries", "stream=index",
           "-of", "csv=p=0", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return bool(proc.stdout.strip())
    except Exception:
        return False


def extract_window_frames(raw_path: Path, start: float, length: float,
                          frames_dir: Path) -> list[Path]:
    """Extract 1 frame per second across the EXACT planned window."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-ss", f"{start:.3f}", "-i", str(raw_path),
           "-t", f"{length:.3f}", "-vf", "fps=1,scale=640:-1",
           str(frames_dir / "frame_%02d.jpg")]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        log.warning("Frame extraction failed: %s", proc.stderr[-300:] if proc.stderr else "")
        return []
    return sorted(frames_dir.glob("frame_*.jpg"))


def ocr_bottom_third(frame: Path) -> str:
    """OCR the bottom third of one frame (where subtitles live)."""
    if not shutil.which("tesseract"):
        return ""
    # Crop bottom 30% to a temp file, then OCR it.
    cropped = frame.with_suffix(".crop.jpg")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(frame),
           "-vf", "crop=iw:ih*0.30:0:ih*0.70", str(cropped)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or not cropped.exists():
        return ""
    try:
        out = subprocess.run(["tesseract", str(cropped), "stdout", "--psm", "6"],
                             capture_output=True, text=True, timeout=60)
        return (out.stdout or "").strip()
    except Exception:
        return ""
    finally:
        cropped.unlink(missing_ok=True)


def screen_full_window(raw_path: Path, start: float, length: float,
                       video_id: str) -> tuple[bool, str]:
    """
    Screen the FULL 10s window at 1 fps for burned-in subtitles / text.
    Returns (passed, note).
    - With tesseract: any frame whose bottom third OCRs to >= 3 letters
      fails the window. A narration-heavy source fails the same way.
    - Without tesseract: frames are saved under screening_lotr_3cat_10s/
      for manual review and the clip is marked UNVERIFIED (not claimed
      clean). Set --require-ocr to make this a hard fail instead.
    """
    frames_dir = SCREENING_DIR / video_id
    frames = extract_window_frames(raw_path, start, length, frames_dir)
    if not frames:
        return False, "no frames extracted for screening"
    if not shutil.which("tesseract"):
        return True, (f"UNVERIFIED - no tesseract, {len(frames)} frames saved "
                      f"in {frames_dir} for manual review")
    for frame in frames:
        text = ocr_bottom_third(frame)
        letters = "".join(c for c in text if c.isalpha())
        if len(letters) >= 3:
            return False, f"burned-in text in {frame.name}: {text[:80]!r}"
    return True, f"clean - {len(frames)} frames screened, no subtitle text found"


def trim_and_encode(raw_path: Path, out_path: Path,
                    full_duration: float, crf: int = 23,
                    audio_bitrate: str = "128k") -> bool:
    """
    Extract max 10s from the middle:
        start = max(0, duration/2 - 5)
        length = min(10, duration - start)
    Re-encode: libx264 + AAC + faststart, scaled to max 1280 wide.
    -sn strips subtitle tracks; explicit -map drops any other streams.
    """
    start = max(0.0, (full_duration / 2.0) - 5.0)
    length = min(CLIP_DURATION, full_duration - start)
    if length <= 0:
        return False

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(raw_path),
        "-t", f"{length:.3f}",
        "-map", "0:v:0", "-map", "0:a:0?",
        "-sn",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-vf", "scale='min(1280,iw)':-2",
        "-c:a", "aac",
        "-b:a", audio_bitrate,
        "-movflags", "+faststart",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out_path.exists():
        log.error("FFmpeg failed: %s", proc.stderr[-500:] if proc.stderr else "")
        return False

    size_mb = out_path.stat().st_size / 1024 / 1024
    log.info("  Trimmed clip: start=%.1fs len=%.1fs size=%.1f MB (crf=%d)",
             start, length, size_mb, crf)
    return True


def build_clip(raw_path: Path, out_path: Path, full_duration: float) -> bool:
    """First pass at CRF 23; if still too big, second pass at CRF 28."""
    if not trim_and_encode(raw_path, out_path, full_duration, crf=23):
        return False
    if out_path.stat().st_size <= TELEGRAM_SAFE_BYTES:
        return True
    log.warning("  Clip >48 MB, re-encoding at lower quality (CRF 28, 96k audio)...")
    if not trim_and_encode(raw_path, out_path, full_duration, crf=28, audio_bitrate="96k"):
        return False
    if out_path.stat().st_size > TELEGRAM_MAX_BYTES:
        log.error("  Clip still exceeds Telegram 50 MB limit, skipping.")
        return False
    return True


# ---------------------------------------------------------------------------
# Telegram: stream upload via Bot API (requests, no python-telegram-bot needed)
# ---------------------------------------------------------------------------

def build_caption(category: dict, title: str, video_id: str,
                  index: int, total: int) -> str:
    safe_title = html.escape(title)
    return (
        f"#{category['tag']}\n"
        f"<b>{safe_title}</b>\n\n"
        f"[Clip {index}/{total}] • {category['name']}\n\n"
        f"Source: https://www.youtube.com/watch?v={video_id}"
    )


def upload_to_telegram(token: str, chat_id: str, video_path: Path,
                       caption: str) -> int | None:
    url = f"https://api.telegram.org/bot{token}/sendVideo"
    with open(video_path, "rb") as f:
        files = {"video": (video_path.name, f, "video/mp4")}
        data = {
            "chat_id": chat_id,
            "caption": caption,
            "parse_mode": "HTML",
            "supports_streaming": "true",
        }
        try:
            resp = requests.post(url, data=data, files=files, timeout=180)
        except requests.RequestException as exc:
            log.error("Telegram request failed: %s", exc)
            return None

    try:
        payload = resp.json()
    except ValueError:
        log.error("Telegram returned non-JSON (HTTP %s): %s", resp.status_code, resp.text[:300])
        return None

    if not payload.get("ok"):
        log.error("Telegram error: %s", payload.get("description"))
        return None

    message_id = payload.get("result", {}).get("message_id")
    log.info("  Uploaded OK, message_id=%s", message_id)
    return message_id


# ---------------------------------------------------------------------------
# Main batch flow (one-time, sequential)
# ---------------------------------------------------------------------------

def process_category(category_key: str, category: dict, token: str, chat_id: str,
                     sent_ids: set, records: list, max_per_category: int,
                     dry_run: bool, require_ocr: bool,
                     sent_file: Path = SENT_FILE,
                     records_file: Path = RECORDS_FILE) -> int:
    sent_count = 0
    seen_this_run: set = set()
    seen_segments: set = {(r.get("video_id"), r.get("start")) for r in records}

    log.info("=" * 70)
    log.info("CATEGORY: %s  (target: %d clips)", category["name"], max_per_category)
    log.info("=" * 70)

    for query in category["queries"]:
        if sent_count >= max_per_category:
            break

        candidates = search_youtube(query)
        random_delay(2, 5)

        for cand in candidates:
            if sent_count >= max_per_category:
                break

            video_id = cand["id"]
            title = cand["title"]
            uploader = cand.get("uploader", "")

            if video_id in sent_ids:
                log.info("  SKIP (already sent): %s", title)
                continue
            if video_id in seen_this_run:
                continue
            seen_this_run.add(video_id)

            bad = contains_negative_keyword(title, uploader)
            if bad:
                log.info('  SKIP (negative keyword "%s"): %s', bad, title)
                continue

            lang = language_excluded(title, uploader)
            if lang:
                log.info('  SKIP (language exclusion "%s"): %s', lang, title)
                continue

            if not title_names_lotr(title):
                log.info("  SKIP (title does not name LOTR work): %s", title)
                continue

            log.info("[%s %d/%d] Processing: %s (%s) by %s",
                     category["tag"], sent_count + 1, max_per_category,
                     title, video_id, uploader or "unknown")

            tmpdir = Path(tempfile.mkdtemp(prefix="lotr_clip_"))
            try:
                raw_path = tmpdir / "raw.mp4"
                clip_path = tmpdir / "clip.mp4"

                if not download_video(video_id, raw_path):
                    continue
                random_delay(2, 5)

                # HD check on the ACTUAL file, after any fallback.
                height = probe_height(raw_path)
                if height is None:
                    log.error("  Could not probe height, skipping")
                    continue
                if height < MIN_HEIGHT:
                    log.info("  SKIP (actual height %dp < %dp)", height, MIN_HEIGHT)
                    continue
                log.info("  Actual height: %dp OK", height)

                if probe_has_subtitle_stream(raw_path):
                    log.info("  Note: source has a subtitle stream - it will be stripped (-sn)")

                duration = cand.get("duration") or probe_duration(raw_path)
                if not duration:
                    log.error("  Could not determine duration, skipping")
                    continue
                if duration < 3:
                    log.info("  SKIP (too short: %.1fs)", duration)
                    continue

                start = max(0.0, (duration / 2.0) - 5.0)
                length = min(CLIP_DURATION, duration - start)
                if (video_id, round(start, 1)) in seen_segments:
                    log.info("  SKIP (segment already sent: %s @ %.1fs)", video_id, start)
                    continue

                # Full-window screening BEFORE cutting / uploading.
                passed, screen_note = screen_full_window(raw_path, start, length, video_id)
                log.info("  Screening: %s", screen_note)
                if not passed:
                    log.info("  SKIP (screening failed): %s", screen_note)
                    continue
                if require_ocr and screen_note.startswith("UNVERIFIED"):
                    log.info("  SKIP (--require-ocr: no tesseract, cannot prove clean)")
                    continue

                if not build_clip(raw_path, clip_path, duration):
                    continue

                final_dur = probe_duration(clip_path)
                if final_dur is not None and final_dur > 10.5:
                    log.error("  Final clip too long (%.2fs), skipping", final_dur)
                    continue
                if probe_has_subtitle_stream(clip_path):
                    log.error("  Final clip still has a subtitle stream, skipping")
                    continue

                caption = build_caption(category, title, video_id,
                                        sent_count + 1, max_per_category)

                if dry_run:
                    log.info("  [DRY-RUN] Would upload with caption:\n%s", caption)
                    message_id = None
                    success = True
                else:
                    message_id = upload_to_telegram(token, chat_id, clip_path, caption)
                    success = message_id is not None

                if success:
                    sent_ids.add(video_id)
                    seen_segments.add((video_id, round(start, 1)))
                    if not dry_run:
                        save_sent_ids(sent_file, sent_ids)
                        records.append({
                            "category": category_key,
                            "title": title,
                            "video_id": video_id,
                            "url": f"https://www.youtube.com/watch?v={video_id}",
                            "start": round(start, 1),
                            "length": round(length, 1),
                            "height": height,
                            "screening": screen_note,
                            "message_id": message_id,
                        })
                        save_records(records_file, records)
                    sent_count += 1
                    log.info("  Progress: %s %d/%d done",
                             category["name"], sent_count, max_per_category)
                else:
                    log.error("  Upload failed for %s, not marking as sent", video_id)

            except Exception as exc:
                log.exception("  Error processing %s: %s", video_id, exc)
            finally:
                # Strict cleanup: raw + trimmed files deleted immediately.
                # Screening frames are kept under screening_lotr_3cat_10s/
                # for review - they are small JPGs, not video.
                shutil.rmtree(tmpdir, ignore_errors=True)

            random_delay(2, 5)

    log.info("CATEGORY DONE: %s -> %d/%d clips",
             category["name"], sent_count, max_per_category)
    if sent_count < max_per_category:
        log.warning("  SHORTFALL: %s left %d clips unfilled - no passing source found",
                    category["name"], max_per_category - sent_count)
    return sent_count


def main() -> int:
    parser = argparse.ArgumentParser(description="LOTR 10s clips -> Telegram (one-time batch) v2")
    parser.add_argument("--max-per-category", type=int, default=10,
                        help="Max clips per category (default: 10)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Search, download and trim, but do NOT upload or mark as sent")
    parser.add_argument("--require-ocr", action="store_true",
                        help="Fail screening when tesseract is not available")
    parser.add_argument("--sent-file", type=str, default=str(SENT_FILE),
                        help="Path to sent_clips.json")
    parser.add_argument("--records-file", type=str, default=str(RECORDS_FILE),
                        help="Path to detailed records JSON")
    parser.add_argument("--category", type=str, default="all",
                        choices=["all", "landscape", "battle", "iconic"],
                        help="Run only one category (for GitHub Actions matrix jobs). Default: all")
    parser.add_argument("--cookies-file", type=str, default=None,
                        help="Path to a Netscape-format YouTube cookies.txt. "
                             "Also read from YOUTUBE_COOKIES_FILE env var. "
                             "Cookie values are never printed or stored in this repo.")
    args = parser.parse_args()

    global COOKIES_FILE
    cookies_path = args.cookies_file or os.getenv("YOUTUBE_COOKIES_FILE", "").strip() or None
    if cookies_path:
        COOKIES_FILE = Path(cookies_path)
        if not COOKIES_FILE.exists():
            log.error("Cookies file not found: %s", COOKIES_FILE)
            return 2
        log.info("Using YouTube cookies file: %s (values never logged)", COOKIES_FILE)
    else:
        log.info("No cookies file given - running anonymous (no --cookies)")

    sent_file = Path(args.sent_file)
    records_file = Path(args.records_file)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.error("Missing credentials. Set env vars before running:")
        log.error("  export TELEGRAM_BOT_TOKEN='...'")
        log.error("  export TELEGRAM_CHAT_ID='...'")
        return 2
    if args.dry_run:
        log.info("DRY-RUN mode: no uploads, no sent_clips.json updates for new clips")

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        log.error("ffmpeg/ffprobe not found on PATH. Install ffmpeg first.")
        return 2

    sent_ids = load_sent_ids(sent_file)
    records = load_records(records_file)
    log.info("Loaded %d previously sent video IDs from %s", len(sent_ids), sent_file)
    log.info("Loaded %d records from %s", len(records), records_file)
    if args.category != "all":
        categories_to_run = {args.category: CATEGORIES[args.category]}
    else:
        categories_to_run = CATEGORIES
    log.info("Starting one-time batch: %d categories x %d clips",
             len(categories_to_run), args.max_per_category)
    log.info("Categories to run: %s", ", ".join(categories_to_run))
    if not shutil.which("tesseract"):
        log.warning("tesseract not found - burned-subtitle screening will be UNVERIFIED "
                    "(frames saved for manual review). Install tesseract-ocr for auto screening.")

    total = 0
    for key, category in categories_to_run.items():
        # A re-run must not exceed the quota: count what this batch
        # variant already sent in this category and only fill the rest.
        already = sum(1 for r in records if r.get("category") == key)
        remaining = args.max_per_category - already
        if remaining <= 0:
            log.info("CATEGORY SKIP: %s already has %d/%d clips",
                     category["name"], already, args.max_per_category)
            continue
        log.info("CATEGORY: %s already sent %d, remaining quota %d",
                 category["name"], already, remaining)
        total += process_category(key, category, token, chat_id,
                                  sent_ids, records, remaining,
                                  args.dry_run, args.require_ocr,
                                  sent_file, records_file)

    log.info("=" * 70)
    log.info("BATCH COMPLETE: %d clips uploaded in total. Exiting cleanly.", total)
    log.info("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
