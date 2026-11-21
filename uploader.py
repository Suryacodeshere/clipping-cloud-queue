#!/usr/bin/env python3
"""
uploader.py — 24/7 Cloud Uploader for Instagram Reels & YouTube Shorts (Runs on GitHub Actions)
"""

import os
import sys
import json
import re
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


def normalize_title(t):
    if not t:
        return ""
    t = re.sub(r"#shorts", "", t, flags=re.IGNORECASE).strip().lower()
    t = re.sub(r"^(gta 6 - |gta6 - |gta vi - )", "", t).strip()
    return re.sub(r"\.\.\.$", "", t).strip()


def build_viral_instagram_caption(item):
    vp = item.get("video_path", "")
    raw_hook = item.get("title_inggris") or item.get("title") or item.get("youtube_title_final") or "Viral Highlight"
    hook_title = re.sub(r"#shorts", "", raw_hook, flags=re.IGNORECASE).strip()
    hook_title = re.sub(r"^(GTA 6 - |GTA6 - |GTA VI - )", "", hook_title).strip()
    hook_title = re.sub(r"\.\.\.$", "", hook_title).strip()

    raw_desc = (item.get("tiktok_caption_final") or item.get("youtube_description_final") or "").strip()
    raw_lines = [l.strip() for l in raw_desc.splitlines() if l.strip() and not l.strip().startswith("#") and not l.strip().startswith("Subscribe") and not "---" in l]

    story_parts = []
    for l in raw_lines:
        c_line = re.sub(r"#[a-zA-Z_]\w*", "", l).strip()
        c_line = re.sub(r"(drop a .*|vote below.*|drop below.*|let us know.*|tell us.*|comment below.*|subscribe.*|follow for more.*|who else relates.*|did anyone else.*|w or l.*)", "", c_line, flags=re.IGNORECASE).strip()
        c_line = re.sub(r"[👇👉👈👆]+", "", c_line).strip()
        sentences = re.split(r"(?<=[.!?])\s+", c_line)
        clean_s = []
        for s in sentences:
            s_clean = s.strip()
            if re.search(r"^(would|do|have|what|who|did|can|is|are|could|why)\b", s_clean, re.IGNORECASE) or s_clean.endswith("?"):
                continue
            if s_clean:
                clean_s.append(s_clean)
        res = " ".join(clean_s)
        res = re.sub(r"[\?:\,\s!]+$", "", res).strip()
        if res and len(res) > 10 and res.lower() not in hook_title.lower() and hook_title.lower() not in res.lower():
            story_parts.append(res)

    if "carry" in vp:
        dm_cta = "✈️ Send this to your duo who's been waiting for GTA 6 since school 💀"
        question = "Drop a 🔥 if Carry's reaction was pure gold, or 💀 if you'd react differently 👇"
        hashtags = ["#CarryMinati", "#CarryIsLive", "#GTA6", "#IndianGaming", "#GTA6Gameplay"]
        fallback_context = "Carry's first live reaction to GTA 6 had the entire chat in absolute tears. After 12 years of waiting, the emotion is 100% genuine!"
    elif "techno2" in vp:
        dm_cta = "✈️ Share this with that friend who explores every Easter egg in GTA 🎮"
        question = "W or L mission escape? Drop your vote below 👇"
        hashtags = ["#TechnoGamerz", "#GTA6", "#GTA6Gameplay", "#IndianGaming", "#GamingReels"]
        fallback_context = "Techno Gamerz pulling off high-risk maneuvers in GTA 6 Vice City. The next-gen police AI is legitimately ruthless!"
    elif "mythpat" in vp:
        dm_cta = "✈️ Send this to your gaming partner who panics during every single heist 😭💀"
        question = "Rate Mythpat's survival skills 1 to 10 in the comments 👇"
        hashtags = ["#Mythpat", "#GTA6", "#IndianGaming", "#GamingHumor", "#GTA6Gameplay"]
        fallback_context = "Mythpat entering full panic mode the second the mission starts. How he even made it out alive is a mystery 💀"
    elif "liveinsaan" in vp:
        dm_cta = "✈️ Share this with the friend who always starts unnecessary chaos in GTA 💀"
        question = "Could you survive this gang ambush? Drop a 🔥 if YES or 💀 if BUSTED 👇"
        hashtags = ["#LiveInsaan", "#TriggeredInsaan", "#GTA6", "#IndianGaming", "#GamingMoments"]
        fallback_context = "Triggered Insaan's chaotic gameplay went from 0 to 100 in two seconds flat. The entire Vice City police department showed up!"
    elif "dattrax" in vp:
        dm_cta = "✈️ Send this to your duo who crashes the getaway vehicle every single time 😭"
        question = "10/10 escape or pure luck? Tell us in the comments 👇"
        hashtags = ["#DattraxGaming", "#GTA6", "#GTA6Gameplay", "#IndianGamers", "#ViceCity"]
        fallback_context = "High-stakes police chase and shootout in GTA 6 Vice City. Every turn is pure adrenaline and split-second survival!"
    elif "mrbeast" in vp:
        dm_cta = "✈️ Send this to a friend who would do this challenge for a million dollars 💰"
        question = "Would your mom be proud or mad? Tell us in the comments 👇"
        hashtags = ["#MrBeast", "#MrBeastGaming", "#GTA6", "#GamingChallenge", "#GamingReels"]
        fallback_context = "MrBeast turns GTA 6 into an absurd high-stakes challenge. High risk, hilarious chaos, and an unexpected ending!"
    elif any(k in vp for k in ["highlight", "stream2", "v2_highlight"]):
        dm_cta = "✈️ Send this to your duo who always triggers a 3-star wanted level in 2 minutes 💀"
        question = "W or L move? Drop your thoughts below 👇"
        hashtags = ["#GTA6", "#GTA6Gameplay", "#IndianGaming", "#GamingMoments", "#ViceCity"]
        fallback_context = "Just when you think you've seen everything in GTA 6, this happens. The next-gen physics and NPC reactions are wild!"
    elif "munawar" in vp:
        dm_cta = "✈️ Share this with that friend who overthinks every single situation 😭💀"
        question = "Could you hold your laugh here? Rate this bit 1 to 10 👇"
        hashtags = ["#MunawarFaruqui", "#Dhandho", "#IndianStandup", "#ComedyReels", "#DesiHumor"]
        fallback_context = "Munawar Faruqui breaking down relatable life struggles in 'Dhandho'. His observational timing on this punchline is unmatched!"
    elif "samay" in vp:
        dm_cta = "✈️ Send this to that one friend whose humor is completely unhinged 💀"
        question = "W or L roast? Drop your reaction in the comments 👇"
        hashtags = ["#SamayRaina", "#StillAlive", "#IndianStandup", "#RoastComedy", "#DesiHumor"]
        fallback_context = "Samay Raina turning raw life experiences and unfiltered crowd interactions into pure comedic gold in 'STILL ALIVE'!"
    elif "pranit" in vp:
        dm_cta = "✈️ Share this with that friend who would get roasted in the front row 💀"
        question = "Who took the bigger L here? Tell us below 👇"
        hashtags = ["#PranitMore", "#CrowdWork", "#IndianComedy", "#StandupComedy", "#DesiHumor"]
        fallback_context = "When crowd work goes completely off-script! Pranit More's quick-witted reaction to this audience confession is hysterical."
    elif "satish" in vp:
        dm_cta = "✈️ Send this to that friend who's surviving on pure middle-class struggle 😂💀"
        question = "How relatable is this on a scale of 1 to 10? Drop below 👇"
        hashtags = ["#SatishRay", "#BerozgaarYuva", "#IndianStandup", "#DesiHumor", "#EngineeringLife"]
        fallback_context = "Satish Ray capturing the hilarious reality of career struggles and everyday desi relatable dilemmas in 'Berozgaar Yuva'!"
    elif "vikas" in vp:
        dm_cta = "✈️ Share this with someone who needs this mindset perspective today ✨"
        question = "Agree or Disagree with Dr. Vikas? Drop a 🧠 for YES or ❌ for NO 👇"
        hashtags = ["#VikasDivyakirti", "#RajShamani", "#FiguringOut", "#LifeLessons", "#MindsetShift"]
        fallback_context = "Dr. Vikas Divyakirti breaks down profound psychological truths about human ambition, emotional resilience, and deep thinking."
    elif "sweta" in vp:
        dm_cta = "✈️ Send this to a friend who is fighting overthinking and procrastination 💡"
        question = "Will you try this brain technique today? Drop a 🧠 below 👇"
        hashtags = ["#BillionaireBrain", "#BrainHacks", "#RajShamani", "#ProductivityHacks", "#MindsetShift"]
        fallback_context = "Neuroscience-backed cognitive reframing to overcome chronic procrastination, reduce anxiety, and unlock peak mental focus."
    elif "khan_sir" in vp:
        dm_cta = "✈️ Share this with someone who follows world affairs and history 🇮🇳"
        question = "100% facts or debatable? Drop your thoughts below 👇"
        hashtags = ["#KhanSir", "#Geopolitics", "#WorldWar", "#IndianDefense", "#LifeLessons"]
        fallback_context = "Khan Sir simplifying complex global geopolitics, defense economies, and historical realities with unfiltered desi clarity."
    elif "mallya" in vp:
        dm_cta = "✈️ Send this to a business enthusiast or friend who follows corporate stories 💸"
        question = "Do you believe his side of the story? Drop your opinion below 👇"
        hashtags = ["#VijayMallya", "#Kingfisher", "#RajShamani", "#BusinessMindset", "#IndianBusiness"]
        fallback_context = "Vijay Mallya addresses allegations, the Kingfisher Airlines collapse, and the untold reality behind high-stakes corporate finance."
    else:
        dm_cta = "✈️ Share this with your friends who need to see this right now 💀"
        question = "What are your thoughts on this? Drop a comment below 👇"
        hashtags = ["#ViralMoments", "#TrendingReels", "#DesiReels", "#IndianContent", "#MustWatch"]
        fallback_context = "An unbelievable moment captured live that you have to see to believe!"

    if story_parts:
        story = ". ".join(story_parts[:2]).replace("..", ".")
        if len(story) < 35:
            story = f"{story}. {fallback_context}"
    else:
        story = fallback_context

    story = re.sub(r"([!?.])\s*[!?.]+", r"\1", story).strip()
    if not any(story.endswith(c) for c in [".", "!", "?", "💀", "😭", "😂", "🔥", "✨", "🧠", "❤️"]):
        story += "!"

    caption = f"""{hook_title}

{story}

{dm_cta}

{question}

📌 Save this reel so you don't forget this!

{" ".join(hashtags)}""".strip()

    return caption


