import logging
import os
import sqlite3
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
DB_PATH = os.getenv("DB_PATH", "bot_data.db").strip()

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
# БАЗА SQLITE
# =========================================

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admin_targets (
            admin_id INTEGER PRIMARY KEY,
            target_user_id INTEGER NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            is_open INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


def save_user(user_id: int, full_name: str, username: str | None) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, full_name, username)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            full_name=excluded.full_name,
            username=excluded.username
    """, (user_id, full_name, username))
    conn.commit()
    conn.close()


def get_all_users() -> list[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, full_name, username FROM users ORDER BY user_id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows


def create_or_get_open_ticket(user_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT ticket_id
        FROM tickets
        WHERE user_id = ? AND is_open = 1
        ORDER BY ticket_id DESC
        LIMIT 1
    """, (user_id,))
    row = cur.fetchone()

    if row:
        ticket_id = row["ticket_id"]
        cur.execute("""
            UPDATE tickets
            SET updated_at = CURRENT_TIMESTAMP
            WHERE ticket_id = ?
        """, (ticket_id,))
    else:
        cur.execute("""
            INSERT INTO tickets (user_id, is_open)
            VALUES (?, 1)
        """, (user_id,))
        ticket_id = cur.lastrowid

    conn.commit()
    conn.close()
    return ticket_id


def close_ticket(ticket_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE tickets
        SET is_open = 0,
            updated_at = CURRENT_TIMESTAMP
        WHERE ticket_id = ?
    """, (ticket_id,))
    conn.commit()
    conn.close()


def get_ticket(ticket_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT ticket_id, user_id, is_open, created_at, updated_at
        FROM tickets
        WHERE ticket_id = ?
        LIMIT 1
    """, (ticket_id,))
    row = cur.fetchone()
    conn.close()
    return row


def set_admin_target(admin_id: int, target_user_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO admin_targets (admin_id, target_user_id)
        VALUES (?, ?)
        ON CONFLICT(admin_id) DO UPDATE SET
            target_user_id=excluded.target_user_id
    """, (admin_id, target_user_id))
    conn.commit()
    conn.close()


def get_admin_target(admin_id: int) -> int | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT target_user_id
        FROM admin_targets
        WHERE admin_id = ?
        LIMIT 1
    """, (admin_id,))
    row = cur.fetchone()
    conn.close()
    return row["target_user_id"] if row else None


