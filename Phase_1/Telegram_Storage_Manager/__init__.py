"""
Telegram Storage Manager Package
"""
from .telegram_user_manager import (
    load_all_users,
    save_all_users,
    get_or_register_user,
    set_user_nickname,
    set_user_password,
    set_user_apify_token,
    get_user_apify_token,
    increment_user_scrape_count,
    verify_and_login_user,
    logout_user,
    generate_recovery_otp,
    reset_password_with_otp,
    handle_user_message,
    handle_user_callback,
    get_back_keyboard,
    get_nickname_keyboard,
    get_password_keyboard,
    get_authenticated_keyboard,
    USERS_JSON_PATH,
)

from .telegram_vault_indexer import (
    TelegramVaultIndexer,
    _empty_vault_index,
    MASTER_INDEX_FILE,
)

__all__ = [
    "load_all_users",
    "save_all_users",
    "get_or_register_user",
    "set_user_nickname",
    "set_user_password",
    "set_user_apify_token",
    "get_user_apify_token",
    "increment_user_scrape_count",
    "verify_and_login_user",
    "logout_user",
    "generate_recovery_otp",
    "reset_password_with_otp",
    "handle_user_message",
    "handle_user_callback",
    "get_back_keyboard",
    "get_nickname_keyboard",
    "get_password_keyboard",
    "get_authenticated_keyboard",
    "USERS_JSON_PATH",
    "TelegramVaultIndexer",
    "_empty_vault_index",
    "MASTER_INDEX_FILE",
]
