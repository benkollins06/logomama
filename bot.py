"""Telegram subscription gate with an in-Telegram admin panel.

Python 3.10+, aiogram 3.x. No external web panel is required.
Automatically approves join requests for enabled, configured channels.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatJoinRequest, InlineKeyboardMarkup, Message, MessageOriginChannel
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/gate.sqlite3"))
DEFAULT_TEXTS = {
    "welcome_text": (
        "👋 Добро пожаловать в официальный бот Маргариты Макайды!\n\n"
        "Чтобы получить доступ к обучающим материалам, подайте заявку "
        "на вступление в закрытый канал. После одобрения вернитесь сюда "
        "и нажмите «Проверить подписку»."
    ),
    "success_text": "✅ Подписка подтверждена! Доступ открыт.",
    "pending_text": (
        "⏳ Подписка пока не подтверждена. Если вы отправили заявку, "
        "дождитесь одобрения администратором канала и повторите проверку."
    ),
    "check_button": "✅ Проверить подписку",
    "content_url": "",
}
EDIT_LABELS = {
    "welcome_text": "Приветствие",
    "success_text": "Текст после проверки",
    "pending_text": "Текст ожидания",
    "check_button": "Надпись на кнопке проверки",
    "content_url": "Ссылка на материалы (необязательно)",
}
router = Router()


@contextmanager
def database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_database() -> None:
    with database() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                invite_url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                verified_at TEXT
            );
            CREATE TABLE IF NOT EXISTS last_bot_messages (
                chat_id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL
            );
            """
        )


def get_setting(key: str) -> str:
    if key not in DEFAULT_TEXTS:
        raise KeyError(key)
    with database() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_TEXTS[key]


def set_setting(key: str, value: str) -> None:
    if key not in DEFAULT_TEXTS:
        raise KeyError(key)
    with database() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def list_channels(only_enabled: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM channels"
    if only_enabled:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    with database() as conn:
        return conn.execute(sql).fetchall()


def get_channel(channel_id: int) -> sqlite3.Row | None:
    with database() as conn:
        return conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()


def add_channel(chat_id: str, title: str, invite_url: str) -> None:
    with database() as conn:
        conn.execute(
            "INSERT INTO channels(chat_id,title,invite_url) VALUES(?,?,?)",
            (chat_id, title, invite_url),
        )


def update_channel(channel_id: int, field: str, value: str) -> None:
    if field not in {"chat_id", "title", "invite_url"}:
        raise ValueError("Invalid channel field")
    with database() as conn:
        conn.execute(f"UPDATE channels SET {field}=? WHERE id=?", (value, channel_id))


def toggle_channel(channel_id: int) -> None:
    with database() as conn:
        conn.execute(
            "UPDATE channels SET enabled=1-enabled WHERE id=?", (channel_id,)
        )


def delete_channel(channel_id: int) -> None:
    with database() as conn:
        conn.execute("DELETE FROM channels WHERE id=?", (channel_id,))


def record_user(user_id: int) -> None:
    with database() as conn:
        conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))


def mark_verified(user_id: int) -> None:
    with database() as conn:
        conn.execute(
            "INSERT INTO users(user_id, verified_at) VALUES(?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(user_id) DO UPDATE SET verified_at=CURRENT_TIMESTAMP",
            (user_id,),
        )


def get_stats() -> tuple[int, int]:
    with database() as conn:
        total, ever_verified = conn.execute(
            "SELECT COUNT(*), COUNT(verified_at) FROM users"
        ).fetchone()
    return total, ever_verified


def admin_ids() -> set[int]:
    result = set()
    for part in os.getenv("ADMIN_IDS", "").split(","):
        part = part.strip()
        if part:
            result.add(int(part))
    return result


def is_admin(user_id: int) -> bool:
    return user_id in admin_ids()


def normalize_chat_id(value: str) -> str | None:
    value = value.strip()
    if re.fullmatch(r"-?\d{5,20}", value):
        return str(int(value))
    if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", value):
        return value
    return None


