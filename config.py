import os

# ── Telegram ──────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

# ── Яндекс почта ─────────────────────────────────────────────
YANDEX_EMAIL        = os.getenv("YANDEX_EMAIL", "")
YANDEX_APP_PASSWORD = os.getenv("YANDEX_APP_PASSWORD", "")

# ── API ключи ─────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")

# ── Фильтр отправителей ───────────────────────────────────────
ALLOWED_DOMAINS = [
    "@p-krsk.ru",
    "@polymetal.ru",
    "@areal.ru",
]

# ── Папки для мониторинга ─────────────────────────────────────
# Названия папок точно как в Яндекс.Почте
MONITORED_FOLDERS = [
    "INBOX",           # Входящие
    "Снабжение",
    "Аметистовое",
    "Центральный офис",
    "Валунистый",
    "УК HG Москва",
    "Директора",
]

# ── Интервал проверки почты (секунды) ─────────────────────────
IMAP_CHECK_INTERVAL = 60