def upload_youtube_short(youtube, video_path, clip_data):
    from googleapiclient.http import MediaFileUpload
    import re

    raw_title = (
        clip_data.get("youtube_title_final")
        or clip_data.get("title")
        or clip_data.get("title_inggris", "Viral Highlight")
    )
    content_type = str(clip_data.get("content_type", "gameplay")).lower()

    # 1. Mobile-Optimized Hook Title (< 55 chars)
    clean_title = raw_title.replace("GTA 6 - ", "").replace("GTA6 - ", "").strip()
    clean_title = re.sub(r"#shorts", "", clean_title, flags=re.IGNORECASE).strip()
    if len(clean_title) > 48:
        clean_title = clean_title[:45].rstrip() + "..."
    title = f"{clean_title} #shorts"

    # 2. Content-Aware Category, Audio Language & Targeted Tags
    raw_desc = (
        clip_data.get("youtube_description_final", "")
        or clip_data.get("tiktok_caption_final", "")
        or clean_title
    )
    clean_desc_lines = [l for l in raw_desc.splitlines() if not l.strip().startswith("#")]
    hook_desc = "\n".join(clean_desc_lines[:3]).strip()

    if "comedy" in content_type:
        category_id = "23"  # Comedy
        niche_tags = ["Standup Comedy", "Indian Comedy", "Comedy Shorts", "Viral Comedy", "Hindi Standup"]
        desc_hashtags = "#Shorts #Comedy #StandupComedy"
        channel_hook = "Subscribe to @bubucreates-p3c for daily viral standup & comedy moments!"
    elif "podcast" in content_type:
        category_id = "24"  # Entertainment
        niche_tags = ["Podcast", "Indian Podcast", "Podcast Highlights", "Motivation", "Hindi Podcast"]
        desc_hashtags = "#Shorts #Podcast #Inspiration"
        channel_hook = "Subscribe to @bubucreates-p3c for daily life insights & podcast highlights!"
    else:
        category_id = "20"  # Gaming
        niche_tags = ["Gaming", "Gaming Shorts", "GTA 6", "GTA 6 Gameplay", "Vice City"]
        desc_hashtags = "#Shorts #Gaming #GTA6"
        channel_hook = "Subscribe to @bubucreates-p3c for daily GTA 6 gameplay & viral moments!"

    clip_tags = clip_data.get("youtube_tags_final") or clip_data.get("tags") or []
    final_tags = list(niche_tags)
    for ct in clip_tags:
        ct_clean = str(ct).replace("#", "").strip()
        if ct_clean and ct_clean.lower() not in [t.lower() for t in final_tags]:
            final_tags.append(ct_clean)
        if len(final_tags) >= 10:
            break

    seo_description = f"""{hook_desc}

🔔 {channel_hook}

━━━━━━━━━━━━━━━━━━━━━━━━━━
{desc_hashtags}""".strip()

    body = {
        "snippet": {
            "title": title,
            "description": seo_description,
            "tags": final_tags,
            "categoryId": category_id,
            "defaultLanguage": "hi",
            "defaultAudioLanguage": "hi",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    print(f"\n[YOUTUBE UPLOAD] Publishing High-Retention Short: {title}")
    print(f"   Category: {category_id} | AudioLang: hi | Tags ({len(final_tags)}): {', '.join(final_tags[:5])}...")
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
    published_videos = set()
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE, "r", encoding="utf-8") as rf:
                results = json.load(rf)
                published_titles = {
                    normalize_title(r.get("title", ""))
                    for r in results if r.get("status") == "published" and r.get("title")
                }
                published_videos = {
                    safe_basename(r.get("video", "")).lower()
                    for r in results if r.get("status") == "published" and r.get("video")
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
        raw_vpath = clip.get("output_file") or clip.get("video_path")
        vname = safe_basename(raw_vpath)
        vpath = os.path.join("queue", vname) if vname else None

        # Check if already published by filename or normalized title
        if (vname and vname.lower() in published_videos) or (title and normalize_title(title) in published_titles):
            continue

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
            if normalize_title(c.get("title") or c.get("youtube_title_final") or c.get("title_inggris", "")) not in published_titles
            and safe_basename(c.get("output_file") or c.get("video_path")).lower() not in published_videos
        ]
        if unposted:
            raise FileNotFoundError(f"Found {len(unposted)} pending clips in manifest, but their video files were not found in queue/ directory!")
        print("[OK] All clips in queue have already been published! Nothing left to upload.")
        return

    title = target_clip.get("title") or target_clip.get("youtube_title_final") or target_clip.get("title_inggris", "")
    caption = target_clip.get("instagram_caption_final") or target_clip.get("tiktok_caption_final") or target_clip.get("tiktok_caption")
    if not caption or "✈️" not in caption:
        caption = build_viral_instagram_caption(target_clip)

    print(f"\n[CLOUD UPLOAD] Publishing next Clip: {title}")
    print(f"   Video: {target_video_path}")
    if target_thumb_path:
        print(f"   Thumbnail: {target_thumb_path}")

    # 1. Upload to YouTube Shorts (if configured & pacing allows: 2-3 shorts/day on fresh channel)
    yt_client = get_youtube_client()
    yt_data = None
    should_upload_yt = True
    if yt_client:
        yt_interval_hours = 6.0  # Safe 6h pacing prevents bot-spam throttle on new channel
        if os.path.exists(YT_RESULTS_FILE):
            try:
                with open(YT_RESULTS_FILE, "r", encoding="utf-8") as yf:
                    yt_history = json.load(yf)
                if yt_history:
                    last_yt_ts = parse_iso_timestamp(yt_history[-1].get("timestamp", ""))
                    if last_yt_ts:
                        now_utc = datetime.now(timezone.utc)
                        elapsed_yt = (now_utc.timestamp() - last_yt_ts.timestamp()) / 3600.0
                        if elapsed_yt < yt_interval_hours:
                            print(f"[YOUTUBE PACING] Last Short posted {elapsed_yt:.1f}h ago. YouTube pacing requires {yt_interval_hours}h for algorithm seeding. Skipping YouTube this cycle.")
                            should_upload_yt = False
            except Exception as e:
                print(f"[WARN] Failed checking YouTube pacing: {e}")

    if yt_client and should_upload_yt:
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