def valid_url(value: str, *, telegram_only: bool = False) -> bool:
    if not value or len(value) > 1900 or any(ch.isspace() for ch in value):
        return False
    try:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        if telegram_only:
            return parsed.hostname.lower() in {"t.me", "telegram.me", "telegram.dog"} and bool(parsed.path.strip("/"))
        return True
    except ValueError:
        return False


def to_telegram_chat_id(chat_id: str) -> int | str:
    return int(chat_id) if re.fullmatch(r"-?\d+", chat_id) else chat_id


def subscription_markup(channels: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for channel in channels:
        kb.button(text=f"📢 {channel['title']}", url=channel["invite_url"])
    kb.button(text=get_setting("check_button"), callback_data="gate:check")
    kb.adjust(1)
    return kb.as_markup()


def content_markup() -> InlineKeyboardMarkup | None:
    url = get_setting("content_url")
    if not url:
        return None
    kb = InlineKeyboardBuilder()
    kb.button(text="📚 Открыть материалы", url=url)
    kb.adjust(1)
    return kb.as_markup()


async def membership_status(bot: Bot, channel: sqlite3.Row, user_id: int) -> bool:
    member = await bot.get_chat_member(
        chat_id=to_telegram_chat_id(channel["chat_id"]), user_id=user_id
    )
    if member.status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    }:
        return True
    return member.status == ChatMemberStatus.RESTRICTED and bool(
        getattr(member, "is_member", False)
    )


async def is_bot_admin_in_channel(bot: Bot, chat_id: str) -> bool:
    me = await bot.get_me()
    member = await bot.get_chat_member(
        chat_id=to_telegram_chat_id(chat_id), user_id=me.id
    )
    return member.status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
    }


async def check_access(bot: Bot, user_id: int) -> tuple[str, list[sqlite3.Row]]:
    """Returns ('ok' | 'pending' | 'error' | 'unconfigured', missing_channels).

    Fail closed: a Telegram API failure never gives access.
    """
    channels = list_channels(only_enabled=True)
    if not channels:
        return "unconfigured", []
    missing: list[sqlite3.Row] = []
    had_error = False
    for channel in channels:
        try:
            if not await membership_status(bot, channel, user_id):
                missing.append(channel)
        except TelegramAPIError:
            logging.exception("Unable to check channel id=%s", channel["id"])
            had_error = True
    if had_error:
        return "error", missing
    if missing:
        return "pending", missing
    return "ok", []


async def send_clean_message(bot: Bot, chat_id: int, text: str, **kwargs) -> None:
    """Keep only the newest tracked bot message in a user's private chat.

    The API cannot reliably remove messages older than 48 hours, or messages
    sent before this version started tracking them. Never delete user messages.
    """
    # Send first: if Telegram rejects it, the previous usable button survives.
    sent = await bot.send_message(chat_id, text, **kwargs)
    with database() as conn:
        old = conn.execute(
            "SELECT message_id FROM last_bot_messages WHERE chat_id=?", (chat_id,)
        ).fetchone()
        conn.execute(
            "INSERT INTO last_bot_messages(chat_id, message_id) VALUES(?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET message_id=excluded.message_id",
            (chat_id, sent.message_id),
        )
    if old and old["message_id"] != sent.message_id:
        try:
            await bot.delete_message(chat_id, old["message_id"])
        except TelegramAPIError:
            logging.info("Old message cannot be deleted for chat %s", chat_id)


async def send_welcome(bot: Bot, user_id: int) -> None:
    channels = list_channels(only_enabled=True)
    if not channels:
        await send_clean_message(bot, user_id, "🛠 Бот пока настраивается. Попробуйте позже.")
        return
    await send_clean_message(bot, 
        user_id,
        get_setting("welcome_text"),
        reply_markup=subscription_markup(channels),
    )


