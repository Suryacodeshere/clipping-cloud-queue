#!/usr/bin/env python3
"""
uploader.py — Fast, Decoupled 24/7 Cloud Uploader for Instagram Reels & YouTube Shorts

Efficiency & Reliability Architecture:
- `--plan-only` runs on bare Python 3 stdlib (<0.2s, no pip packages, no video blobs) to decide
  whether YouTube or Instagram is eligible right now (pacing, cooldown, IST peak window) and
  outputs only the exact 1-2 needed `/queue/...` paths for blobless sparse-checkout.
- Skipped runs exit in ~5s instead of downloading ~4 GB of media and installing dependencies.
- YouTube Shorts and Instagram Reels are 100% decoupled with separate tracking + live YT check.
- IST peak-window pacing skips the dead 01:00–07:30 AM IST sleep window (override with `--force`).
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # --plan-only on bare CI Python runs before pip install without python-dotenv
    pass

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SESSION_FILE = "queue/ig_session.json"
DEFAULT_MANIFEST = "queue/render_manifest.json"
RESULTS_FILE = "queue/instagram_upload_results.json"
YT_RESULTS_FILE = "queue/youtube_upload_results.json"
IG_AUTH_COOLDOWN_HOURS = 6.0
IST_TZ = timezone(timedelta(hours=5, minutes=30))
QUIET_START_HOUR_IST = 1.0   # 01:00 AM IST
QUIET_END_HOUR_IST = 7.5     # 07:30 AM IST
YT_PEAK_START_HOUR_IST = 17.5  # 05:30 PM IST (India prime evening Shorts window)
YT_PEAK_END_HOUR_IST = 22.5    # 10:30 PM IST
PLAYLIST_BY_PILLAR = {
    "gameplay": "PLZ-j05R9sLtw",
    "comedy": "PLRdva2Nr33t8",
    "podcast": "PLMYF9fyZx174",
}


def safe_basename(path_str):
    if not path_str:
        return None
    return path_str.replace("\\", "/").rstrip("/").split("/")[-1]


def parse_iso_timestamp(ts_str):
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST_TZ)
        return dt
    except Exception:
        return None


def normalize_title(t):
    if not t:
        return ""
    t = re.sub(r"#shorts\b", "", str(t), flags=re.IGNORECASE).strip().lower()
    t = re.sub(r"^(gta 6 - |gta6 - |gta vi - )", "", t).strip()
    return re.sub(r"\.\.\.$", "", t).strip()


def clip_title_candidates(clip):
    raw = [
        clip.get("title"),
        clip.get("youtube_title_final"),
        clip.get("title_inggris"),
    ]
    return {normalize_title(t) for t in raw if t and normalize_title(t)}


def is_clip_published(clip, published_videos, published_titles):
    raw_vpath = clip.get("output_file") or clip.get("video_path")
    vname = safe_basename(raw_vpath)
    if vname and vname.lower() in published_videos:
        return True
    if clip_title_candidates(clip) & published_titles:
        return True
    return False


def get_available_queue_files():
    """Return set of filenames currently in `queue/` (supports blobless git sparse trees)."""
    files = set()
    try:
        res = subprocess.run(
            ["git", "ls-tree", "--name-only", "HEAD", "queue/"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                name = safe_basename(line.strip())
                if name:
                    files.add(name)
    except Exception:
        pass
    if not files and os.path.isdir("queue"):
        files = set(os.listdir("queue"))
    return files


def ensure_file_materialized(rel_path):
    """Fetch a single file blob via sparse-checkout if it exists in git tree but isn't checked out."""
    if not rel_path:
        return False
    if os.path.exists(rel_path):
        return True
    norm_path = rel_path.replace("\\", "/").lstrip("/")
    try:
        subprocess.run(
            ["git", "sparse-checkout", "add", f"/{norm_path}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        pass
    return os.path.exists(norm_path)


def find_next_clip(clips, published_videos, published_titles, available_files=None):
    for i, clip in enumerate(clips):
        if is_clip_published(clip, published_videos, published_titles):
            continue

        raw_vpath = clip.get("output_file") or clip.get("video_path")
        vname = safe_basename(raw_vpath)
        if not vname:
            continue
        vpath = f"queue/{vname}"

        if available_files is not None:
            if vname not in available_files:
                continue
        elif not os.path.exists(vpath):
            continue

        raw_tpath = clip.get("thumbnail_path")
        tname = safe_basename(raw_tpath)
        tpath = None
        if tname:
            if available_files is not None and tname in available_files:
                tpath = f"queue/{tname}"
            elif os.path.exists(f"queue/{tname}"):
                tpath = f"queue/{tname}"
        return i, clip, vpath, tpath
    return None


def compute_ig_auth_fingerprint():
    username = os.getenv("IG_USERNAME", "").strip()
    password = os.getenv("IG_PASSWORD", "").strip()
    sessionid = os.getenv("IG_SESSIONID", "").strip()
    settings = os.getenv("IG_SETTINGS", "").strip()
    payload = f"{username}|{password}|{sessionid}|{settings}".encode("utf-8", errors="ignore")
    return hashlib.sha256(payload).hexdigest()[:12]


def is_auth_related_error(err_text):
    markers = (
        "login_required",
        "401",
        "429",
        "pleasewaitfewminutes",
        "challengerequired",
        "clientneedsupgradeerror",
        "twofactorrequired",
        "badpassword",
        "incorrect password",
        "account check failed",
        "out of date",
        "unauthorized",
    )
    low = (err_text or "").lower()
    return any(m in low for m in markers)


def in_quiet_hours_ist(now_utc):
    now_ist = now_utc.astimezone(IST_TZ)
    hour_float = now_ist.hour + now_ist.minute / 60.0
    return QUIET_START_HOUR_IST <= hour_float < QUIET_END_HOUR_IST, now_ist


def in_yt_peak_window_ist(now_utc):
    now_ist = now_utc.astimezone(IST_TZ)
    hour_float = now_ist.hour + now_ist.minute / 60.0
    return YT_PEAK_START_HOUR_IST <= hour_float < YT_PEAK_END_HOUR_IST, now_ist


def evaluate_schedules(
    clips,
    ig_results,
    yt_results,
    min_interval_hours,
    yt_interval_hours,
    force,
    respect_quiet_hours,
    now_utc,
    auth_fp,
    respect_yt_peak=True,
):
    quiet_active, now_ist = in_quiet_hours_ist(now_utc)
    if quiet_active and respect_quiet_hours and not force:
        print(
            f"[QUIET HOURS] Current IST is {now_ist.strftime('%H:%M')} "
            f"(paused 01:00–07:30 IST to maximize awake audience reach)."
        )
        return False, False, "[QUIET HOURS]"

    # YouTube eligibility check (pure local state, zero network calls)
    has_yt_token = bool(os.getenv("YOUTUBE_TOKEN", "").strip() or os.path.exists("queue/youtube_token.json"))
    should_upload_yt = False
    yt_reason = "No YOUTUBE_TOKEN configured"
    if has_yt_token:
        yt_peak_ok, _ = in_yt_peak_window_ist(now_utc)
        if respect_yt_peak and not force and not yt_peak_ok:
            yt_reason = f"Waiting for India peak evening (17:30-22:30 IST, now {now_ist.strftime('%H:%M')})"
        else:
            should_upload_yt = True
            yt_reason = "Eligible"
            if not force and yt_results:
                last_yt_ts = parse_iso_timestamp(yt_results[-1].get("timestamp", ""))
                if last_yt_ts:
                    elapsed_yt = (now_utc.timestamp() - last_yt_ts.timestamp()) / 3600.0
                    if elapsed_yt < yt_interval_hours:
                        should_upload_yt = False
                        yt_reason = f"Pacing hold ({elapsed_yt:.1f}h / {yt_interval_hours:.1f}h)"

    # Instagram eligibility check (pure local state, zero network calls)
    should_upload_ig = True
    ig_reason = "Eligible"
    if not force and ig_results:
        pub_records = [r for r in ig_results if r.get("status") == "published" and r.get("timestamp")]
        if pub_records:
            last_pub = pub_records[-1]
            last_ts = parse_iso_timestamp(last_pub.get("timestamp", ""))
            if last_ts:
                elapsed_ig = (now_utc.timestamp() - last_ts.timestamp()) / 3600.0
                if elapsed_ig < min_interval_hours:
                    rem = min_interval_hours - elapsed_ig
                    should_upload_ig = False
                    ig_reason = f"Pacing hold ({elapsed_ig:.2f}h / {min_interval_hours:.1f}h, next in ~{rem:.1f}h)"

        if should_upload_ig:
            last_rec = ig_results[-1]
            if last_rec.get("status") == "failed" and last_rec.get("auth_error"):
                last_fp = last_rec.get("auth_fingerprint", "")
                last_fail_ts = parse_iso_timestamp(last_rec.get("timestamp", ""))
                if last_fp == auth_fp and last_fail_ts:
                    elapsed_fail = (now_utc.timestamp() - last_fail_ts.timestamp()) / 3600.0
                    if elapsed_fail < IG_AUTH_COOLDOWN_HOURS:
                        rem_cd = IG_AUTH_COOLDOWN_HOURS - elapsed_fail
                        should_upload_ig = False
                        ig_reason = f"Auth cooldown ({elapsed_fail:.2f}h / {IG_AUTH_COOLDOWN_HOURS:.1f}h, next in ~{rem_cd:.1f}h)"

    print(f"[PLAN] IST={now_ist.strftime('%Y-%m-%d %H:%M')} | YouTube: {yt_reason} | Instagram: {ig_reason}")
    return should_upload_yt, should_upload_ig, ""


def get_instagram_client():
    from instagrapi import Client

    username = os.getenv("IG_USERNAME", "").strip()
    password = os.getenv("IG_PASSWORD", "").strip()
    sessionid = os.getenv("IG_SESSIONID", "").strip()

    ig_settings_env = os.getenv("IG_SETTINGS", "").strip()
    if ig_settings_env:
        cl0 = Client()
        cl0.delay_range = [2, 5]
        try:
            settings = json.loads(ig_settings_env)
            cl0.set_settings(settings)
            print("[INFO] Loaded persistent device settings from IG_SETTINGS secret.")
            cl0.get_timeline_feed()
            raw_user = getattr(cl0, "username", None)
            actual_username = (raw_user if raw_user else username).strip()
            print(f"[OK] Reused authenticated session for @{actual_username}")
            if actual_username.lower() != "chicanayaa":
                raise ValueError(f"Account check failed: got @{actual_username}, expected @chicanayaa")
            return cl0
        except Exception as e:
            if "Account check failed" in str(e):
                raise
            print(f"[WARN] IG_SETTINGS session check failed ({e}), trying fallback...")

    if os.path.exists(SESSION_FILE):
        cl1 = Client()
        cl1.delay_range = [2, 5]
        try:
            cl1.load_settings(SESSION_FILE)
            cl1.get_timeline_feed()
            raw_user = getattr(cl1, "username", None)
            actual_username = (raw_user if raw_user else username).strip()
            print(f"[OK] Reused cached session for @{actual_username}")
            if actual_username.lower() != "chicanayaa":
                raise ValueError(f"Account check failed: got @{actual_username}, expected @chicanayaa")
            return cl1
        except Exception as e:
            if "Account check failed" in str(e):
                raise
            print(f"[WARN] Cached session invalid ({e}), falling back to session ID...")

    if sessionid:
        sessionid = urllib.parse.unquote(sessionid)
        cl2 = Client()
        cl2.delay_range = [2, 5]
        try:
            print("[INFO] Logging in with IG_SESSIONID...")
            cl2.login_by_sessionid(sessionid)
            cl2.dump_settings(SESSION_FILE)
            actual_username = getattr(cl2, "username", "").strip()
            print(f"[OK] Logged in via sessionid as @{actual_username}")
            if actual_username.lower() != "chicanayaa":
                raise ValueError(f"Account check failed: got @{actual_username}, expected @chicanayaa")
            return cl2
        except Exception as e:
            if "Account check failed" in str(e):
                raise
            print(f"[WARN] Session ID login failed ({e}), trying password...")

    if not username or not password:
        raise ValueError("Missing credentials: need valid IG_SESSIONID / IG_SETTINGS or IG_USERNAME + IG_PASSWORD.")

    cl3 = Client()
    cl3.delay_range = [2, 5]
    print(f"[INFO] Logging in with password as @{username}...")
    cl3.login(username, password)
    cl3.dump_settings(SESSION_FILE)
    actual_username = getattr(cl3, "username", "").strip()
    if actual_username.lower() != "chicanayaa":
        raise ValueError(f"Account check failed: got @{actual_username}, expected @chicanayaa")
    return cl3


def get_youtube_client():
    token_str = os.getenv("YOUTUBE_TOKEN", "").strip()
    if not token_str and os.path.exists("queue/youtube_token.json"):
        with open("queue/youtube_token.json", "r", encoding="utf-8") as f:
            token_str = f.read()

    if not token_str:
        return None

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        token_data = json.loads(token_str)
        scopes = token_data.get("scopes") or [
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
            "https://www.googleapis.com/auth/youtube.force-ssl",
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


def fetch_existing_youtube_titles(youtube):
    """Fetch normalized titles currently live on the channel to prevent duplicate uploads."""
    live_titles = set()
    try:
        ch = youtube.channels().list(part="contentDetails", mine=True).execute()
        items = ch.get("items", [])
        if not items:
            return live_titles
        uploads_pl = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        req = youtube.playlistItems().list(part="snippet", playlistId=uploads_pl, maxResults=50)
        fetched = 0
        while req and fetched < 200:
            res = req.execute()
            batch = res.get("items", [])
            for it in batch:
                title = (it.get("snippet") or {}).get("title", "")
                norm = normalize_title(title)
                if norm:
                    live_titles.add(norm)
            fetched += len(batch)
            req = youtube.playlistItems().list_next(req, res)
        print(f"[INFO] Verified {len(live_titles)} existing videos on live YouTube channel.")
    except Exception as e:
        print(f"[WARN] Could not query live YouTube channel playlist (non-fatal): {e}")
    return live_titles


def build_viral_instagram_caption(item):
    vp = item.get("video_path", "")
    raw_hook = item.get("title_inggris") or item.get("title") or item.get("youtube_title_final") or "Viral Highlight"
    hook_title = re.sub(r"#shorts\b", "", raw_hook, flags=re.IGNORECASE).strip()
    hook_title = re.sub(r"^(GTA 6 - |GTA6 - |GTA VI - )", "", hook_title).strip()
    hook_title = re.sub(r"\.\.\.$", "", hook_title).strip()

    raw_desc = (item.get("tiktok_caption_final") or item.get("youtube_description_final") or "").strip()
    raw_lines = [
        l.strip()
        for l in raw_desc.splitlines()
        if l.strip() and not l.strip().startswith("#") and not l.strip().startswith("Subscribe") and "---" not in l
    ]

    story_parts = []
    for idx, l in enumerate(raw_lines):
        if idx == 0 and (l.strip().lower() in hook_title.lower() or hook_title.lower() in l.strip().lower()):
            continue
        c_line = re.sub(r"#[a-zA-Z_]\w*", "", l).strip()
        c_line = re.sub(
            r"(tag that friend.*|drop a .*|vote below.*|drop below.*|let us know.*|tell us.*|comment below.*|subscribe.*|follow for more.*|who else relates.*|did anyone else.*|w or l.*)",
            "",
            c_line,
            flags=re.IGNORECASE,
        ).strip()
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

    return f"""{hook_title}

{story}

{dm_cta}

{question}

📌 Save this reel so you don't forget this!

{" ".join(hashtags)}""".strip()


def upload_youtube_short(youtube, video_path, clip_data):
    from googleapiclient.http import MediaFileUpload

    raw_title = (
        clip_data.get("youtube_title_final")
        or clip_data.get("title")
        or clip_data.get("title_inggris", "Viral Highlight")
    )
    content_type = str(clip_data.get("content_type", "gameplay")).lower()

    clean_title = raw_title.replace("GTA 6 - ", "").replace("GTA6 - ", "").strip()
    clean_title = re.sub(r"#shorts\b", "", clean_title, flags=re.IGNORECASE).strip()
    if len(clean_title) > 70:
        cut = clean_title[:68]
        clean_title = (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" ,:-")
    title = clean_title

    if "comedy" in content_type:
        pillar = "comedy"
        category_id = str(clip_data.get("category_id") or "23")
        niche_tags = ["Indian Standup", "Comedy Shorts", "Hindi Comedy", "Standup Comedy", "Viral Comedy"]
        desc_hashtags = "#Shorts #StandupComedy #IndianComedy"
        channel_hook = "Subscribe to @bubucreates-p3c for daily viral Indian standup comedy Shorts!"
    elif "podcast" in content_type:
        pillar = "podcast"
        category_id = str(clip_data.get("category_id") or "24")
        niche_tags = ["Hindi Podcast", "Indian Podcast", "Podcast Shorts", "Motivation", "Life Lessons"]
        desc_hashtags = "#Shorts #Podcast #HindiPodcast"
        channel_hook = "Subscribe to @bubucreates-p3c for daily life insights & podcast Shorts!"
    else:
        pillar = "gameplay"
        category_id = str(clip_data.get("category_id") or "20")
        niche_tags = ["GTA 6", "GTA 6 Gameplay", "Indian Gaming", "Gaming Shorts", "Vice City"]
        desc_hashtags = "#Shorts #GTA6 #IndianGaming"
        channel_hook = "Subscribe to @bubucreates-p3c for daily GTA 6 gameplay & viral gaming Shorts!"

    # Preserve pre-built YouTube SEO description if already cleanly formatted with #Shorts
    pre_desc = (clip_data.get("youtube_description_final") or "").strip()
    if pre_desc and "#shorts" in pre_desc.lower() and "━━" not in pre_desc and "✈️" not in pre_desc:
        seo_description = pre_desc
    else:
        raw_desc = pre_desc or clip_data.get("tiktok_caption_final", "") or clean_title
        clean_desc_lines = [
            l.strip()
            for l in raw_desc.splitlines()
            if l.strip()
            and not l.strip().startswith("#")
            and "✈️" not in l
            and "📌" not in l
            and "━━" not in l
            and "Subscribe" not in l
        ]
        hook_desc = "\n".join(clean_desc_lines[:2]).strip() or clean_title
        seo_description = f"""{hook_desc}

{channel_hook}

{desc_hashtags}""".strip()

    # Put creator-specific tags FIRST, backfill with niche tags up to 10
    clip_tags = clip_data.get("youtube_tags_final") or clip_data.get("tags") or []
    final_tags = []
    for t in list(clip_tags) + niche_tags:
        tc = str(t).replace("#", "").strip()
        if tc and tc.lower() not in {x.lower() for x in final_tags}:
            final_tags.append(tc)
        if len(final_tags) >= 10:
            break

    default_lang = clip_data.get("default_language", "hi")
    default_audio_lang = clip_data.get("default_audio_language", "hi")

    body = {
        "snippet": {
            "title": title,
            "description": seo_description,
            "tags": final_tags,
            "categoryId": category_id,
            "defaultLanguage": default_lang,
            "defaultAudioLanguage": default_audio_lang,
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    print(f"\n[YOUTUBE UPLOAD] Publishing High-Retention Short: {title}")
    print(f"   Category: {category_id} | AudioLang: {default_audio_lang} | Tags ({len(final_tags)}): {', '.join(final_tags[:5])}...")
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

    pl_id = PLAYLIST_BY_PILLAR.get(pillar)
    if pl_id and video_id:
        try:
            youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": pl_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            ).execute()
            print(f"   Added Short {video_id} to {pillar} playlist ({pl_id})")
        except Exception as pe:
            print(f"[WARN] Could not add Short to playlist ({pl_id}): {pe}")

    return {"video_id": video_id, "url": yt_url, "title": title}


def load_json_list(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[WARN] Failed reading {path}: {e}")
        return []


def save_json_list(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def write_github_output(run_needed, fetch_files):
    gh_out = os.getenv("GITHUB_OUTPUT", "").strip()
    if not gh_out:
        return
    try:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"run_needed={'true' if run_needed else 'false'}\n")
            f.write(f"fetch_files={' '.join(fetch_files)}\n")
    except Exception as e:
        print(f"[WARN] Failed writing GITHUB_OUTPUT: {e}")


def cleanup_completed_files(clips, ig_published_videos, ig_published_titles, yt_published_videos, yt_published_titles, has_yt):
    for clip in clips:
        raw_vpath = clip.get("output_file") or clip.get("video_path")
        vname = safe_basename(raw_vpath)
        if not vname:
            continue
        vpath = f"queue/{vname}"
        tname = safe_basename(clip.get("thumbnail_path"))
        tpath = f"queue/{tname}" if tname else None

        ig_done = is_clip_published(clip, ig_published_videos, ig_published_titles)
        yt_done = (not has_yt) or is_clip_published(clip, yt_published_videos, yt_published_titles)

        if ig_done and yt_done:
            if os.path.exists(vpath):
                os.remove(vpath)
                print(f"   Deleted fully published video from queue: {vpath}")
            if tpath and os.path.exists(tpath):
                os.remove(tpath)
                print(f"   Deleted thumbnail: {tpath}")


def run_cloud_upload(
    manifest_path=DEFAULT_MANIFEST,
    min_interval_hours=2.5,
    yt_interval_hours=18.0,
    force=False,
    plan_only=False,
    respect_quiet_hours=True,
    respect_yt_peak=True,
):
    if not os.path.exists(manifest_path):
        print(f"[ERR] Manifest not found: {manifest_path}")
        write_github_output(False, [])
        return 1

    clips = load_json_list(manifest_path)
    if not clips:
        print("[INFO] Manifest is empty.")
        write_github_output(False, [])
        return 0

    now_utc = datetime.now(timezone.utc)
    auth_fp = compute_ig_auth_fingerprint()
    available_files = get_available_queue_files()

    ig_results = load_json_list(RESULTS_FILE)
    ig_published_videos = {
        safe_basename(r.get("video", "")).lower()
        for r in ig_results if r.get("status") == "published" and r.get("video")
    }
    ig_published_titles = {
        normalize_title(r.get("title", ""))
        for r in ig_results if r.get("status") == "published" and r.get("title")
    }

    yt_results = load_json_list(YT_RESULTS_FILE)
    yt_published_videos = {
        safe_basename(r.get("video", "")).lower()
        for r in yt_results if r.get("video")
    }
    yt_published_titles = {
        normalize_title(r.get("title", ""))
        for r in yt_results if r.get("title")
    }

    should_upload_yt, should_upload_ig, _ = evaluate_schedules(
        clips,
        ig_results,
        yt_results,
        min_interval_hours,
        yt_interval_hours,
        force,
        respect_quiet_hours,
        now_utc,
        auth_fp,
        respect_yt_peak=respect_yt_peak,
    )

    yt_next = find_next_clip(clips, yt_published_videos, yt_published_titles, available_files) if should_upload_yt else None
    ig_next = find_next_clip(clips, ig_published_videos, ig_published_titles, available_files) if should_upload_ig else None

    if plan_only:
        needed = []
        seen = set()
        for nxt, needs_thumb in ((yt_next, False), (ig_next, True)):
            if not nxt:
                continue
            _, _, vp, tp = nxt
            for p in ([vp, tp] if needs_thumb and tp else [vp]):
                sparse_p = f"/{p.lstrip('/')}"
                if sparse_p not in seen:
                    seen.add(sparse_p)
                    needed.append(sparse_p)
        run_needed = bool(needed)
        print(f"[PLAN-ONLY] run_needed={run_needed} | fetch_files={needed}")
        write_github_output(run_needed, needed)
        return 0

    if not yt_next and not ig_next:
        print("[SKIP] No uploads due this cycle. Exiting fast cleanly.")
        return 0

    # ---------------------------------------------------------
    # 1. INDEPENDENT YOUTUBE SHORTS PIPELINE
    # ---------------------------------------------------------
    yt_client = None
    has_yt = bool(os.getenv("YOUTUBE_TOKEN", "").strip() or os.path.exists("queue/youtube_token.json"))
    if should_upload_yt and yt_next:
        yt_client = get_youtube_client()
        if yt_client:
            live_yt_titles = fetch_existing_youtube_titles(yt_client)
            combined_yt_titles = yt_published_titles | live_yt_titles
            yt_next = find_next_clip(clips, yt_published_videos, combined_yt_titles, available_files)
            if not yt_next:
                print("[YOUTUBE] All available clips on disk are already published on live YouTube channel.")
            else:
                yt_idx, yt_clip, yt_vpath, _ = yt_next
                if not ensure_file_materialized(yt_vpath):
                    print(f"[WARN] Could not materialize YouTube video file: {yt_vpath}")
                else:
                    print(f"\n[CLOUD UPLOAD -> YOUTUBE] Clip {yt_idx}: {yt_vpath}")
                    try:
                        yt_data = upload_youtube_short(yt_client, yt_vpath, yt_clip)
                        yt_results.append({
                            "clip_index": yt_idx,
                            "video": yt_vpath,
                            "title": yt_data["title"],
                            "video_id": yt_data["video_id"],
                            "url": yt_data["url"],
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                        save_json_list(YT_RESULTS_FILE, yt_results)
                        yt_vname = safe_basename(yt_vpath)
                        if yt_vname:
                            yt_published_videos.add(yt_vname.lower())
                        yt_published_titles.add(normalize_title(yt_data["title"]))
                    except Exception as e:
                        print(f"[WARN] YouTube upload failed: {e}")

    # ---------------------------------------------------------
    # 2. INDEPENDENT INSTAGRAM REELS PIPELINE
    # ---------------------------------------------------------
    ig_failed_actively = False
    if should_upload_ig and ig_next:
        ig_idx, ig_clip, ig_vpath, ig_tpath = ig_next
        title = ig_clip.get("title") or ig_clip.get("youtube_title_final") or ig_clip.get("title_inggris", "")
        caption = (
            ig_clip.get("instagram_caption_final")
            or ig_clip.get("tiktok_caption_final")
            or ig_clip.get("tiktok_caption")
        )
        if not caption or "✈️" not in caption:
            caption = build_viral_instagram_caption(ig_clip)

        if not ensure_file_materialized(ig_vpath):
            print(f"[ERR] Could not materialize Instagram video file: {ig_vpath}")
            ig_failed_actively = True
        else:
            if ig_tpath:
                ensure_file_materialized(ig_tpath)

            print(f"\n[INSTAGRAM UPLOAD] Publishing Clip {ig_idx} to @chicanayaa: {title}")
            print(f"   Video: {ig_vpath}")
            if ig_tpath and os.path.exists(ig_tpath):
                print(f"   Thumbnail: {ig_tpath}")

            try:
                cl = get_instagram_client()
                upload_kwargs = {"path": ig_vpath, "caption": caption}
                if ig_tpath and os.path.exists(ig_tpath):
                    upload_kwargs["thumbnail"] = ig_tpath

                media = None
                for attempt in range(2):
                    try:
                        media = cl.clip_upload(**upload_kwargs)
                        break
                    except Exception as up_err:
                        if attempt == 0 and not is_auth_related_error(str(up_err)):
                            print(f"[WARN] Transient Instagram upload error ({up_err}), retrying once in 5s...")
                            time.sleep(5)
                            continue
                        raise

                media_id = getattr(media, "id", None) or getattr(media, "pk", "")
                media_code = getattr(media, "code", "")
                post_url = f"https://www.instagram.com/reel/{media_code}/" if media_code else ""
                print(f"\n[SUCCESS] Instagram Reel Published! {post_url}")

                ig_results.append({
                    "clip_index": ig_idx,
                    "video": ig_vpath,
                    "title": title,
                    "status": "published",
                    "media_id": str(media_id),
                    "url": post_url,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                save_json_list(RESULTS_FILE, ig_results)
                ig_vname = safe_basename(ig_vpath)
                if ig_vname:
                    ig_published_videos.add(ig_vname.lower())
                ig_published_titles.add(normalize_title(title))
            except Exception as e:
                err_str = str(e)
                print(f"[ERR] Instagram upload failed: {err_str}")
                ig_results.append({
                    "clip_index": ig_idx,
                    "video": ig_vpath,
                    "title": title,
                    "status": "failed",
                    "auth_error": is_auth_related_error(err_str),
                    "auth_fingerprint": auth_fp,
                    "error": err_str,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                save_json_list(RESULTS_FILE, ig_results)
                ig_failed_actively = True

    # ---------------------------------------------------------
    # 3. CLEANUP MATERIALIZED FILES COMPLETED BY BOTH PLATFORMS
    # ---------------------------------------------------------
    cleanup_completed_files(
        clips,
        ig_published_videos,
        ig_published_titles,
        yt_published_videos,
        yt_published_titles,
        has_yt,
    )

    if ig_failed_actively:
        return 1
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--test-mode", action="store_true", default=True)
    parser.add_argument("--min-interval", type=float, default=2.5, help="Minimum interval in hours between IG uploads")
    parser.add_argument("--yt-interval", type=float, default=18.0, help="Minimum interval in hours between YT uploads")
    parser.add_argument("--force", action="store_true", help="Force upload now, ignoring interval, cooldown & quiet hours")
    parser.add_argument("--plan-only", action="store_true", help="Stdlib-only fast pacing & sparse-fetch planner")
    parser.add_argument("--no-quiet-hours", action="store_true", help="Disable IST overnight quiet window (01:00-07:30 IST)")
    parser.add_argument("--no-yt-peak-window", action="store_true", help="Allow YT uploads outside 17:30-22:30 IST prime evening")
    args = parser.parse_args()

    exit_code = run_cloud_upload(
        manifest_path=args.manifest,
        min_interval_hours=args.min_interval,
        yt_interval_hours=args.yt_interval,
        force=args.force,
        plan_only=args.plan_only,
        respect_quiet_hours=not args.no_quiet_hours,
        respect_yt_peak=not args.no_yt_peak_window,
    )
    sys.exit(exit_code)
