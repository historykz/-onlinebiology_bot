import logging
import os
import asyncio
from html import escape
from typing import Final
from datetime import datetime, timedelta, timezone

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================
# НАСТРОЙКИ ИЗ ENV
# =========================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMINS_RAW = os.getenv("ADMINS", "").strip()
BOT_UTC_OFFSET = int(os.getenv("BOT_UTC_OFFSET", "5").strip())  # Казахстан по умолчанию +5

if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в Environment Variables")

if not ADMINS_RAW:
    raise ValueError("Не найден ADMINS в Environment Variables")

try:
    ADMINS: Final[set[int]] = {
        int(admin_id.strip())
        for admin_id in ADMINS_RAW.split(",")
        if admin_id.strip()
    }
except ValueError as e:
    raise ValueError(
        "ADMINS должен быть списком числовых Telegram ID через запятую"
    ) from e

TZ = timezone(timedelta(hours=BOT_UTC_OFFSET))

COURSE_BIOSPRINT = "Биоспринт"
COURSE_SECTIONS = "Разделы"

SECTION_OPTIONS = [
    "Ботаника",
    "Зоология",
    "Анатомия",
    "Генетика",
    "Молекулярная биология",
    "Эволюция и экология",
    "Прикладной курс",
]

SHIFT_SLOTS = [
    "12:00-14:00",
    "14:00-16:00",
    "16:00-18:00",
    "18:00-20:00",
    "20:00-22:00",
]

# =========================================
# ХРАНИЛИЩА В ПАМЯТИ
# =========================================

# Какому тикету отвечает админ
admin_reply_target: dict[int, int] = {}

# Профили учеников
# user_profiles[user_id] = {
#   "course": str | None,
#   "sections": list[str],
#   "survey_sent": bool,
# }
user_profiles: dict[int, dict] = {}

# known_users[user_id] = {"full_name": ..., "username": ...}
known_users: dict[int, dict] = {}

# Входящие тикеты
# tickets[ticket_id] = {
#   "user_id": int,
#   "kind": "text" | "photo" | "document",
#   "text": str,
#   "answered": bool,
#   "answered_by_id": int | None,
#   "answered_by_name": str | None,
#   "admin_messages": [{"admin_id": int, "message_id": int, "kind": "text" | "photo" | "document"}]
# }
tickets: dict[int, dict] = {}
ticket_counter = 0

# Смены
# shifts["YYYY-MM-DD"]["12:00-14:00"] = admin_id
shifts: dict[str, dict[str, int]] = {}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =========================================
# ОБЩИЕ УТИЛИТЫ
# =========================================

def now_local() -> datetime:
    return datetime.now(TZ)


def today_key() -> str:
    return now_local().strftime("%Y-%m-%d")


def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def ensure_profile(user_id: int) -> dict:
    if user_id not in user_profiles:
        user_profiles[user_id] = {
            "course": None,
            "sections": [],
            "survey_sent": False,
        }
    return user_profiles[user_id]


def get_profile_text(user_id: int) -> str:
    profile = ensure_profile(user_id)
    course = profile.get("course") or "не выбран"
    sections = profile.get("sections") or []
    sections_text = ", ".join(sections) if sections else "не выбраны"

    return (
        f"<b>Курс:</b> {escape(course)}\n"
        f"<b>Разделы:</b> {escape(sections_text)}\n"
    )


def status_text(ticket: dict) -> str:
    if ticket.get("answered"):
        admin_name = escape(ticket.get("answered_by_name") or "Неизвестно")
        return f"✅ <b>ОТВЕЧЕНО</b> админом: {admin_name}"
    return "❌ <b>НЕ ОТВЕЧЕНО</b>"


def admin_display_name(user) -> str:
    if user.full_name:
        return user.full_name
    if user.username:
        return f"@{user.username}"
    return str(user.id)


def current_shift_slot() -> str | None:
    current = now_local().time()
    for slot in SHIFT_SLOTS:
        start_str, end_str = slot.split("-")
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        start_t = current.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end_t = current.replace(hour=eh, minute=em, second=0, microsecond=0)
        if start_t <= current < end_t:
            return slot
    return None


