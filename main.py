"""
RawGrab - Multi-User Media Ingestion & Vault Engine (Phase 1 Entry Point)
Author: imidhunkrishna (RawGrab v1.0)
"""
import os
import sys
from pathlib import Path

# Set Repository Root to sys.path
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load Environment Variables from root .env file
from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env", override=True)


def main():
    print("=" * 60)
    print(" ⚡ RawGrab - Multi-User Media Ingestion & Vault Engine")
    print(" 🚀 Launching Telegram Listener Daemon...")
    print("=" * 60)
    
    from Phase_1.Downloader_Modules.telegram_listener import start_listening_loop
    start_listening_loop()


if __name__ == "__main__":
    main()
