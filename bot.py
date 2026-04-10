import asyncio
import logging
import os
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

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================================
# ПАМЯТЬ БОТА
# =========================================

# admin_id -> (user_id, source_message_id)
admin_reply_target: dict[int, tuple[int, int]] = {}

# Кто уже писал боту
known_users: dict[int, dict] = {}

# Копии сообщений учеников у админов
# key = (user_id, source_message_id)
# value = {
#   "status": {"answered": bool, "admin_label": str | None},
#   "copies": {
#       admin_id: {
#           "kind": "text" | "photo" | "document",
#           "message_id": int,
#           "base_text": str
#       }
#   }
# }
admin_message_copies: dict[tuple[int, int], dict] = {}


# =========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def admin_label(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.full_name or str(user.id)


def status_text(answered: bool, who: str | None = None) -> str:
    if answered and who:
        return f"ОТВЕЧЕНО ✅ {escape(who)}"
    return "НЕ ОТВЕЧЕНО ❌"


def reaction_alias_to_emoji(alias: str) -> str | None:
    mapping = {
        "r1": "💞",
        "r2": "🥲",
        "r3": "😍",
        "r4": "🤔",
    }
    return mapping.get(alias)


def reply_keyboard(user_id: int, source_message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✉️ Ответить",
                    callback_data=f"reply:{user_id}:{source_message_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "💞",
                    callback_data=f"react:{user_id}:{source_message_id}:r1"
                ),
                InlineKeyboardButton(
                    "🥲",
                    callback_data=f"react:{user_id}:{source_message_id}:r2"
                ),
                InlineKeyboardButton(
                    "😍",
                    callback_data=f"react:{user_id}:{source_message_id}:r3"
                ),
                InlineKeyboardButton(
                    "🤔",
                    callback_data=f"react:{user_id}:{source_message_id}:r4"
                ),
            ],
        ]
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
    )


def target_user_label(user_id: int) -> str:
    data = known_users.get(user_id, {})

    full_name_raw = data.get("full_name") or "Без имени"
    username_raw = data.get("username")

    full_name = escape(full_name_raw)

    if username_raw:
        username = f"@{escape(username_raw)}"
        return f"{full_name} ({username})"

    return f'<a href="tg://user?id={user_id}">{full_name}</a>'


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
) -> None:
    try:
        sent = await message.reply_text(text, parse_mode=parse_mode)
        await delete_message_later(
            context=context,
            chat_id=sent.chat_id,
            message_id=sent.message_id,
            delay=delay,
        )
    except Exception as e:
        logger.warning("Не удалось отправить временное сообщение: %s", e)


def build_admin_message(base_text: str, answered: bool, who: str | None = None) -> str:
    return f"{base_text}\n\n<b>Статус:</b> {status_text(answered, who)}"


async def store_admin_copy(
    admin_id: int,
    kind: str,
    sent_message_id: int,
    user_id: int,
    source_message_id: int,
    base_text: str,
) -> None:
    key = (user_id, source_message_id)
    if key not in admin_message_copies:
        admin_message_copies[key] = {
            "status": {
                "answered": False,
                "admin_label": None,
            },
            "copies": {},
        }

    admin_message_copies[key]["copies"][admin_id] = {
        "kind": kind,
        "message_id": sent_message_id,
        "base_text": base_text,
    }


async def refresh_admin_copies(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    source_message_id: int,
) -> None:
    key = (user_id, source_message_id)
    data = admin_message_copies.get(key)
    if not data:
        return

    answered = data["status"]["answered"]
    who = data["status"]["admin_label"]

    for admin_id, copy_data in data["copies"].items():
        try:
            new_text = build_admin_message(copy_data["base_text"], answered, who)

            if copy_data["kind"] == "text":
                await context.bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=copy_data["message_id"],
                    text=new_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_keyboard(user_id, source_message_id),
                )
            else:
                await context.bot.edit_message_caption(
                    chat_id=admin_id,
                    message_id=copy_data["message_id"],
                    caption=new_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_keyboard(user_id, source_message_id),
                )
        except Exception as e:
            logger.warning(
                "Не удалось обновить копию сообщения для админа %s: %s",
                admin_id,
                e,
            )