def assigned_admin_for_current_shift() -> int | None:
    slot = current_shift_slot()
    if not slot:
        return None
    day = today_key()
    return shifts.get(day, {}).get(slot)


def ticket_reply_keyboard(ticket_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply:{ticket_id}:{user_id}")],
            [InlineKeyboardButton("🛑 Закрыть диалог", callback_data=f"close:{ticket_id}:{user_id}")],
        ]
    )


def survey_course_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("1. Биоспринт", callback_data="course:Биоспринт")],
            [InlineKeyboardButton("2. Разделы", callback_data="course:Разделы")],
        ]
    )


def sections_keyboard(selected: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for idx, section in enumerate(SECTION_OPTIONS, start=1):
        mark = "✅ " if section in selected else ""
        rows.append([InlineKeyboardButton(f"{mark}{idx}. {section}", callback_data=f"section_toggle:{section}")])
    rows.append([InlineKeyboardButton("Готово", callback_data="section_done")])
    return InlineKeyboardMarkup(rows)


def shift_keyboard() -> InlineKeyboardMarkup:
    day = today_key()
    rows = []
    for slot in SHIFT_SLOTS:
        assigned = shifts.get(day, {}).get(slot)
        assigned_text = ""
        if assigned:
            assigned_text = f" — занято {assigned}"
        rows.append([InlineKeyboardButton(f"{slot}{assigned_text}", callback_data=f"shift:{slot}")])
    rows.append([InlineKeyboardButton("Снять мою смену", callback_data="shift_clear")])
    return InlineKeyboardMarkup(rows)


def build_admin_text_message(user, user_id: int, ticket_id: int, body_text: str) -> str:
    username = f"@{escape(user.username)}" if user.username else "нет"
    full_name = escape(user.full_name or "Без имени")

    ticket = tickets[ticket_id]

    reminder = ""
    assigned_admin = assigned_admin_for_current_shift()
    if assigned_admin:
        reminder = f"\n🕐 <b>Смена назначена админу:</b> <code>{assigned_admin}</code>\n"

    return (
        f"📩 <b>Новое сообщение</b>\n\n"
        f"<b>Тикет:</b> <code>{ticket_id}</code>\n"
        f"<b>Имя:</b> {full_name}\n"
        f"<b>Username:</b> {username}\n"
        f"<b>ID:</b> <code>{user_id}</code>\n"
        f"{get_profile_text(user_id)}"
        f"<b>Статус:</b> {status_text(ticket)}"
        f"{reminder}\n\n"
        f"<b>Сообщение:</b>\n{escape(body_text)}"
    )


def build_admin_media_caption(user, user_id: int, ticket_id: int, label: str, caption_text: str) -> str:
    username = f"@{escape(user.username)}" if user.username else "нет"
    full_name = escape(user.full_name or "Без имени")

    ticket = tickets[ticket_id]

    reminder = ""
    assigned_admin = assigned_admin_for_current_shift()
    if assigned_admin:
        reminder = f"\n🕐 <b>Смена назначена админу:</b> <code>{assigned_admin}</code>\n"

    return (
        f"📩 <b>Новое сообщение</b>\n\n"
        f"<b>Тикет:</b> <code>{ticket_id}</code>\n"
        f"<b>Имя:</b> {full_name}\n"
        f"<b>Username:</b> {username}\n"
        f"<b>ID:</b> <code>{user_id}</code>\n"
        f"{get_profile_text(user_id)}"
        f"<b>Статус:</b> {status_text(ticket)}"
        f"{reminder}\n\n"
        f"<b>{label}</b>\n"
        f"<b>Подпись:</b> {escape(caption_text or 'без подписи')}"
    )


async def delete_message_later(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    delay: int = 3,
) -> None:
    try:
        await asyncio.sleep(delay)
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning("Не удалось удалить сообщение %s: %s", message_id, e)


async def send_temp_reply(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    delay: int = 3,
    parse_mode: str | None = None,
) -> None:
    try:
        sent = await message.reply_text(text, parse_mode=parse_mode)
        await delete_message_later(context, sent.chat_id, sent.message_id, delay)
    except Exception as e:
        logger.warning("Не удалось отправить временное сообщение: %s", e)


def new_ticket(user_id: int, kind: str, text: str) -> int:
    global ticket_counter
    ticket_counter += 1
    ticket_id = ticket_counter
    tickets[ticket_id] = {
        "user_id": user_id,
        "kind": kind,
        "text": text,
        "answered": False,
        "answered_by_id": None,
        "answered_by_name": None,
        "admin_messages": [],
    }
    return ticket_id


async def update_ticket_status_messages(
    context: ContextTypes.DEFAULT_TYPE,
    ticket_id: int,
    original_user,
) -> None:
    ticket = tickets.get(ticket_id)
    if not ticket:
        return

    admin_messages = ticket.get("admin_messages", [])
    user_id = ticket["user_id"]

    for item in admin_messages:
        admin_id = item["admin_id"]
        message_id = item["message_id"]
        kind = item["kind"]

        try:
            if kind == "text":
                text = build_admin_text_message(
                    user=original_user,
                    user_id=user_id,
                    ticket_id=ticket_id,
                    body_text=ticket["text"],
                )
                await context.bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=ticket_reply_keyboard(ticket_id, user_id),
                )

            elif kind == "photo":
                caption = build_admin_media_caption(
                    user=original_user,
                    user_id=user_id,
                    ticket_id=ticket_id,
                    label="Фото",
                    caption_text=ticket["text"],
                )
                await context.bot.edit_message_caption(
                    chat_id=admin_id,
                    message_id=message_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=ticket_reply_keyboard(ticket_id, user_id),
                )

            elif kind == "document":
                caption = build_admin_media_caption(
                    user=original_user,
                    user_id=user_id,
                    ticket_id=ticket_id,
                    label="Документ",
                    caption_text=ticket["text"],
                )
                await context.bot.edit_message_caption(
                    chat_id=admin_id,
                    message_id=message_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=ticket_reply_keyboard(ticket_id, user_id),
                )
        except Exception as e:
            logger.warning("Не удалось обновить статус тикета %s: %s", ticket_id, e)