async def send_access_result(bot: Bot, user_id: int) -> None:
    status, missing = await check_access(bot, user_id)
    if status == "unconfigured":
        await send_clean_message(bot, user_id, "🛠 Бот пока настраивается. Попробуйте позже.")
    elif status == "error":
        await send_clean_message(bot, 
            user_id,
            "⚠️ Сейчас не удаётся проверить подписку. Попробуйте позже. "
            "Если ошибка повторяется, сообщите администратору.",
        )
    elif status == "pending":
        await send_clean_message(bot, 
            user_id,
            get_setting("pending_text"),
            reply_markup=subscription_markup(missing),
        )
    else:
        mark_verified(user_id)
        await send_clean_message(bot, 
            user_id, get_setting("success_text"), reply_markup=content_markup()
        )


@router.chat_join_request()
async def approve_configured_channel_request(request: ChatJoinRequest, bot: Bot) -> None:
    """Approve join requests only for channels enabled in the admin panel."""
    configured_chat_ids = {
        channel["chat_id"] for channel in list_channels(only_enabled=True)
    }
    if str(request.chat.id) not in configured_chat_ids:
        logging.info("Ignored join request for unconfigured channel %s", request.chat.id)
        return
    try:
        await bot.approve_chat_join_request(
            chat_id=request.chat.id, user_id=request.from_user.id
        )
        logging.info(
            "Approved join request: channel=%s user=%s",
            request.chat.id, request.from_user.id,
        )
    except TelegramAPIError:
        logging.exception(
            "Could not approve join request: channel=%s user=%s",
            request.chat.id, request.from_user.id,
        )
        return

    # Congratulate only after Telegram confirms approval and all required
    # subscriptions have actually been verified.
    try:
        status, _ = await check_access(bot, request.from_user.id)
        if status == "ok":
            record_user(request.from_user.id)
            mark_verified(request.from_user.id)
            await send_clean_message(bot, 
                request.from_user.id,
                "🎉 Поздравляем! Ваша заявка одобрена, и теперь у вас есть "
                "доступ к закрытому каналу! Добро пожаловать! ✅",
                reply_markup=content_markup(),
            )
        else:
            logging.info(
                "Join approved but access not fully verified: user=%s status=%s",
                request.from_user.id, status,
            )
    except TelegramAPIError:
        # Telegram may refuse a private message if the user never opened the bot.
        # Approval still succeeds; the user can use /materials to check access.
        logging.exception("Cannot notify approved user %s", request.from_user.id)


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    if message.from_user is None:
        return
    record_user(message.from_user.id)
    await send_welcome(bot, message.from_user.id)


@router.message(Command("id"), F.chat.type == ChatType.PRIVATE)
async def show_user_id(message: Message) -> None:
    if message.from_user:
        await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@router.message(Command("materials"), F.chat.type == ChatType.PRIVATE)
async def materials(message: Message, bot: Bot) -> None:
    if message.from_user:
        await send_access_result(bot, message.from_user.id)


@router.callback_query(F.data == "gate:check")
async def check_subscription(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer("Проверяю подписку…")
    await send_access_result(bot, callback.from_user.id)


class SettingEdit(StatesGroup):
    waiting = State()


class ChannelAdd(StatesGroup):
    chat_id = State()
    title = State()
    link = State()


class ChannelEdit(StatesGroup):
    waiting = State()


def admin_home_markup() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key in ("welcome_text", "success_text", "pending_text", "check_button", "content_url"):
        kb.button(text=f"✏️ {EDIT_LABELS[key]}", callback_data=f"adm:edit:{key}")
    kb.button(text="📢 Каналы", callback_data="adm:channels")
    kb.button(text="👁 Предпросмотр", callback_data="adm:preview")
    kb.button(text="📊 Статистика", callback_data="adm:stats")
    kb.adjust(1)
    return kb.as_markup()


def back_markup(target: str = "adm:home") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=target)
    return kb.as_markup()