def clear_admin_target(admin_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM admin_targets WHERE admin_id = ?", (admin_id,))
    conn.commit()
    conn.close()


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


def ticket_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✉️ Ответить", callback_data=f"reply_ticket:{ticket_id}")],
            [InlineKeyboardButton("🛑 Закрыть диалог", callback_data=f"close_ticket:{ticket_id}")],
        ]
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
    ticket_id: int,
) -> None:
    for admin_id in ADMINS:
        try:
            if kind == "photo":
                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=ticket_keyboard(ticket_id),
                )
            elif kind == "document":
                await context.bot.send_document(
                    chat_id=admin_id,
                    document=file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=ticket_keyboard(ticket_id),
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

    clear_admin_target(user.id)
    await update.message.reply_text("Режим ответа отменён.")


async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not update.message or not user or not is_admin(user.id):
        return

    rows = get_all_users()
    if not rows:
        await update.message.reply_text("Пока никто не писал.")
        return

    lines = ["📋 <b>Пользователи, писавшие боту:</b>\n"]
    for row in rows:
        full_name = escape(row["full_name"] or "Без имени")
        username = row["username"]
        username_text = f"@{escape(username)}" if username else "нет"
        lines.append(
            f"• {full_name} | {username_text} | <code>{row['user_id']}</code>"
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
        if query.message:
            await query.message.reply_text("Нет доступа.")
        return

    data = query.data or ""

    if data.startswith("reply_ticket:"):
        ticket_id = int(data.split(":")[1])
        ticket = get_ticket(ticket_id)

        if not ticket or ticket["is_open"] != 1:
            if query.message:
                await query.message.reply_text("Тикет не найден или уже закрыт.")
            return

        target_user_id = int(ticket["user_id"])
        set_admin_target(admin.id, target_user_id)

        if query.message:
            temp = await query.message.reply_text(
                f"Теперь вы отвечаете пользователю <code>{target_user_id}</code>.\n"
                f"Тикет: <code>{ticket_id}</code>\n"
                f"Просто отправьте следующее сообщение боту.",
                parse_mode=ParseMode.HTML,
            )
            await delete_message_later(
                context=context,
                chat_id=temp.chat_id,
                message_id=temp.message_id,
                delay=3,
            )

    elif data.startswith("close_ticket:"):
        ticket_id = int(data.split(":")[1])
        ticket = get_ticket(ticket_id)

        if not ticket:
            if query.message:
                await query.message.reply_text("Тикет не найден.")
            return

        user_id = int(ticket["user_id"])
        close_ticket(ticket_id)

        if get_admin_target(admin.id) == user_id:
            clear_admin_target(admin.id)

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="Диалог завершён. Если нужно, можете написать снова.",
            )
        except Exception:
            pass

        if query.message:
            temp = await query.message.reply_text(
                f"Диалог с пользователем <code>{user_id}</code> закрыт.",
                parse_mode=ParseMode.HTML,
            )
            await delete_message_later(
                context=context,
                chat_id=temp.chat_id,
                message_id=temp.message_id,
                delay=3,
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
        target_user_id = get_admin_target(user.id)
        if not target_user_id:
            await send_temp_reply(
                message=message,
                context=context,
                text="Сначала нажмите «Ответить» под сообщением ученика.",
                delay=3,
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

    # Если пишет ученик
    save_user(user.id, user.full_name, user.username)
    ticket_id = create_or_get_open_ticket(user.id)

    await send_temp_reply(
        message=message,
        context=context,
        text="Ваше сообщение отправлено администрации.",
        delay=3,
    )

    text = (
        f"{user_card(user)}"
        f"<b>Тикет:</b> <code>{ticket_id}</code>\n\n"
        f"<b>Сообщение:</b>\n{escape(message.text)}"
    )

    await send_to_all_admins(
        context=context,
        text=text,
        reply_markup=ticket_keyboard(ticket_id),
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
        target_user_id = get_admin_target(user.id)
        if not target_user_id:
            await send_temp_reply(
                message=message,
                context=context,
                text="Сначала выберите диалог кнопкой «Ответить».",
                delay=3,
            )
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

    save_user(user.id, user.full_name, user.username)
    ticket_id = create_or_get_open_ticket(user.id)

    await send_temp_reply(
        message=message,
        context=context,
        text="Фото отправлено администрации.",
        delay=3,
    )

    caption = (
        f"{user_card(user)}"
        f"<b>Тикет:</b> <code>{ticket_id}</code>\n\n"
        f"<b>Фото</b>\n"
        f"<b>Подпись:</b> {escape(message.caption or 'без подписи')}"
    )

    await send_media_to_all_admins(
        context=context,
        kind="photo",
        file_id=message.photo[-1].file_id,
        caption=caption,
        ticket_id=ticket_id,
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
        target_user_id = get_admin_target(user.id)
        if not target_user_id:
            await send_temp_reply(
                message=message,
                context=context,
                text="Сначала выберите диалог кнопкой «Ответить».",
                delay=3,
            )
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

    save_user(user.id, user.full_name, user.username)
    ticket_id = create_or_get_open_ticket(user.id)

    await send_temp_reply(
        message=message,
        context=context,
        text="Документ отправлен администрации.",
        delay=3,
    )

    file_name = escape(message.document.file_name or "без имени")
    caption = (
        f"{user_card(user)}"
        f"<b>Тикет:</b> <code>{ticket_id}</code>\n\n"
        f"<b>Документ:</b> {file_name}\n"
        f"<b>Подпись:</b> {escape(message.caption or 'без подписи')}"
    )

    await send_media_to_all_admins(
        context=context,
        kind="document",
        file_id=message.document.file_id,
        caption=caption,
        ticket_id=ticket_id,
    )


# =========================================
# ЗАПУСК
# =========================================

def main() -> None:
    init_db()

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