async def notify_other_admins_about_admin_reply(
    context: ContextTypes.DEFAULT_TYPE,
    replying_admin,
    ticket_id: int,
    target_user_id: int,
    reply_text: str,
) -> None:
    name = escape(admin_display_name(replying_admin))
    username = f"@{escape(replying_admin.username)}" if replying_admin.username else "нет"

    text = (
        f"👨‍💼 <b>Ответ администратора</b>\n\n"
        f"<b>Тикет:</b> <code>{ticket_id}</code>\n"
        f"<b>Кто ответил:</b> {name}\n"
        f"<b>Username:</b> {username}\n"
        f"<b>ID админа:</b> <code>{replying_admin.id}</code>\n"
        f"<b>Кому ответил:</b> <code>{target_user_id}</code>\n\n"
        f"<b>Текст ответа:</b>\n{escape(reply_text)}"
    )

    for admin_id in ADMINS:
        if admin_id == replying_admin.id:
            continue
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("Не удалось уведомить админа %s: %s", admin_id, e)


async def maybe_start_survey(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = ensure_profile(user_id)
    if profile["survey_sent"]:
        return
    profile["survey_sent"] = True

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "📚 <b>К какому курсу вы относитесь?</b>\n"
                "Пожалуйста, выберите свой курс:"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=survey_course_keyboard(),
        )
    except Exception as e:
        logger.warning("Не удалось отправить опрос пользователю %s: %s", user_id, e)


