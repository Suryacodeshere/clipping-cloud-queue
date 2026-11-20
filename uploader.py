#!/usr/bin/env python3
"""
uploader.py — 24/7 Cloud Uploader for Instagram Reels & YouTube Shorts (Runs on GitHub Actions)
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
YT_RESULTS_FILE = "queue/youtube_upload_results.json"


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


def get_youtube_client():
    token_str = os.getenv("YOUTUBE_TOKEN", "").strip()
    if not token_str:
        if os.path.exists("queue/youtube_token.json"):
            with open("queue/youtube_token.json", "r", encoding="utf-8") as f:
                token_str = f.read()

    if not token_str:
        print("[INFO] No YOUTUBE_TOKEN found in environment. Skipping YouTube Shorts upload.")
        return None

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token_data = json.loads(token_str)
        scopes = [
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
        ]
        creds = Credentials.from_authorized_user_info(token_data, scopes)
        if not creds.valid and creds.refresh_token:
            print("[INFO] Refreshing YouTube OAuth token...")
            creds.refresh(Request())

        yt = build("youtube", "v3", credentials=creds)
        print("[OK] YouTube API client authenticated successfully!")
        return yt
    except Exception as e:
        print(f"[WARN] Failed to initialize YouTube client: {e}")
        return None


def upload_youtube_short(youtube, video_path, clip_data):
    from googleapiclient.http import MediaFileUpload

    raw_title = (
        clip_data.get("youtube_title_final")
        or clip_data.get("title")
        or clip_data.get("title_inggris", "GTA 6 Gameplay Highlight")
    )

    # 1. Title SEO Optimization
    title = raw_title.strip()
    if "GTA 6" not in title and "GTA" not in title:
        title = f"GTA 6 - {title}"
    if "#Shorts" not in title and "#shorts" not in title:
        title = f"{title[:88].rstrip()} #Shorts"

    # 2. Description SEO Structure
    raw_desc = (
        clip_data.get("youtube_description_final", "")
        or clip_data.get("tiktok_caption_final", "")
        or raw_title
    )
    clean_desc_lines = [l for l in raw_desc.splitlines() if not l.strip().startswith("#")]
    hook_desc = "\n".join(clean_desc_lines).strip()

    seo_description = f"""{hook_desc}

🎮 Game: Grand Theft Auto VI (GTA 6)
🔥 Streamer Gameplay Highlights & Viral Moments
🔔 Subscribe to @bubucreates-p3c for daily GTA 6 Shorts, epic fails & gameplay moments!

━━━━━━━━━━━━━━━━━━━━━━━━━━
#Shorts #GTA6 #GTAVI #Gaming #TechnoGamerz #CarryMinati #Mythpat #GamingShorts #ShortsFeed #ViceCity""".strip()

    # 3. Rich SEO Tags Stack
    base_tags = [
        "GTA 6", "Grand Theft Auto VI", "GTA 6 Gameplay", "GTA 6 Shorts",
        "GTA 6 Clips", "Gaming", "Gaming Shorts", "Shorts", "Vice City",
        "Techno Gamerz", "Techno Gamerz GTA 6", "CarryMinati", "Carry GTA 6",
        "Mythpat", "Mythpat GTA 6", "GTA 6 Supercar", "GTA 6 Police Chase",
        "Viral Shorts", "Trending Gaming"
    ]
    clip_tags = clip_data.get("youtube_tags_final") or clip_data.get("tags") or []
    for ct in clip_tags:
        ct_clean = str(ct).replace("#", "").strip()
        if ct_clean and ct_clean.lower() not in [t.lower() for t in base_tags]:
            base_tags.append(ct_clean)

    body = {
        "snippet": {
            "title": title[:100],
            "description": seo_description,
            "tags": base_tags[:25],
            "categoryId": "20",  # Gaming
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    print(f"\n[YOUTUBE UPLOAD] Publishing SEO-Optimized Short: {title}")
    print(f"   Tags ({len(base_tags[:25])} tags): {', '.join(base_tags[:8])}...")
    media = MediaFileUpload(video_path, chunksize=8 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
        notifySubscribers=True,
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"   YouTube upload progress: {int(status.progress() * 100)}%")

    video_id = response.get("id")
    yt_url = f"https://youtube.com/shorts/{video_id}"
    print(f"[SUCCESS] YouTube Short Live: {yt_url}")
    return {"video_id": video_id, "url": yt_url, "title": title}


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

    print(f"\n[CLOUD UPLOAD] Publishing next Clip: {title}")
    print(f"   Video: {target_video_path}")
    if target_thumb_path:
        print(f"   Thumbnail: {target_thumb_path}")

    # 1. Upload to YouTube Shorts (if configured)
    yt_client = get_youtube_client()
    yt_data = None
    if yt_client:
        try:
            yt_data = upload_youtube_short(yt_client, target_video_path, target_clip)
            # Save to youtube results file
            yt_results = []
            if os.path.exists(YT_RESULTS_FILE):
                try:
                    with open(YT_RESULTS_FILE, "r", encoding="utf-8") as yf:
                        yt_results = json.load(yf)
                except Exception:
                    yt_results = []
            yt_results.append({
                "clip_index": target_index,
                "video": target_video_path,
                "title": yt_data["title"],
                "video_id": yt_data["video_id"],
                "url": yt_data["url"],
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            with open(YT_RESULTS_FILE, "w", encoding="utf-8") as yf:
                json.dump(yt_results, yf, indent=2)
        except Exception as e:
            print(f"[WARN] YouTube upload failed: {e}")

    # 2. Upload to Instagram Reels
    print(f"\n[INSTAGRAM UPLOAD] Publishing Reel to @chicanayaa: {title}")
    cl = get_instagram_client()

    upload_kwargs = {"path": target_video_path, "caption": caption}
    if target_thumb_path:
        upload_kwargs["thumbnail"] = target_thumb_path

    try:
        media = cl.clip_upload(**upload_kwargs)
        media_id = getattr(media, "id", None) or getattr(media, "pk", "")
        media_code = getattr(media, "code", "")
        post_url = f"https://www.instagram.com/reel/{media_code}/" if media_code else ""

        print(f"\n[SUCCESS] Instagram Reel Published! {post_url}")

        result_entry = {
            "clip_index": target_index,
            "video": target_video_path,
            "title": title,
            "status": "published",
            "media_id": str(media_id),
            "url": post_url,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        if yt_data:
            result_entry["youtube_url"] = yt_data["url"]

        results.append(result_entry)

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
        print(f"[ERR] Instagram upload failed: {e}")
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
