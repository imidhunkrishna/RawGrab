"""
apify_downloader.py — AMTCE Apify Integration Module
=====================================================
Two responsibilities:

  Track A · Tier 9 Fallback
    apify_get_video_url(instagram_url) → str | None
    Called by downloader.py ONLY when all 8 yt-dlp strategies fail.
    Returns a direct CDN .mp4 URL or None.

  Track B · Account Mode (Auto-Discovery)
    apify_scrape_creator_accounts(creator_name, source_accounts, limit) → list[dict]
    Called by content_scheduler.py daily.
    Returns a list of reel metadata dicts including videoUrl.
    Applies two-stage filtering before returning:
      Stage 1 — Metadata filters (free, no API calls)
      Stage 2 — Gemini thumbnail pre-screen (uses Apify displayUrl JPEG)
"""

import os
import re
import io
import json
import logging
import time
from typing import Optional, List, Dict, Tuple

# Only real Instagram post/reel/story URLs are valid Apify inputs.
# CDN URLs (scontent-*.cdninstagram.com) must NEVER be sent to Apify.
_INSTAGRAM_URL_RE = re.compile(
    r"^https://(www\.)?instagram\.com/[A-Za-z0-9._\-/?=&%]+$"
)

import signal
logger = logging.getLogger(__name__)

_active_apify_run_id: Optional[str] = None


def _emergency_shutdown(sig=None, frame=None):
    global _active_apify_run_id
    print("\n🛑 [EMERGENCY SHUTDOWN] Sudden user exit requested (Ctrl + C). Terminating process immediately...")
    if _active_apify_run_id:
        try:
            print(f"⚡ [APIFY ABORT] Aborting active Apify Cloud run {_active_apify_run_id} to save API credits...")
            client = _get_client()
            client.run(_active_apify_run_id).abort()
        except Exception:
            pass
    os._exit(0)


try:
    signal.signal(signal.SIGINT, _emergency_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _emergency_shutdown)
except Exception:
    pass

# Ensure .env is loaded from project root
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
try:
    from dotenv import load_dotenv
    _local_env = os.path.join(os.path.dirname(__file__), ".env")
    _root_env = os.path.join(_REPO_ROOT, ".env")
    if os.path.exists(_local_env):
        load_dotenv(_local_env, override=True)
    if os.path.exists(_root_env):
        load_dotenv(_root_env, override=True)
except ImportError:
    pass

# ── ENV Config ────────────────────────────────────────────────────────────────
APIFY_TOKEN         = os.getenv("APIFY_API_TOKEN") or os.getenv("APIFY_TOKEN") or ""
APIFY_ACTOR         = "apify/instagram-scraper"
APIFY_GENERIC_ACTOR = os.getenv("APIFY_GENERIC_ACTOR", "apify/website-scraper")
DAILY_QUOTA         = int(os.getenv("APIFY_DAILY_QUOTA", "50"))

_VIDEO_URL_RE = re.compile(
    r"https?://[^\s\'\"]+?\.(?:mp4|webm|m3u8|mov|ogg)(?:\?[^\s\'\"]*)?",
    re.IGNORECASE,
)

# ── Pre-Screen Config ──────────────────────────────────────────────────────────
PRESCREEN_ENABLED   = os.getenv("APIFY_GEMINI_PRESCREEN", "yes").lower() in ("yes", "true", "1")
PRESCREEN_BUDGET    = int(os.getenv("APIFY_PRESCREEN_BUDGET", "50"))   # max calls per harvest cycle
MIN_VIEW_AND_LIKES  = int(os.getenv("APIFY_MIN_VIEW_AND_LIKES_COUNT", "0"))  # 0 = disabled. Controls minimum views AND minimum likes threshold.
BLOCK_SPONSORED     = os.getenv("APIFY_BLOCK_SPONSORED", "yes").lower() in ("yes", "true", "1")
MAX_PEOPLE_IN_FRAME = int(os.getenv("APIFY_MAX_PEOPLE_IN_FRAME", "3"))

# ── Deep / Bottom-Scrape Config ───────────────────────────────────────────────
# APIFY_DEEP_LIMIT — how many posts per account Apify actually FETCHES from
# Instagram (the scrape pool).  This is independent of how many are downloaded.
# Set to 0 to defer to the limit_per_account arg passed by the caller.
# Recommended: 50–200 when you want to go deep.  Default 0 = caller decides.
APIF_DEEP_LIMIT     = int(os.getenv("APIFY_DEEP_LIMIT", "0"))

# APIFY_SCRAPE_FROM_DATE — ISO date (YYYY-MM-DD) or empty string.
# When set, Apify scrapes backwards until it reaches posts from this date.
# Example: APIFY_SCRAPE_FROM_DATE=2025-01-01  →  pulls everything since Jan 2025
# Leave blank (default) to only fetch the newest N posts (APIFY_DEEP_LIMIT).
APIF_SCRAPE_FROM_DATE = os.getenv("APIFY_SCRAPE_FROM_DATE", "").strip()

# Real-Time Relative Timing Filter (APIFY_MAX_POST_AGE_HOURS)
# 0 = disabled (any post age allowed). Example: 2.0 = only videos published in last 2 hours.
MAX_POST_AGE_HOURS = float(os.getenv("APIFY_MAX_POST_AGE_HOURS", "0"))

# APIFY_OLDEST_FIRST — set to 'yes' to return results from oldest→newest
# so you process bottom posts first (useful to fill archive gaps).
# Default: 'no' → newest first (normal daily harvest mode).
APIF_OLDEST_FIRST   = os.getenv("APIFY_OLDEST_FIRST", "no").lower() in ("yes", "true", "1")