# =========================================
# КОМАНДЫ
# =========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message:
        return

    if is_admin(user.id):
        await message.reply_text(
            "Вы вошли как администратор.\n\n"
            "/users — список учеников\n"
            "/id — ваш ID\n"
            "/cancel — отменить режим ответа\n"
            "/shift — выбрать смену"
        )
    else:
        ensure_profile(user.id)
        await message.reply_text(
            "Здравствуйте.\n"
            "Напишите ваше сообщение, и администрация увидит его анонимно."
        )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if update.message and user:
        await update.message.reply_text(f"Ваш Telegram ID: {user.id}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message or not is_admin(user.id):
        return
    admin_reply_target.pop(user.id, None)
    await message.reply_text("Режим ответа отменён.")


async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message or not is_admin(user.id):
        return

    if not known_users:
        await message.reply_text("Пока никто не писал.")
        return

    lines = ["📋 <b>Пользователи:</b>\n"]
    for uid, data in known_users.items():
        full_name = escape(data.get("full_name", "Без имени"))
        username = data.get("username")
        username_text = f"@{escape(username)}" if username else "нет"
        profile_info = get_profile_text(uid)
        lines.append(f"• {full_name} | {username_text} | <code>{uid}</code>\n{profile_info}")

    await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def shift_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message or not is_admin(user.id):
        return

    await message.reply_text(
        f"🕐 Выберите смену на сегодня ({today_key()}):",
        reply_markup=shift_keyboard(),
    )


# =========================================
# CALLBACK
# =========================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    user = query.from_user
    data = query.data or ""

    # ----- ОПРОС: КУРС -----
    if data.startswith("course:"):
        course = data.split(":", 1)[1]
        profile = ensure_profile(user.id)
        profile["course"] = course

        if course == COURSE_BIOSPRINT:
            await query.message.reply_text("✅ Курс сохранён: Биоспринт")
        else:
            await query.message.reply_text(
                "✅ Курс сохранён: Разделы\n\n"
                "Теперь выберите ваш раздел/ы:",
                reply_markup=sections_keyboard(profile["sections"]),
            )
        return

    # ----- ОПРОС: РАЗДЕЛЫ -----
    if data.startswith("section_toggle:"):
        section = data.split(":", 1)[1]
        profile = ensure_profile(user.id)
        selected = profile["sections"]

        if section in selected:
            selected.remove(section)
        else:
            selected.append(section)

        await query.message.edit_reply_markup(reply_markup=sections_keyboard(selected))
        return

    if data == "section_done":
        profile = ensure_profile(user.id)
        sections = profile["sections"]
        text = "✅ Разделы сохранены:\n" + ("\n".join(f"• {s}" for s in sections) if sections else "ничего не выбрано")
        await query.message.reply_text(text)
        return

    # ----- СМЕНЫ -----
    if data.startswith("shift:"):
        if not is_admin(user.id):
            await query.message.reply_text("Нет доступа.")
            return

        slot = data.split(":", 1)[1]
        day = today_key()
        shifts.setdefault(day, {})
        shifts[day][slot] = user.id

        await query.message.reply_text(f"✅ Ваша смена назначена: {slot}")
        return

    if data == "shift_clear":
        if not is_admin(user.id):
            await query.message.reply_text("Нет доступа.")
            return

        day = today_key()
        if day in shifts:
            slots_to_remove = [slot for slot, admin_id in shifts[day].items() if admin_id == user.id]
            for slot in slots_to_remove:
                shifts[day].pop(slot, None)

        await query.message.reply_text("✅ Ваша смена снята.")
        return

    # ----- КНОПКИ АДМИНА -----
    if not is_admin(user.id):
        if query.message:
            await query.message.reply_text("Нет доступа.")
        return

    if data.startswith("reply:"):
        _, ticket_id_str, user_id_str = data.split(":")
        ticket_id = int(ticket_id_str)
        user_id = int(user_id_str)
        admin_reply_target[user.id] = ticket_id

        msg = await query.message.reply_text(
            f"Теперь вы отвечаете по тикету <code>{ticket_id}</code> пользователю <code>{user_id}</code>.",
            parse_mode=ParseMode.HTML,
        )
        await delete_message_later(context, msg.chat_id, msg.message_id, 3)
        return

    if data.startswith("close:"):
        _, ticket_id_str, user_id_str = data.split(":")
        ticket_id = int(ticket_id_str)
        user_id = int(user_id_str)

        if admin_reply_target.get(user.id) == ticket_id:
            admin_reply_target.pop(user.id, None)

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="Диалог завершён. При необходимости можете написать снова.",
            )
        except Exception:
            pass

        msg = await query.message.reply_text(
            f"Диалог по тикету <code>{ticket_id}</code> закрыт.",
            parse_mode=ParseMode.HTML,
        )
        await delete_message_later(context, msg.chat_id, msg.message_id, 3)
        return