async def send_admin_home(bot: Bot, user_id: int) -> None:
    channels = list_channels()
    await bot.send_message(
        user_id,
        "⚙️ Админ-панель\n"
        f"Каналов: {len(channels)} (активных: {sum(bool(c['enabled']) for c in channels)}).\n\n"
        "Здесь можно поменять тексты, кнопки и ссылки без редактирования кода.",
        reply_markup=admin_home_markup(),
    )


def channels_markup() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c in list_channels():
        symbol = "🟢" if c["enabled"] else "⚪️"
        kb.button(text=f"{symbol} {c['title']}", callback_data=f"adm:channel:{c['id']}")
    kb.button(text="➕ Добавить канал", callback_data="adm:add")
    kb.button(text="⬅️ В админ-панель", callback_data="adm:home")
    kb.adjust(1)
    return kb.as_markup()


async def send_channels(bot: Bot, user_id: int) -> None:
    await bot.send_message(
        user_id,
        "📢 Каналы для обязательной подписки\n"
        "Пользователь должен состоять во ВСЕХ активных каналах.\n"
        "Отключённые каналы не проверяются.",
        reply_markup=channels_markup(),
    )


async def send_channel_detail(bot: Bot, user_id: int, channel_id: int) -> None:
    channel = get_channel(channel_id)
    if channel is None:
        await bot.send_message(user_id, "Канал уже удалён.", reply_markup=back_markup("adm:channels"))
        return
    kb = InlineKeyboardBuilder()
    for field, label in (
        ("title", "✏️ Название"),
        ("chat_id", "✏️ ID канала"),
        ("invite_url", "✏️ Ссылка на вступление"),
    ):
        kb.button(text=label, callback_data=f"adm:c_edit:{channel_id}:{field}")
    kb.button(
        text="⏸ Отключить" if channel["enabled"] else "▶️ Включить",
        callback_data=f"adm:c_toggle:{channel_id}",
    )
    kb.button(text="🗑 Удалить", callback_data=f"adm:c_delete:{channel_id}")
    kb.button(text="⬅️ К каналам", callback_data="adm:channels")
    kb.adjust(1)
    await bot.send_message(
        user_id,
        f"📢 {channel['title']}\n"
        f"ID: {channel['chat_id']}\n"
        f"Ссылка: {channel['invite_url']}\n"
        f"Статус: {'активен' if channel['enabled'] else 'отключён'}",
        reply_markup=kb.as_markup(),
        disable_web_page_preview=True,
    )


