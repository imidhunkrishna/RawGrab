"""
telegram_storage_main.py — Standalone Telegram Storage Manager Runner
=====================================================================
Executes Telegram Storage Manager as an independent standalone service.
Supports viewing active users, managing storage groups, pinned index entries,
and inspecting master vault index columns.
"""

import sys
import os
import argparse
import json

# Ensure UTF-8 output formatting on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_STORAGE_DIR = os.path.dirname(os.path.abspath(__file__))
if _STORAGE_DIR not in sys.path:
    sys.path.insert(0, _STORAGE_DIR)

from telegram_user_manager import load_all_users
from telegram_vault_indexer import TelegramVaultIndexer


def main():
    parser = argparse.ArgumentParser(description="Standalone Telegram Storage Manager Runner")
    parser.add_argument("--users", action="store_true", help="List all registered user accounts")
    parser.add_argument("--catalog", action="store_true", help="List all indexed Column 1 & Column 2 vault items")
    parser.add_argument("--reels", action="store_true", help="List Column 1 master reel entries")
    parser.add_argument("--sources", action="store_true", help="List Column 2 raw source entries")

    args = parser.parse_args()

    print("\n📦 [TELEGRAM STORAGE MANAGER — STANDALONE VAULT RUNNER]")
    print("=======================================================")

    indexer = TelegramVaultIndexer()
    vault = indexer.vault_index

    if args.users:
        users = load_all_users()
        print(f"\n👤 Registered Users ({len(users)} total):")
        print(json.dumps(users, indent=2, ensure_ascii=False))

    elif args.catalog:
        print(f"\n📄 Master Vault Index (Updated: {vault.get('updated_at')}):")
        print(json.dumps(vault, indent=2, ensure_ascii=False))

    elif args.reels:
        reels = vault.get("column_1_processed_reels", {}).get("by_session_id", {})
        print(f"\n🎬 Column 1 Master Reels ({len(reels)} total):")
        print(json.dumps(reels, indent=2, ensure_ascii=False))

    elif args.sources:
        sources = vault.get("column_2_downloaded_sources", {}).get("by_social_media_id", {})
        print(f"\n📥 Column 2 Downloaded Sources ({len(sources)} total):")
        print(json.dumps(sources, indent=2, ensure_ascii=False))

    else:
        users = load_all_users()
        reels = vault.get("column_1_processed_reels", {}).get("by_session_id", {})
        sources = vault.get("column_2_downloaded_sources", {}).get("by_social_media_id", {})
        print(f"\n📊 Summary Stats:")
        print(f"  • Registered Users: {len(users)}")
        print(f"  • Column 1 Master Reels: {len(reels)}")
        print(f"  • Column 2 Downloaded Sources: {len(sources)}")
        print(f"  • Pinned Message ID: {vault.get('pinned_message_id', 'None')}")
        print("\nTip: Run with --help to see all standalone commands!\n")


if __name__ == "__main__":
    main()