# =========================================
# СООБЩЕНИЯ
# =========================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not user or not message.text:
        return

    # ------- ОТВЕТ АДМИНА -------
    if is_admin(user.id):
        ticket_id = admin_reply_target.get(user.id)
        if not ticket_id:
            await message.reply_text("Сначала нажмите «Ответить» под сообщением ученика.")
            return

        ticket = tickets.get(ticket_id)
        if not ticket:
            await message.reply_text("Тикет не найден.")
            return

        target_user_id = ticket["user_id"]

        try:
            await context.bot.send_message(chat_id=target_user_id, text=message.text)

            ticket["answered"] = True
            ticket["answered_by_id"] = user.id
            ticket["answered_by_name"] = admin_display_name(user)

            fake_user = type("Obj", (), {
                "username": known_users.get(target_user_id, {}).get("username"),
                "full_name": known_users.get(target_user_id, {}).get("full_name", "Без имени"),
            })()

            await update_ticket_status_messages(context, ticket_id, fake_user)

            await notify_other_admins_about_admin_reply(
                context=context,
                replying_admin=user,
                ticket_id=ticket_id,
                target_user_id=target_user_id,
                reply_text=message.text,
            )

            await send_temp_reply(message, context, "Ответ отправлен анонимно.", 3)
        except Exception as e:
            logger.error("Ошибка отправки ответа: %s", e)
            await message.reply_text("Не удалось отправить ответ.")
        return

    # ------- СООБЩЕНИЕ УЧЕНИКА -------
    known_users[user.id] = {
        "full_name": user.full_name,
        "username": user.username,
    }
    ensure_profile(user.id)

    ticket_id = new_ticket(user.id, "text", message.text)
    text = build_admin_text_message(user, user.id, ticket_id, message.text)

    for admin_id in ADMINS:
        try:
            sent = await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=ticket_reply_keyboard(ticket_id, user.id),
            )
            tickets[ticket_id]["admin_messages"].append({
                "admin_id": admin_id,
                "message_id": sent.message_id,
                "kind": "text",
            })

            assigned_admin = assigned_admin_for_current_shift()
            if assigned_admin == admin_id:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text="🕐 Сейчас ваша смена — ответьте: админ ✍️"
                )
        except Exception as e:
            logger.warning("Не удалось отправить админу %s: %s", admin_id, e)

    await send_temp_reply(message, context, "Ваше сообщение отправлено администрации.", 3)
    await maybe_start_survey(user.id, context)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not user or not message.photo:
        return

    # ------- ОТВЕТ АДМИНА ФОТО -------
    if is_admin(user.id):
        ticket_id = admin_reply_target.get(user.id)
        if not ticket_id:
            await message.reply_text("Сначала выберите диалог кнопкой «Ответить».")
            return

        ticket = tickets.get(ticket_id)
        if not ticket:
            await message.reply_text("Тикет не найден.")
            return

        target_user_id = ticket["user_id"]

        try:
            await context.bot.send_photo(
                chat_id=target_user_id,
                photo=message.photo[-1].file_id,
                caption=message.caption or "",
            )

            ticket["answered"] = True
            ticket["answered_by_id"] = user.id
            ticket["answered_by_name"] = admin_display_name(user)

            fake_user = type("Obj", (), {
                "username": known_users.get(target_user_id, {}).get("username"),
                "full_name": known_users.get(target_user_id, {}).get("full_name", "Без имени"),
            })()

            await update_ticket_status_messages(context, ticket_id, fake_user)

            await notify_other_admins_about_admin_reply(
                context=context,
                replying_admin=user,
                ticket_id=ticket_id,
                target_user_id=target_user_id,
                reply_text=f"[Фото] {message.caption or 'без подписи'}",
            )

            await send_temp_reply(message, context, "Фото отправлено анонимно.", 3)
        except Exception as e:
            logger.error("Ошибка отправки фото: %s", e)
            await message.reply_text("Не удалось отправить фото.")
        return

    # ------- ФОТО ОТ УЧЕНИКА -------
    known_users[user.id] = {
        "full_name": user.full_name,
        "username": user.username,
    }
    ensure_profile(user.id)

    caption_text = message.caption or ""
    ticket_id = new_ticket(user.id, "photo", caption_text)
    caption = build_admin_media_caption(user, user.id, ticket_id, "Фото", caption_text)

    for admin_id in ADMINS:
        try:
            sent = await context.bot.send_photo(
                chat_id=admin_id,
                photo=message.photo[-1].file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=ticket_reply_keyboard(ticket_id, user.id),
            )
            tickets[ticket_id]["admin_messages"].append({
                "admin_id": admin_id,
                "message_id": sent.message_id,
                "kind": "photo",
            })

            assigned_admin = assigned_admin_for_current_shift()
            if assigned_admin == admin_id:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text="🕐 Сейчас ваша смена — ответьте: админ ✍️"
                )
        except Exception as e:
            logger.warning("Не удалось отправить фото админу %s: %s", admin_id, e)

    await send_temp_reply(message, context, "Фото отправлено администрации.", 3)
    await maybe_start_survey(user.id, context)


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not user or not message.document:
        return

    # ------- ОТВЕТ АДМИНА ДОКУМЕНТОМ -------
    if is_admin(user.id):
        ticket_id = admin_reply_target.get(user.id)
        if not ticket_id:
            await message.reply_text("Сначала выберите диалог кнопкой «Ответить».")
            return

        ticket = tickets.get(ticket_id)
        if not ticket:
            await message.reply_text("Тикет не найден.")
            return

        target_user_id = ticket["user_id"]

        try:
            await context.bot.send_document(
                chat_id=target_user_id,
                document=message.document.file_id,
                caption=message.caption or "",
            )

            ticket["answered"] = True
            ticket["answered_by_id"] = user.id
            ticket["answered_by_name"] = admin_display_name(user)

            fake_user = type("Obj", (), {
                "username": known_users.get(target_user_id, {}).get("username"),
                "full_name": known_users.get(target_user_id, {}).get("full_name", "Без имени"),
            })()

            await update_ticket_status_messages(context, ticket_id, fake_user)

            await notify_other_admins_about_admin_reply(
                context=context,
                replying_admin=user,
                ticket_id=ticket_id,
                target_user_id=target_user_id,
                reply_text=f"[Документ] {message.document.file_name or 'без имени'} | {message.caption or 'без подписи'}",
            )

            await send_temp_reply(message, context, "Документ отправлен анонимно.", 3)
        except Exception as e:
            logger.error("Ошибка отправки документа: %s", e)
            await message.reply_text("Не удалось отправить документ.")
        return

    # ------- ДОКУМЕНТ ОТ УЧЕНИКА -------
    known_users[user.id] = {
        "full_name": user.full_name,
        "username": user.username,
    }
    ensure_profile(user.id)

    caption_text = message.caption or message.document.file_name or ""
    ticket_id = new_ticket(user.id, "document", caption_text)
    caption = build_admin_media_caption(user, user.id, ticket_id, "Документ", caption_text)

    for admin_id in ADMINS:
        try:
            sent = await context.bot.send_document(
                chat_id=admin_id,
                document=message.document.file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=ticket_reply_keyboard(ticket_id, user.id),
            )
            tickets[ticket_id]["admin_messages"].append({
                "admin_id": admin_id,
                "message_id": sent.message_id,
                "kind": "document",
            })

            assigned_admin = assigned_admin_for_current_shift()
            if assigned_admin == admin_id:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text="🕐 Сейчас ваша смена — ответьте: админ ✍️"
                )
        except Exception as e:
            logger.warning("Не удалось отправить документ админу %s: %s", admin_id, e)

    await send_temp_reply(message, context, "Документ отправлен администрации.", 3)
    await maybe_start_survey(user.id, context)


# =========================================
# ЗАПУСК
# =========================================

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("users", users_list))
    app.add_handler(CommandHandler("shift", shift_command))

    app.add_handler(CallbackQueryHandler(callback_handler))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))

    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
