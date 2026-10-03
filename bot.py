import asyncio
import html
import logging
import os
import secrets
import time
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup

load_dotenv()

# =========================
# Logging
# =========================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("iphone_giveaway_bot")

# =========================
# Environment / constants
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()
CHANNEL_URL_ENV = os.getenv("CHANNEL_URL", "").strip()
CARD_NUMBER_ENV = os.getenv("CARD_NUMBER", "").strip()
SUPPORT_USERNAME_ENV = os.getenv("SUPPORT_USERNAME", "").strip()
COOPERATION_USERNAME_ENV = os.getenv("COOPERATION_USERNAME", "").strip()
TICKET_PRICE_ENV = os.getenv("TICKET_PRICE", "300").strip()
TOTAL_TICKETS_ENV = os.getenv("TOTAL_TICKETS", "70").strip()
REFERRALS_FOR_FREE_TICKET_ENV = os.getenv("REFERRALS_FOR_FREE_TICKET", "10").strip()
RESERVATION_MINUTES_ENV = os.getenv("RESERVATION_MINUTES", "30").strip()
PORT_ENV = os.getenv("PORT", "8080").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "/app/data/bot.db").strip()


def parse_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


ADMIN_IDS = {
    int(value.strip())
    for value in ADMIN_IDS_RAW.replace(";", ",").split(",")
    if value.strip().lstrip("-").isdigit()
}
CHANNEL_ID: int | str
if CHANNEL_ID_RAW.lstrip("-").isdigit():
    CHANNEL_ID = int(CHANNEL_ID_RAW)
else:
    CHANNEL_ID = CHANNEL_ID_RAW

ENV_TICKET_PRICE = max(1, parse_int(TICKET_PRICE_ENV, 300))
ENV_TOTAL_TICKETS = max(1, parse_int(TOTAL_TICKETS_ENV, 70))
ENV_REFERRALS_FOR_FREE = max(1, parse_int(REFERRALS_FOR_FREE_TICKET_ENV, 10))
ENV_RESERVATION_MINUTES = max(1, parse_int(RESERVATION_MINUTES_ENV, 30))
PORT = max(1, parse_int(PORT_ENV, 8080))

DB: aiosqlite.Connection | None = None
DB_LOCK = asyncio.Lock()
BOT_USERNAME = ""
BOT: Bot | None = None

# Simple per-process throttling. The app is intentionally designed for one instance.
RATE_LIMITS: dict[str, float] = {}


class PurchaseStates(StatesGroup):
    waiting_receipt = State()
    gift_mode = State()


class AdminInputStates(StatesGroup):
    waiting_value = State()


class BroadcastStates(StatesGroup):
    waiting_content = State()
    waiting_button_choice = State()
    waiting_button_text = State()
    waiting_button_url = State()
    waiting_confirmation = State()


class UserSearchStates(StatesGroup):
    waiting_query = State()


DEFAULT_RULES = """📋 <b>ПРАВИЛА РОЗІГРАШУ</b>

🎁 <b>Приз:</b> iPhone 15 Pro.

🎟 <b>Кількість квитків:</b> 70.
💰 <b>Вартість одного квитка:</b> 300 грн.

<b>Як взяти участь:</b>
1. Підпишіться на канал розіграшу.
2. Натисніть «🎟 Купити квиток» та оберіть вільний номер або декілька номерів.
3. Оплатіть вказану суму за реквізитами, які відображає бот.
4. Надішліть фото квитанції одним повідомленням.
5. Дочекайтеся підтвердження адміністратора.

<b>Резервування:</b>
Обрані платні номери тимчасово резервуються. Якщо квитанцію не надіслано в межах встановленого часу резерву, номери автоматично повертаються у продаж.

<b>Підтвердження:</b>
Квиток вважається оплаченим після перевірки квитанції адміністратором. Після підтвердження номер більше не продається іншому учаснику.

<b>Реферальна програма:</b>
За кожних 10 валідних запрошених користувачів можна отримати 1 безкоштовний квиток. Запрошення зараховується один раз для одного Telegram ID після реального запуску бота та підтвердження підписки. Самореферали та повторне зарахування не допускаються.

<b>Визначення переможця:</b>
Після закриття продажу переможець визначається випадково серед усіх 70 зайнятих номерів, включно з безкоштовними квитками. Результат фіксується в базі та не змінюється повторним запуском.

🏁 Після того як усі номери будуть розподілені, продаж закривається та адміністрація визначає переможця.

💬 Підтримка: {support}
🤝 Співпраця: {cooperation}

ℹ️ Умови та порядок проведення можуть бути змінені адміністрацією до моменту визначення переможця. Учасникам варто самостійно ознайомитися з вимогами законодавства, які можуть застосовуватися до платних розіграшів у їхній юрисдикції."""

DEFAULT_ABOUT = """🏆 <b>ПРО РОЗІГРАШ</b>

📱 Приз: <b>iPhone 15 Pro</b>
🎟 Квитків: <b>70</b>
💰 Ціна квитка: <b>300 грн</b>

Загальна максимальна сума оплат за 70 платних квитків — <b>21 000 грн</b>.

Переможець визначається сервером випадковим вибором серед усіх зайнятих номерів після завершення продажу."""


# =========================
# Generic helpers
# =========================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def display_user(username: str | None, first_name: str | None, telegram_id: int) -> str:
    if username:
        return f"@{esc(username)}"
    if first_name:
        return esc(first_name)
    return f"<code>{telegram_id}</code>"


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def throttled(key: str, seconds: float = 0.7) -> bool:
    current = time.monotonic()
    previous = RATE_LIMITS.get(key, 0.0)
    if current - previous < seconds:
        return True
    RATE_LIMITS[key] = current
    if len(RATE_LIMITS) > 10000:
        cutoff = current - 300
        for item in list(RATE_LIMITS):
            if RATE_LIMITS[item] < cutoff:
                RATE_LIMITS.pop(item, None)
    return False


def normalize_username(value: str) -> str:
    return value.strip().lstrip("@").lower()


def format_money(amount: int) -> str:
    return f"{amount:,}".replace(",", " ") + " грн"


def status_icon(status: str) -> str:
    return {
        "FREE": "🟢",
        "RESERVED": "🟡",
        "PENDING_PAYMENT": "🟡",
        "SOLD": "🔴",
        "FREE_GIFT": "🎁",
    }.get(status, "⚪")


def short_status(status: str) -> str:
    return {
        "FREE": "Вільний",
        "RESERVED": "Заброньований",
        "PENDING_PAYMENT": "Очікує оплати",
        "SOLD": "Проданий",
        "FREE_GIFT": "Безкоштовний",
    }.get(status, status)


def payment_status_text(status: str) -> str:
    return {
        "PENDING": "🕐 Очікує",
        "CONFIRMED": "✅ Підтверджена",
        "REJECTED": "❌ Відхилена",
        "EXPIRED": "⌛ Прострочена",
    }.get(status, status)


async def require_db() -> aiosqlite.Connection:
    if DB is None:
        raise RuntimeError("Database is not initialized")
    return DB


async def db_fetchone(sql: str, params: tuple = ()) -> aiosqlite.Row | None:
    db = await require_db()
    async with DB_LOCK:
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return row