@router.message(Command("admin"), F.chat.type == ChatType.PRIVATE)
async def admin_command(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    await state.clear()
    await send_admin_home(bot, message.from_user.id)


@router.message(Command("cancel"), F.chat.type == ChatType.PRIVATE)
async def cancel(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Действие отменено.")
        await send_admin_home(bot, message.from_user.id)


def extract_chat_id(message: Message) -> str | None:
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        return str(origin.chat.id)
    return normalize_chat_id(message.text or "")


@router.callback_query(F.data.startswith("adm:"))
async def admin_callback(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    await callback.answer()
    uid = callback.from_user.id
    data = callback.data or ""

    if data == "adm:home":
        await state.clear()
        await send_admin_home(bot, uid)
    elif data == "adm:channels":
        await state.clear()
        await send_channels(bot, uid)
    elif data == "adm:preview":
        await state.clear()
        await send_welcome(bot, uid)
        await bot.send_message(uid, "👆 Так выглядит сообщение для пользователя.", reply_markup=back_markup())
    elif data == "adm:stats":
        await state.clear()
        total, verified = get_stats()
        await bot.send_message(
            uid,
            f"📊 Пользователей, запустивших бота: {total}\n"
            f"Когда-либо прошли проверку: {verified}\n\n"
            "Это не текущее количество участников канала.",
            reply_markup=back_markup(),
        )
    elif data.startswith("adm:edit:"):
        key = data.removeprefix("adm:edit:")
        if key not in EDIT_LABELS:
            await bot.send_message(uid, "Неизвестная настройка.")
            return
        await state.set_state(SettingEdit.waiting)
        await state.update_data(key=key)
        extra = "\nОтправьте - для удаления ссылки." if key == "content_url" else ""
        await bot.send_message(
            uid,
            f"✏️ {EDIT_LABELS[key]}\n\nСейчас:\n{get_setting(key) or '(не задано)'}\n\n"
            f"Отправьте новое значение одним сообщением.{extra}\n/cancel — отмена.",
        )
    elif data == "adm:add":
        await state.set_state(ChannelAdd.chat_id)
        await bot.send_message(
            uid,
            "➕ Новый канал.\n\n"
            "Отправьте ID канала (например, -1001234567890) "
            "или перешлите пост из канала, если пересылка разрешена.\n"
            "Для публичного канала можно @username.\n/cancel — отмена.",
        )
    elif data.startswith("adm:channel:"):
        channel_id = parse_channel_number(data, "adm:channel:")
        if channel_id:
            await send_channel_detail(bot, uid, channel_id)
    elif data.startswith("adm:c_edit:"):
        match = re.fullmatch(r"adm:c_edit:(\d+):(chat_id|title|invite_url)", data)
        if not match or get_channel(int(match[1])) is None:
            await bot.send_message(uid, "Канал не найден.")
            return
        channel_id, field = int(match[1]), match[2]
        await state.set_state(ChannelEdit.waiting)
        await state.update_data(channel_id=channel_id, field=field)
        await bot.send_message(
            uid,
            f"Отправьте новое значение поля «{field}» одним сообщением.\n"
            "/cancel — отмена.",
        )
    elif data.startswith("adm:c_toggle:"):
        channel_id = parse_channel_number(data, "adm:c_toggle:")
        if channel_id and get_channel(channel_id):
            await state.clear()
            toggle_channel(channel_id)
            await send_channel_detail(bot, uid, channel_id)
    elif data.startswith("adm:c_delete:"):
        channel_id = parse_channel_number(data, "adm:c_delete:")
        if channel_id and (channel := get_channel(channel_id)):
            kb = InlineKeyboardBuilder()
            kb.button(text="Да, удалить", callback_data=f"adm:c_del_yes:{channel_id}")
            kb.button(text="Отмена", callback_data=f"adm:channel:{channel_id}")
            kb.adjust(1)
            await bot.send_message(
                uid, f"Удалить канал «{channel['title']}» из проверки?", reply_markup=kb.as_markup()
            )
    elif data.startswith("adm:c_del_yes:"):
        channel_id = parse_channel_number(data, "adm:c_del_yes:")
        if channel_id:
            await state.clear()
            delete_channel(channel_id)
            await bot.send_message(uid, "Канал удалён из настроек бота.")
            await send_channels(bot, uid)
    else:
        await bot.send_message(uid, "Неизвестное действие. Нажмите /admin.")


def parse_channel_number(data: str, prefix: str) -> int | None:
    suffix = data.removeprefix(prefix)
    return int(suffix) if suffix.isdigit() else None


@router.message(SettingEdit.waiting, F.chat.type == ChatType.PRIVATE)
async def receive_setting(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("key")
    if key not in EDIT_LABELS:
        await state.clear()
        return
    value = (message.text or "").strip()
    if key == "content_url":
        if value == "-":
            value = ""
        elif not valid_url(value):
            await message.answer("Нужна полная HTTPS-ссылка, например https://example.com/materials")
            return
    elif key == "check_button":
        if not 1 <= len(value) <= 60:
            await message.answer("Название кнопки должно содержать 1–60 символов.")
            return
    elif not 1 <= len(value) <= 3500:
        await message.answer("Введите текст длиной от 1 до 3500 символов.")
        return
    set_setting(key, value)
    await state.clear()
    await message.answer("✅ Настройка сохранена.")
    await send_admin_home(bot, message.from_user.id)


@router.message(ChannelAdd.chat_id, F.chat.type == ChatType.PRIVATE)
async def receive_channel_id(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    chat_id = extract_chat_id(message)
    if not chat_id:
        await message.answer("Не распознал ID. Пришлите -100... или перешлите пост из канала.")
        return
    await state.update_data(chat_id=chat_id)
    await state.set_state(ChannelAdd.title)
    await message.answer("Как назвать канал на кнопке? Например: «Обучающие материалы».")


@router.message(ChannelAdd.title, F.chat.type == ChatType.PRIVATE)
async def receive_channel_title(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    title = (message.text or "").strip()
    if not 1 <= len(title) <= 60:
        await message.answer("Название должно содержать 1–60 символов.")
        return
    await state.update_data(title=title)
    await state.set_state(ChannelAdd.link)
    await message.answer(
        "Пришлите HTTPS-ссылку для вступления в канал.\n"
        "Для закрытого канала создайте пригласительную ссылку с одобрением заявок "
        "и пришлите её (https://t.me/+...)."
    )


@router.message(ChannelAdd.link, F.chat.type == ChatType.PRIVATE)
async def receive_channel_link(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    link = (message.text or "").strip()
    if not valid_url(link, telegram_only=True):
        await message.answer("Нужна корректная ссылка t.me на канал (HTTPS).")
        return
    data = await state.get_data()
    chat_id = data["chat_id"]
    try:
        allowed = await is_bot_admin_in_channel(bot, chat_id)
    except TelegramAPIError:
        logging.exception("Cannot check bot admin rights for %s", chat_id)
        allowed = False
    if not allowed:
        await message.answer(
            "⚠️ Бот не является администратором указанного канала или ID неверный.\n"
            "Добавьте бота в администраторы канала и снова пришлите ссылку."
        )
        return
    try:
        add_channel(chat_id, data["title"], link)
    except sqlite3.IntegrityError:
        await message.answer("Такой канал уже добавлен. Измените его в разделе «Каналы».")
        await state.clear()
        await send_channels(bot, message.from_user.id)
        return
    await state.clear()
    await message.answer("✅ Канал подключён. Новые заявки будут одобряться автоматически, пока бот запущен.")
    await send_channels(bot, message.from_user.id)


@router.message(ChannelEdit.waiting, F.chat.type == ChatType.PRIVATE)
async def receive_channel_change(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    channel_id, field = data.get("channel_id"), data.get("field")
    if not isinstance(channel_id, int) or field not in {"chat_id", "title", "invite_url"}:
        await state.clear()
        return
    if get_channel(channel_id) is None:
        await state.clear()
        await message.answer("Канал был удалён.")
        return
    value = (message.text or "").strip()
    if field == "title":
        if not 1 <= len(value) <= 60:
            await message.answer("Название должно содержать 1–60 символов.")
            return
    elif field == "invite_url":
        if not valid_url(value, telegram_only=True):
            await message.answer("Нужна HTTPS-ссылка t.me на канал.")
            return
    else:
        value = extract_chat_id(message) or ""
        if not value:
            await message.answer("Нужен числовой ID (-100...) или @username.")
            return
        try:
            allowed = await is_bot_admin_in_channel(bot, value)
        except TelegramAPIError:
            allowed = False
        if not allowed:
            await message.answer("Бот должен быть администратором нового канала.")
            return
    try:
        update_channel(channel_id, field, value)
    except sqlite3.IntegrityError:
        await message.answer("Этот ID уже используется другим каналом.")
        return
    await state.clear()
    await message.answer("✅ Канал обновлён.")
    await send_channel_detail(bot, message.from_user.id, channel_id)


async def main() -> None:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "PASTE_BOT_TOKEN_HERE":
        raise SystemExit("Укажите BOT_TOKEN в .env")
    if not admin_ids():
        raise SystemExit("Укажите ADMIN_IDS в .env")
    init_database()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
