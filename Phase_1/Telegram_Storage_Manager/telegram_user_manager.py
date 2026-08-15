"""
telegram_user_manager.py — Telegram User Session & Password Storage Manager
===========================================================================
Manages user registration, nickname setup, password creation, hashing,
session verification, OTP recovery, and persistent storage inside Phase_1/Telegram_Storage_Manager/.
"""

import os
import json
import secrets
import logging
import hashlib
from datetime import datetime
from typing import Dict, Optional, Tuple

logger = logging.getLogger("telegram_user_manager")

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
JSONS_DIR = os.path.join(_REPO_ROOT, "Phase_1", "The jsons", "Telegram_Storage_Manager_json")
STORAGE_DIR = JSONS_DIR
USERS_JSON_PATH = os.path.join(JSONS_DIR, "telegram_users.json")


def _hash_password(password: str) -> str:
    """Computes SHA-256 hash for secure password storage."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def load_all_users() -> Dict[str, Dict]:
    """Loads all registered user records from Phase_1/The jsons/Telegram_Storage_Manager_json/telegram_users.json."""
    if not os.path.exists(USERS_JSON_PATH):
        return {}
    try:
        with open(USERS_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as err:
        logger.error("Failed to load %s: %s", USERS_JSON_PATH, err)
        return {}


def sync_users_json_to_telegram_vault(upload_fn=None):
    """Uploads updated telegram_users.json to Storage Group & updates pinned master_vault_index.json."""
    try:
        if not upload_fn:
            try:
                from Phase_1.Downloader_Modules.telegram_listener import _send_file_multipart
                upload_fn = _send_file_multipart
            except Exception:
                upload_fn = None

        from Phase_1.Telegram_Storage_Manager.telegram_vault_indexer import TelegramVaultIndexer
        storage_group_id = os.getenv("TELEGRAM_STORAGE_GROUP_ID") or os.getenv("TELEGRAM_CHAT_ID")
        if storage_group_id and upload_fn and os.path.exists(USERS_JSON_PATH):
            res = upload_fn("sendDocument", storage_group_id, "document", USERS_JSON_PATH, caption=f"👤 **[VAULT BACKUP]** `telegram_users.json` (Updated {time.strftime('%H:%M:%S')})")
            if res and isinstance(res, dict):
                users_doc_id = res.get("document", {}).get("file_id")
                if users_doc_id:
                    indexer = TelegramVaultIndexer()
                    indexer.vault_index["telegram_users_file_id"] = users_doc_id
                    indexer._save_local_index()
                    indexer.upload_and_pin_vault_index_sync(upload_fn)
                    logger.info("✅ [USER VAULT BACKUP] Uploaded & PINNED updated telegram_users.json to Storage Group (file_id: %s)", users_doc_id[:15])
    except Exception as err:
        logger.warning("Notice uploading telegram_users.json to vault: %s", err)


def save_all_users(users: Dict[str, Dict], upload_fn=None, sync_to_vault: bool = True) -> bool:
    """Saves user records dictionary to telegram_users.json and syncs to Telegram Storage Group."""
    try:
        os.makedirs(JSONS_DIR, exist_ok=True)
        with open(USERS_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=2, ensure_ascii=False)
        
        if sync_to_vault:
            sync_users_json_to_telegram_vault(upload_fn=upload_fn)
        return True
    except Exception as err:
        logger.error("Failed to save %s: %s", USERS_JSON_PATH, err)
        return False


SESSION_TIMEOUT_HOURS = float(os.getenv("TELEGRAM_SESSION_TIMEOUT_HOURS", "168.0"))  # Default: 7 days (168 hours)


def _escape_md(text: str) -> str:
    """Escapes Markdown special characters (like underscores in usernames) to prevent parser errors."""
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


def check_and_expire_session(user_record: Dict) -> bool:
    """
    Checks if authenticated user session has expired due to inactivity (>7 days / 168 hours).
    Returns True if session was expired, False if still active.
    """
    if not user_record.get("authenticated", False):
        return False
    last_act_str = user_record.get("last_active", "")
    if last_act_str:
        try:
            last_act_dt = datetime.fromisoformat(last_act_str)
            inactive_hours = (datetime.now() - last_act_dt).total_seconds() / 3600.0
            if inactive_hours > SESSION_TIMEOUT_HOURS:
                user_record["authenticated"] = False
                user_id_str = str(user_record.get("user_id") or user_record.get("chat_id"))
                users = load_all_users()
                if user_id_str in users:
                    users[user_id_str]["authenticated"] = False
                    save_all_users(users)
                logger.info("🔒 [TELEGRAM USER MANAGER] Session expired due to inactivity (%.1f hrs) for User ID %s", inactive_hours, user_id_str)
                return True
        except Exception as _ex:
            logger.debug("Session parse error: %s", _ex)
    return False


def get_or_register_user(from_user: Dict, chat_id: str, admin_chat_id: Optional[str] = None) -> Dict:
    """
    Registers a new Telegram user record if not present, updates last active timestamp,
    and checks for 7-day session inactivity expiration.
    """
    users = load_all_users()
    user_id_str = str(from_user.get("id") or chat_id)

    if user_id_str not in users:
        is_admin = bool(admin_chat_id and (user_id_str == str(admin_chat_id) or chat_id == str(admin_chat_id)))
        users[user_id_str] = {
            "chat_id": str(chat_id),
            "user_id": from_user.get("id"),
            "first_name": from_user.get("first_name", "User"),
            "username": from_user.get("username", ""),
            "nickname": "",  # Set via /setnickname <nickname>
            "role": "admin" if is_admin else "user",
            "password_hash": "",
            "recovery_otp": "",
            "authenticated": False,  # Requires 1-time Nickname & Password setup
            "scrape_count": 0,
            "daily_scrape_count": 0,
            "quota_lock_timestamp": "",
            "apify_api_token": "",
            "joined_at": datetime.now().isoformat(),
            "last_active": datetime.now().isoformat()
        }
        save_all_users(users)
        logger.info("✨ [TELEGRAM USER MANAGER] Registered user: %s (@%s) [ID: %s]", from_user.get("first_name"), from_user.get("username"), user_id_str)
    else:
        # Check if session expired due to inactivity
        if users[user_id_str].get("password_hash") and users[user_id_str].get("nickname"):
            check_and_expire_session(users[user_id_str])
        else:
            users[user_id_str]["authenticated"] = False
        users[user_id_str].setdefault("scrape_count", 0)
        users[user_id_str].setdefault("daily_scrape_count", 0)
        users[user_id_str].setdefault("quota_lock_timestamp", "")
        users[user_id_str].setdefault("apify_api_token", "")
        users[user_id_str]["last_active"] = datetime.now().isoformat()
        if admin_chat_id and (user_id_str == str(admin_chat_id) or chat_id == str(admin_chat_id)):
            users[user_id_str]["role"] = "admin"
        save_all_users(users)

    return users[user_id_str]


def check_and_reset_daily_quota(user_record: Dict) -> Tuple[int, Optional[str], Optional[str]]:
    """
    Checks if 24 hours have elapsed since the 5th clip download timestamp.
    If 24h passed, auto-resets daily_scrape_count = 0.
    Returns (current_daily_count, countdown_str, reset_time_str).
    """
    lock_ts_str = user_record.get("quota_lock_timestamp", "")
    current_count = user_record.get("daily_scrape_count", 0)

    if not lock_ts_str:
        return current_count, None, None

    try:
        from datetime import datetime, timedelta
        lock_dt = datetime.fromisoformat(lock_ts_str)
        elapsed_sec = (datetime.now() - lock_dt).total_seconds()

        if elapsed_sec >= 86400.0:  # 24 hours = 86,400 seconds
            user_record["daily_scrape_count"] = 0
            user_record["quota_lock_timestamp"] = ""
            logger.info("⏳ [QUOTA RESET] 24 hours elapsed since 5th clip lock for user. Quota reset to 0/5!")
            return 0, None, None
        else:
            rem_sec = 86400.0 - elapsed_sec
            hours = int(rem_sec // 3600)
            mins = int((rem_sec % 3600) // 60)
            countdown_str = f"{hours}h {mins}m"
            reset_time_str = (lock_dt + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
            return current_count, countdown_str, reset_time_str
    except Exception as _e:
        logger.warning("Error computing daily quota reset: %s", _e)
        return current_count, None, None


def increment_user_scrape_count(user_id_str: str) -> int:
    """Increments the daily scrape count for a registered user, logging 5th clip lock timestamp."""
    users = load_all_users()
    user_id_str = str(user_id_str)
    if user_id_str in users:
        u_rec = users[user_id_str]
        check_and_reset_daily_quota(u_rec)
        new_cnt = u_rec.get("daily_scrape_count", 0) + 1
        u_rec["daily_scrape_count"] = new_cnt
        u_rec["scrape_count"] = u_rec.get("scrape_count", 0) + 1
        if new_cnt >= 5 and not u_rec.get("quota_lock_timestamp"):
            u_rec["quota_lock_timestamp"] = datetime.now().isoformat()
            logger.info("⏱️ [QUOTA LOCK 5th CLIP] User ID %s reached 5th clip at %s. 24h timer started!", user_id_str, u_rec["quota_lock_timestamp"])
        save_all_users(users)
        logger.info("📊 [TELEGRAM USER MANAGER] User ID %s daily scrape count incremented to %d/5", user_id_str, new_cnt)
        return new_cnt
    return 0


def set_user_apify_token(user_id_str: str, apify_token: str) -> bool:
    """Saves user personal Apify API token."""
    users = load_all_users()
    user_id_str = str(user_id_str)
    clean_token = apify_token.strip()
    if user_id_str in users and clean_token:
        users[user_id_str]["apify_api_token"] = clean_token
        save_all_users(users)
        logger.info("🔑 [TELEGRAM USER MANAGER] Personal Apify token saved for User ID %s", user_id_str)
        return True
    return False


def get_user_apify_token(user_id_str: str) -> Optional[str]:
    """Returns user personal Apify API token if present."""
    users = load_all_users()
    user_id_str = str(user_id_str)
    if user_id_str in users:
        return users[user_id_str].get("apify_api_token", "").strip() or None
    return None


def set_user_nickname(user_id_str: str, nickname_text: str) -> Tuple[bool, str]:
    """Sets personal unique nickname for a registered user, enforcing strict uniqueness across all accounts."""
    users = load_all_users()
    clean_nick = nickname_text.strip()
    clean_nick_lower = clean_nick.lower()
    
    if not clean_nick:
        return False, "⚠️ *Nickname Cannot Be Blank*\n\nUsage: `/setnickname your_nickname`"

    # Enforce strict unique nickname constraint across all registered users
    for uid, udata in users.items():
        if str(uid) != str(user_id_str):
            existing_nick = (udata.get("nickname") or "").strip().lower()
            if existing_nick and existing_nick == clean_nick_lower:
                logger.warning("⚠️ [TELEGRAM USER MANAGER] Duplicate nickname attempt '%s' rejected for User ID %s (owned by User ID %s)", clean_nick, user_id_str, uid)
                return False, f"❌ *Nickname Taken*\n\nNickname `'{clean_nick}'` is already registered to another account! Please choose a unique nickname."

    if str(user_id_str) in users:
        users[str(user_id_str)]["nickname"] = clean_nick
        save_all_users(users)
        logger.info("🏷️ [TELEGRAM USER MANAGER] Unique nickname set to '%s' for User ID %s", clean_nick, user_id_str)
        return True, f"✅ *Nickname Updated!* You will now be called *{_escape_md(clean_nick)}*."
    return False, "❌ User account not found."


def set_user_password(user_id_str: str, plain_password: str) -> bool:
    """Sets and hashes personal account password for a registered user."""
    users = load_all_users()
    if user_id_str in users and plain_password.strip():
        users[user_id_str]["password_hash"] = _hash_password(plain_password.strip())
        users[user_id_str]["authenticated"] = True
        save_all_users(users)
        logger.info("🔐 [TELEGRAM USER MANAGER] Password set and session authenticated for User ID %s", user_id_str)
        return True
    return False


def verify_and_login_user(user_id_str: str, plain_password: str) -> bool:
    """Verifies plain text password against stored hash and authenticates user session."""
    users = load_all_users()
    if user_id_str in users:
        stored_hash = users[user_id_str].get("password_hash", "")
        if stored_hash and stored_hash == _hash_password(plain_password.strip()):
            users[user_id_str]["authenticated"] = True
            save_all_users(users)
            logger.info("🔓 [TELEGRAM USER MANAGER] User ID %s logged in successfully", user_id_str)
            return True
    return False


from datetime import datetime, timedelta


def generate_recovery_otp(user_id_str: str, valid_minutes: int = 10) -> Optional[Tuple[str, int]]:
    """
    Generates a 6-digit secure single-use OTP with an expiration time window (default: 10 minutes).
    Returns (otp_code, valid_minutes) or None on failure.
    """
    users = load_all_users()
    if user_id_str in users:
        otp = f"OTP-{secrets.randbelow(900000) + 100000}"
        expires_dt = datetime.now() + timedelta(minutes=valid_minutes)
        users[user_id_str]["recovery_otp"] = otp
        users[user_id_str]["recovery_otp_expires"] = expires_dt.isoformat()
        save_all_users(users)
        logger.info("🔑 [TELEGRAM USER MANAGER] Generated recovery OTP for User ID %s (expires in %d min)", user_id_str, valid_minutes)
        return otp, valid_minutes
    return None


def reset_password_with_otp(user_id_str: str, otp_input: str, new_password: str) -> Tuple[bool, str]:
    """
    Verifies OTP code and expiration timestamp.
    Returns (success: bool, status_reason: str).
      - (True, "success")
      - (False, "expired")
      - (False, "invalid")
    """
    users = load_all_users()
    if user_id_str in users:
        stored_otp = users[user_id_str].get("recovery_otp", "")
        expires_str = users[user_id_str].get("recovery_otp_expires", "")

        if not stored_otp or stored_otp.strip() != otp_input.strip():
            return False, "invalid"

        # Check expiration timestamp
        if expires_str:
            try:
                expires_dt = datetime.fromisoformat(expires_str)
                if datetime.now() > expires_dt:
                    # Expired — clear OTP
                    users[user_id_str]["recovery_otp"] = ""
                    users[user_id_str]["recovery_otp_expires"] = ""
                    save_all_users(users)
                    logger.warning("⏳ [TELEGRAM USER MANAGER] Expired OTP attempt for User ID %s", user_id_str)
                    return False, "expired"
            except Exception as _te:
                logger.debug("OTP expiry parse notice: %s", _te)

        if new_password.strip():
            users[user_id_str]["password_hash"] = _hash_password(new_password.strip())
            users[user_id_str]["authenticated"] = True
            users[user_id_str]["recovery_otp"] = ""  # Burn single-use OTP
            users[user_id_str]["recovery_otp_expires"] = ""
            save_all_users(users)
            logger.info("🎉 [TELEGRAM USER MANAGER] Successfully reset password via OTP for User ID %s", user_id_str)
            return True, "success"

    return False, "invalid"


def logout_user(user_id_str: str) -> bool:
    """Logs out user session."""
    users = load_all_users()
    if user_id_str in users:
        users[user_id_str]["authenticated"] = False
        save_all_users(users)
        return True
    return False


# ── Interactive Keyboard Helpers ─────────────────────────────────────────────
def get_back_keyboard() -> Dict:
    """Returns an inline keyboard with a Back to Main Menu button."""
    return {
        "inline_keyboard": [
            [{"text": "🔙 Back to Main Menu", "callback_data": "action_main_menu"}]
        ]
    }


def get_nickname_keyboard() -> Dict:
    """Inline buttons for Nickname prompt screen (Back to Main Menu only)."""
    return get_back_keyboard()


def get_password_keyboard() -> Dict:
    """Inline buttons for Password prompt screen (Back to Main Menu only)."""
    return get_back_keyboard()


def get_authenticated_keyboard() -> Dict:
    """Inline buttons for Authenticated users."""
    return {
        "inline_keyboard": [
            [
                {"text": "🟢 Bot Status", "callback_data": "action_status"},
                {"text": "🔐 Change Password", "callback_data": "prompt_password"}
            ],
            [
                {"text": "🏷️ Change Nickname", "callback_data": "prompt_nickname"},
                {"text": "🔑 Forgot Password", "callback_data": "action_forgot"}
            ]
        ]
    }


# ── User Interaction & Onboarding Router ─────────────────────────────────────
def handle_user_callback(cb: Dict, send_msg_fn, answer_cb_fn):
    """Processes Inline Button clicks for user onboarding & account management."""
    cb_id = cb.get("id")
    cb_data = cb.get("data", "")
    from_user = cb.get("from", {})
    msg = cb.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))

    if cb_id and answer_cb_fn:
        answer_cb_fn(cb_id)

    storage_group_id = os.getenv("TELEGRAM_STORAGE_GROUP_ID", "").strip()
    chat_type = msg.get("chat", {}).get("type", "")
    if (storage_group_id and chat_id == storage_group_id) or chat_type in ["group", "supergroup", "channel"]:
        return

    user_record = get_or_register_user(from_user, chat_id)
    user_id_str = str(from_user.get("id") or chat_id)
    nickname = user_record.get("nickname", "").strip()
    raw_handle = f"@{user_record.get('username')}" if user_record.get('username') else f"ID: {user_id_str}"
    safe_handle = _escape_md(raw_handle)
    safe_nickname = _escape_md(nickname or 'User')

    if cb_data == "action_main_menu":
        send_msg_fn(
            chat_id,
            f"🤖 *Universal Media Ingestion Bot*\n\n"
            f"Hello *{safe_nickname}* ({safe_handle})!\n"
            f"Send me any *Video, Image, PDF/Comic, or Audio URL* to download and sanitize media!\n\n"
            f"• *Commands*:\n"
            f"  `/start` - Show help menu\n"
            f"  `/status` - Check bot health & account info\n"
            f"  `/setnickname your_name` - Change nickname\n"
            f"  `/setpassword your_password` - Change password\n"
            f"  `/logout` - Lock session\n",
            reply_markup=get_authenticated_keyboard()
        )

    elif cb_data == "prompt_nickname":
        users = load_all_users()
        if user_id_str in users:
            users[user_id_str]["prompt_state"] = "awaiting_nickname"
            save_all_users(users)
        send_msg_fn(
            chat_id,
            "🏷️ *Set Your Personal Nickname*\n\n"
            "👉 Reply directly with your name or send:\n"
            "`/setnickname your_nickname`",
            reply_markup=get_back_keyboard()
        )

    elif cb_data == "prompt_password":
        users = load_all_users()
        if user_id_str in users:
            users[user_id_str]["prompt_state"] = "awaiting_password"
            save_all_users(users)
        send_msg_fn(
            chat_id,
            "🔐 *Set Your Personal Account Password*\n\n"
            "👉 Reply directly with your password or send:\n"
            "`/setpassword your_password`",
            reply_markup=get_back_keyboard()
        )

    elif cb_data == "action_status":
        send_msg_fn(
            chat_id,
            f"🟢 *System Health*: Operational\n"
            f"👤 *Nickname*: `{safe_nickname}` ({safe_handle})\n"
            f"🆔 *User ID*: `{user_id_str}`\n"
            f"👑 *Role*: `{user_record.get('role', 'user')}`\n"
            f"🛡️ *Security Validator*: Active",
            reply_markup=get_authenticated_keyboard()
        )

    elif cb_data == "action_forgot":
        otp_res = generate_recovery_otp(user_id_str, valid_minutes=10)
        if otp_res:
            otp_code, valid_mins = otp_res
            send_msg_fn(
                chat_id,
                f"🔑 *Password Recovery Code Generated!*\n\n"
                f"Your single-use recovery code is:\n`{otp_code}`\n\n"
                f"⏱️ *Expires in*: `{valid_mins} minutes`\n\n"
                f"👉 Reset your password using:\n`/reset {otp_code} new_password`",
                reply_markup=get_back_keyboard()
            )


def handle_user_message(msg: Dict, send_msg_fn, admin_chat_id: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """
    Processes incoming text messages for user registration, nickname setup, password verification, & status.
    Returns (is_authenticated: bool, url_to_ingest: Optional[str]).
    """
    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", ""))
    from_user = msg.get("from", {})
    message_id = msg.get("message_id")
    text = (msg.get("text") or msg.get("caption") or "").strip()

    if not chat_id:
        return False, None

    storage_group_id = os.getenv("TELEGRAM_STORAGE_GROUP_ID", "").strip()
    chat_type = chat.get("type", "")
    if (storage_group_id and chat_id == storage_group_id) or chat_type in ["group", "supergroup", "channel"]:
        return False, None

    user_record = get_or_register_user(from_user, chat_id, admin_chat_id=admin_chat_id)
    user_id_str = str(from_user.get("id") or chat_id)
    raw_handle = f"@{user_record.get('username')}" if user_record.get('username') else f"ID: {user_id_str}"
    nickname = user_record.get("nickname", "").strip()
    display_name = nickname if nickname else raw_handle
    has_password = bool(user_record.get("password_hash"))
    safe_handle = _escape_md(raw_handle)
    safe_nickname = _escape_md(nickname or 'User')
    safe_display = _escape_md(display_name)

    logger.info("📩 [TELEGRAM USER MANAGER] User %s (%s, Chat %s): %s", display_name, user_id_str, chat_id, text[:80])

    # ── Command: /setnickname <nickname> ─────────────────────────────────────
    if text.startswith("/setnickname"):
        parts = text.split(maxsplit=1)
        nick_val = parts[1].strip() if len(parts) > 1 else ""
        if not nick_val:
            send_msg_fn(
                chat_id,
                "⚠️ *Nickname Cannot Be Blank*\n\nUsage: `/setnickname your_nickname`",
                reply_to_message_id=message_id,
                reply_markup=get_back_keyboard()
            )
            return False, None

        ok, nick_msg = set_user_nickname(user_id_str, nick_val)
        if not ok:
            send_msg_fn(chat_id, nick_msg, reply_to_message_id=message_id, reply_markup=get_back_keyboard())
        else:
            fresh_users = load_all_users()
            u_rec = fresh_users.get(user_id_str, {})
            if not u_rec.get("password_hash"):
                u_rec["prompt_state"] = "awaiting_password"
                save_all_users(fresh_users)
                send_msg_fn(
                    chat_id,
                    f"✅ *Nickname Updated to {_escape_md(nick_val)}!*\n\n"
                    f"🔐 *Account Security Setup (Step 2/2)*\n"
                    f"Now please set your personal account password to protect your user data & media downloads.\n\n"
                    f"👉 Reply with: `/setpassword your_password`",
                    reply_to_message_id=message_id,
                    reply_markup=get_back_keyboard()
                )
            else:
                send_msg_fn(chat_id, nick_msg, reply_to_message_id=message_id, reply_markup=get_authenticated_keyboard())
        return False, None

    # ── Command: /setpassword <password> ─────────────────────────────────────
    if text.startswith("/setpassword"):
        parts = text.split(maxsplit=1)
        pwd = parts[1].strip() if len(parts) > 1 else ""
        if not pwd:
            send_msg_fn(
                chat_id,
                "⚠️ *Password Cannot Be Blank*\n\nUsage: `/setpassword your_new_password`",
                reply_to_message_id=message_id,
                reply_markup=get_back_keyboard()
            )
            return False, None

        if set_user_password(user_id_str, pwd):
            send_msg_fn(
                chat_id,
                f"🎉 *Account Password Created & Secured!*\n\n"
                f"Welcome `{safe_display}`!\n"
                f"Your account is now protected and authenticated.\n\n"
                f"Send any video or media URL to start downloading!",
                reply_to_message_id=message_id,
                reply_markup=get_authenticated_keyboard()
            )
        return False, None

    # ── Command: /forgot or /reset (OTP Password Recovery) ───────────────────
    if text.startswith("/forgot"):
        otp_res = generate_recovery_otp(user_id_str, valid_minutes=10)
        if otp_res:
            otp_code, valid_mins = otp_res
            send_msg_fn(
                chat_id,
                f"🔑 *Password Recovery Code Generated!*\n\n"
                f"Your single-use recovery code is:\n`{otp_code}`\n\n"
                f"⏱️ *Expires in*: `{valid_mins} minutes`\n\n"
                f"👉 Reset your password using:\n`/reset {otp_code} new_password`",
                reply_to_message_id=message_id,
                reply_markup=get_back_keyboard()
            )
        return False, None

    if text.startswith("/reset"):
        parts = text.split()
        if len(parts) < 3:
            send_msg_fn(
                chat_id,
                "⚠️ *Usage*: `/reset OTP-CODE new_password`\n"
                "Example: `/reset OTP-123456 mynewpassword`",
                reply_to_message_id=message_id,
                reply_markup=get_back_keyboard()
            )
            return False, None
        otp_in = parts[1].strip()
        new_pwd = parts[2].strip()
        success, reason = reset_password_with_otp(user_id_str, otp_in, new_pwd)
        if success:
            send_msg_fn(
                chat_id,
                f"🎉 *Password Successfully Reset!*\n\nWelcome back *{safe_display}*! Session is now unlocked.",
                reply_to_message_id=message_id,
                reply_markup=get_authenticated_keyboard()
            )
        elif reason == "expired":
            send_msg_fn(
                chat_id,
                "⏳ *Recovery Code Expired!*\n\nThis OTP was valid for 10 minutes and has expired.\nSend `/forgot` to generate a fresh code.",
                reply_to_message_id=message_id,
                reply_markup=get_back_keyboard()
            )
        else:
            send_msg_fn(chat_id, "❌ *Invalid Recovery Code or Password.* Send `/forgot` to get a new code.", reply_to_message_id=message_id, reply_markup=get_back_keyboard())
        return False, None

    # ── Command: /login <password> ───────────────────────────────────────────
    if text.startswith("/login"):
        parts = text.split(maxsplit=1)
        pwd = parts[1].strip() if len(parts) > 1 else ""
        if verify_and_login_user(user_id_str, pwd):
            send_msg_fn(
                chat_id,
                f"🔓 *Login Successful!*\n\nWelcome back *{safe_display}*!\nSend any video or media URL to start downloading.",
                reply_to_message_id=message_id,
                reply_markup=get_authenticated_keyboard()
            )
        else:
            send_msg_fn(
                chat_id,
                "❌ *Incorrect Password.*\nUsage: `/login your_password`\nForgot password? Send `/forgot`",
                reply_to_message_id=message_id,
                reply_markup=get_back_keyboard()
            )
        return False, None

    # ── Command: /logout ─────────────────────────────────────────────────────
    if text.startswith("/logout"):
        logout_user(user_id_str)
        send_msg_fn(chat_id, "🔒 *Logged Out Successfully.* Send `/login your_password` to unlock session.", reply_to_message_id=message_id, reply_markup=get_back_keyboard())
        return False, None

    # ── Handle Active Prompt State (Direct text responses after button click) ──
    prompt_state = user_record.get("prompt_state", "")

    if prompt_state == "awaiting_nickname" and not text.startswith("/") and "http" not in text:
        ok, nick_msg = set_user_nickname(user_id_str, text)
        if not ok:
            send_msg_fn(chat_id, nick_msg, reply_to_message_id=message_id, reply_markup=get_back_keyboard())
        else:
            fresh_users = load_all_users()
            u_rec = fresh_users.get(user_id_str, {})
            if not u_rec.get("password_hash"):
                u_rec["prompt_state"] = "awaiting_password"
                save_all_users(fresh_users)
                send_msg_fn(
                    chat_id,
                    f"✅ *Nickname Updated to {_escape_md(text)}!*\n\n"
                    f"🔐 *Account Security Setup (Step 2/2)*\n"
                    f"Now please set your personal account password to protect your user data & media downloads.\n\n"
                    f"👉 Reply with: `/setpassword your_password`",
                    reply_to_message_id=message_id,
                    reply_markup=get_back_keyboard()
                )
            else:
                u_rec["prompt_state"] = ""
                save_all_users(fresh_users)
                send_msg_fn(chat_id, nick_msg, reply_to_message_id=message_id, reply_markup=get_authenticated_keyboard())
        return False, None

    if prompt_state == "awaiting_password" and not text.startswith("/") and "http" not in text:
        set_user_password(user_id_str, text)
        user_record["prompt_state"] = ""
        save_all_users(load_all_users())
        send_msg_fn(
            chat_id,
            f"🎉 *Account Password Created & Secured!*\n\n"
            f"Welcome `{safe_display}`!\n"
            f"Your account is now protected and authenticated.\n\n"
            f"Send any video or media URL to start downloading!",
            reply_to_message_id=message_id,
            reply_markup=get_authenticated_keyboard()
        )
        return False, None

    # STAGE 3: Session Locked / 1-Time Onboarding Check
    if not user_record.get("nickname"):
        send_msg_fn(
            chat_id,
            f"🏷️ *Account Setup Required (Step 1/2)*\n\n"
            f"Welcome! Please set a personal unique nickname before downloading media.\n\n"
            f"👉 Reply with: `/setnickname your_name`",
            reply_to_message_id=message_id,
            reply_markup=get_back_keyboard()
        )
        return False, None

    if not user_record.get("password_hash"):
        send_msg_fn(
            chat_id,
            f"🔐 *Account Security Setup Required (Step 2/2)*\n\n"
            f"Hello `{safe_nickname}`! Please set your account password to protect your user data & media downloads.\n\n"
            f"👉 Reply with: `/setpassword your_password`",
            reply_to_message_id=message_id,
            reply_markup=get_back_keyboard()
        )
        return False, None

    if not user_record.get("authenticated", False):
        if verify_and_login_user(user_id_str, text):
            send_msg_fn(
                chat_id,
                f"🔓 *Login Successful!*\n\nWelcome back *{safe_nickname}*!\nSend any video or media URL to start downloading.",
                reply_to_message_id=message_id,
                reply_markup=get_authenticated_keyboard()
            )
            return False, None

        send_msg_fn(
            chat_id,
            f"🔒 *Session Locked for {safe_nickname}*\n\n"
            f"Please log in to your account:\n`/login your_password`\nForgot password? Send `/forgot`",
            reply_to_message_id=message_id,
            reply_markup=get_back_keyboard()
        )
        return False, None

    # ── Command: /setapify <token> ───────────────────────────────────────────
    if text.startswith("/setapify") or text.startswith("apify_api_"):
        parts = text.split(maxsplit=1)
        token_val = parts[1].strip() if text.startswith("/setapify") and len(parts) > 1 else text.strip()
        if not token_val.startswith("apify_api_") and len(token_val) < 20:
            send_msg_fn(
                chat_id,
                "⚠️ *Invalid Apify API Token*\n\nYour token should start with `apify_api_`.\n\nGet your token here: https://console.apify.com/settings/integrations",
                reply_to_message_id=message_id,
                reply_markup=get_back_keyboard()
            )
            return False, None
        set_user_apify_token(user_id_str, token_val)
        send_msg_fn(
            chat_id,
            f"🎉 *Apify API Token Saved & Verified!*\n\n"
            f"Unlimited free media downloads are now unlocked for `{safe_display}`!\n\n"
            f"Send any video or media URL to start downloading.",
            reply_to_message_id=message_id,
            reply_markup=get_authenticated_keyboard()
        )
        return False, None

    # ── Commands for Authenticated Users ─────────────────────────────────────
    if text.startswith("/start") or text.startswith("/help"):
        send_msg_fn(
            chat_id,
            f"🤖 *Universal Media Ingestion Bot*\n\n"
            f"Hello *{safe_nickname}* ({safe_handle})!\n"
            f"Send me any *Video, Image, PDF/Comic, or Audio URL* to download and sanitize media!\n\n"
            f"• *Commands*:\n"
            f"  `/start` - Show help menu\n"
            f"  `/status` - Check bot health, scrape count & account info\n"
            f"  `/setnickname your_name` - Change nickname\n"
            f"  `/setpassword your_password` - Change password\n"
            f"  `/setapify apify_api_token` - Add personal Apify key\n"
            f"  `/logout` - Lock session\n",
            reply_to_message_id=message_id,
            reply_markup=get_authenticated_keyboard()
        )
        return False, None

    elif text.startswith("/status"):
        daily_cnt, countdown_str, reset_time_str = check_and_reset_daily_quota(user_record)
        timer_info = f" (Resets in {countdown_str})" if countdown_str else ""
        send_msg_fn(
            chat_id,
            f"🟢 *System Health*: Operational\n"
            f"👤 *Nickname*: `{safe_nickname}` ({safe_handle})\n"
            f"🆔 *User ID*: `{user_id_str}`\n"
            f"📊 *Daily Free Scrapes*: `{daily_cnt} / 5`{timer_info}\n"
            f"🔑 *Personal Apify Token*: `{'Connected ✅' if user_record.get('apify_api_token') else 'Not Set (Using Shared Pool)'}`\n"
            f"👑 *Role*: `{user_record.get('role', 'user')}`\n"
            f"🛡️ *Security Validator*: Active (Whitelist/Greylist/Blacklist + Remux Sanitization)",
            reply_to_message_id=message_id,
            reply_markup=get_authenticated_keyboard()
        )
        return False, None

    # ── 24-Hour Rolling 5 Free Scrapes Limit Enforcement ────────────────────
    daily_cnt, countdown_str, reset_time_str = check_and_reset_daily_quota(user_record)
    save_all_users(load_all_users())

    is_admin = (user_record.get("role") == "admin")
    has_apify_token = bool(user_record.get("apify_api_token"))

    # Extract URLs for ingestion
    urls = [word for word in text.split() if word.startswith("http://") or word.startswith("https://")]

    if urls and not is_admin and daily_cnt >= 5 and not has_apify_token:
        timer_line = f"⏱️ **Next 5 Free Scrapes Release In**: `{countdown_str}`\n*(Resets at: {reset_time_str})*\n\n" if countdown_str else ""
        send_msg_fn(
            chat_id,
            f"⏳ *Daily Free Downloads Limit Reached (5/5)*\n\n"
            f"Hello `{safe_nickname}`! You have used your **5 free daily downloads**.\n\n"
            f"{timer_line}"
            f"💡 **Want to bypass the timer?** Connect your free personal **Apify API Key** to get **unlimited downloads immediately**:\n\n"
            f"1️⃣ Open Apify Integrations: https://console.apify.com/settings/integrations\n"
            f"2️⃣ Copy the **Default API token** created on sign up.\n"
            f"3️⃣ Send your token in this chat:\n"
            f"   `/setapify apify_api_your_token`\n"
            f"   *(or paste your apify_api_... token here)*",
            reply_to_message_id=message_id,
            reply_markup=get_back_keyboard()
        )
        return False, None

    if not urls:
        send_msg_fn(chat_id, "ℹ️ Send a valid HTTP(S) URL or media link to ingest.", reply_to_message_id=message_id, reply_markup=get_authenticated_keyboard())
        return False, None

    return True, urls[0]
