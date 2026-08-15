"""
downloader_main.py — Standalone Phase 1 Ingestion & Downloader Orchestrator
=============================================================================
Orchestrates Phase 1 Video Ingestion & Downloader pipeline.

Supports 2 Standalone Ingestion Workers:
  1. Worker 1 (Automated Harvester):
     Scrapes target accounts listed in `Content_Scraper_Modules/source_accounts.json`
     via `apify_downloader.py` & `harvester.py`.
  2. Worker 2 (Manual Input Downloader):
     Ingests raw video clip directly from a specific URL or file path via `downloader.py`.

Usage:
    from Downloader_Modules.downloader_main import run_phase1_ingestion

    # Run Worker 1 (Automated Scraping)
    result = run_phase1_ingestion(mode="auto")

    # Run Worker 2 (Manual URL Download)
    result = run_phase1_ingestion(mode="manual", url="https://instagram.com/p/...")

CLI:
    python Downloader_Modules/downloader_main.py --mode auto
    python Downloader_Modules/downloader_main.py --url https://instagram.com/p/...
"""

import os
import sys
import json
import logging
import argparse
from typing import Dict, List, Optional, Any

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("downloader_main")

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import re

def _extract_shortcode(url: str) -> Optional[str]:
    """Extracts shortcode/ID from Instagram Reel, YouTube Short, TikTok, or generic URL."""
    if not url:
        return None
    m_ig = re.search(r"/(?:p|reel|reels)/([A-Za-z0-9_-]+)", url)
    if m_ig:
        return m_ig.group(1)
    m_yt = re.search(r"(?:shorts/|v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    if m_yt:
        return m_yt.group(1)
    m_tt = re.search(r"/video/(\d+)", url)
    if m_tt:
        return m_tt.group(1)
    return None


def run_phase1_ingestion(
    mode: str = "auto",
    url: Optional[str] = None,
    limit_per_account: Optional[int] = None,
    target_accounts: Optional[List[str]] = None,
    platform: str = "instagram"
) -> Dict[str, Any]:
    """
    Execute Phase 1 Ingestion Pipeline across multiple platforms.

    Args:
        mode:              "auto" (Worker 1: Automated Harvester) or "manual" (Worker 2: Manual URL)
        url:               Target video URL (required if mode="manual")
        limit_per_account: Max reels to download per account
        target_accounts:   Explicit list of creator handles / channel IDs to scrape
        platform:          "instagram", "youtube", "tiktok", or "direct"

    Returns:
        Dict containing downloaded video paths, count, and execution status.
    """
    if limit_per_account is None:
        try:
            limit_per_account = int(os.getenv("APIFY_REELS_PER_ACCOUNT", "11"))
        except (ValueError, TypeError):
            limit_per_account = 11

    logger.info(f"📥 [PHASE 1 INGESTION] Starting Phase 1 Orchestrator (mode='{mode}', platform='{platform}')...")
    downloads_dir = os.path.join(_REPO_ROOT, "downloads")
    os.makedirs(downloads_dir, exist_ok=True)

    downloaded_files = []

    # ── WORKER 2: Manual Input Downloader (Direct URL / Any Platform) ─────────
    if mode == "manual" or url:
        if not url:
            raise ValueError("Manual mode requires a valid video URL (--url).")

        logger.info(f"👤 [WORKER 2 - MANUAL] Ingesting single video from ({platform}): {url}")
        try:
            try:
                from .universal_downloader import ingest
            except ImportError:
                try:
                    from Phase_1.Downloader_Modules import ingest
                except ImportError:
                    from Tools.Downloader_Modules import ingest  # type: ignore
            shortcode = _extract_shortcode(url) or f"manual_{int(time.time())}"
            clip_folder_name = f"manual_{shortcode}"
            clip_dir = os.path.join(downloads_dir, clip_folder_name)
            os.makedirs(clip_dir, exist_ok=True)

            scan_res = ingest(url)
            out_file = scan_res.safe_path if scan_res.safe else None
            if out_file and isinstance(out_file, str) and os.path.exists(out_file):
                meta_path = os.path.join(clip_dir, "metadata.json")
                with open(meta_path, "w", encoding="utf-8") as mf:
                    json.dump({"shortcode": shortcode, "url": url, "ownerUsername": "manual", "platform": platform, "sha256": scan_res.sha256}, mf, indent=2)
                downloaded_files.append(os.path.abspath(out_file))
                logger.info(f"   ✓ [WORKER 2 SUCCESS] Saved media -> {out_file} & metadata.json")

                try:
                    try:
                        from Audio_Modules.audio_extractor import run_phase1_audio_analysis
                    except ImportError:
                        from Tools.Audio_Modules.audio_extractor import run_phase1_audio_analysis  # type: ignore
                    run_phase1_audio_analysis(out_file, clip_dir)
                except Exception as ae:
                    logger.warning(f"   ⚠ Audio analysis warning: {ae}")

            return {"success": len(downloaded_files) > 0, "mode": "manual", "downloaded_files": downloaded_files}
        except Exception as err:
            logger.error(f"❌ [WORKER 2 FAILED] Manual download error: {err}")
            return {"success": False, "mode": "manual", "downloaded_files": [], "error": str(err)}

    # ── WORKER 1: Automated Account Harvester ─────────────────────────────────
    else:
        logger.info(f"🤖 [WORKER 1 - AUTOMATED] Resolving target accounts for platform '{platform}'...")
        _phase_1_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        _the_jsons_path = os.path.join(_phase_1_root, "The jsons", "Downloader_moduels_json", "source_accounts.json")
        _local_path = os.path.join(os.path.dirname(__file__), "source_accounts.json")
        accounts_file = _the_jsons_path if os.path.exists(_the_jsons_path) else _local_path
        sources = []

        if target_accounts and isinstance(target_accounts, list):
            sources = [a.strip().lstrip("@") for a in target_accounts if a.strip()]
        elif os.path.exists(accounts_file):
            try:
                with open(accounts_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if isinstance(data, dict):
                    plat_list = data.get(platform.lower()) or data.get("source_accounts") or []
                    for item in plat_list:
                        if isinstance(item, str):
                            sources.append(item.lstrip("@"))
                        elif isinstance(item, dict) and "id" in item:
                            sources.append(str(item["id"]).lstrip("@"))
            except Exception as e:
                logger.warning(f"   ⚠ Failed to load source_accounts.json: {e}")

        # Auto-create or update clean platform-structured source_accounts.json
        try:
            acc_data = {"instagram": [], "youtube": [], "tiktok": []}
            if os.path.exists(accounts_file):
                try:
                    with open(accounts_file, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        acc_data.update(loaded)
                except Exception:
                    pass

            if sources:
                acc_data[platform.lower()] = sources

            with open(accounts_file, "w", encoding="utf-8") as f:
                json.dump(acc_data, f, indent=2)
            logger.info("✨ [AUTO-CREATE/UPDATE] Saved platform '%s' accounts into %s", platform, accounts_file)
        except Exception as _se:
            logger.warning(f"   ⚠ Could not update source_accounts.json: {_se}")

        if not sources:
            logger.error("❌ No target accounts configured in source_accounts.json. Specify --target handle1,handle2 or add handles to source_accounts.json")
            return {
                "success": False,
                "mode": "auto",
                "downloaded_files": [],
                "error": "No target accounts specified. Pass --target handle1,handle2 or add handles to source_accounts.json"
            }

        try:
            try:
                from .apify_downloader import apify_scrape_creator_accounts
                from .downloader import download_video
            except ImportError:
                try:
                    from Phase_1.Downloader_Modules.apify_downloader import apify_scrape_creator_accounts
                    from Phase_1.Downloader_Modules.downloader import download_video
                except ImportError:
                    from Tools.Downloader_Modules.apify_downloader import apify_scrape_creator_accounts  # type: ignore
                    from Tools.Downloader_Modules.downloader import download_video  # type: ignore
            approved_reels = apify_scrape_creator_accounts("General", sources, limit_per_account=limit_per_account)
            downloaded_files = []
            for item in (approved_reels or []):
                if isinstance(item, str) and os.path.exists(item):
                    downloaded_files.append(os.path.abspath(item))
                elif isinstance(item, dict):
                    shortcode = item.get("shortcode") or f"clip_{int(time.time())}"
                    owner = item.get("ownerUsername") or item.get("uploader") or "creator"
                    clip_folder_name = f"{owner}_{shortcode}"
                    clip_dir = os.path.join(downloads_dir, clip_folder_name)

                    # Quick disk-check: skip if already downloaded with video.mp4 & metadata.json
                    meta_path = os.path.join(clip_dir, "metadata.json")
                    video_file_check = os.path.join(clip_dir, "video.mp4")
                    if os.path.exists(meta_path) and os.path.exists(video_file_check):
                        logger.info(f"♻️ [DISK SKIP] Reel {clip_folder_name} already exists in downloads/")
                        downloaded_files.append(video_file_check)
                        continue

                    os.makedirs(clip_dir, exist_ok=True)
                    video_url = item.get("videoUrl") or item.get("url") or (f"https://www.instagram.com/reel/{shortcode}/" if shortcode else None)
                    if video_url:
                        try:
                            logger.info(f"⬇️ [WORKER 1] Downloading approved reel ({clip_folder_name}): {video_url}")
                            res = download_video(
                                video_url,
                                custom_title="video",
                                force_filename="video.mp4",
                                destination_dir=clip_dir
                            )
                            out_file = res[0] if isinstance(res, (tuple, list)) else res
                            if out_file and isinstance(out_file, str) and os.path.exists(out_file):
                                # Save full JSON metadata alongside video.mp4
                                with open(meta_path, "w", encoding="utf-8") as mf:
                                    json.dump(item, mf, indent=2, ensure_ascii=False)

                                downloaded_files.append(os.path.abspath(out_file))
                                logger.info(f"   ✓ [SAVED] {clip_folder_name}/video.mp4 & metadata.json")

                                try:
                                    from Tools.Audio_Modules.audio_extractor import run_phase1_audio_analysis
                                    logger.info(f"   🎵 [AUDIO] Running Phase 1 audio extraction + beat analysis...")
                                    run_phase1_audio_analysis(out_file, clip_dir)
                                except Exception as audio_err:
                                    logger.warning(f"   ⚠ Phase 1 audio analysis failed (non-fatal): {audio_err}")
                        except Exception as dl_err:
                            logger.warning(f"   ⚠ Failed to download reel {video_url}: {dl_err}")

            logger.info(f"   ✓ [WORKER 1 SUCCESS] Harvested {len(downloaded_files)} new clip(s) into downloads/")
        except Exception as harvest_err:
            logger.error(f"❌ [WORKER 1 FAILED] Automated harvest error: {harvest_err}")
            return {"success": False, "mode": "auto", "downloaded_files": [], "error": str(harvest_err)}

    return {
        "success": True,
        "mode": mode,
        "count": len(downloaded_files),
        "downloaded_files": downloaded_files,
        "downloads_dir": downloads_dir,
    }


# ── Instant Emergency Shutdown Handler (Ctrl + C) ───────────────────────────
import signal

def _emergency_shutdown(sig, frame):
    print("\n🛑 [EMERGENCY SHUTDOWN] Sudden user exit requested (Ctrl + C). Terminating process immediately...")
    os._exit(0)

signal.signal(signal.SIGINT, _emergency_shutdown)
if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, _emergency_shutdown)


# ── CLI Entrypoint ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 1 Ingestion Orchestrator (Worker 1 & Worker 2)")
    parser.add_argument("--mode", type=str, choices=["auto", "manual", "scheduled"], default="auto", help="Ingestion mode ('auto', 'manual', or 'scheduled')")
    parser.add_argument("--url", "-i", type=str, default=None, help="Target video URL for manual input mode")
    parser.add_argument("--target", "-t", type=str, default=None, help="Comma-separated target creator accounts / channel IDs for automated mode")
    parser.add_argument("--limit", type=int, default=None, help="Max reels per account for automated mode (defaults to APIFY_REELS_PER_ACCOUNT in .env)")

    args = parser.parse_args()
    if args.mode == "scheduled":
        from .scheduled_scraper_manager import start_scheduled_clock_daemon
        start_scheduled_clock_daemon()
        sys.exit(0)

    mode_to_use = "manual" if args.url else args.mode
    target_list = [a.strip() for a in args.target.split(",") if a.strip()] if args.target else None

    try:
        res = run_phase1_ingestion(mode=mode_to_use, url=args.url, limit_per_account=args.limit, target_accounts=target_list)
        if res.get("success"):
            print(f"\n🎉 PHASE 1 INGESTION COMPLETE: {res['count']} clip(s) ready in {res['downloads_dir']}")
        else:
            print(f"\n💥 INGESTION FAILED: {res.get('error')}")
            sys.exit(1)
    except (KeyboardInterrupt, SystemExit):
        _emergency_shutdown(None, None)
