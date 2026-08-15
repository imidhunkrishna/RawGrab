# ⚡ RawGrab - Multi-User Media Ingestion & Vault Engine (Phase 1)

**RawGrab** is a high-speed, serverless, multi-user media ingestion and stream extraction daemon built with Python and Telegram Bot API.

It handles high-throughput video/audio ingestion from Instagram, YouTube, TikTok, and direct URLs with zero duplicate upload bandwidth, instant `< 50ms` long-polling response times, and automated Telegram Cloud Vault indexing.

---

## 🔥 Key Features

- **🚀 Sub-50ms Long Polling Daemon**: Non-blocking asynchronous update processing over persistent Keep-Alive HTTP pools.
- **⚡ Single-Upload File ID Vault Dispatch**: Uploads raw media **ONCE** to Telegram Storage Group, capturing `file_id` for instant `< 0.05s` delivery to user chats with zero duplicate bandwidth.
- **🔒 Sequential 2-Step Onboarding**:
  - `Step 1/2`: Personal Unique Nickname (`/setnickname your_name`)
  - `Step 2/2`: Account Security Password (`/setpassword your_pass`)
- **⏱️ 24-Hour Rolling 5-Scrape Daily Quota**: Real-time 5th clip timestamp logging with 24-hour countdown timer & `/setapify` personal key support.
- **☁️ Serverless Cloud Vault Hydration**: Auto-pins `master_vault_index.json` to the top of Telegram Storage Group with zero-data-loss cloud recovery on daemon startup.
- **🛡️ Security Validator & Remux Sanitizer**: Remuxes media streams via FFmpeg (`+faststart`) to strip malicious atoms and trailing payloads.

---

## 🛠️ Installation & Setup

### 1. Prerequisites
- Python 3.10+
- FFmpeg on PATH

### 2. Environment Configuration
Create a `.env` file in the root directory:

```env
TELEGRAM_BOT_TOKEN="your_bot_token_here"
TELEGRAM_CHAT_ID="your_admin_chat_id_here"
TELEGRAM_STORAGE_GROUP_ID="-100xxxxxxxxx"
APIFY_API_TOKEN="your_shared_apify_token_here"
GEMINI_API_KEY="your_gemini_api_key_here"
```

### 3. Install Dependencies
```bash
python -m venv venv
venv\Scripts\activate  # On Windows
pip install -r Phase_1/Downloader_Modules/requirements.txt
```

### 4. Run the Daemon
```bash
python Phase_1/Downloader_Modules/telegram_listener.py
```

---

## 🏗️ Architecture

```
User Link Submission
       │
       ▼
RuFlow Router & 4FA Auth Check (telegram_user_manager.py)
       │
       ▼
Security Validation & Remux Sanitizer (security_validator.py)
       │
       ▼
Single-Upload to Telegram Storage Group (telegram_listener.py)
       │
       ▼
Instant file_id Share to User Chat (< 0.05s)
       │
       ▼
Background Indexing & Cloud Vault Sync (telegram_vault_indexer.py)
```

---

## 📄 License
MIT License
