import logging
import os
import asyncio
from html import escape
from typing import Final

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

# =========================================
# ДАННЫЕ В ПАМЯТИ
# =========================================

# Какому пользователю сейчас отвечает конкретный админ
admin_reply_target: dict[int, int] = {}

# Кто из учеников уже писал
known_users: dict[int, dict] = {}

# Анкета пользователя
# user_profiles[user_id] = {
#   "course": "Биоспринт" | "Разделы" | None,
#   "sections": [..],
#   "survey_sent": bool,
#   "survey_done": bool,
# }
user_profiles: dict[int, dict] = {}

SECTION_OPTIONS = [
    "Ботаника",
    "Зоология",
    "Анатомия",
    "Генетика",
    "Молекулярная биология",
    "Эволюция и экология",
    "Прикладной курс",
]

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def ensure_profile(user_id: int) -> dict:
    if user_id not in user_profiles:
        user_profiles[user_id] = {
            "course": None,
            "sections": [],
            "survey_sent": False,
            "survey_done": False,
        }
    return user_profiles[user_id]


def profile_text(user_id: int) -> str:
    profile = ensure_profile(user_id)

    course = profile.get("course")
    sections = profile.get("sections", [])

    if not course:
        return "<b>Курс:</b> не выбран\n"

    if course == "Биоспринт":
        return "<b>Курс:</b> Биоспринт\n"

    sections_text = ", ".join(sections) if sections else "не выбраны"
    return (
        "<b>Курс:</b> Разделы\n"
        f"<b>Разделы:</b> {escape(sections_text)}\n"
    )


def user_card(user) -> str:
    first_name = escape(user.first_name or "")
    last_name = escape(user.last_name or "")
    full_name = f"{first_name} {last_name}".strip() or "Без имени"

    username = f"@{escape(user.username)}" if user.username else "нет"
    nick = escape(user.full_name) if getattr(user, "full_name", None) else full_name

    return (
        f"👤 <b>Ученик</b>\n"
        f"<b>Имя:</b> {full_name}\n"
        f"<b>Ник:</b> {nick}\n"
        f"<b>Username:</b> {username}\n"
        f"<b>ID:</b> <code>{user.id}</code>\n"
        f"{profile_text(user.id)}"
    )


def reply_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply:{user_id}")],
            [InlineKeyboardButton("🛑 Закрыть диалог", callback_data=f"close:{user_id}")],
        ]
    )


def course_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("1️⃣ Биоспринт", callback_data="course:Биоспринт")],
            [InlineKeyboardButton("2️⃣ Разделы", callback_data="course:Разделы")],
        ]
    )


def sections_keyboard(user_id: int) -> InlineKeyboardMarkup:
    profile = ensure_profile(user_id)
    selected = set(profile.get("sections", []))

    rows = []
    for section in SECTION_OPTIONS:
        mark = "✅ " if section in selected else ""
        rows.append(
            [InlineKeyboardButton(f"{mark}{section}", callback_data=f"section:{section}")]
        )

    rows.append([InlineKeyboardButton("✅ Готово", callback_data="section_done")])
    return InlineKeyboardMarkup(rows)


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
        logger.warning(
            "Не удалось удалить сообщение %s в чате %s: %s",
            message_id,
            chat_id,
            e,
        )


async def send_temp_reply(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    delay: int = 3,
    parse_mode: str | None = None,
    reply_markup=None,
) -> None:
    try:
        sent = await message.reply_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
        await delete_message_later(
            context=context,
            chat_id=sent.chat_id,
            message_id=sent.message_id,
            delay=delay,
        )
    except Exception as e:
        logger.warning("Не удалось отправить временное сообщение: %s", e)


async def send_survey_if_needed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    profile = ensure_profile(user_id)

    if profile["survey_done"]:
        return

    if profile["survey_sent"]:
        return

    profile["survey_sent"] = True

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "📚 <b>К какому курсу вы относитесь?</b>\n\n"
                "Пожалуйста, выберите свой курс:"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=course_keyboard(),
        )
    except Exception as e:
        logger.warning("Не удалось отправить опрос пользователю %s: %s", user_id, e)


async def send_to_all_admins(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    for admin_id in ADMINS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except Exception as e:
            logger.warning("Не удалось отправить сообщение админу %s: %s", admin_id, e)


async def send_media_to_all_admins(
    context: ContextTypes.DEFAULT_TYPE,
    kind: str,
    file_id: str,
    caption: str,
    user_id: int,
) -> None:
    for admin_id in ADMINS:
        try:
            if kind == "photo":
                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_keyboard(user_id),
                )
            elif kind == "document":
                await context.bot.send_document(
                    chat_id=admin_id,
                    document=file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_keyboard(user_id),
                )
        except Exception as e:
            logger.warning("Не удалось отправить %s админу %s: %s", kind, admin_id, e)


