#!/usr/bin/env python3
"""
uploader.py — 24/7 Cloud Uploader for Instagram Reels (Runs on GitHub Actions)
"""

import os
import sys
import json
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

SESSION_FILE = "queue/ig_session.json"
DEFAULT_MANIFEST = "queue/render_manifest.json"
RESULTS_FILE = "queue/instagram_upload_results.json"


def get_instagram_client():
    from instagrapi import Client

    username = os.getenv("IG_USERNAME", "").strip()
    password = os.getenv("IG_PASSWORD", "").strip()
    sessionid = os.getenv("IG_SESSIONID", "").strip()

    cl = Client()
    cl.delay_range = [2, 5]

    # 1. Try loading cached session file
    if os.path.exists(SESSION_FILE):
        try:
            cl.load_settings(SESSION_FILE)
            cl.get_timeline_feed()
            raw_user = getattr(cl, "username", None)
            actual_username = (raw_user if raw_user else username).strip()
            print(f"[OK] Reused cached session for @{actual_username}")
            if actual_username.lower() != "chicanayaa":
                raise ValueError(f"Account check failed: got @{actual_username}, expected @chicanayaa")
            return cl
        except Exception as e:
            print(f"[WARN] Cached session invalid ({e}), falling back to session ID...")

    # 2. Try sessionid
    if sessionid:
        sessionid = urllib.parse.unquote(sessionid)
        try:
            print("[INFO] Logging in with IG_SESSIONID...")
            cl.login_by_sessionid(sessionid)
            cl.dump_settings(SESSION_FILE)
            actual_username = getattr(cl, "username", "").strip()
            print(f"[OK] Logged in via sessionid as @{actual_username}")
            if actual_username.lower() != "chicanayaa":
                raise ValueError(f"Account check failed: got @{actual_username}, expected @chicanayaa")
            return cl
        except Exception as e:
            if "Account check failed" in str(e):
                raise
            print(f"[WARN] Session ID login failed ({e}), trying password...")

    # 3. Password fallback
    if not username or not password:
        raise ValueError("Missing credentials: need IG_SESSIONID or IG_USERNAME + IG_PASSWORD.")

    print(f"[INFO] Logging in with password as @{username}...")
    cl.login(username, password)
    cl.dump_settings(SESSION_FILE)
    actual_username = getattr(cl, "username", "").strip()
    if actual_username.lower() != "chicanayaa":
        raise ValueError(f"Account check failed: got @{actual_username}, expected @chicanayaa")

    return cl


def safe_basename(path_str):
    if not path_str:
        return None
    return path_str.replace("\\", "/").rstrip("/").split("/")[-1]


def parse_iso_timestamp(ts_str):
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
        return dt
    except Exception:
        return None


