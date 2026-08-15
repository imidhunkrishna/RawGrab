"""
scheduled_scraper_manager.py — Max 2-Account Rotating Scheduled Scraper Manager
=================================================================================
Manages scheduled account rotation for source_accounts.json:
  - Selects max 2 accounts per scheduled batch via thread-safe SQLite pointer.
  - Updates source_accounts.json target list.
  - Executes Phase 1 Ingestion + Phase 2 AI Editing + Yields rendered reels one-by-one.
"""

import os
import sys
import json
import time
import logging
from typing import Dict, List, Optional, Any

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from .state_store import get_state_store
except ImportError:
    try:
        from Phase_1.Downloader_Modules.state_store import get_state_store
    except ImportError:
        from Tools.Downloader_Modules.state_store import get_state_store  # type: ignore

logger = logging.getLogger("scheduled_scraper_manager")

_PHASE_1_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
THE_JSONS_ACCOUNTS = os.path.join(_PHASE_1_ROOT, "The jsons", "Downloader_moduels_json", "source_accounts.json")
LOCAL_ACCOUNTS = os.path.join(os.path.dirname(__file__), "source_accounts.json")
ACCOUNTS_JSON = THE_JSONS_ACCOUNTS if os.path.exists(THE_JSONS_ACCOUNTS) else LOCAL_ACCOUNTS


def get_rotated_max_two_accounts(max_accounts: int = 2) -> List[str]:
    """
    Reads source_accounts.json, selects max_accounts (2) using round-robin index,
    and updates the active target list via thread-safe SQLite StateStore.
    """
    if not os.path.exists(ACCOUNTS_JSON):
        try:
            os.makedirs(os.path.dirname(ACCOUNTS_JSON), exist_ok=True)
            with open(ACCOUNTS_JSON, "w", encoding="utf-8") as f:
                json.dump({"instagram": [], "youtube": [], "tiktok": []}, f, indent=2)
            logger.info("✨ [AUTO-CREATE] Created missing %s", ACCOUNTS_JSON)
        except Exception as _ce:
            logger.warning("Failed to auto-create %s: %s", ACCOUNTS_JSON, _ce)
        return []

    try:
        with open(ACCOUNTS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)

        all_accounts = []
        if isinstance(data, dict):
            # Parse platform lists (instagram, youtube, tiktok) or source_accounts fallback
            for plat_key in ("instagram", "source_accounts", "youtube", "tiktok"):
                items = data.get(plat_key, [])
                if isinstance(items, list):
                    for item in items:
                        val = item.get("id") if isinstance(item, dict) else item
                        if val and val not in all_accounts:
                            all_accounts.append(str(val).lstrip("@"))
        elif isinstance(data, list):
            all_accounts = [str(x.get("id") if isinstance(x, dict) else x).lstrip("@") for x in data if x]

        if not all_accounts:
            logger.warning(f"⚠️ No accounts found in {ACCOUNTS_JSON}.")
            return []

        # Track rotation pointer via SQLite StateStore
        store = get_state_store()
        pointer, _last_selected = store.get_rotation_pointer("paparazzi")

        selected = []
        for i in range(min(max_accounts, len(all_accounts))):
            idx = (pointer + i) % len(all_accounts)
            selected.append(all_accounts[idx])

        # Save next pointer
        new_pointer = (pointer + len(selected)) % len(all_accounts)
        store.set_rotation_pointer("paparazzi", new_pointer, selected)

        logger.info(f"🔄 [SCHEDULED SCRAPER] Rotated account pool (max {max_accounts}): selected={selected}")
        return selected
    except Exception as e:
        logger.error(f"❌ Error rotating source accounts: {e}", exc_info=True)
        return []


def run_scheduled_scraper_batch(max_accounts: int = 2) -> List[str]:
    """
    Runs a scheduled batch with max 2 target accounts:
    1. Selects 2 target accounts.
    2. Executes Phase 1 Ingestion.
    3. Executes Phase 2 & 3 Master AI Editing.
    4. Returns list of rendered master reels.
    """
    target_accounts = get_rotated_max_two_accounts(max_accounts=max_accounts)
    logger.info(f"🚀 [SCHEDULED BATCH] Triggering scraper for accounts: {target_accounts}")

    try:
        from .downloader_main import run_phase1_ingestion
    except ImportError:
        try:
            from Phase_1.Downloader_Modules.downloader_main import run_phase1_ingestion
        except ImportError:
            from Tools.Downloader_Modules.downloader_main import run_phase1_ingestion  # type: ignore
    from Main_Modules.phase2_main import run_phase2_orchestration

    # Run ingestion for selected accounts
    ingest_res = run_phase1_ingestion(mode="auto", limit_per_account=3)
    if not ingest_res.get("success") or not ingest_res.get("downloaded_files"):
        logger.warning("⚠️ [SCHEDULED BATCH] Ingestion returned 0 new clips.")
        return []

    # Run AI Master Editor
    phase2_res = run_phase2_orchestration()
    rendered_reels = phase2_res.get("rendered_files", [])
    logger.info(f"🎬 [SCHEDULED BATCH COMPLETE] Rendered {len(rendered_reels)} reel(s).")
    return rendered_reels


def get_configured_schedule_times() -> List[str]:
    """
    Parses APIFY_SCRAPE_SCHEDULE_TIMES env variable (e.g. '02:30,14:30').
    Returns a list of 'HH:MM' string triggers.
    """
    raw = os.getenv("APIFY_SCRAPE_SCHEDULE_TIMES", "02:30,14:30").strip()
    times = [t.strip() for t in raw.split(",") if t.strip()]
    return times


def should_trigger_clock_scrape(current_time_str: Optional[str] = None) -> bool:
    """
    Checks if current local clock time (HH:MM) matches any scheduled trigger time.
    """
    now_str = current_time_str or time.strftime("%H:%M")
    sched_times = get_configured_schedule_times()
    return now_str in sched_times


def start_scheduled_clock_daemon(check_interval_sec: int = 30) -> None:
    """
    Runs a continuous background loop checking local clock against APIFY_SCRAPE_SCHEDULE_TIMES.
    Triggers batch scraping automatically at exact scheduled times (e.g. 02:30, 14:30).
    """
    logger.info("⏰ [CLOCK SCHEDULER DAEMON] Started. Trigger times: %s", get_configured_schedule_times())
    last_triggered_time = None

    while True:
        try:
            now_hhmm = time.strftime("%H:%M")
            if should_trigger_clock_scrape(now_hhmm) and last_triggered_time != now_hhmm:
                logger.info("🔔 [CLOCK TRIGGER MATCH] Scheduled time reached: %s — launching batch scrape...", now_hhmm)
                last_triggered_time = now_hhmm
                run_scheduled_scraper_batch()
            time.sleep(check_interval_sec)
        except KeyboardInterrupt:
            logger.info("🛑 [CLOCK SCHEDULER] Daemon stopped by user.")
            break
        except Exception as err:
            logger.error("💥 [CLOCK SCHEDULER] Exception in daemon loop: %s", err)
            time.sleep(check_interval_sec)
