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

# Какому пользователю сейчас отвечает конкретный админ
admin_reply_target: dict[int, int] = {}

# Кто из учеников уже писал
known_users: dict[int, dict] = {}

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


def reply_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply:{user_id}")],
            [InlineKeyboardButton("🛑 Закрыть диалог", callback_data=f"close:{user_id}")],
        ]
    )


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
            "/users — кто писал боту\n"
            "/cancel — отменить текущий режим ответа\n"
            "/id — показать ваш Telegram ID\n\n"
            "Чтобы ответить ученику, нажмите кнопку «Ответить» под его сообщением."
        )
    else:
        await update.message.reply_text(
            "Здравствуйте.\n"
            "Напишите ваше сообщение, и администрация увидит его анонимно.\n"
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
    await update.message.reply_text("Режим ответа отменён.")


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
        lines.append(
            f"• {full_name} | {username_text} | <code>{uid}</code>"
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

    admin = query.from_user
    if not admin or not is_admin(admin.id):
        await query.message.reply_text("Нет доступа.")
        return

    data = query.data or ""

    if data.startswith("reply:"):
        user_id = int(data.split(":")[1])
        admin_reply_target[admin.id] = user_id
        await query.message.reply_text(
            f"Теперь вы отвечаете пользователю <code>{user_id}</code>.\n"
            f"Просто отправьте следующее сообщение боту.",
            parse_mode=ParseMode.HTML,
        )

    elif data.startswith("close:"):
        user_id = int(data.split(":")[1])

        if admin_reply_target.get(admin.id) == user_id:
            admin_reply_target.pop(admin.id, None)

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="Диалог завершён. Если нужно, можете написать снова.",
            )
        except Exception:
            pass

        await query.message.reply_text(
            f"Диалог с пользователем <code>{user_id}</code> закрыт.",
            parse_mode=ParseMode.HTML,
        )


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
            await message.reply_text("Ответ отправлен анонимно.")
        except Exception as e:
            logger.error("Ошибка отправки ответа: %s", e)
            await message.reply_text("Не удалось отправить ответ.")
        return

    # Если пишет ученик
    known_users[user.id] = {
        "full_name": user.full_name,
        "username": user.username,
    }

    await message.reply_text("Ваше сообщение отправлено администрации.")

    text = (
        f"{user_card(user)}\n"
        f"<b>Сообщение:</b>\n{escape(message.text)}"
    )

    await send_to_all_admins(
        context=context,
        text=text,
        reply_markup=reply_keyboard(user.id),
    )


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
            await message.reply_text("Фото отправлено анонимно.")
        except Exception:
            await message.reply_text("Не удалось отправить фото.")
        return

    known_users[user.id] = {
        "full_name": user.full_name,
        "username": user.username,
    }

    await message.reply_text("Фото отправлено администрации.")

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
            await message.reply_text("Документ отправлен анонимно.")
        except Exception:
            await message.reply_text("Не удалось отправить документ.")
        return

    known_users[user.id] = {
        "full_name": user.full_name,
        "username": user.username,
    }

    await message.reply_text("Документ отправлен администрации.")

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