def run_cloud_upload(manifest_path=DEFAULT_MANIFEST, test_mode=True, min_interval_hours=2.5, force=False):
    if not os.path.exists(manifest_path):
        print(f"[ERR] Manifest not found: {manifest_path}")
        return

    with open(manifest_path, "r", encoding="utf-8") as f:
        clips = json.load(f)

    if not clips:
        print("[INFO] Manifest is empty.")
        return

    results = []
    published_titles = set()
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE, "r", encoding="utf-8") as rf:
                results = json.load(rf)
                published_titles = {
                    r.get("title", "").strip().lower()
                    for r in results if r.get("status") == "published" and r.get("title")
                }
        except Exception:
            results = []

    # Check minimum interval between uploads
    if not force and results:
        published_records = [r for r in results if r.get("status") == "published" and r.get("timestamp")]
        if published_records:
            last_pub = published_records[-1]
            last_ts = parse_iso_timestamp(last_pub.get("timestamp", ""))
            if last_ts:
                now_utc = datetime.now(timezone.utc)
                elapsed_hours = (now_utc.timestamp() - last_ts.timestamp()) / 3600.0
                if elapsed_hours < min_interval_hours:
                    remaining_hours = min_interval_hours - elapsed_hours
                    print(f"[PACING HOLD] Last Reel ('{last_pub.get('title')}') was uploaded {elapsed_hours:.2f}h ago.")
                    print(f"   Policy requires at least {min_interval_hours:.1f}h spacing between uploads.")
                    print(f"   Next upload eligible in ~{remaining_hours:.1f} hour(s). Exiting cleanly until next cron window.")
                    return

    # Find the next unposted clip
    target_clip = None
    target_index = -1
    target_video_path = None
    target_thumb_path = None

    for i, clip in enumerate(clips):
        title = clip.get("title") or clip.get("youtube_title_final") or clip.get("title_inggris", "")
        if title and title.strip().lower() in published_titles:
            continue

        # Resolve path within queue directory (using safe_basename for cross-platform Linux/Windows support)
        raw_vpath = clip.get("output_file") or clip.get("video_path")
        vname = safe_basename(raw_vpath)
        vpath = os.path.join("queue", vname) if vname else None

        if not vpath or not os.path.exists(vpath):
            print(f"[WARN] Clip {i + 1} file not found on disk: {vpath}")
            continue

        raw_tpath = clip.get("thumbnail_path")
        tname = safe_basename(raw_tpath)
        tpath = os.path.join("queue", tname) if tname and os.path.exists(os.path.join("queue", tname)) else None

        target_clip = clip
        target_index = i
        target_video_path = vpath
        target_thumb_path = tpath
        break

    if not target_clip:
        unposted = [
            c for c in clips
            if (c.get("title") or c.get("youtube_title_final") or c.get("title_inggris", "")).strip().lower() not in published_titles
        ]
        if unposted:
            raise FileNotFoundError(f"Found {len(unposted)} pending clips in manifest, but their video files were not found in queue/ directory!")
        print("[OK] All clips in queue have already been published! Nothing left to upload.")
        return

    title = target_clip.get("title") or target_clip.get("youtube_title_final") or target_clip.get("title_inggris", "")
    caption = target_clip.get("tiktok_caption") or target_clip.get("tiktok_caption_final", "") or title
    tags = target_clip.get("tags") or target_clip.get("youtube_tags_final", [])
    if tags and "#" not in caption:
        tag_str = " ".join(f"#{t.replace(' ', '')}" for t in tags[:5])
        caption = f"{caption}\n\n{tag_str}"

    print(f"\n[CLOUD UPLOAD] Publishing next Reel to @chicanayaa: {title}")
    print(f"   Video: {target_video_path}")
    if target_thumb_path:
        print(f"   Thumbnail: {target_thumb_path}")

    cl = get_instagram_client()

    upload_kwargs = {"path": target_video_path, "caption": caption}
    if target_thumb_path:
        upload_kwargs["thumbnail"] = target_thumb_path

    try:
        media = cl.clip_upload(**upload_kwargs)
        media_id = getattr(media, "id", None) or getattr(media, "pk", "")
        media_code = getattr(media, "code", "")
        post_url = f"https://www.instagram.com/reel/{media_code}/" if media_code else ""

        print(f"\n[SUCCESS] Reel Published! {post_url}")

        results.append({
            "clip_index": target_index,
            "video": target_video_path,
            "title": title,
            "status": "published",
            "media_id": str(media_id),
            "url": post_url,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)

        # Delete local copy to keep repo clean
        if os.path.exists(target_video_path):
            os.remove(target_video_path)
            print(f"   Deleted published video from queue: {target_video_path}")
        if target_thumb_path and os.path.exists(target_thumb_path):
            os.remove(target_thumb_path)
            print(f"   Deleted thumbnail: {target_thumb_path}")

    except Exception as e:
        print(f"[ERR] Upload failed: {e}")
        results.append({
            "clip_index": target_index,
            "video": target_video_path,
            "title": title,
            "status": "failed",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--test-mode", action="store_true", default=True)
    parser.add_argument("--min-interval", type=float, default=2.5, help="Minimum interval in hours between uploads")
    parser.add_argument("--force", action="store_true", help="Force upload now, ignoring interval")
    args = parser.parse_args()

    run_cloud_upload(
        manifest_path=args.manifest,
        test_mode=args.test_mode,
        min_interval_hours=args.min_interval,
        force=args.force
    )