async def db_fetchall(sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
    db = await require_db()
    async with DB_LOCK:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return rows


async def db_execute(sql: str, params: tuple = ()) -> int:
    db = await require_db()
    async with DB_LOCK:
        cursor = await db.execute(sql, params)
        rowcount = cursor.rowcount
        await cursor.close()
        await db.commit()
        return rowcount


async def get_setting(key: str, default: str = "") -> str:
    row = await db_fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    return str(row["value"]) if row else default


async def get_int_setting(key: str, default: int) -> int:
    return max(0, parse_int(await get_setting(key, str(default)), default))


async def set_setting(key: str, value: str) -> None:
    db = await require_db()
    async with DB_LOCK:
        await db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def log_admin(admin_id: int, action: str, details: str = "") -> None:
    db = await require_db()
    async with DB_LOCK:
        await db.execute(
            "INSERT INTO admin_logs(admin_id, action, details, created_at) VALUES(?, ?, ?, ?)",
            (admin_id, action, details, iso(now_utc())),
        )
        await db.commit()


# =========================
# Database
# =========================
async def init_db() -> None:
    global DB
    os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
    DB = await aiosqlite.connect(DATABASE_PATH)
    DB.row_factory = aiosqlite.Row
    await DB.execute("PRAGMA foreign_keys = ON")
    await DB.execute("PRAGMA journal_mode = WAL")
    await DB.execute("PRAGMA synchronous = NORMAL")
    await DB.execute("PRAGMA busy_timeout = 10000")

    async with DB_LOCK:
        await DB.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                referrer_id INTEGER,
                referral_count INTEGER NOT NULL DEFAULT 0,
                free_tickets INTEGER NOT NULL DEFAULT 0,
                free_awards_granted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(referrer_id) REFERENCES users(telegram_id)
            );

            CREATE TABLE IF NOT EXISTS tickets (
                number INTEGER PRIMARY KEY,
                user_id INTEGER,
                status TEXT NOT NULL DEFAULT 'FREE',
                payment_id INTEGER,
                reserved_until TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(telegram_id)
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                ticket_numbers TEXT NOT NULL,
                receipt_file_id TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                confirmed_by INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(telegram_id)
            );

            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                valid INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                validated_at TEXT,
                FOREIGN KEY(referrer_id) REFERENCES users(telegram_id),
                FOREIGN KEY(referred_id) REFERENCES users(telegram_id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id);
            CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
            CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
            CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            """
        )

        defaults = {
            "channel_url": CHANNEL_URL_ENV,
            "card_number": CARD_NUMBER_ENV,
            "support_username": SUPPORT_USERNAME_ENV,
            "cooperation_username": COOPERATION_USERNAME_ENV,
            "ticket_price": str(ENV_TICKET_PRICE),
            "total_tickets": str(ENV_TOTAL_TICKETS),
            "referrals_for_free_ticket": str(ENV_REFERRALS_FOR_FREE),
            "reservation_minutes": str(ENV_RESERVATION_MINUTES),
            "rules_text": DEFAULT_RULES,
            "about_text": DEFAULT_ABOUT,
            "sales_closed": "0",
            "winner_ticket_number": "",
            "winner_user_id": "",
            "winner_published_at": "",
        }
        for key, value in defaults.items():
            await DB.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
                (key, value),
            )

        total = await _setting_total_unlocked()
        cursor = await DB.execute("SELECT COALESCE(MAX(number), 0) AS max_number FROM tickets")
        row = await cursor.fetchone()
        await cursor.close()
        max_number = int(row["max_number"] or 0)
        effective_total = max(total, max_number)
        if effective_total != total:
            await DB.execute(
                "UPDATE settings SET value = ? WHERE key = 'total_tickets'",
                (str(effective_total),),
            )
            total = effective_total

        created = iso(now_utc())
        for number in range(max_number + 1, total + 1):
            await DB.execute(
                "INSERT INTO tickets(number, status, created_at) VALUES(?, 'FREE', ?)",
                (number, created),
            )
        await DB.commit()


async def _setting_total_unlocked() -> int:
    cursor = await DB.execute("SELECT value FROM settings WHERE key = 'total_tickets'")  # type: ignore[union-attr]
    row = await cursor.fetchone()
    await cursor.close()
    return max(1, parse_int(str(row["value"]), ENV_TOTAL_TICKETS) if row else ENV_TOTAL_TICKETS)


async def close_db() -> None:
    global DB
    if DB is not None:
        with suppress(Exception):
            await DB.close()
        DB = None


# =========================
# User / referral helpers
# =========================
async def ensure_user(tg_user, start_param: str | None = None) -> aiosqlite.Row:
    db = await require_db()
    telegram_id = int(tg_user.id)
    username = tg_user.username or None
    first_name = tg_user.first_name or ""
    async with DB_LOCK:
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row:
            await db.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE telegram_id = ?",
                (username, first_name, telegram_id),
            )
            await db.commit()
            cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            refreshed = await cursor.fetchone()
            await cursor.close()
            return refreshed

        referrer_id: int | None = None
        if start_param and start_param.isdigit():
            candidate = int(start_param)
            if candidate != telegram_id:
                cursor = await db.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (candidate,))
                exists = await cursor.fetchone()
                await cursor.close()
                if exists:
                    referrer_id = candidate

        created_at = iso(now_utc())
        await db.execute(
            "INSERT INTO users(telegram_id, username, first_name, referrer_id, created_at) VALUES(?, ?, ?, ?, ?)",
            (telegram_id, username, first_name, referrer_id, created_at),
        )
        if referrer_id is not None:
            await db.execute(
                "INSERT OR IGNORE INTO referrals(referrer_id, referred_id, valid, created_at) VALUES(?, ?, 0, ?)",
                (referrer_id, telegram_id, created_at),
            )
        await db.commit()
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        created_row = await cursor.fetchone()
        await cursor.close()
        return created_row


async def is_subscribed(user_id: int) -> bool:
    if not CHANNEL_ID_RAW:
        return False
    if BOT is None:
        return False
    try:
        member = await BOT.get_chat_member(CHANNEL_ID, user_id)
        if member.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}:
            return True
        if member.status == ChatMemberStatus.RESTRICTED:
            return bool(member.is_member)
        return False
    except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError) as exc:
        logger.warning("Subscription check failed for %s: %s", user_id, exc)
        return False


async def validate_referral_if_possible(user_id: int) -> tuple[bool, int | None]:
    if not await is_subscribed(user_id):
        return False, None
    db = await require_db()
    async with DB_LOCK:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT id, referrer_id, valid FROM referrals WHERE referred_id = ? LIMIT 1",
                (user_id,),
            )
            referral = await cursor.fetchone()
            await cursor.close()
            if not referral or int(referral["valid"]) == 1:
                await db.commit()
                return False, None

            referrer_id = int(referral["referrer_id"])
            await db.execute(
                "UPDATE referrals SET valid = 1, validated_at = ? WHERE id = ? AND valid = 0",
                (iso(now_utc()), int(referral["id"])),
            )
            await db.execute(
                "UPDATE users SET referral_count = referral_count + 1 WHERE telegram_id = ?",
                (referrer_id,),
            )

            cursor = await db.execute(
                "SELECT referral_count, free_awards_granted, free_tickets FROM users WHERE telegram_id = ?",
                (referrer_id,),
            )
            referrer = await cursor.fetchone()
            await cursor.close()
            if referrer:
                referrals_for_gift = await _setting_int_unlocked(db, "referrals_for_free_ticket", ENV_REFERRALS_FOR_FREE)
                earned = int(referrer["referral_count"]) // referrals_for_gift
                granted = int(referrer["free_awards_granted"])
                if earned > granted:
                    delta = earned - granted
                    await db.execute(
                        "UPDATE users SET free_awards_granted = ?, free_tickets = free_tickets + ? WHERE telegram_id = ?",
                        (earned, delta, referrer_id),
                    )
            await db.commit()
            return True, referrer_id
        except Exception:
            await db.rollback()
            raise


async def _setting_int_unlocked(db: aiosqlite.Connection, key: str, default: int) -> int:
    cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cursor.fetchone()
    await cursor.close()
    return max(0, parse_int(str(row["value"]), default) if row else default)


async def get_user(user_id: int) -> aiosqlite.Row | None:
    return await db_fetchone("SELECT * FROM users WHERE telegram_id = ?", (user_id,))


async def ensure_subscription_or_message(message: Message) -> bool:
    user_id = message.from_user.id  # type: ignore[union-attr]
    if await is_subscribed(user_id):
        await validate_referral_if_possible(user_id)
        return True
    channel_url = await get_setting("channel_url", CHANNEL_URL_ENV)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Підписатися", url=channel_url or "https://t.me/")],
            [InlineKeyboardButton(text="🔄 Перевірити підписку", callback_data="sub:check")],
        ]
    )
    await message.answer("❌ <b>Спочатку потрібно підписатися на канал.</b>", reply_markup=markup)
    return False


async def subscription_keyboard() -> InlineKeyboardMarkup:
    channel_url = await get_setting("channel_url", CHANNEL_URL_ENV)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Підписатися", url=channel_url or "https://t.me/")],
            [InlineKeyboardButton(text="🔄 Перевірити підписку", callback_data="sub:check")],
        ]
    )


# =========================
# Main/user keyboards
# =========================
def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📢 Підписатися на канал"), KeyboardButton(text="✅ Перевірити підписку")],
            [KeyboardButton(text="🎟 Купити квиток"), KeyboardButton(text="📋 Правила")],
            [KeyboardButton(text="🏆 Про розіграш"), KeyboardButton(text="👥 Запросити друзів")],
            [KeyboardButton(text="👤 Мій профіль"), KeyboardButton(text="💬 Підтримка")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="🎟 Квитки")],
            [KeyboardButton(text="💳 Оплати"), KeyboardButton(text="👥 Користувачі")],
            [KeyboardButton(text="🎁 Реферали"), KeyboardButton(text="📢 Розсилка")],
            [KeyboardButton(text="🏆 Переможець"), KeyboardButton(text="⚙️ Налаштування")],
            [KeyboardButton(text="🏠 Головне меню")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def back_inline(callback_data: str = "main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=callback_data)]])


def profile_inline(free_tickets: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🎟 Мої квитки", callback_data="profile:tickets")]]
    if free_tickets > 0:
        rows.append([InlineKeyboardButton(text=f"🎁 Забрати безкоштовний квиток ({free_tickets})", callback_data="gift:open")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def referral_inline(free_tickets: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if free_tickets:
        rows.append([InlineKeyboardButton(text=f"🎁 Обрати безкоштовний номер ({free_tickets})", callback_data="gift:open")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_ticket_grid(user_id: int, gift_mode: bool = False) -> InlineKeyboardMarkup:
    await cleanup_expired_reservations()
    rows_data = await db_fetchall("SELECT number, user_id, status FROM tickets ORDER BY number")
    buttons: list[InlineKeyboardButton] = []
    for row in rows_data:
        number = int(row["number"])
        status = str(row["status"])
        owner_id = int(row["user_id"]) if row["user_id"] is not None else None
        if gift_mode and status == "FREE":
            text = f"🟢 {number}"
        elif status == "FREE":
            text = f"🟢 {number}"
        elif status in {"RESERVED", "PENDING_PAYMENT"} and owner_id == user_id:
            text = f"🟡 {number}"
        else:
            text = f"{status_icon(status)} {number}"
        # In gift mode only free tickets should be clickable. In payment mode only free/current-user-reserved.
        if gift_mode:
            cb = f"gift:{number}" if status == "FREE" else "noop"
        else:
            can_toggle = status == "FREE" or (status == "RESERVED" and owner_id == user_id)
            cb = f"ticket:{number}" if can_toggle else "noop"
        buttons.append(InlineKeyboardButton(text=text, callback_data=cb))

    grid: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 5):
        grid.append(buttons[i : i + 5])

    extra: list[list[InlineKeyboardButton]] = []
    if gift_mode:
        extra.append([InlineKeyboardButton(text="🎁 Безкоштовний квиток", callback_data="gift:info")])
        extra.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="profile")])
    else:
        selected = await db_fetchall(
            "SELECT number FROM tickets WHERE user_id = ? AND status = 'RESERVED' ORDER BY number",
            (user_id,),
        )
        pending = await db_fetchone(
            "SELECT id FROM payments WHERE user_id = ? AND status = 'PENDING' ORDER BY id DESC LIMIT 1",
            (user_id,),
        )
        if pending:
            extra.append([InlineKeyboardButton(text="📸 Надіслати квитанцію", callback_data=f"pay:receipt:{int(pending['id'])}")])
            extra.append([InlineKeyboardButton(text="🏠 Головне меню", callback_data="main")])
        else:
            selected_count = len(selected)
            extra.append([InlineKeyboardButton(text=f"🎟 Обрано: {selected_count}", callback_data="noop")])
            extra.append([InlineKeyboardButton(text="💳 Перейти до оплати", callback_data="pay:start")])
            extra.append([InlineKeyboardButton(text="❌ Очистити вибір", callback_data="ticket:clear")])
            extra.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=grid + extra)


# =========================
# Ticket / payment core
# =========================
async def cleanup_expired_reservations() -> int:
    db = await require_db()
    now = iso(now_utc())
    released = 0
    async with DB_LOCK:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT number, payment_id FROM tickets WHERE status IN ('RESERVED', 'PENDING_PAYMENT') "
                "AND reserved_until IS NOT NULL AND reserved_until < ?",
                (now,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            for row in rows:
                payment_id = row["payment_id"]
                if payment_id is not None:
                    cursor = await db.execute(
                        "SELECT receipt_file_id, status FROM payments WHERE id = ?", (int(payment_id),)
                    )
                    payment = await cursor.fetchone()
                    await cursor.close()
                    if payment and payment["status"] == "PENDING" and not payment["receipt_file_id"]:
                        await db.execute(
                            "UPDATE payments SET status = 'EXPIRED' WHERE id = ? AND status = 'PENDING'",
                            (int(payment_id),),
                        )
                        await db.execute(
                            "UPDATE tickets SET user_id = NULL, status = 'FREE', payment_id = NULL, reserved_until = NULL "
                            "WHERE payment_id = ? AND status = 'PENDING_PAYMENT' AND reserved_until < ?",
                            (int(payment_id), now),
                        )
                    else:
                        continue
                else:
                    await db.execute(
                        "UPDATE tickets SET user_id = NULL, status = 'FREE', payment_id = NULL, reserved_until = NULL "
                        "WHERE number = ? AND status = 'RESERVED' AND reserved_until < ?",
                        (int(row["number"]), now),
                    )
                released += 1
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return released


async def current_user_reserved(user_id: int) -> list[int]:
    rows = await db_fetchall(
        "SELECT number FROM tickets WHERE user_id = ? AND status = 'RESERVED' ORDER BY number",
        (user_id,),
    )
    return [int(row["number"]) for row in rows]


async def current_pending_payment(user_id: int) -> aiosqlite.Row | None:
    return await db_fetchone(
        "SELECT * FROM payments WHERE user_id = ? AND status = 'PENDING' ORDER BY id DESC LIMIT 1",
        (user_id,),
    )


async def toggle_ticket(user_id: int, number: int) -> tuple[bool, str]:
    db = await require_db()
    price = await get_int_setting("ticket_price", ENV_TICKET_PRICE)
    total = await get_int_setting("total_tickets", ENV_TOTAL_TICKETS)
    if number < 1 or number > total:
        return False, "Невірний номер квитка."
    async with DB_LOCK:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT * FROM tickets WHERE number = ?", (number,))
            ticket = await cursor.fetchone()
            await cursor.close()
            if not ticket:
                await db.rollback()
                return False, "Квиток не знайдено."
            if ticket["status"] == "FREE":
                reserved_until = iso(now_utc() + timedelta(minutes=await _setting_int_unlocked(db, "reservation_minutes", ENV_RESERVATION_MINUTES)))
                cursor = await db.execute(
                    "UPDATE tickets SET user_id = ?, status = 'RESERVED', reserved_until = ? "
                    "WHERE number = ? AND status = 'FREE'",
                    (user_id, reserved_until, number),
                )
                updated = cursor.rowcount
                await cursor.close()
                if updated != 1:
                    await db.rollback()
                    return False, "⚠️ Квиток уже забрали."
                await db.commit()
                return True, "✅ Квиток додано до вибору."
            if ticket["status"] == "RESERVED" and ticket["user_id"] == user_id:
                await db.execute(
                    "UPDATE tickets SET user_id = NULL, status = 'FREE', reserved_until = NULL WHERE number = ? AND status = 'RESERVED' AND user_id = ?",
                    (number, user_id),
                )
                await db.commit()
                return True, "✅ Квиток прибрано з вибору."
            if ticket["status"] == "SOLD" or ticket["status"] == "FREE_GIFT":
                await db.rollback()
                return False, "❌ Цей номер уже зайнятий."
            if ticket["status"] in {"RESERVED", "PENDING_PAYMENT"}:
                await db.rollback()
                return False, "🟡 Цей номер зараз зарезервований іншим користувачем."
            await db.rollback()
            return False, "❌ Неможливо змінити цей номер."
        except Exception:
            await db.rollback()
            raise


async def clear_user_selection(user_id: int) -> None:
    db = await require_db()
    async with DB_LOCK:
        await db.execute(
            "UPDATE tickets SET user_id = NULL, status = 'FREE', reserved_until = NULL "
            "WHERE user_id = ? AND status = 'RESERVED'",
            (user_id,),
        )
        await db.commit()


async def create_payment_for_reserved(user_id: int) -> aiosqlite.Row | None:
    db = await require_db()
    async with DB_LOCK:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT id, ticket_numbers, amount, receipt_file_id, status FROM payments WHERE user_id = ? AND status = 'PENDING' ORDER BY id DESC LIMIT 1",
                (user_id,),
            )
            existing = await cursor.fetchone()
            await cursor.close()
            if existing:
                await db.commit()
                return existing

            current_time = iso(now_utc())
            await db.execute(
                "UPDATE tickets SET user_id = NULL, status = 'FREE', payment_id = NULL, reserved_until = NULL "
                "WHERE user_id = ? AND status = 'RESERVED' AND reserved_until IS NOT NULL AND reserved_until < ?",
                (user_id, current_time),
            )
            cursor = await db.execute(
                "SELECT number FROM tickets WHERE user_id = ? AND status = 'RESERVED' "
                "AND (reserved_until IS NULL OR reserved_until > ?) ORDER BY number",
                (user_id, current_time),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            if not rows:
                await db.rollback()
                return None

            price = await _setting_int_unlocked(db, "ticket_price", ENV_TICKET_PRICE)
            numbers = [int(row["number"]) for row in rows]
            amount = price * len(numbers)
            created_at = iso(now_utc())
            cursor = await db.execute(
                "INSERT INTO payments(user_id, amount, ticket_numbers, status, created_at) VALUES(?, ?, ?, 'PENDING', ?)",
                (user_id, amount, ",".join(map(str, numbers)), created_at),
            )
            payment_id = cursor.lastrowid
            await cursor.close()
            await db.execute(
                "UPDATE tickets SET status = 'PENDING_PAYMENT', payment_id = ? WHERE user_id = ? AND status = 'RESERVED'",
                (payment_id, user_id),
            )
            await db.commit()
            cursor = await db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
            created = await cursor.fetchone()
            await cursor.close()
            return created
        except Exception:
            await db.rollback()
            raise


async def save_receipt(payment_id: int, user_id: int, file_id: str) -> bool:
    db = await require_db()
    async with DB_LOCK:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute(
                "SELECT status, user_id FROM payments WHERE id = ?",
                (payment_id,),
            )
            payment = await cursor.fetchone()
            await cursor.close()
            if not payment or int(payment["user_id"]) != user_id or payment["status"] != "PENDING":
                await db.rollback()
                return False
            await db.execute(
                "UPDATE payments SET receipt_file_id = ? WHERE id = ? AND status = 'PENDING'",
                (file_id, payment_id),
            )
            # Receipt received: remove the 30-minute expiration. Admin review now owns the flow.
            await db.execute(
                "UPDATE tickets SET reserved_until = NULL WHERE payment_id = ? AND status = 'PENDING_PAYMENT'",
                (payment_id,),
            )
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            raise


async def confirm_payment(payment_id: int, admin_id: int) -> tuple[bool, str, list[int], int | None, bool]:
    db = await require_db()
    async with DB_LOCK:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
            payment = await cursor.fetchone()
            await cursor.close()
            if not payment:
                await db.rollback()
                return False, "Оплату не знайдено.", [], None, False
            if payment["status"] != "PENDING":
                await db.rollback()
                return False, f"Оплата вже має статус {payment_status_text(payment['status'])}.", [], int(payment["user_id"]), False
            if not payment["receipt_file_id"]:
                await db.rollback()
                return False, "Немає квитанції для перевірки.", [], int(payment["user_id"]), False

            numbers = [int(n) for n in str(payment["ticket_numbers"]).split(",") if n.strip().isdigit()]
            placeholders = ",".join("?" for _ in numbers)
            cursor = await db.execute(
                f"SELECT number, status, user_id FROM tickets WHERE number IN ({placeholders})",
                tuple(numbers),
            )
            tickets = await cursor.fetchall()
            await cursor.close()
            if len(tickets) != len(numbers) or any(row["status"] != "PENDING_PAYMENT" or int(row["user_id"] or 0) != int(payment["user_id"]) for row in tickets):
                await db.rollback()
                return False, "Стан квитків змінився. Потрібна ручна перевірка.", [], int(payment["user_id"]), False

            confirmed_at = iso(now_utc())
            await db.execute(
                "UPDATE payments SET status = 'CONFIRMED', confirmed_at = ?, confirmed_by = ? WHERE id = ? AND status = 'PENDING'",
                (confirmed_at, admin_id, payment_id),
            )
            await db.execute(
                "UPDATE tickets SET status = 'SOLD', reserved_until = NULL WHERE payment_id = ? AND status = 'PENDING_PAYMENT'",
                (payment_id,),
            )
            occupied = await _occupied_count_unlocked(db)
            total = await _setting_int_unlocked(db, "total_tickets", ENV_TOTAL_TICKETS)
            closed = occupied >= total
            if closed:
                await db.execute("UPDATE settings SET value = '1' WHERE key = 'sales_closed'")
            await db.commit()
            return True, "Оплату підтверджено.", numbers, int(payment["user_id"]), closed
        except Exception:
            await db.rollback()
            raise


async def reject_payment(payment_id: int, admin_id: int) -> tuple[bool, int | None, list[int]]:
    db = await require_db()
    async with DB_LOCK:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,))
            payment = await cursor.fetchone()
            await cursor.close()
            if not payment or payment["status"] != "PENDING":
                await db.rollback()
                return False, int(payment["user_id"]) if payment else None, []
            numbers = [int(n) for n in str(payment["ticket_numbers"]).split(",") if n.strip().isdigit()]
            await db.execute(
                "UPDATE payments SET status = 'REJECTED', confirmed_at = ?, confirmed_by = ? WHERE id = ? AND status = 'PENDING'",
                (iso(now_utc()), admin_id, payment_id),
            )
            await db.execute(
                "UPDATE tickets SET user_id = NULL, status = 'FREE', payment_id = NULL, reserved_until = NULL "
                "WHERE payment_id = ? AND status = 'PENDING_PAYMENT'",
                (payment_id,),
            )
            await db.commit()
            return True, int(payment["user_id"]), numbers
        except Exception:
            await db.rollback()
            raise


async def claim_free_ticket(user_id: int, number: int) -> tuple[bool, str]:
    db = await require_db()
    async with DB_LOCK:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("SELECT free_tickets FROM users WHERE telegram_id = ?", (user_id,))
            user = await cursor.fetchone()
            await cursor.close()
            if not user or int(user["free_tickets"]) <= 0:
                await db.rollback()
                return False, "У вас немає доступних безкоштовних квитків."

            cursor = await db.execute("SELECT status FROM tickets WHERE number = ?", (number,))
            ticket = await cursor.fetchone()
            await cursor.close()
            if not ticket or ticket["status"] != "FREE":
                await db.rollback()
                return False, "Цей номер уже зайнятий. Оберіть інший вільний номер."

            cursor = await db.execute(
                "UPDATE tickets SET user_id = ?, status = 'FREE_GIFT', payment_id = NULL, reserved_until = NULL WHERE number = ? AND status = 'FREE'",
                (user_id, number),
            )
            updated = cursor.rowcount
            await cursor.close()
            if updated != 1:
                await db.rollback()
                return False, "⚠️ Номер уже хтось забрав."
            await db.execute("UPDATE users SET free_tickets = free_tickets - 1 WHERE telegram_id = ? AND free_tickets > 0", (user_id,))
            occupied = await _occupied_count_unlocked(db)
            total = await _setting_int_unlocked(db, "total_tickets", ENV_TOTAL_TICKETS)
            if occupied >= total:
                await db.execute("UPDATE settings SET value = '1' WHERE key = 'sales_closed'")
            await db.commit()
            return True, f"🎁 Безкоштовний квиток №{number} успішно отримано!"
        except Exception:
            await db.rollback()
            raise


async def _occupied_count_unlocked(db: aiosqlite.Connection) -> int:
    cursor = await db.execute("SELECT COUNT(*) AS c FROM tickets WHERE status IN ('SOLD', 'FREE_GIFT')")
    row = await cursor.fetchone()
    await cursor.close()
    return int(row["c"] or 0)


async def sales_are_closed() -> bool:
    closed = await get_setting("sales_closed", "0") == "1"
    if closed:
        return True
    occupied = await db_fetchone("SELECT COUNT(*) AS c FROM tickets WHERE status IN ('SOLD', 'FREE_GIFT')")
    total = await get_int_setting("total_tickets", ENV_TOTAL_TICKETS)
    if occupied and int(occupied["c"]) >= total:
        await set_setting("sales_closed", "1")
        return True
    return False


# =========================
# UI content
# =========================
async def start_text() -> str:
    total = await get_int_setting("total_tickets", ENV_TOTAL_TICKETS)
    price = await get_int_setting("ticket_price", ENV_TICKET_PRICE)
    return (
        "📱 <b>РОЗІГРАШ iPHONE 15 PRO</b>\n\n"
        f"🎟 Всього квитків: <b>{total}</b>\n"
        f"💰 Вартість: <b>{format_money(price)}</b>\n\n"
        "Для участі:\n\n"
        "1. Підпишись на канал\n"
        "2. Обери номер або декілька номерів\n"
        "3. Оплати квитки\n"
        "4. Надішли квитанцію\n"
        "5. Дочекайся підтвердження оплати\n\n"
        "🍀 Удачі!"
    )


async def rules_text() -> str:
    support = await get_setting("support_username", SUPPORT_USERNAME_ENV)
    cooperation = await get_setting("cooperation_username", COOPERATION_USERNAME_ENV)
    rules = await get_setting("rules_text", DEFAULT_RULES)
    support_display = f"@{esc(support.lstrip('@'))}" if support else "не вказано"
    cooperation_display = f"@{esc(cooperation.lstrip('@'))}" if cooperation else "не вказано"
    return rules.format(support=support_display, cooperation=cooperation_display)


async def about_text() -> str:
    return await get_setting("about_text", DEFAULT_ABOUT)


async def payment_text(payment: aiosqlite.Row) -> str:
    card = await get_setting("card_number", CARD_NUMBER_ENV)
    numbers = "\n".join(f"🎟 №{esc(n)}" for n in str(payment["ticket_numbers"]).split(",") if n)
    return (
        "💳 <b>ОПЛАТА</b>\n\n"
        f"🎟 <b>Квитки:</b>\n{numbers}\n\n"
        f"💰 <b>Сума:</b> {format_money(int(payment['amount']))}\n\n"
        "💳 <b>Реквізити для оплати:</b>\n"
        f"<code>{esc(card or 'Реквізити ще не налаштовані')}</code>\n\n"
        "Після оплати надішліть фото квитанції одним повідомленням."
    )


async def ticket_selection_text(user_id: int, gift_mode: bool = False) -> str:
    await cleanup_expired_reservations()
    if gift_mode:
        user = await get_user(user_id)
        available = int(user["free_tickets"]) if user else 0
        return (
            "🎁 <b>ОБЕРІТЬ БЕЗКОШТОВНИЙ КВИТОК</b>\n\n"
            f"У вас доступно безкоштовних квитків: <b>{available}</b>\n\n"
            "🟢 — вільний номер\n"
            "🎁 — вже виданий як безкоштовний\n\n"
            "Натисніть на вільний номер, щоб отримати його."
        )
    closed = await sales_are_closed()
    if closed:
        return "🔴 <b>ВСІ КВИТКИ РОЗПОДІЛЕНО!</b>\n\nПродаж завершено."
    reserved = await current_user_reserved(user_id)
    selected_count = len(reserved)
    price = await get_int_setting("ticket_price", ENV_TICKET_PRICE)
    return (
        "🎟 <b>ВИБІР КВИТКІВ</b>\n\n"
        "🟢 — вільний\n"
        "🟡 — ваш резерв / очікує оплати\n"
        "🔴 — проданий\n"
        "🎁 — безкоштовний\n\n"
        f"🎟 Обрано: <b>{selected_count}</b>\n"
        f"💰 До оплати: <b>{format_money(selected_count * price)}</b>\n\n"
        "Резерв діє 30 хвилин, якщо квитанцію ще не надіслано."
    )


# =========================
# User handlers
# =========================
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not message.from_user:
        return
    start_param = None
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2:
        start_param = parts[1].strip()
    await ensure_user(message.from_user, start_param)
    await message.answer(await start_text(), reply_markup=main_reply_keyboard())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("✅ Дію скасовано.", reply_markup=admin_reply_keyboard() if message.from_user and is_admin(message.from_user.id) else main_reply_keyboard())


@router.message(F.text == "📢 Підписатися на канал")
async def subscribe_menu(message: Message) -> None:
    await message.answer("📢 <b>Підпишіться на наш канал, щоб брати участь.</b>", reply_markup=await subscription_keyboard())


@router.message(F.text == "✅ Перевірити підписку")
async def verify_subscription(message: Message) -> None:
    if not message.from_user:
        return
    if await is_subscribed(message.from_user.id):
        await validate_referral_if_possible(message.from_user.id)
        await message.answer("✅ <b>Підписку підтверджено!</b>")
    else:
        await message.answer("❌ <b>Підписку ще не підтверджено.</b>", reply_markup=await subscription_keyboard())


@router.callback_query(F.data == "sub:check")
async def verify_subscription_callback(callback: CallbackQuery) -> None:
    if throttled(f"cb:{callback.from_user.id}"):
        await callback.answer("⏳ Не так швидко.")
        return
    if await is_subscribed(callback.from_user.id):
        await validate_referral_if_possible(callback.from_user.id)
        await callback.answer("✅ Підписку підтверджено!")
        with suppress(TelegramBadRequest):
            await callback.message.edit_text("✅ <b>Підписку підтверджено!</b>\n\nТепер можна брати участь у розіграші.", reply_markup=back_inline("main"))
    else:
        await callback.answer("❌ Підписку не знайдено.", show_alert=True)


@router.message(F.text == "📋 Правила")
async def rules_menu(message: Message) -> None:
    await message.answer(await rules_text())


@router.message(F.text == "🏆 Про розіграш")
async def about_menu(message: Message) -> None:
    await message.answer(await about_text())


@router.message(F.text == "💬 Підтримка")
async def support_menu(message: Message) -> None:
    support = await get_setting("support_username", SUPPORT_USERNAME_ENV)
    cooperation = await get_setting("cooperation_username", COOPERATION_USERNAME_ENV)
    support_text = f"@{esc(support.lstrip('@'))}" if support else "не вказано"
    cooperation_text = f"@{esc(cooperation.lstrip('@'))}" if cooperation else "не вказано"
    await message.answer(
        "💬 <b>ПІДТРИМКА</b>\n\n"
        f"👤 Адміністратор: {support_text}\n"
        f"🤝 Співпраця: {cooperation_text}"
    )


@router.message(F.text == "👥 Запросити друзів")
async def referrals_menu(message: Message) -> None:
    if not message.from_user:
        return
    await validate_referral_if_possible(message.from_user.id)
    user = await get_user(message.from_user.id)
    if not user:
        return
    bot_username = BOT_USERNAME or (await BOT.get_me()).username  # type: ignore[union-attr]
    link = f"https://t.me/{bot_username}?start={message.from_user.id}"
    referrals_for_gift = await get_int_setting("referrals_for_free_ticket", ENV_REFERRALS_FOR_FREE)
    count = int(user["referral_count"])
    free_tickets = int(user["free_tickets"])
    await message.answer(
        "👥 <b>ЗАПРОСИТИ ДРУЗІВ</b>\n\n"
        f"🔗 Ваше посилання:\n<code>{esc(link)}</code>\n\n"
        f"Запрошено: <b>{count} / {referrals_for_gift}</b>\n"
        f"🎁 Безкоштовних квитків: <b>{free_tickets}</b>\n\n"
        f"За кожних {referrals_for_gift} валідних запрошених користувачів ви отримуєте 1 безкоштовний квиток."
        , reply_markup=referral_inline(free_tickets))


@router.message(F.text == "👤 Мій профіль")
async def profile_menu(message: Message) -> None:
    if not message.from_user:
        return
    await validate_referral_if_possible(message.from_user.id)
    await send_profile(message.from_user.id, message)


async def send_profile(user_id: int, target: Message | CallbackQuery) -> None:
    user = await get_user(user_id)
    if not user:
        return
    paid = await db_fetchone(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE user_id = ? AND status = 'CONFIRMED'",
        (user_id,),
    )
    tickets = await db_fetchall(
        "SELECT number, status FROM tickets WHERE user_id = ? ORDER BY number",
        (user_id,),
    )
    ticket_lines = "\n".join(
        f"🎟 №{int(row['number'])} — {('✅ Підтверджено' if row['status'] in {'SOLD', 'FREE_GIFT'} else '🕐 Очікує оплати')}"
        for row in tickets
    ) or "—"
    text = (
        "👤 <b>ВАШ ПРОФІЛЬ</b>\n\n"
        f"ID: <code>{user_id}</code>\n\n"
        f"🎟 <b>Мої квитки:</b>\n{ticket_lines}\n\n"
        f"👥 Запрошено: <b>{int(user['referral_count'])}</b>\n"
        f"🎁 Безкоштовних квитків: <b>{int(user['free_tickets'])}</b>\n"
        f"💰 Витрачено: <b>{format_money(int(paid['total'] or 0))}</b>"
    )
    markup = profile_inline(int(user["free_tickets"]))
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        with suppress(TelegramBadRequest):
            await target.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "profile")
async def profile_callback(callback: CallbackQuery) -> None:
    if throttled(f"cb:{callback.from_user.id}"):
        await callback.answer("⏳ Не так швидко.")
        return
    await send_profile(callback.from_user.id, callback)
    await callback.answer()


@router.callback_query(F.data == "profile:tickets")
async def my_tickets_callback(callback: CallbackQuery) -> None:
    if throttled(f"cb:{callback.from_user.id}"):
        await callback.answer("⏳ Не так швидко.")
        return
    rows = await db_fetchall(
        "SELECT number, status FROM tickets WHERE user_id = ? ORDER BY number",
        (callback.from_user.id,),
    )
    text = "🎟 <b>МОЇ КВИТКИ</b>\n\n"
    if not rows:
        text += "У вас поки немає квитків."
    else:
        for row in rows:
            text += f"№{int(row['number'])} — {('✅ Підтверджено' if row['status'] in {'SOLD', 'FREE_GIFT'} else '🕐 Очікує оплати')}\n"
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=back_inline("profile"))
    await callback.answer()


async def open_ticket_selection(user_id: int, message: Message | CallbackQuery, gift_mode: bool = False) -> None:
    if not gift_mode:
        if not await is_subscribed(user_id):
            markup = await subscription_keyboard()
            if isinstance(message, Message):
                await message.answer("❌ <b>Спочатку потрібно підписатися на канал.</b>", reply_markup=markup)
            else:
                await message.message.edit_text("❌ <b>Спочатку потрібно підписатися на канал.</b>", reply_markup=markup)
            return
        await validate_referral_if_possible(user_id)
        if await sales_are_closed():
            text = await ticket_selection_text(user_id, False)
            if isinstance(message, Message):
                await message.answer(text)
            else:
                with suppress(TelegramBadRequest):
                    await message.message.edit_text(text, reply_markup=back_inline("main"))
            return
        pending = await current_pending_payment(user_id)
        if pending:
            text = await payment_text(pending)
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📸 Надіслати квитанцію", callback_data=f"pay:receipt:{int(pending['id'])}")],
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="main")],
                ]
            )
            if isinstance(message, Message):
                await message.answer(text, reply_markup=markup)
            else:
                with suppress(TelegramBadRequest):
                    await message.message.edit_text(text, reply_markup=markup)
            return

    text = await ticket_selection_text(user_id, gift_mode)
    markup = await build_ticket_grid(user_id, gift_mode)
    if isinstance(message, Message):
        await message.answer(text, reply_markup=markup)
    else:
        with suppress(TelegramBadRequest):
            await message.message.edit_text(text, reply_markup=markup)


@router.message(F.text == "🎟 Купити квиток")
async def buy_ticket_menu(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    await state.clear()
    await open_ticket_selection(message.from_user.id, message, False)


@router.callback_query(F.data.startswith("ticket:"))
async def ticket_callback(callback: CallbackQuery) -> None:
    if throttled(f"ticket:{callback.from_user.id}"):
        await callback.answer("⏳ Не так швидко.")
        return
    action = callback.data.split(":", 1)[1]
    if action == "clear":
        await clear_user_selection(callback.from_user.id)
        await callback.answer("✅ Вибір очищено.")
        await open_ticket_selection(callback.from_user.id, callback, False)
        return
    try:
        number = int(action)
    except ValueError:
        await callback.answer("❌ Невірний номер.", show_alert=True)
        return
    ok, text = await toggle_ticket(callback.from_user.id, number)
    await callback.answer(text)
    await open_ticket_selection(callback.from_user.id, callback, False)


@router.callback_query(F.data == "pay:start")
async def payment_start(callback: CallbackQuery, state: FSMContext) -> None:
    if throttled(f"pay:{callback.from_user.id}"):
        await callback.answer("⏳ Не так швидко.")
        return
    if not await is_subscribed(callback.from_user.id):
        await callback.answer("❌ Потрібна підписка на канал.", show_alert=True)
        return
    payment = await create_payment_for_reserved(callback.from_user.id)
    if not payment:
        await callback.answer("❌ Спочатку оберіть хоча б один вільний номер.", show_alert=True)
        return
    await state.set_state(PurchaseStates.waiting_receipt)
    await state.update_data(payment_id=int(payment["id"]))
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            await payment_text(payment),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📸 Я оплатив — надіслати квитанцію", callback_data=f"pay:receipt:{int(payment['id'])}")],
                    [InlineKeyboardButton(text="❌ Скасувати очікування", callback_data="pay:cancel")],
                ]
            ),
        )


@router.callback_query(F.data.startswith("pay:receipt:"))
async def payment_receipt_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if throttled(f"receipt:{callback.from_user.id}"):
        await callback.answer("⏳ Не так швидко.")
        return
    payment_id = int(callback.data.rsplit(":", 1)[1])
    payment = await db_fetchone("SELECT * FROM payments WHERE id = ? AND user_id = ? AND status = 'PENDING'", (payment_id, callback.from_user.id))
    if not payment:
        await callback.answer("❌ Цю оплату вже закрито.", show_alert=True)
        return
    await state.set_state(PurchaseStates.waiting_receipt)
    await state.update_data(payment_id=payment_id)
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            "📸 <b>Надішліть фото квитанції одним повідомленням.</b>\n\n"
            "Після отримання квитанції адміністратор перевірить оплату.",
            reply_markup=back_inline("main"),
        )


@router.callback_query(F.data == "pay:cancel")
async def payment_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await clear_user_selection(callback.from_user.id)
    await callback.answer("✅ Резерв скасовано.")
    with suppress(TelegramBadRequest):
        await callback.message.edit_text("✅ Резерв скасовано. Номери повернуто у продаж.", reply_markup=back_inline("main"))


@router.message(StateFilter(PurchaseStates.waiting_receipt), F.photo)
async def receive_receipt(message: Message, state: FSMContext) -> None:
    if not message.from_user or not message.photo:
        return
    data = await state.get_data()
    payment_id = int(data.get("payment_id", 0))
    if not payment_id:
        await state.clear()
        await message.answer("❌ Оплату не знайдено. Спробуйте почати покупку ще раз.")
        return
    file_id = message.photo[-1].file_id
    saved = await save_receipt(payment_id, message.from_user.id, file_id)
    if not saved:
        await state.clear()
        await message.answer("❌ Не вдалося прийняти цю квитанцію. Можливо, оплата вже закрита.")
        return
    await state.clear()
    await message.answer("🕐 <b>Квитанцію отримано.</b>\n\nОчікуйте підтвердження адміністратора.")
    payment = await db_fetchone("SELECT * FROM payments WHERE id = ?", (payment_id,))
    if payment:
        await notify_admin_about_payment(payment)


@router.message(StateFilter(PurchaseStates.waiting_receipt))
async def receipt_only_photo(message: Message) -> None:
    await message.answer("📸 Будь ласка, надішліть саме фото квитанції одним повідомленням.")


async def notify_admin_about_payment(payment: aiosqlite.Row) -> None:
    if BOT is None:
        return
    user = await get_user(int(payment["user_id"]))
    if not user:
        return
    text = (
        "💳 <b>НОВА ОПЛАТА</b>\n\n"
        f"👤 Користувач: {display_user(user['username'], user['first_name'], int(user['telegram_id']))}\n"
        f"ID: <code>{int(user['telegram_id'])}</code>\n\n"
        f"🎟 Квитки: №{', №'.join(str(n) for n in str(payment['ticket_numbers']).split(','))}\n"
        f"💰 Сума: <b>{format_money(int(payment['amount']))}</b>\n\n"
        "📸 Квитанція прикріплена нижче."
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"admin:pay:confirm:{int(payment['id'])}"), InlineKeyboardButton(text="❌ Відхилити", callback_data=f"admin:pay:reject:{int(payment['id'])}")],
            [InlineKeyboardButton(text="👤 Профіль", callback_data=f"admin:profile:{int(user['telegram_id'])}"), InlineKeyboardButton(text="🎟 Квитки користувача", callback_data=f"admin:utickets:{int(user['telegram_id'])}")],
        ]
    )
    for admin_id in ADMIN_IDS:
        try:
            await BOT.send_photo(admin_id, payment["receipt_file_id"], caption=text, reply_markup=markup)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            with suppress(Exception):
                await BOT.send_photo(admin_id, payment["receipt_file_id"], caption=text, reply_markup=markup)
        except TelegramForbiddenError:
            logger.warning("Admin %s cannot receive bot messages", admin_id)
        except TelegramBadRequest as exc:
            logger.warning("Failed to notify admin %s: %s", admin_id, exc)


@router.callback_query(F.data.startswith("gift:"))
async def gift_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if throttled(f"gift:{callback.from_user.id}"):
        await callback.answer("⏳ Не так швидко.")
        return
    action = callback.data.split(":", 1)[1]
    if action == "open":
        if not await is_subscribed(callback.from_user.id):
            await callback.answer("❌ Спочатку підпишіться на канал.", show_alert=True)
            with suppress(TelegramBadRequest):
                await callback.message.edit_text("❌ <b>Спочатку потрібно підписатися на канал.</b>", reply_markup=await subscription_keyboard())
            return
        await validate_referral_if_possible(callback.from_user.id)
        user = await get_user(callback.from_user.id)
        if not user or int(user["free_tickets"]) <= 0:
            await callback.answer("🎁 Безкоштовних квитків немає.", show_alert=True)
            return
        await state.set_state(PurchaseStates.gift_mode)
        await open_ticket_selection(callback.from_user.id, callback, True)
        await callback.answer()
        return
    if action == "info":
        await callback.answer("🎁 Оберіть будь-який вільний номер.")
        return
    try:
        number = int(action)
    except ValueError:
        await callback.answer("❌ Невірний номер.", show_alert=True)
        return
    ok, text = await claim_free_ticket(callback.from_user.id, number)
    await callback.answer(text, show_alert=not ok)
    user = await get_user(callback.from_user.id)
    if ok:
        await callback.message.edit_text(
            f"✅ <b>Безкоштовний квиток отримано!</b>\n\n🎟 №{number}\n\n🍀 Бажаємо удачі!",
            reply_markup=profile_inline(int(user["free_tickets"]) if user else 0),
        )
        await state.clear()
    elif user and int(user["free_tickets"]) > 0:
        await open_ticket_selection(callback.from_user.id, callback, True)


# =========================
# Admin panels
# =========================
def admin_panel_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"), InlineKeyboardButton(text="🎟 Квитки", callback_data="admin:tickets")],
            [InlineKeyboardButton(text="💳 Оплати", callback_data="admin:payments"), InlineKeyboardButton(text="👥 Користувачі", callback_data="admin:users")],
            [InlineKeyboardButton(text="🎁 Реферали", callback_data="admin:refs"), InlineKeyboardButton(text="📢 Розсилка", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="🏆 Переможець", callback_data="admin:winner"), InlineKeyboardButton(text="⚙️ Налаштування", callback_data="admin:settings")],
        ]
    )


async def admin_guard(callback: CallbackQuery) -> bool:
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ заборонено.", show_alert=True)
        return False
    return True


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ заборонено.")
        return
    await message.answer("⚙️ <b>АДМІН-ПАНЕЛЬ</b>", reply_markup=admin_reply_keyboard())
    await message.answer("Оберіть розділ:", reply_markup=admin_panel_inline())


@router.message(F.text == "📊 Статистика")
async def admin_stats_message(message: Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await send_admin_stats(message)


@router.message(F.text == "🎟 Квитки")
async def admin_tickets_message(message: Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await send_admin_tickets(message)


@router.message(F.text == "💳 Оплати")
async def admin_payments_message(message: Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await send_admin_payments(message, "PENDING", 0)


@router.message(F.text == "👥 Користувачі")
async def admin_users_message(message: Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await send_admin_users(message)


@router.message(F.text == "🎁 Реферали")
async def admin_refs_message(message: Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await send_admin_refs(message)


@router.message(F.text == "📢 Розсилка")
async def admin_broadcast_message(message: Message, state: FSMContext) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await state.set_state(BroadcastStates.waiting_content)
        await message.answer(
            "📢 <b>РОЗСИЛКА</b>\n\nНадішліть повідомлення для розсилки. Можна надіслати текст або фото з підписом.\n\n/cancel — скасувати."
        )


@router.message(F.text == "🏆 Переможець")
async def admin_winner_message(message: Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await send_admin_winner(message)


@router.message(F.text == "⚙️ Налаштування")
async def admin_settings_message(message: Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await send_admin_settings(message)


@router.message(F.text == "🏠 Головне меню")
async def home_menu(message: Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await message.answer(await start_text(), reply_markup=admin_reply_keyboard())
    else:
        await message.answer(await start_text(), reply_markup=main_reply_keyboard())


async def send_admin_stats(target: Message | CallbackQuery) -> None:
    stats = await get_stats()
    text = (
        "📊 <b>СТАТИСТИКА</b>\n\n"
        f"🎟 Продано: <b>{stats['sold']} / {stats['total']}</b>\n"
        f"🎁 Безкоштовних квитків: <b>{stats['gifts']}</b>\n"
        f"🟢 Вільно: <b>{stats['free']}</b>\n"
        f"🕐 Заброньовано: <b>{stats['reserved']}</b>\n\n"
        f"👥 Учасників: <b>{stats['participants']}</b>\n\n"
        f"💰 Підтверджено оплат: <b>{format_money(stats['confirmed_amount'])}</b>\n"
        f"💳 Очікують підтвердження: <b>{stats['pending_payments']}</b>\n\n"
        f"📌 Продажі: {'закриті' if stats['closed'] else 'відкриті'}"
    )
    markup = back_inline("admin:panel")
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        with suppress(TelegramBadRequest):
            await target.message.edit_text(text, reply_markup=markup)


async def get_stats() -> dict[str, int | bool]:
    total = await get_int_setting("total_tickets", ENV_TOTAL_TICKETS)
    row = await db_fetchone("SELECT COUNT(*) AS c FROM tickets WHERE status = 'SOLD'")
    sold = int(row["c"] or 0)
    row = await db_fetchone("SELECT COUNT(*) AS c FROM tickets WHERE status = 'FREE_GIFT'")
    gifts = int(row["c"] or 0)
    row = await db_fetchone("SELECT COUNT(*) AS c FROM tickets WHERE status = 'FREE'")
    free = int(row["c"] or 0)
    row = await db_fetchone("SELECT COUNT(*) AS c FROM tickets WHERE status IN ('RESERVED', 'PENDING_PAYMENT')")
    reserved = int(row["c"] or 0)
    row = await db_fetchone("SELECT COUNT(DISTINCT user_id) AS c FROM tickets WHERE status IN ('SOLD', 'FREE_GIFT')")
    participants = int(row["c"] or 0)
    row = await db_fetchone("SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE status = 'CONFIRMED'")
    confirmed_amount = int(row["total"] or 0)
    row = await db_fetchone("SELECT COUNT(*) AS c FROM payments WHERE status = 'PENDING'")
    pending_payments = int(row["c"] or 0)
    return {
        "total": total,
        "sold": sold,
        "gifts": gifts,
        "free": free,
        "reserved": reserved,
        "participants": participants,
        "confirmed_amount": confirmed_amount,
        "pending_payments": pending_payments,
        "closed": await sales_are_closed(),
    }


@router.callback_query(F.data == "admin:panel")
async def admin_panel_callback(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text("⚙️ <b>АДМІН-ПАНЕЛЬ</b>", reply_markup=admin_panel_inline())


@router.callback_query(F.data == "admin:stats")
async def admin_stats_callback(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    await send_admin_stats(callback)
    await callback.answer()


async def send_admin_tickets(target: Message | CallbackQuery) -> None:
    rows = await db_fetchall("SELECT number, status, user_id FROM tickets ORDER BY number")
    buttons = []
    for row in rows:
        number = int(row["number"])
        buttons.append(InlineKeyboardButton(text=f"{status_icon(row['status'])} {number}", callback_data=f"admin:ticket:{number}"))
    grid = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
    grid.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:panel")])
    markup = InlineKeyboardMarkup(inline_keyboard=grid)
    text = "🎟 <b>КВИТКИ</b>\n\n🟢 Вільний · 🟡 Очікує/резерв · 🔴 Проданий · 🎁 Безкоштовний"
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        with suppress(TelegramBadRequest):
            await target.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "admin:tickets")
async def admin_tickets_callback(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    await send_admin_tickets(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:ticket:"))
async def admin_ticket_detail(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    number = int(callback.data.rsplit(":", 1)[1])
    row = await db_fetchone(
        "SELECT t.*, u.username, u.first_name, p.amount, p.id AS payment_id, p.status AS payment_status "
        "FROM tickets t LEFT JOIN users u ON u.telegram_id = t.user_id "
        "LEFT JOIN payments p ON p.id = t.payment_id WHERE t.number = ?",
        (number,),
    )
    if not row:
        await callback.answer("Не знайдено", show_alert=True)
        return
    owner_text = "—"
    if row["user_id"] is not None:
        owner_text = display_user(row["username"], row["first_name"], int(row["user_id"]))
    amount = format_money(int(row["amount"])) if row["amount"] is not None else "—"
    date = esc(row["created_at"])
    reserve = esc(row["reserved_until"] or "—")
    text = (
        f"🎟 <b>Квиток №{number}</b>\n\n"
        f"Статус: {status_icon(row['status'])} <b>{esc(short_status(row['status']))}</b>\n"
        f"👤 Власник: {owner_text}\n"
        f"Telegram ID: <code>{row['user_id'] or '—'}</code>\n"
        f"💰 Оплата: {amount}\n"
        f"💳 Статус оплати: {payment_status_text(row['payment_status']) if row['payment_status'] else '—'}\n"
        f"📅 Дата створення: <code>{date}</code>\n"
        f"⏱ Резерв до: <code>{reserve}</code>"
    )
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=back_inline("admin:tickets"))


async def send_admin_payments(target: Message | CallbackQuery, status: str = "PENDING", page: int = 0) -> None:
    page_size = 8
    offset = page * page_size
    rows = await db_fetchall(
        "SELECT p.*, u.username, u.first_name FROM payments p JOIN users u ON u.telegram_id = p.user_id "
        "WHERE p.status = ? ORDER BY p.id DESC LIMIT ? OFFSET ?",
        (status, page_size + 1, offset),
    )
    has_next = len(rows) > page_size
    rows = rows[:page_size]
    lines = [
        f"💳 <b>ОПЛАТИ — {payment_status_text(status)}</b>",
        "",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        numbers = str(row["ticket_numbers"]).replace(",", ", ")
        label = f"#{int(row['id'])} · {numbers} · {format_money(int(row['amount']))}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"admin:payment:{int(row['id'])}")])
        lines.append(f"#{int(row['id'])} — {display_user(row['username'], row['first_name'], int(row['user_id']))} — {format_money(int(row['amount']))}")
    if not rows:
        lines.append("Нічого не знайдено.")
    filter_row = [
        InlineKeyboardButton(text="🕐 Очікують", callback_data="admin:payments:PENDING:0"),
        InlineKeyboardButton(text="✅ Підтверджені", callback_data="admin:payments:CONFIRMED:0"),
        InlineKeyboardButton(text="❌ Відхилені", callback_data="admin:payments:REJECTED:0"),
    ]
    buttons.append(filter_row)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"admin:payments:{status}:{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"admin:payments:{status}:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:panel")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = "\n".join(lines)
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        with suppress(TelegramBadRequest):
            await target.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "admin:payments")
async def admin_payments_callback(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    await send_admin_payments(callback, "PENDING", 0)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:payments:"))
async def admin_payments_filter_callback(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    _, _, status, page = callback.data.split(":")
    await send_admin_payments(callback, status, int(page))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:payment:"))
async def admin_payment_detail(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    payment_id = int(callback.data.rsplit(":", 1)[1])
    payment = await db_fetchone(
        "SELECT p.*, u.username, u.first_name FROM payments p JOIN users u ON u.telegram_id = p.user_id WHERE p.id = ?",
        (payment_id,),
    )
    if not payment:
        await callback.answer("Оплату не знайдено.", show_alert=True)
        return
    text = (
        f"💳 <b>ОПЛАТА #{payment_id}</b>\n\n"
        f"👤 Користувач: {display_user(payment['username'], payment['first_name'], int(payment['user_id']))}\n"
        f"ID: <code>{int(payment['user_id'])}</code>\n"
        f"🎟 Квитки: №{', №'.join(str(n) for n in str(payment['ticket_numbers']).split(','))}\n"
        f"💰 Сума: <b>{format_money(int(payment['amount']))}</b>\n"
        f"📅 Створено: <code>{esc(payment['created_at'])}</code>\n"
        f"Статус: <b>{payment_status_text(payment['status'])}</b>\n"
    )
    buttons: list[list[InlineKeyboardButton]] = []
    if payment["status"] == "PENDING":
        buttons.append([
            InlineKeyboardButton(text="✅ Підтвердити", callback_data=f"admin:pay:confirm:{payment_id}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"admin:pay:reject:{payment_id}"),
        ])
    buttons.append([InlineKeyboardButton(text="👤 Профіль", callback_data=f"admin:profile:{int(payment['user_id'])}"), InlineKeyboardButton(text="🎟 Квитки користувача", callback_data=f"admin:utickets:{int(payment['user_id'])}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:payments")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.answer()
    if payment["receipt_file_id"]:
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            logger.debug("Original payment message was already gone")
        await callback.bot.send_photo(callback.from_user.id, payment["receipt_file_id"], caption=text, reply_markup=markup)
    else:
        with suppress(TelegramBadRequest):
            await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("admin:pay:confirm:"))
async def admin_confirm_payment(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    payment_id = int(callback.data.rsplit(":", 1)[1])
    ok, text, numbers, user_id, closed = await confirm_payment(payment_id, callback.from_user.id)
    await log_admin(callback.from_user.id, "payment_confirm" if ok else "payment_confirm_failed", f"payment_id={payment_id} user_id={user_id}")
    if ok and user_id:
        try:
            await callback.bot.send_message(
                user_id,
                "✅ <b>ОПЛАТУ ПІДТВЕРДЖЕНО!</b>\n\n"
                + "\n".join(f"🎟 №{n}" for n in numbers)
                + "\n\n🍀 Бажаємо удачі!",
            )
        except TelegramForbiddenError:
            logger.info("User %s blocked the bot", user_id)
    if ok:
        await callback.answer("✅ Оплату підтверджено.")
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
        if closed:
            for admin_id in ADMIN_IDS:
                with suppress(Exception):
                    await callback.bot.send_message(admin_id, "🔴 <b>ВСІ 70 КВИТКІВ РОЗПОДІЛЕНО!</b>\n\nПродаж завершено. Можна визначати переможця.")
    else:
        await callback.answer(text, show_alert=True)


@router.callback_query(F.data.startswith("admin:pay:reject:"))
async def admin_reject_payment(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    payment_id = int(callback.data.rsplit(":", 1)[1])
    ok, user_id, numbers = await reject_payment(payment_id, callback.from_user.id)
    await log_admin(callback.from_user.id, "payment_reject" if ok else "payment_reject_failed", f"payment_id={payment_id} user_id={user_id}")
    if ok and user_id:
        with suppress(TelegramForbiddenError, TelegramBadRequest):
            await callback.bot.send_message(
                user_id,
                "❌ <b>ОПЛАТУ ВІДХИЛЕНО</b>\n\n"
                f"Квитки {', '.join('№'+str(n) for n in numbers)} повернуто у продаж.\n\n"
                "За потреби зверніться до підтримки."
            )
        await callback.answer("❌ Оплату відхилено.")
        with suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
    else:
        await callback.answer("❌ Не вдалося відхилити оплату.", show_alert=True)


async def send_admin_users(target: Message | CallbackQuery) -> None:
    rows = await db_fetchall(
        "SELECT u.telegram_id, u.username, u.first_name, u.referral_count, u.free_tickets, u.created_at, "
        "(SELECT COUNT(*) FROM tickets t WHERE t.user_id=u.telegram_id AND t.status IN ('SOLD','FREE_GIFT')) AS ticket_count "
        "FROM users u ORDER BY u.created_at DESC LIMIT 20"
    )
    buttons = [[InlineKeyboardButton(text="🔎 Пошук користувача", callback_data="admin:user_search")]]
    for row in rows:
        label = f"{display_user(row['username'], row['first_name'], int(row['telegram_id']))} · {int(row['ticket_count'])} кв."
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"admin:profile:{int(row['telegram_id'])}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:panel")])
    text = "👥 <b>КОРИСТУВАЧІ</b>\n\nОстанні 20 користувачів."
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        with suppress(TelegramBadRequest):
            await target.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "admin:users")
async def admin_users_callback(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    await send_admin_users(callback)
    await callback.answer()


@router.callback_query(F.data == "admin:user_search")
async def admin_user_search(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    await state.set_state(UserSearchStates.waiting_query)
    await callback.message.answer("🔎 Введіть Telegram ID або @username користувача.")
    await callback.answer()


@router.message(StateFilter(UserSearchStates.waiting_query))
async def admin_user_search_input(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await state.clear()
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("❌ Введіть ID або username.")
        return
    if query.lstrip("-").isdigit():
        row = await get_user(int(query))
    else:
        username = normalize_username(query)
        row = await db_fetchone("SELECT * FROM users WHERE lower(username) = ?", (username,))
    await state.clear()
    if not row:
        await message.answer("❌ Користувача не знайдено.")
        return
    await send_admin_user_profile(message.from_user.id, int(row["telegram_id"]), message)


@router.callback_query(F.data.startswith("admin:profile:"))
async def admin_profile_callback(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    user_id = int(callback.data.rsplit(":", 1)[1])
    await send_admin_user_profile(callback.from_user.id, user_id, callback)
    await callback.answer()


async def send_admin_user_profile(admin_id: int, user_id: int, target: Message | CallbackQuery) -> None:
    user = await get_user(user_id)
    if not user:
        text = "❌ Користувача не знайдено."
        markup = back_inline("admin:users")
    else:
        tickets = await db_fetchall("SELECT number, status FROM tickets WHERE user_id = ? ORDER BY number", (user_id,))
        paid = await db_fetchone("SELECT COALESCE(SUM(amount),0) AS total FROM payments WHERE user_id = ? AND status='CONFIRMED'", (user_id,))
        ticket_list = ", ".join(f"№{int(row['number'])}" for row in tickets) or "—"
        text = (
            "👤 <b>ПРОФІЛЬ КОРИСТУВАЧА</b>\n\n"
            f"ID: <code>{user_id}</code>\n"
            f"Username: @{esc(user['username']) if user['username'] else '—'}\n"
            f"Ім'я: {esc(user['first_name'])}\n"
            f"Квитків: <b>{len(tickets)}</b>\n"
            f"Номери: {ticket_list}\n"
            f"Рефералів: <b>{int(user['referral_count'])}</b>\n"
            f"Сума оплат: <b>{format_money(int(paid['total'] or 0))}</b>\n"
            f"Дата реєстрації: <code>{esc(user['created_at'])}</code>"
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🎟 Квитки користувача", callback_data=f"admin:utickets:{user_id}")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:users")],
            ]
        )
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        with suppress(TelegramBadRequest):
            await target.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("admin:utickets:"))
async def admin_user_tickets_callback(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    user_id = int(callback.data.rsplit(":", 1)[1])
    rows = await db_fetchall("SELECT number, status FROM tickets WHERE user_id = ? ORDER BY number", (user_id,))
    text = "🎟 <b>КВИТКИ КОРИСТУВАЧА</b>\n\n" + ("\n".join(f"№{int(row['number'])} — {status_icon(row['status'])} {short_status(row['status'])}" for row in rows) if rows else "Немає квитків.")
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=back_inline(f"admin:profile:{user_id}"))


async def send_admin_refs(target: Message | CallbackQuery) -> None:
    rows = await db_fetchall(
        "SELECT u.telegram_id, u.username, u.first_name, u.referral_count, u.free_tickets "
        "FROM users u WHERE u.referral_count > 0 ORDER BY u.referral_count DESC, u.created_at ASC LIMIT 30"
    )
    text = "🎁 <b>РЕФЕРАЛИ</b>\n\n"
    if not rows:
        text += "Поки немає валідних рефералів."
    else:
        for idx, row in enumerate(rows, 1):
            text += f"{idx}. {display_user(row['username'], row['first_name'], int(row['telegram_id']))} — {int(row['referral_count'])} реф. · 🎁 {int(row['free_tickets'])}\n"
    markup = back_inline("admin:panel")
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        with suppress(TelegramBadRequest):
            await target.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "admin:refs")
async def admin_refs_callback(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    await send_admin_refs(callback)
    await callback.answer()


# =========================
# Admin broadcast
# =========================
@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    await state.set_state(BroadcastStates.waiting_content)
    await callback.message.answer("📢 Надішліть текст або фото з підписом для розсилки.\n\n/cancel — скасувати.")
    await callback.answer()


@router.message(StateFilter(BroadcastStates.waiting_content), F.photo)
async def broadcast_receive_photo(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id) or not message.photo:
        return
    await state.update_data(content_type="photo", file_id=message.photo[-1].file_id, text=message.caption or "", button_text="", button_url="")
    await state.set_state(BroadcastStates.waiting_button_choice)
    await message.answer(
        "📢 Повідомлення отримано. Додати Inline-кнопку?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Додати кнопку", callback_data="broadcast:add")],
                [InlineKeyboardButton(text="🚫 Без кнопки", callback_data="broadcast:none")],
                [InlineKeyboardButton(text="❌ Скасувати", callback_data="broadcast:cancel")],
            ]
        ),
    )


@router.message(StateFilter(BroadcastStates.waiting_content))
async def broadcast_receive_text(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    text = message.text or message.caption or ""
    if not text:
        await message.answer("❌ Потрібен текст або фото з підписом.")
        return
    await state.update_data(content_type="text", file_id="", text=text, button_text="", button_url="")
    await state.set_state(BroadcastStates.waiting_button_choice)
    await message.answer(
        "📢 Повідомлення отримано. Додати Inline-кнопку?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="➕ Додати кнопку", callback_data="broadcast:add")],
                [InlineKeyboardButton(text="🚫 Без кнопки", callback_data="broadcast:none")],
                [InlineKeyboardButton(text="❌ Скасувати", callback_data="broadcast:cancel")],
            ]
        ),
    )


@router.callback_query(F.data == "broadcast:add")
async def broadcast_add_button(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    await state.set_state(BroadcastStates.waiting_button_text)
    await callback.message.answer("🔗 Введіть текст кнопки.")
    await callback.answer()


@router.message(StateFilter(BroadcastStates.waiting_button_text))
async def broadcast_button_text(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Текст кнопки не може бути порожнім.")
        return
    await state.update_data(button_text=text)
    await state.set_state(BroadcastStates.waiting_button_url)
    await message.answer("🔗 Введіть URL кнопки. Наприклад: https://t.me/ua_2024k")


@router.message(StateFilter(BroadcastStates.waiting_button_url))
async def broadcast_button_url(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    url = (message.text or "").strip()
    if not (url.startswith("https://") or url.startswith("http://") or url.startswith("tg://")):
        await message.answer("❌ URL має починатися з http://, https:// або tg://")
        return
    await state.update_data(button_url=url)
    await show_broadcast_confirmation(message, state)


@router.callback_query(F.data == "broadcast:none")
async def broadcast_no_button(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    await state.update_data(button_text="", button_url="")
    await show_broadcast_confirmation(callback.message, state)
    await callback.answer()


async def show_broadcast_confirmation(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    content_type = data.get("content_type", "text")
    text = data.get("text", "")
    button_text = data.get("button_text", "")
    button_url = data.get("button_url", "")
    buttons = []
    if button_text and button_url:
        buttons.append([InlineKeyboardButton(text=button_text, url=button_url)])
    buttons.append([InlineKeyboardButton(text="✅ Відправити", callback_data="broadcast:send"), InlineKeyboardButton(text="❌ Скасувати", callback_data="broadcast:cancel")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    preview = "📢 <b>ПІДТВЕРДИТИ РОЗСИЛКУ?</b>\n\n" + ("Фото з підписом" if content_type == "photo" else "Текстове повідомлення")
    await message.answer(preview, reply_markup=markup)
    await state.set_state(BroadcastStates.waiting_confirmation)
    # Send a real preview as a separate message, so HTML/caption rendering is visible.
    if content_type == "photo":
        await message.answer_photo(data["file_id"], caption=text or None, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]]) if button_text and button_url else None)
    else:
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]]) if button_text and button_url else None)


@router.callback_query(F.data == "broadcast:send")
async def broadcast_send(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    data = await state.get_data()
    content_type = data.get("content_type")
    text = data.get("text", "")
    file_id = data.get("file_id", "")
    button_text = data.get("button_text", "")
    button_url = data.get("button_url", "")
    reply_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]]) if button_text and button_url else None
    users = await db_fetchall("SELECT telegram_id FROM users ORDER BY telegram_id")
    sent = 0
    errors = 0
    await callback.answer("📢 Розсилка запущена.")
    await callback.message.edit_text("📢 <b>РОЗСИЛКА ВИКОНУЄТЬСЯ...</b>")
    for row in users:
        user_id = int(row["telegram_id"])
        try:
            if content_type == "photo":
                await callback.bot.send_photo(user_id, file_id, caption=text or None, reply_markup=reply_markup)
            else:
                await callback.bot.send_message(user_id, text, reply_markup=reply_markup)
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            try:
                if content_type == "photo":
                    await callback.bot.send_photo(user_id, file_id, caption=text or None, reply_markup=reply_markup)
                else:
                    await callback.bot.send_message(user_id, text, reply_markup=reply_markup)
                sent += 1
            except Exception:
                errors += 1
        except (TelegramForbiddenError, TelegramBadRequest, TelegramNetworkError):
            errors += 1
        except Exception as exc:
            errors += 1
            logger.warning("Broadcast error for %s: %s", user_id, exc)
        await asyncio.sleep(0.06)
    await log_admin(callback.from_user.id, "broadcast", f"sent={sent} errors={errors}")
    await state.clear()
    await callback.message.answer(f"📢 <b>РОЗСИЛКУ ЗАВЕРШЕНО</b>\n\nВідправлено: <b>{sent}</b>\nПомилки: <b>{errors}</b>")


@router.callback_query(F.data == "broadcast:cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    await state.clear()
    await callback.answer("❌ Розсилку скасовано.")
    with suppress(TelegramBadRequest):
        await callback.message.edit_text("❌ <b>Розсилку скасовано.</b>", reply_markup=admin_panel_inline())


# =========================
# Admin winner
# =========================
async def send_admin_winner(target: Message | CallbackQuery) -> None:
    stats = await get_stats()
    winner_number_raw = await get_setting("winner_ticket_number", "")
    winner_user_raw = await get_setting("winner_user_id", "")
    if winner_number_raw and winner_user_raw:
        winner = await get_user(int(winner_user_raw))
        text = (
            "🏆 <b>ПЕРЕМОЖЕЦЬ ВЖЕ ВИЗНАЧЕНИЙ</b>\n\n"
            f"🎟 Квиток №<b>{esc(winner_number_raw)}</b>\n"
            f"👤 {display_user(winner['username'], winner['first_name'], int(winner_user_raw)) if winner else '—'}\n"
            f"Telegram ID: <code>{esc(winner_user_raw)}</code>"
        )
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📢 Опублікувати переможця", callback_data="admin:winner:publish")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:panel")],
            ]
        )
    else:
        occupied = stats["sold"] + stats["gifts"]
        text = (
            "🏆 <b>ВИЗНАЧЕННЯ ПЕРЕМОЖЦЯ</b>\n\n"
            f"🎟 У розіграші: <b>{stats['total']} квитків</b>\n"
            f"👥 Учасників: <b>{stats['participants']}</b>\n"
            f"📌 Розподілено номерів: <b>{occupied} / {stats['total']}</b>\n\n"
        )
        if int(occupied) < int(stats["total"]):
            text += "🕐 Дочекатися завершення розподілу всіх номерів."
            markup = back_inline("admin:panel")
        else:
            text += "🎲 Усі номери розподілені. Результат можна визначити один раз."
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🎲 Визначити переможця", callback_data="admin:winner:draw")],
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:panel")],
                ]
            )
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        with suppress(TelegramBadRequest):
            await target.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "admin:winner")
async def admin_winner_callback(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    await send_admin_winner(callback)
    await callback.answer()


@router.callback_query(F.data == "admin:winner:draw")
async def draw_winner(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    db = await require_db()
    async with DB_LOCK:
        await db.execute("BEGIN IMMEDIATE")
        try:
            winner_ticket_raw = await _setting_value_unlocked(db, "winner_ticket_number", "")
            winner_user_raw = await _setting_value_unlocked(db, "winner_user_id", "")
            if winner_ticket_raw and winner_user_raw:
                await db.commit()
                await send_admin_winner(callback)
                await callback.answer("🏆 Переможець уже визначений.", show_alert=True)
                return
            total = await _setting_int_unlocked(db, "total_tickets", ENV_TOTAL_TICKETS)
            cursor = await db.execute("SELECT number, user_id FROM tickets WHERE status IN ('SOLD', 'FREE_GIFT') ORDER BY number")
            candidates = await cursor.fetchall()
            await cursor.close()
            if len(candidates) < total:
                await db.rollback()
                await callback.answer("🕐 Ще не всі номери розподілені.", show_alert=True)
                return
            winner = secrets.choice(candidates)
            await db.execute("UPDATE settings SET value = ? WHERE key = 'winner_ticket_number'", (str(int(winner['number'])),))
            await db.execute("UPDATE settings SET value = ? WHERE key = 'winner_user_id'", (str(int(winner['user_id'])),))
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    await log_admin(callback.from_user.id, "winner_draw", f"ticket={winner['number']} user={winner['user_id']}")
    await callback.answer("🏆 Переможця визначено!")
    await send_admin_winner(callback)


async def _setting_value_unlocked(db: aiosqlite.Connection, key: str, default: str = "") -> str:
    cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cursor.fetchone()
    await cursor.close()
    return str(row["value"]) if row else default


@router.callback_query(F.data == "admin:winner:publish")
async def publish_winner(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    ticket_raw = await get_setting("winner_ticket_number", "")
    user_raw = await get_setting("winner_user_id", "")
    if not ticket_raw or not user_raw:
        await callback.answer("❌ Спочатку визначте переможця.", show_alert=True)
        return
    if await get_setting("winner_published_at", ""):
        await callback.answer("📢 Переможця вже опубліковано.", show_alert=True)
        return
    user = await get_user(int(user_raw))
    username_text = display_user(user["username"], user["first_name"], int(user_raw)) if user else f"<code>{user_raw}</code>"
    announcement = (
        "🏆 <b>ПЕРЕМОЖЕЦЬ РОЗІГРАШУ</b> 🏆\n\n"
        "🎟 Виграшний квиток: <b>№" + esc(ticket_raw) + "</b>\n"
        "👤 Переможець: " + username_text + "\n\n"
        "🎉 Вітаємо! Дякуємо всім за участь."
    )
    try:
        await callback.bot.send_message(CHANNEL_ID, announcement)
    except Exception as exc:
        await callback.answer(f"❌ Не вдалося опублікувати: {exc}", show_alert=True)
        return
    await set_setting("winner_published_at", iso(now_utc()) or "")
    await log_admin(callback.from_user.id, "winner_publish", f"ticket={ticket_raw} user={user_raw}")
    await callback.answer("📢 Опубліковано!")
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(announcement + "\n\n✅ <b>Результат опубліковано в каналі.</b>", reply_markup=back_inline("admin:panel"))


# =========================
# Admin settings
# =========================
async def send_admin_settings(target: Message | CallbackQuery) -> None:
    card = await get_setting("card_number", CARD_NUMBER_ENV)
    channel = await get_setting("channel_url", CHANNEL_URL_ENV)
    support = await get_setting("support_username", SUPPORT_USERNAME_ENV)
    coop = await get_setting("cooperation_username", COOPERATION_USERNAME_ENV)
    price = await get_int_setting("ticket_price", ENV_TICKET_PRICE)
    text = (
        "⚙️ <b>НАЛАШТУВАННЯ</b>\n\n"
        f"💳 Картка: <code>{esc(card or '—')}</code>\n"
        f"📢 Канал: <code>{esc(channel or '—')}</code>\n"
        f"💬 Підтримка: @{esc(support.lstrip('@')) if support else '—'}\n"
        f"🤝 Співпраця: @{esc(coop.lstrip('@')) if coop else '—'}\n"
        f"💰 Ціна квитка: <b>{format_money(price)}</b>\n\n"
        "Редагування нижче змінює значення в SQLite та зберігається після перезапуску."
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Змінити картку", callback_data="admin:set:card_number")],
            [InlineKeyboardButton(text="📢 Змінити URL каналу", callback_data="admin:set:channel_url")],
            [InlineKeyboardButton(text="💬 Змінити підтримку", callback_data="admin:set:support_username")],
            [InlineKeyboardButton(text="🤝 Змінити співпрацю", callback_data="admin:set:cooperation_username")],
            [InlineKeyboardButton(text="💰 Змінити ціну", callback_data="admin:set:ticket_price")],
            [InlineKeyboardButton(text="📋 Змінити правила", callback_data="admin:set:rules_text")],
            [InlineKeyboardButton(text="🏆 Змінити опис", callback_data="admin:set:about_text")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:panel")],
        ]
    )
    if isinstance(target, Message):
        await target.answer(text, reply_markup=markup)
    else:
        with suppress(TelegramBadRequest):
            await target.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "admin:settings")
async def admin_settings_callback(callback: CallbackQuery) -> None:
    if not await admin_guard(callback):
        return
    await send_admin_settings(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:set:"))
async def admin_setting_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_guard(callback):
        return
    key = callback.data.split(":", 2)[2]
    prompts = {
        "card_number": "💳 Введіть нові реквізити для оплати.",
        "channel_url": "📢 Введіть повний URL каналу, наприклад https://t.me/your_channel",
        "support_username": "💬 Введіть @username підтримки.",
        "cooperation_username": "🤝 Введіть @username для співпраці.",
        "ticket_price": "💰 Введіть нову ціну квитка цілим числом у гривнях.",
        "rules_text": "📋 Надішліть повний новий текст правил. HTML Telegram formatting дозволений.",
        "about_text": "🏆 Надішліть новий текст опису розіграшу. HTML Telegram formatting дозволений.",
    }
    if key not in prompts:
        await callback.answer("❌ Невідоме налаштування.", show_alert=True)
        return
    await state.set_state(AdminInputStates.waiting_value)
    await state.update_data(setting_key=key)
    await callback.message.answer(prompts[key] + "\n\n/cancel — скасувати.")
    await callback.answer()


@router.message(StateFilter(AdminInputStates.waiting_value))
async def admin_setting_value(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    key = data.get("setting_key")
    value = message.text or message.caption or ""
    value = value.strip()
    if key == "ticket_price":
        if not value.isdigit() or int(value) <= 0:
            await message.answer("❌ Ціна має бути додатним цілим числом.")
            return
        value = str(int(value))
    elif key in {"channel_url"}:
        if not (value.startswith("https://") or value.startswith("http://")):
            await message.answer("❌ URL повинен починатися з https:// або http://")
            return
    elif key in {"support_username", "cooperation_username"}:
        value = value.lstrip("@").strip()
    elif key in {"rules_text", "about_text"} and not value:
        await message.answer("❌ Текст не може бути порожнім.")
        return
    await set_setting(key, value)
    await log_admin(message.from_user.id, "setting_update", f"key={key}")
    await state.clear()
    await message.answer("✅ <b>Налаштування збережено.</b>")
    await send_admin_settings(message)


# =========================
# Admin payment/profile callback routing helpers
# =========================
@router.callback_query(F.data == "main")
async def back_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(await start_text(), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Головне меню", callback_data="main:done")]]))


@router.callback_query(F.data == "main:done")
async def main_done(callback: CallbackQuery) -> None:
    await callback.answer()


# =========================
# Background cleanup / app lifecycle
# =========================
async def cleanup_loop() -> None:
    while True:
        try:
            released = await cleanup_expired_reservations()
            if released:
                logger.info("Released %s expired reservations", released)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Cleanup loop error")
        await asyncio.sleep(30)


# =========================
# FastAPI health check
# =========================
app = FastAPI(title="iPhone Giveaway Bot", version="1.0.0")


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health")
async def health() -> dict[str, str]:
    if DB is None:
        return {"status": "starting"}
    try:
        await db_fetchone("SELECT 1 AS ok")
        return {"status": "ok"}
    except Exception:
        return {"status": "error"}


async def run_http_server() -> None:
    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


# =========================
# Startup / shutdown
# =========================
async def validate_config() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not ADMIN_IDS:
        missing.append("ADMIN_IDS")
    if not CHANNEL_ID_RAW:
        missing.append("CHANNEL_ID")
    if not CHANNEL_URL_ENV:
        missing.append("CHANNEL_URL")
    if not CARD_NUMBER_ENV:
        missing.append("CARD_NUMBER")
    if missing:
        raise RuntimeError("Не заповнені обов'язкові змінні середовища: " + ", ".join(missing))


async def startup() -> None:
    global BOT, BOT_USERNAME
    await validate_config()
    await init_db()
    BOT = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await BOT.get_me()
    BOT_USERNAME = me.username or ""
    await BOT.delete_webhook(drop_pending_updates=True)
    await BOT.set_my_commands(
        commands=[
            BotCommand(command="start", description="Запустити бота"),
            BotCommand(command="admin", description="Адмін-панель"),
            BotCommand(command="cancel", description="Скасувати дію"),
        ]
    )
    logger.info("Bot started as @%s", BOT_USERNAME)


async def shutdown() -> None:
    global BOT
    if BOT is not None:
        with suppress(Exception):
            await BOT.session.close()
        BOT = None
    await close_db()
    logger.info("Shutdown complete")


async def main() -> None:
    await startup()
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    cleanup_task = asyncio.create_task(cleanup_loop(), name="cleanup_loop")
    polling_task = asyncio.create_task(
        dispatcher.start_polling(BOT, allowed_updates=["message", "callback_query"]),  # type: ignore[arg-type]
        name="telegram_polling",
    )
    web_task = asyncio.create_task(run_http_server(), name="fastapi_server")
    try:
        done, pending = await asyncio.wait(
            {polling_task, web_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            with suppress(asyncio.CancelledError):
                exception = task.exception()
                if exception:
                    raise exception
        await asyncio.gather(*pending)
    finally:
        for task in (polling_task, web_task, cleanup_task):
            if not task.done():
                task.cancel()
        for task in (polling_task, web_task, cleanup_task):
            with suppress(asyncio.CancelledError, Exception):
                await task
        await shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Process stopped")