async def notify_admins_about_admin_reply(
    context: ContextTypes.DEFAULT_TYPE,
    replying_admin,
    target_user_id: int,
    reply_text: str,
) -> None:
    admin_name = escape(replying_admin.full_name or "Без имени")
    admin_username = (
        f"@{escape(replying_admin.username)}"
        if replying_admin.username
        else "нет"
    )

    text = (
        f"👨‍💼 <b>Ответ администратора</b>\n\n"
        f"<b>Кто ответил:</b> {admin_name}\n"
        f"<b>Username:</b> {admin_username}\n"
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
            logger.warning(
                "Не удалось отправить уведомление админу %s: %s",
                admin_id,
                e,
            )


# =========================================
# КОМАНДЫ
# =========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not update.message or not user:
        return

    if is_admin(user.id):
        await update.message.reply_text(
            "Вы вошли как администратор.\n\n"
            "Команды:\n"
            "/users — список пользователей с курсами и разделами\n"
            "/cancel — отменить текущий режим ответа\n"
            "/id — показать ваш Telegram ID\n\n"
            "Чтобы ответить ученику, нажмите кнопку «Ответить» под его сообщением."
        )
    else:
        ensure_profile(user.id)

        await update.message.reply_text(
            "Здравствуйте.\n"
            "Напишите ваше сообщение,администрация увидит и свяжеться .\n"
            "Ответ придёт от имени бота."
        )

        # Новый пользователь получает опрос сразу после /start
        await send_survey_if_needed(user.id, context)


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if update.message and user:
        await update.message.reply_text(f"Ваш Telegram ID: {user.id}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    admin_reply_target.pop(user_id, None)
    await update.message.reply_text("Режим ответа отменён.")


async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not update.message or not user or not is_admin(user.id):
        return

    if not known_users:
        await update.message.reply_text("Пока никто не писал.")
        return

    lines = ["📋 <b>Пользователи бота:</b>\n"]

    for uid, data in known_users.items():
        full_name = escape(data.get("full_name", "Без имени"))
        username = data.get("username")
        username_text = f"@{escape(username)}" if username else "нет"

        profile = ensure_profile(uid)
        course = profile.get("course")
        sections = profile.get("sections", [])

        if not course:
            course_text = "не выбран"
        elif course == "Биоспринт":
            course_text = "Биоспринт"
        else:
            course_text = "Разделы"
            if sections:
                course_text += " — " + ", ".join(sections)

        lines.append(
            f"• <b>{full_name}</b>\n"
            f"Username: {username_text}\n"
            f"ID: <code>{uid}</code>\n"
            f"Курс/раздел: {escape(course_text)}\n"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# =========================================
# CALLBACK-КНОПКИ
# =========================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()

    user = query.from_user
    if not user:
        return

    data = query.data or ""

    # -------------------------------------
    # ОПРОС ПОЛЬЗОВАТЕЛЯ
    # -------------------------------------

    if data.startswith("course:"):
        course = data.split(":", 1)[1]
        profile = ensure_profile(user.id)
        profile["course"] = course

        if course == "Биоспринт":
            profile["survey_done"] = True
            await query.message.reply_text("✅ Спасибо! Курс сохранён: Биоспринт.")
        else:
            await query.message.reply_text(
                "📚 Выберите ваш раздел или несколько разделов:",
                reply_markup=sections_keyboard(user.id),
            )
        return

    if data.startswith("section:"):
        section = data.split(":", 1)[1]
        profile = ensure_profile(user.id)

        if profile["course"] != "Разделы":
            profile["course"] = "Разделы"

        if section in profile["sections"]:
            profile["sections"].remove(section)
        else:
            profile["sections"].append(section)

        try:
            await query.message.edit_reply_markup(
                reply_markup=sections_keyboard(user.id)
            )
        except Exception:
            pass
        return

    if data == "section_done":
        profile = ensure_profile(user.id)
        profile["course"] = "Разделы"
        profile["survey_done"] = True

        sections = profile.get("sections", [])
        sections_text = ", ".join(sections) if sections else "не выбраны"

        await query.message.reply_text(
            f"✅ Спасибо! Ваши разделы сохранены:\n{sections_text}"
        )
        return

    # -------------------------------------
    # АДМИНСКИЕ КНОПКИ
    # -------------------------------------

    if not is_admin(user.id):
        if query.message:
            await query.message.reply_text("Нет доступа.")
        return

    if data.startswith("reply:"):
        target_user_id = int(data.split(":")[1])
        admin_reply_target[user.id] = target_user_id

        msg = await query.message.reply_text(
            f"Теперь вы отвечаете пользователю <code>{target_user_id}</code>.\n"
            f"Просто отправьте следующее сообщение боту.",
            parse_mode=ParseMode.HTML,
        )
        await delete_message_later(context, msg.chat_id, msg.message_id, 3)
        return

    elif data.startswith("close:"):
        target_user_id = int(data.split(":")[1])

        if admin_reply_target.get(user.id) == target_user_id:
            admin_reply_target.pop(user.id, None)

        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text="Диалог завершён. Если нужно, можете написать снова.",
            )
        except Exception:
            pass

        msg = await query.message.reply_text(
            f"Диалог с пользователем <code>{target_user_id}</code> закрыт.",
            parse_mode=ParseMode.HTML,
        )
        await delete_message_later(context, msg.chat_id, msg.message_id, 3)
        return


# =========================================
# ТЕКСТОВЫЕ СООБЩЕНИЯ
# =========================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user

    if not message or not user or not message.text:
        return

    # Если пишет админ — значит это ответ ученику
    if is_admin(user.id):
        target_user_id = admin_reply_target.get(user.id)
        if not target_user_id:
            await message.reply_text(
                "Сначала нажмите «Ответить» под сообщением ученика."
            )
            return

        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=message.text,
            )

            await notify_admins_about_admin_reply(
                context=context,
                replying_admin=user,
                target_user_id=target_user_id,
                reply_text=message.text,
            )

            await send_temp_reply(
                message=message,
                context=context,
                text="Ответ отправлен анонимно.",
                delay=3,
            )
        except Exception as e:
            logger.error("Ошибка отправки ответа: %s", e)
            await message.reply_text("Не удалось отправить ответ.")
        return

    # Если пишет обычный пользователь
    ensure_profile(user.id)

    known_users[user.id] = {
        "full_name": user.full_name,
        "username": user.username,
    }

    await send_temp_reply(
        message=message,
        context=context,
        text="Ваше сообщение отправлено администрации.",
        delay=3,
    )

    text = (
        f"{user_card(user)}\n"
        f"<b>Сообщение:</b>\n{escape(message.text)}"
    )

    await send_to_all_admins(
        context=context,
        text=text,
        reply_markup=reply_keyboard(user.id),
    )

    # Старым пользователям, которые не проходили опрос,
    # отправляем его после первого нового сообщения
    await send_survey_if_needed(user.id, context)


# =========================================
# ФОТО
# =========================================

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user

    if not message or not user or not message.photo:
        return

    if is_admin(user.id):
        target_user_id = admin_reply_target.get(user.id)
        if not target_user_id:
            await message.reply_text("Сначала выберите диалог кнопкой «Ответить».")
            return

        try:
            await context.bot.send_photo(
                chat_id=target_user_id,
                photo=message.photo[-1].file_id,
                caption=message.caption or "",
            )

            await notify_admins_about_admin_reply(
                context=context,
                replying_admin=user,
                target_user_id=target_user_id,
                reply_text=f"[Фото] {message.caption or 'без подписи'}",
            )

            await send_temp_reply(
                message=message,
                context=context,
                text="Фото отправлено анонимно.",
                delay=3,
            )
        except Exception as e:
            logger.error("Ошибка отправки фото: %s", e)
            await message.reply_text("Не удалось отправить фото.")
        return

    ensure_profile(user.id)

    known_users[user.id] = {
        "full_name": user.full_name,
        "username": user.username,
    }

    await send_temp_reply(
        message=message,
        context=context,
        text="Фото отправлено администрации.",
        delay=3,
    )

    caption = (
        f"{user_card(user)}\n"
        f"<b>Фото</b>\n"
        f"<b>Подпись:</b> {escape(message.caption or 'без подписи')}"
    )

    await send_media_to_all_admins(
        context=context,
        kind="photo",
        file_id=message.photo[-1].file_id,
        caption=caption,
        user_id=user.id,
    )

    await send_survey_if_needed(user.id, context)


# =========================================
# ДОКУМЕНТЫ
# =========================================

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user

    if not message or not user or not message.document:
        return

    if is_admin(user.id):
        target_user_id = admin_reply_target.get(user.id)
        if not target_user_id:
            await message.reply_text("Сначала выберите диалог кнопкой «Ответить».")
            return

        try:
            await context.bot.send_document(
                chat_id=target_user_id,
                document=message.document.file_id,
                caption=message.caption or "",
            )

            await notify_admins_about_admin_reply(
                context=context,
                replying_admin=user,
                target_user_id=target_user_id,
                reply_text=(
                    f"[Документ] "
                    f"{message.document.file_name or 'без имени'} | "
                    f"{message.caption or 'без подписи'}"
                ),
            )

            await send_temp_reply(
                message=message,
                context=context,
                text="Документ отправлен анонимно.",
                delay=3,
            )
        except Exception as e:
            logger.error("Ошибка отправки документа: %s", e)
            await message.reply_text("Не удалось отправить документ.")
        return

    ensure_profile(user.id)

    known_users[user.id] = {
        "full_name": user.full_name,
        "username": user.username,
    }

    await send_temp_reply(
        message=message,
        context=context,
        text="Документ отправлен администрации.",
        delay=3,
    )

    file_name = escape(message.document.file_name or "без имени")
    caption = (
        f"{user_card(user)}\n"
        f"<b>Документ:</b> {file_name}\n"
        f"<b>Подпись:</b> {escape(message.caption or 'без подписи')}"
    )

    await send_media_to_all_admins(
        context=context,
        kind="document",
        file_id=message.document.file_id,
        caption=caption,
        user_id=user.id,
    )

    await send_survey_if_needed(user.id, context)


# =========================================
# ЗАПУСК
# =========================================

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("users", users_list))

    app.add_handler(CallbackQueryHandler(callback_handler))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))

    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