# ── Disk-Persisted Quota (salesman_state) ─────────────────────────────────────
# Replaces the old in-memory _quota_used / _quota_date which reset on restart.
# Now survives restarts — protecting the $5/month Apify budget.
try:
    from Core_Modules.salesman_state import get_apify_quota as _get_apify_quota
    _SALESMAN_QUOTA_AVAILABLE = True
except ImportError:
    _SALESMAN_QUOTA_AVAILABLE = False
    logger.debug("salesman_state not found — using standard quota tracking")

# Legacy in-memory fallback (only used if salesman_state import fails)
_quota_used: int = 0
_quota_date: str = ""


def _get_instagram_cookies() -> Optional[str]:
    """
    Retrieves Instagram cookies to authenticate Apify scraper.
    Checks:
      1. APIFY_INSTAGRAM_COOKIES or INSTAGRAM_COOKIES env variable
      2. Mapped COOKIES_FILE (by parsing its Netscape or JSON format)
    """
    # 1. Check env vars
    for env_var in ["APIFY_INSTAGRAM_COOKIES", "INSTAGRAM_COOKIES"]:
        val = os.getenv(env_var, "").strip()
        if val:
            logger.info("🍪 Found Instagram cookies in env variable: %s", env_var)
            return val

    # 2. Check cookies file
    cookies_file = os.getenv("COOKIES_FILE", "cookies.txt")
    if os.path.exists(cookies_file) and os.path.getsize(cookies_file) > 10:
        logger.info("🍪 Reading Instagram cookies from file: %s", cookies_file)
        try:
            with open(cookies_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().strip()
            
            # Check if it's JSON format
            if content.startswith("[") or content.startswith("{"):
                try:
                    data = json.loads(content)
                    if isinstance(data, list):
                        cookies_list = []
                        for c in data:
                            if isinstance(c, dict) and "instagram.com" in c.get("domain", ""):
                                name = c.get("name")
                                value = c.get("value")
                                if name and value:
                                    cookies_list.append(f"{name}={value}")
                        if cookies_list:
                            logger.info("🍪 Parsed %d Instagram cookies from JSON file", len(cookies_list))
                            return "; ".join(cookies_list)
                except Exception as je:
                    logger.debug("Failed to parse cookies file as JSON: %s", je)
            
            # Netscape cookie format parser
            cookies_list = []
            for line in content.splitlines():
                line = line.strip()
                if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                    continue
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                parts = line.split("\t")
                if len(parts) >= 7:
                    domain, _, _, _, _, name, value = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
                    if "instagram.com" in domain:
                        cookies_list.append(f"{name}={value}")
            
            if cookies_list:
                logger.info("🍪 Parsed %d Instagram cookies from Netscape file", len(cookies_list))
                return "; ".join(cookies_list)
        except Exception as e:
            logger.warning("💥 Failed to parse cookies file: %s", e)

    return None


def _iter_json_values(obj):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_json_values(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_json_values(item)
    else:
        yield obj


def _find_video_urls(item):
    urls = []
    for value in _iter_json_values(item):
        if isinstance(value, str):
            for match in _VIDEO_URL_RE.finditer(value):
                video_url = match.group(0)
                if video_url not in urls:
                    urls.append(video_url)
    return urls


def _check_quota(needed: int = 1) -> bool:
    """
    Returns True if we have quota remaining for today.
    Uses disk-persisted salesman_state when available (survives restarts).
    Falls back to in-memory tracking if salesman_state is unavailable.
    """
    if _SALESMAN_QUOTA_AVAILABLE:
        return _get_apify_quota().check(needed)

    # ── Legacy in-memory fallback ─────────────────────────────────────────────
    global _quota_used, _quota_date
    today = time.strftime("%Y-%m-%d")
    if _quota_date != today:
        _quota_date = today
        _quota_used = 0
        logger.info("📅 Apify daily quota reset for %s", today)

    if _quota_used + needed > DAILY_QUOTA:
        logger.warning(
            "🛑 Apify daily quota exhausted (%d/%d). Sleeping until tomorrow. 💤",
            _quota_used, DAILY_QUOTA
        )
        return False
    return True


def _consume_quota(amount: int = 1) -> None:
    if _SALESMAN_QUOTA_AVAILABLE:
        _get_apify_quota().consume(amount)
    else:
        # Legacy in-memory fallback
        global _quota_used
        _quota_used += amount
        logger.info("💰 Apify quota used: %d/%d today", _quota_used, DAILY_QUOTA)
    
    # Track usage in API key pool if pooled key was used
    if _get_client._last_pooled_user:
        try:
            from Core_Modules.api_key_pool_manager import record_api_usage
            record_api_usage("apify_api_token", _get_client._last_pooled_user, success=True)
            logger.info(f"📊 [API POOL] Recorded usage for user: {_get_client._last_pooled_user}")
        except ImportError:
            pass


def _run_apify_actor_nonblocking(client, actor_name: str, run_input: Dict) -> Dict:
    """
    Runs an Apify actor in non-blocking mode with responsive Ctrl+C signal handling.
    Aborts the cloud container on Apify Cloud if the user interrupts execution (Ctrl+C).
    """
    global _active_apify_run_id
    run_obj = client.actor(actor_name).start(run_input=run_input)
    _active_apify_run_id = run_obj.get("id")
    logger.info("🚀 [APIFY RUN STARTED] Run ID: %s (%s)", _active_apify_run_id, actor_name)
    try:
        while True:
            detail = client.run(_active_apify_run_id).get()
            status = detail.get("status")
            if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                logger.info("🏁 [APIFY RUN FINISHED] Status: %s", status)
                _active_apify_run_id = None
                return detail
            time.sleep(0.5)
    except (KeyboardInterrupt, SystemExit):
        if _active_apify_run_id:
            try:
                print(f"\n⚡ Aborting Apify Cloud run {_active_apify_run_id}...")
                client.run(_active_apify_run_id).abort()
            except Exception:
                pass
        _emergency_shutdown()
        raise


def _get_client(requesting_user_id: Optional[str] = None):
    """
    Returns an ApifyClient instance.
    Checks requesting user's personal Apify token first, then API key pool, then env var.
    """
    if requesting_user_id:
        try:
            from Phase_1.Telegram_Storage_Manager.telegram_user_manager import get_user_apify_token
            u_token = get_user_apify_token(requesting_user_id)
            if u_token:
                logger.info(f"🔑 [USER TOKEN] Using personal Apify token for User ID: {requesting_user_id}")
                from apify_client import ApifyClient
                return ApifyClient(u_token)
        except Exception as _u_tok_err:
            logger.warning("Notice resolving user Apify token: %s", _u_tok_err)

    # Try to get token from API key pool first
    try:
        from Core_Modules.api_key_pool_manager import get_next_api_key
        pooled_key = get_next_api_key("apify_api_token", requesting_user_id)
        if pooled_key:
            token = pooled_key["api_key"]
            logger.info(f"🔑 [API POOL] Using pooled token from user: {pooled_key['user_id']}")
            # Store for usage tracking later
            _get_client._last_pooled_user = pooled_key["user_id"]
        else:
            token = os.getenv("APIFY_API_TOKEN") or os.getenv("APIFY_TOKEN") or APIFY_TOKEN
            _get_client._last_pooled_user = None
    except ImportError:
        token = os.getenv("APIFY_API_TOKEN") or os.getenv("APIFY_TOKEN") or APIFY_TOKEN
        _get_client._last_pooled_user = None
    
    if not token:
        raise RuntimeError(
            "APIFY_API_TOKEN is not set in .env and no pooled keys available. Add your token to .env or provide user credentials."
        )
    try:
        from apify_client import ApifyClient
        return ApifyClient(token)
    except ImportError:
        raise RuntimeError("apify-client not installed. Run: pip install apify-client")

_get_client._last_pooled_user = None


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — Metadata Filter (FREE, no API cost)
# ─────────────────────────────────────────────────────────────────────────────

def _stage1_metadata_filter(item: Dict) -> tuple:
    """
    Fast pre-download filter using only Apify-returned metadata.
    Returns (approved: bool, reject_reason: str)

    Rules:
      1. Drop pinned posts (outdated/promoted content)
      2. Drop sponsored / paid partnership posts
      3. Drop posts with zero or very low engagement (likely private or ad)
      4. Drop non-vertical content (strict aspect ratio check)
         A real 9:16 reel has height >= 1.75x width.
         1:1 square videos (with black bars that fake 9:16) are caught here
         when Instagram DOES report real dimensions.
      5. Drop non-video posts (images, carousels)
    """
    # Rule 1: Pinned
    if item.get("isPinned", False):
        return False, "pinned_post"

    # Rule 2: Sponsored
    if BLOCK_SPONSORED:
        if item.get("isSponsored", False) or item.get("sponsorships"):
            return False, "sponsored_post"
        # Caption-based ad detection (backup — not all sponsored posts have isSponsored)
        caption_lower = (item.get("caption") or "").lower()
        if "paid partnership" in caption_lower or "sponsored" in caption_lower:
            return False, "paid_partnership_caption"

    # Rule 3: Engagement floor
    # NOTE: When Apify falls back to page-scraping (post-details page fallback),
    # it strips videoViewCount and likesCount — they return as 0 or None.
    # We only kill on zero_engagement if BOTH are missing AND no videoUrl exists.
    # This prevents false-rejection of genuine reels scraped via page fallback.
    if MIN_VIEW_AND_LIKES > 0:
        views = item.get("videoViewCount", 0) or 0
        likes = item.get("likesCount", 0) or 0
        has_video_url = bool(item.get("videoUrl", ""))
        if views == 0 and likes == 0 and not has_video_url:
            return False, "zero_engagement_no_url"
        if views > 0 and views < MIN_VIEW_AND_LIKES:
            return False, f"low_views_{views}"
        if likes > 0 and likes < MIN_VIEW_AND_LIKES:
            return False, f"low_likes_{likes}"

    # Rule 4: Strict aspect ratio (only when dimensions are available)
    h = item.get("dimensionsHeight", 0) or 0
    w = item.get("dimensionsWidth", 0) or 0
    if h > 0 and w > 0:
        ratio = h / w
        # 9:16 = 1.777... We require at least 1.6 to be safe (allows slight crop)
        # 1:1 = 1.0, 4:3 = 1.33, these get filtered
        if ratio < 1.6:
            return False, f"wrong_aspect_ratio_{w}x{h}_ratio{ratio:.2f}"

    # Rule 5: Must be a video
    product_type = item.get("productType", "")
    media_type   = item.get("type", "") or item.get("mediaType", "")
    has_video_url = bool(item.get("videoUrl", ""))
    is_video = (
        product_type == "clips"
        or str(media_type).lower() in ("video", "reel")
        or has_video_url
    )
    if not is_video:
        return False, f"not_a_video_product_type={product_type}"

    # Rule 6: Real-Time Post Age Filter (APIFY_MAX_POST_AGE_HOURS)
    if MAX_POST_AGE_HOURS > 0:
        post_timestamp = item.get("timestamp") or item.get("takenAtTimestamp") or item.get("publishedAt")
        if post_timestamp:
            try:
                if isinstance(post_timestamp, str):
                    from datetime import datetime
                    dt = datetime.fromisoformat(post_timestamp.replace("Z", "+00:00"))
                    post_epoch = dt.timestamp()
                else:
                    post_epoch = float(post_timestamp)

                age_hours = (time.time() - post_epoch) / 3600.0
                if age_hours > MAX_POST_AGE_HOURS:
                    return False, f"post_age_{age_hours:.1f}h_exceeds_max_{MAX_POST_AGE_HOURS}h"
            except Exception as _te:
                logger.debug("Post timestamp age parse notice: %s", _te)

    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — Gemini Thumbnail Pre-Screen
# Uses the Apify-provided displayUrl JPEG thumbnail.
# NO video download. NO ffmpeg. One HTTP GET + one Gemini Vision call.
# Replaces Part B (content pre-screening) from gemini_enhance_for_watermark.py
# ─────────────────────────────────────────────────────────────────────────────

def gemini_reel_prescreen(item: Dict, creator_name: str) -> tuple:
    """
    Pre-screens a reel using its thumbnail JPEG from Apify's displayUrl field.

    This function runs PRE-download, using a 20KB JPEG, to avoid wasting
    bandwidth and processing on low-quality or irrelevant content.

    Returns:
        (approved: bool, result: dict)
        result contains: black_bars, is_ad, people_count, primary_subject,
                         woman_is_focus, content_type, confidence, reject_reason
    """
    _default_approve = (True, {
        "approved": True,
        "black_bars": False,
        "is_ad": False,
        "people_count": -1,
        "primary_subject": "unknown",
        "woman_is_focus": True,
        "content_type": "unknown",
        "confidence": 0.0,
        "reject_reason": None,
        "_source": "fallback_approved",
    })

    thumbnail_url = item.get("displayUrl") or item.get("thumbnailUrl") or ""
    if not thumbnail_url:
        logger.debug("[PRESCREEN] No displayUrl available — auto-approving")
        return True, {**_default_approve[1], "_source": "no_thumbnail"}

    # ── Fetch thumbnail JPEG ──────────────────────────────────────────────────
    try:
        import requests
        resp = requests.get(thumbnail_url, timeout=8, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/115.0.0.0"
        })
        resp.raise_for_status()
        img_bytes = resp.content
    except Exception as exc:
        logger.debug("[PRESCREEN] Thumbnail fetch failed: %s — auto-approving", exc)
        return True, {**_default_approve[1], "_source": "fetch_failed"}

    # ── Convert to PIL Image for Gemini ──────────────────────────────────────
    try:
        from PIL import Image
        pil_img = Image.open(io.BytesIO(img_bytes))
        # Ensure RGB (thumbnails can be JPEG/RGBA)
        if pil_img.mode != "RGB":
            pil_img = pil_img.convert("RGB")
    except Exception as exc:
        logger.debug("[PRESCREEN] Thumbnail PIL parse failed: %s — auto-approving", exc)
        return True, {**_default_approve[1], "_source": "pil_failed"}

    # ── Gemini Vision call ────────────────────────────────────────────────────
    try:
        from Phase_1.Gemini_Modules import gemini_router
    except ImportError:
        try:
            from Intelligence_Modules.gemini_governor import gemini_router
        except ImportError:
            return True, {**_default_approve[1], "_source": "import_failed"}

    prompt = f"""You are a reel quality inspector for a social media automation system.
Analyze this Instagram reel thumbnail image and return a strict JSON assessment.

TARGET CREATOR / CONTENT: "{creator_name}"

Analyze for ALL of the following:

1. BLACK BARS / FALSE 9:16
   Does the image have significant black bars on the sides OR top/bottom?
   (This means the actual video content is 1:1 or 4:3 stuffed into a 9:16 frame)

2. AD / PROMOTIONAL CONTENT
   Is this an advertisement? Look for: price tags, product shots, "link in bio" text,
   brand logos with product names, "swipe up", discount offers, e-commerce layouts,
   multiple products displayed, before/after comparisons.

3. PEOPLE COUNT AND GENDER
   How many people are clearly visible in the thumbnail?
   What is the primary subject's gender? (man / woman / unclear)
   Is a single woman the clear visual focus? (not one of many people)

4. CONTENT TYPE
   Classify: dance_fashion | lifestyle_selfie | group_event | talking_head |
             news_interview | product_ad | no_person | other

Return ONLY this JSON, no other text:
{{
  "black_bars": true/false,
  "is_ad": true/false,
  "people_count": <integer 0-10>,
  "primary_subject": "man" | "woman" | "group" | "none",
  "woman_is_focus": true/false,
  "content_type": "<one of the types above>",
  "confidence": <0.0-1.0>
}}"""

    try:
        raw = gemini_router.generate(
            task_type="watermark",   # reuse existing quota bucket
            prompt=[pil_img, prompt],
            module_name="apify_prescreen",
        )
    except Exception as exc:
        logger.warning("[PRESCREEN] Gemini call failed: %s — auto-approving", exc)
        return True, {**_default_approve[1], "_source": "gemini_failed"}

    # ── Parse JSON response ───────────────────────────────────────────────────
    try:
        # Strip markdown fences if present
        cleaned = raw.strip()
        if "```" in cleaned:
            m = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
            cleaned = m.group(1) if m else cleaned.replace("```", "")
        j_start = cleaned.find("{")
        j_end   = cleaned.rfind("}")
        if j_start != -1 and j_end > j_start:
            cleaned = cleaned[j_start:j_end + 1]
        result = json.loads(cleaned)
    except Exception as exc:
        logger.warning("[PRESCREEN] JSON parse failed: %s — auto-approving. Raw: %.100s", exc, raw)
        return True, {**_default_approve[1], "_source": "json_parse_failed"}

    # ── Evaluate rejection criteria ───────────────────────────────────────────
    reject_reason = None

    if result.get("black_bars", False):
        reject_reason = "black_bars_fake_9:16"

    elif result.get("is_ad", False):
        reject_reason = "detected_as_ad"

    elif result.get("content_type", "") in ("product_ad", "news_interview"):
        reject_reason = f"content_type={result.get('content_type')}"

    elif result.get("primary_subject", "") == "man" and not result.get("woman_is_focus", True):
        reject_reason = "primary_subject_is_man"

    elif result.get("people_count", 1) > MAX_PEOPLE_IN_FRAME:
        count = result.get("people_count", 0)
        reject_reason = f"too_many_people_{count}"

    approved = reject_reason is None
    result["approved"]      = approved
    result["reject_reason"] = reject_reason
    result["_source"]       = "gemini_prescreen"

    log_icon = "✅" if approved else "🚫"
    logger.info(
        "%s [PRESCREEN] %s | content=%s | people=%d | subject=%s | ad=%s | bars=%s | conf=%.2f",
        log_icon,
        reject_reason or "APPROVED",
        result.get("content_type", "?"),
        result.get("people_count", -1),
        result.get("primary_subject", "?"),
        result.get("is_ad", False),
        result.get("black_bars", False),
        result.get("confidence", 0.0),
    )
    return approved, result


# ─────────────────────────────────────────────────────────────────────────────
# Track A — Tier 9 Fallback
# Called by downloader.py when all 8 yt-dlp strategies fail on Instagram
# ─────────────────────────────────────────────────────────────────────────────

def apify_get_video_url(instagram_url: str) -> Optional[str]:
    """
    Last-resort fallback for a single Instagram URL.
    Returns a direct CDN .mp4 URL string, or None on failure.

    Cost: 1 Apify result unit per call. Use sparingly.

    GUARD: Only accepts real instagram.com URLs.
    CDN URLs (scontent-*.cdninstagram.com) are rejected immediately
    without making any API call — no quota consumed, no error.
    """
    # ── URL Guard — reject non-instagram.com URLs immediately ────────────────
    if not _INSTAGRAM_URL_RE.match(instagram_url):
        logger.warning(
            "🚫 [TIER 9] Skipping Apify — URL is not a valid instagram.com URL: %.80s…",
            instagram_url
        )
        return None

    if not _check_quota(1):
        return None

    logger.warning("💰 [TIER 9 — APIFY FALLBACK] All yt-dlp strategies failed.")
    logger.warning("🌐 Sending to Apify: %s", instagram_url)

    try:
        client = _get_client()
        run_input = {
            "directUrls":   [instagram_url],
            "resultsType":  "posts",
            "resultsLimit": 1,
        }
        
        cookies = _get_instagram_cookies()
        if cookies:
            run_input["loginCookies"] = cookies
            logger.info("🔑 Injecting Instagram loginCookies into Apify input")

        run = client.actor(APIFY_ACTOR).call(run_input=run_input)

        # Consume quota immediately after actor launch — the call already happened.
        _consume_quota(1)

        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
        if not items:
            logger.error("Apify returned no items for URL: %s", instagram_url)
            return None

        item = items[0]

        # -- Restricted content detection -------------------------------------
        # Apify warns "Restricted access... only partial data available"
        # In this case videoUrl is absent and 'url' is the original instagram URL.
        # We must NOT treat the original instagram URL as a CDN URL or we loop forever.
        is_restricted = (
            "restricted" in str(item.get("error", "")).lower()
            or item.get("isRestricted", False)
            or not item.get("videoUrl")  # no video means restricted/unavailable
        )

        video_url = item.get("videoUrl")  # Only accept videoUrl, never 'url' fallback

        if not video_url:
            if is_restricted:
                logger.error(
                    "Apify: Content is restricted/age-gated and cannot be downloaded: %s",
                    instagram_url
                )
            else:
                logger.error(
                    "Apify item had no videoUrl field. Keys: %s", list(item.keys())
                )
            return None

        # -- CDN URL validation -----------------------------------------------
        # A valid CDN URL must NOT be an instagram.com post URL.
        # If it is, Apify returned a fallback URL that would cause an infinite loop.
        if "instagram.com" in video_url and "/reel/" in video_url:
            logger.error(
                "Apify returned the original post URL instead of a CDN URL "
                "(content likely restricted). Aborting to prevent infinite loop."
            )
            return None

        logger.info("Apify CDN URL retrieved successfully")
        return video_url

    except Exception as exc:
        logger.error("💥 Apify Tier 9 fallback exception: %s", exc)
        return None


def find_live_apify_store_fallback(keyword: str) -> Optional[str]:
    """
    Dynamically queries Apify Store via API when a hardcoded actor is revoked/deprecated.
    Finds the top active free replacement actor matching the platform keyword (e.g. 'instagram', 'tiktok').
    """
    try:
        client = _get_client()
        store_items = client.store().list(search=keyword).items
        for item in store_items:
            name = item.get("name")
            username = item.get("username", "apify")
            actor_id = f"{username}/{name}"
            pricing = item.get("pricingModel", "FREE")
            if pricing == "FREE" or not pricing:
                logger.info("🔄 [APIFY STORE AUTO-RECOVERY] Found active replacement actor for '%s': %s", keyword, actor_id)
                return actor_id
        return None
    except Exception as err:
        logger.error("Apify Store auto-recovery query failed: %s", err)
        return None


def select_smart_apify_actor(url: str, default_actor: Optional[str] = None) -> Tuple[str, Dict]:
    """
    Smartly selects the optimal Apify Store Actor and input payload based on URL domain:
      - Instagram -> 'apify/instagram-scraper' or 'apify/instagram-reel-scraper'
      - TikTok    -> 'apify/tiktok-scraper'
      - Twitter/X -> 'apify/tweet-scraper'
      - Web Pages -> 'apify/website-content-crawler'
    """
    if default_actor and default_actor not in ("apify/website-scraper", ""):
        return default_actor, {"startUrls": [{"url": url}], "maxPagesPerCrawl": 1}

    low_url = url.lower()
    if "instagram.com" in low_url:
        logger.info("🧠 [SMART APIFY ROUTER] Detected Instagram URL -> selecting apify/instagram-scraper")
        return "apify/instagram-scraper", {"directUrls": [url], "resultsLimit": 1}
    elif "youtube.com" in low_url or "youtu.be" in low_url:
        logger.info("🧠 [SMART APIFY ROUTER] Detected YouTube URL -> selecting clockworks/free-youtube-downloader")
        return "clockworks/free-youtube-downloader", {"startUrls": [{"url": url}], "downloadVideo": True}
    elif "tiktok.com" in low_url:
        logger.info("🧠 [SMART APIFY ROUTER] Detected TikTok URL -> selecting apify/tiktok-scraper")
        return "apify/tiktok-scraper", {"postURLs": [url], "resultsPerPage": 1}
    elif "twitter.com" in low_url or "x.com" in low_url:
        logger.info("🧠 [SMART APIFY ROUTER] Detected Twitter/X URL -> selecting apify/tweet-scraper")
        return "apify/tweet-scraper", {"startUrls": [url]}
    elif any(ext in low_url for ext in (".mp4", ".mkv", ".webm", ".m3u8", ".ts")):
        logger.info("🧠 [SMART APIFY ROUTER] Direct media stream -> selecting apify/website-scraper")
        return "apify/website-scraper", {"startUrls": [{"url": url}], "maxPagesPerCrawl": 1}
    else:
        logger.info("🧠 [SMART APIFY ROUTER] Web URL -> selecting apify/website-content-crawler")
        return "apify/website-content-crawler", {"startUrls": [{"url": url}], "maxCrawlDepth": 0}


def apify_get_video_url_any(url: str, actor_name: Optional[str] = None) -> Optional[str]:
    """
    Generic & Smart-Routed Apify fallback for any URL.
    Auto-switches to the best Apify actor based on input URL domain.
    Includes Automatic Apify Store Discovery & Revocation Self-Healing.
    Returns direct video URL when available, or None on failure.
    """
    if not _check_quota(1):
        return None

    actor_to_use, run_input = select_smart_apify_actor(url, actor_name or APIFY_GENERIC_ACTOR)
    logger.warning("💰 [SMART APIFY ROUTER] Dispatching URL to actor %s: %s", actor_to_use, url)

    try:
        client = _get_client()
        run = _run_apify_actor_nonblocking(client, actor_to_use, run_input)
        _consume_quota(1)

        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
        if not items:
            logger.error("Apify actor %s returned no items for URL: %s", actor_to_use, url)
            return None

        for item in items:
            urls = _find_video_urls(item)
            if urls:
                logger.info("Apify actor %s found video URL(s): %s", actor_to_use, urls)
                return urls[0]

        logger.error("Apify actor %s returned no direct video URLs for: %s", actor_to_use, url)
        return None
    except Exception as exc:
        err_msg = str(exc).lower()
        logger.error("💥 Apify smart fallback exception (%s): %s", actor_to_use, exc)
        
        # ── AUTOMATIC REVOCATION SELF-HEALING ─────────────────────────────────
        if any(term in err_msg for term in ("404", "not found", "revoked", "deprecated", "access denied", "cannot find actor")):
            logger.warning("⚠️ Actor '%s' failed/revoked. Initiating Live Apify Store Auto-Discovery...", actor_to_use)
            domain_keyword = "instagram" if "instagram" in url.lower() else ("tiktok" if "tiktok" in url.lower() else ("twitter" if "twitter" in url.lower() or "x.com" in url.lower() else "video"))
            fallback_actor = find_live_apify_store_fallback(domain_keyword)
            if fallback_actor and fallback_actor != actor_to_use:
                try:
                    logger.info("🚀 [AUTO-RECOVERY RETRY] Retrying URL with auto-discovered store actor: %s", fallback_actor)
                    run = client.actor(fallback_actor).call(run_input={"startUrls": [{"url": url}], "directUrls": [url]})
                    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
                    for item in items:
                        urls = _find_video_urls(item)
                        if urls:
                            return urls[0]
                except Exception as retry_exc:
                    logger.error("💥 Apify auto-recovery retry failed: %s", retry_exc)
        
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Track B — Account Mode (Auto-Discovery)
# Called by content_scheduler.py for daily automated scraping
# ─────────────────────────────────────────────────────────────────────────────

def apify_scrape_creator_accounts(
    creator_name:    str,
    source_accounts: List[str],
    limit_per_account: int = 11,
) -> List[Dict]:
    """
    Scrapes the latest N reels from a list of professional source accounts.
    Applies two-stage filtering:
      Stage 1: Free metadata filters (pinned, sponsored, aspect ratio, views)
      Stage 2: Gemini thumbnail pre-screen (black bars, ads, people count, content type)

    Returns a list of metadata dicts. Each dict contains at minimum:
      - videoUrl:       direct CDN link to .mp4
      - caption:        post caption text
      - hashtags:       list of hashtag strings
      - taggedUsers:    list of tagged user dicts
      - ownerUsername:  source account username
      - url:            original Instagram post URL
      - timestamp:      post timestamp
      - _prescreen:     Gemini pre-screen result dict (for logging/debugging)
    """
    total_needed = len(source_accounts) * limit_per_account
    if not _check_quota(total_needed):
        return []

    if not source_accounts:
        logger.warning("⚠️ No source accounts configured for creator: %s", creator_name)
        return []


    # ── Per-Account 24h Cooldown ──────────────────────────────────────────────
    # Block accounts that were already scraped within the last 24 hours.
    # Prevents re-scraping the same content feed multiple times per day.
    try:
        from Core_Modules.salesman_state import get_account_scrape_throttle
        _throttle = get_account_scrape_throttle()
        _ready_accounts, _blocked_accounts = _throttle.filter_ready(source_accounts)
        if not _ready_accounts:
            logger.info(
                "⏸️ [ACCOUNT_THROTTLE] ALL %d accounts are on 24h cooldown — skipping Apify call.",
                len(source_accounts)
            )
            return []
        if len(_ready_accounts) < len(source_accounts):
            logger.info(
                "⏸️ [ACCOUNT_THROTTLE] %d/%d accounts ready (others on cooldown).",
                len(_ready_accounts), len(source_accounts)
            )
        source_accounts = _ready_accounts
    except Exception as _thr_err:
        logger.debug("Account throttle check skipped: %s", _thr_err)

    logger.info(
        "🎬 [ACCOUNT MODE] Scraping %d accounts for '%s' (%d reels each)…",
        len(source_accounts), creator_name, limit_per_account
    )

    profile_urls = [
        f"https://www.instagram.com/{acc.lstrip('@')}/"
        for acc in source_accounts
    ]

    try:
        client = _get_client()

        # ── Deep-scrape pool size ─────────────────────────────────────────────
        # APIFY_DEEP_LIMIT overrides the caller's limit_per_account when set.
        # This lets you widen the Apify fetch pool without changing DOWNLOADS_PER_ACCOUNT.
        effective_limit = APIF_DEEP_LIMIT if APIF_DEEP_LIMIT > 0 else limit_per_account

        # ── How far back to scrape ────────────────────────────────────────────
        # scrapePostsUntilDate tells the actor to keep going backwards in time
        # until it hits posts from this date. Empty = newest N posts only.
        until_date = APIF_SCRAPE_FROM_DATE  # e.g. "2025-01-01" or ""

        if until_date:
            logger.info(
                "⏳ [DEEP SCRAPE] scrapePostsUntilDate=%s | effective_limit=%d per account",
                until_date, effective_limit,
            )
        elif APIF_DEEP_LIMIT > 0:
            logger.info(
                "🔢 [DEEP SCRAPE] APIFY_DEEP_LIMIT=%d (overriding caller limit=%d)",
                APIF_DEEP_LIMIT, limit_per_account,
            )

        run_input = {
            "directUrls":           profile_urls,
            "resultsType":          "posts",
            "resultsLimit":         effective_limit,
            "addParentData":        False,
            "scrapePostsUntilDate": until_date,  # "" = latest only; "YYYY-MM-DD" = go deep
        }

        cookies = _get_instagram_cookies()
        if cookies:
            run_input["loginCookies"] = cookies
            logger.info("🔑 Injecting Instagram loginCookies into Apify input")

        run = _run_apify_actor_nonblocking(client, APIFY_ACTOR, run_input)

        raw_items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
        # Filter out status/error placeholder items
        items = [item for item in raw_items if item.get("shortcode") or item.get("id")]

        # ── Positional Pinned-Post Skip (Per Account) ───────────────────────
        # Instagram always pins posts at the top N profile slots.
        # We drop the first N posts encountered for each account from the raw list
        # to ensure we avoid the pinned posts while preserving all subsequent posts.
        _skip_n = int(os.getenv("APIFY_SKIP_TOP_N", "0"))
        if _skip_n > 0:
            filtered_items = []
            account_post_counters = {}
            for item in items:
                username = (item.get("ownerUsername") or item.get("username") or "").strip().lower()
                if not username:
                    filtered_items.append(item)
                    continue
                
                account_post_counters[username] = account_post_counters.get(username, 0) + 1
                if account_post_counters[username] <= _skip_n:
                    logger.info(
                        "📌 [PINNED_SKIP] Skipping post %d for @%s (pinned avoidance)",
                        account_post_counters[username], username
                    )
                    continue
                filtered_items.append(item)
            items = filtered_items

        actual_count = len(items)
        
        if actual_count > 0:
            _consume_quota(actual_count)
        else:
            logger.warning("⚠️ Apify returned 0 valid reels (likely blocked by Instagram or private account).")

        # Mark accounts as scraped NOW — cooldown starts from this moment.
        # Done immediately after Apify returns so even a crash later won't
        # let the same accounts be re-scraped the same day.
        try:
            from Core_Modules.salesman_state import get_account_scrape_throttle
            get_account_scrape_throttle().mark_scraped(source_accounts)
        except Exception as _mark_err:
            logger.debug("mark_scraped skipped: %s", _mark_err)

        logger.info(
            "📦 Apify returned %d reels for creator '%s'",
            actual_count, creator_name
        )

        # ── Stage 1: Metadata Filters (FREE) ─────────────────────────────────
        stage1_passed = []
        stage1_rejected = 0
        for item in items:
            approved, reason = _stage1_metadata_filter(item)
            if approved:
                stage1_passed.append(item)
            else:
                stage1_rejected += 1
                logger.info(
                    "🚫 [STAGE1] Dropped '%s' from @%s — %s",
                    item.get("shortcode", "?"),
                    item.get("ownerUsername", "?"),
                    reason,
                )

        logger.info(
            "📊 [STAGE1] %d/%d reels passed metadata filter (%d rejected)",
            len(stage1_passed), actual_count, stage1_rejected,
        )

        # ── Sort by timestamp — oldest or newest first ────────────────────────
        # APIFY_OLDEST_FIRST=yes → oldest posts first (archive fill / bottom-scrape)
        # Default: newest first (normal daily harvest)
        def _ts(item):
            ts = item.get("timestamp", "")
            return ts if ts else "0"

        stage1_passed = sorted(stage1_passed, key=_ts, reverse=not APIF_OLDEST_FIRST)
        if stage1_passed:
            _direction = "oldest" if APIF_OLDEST_FIRST else "newest"
            logger.info(
                "📅 Sorted %d reels by timestamp (%s first). Edge post: %s",
                len(stage1_passed), _direction,
                stage1_passed[0].get("timestamp", "unknown"),
            )

        # ── Stage 2: Gemini Thumbnail Pre-Screen ──────────────────────────────
        stage2_passed = []
        stage2_rejected = 0
        prescreen_calls = 0

        if PRESCREEN_ENABLED:
            logger.info(
                "🔍 [STAGE2] Gemini pre-screen enabled (budget: %d calls)", PRESCREEN_BUDGET
            )
            for item in stage1_passed:
                if prescreen_calls >= PRESCREEN_BUDGET:
                    # Budget exhausted — approve the rest (safe default)
                    logger.info(
                        "[STAGE2] Pre-screen budget (%d) exhausted — auto-approving remaining %d reels",
                        PRESCREEN_BUDGET, len(stage1_passed) - len(stage2_passed) - stage2_rejected,
                    )
                    stage2_passed.append(item)
                    continue

                approved, ps_result = gemini_reel_prescreen(item, creator_name)
                prescreen_calls += 1
                item["_prescreen"] = ps_result

                if approved:
                    stage2_passed.append(item)
                else:
                    stage2_rejected += 1
                    logger.info(
                        "🚫 [STAGE2] Dropped '%s' from @%s — %s",
                        item.get("shortcode", "?"),
                        item.get("ownerUsername", "?"),
                        ps_result.get("reject_reason", "unknown"),
                    )
        else:
            logger.info("[STAGE2] Gemini pre-screen disabled (APIFY_GEMINI_PRESCREEN=no)")
            stage2_passed = stage1_passed

        logger.info(
            "📊 [STAGE2] %d/%d reels passed pre-screen (%d rejected, %d Gemini calls)",
            len(stage2_passed), len(stage1_passed), stage2_rejected, prescreen_calls,
        )

        # ── Normalise — ensure every item has consistent keys ─────────────────
        normalised = []
        for item in stage2_passed:
            # Extract shortcode safely with fallback
            _sc = item.get("shortcode") or ""
            if not _sc:
                _url = item.get("url") or item.get("postUrl") or ""
                _match = re.search(r"instagram\.com/(?:reel|p|tv)/([A-Za-z0-9_\-]+)", _url)
                if _match:
                    _sc = _match.group(1)

            normalised.append({
                "shortcode":      _sc,
                "videoUrl":       item.get("videoUrl", ""),   # only real CDN URLs
                "caption":        item.get("caption") or item.get("text", ""),
                "hashtags":       item.get("hashtags", []),
                "taggedUsers":    item.get("taggedUsers", []),
                "ownerUsername":  item.get("ownerUsername") or item.get("username", ""),
                "url":            item.get("url", ""),
                "timestamp":      item.get("timestamp", ""),
                "likesCount":     item.get("likesCount", 0),
                "videoViewCount": item.get("videoViewCount", 0),
                "isPinned":       item.get("isPinned", False),
                "_creator_target": creator_name,
                "_prescreen":     item.get("_prescreen", {}),
            })

        # ── Stage 3: Deduplication — skip posts already scraped in prior runs ──
        # Uses disk-persisted ScrapedPostsRegistry in salesman_state.json.
        # Survives restarts. Prevents Apify from re-downloading and re-uploading
        # the same posts every harvest cycle.
        try:
            from Core_Modules.salesman_state import get_scraped_posts_registry
            _registry = get_scraped_posts_registry()

            # Layer avoid list check: also filter out using military-grade content_ledger.json
            try:
                from Content_Scraper_Modules.content_ledger import get_ledger
                _ledger = get_ledger()
                normalised = [item for item in normalised if not _ledger.is_in_avoid_list(item.get("shortcode"))]
            except Exception as _le:
                logger.debug("content_ledger query skipped: %s", _le)

            # filter_new() drops items whose shortcode is already in the registry
            deduped = _registry.filter_new(normalised)
            # Mark ALL returned shortcodes as seen NOW — even if the pipeline
            # later rejects them (quality / watermark / human reject).
            # This is intentional: we never want to re-download a post we've
            # already attempted, regardless of outcome.
            _new_shortcodes = [item["shortcode"] for item in deduped if item.get("shortcode")]
            if _new_shortcodes:
                _registry.mark_seen(_new_shortcodes)
        except Exception as _dedup_err:
            logger.warning(
                "⚠️ [DEDUP] Deduplication failed (non-fatal) — returning all posts: %s",
                _dedup_err
            )
            deduped = normalised

        logger.info(
            "📊 [STAGE3] %d/%d reels survived deduplication",
            len(deduped), len(normalised),
        )

        return deduped

    except Exception as exc:
        logger.error("💥 Apify account scrape failed for '%s': %s", creator_name, exc)
        return []


# Backward-compat alias — old callers using apify_scrape_actress_accounts still work
apify_scrape_actress_accounts = apify_scrape_creator_accounts