async def notify_admins_about_admin_reply(
    context: ContextTypes.DEFAULT_TYPE,
    replying_admin,
    target_user_id: int,
    reply_text: str,
) -> None:
    who = escape(admin_label(replying_admin))
    target_label = target_user_label(target_user_id)

    text = (
        f"👨‍💼 <b>Ответ администратора</b>\n\n"
        f"<b>Кто ответил:</b> {who}\n"
        f"<b>ID админа:</b> <code>{replying_admin.id}</code>\n"
        f"<b>Кому ответил:</b> {target_label}\n\n"
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
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("Не удалось уведомить админа %s: %s", admin_id, e)


async def mark_answered(
    context: ContextTypes.DEFAULT_TYPE,
    replying_admin,
    target_user_id: int,
    source_message_id: int,
) -> None:
    key = (target_user_id, source_message_id)
    if key in admin_message_copies:
        admin_message_copies[key]["status"]["answered"] = True
        admin_message_copies[key]["status"]["admin_label"] = admin_label(replying_admin)

    await refresh_admin_copies(
        context=context,
        user_id=target_user_id,
        source_message_id=source_message_id,
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
            "/users — кто писал боту\n"
            "/cancel — сбросить выбранного собеседника\n"
            "/id — показать ваш Telegram ID\n\n"
            "Нажмите «Ответить» под нужным сообщением ученика."
        )
    else:
        await update.message.reply_text(
            "Здравствуйте.\n"
            "Напишите ваше сообщение, и администрация увидит его.\n"
            "Ответ придёт от имени бота."
        )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if update.message and user:
        await update.message.reply_text(f"Ваш Telegram ID: {user.id}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not update.message or not user or not is_admin(user.id):
        return

    admin_reply_target.pop(user.id, None)
    await update.message.reply_text("Режим ответа выключен.")


async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not update.message or not user or not is_admin(user.id):
        return

    if not known_users:
        await update.message.reply_text("Пока никто не писал.")
        return

    lines = ["📋 <b>Пользователи, писавшие боту:</b>\n"]
    for uid, data in known_users.items():
        full_name = escape(data.get("full_name", "Без имени"))
        username = data.get("username")
        username_text = f"@{escape(username)}" if username else "нет"
        lines.append(f"• {full_name} | {username_text} | <code>{uid}</code>")

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

    admin = query.from_user
    if not admin or not is_admin(admin.id):
        if query.message:
            await query.message.reply_text("Нет доступа.")
        return

    data = query.data or ""

    if data.startswith("reply:"):
        _, user_id_str, source_message_id_str = data.split(":")
        user_id = int(user_id_str)
        source_message_id = int(source_message_id_str)

        admin_reply_target[admin.id] = (user_id, source_message_id)

        if query.message:
            await query.message.reply_text(
                f"Режим ответа включён ✅\n"
                f"Вы отвечаете пользователю <code>{user_id}</code>\n"
                f"Режим будет активен, пока вы не выберете другого ученика "
                f"или не отправите /cancel",
                parse_mode=ParseMode.HTML,
            )
        return

    if data.startswith("react:"):
        _, user_id_str, source_message_id_str, alias = data.split(":")
        user_id = int(user_id_str)
        source_message_id = int(source_message_id_str)
        emoji = reaction_alias_to_emoji(alias)

        if not emoji:
            return

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"Реакция {emoji}",
                reply_to_message_id=source_message_id,
            )

            if query.message:
                await send_temp_reply(
                    message=query.message,
                    context=context,
                    text=f"Реакция {emoji} поставлена.",
                    delay=3,
                )
        except Exception as e:
            logger.warning("Не удалось отправить реакцию: %s", e)
            if query.message:
                await send_temp_reply(
                    message=query.message,
                    context=context,
                    text="Не удалось отправить реакцию.",
                    delay=3,
                )
        return


# =========================================
# ТЕКСТОВЫЕ СООБЩЕНИЯ
# =========================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user

    if not message or not user or not message.text:
        return

    # Админ отвечает ученику
    if is_admin(user.id):
        target = admin_reply_target.get(user.id)
        if not target:
            await send_temp_reply(
                message=message,
                context=context,
                text="Сначала нажмите «Ответить» под сообщением ученика.",
                delay=3,
            )
            return

        target_user_id, source_message_id = target

        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=message.text,
            )

            await mark_answered(
                context=context,
                replying_admin=user,
                target_user_id=target_user_id,
                source_message_id=source_message_id,
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

    # Ученик пишет боту
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

    base_text = (
        f"{user_card(user)}\n"
        f"<b>Сообщение:</b>\n{escape(message.text)}"
    )
    full_text = build_admin_message(base_text, answered=False)

    for admin_id in ADMINS:
        try:
            sent = await context.bot.send_message(
                chat_id=admin_id,
                text=full_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_keyboard(user.id, message.message_id),
            )
            await store_admin_copy(
                admin_id=admin_id,
                kind="text",
                sent_message_id=sent.message_id,
                user_id=user.id,
                source_message_id=message.message_id,
                base_text=base_text,
            )
        except Exception as e:
            logger.warning("Не удалось отправить сообщение админу %s: %s", admin_id, e)


# =========================================
# ФОТО
# =========================================

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user

    if not message or not user or not message.photo:
        return

    if is_admin(user.id):
        target = admin_reply_target.get(user.id)
        if not target:
            await send_temp_reply(
                message=message,
                context=context,
                text="Сначала нажмите «Ответить» под сообщением ученика.",
                delay=3,
            )
            return

        target_user_id, source_message_id = target

        try:
            await context.bot.send_photo(
                chat_id=target_user_id,
                photo=message.photo[-1].file_id,
                caption=message.caption or "",
            )

            await mark_answered(
                context=context,
                replying_admin=user,
                target_user_id=target_user_id,
                source_message_id=source_message_id,
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

    base_text = (
        f"{user_card(user)}\n"
        f"<b>Фото</b>\n"
        f"<b>Подпись:</b> {escape(message.caption or 'без подписи')}"
    )
    full_caption = build_admin_message(base_text, answered=False)

    for admin_id in ADMINS:
        try:
            sent = await context.bot.send_photo(
                chat_id=admin_id,
                photo=message.photo[-1].file_id,
                caption=full_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_keyboard(user.id, message.message_id),
            )
            await store_admin_copy(
                admin_id=admin_id,
                kind="photo",
                sent_message_id=sent.message_id,
                user_id=user.id,
                source_message_id=message.message_id,
                base_text=base_text,
            )
        except Exception as e:
            logger.warning("Не удалось отправить фото админу %s: %s", admin_id, e)


# =========================================
# ДОКУМЕНТЫ
# =========================================

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user

    if not message or not user or not message.document:
        return

    if is_admin(user.id):
        target = admin_reply_target.get(user.id)
        if not target:
            await send_temp_reply(
                message=message,
                context=context,
                text="Сначала нажмите «Ответить» под сообщением ученика.",
                delay=3,
            )
            return

        target_user_id, source_message_id = target

        try:
            await context.bot.send_document(
                chat_id=target_user_id,
                document=message.document.file_id,
                caption=message.caption or "",
            )

            await mark_answered(
                context=context,
                replying_admin=user,
                target_user_id=target_user_id,
                source_message_id=source_message_id,
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
    base_text = (
        f"{user_card(user)}\n"
        f"<b>Документ:</b> {file_name}\n"
        f"<b>Подпись:</b> {escape(message.caption or 'без подписи')}"
    )
    full_caption = build_admin_message(base_text, answered=False)

    for admin_id in ADMINS:
        try:
            sent = await context.bot.send_document(
                chat_id=admin_id,
                document=message.document.file_id,
                caption=full_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_keyboard(user.id, message.message_id),
            )
            await store_admin_copy(
                admin_id=admin_id,
                kind="document",
                sent_message_id=sent.message_id,
                user_id=user.id,
                source_message_id=message.message_id,
                base_text=base_text,
            )
        except Exception as e:
            logger.warning("Не удалось отправить документ админу %s: %s", admin_id, e)


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
