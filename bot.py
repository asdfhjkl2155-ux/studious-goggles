"""Telegram bot: send a description, get an AI-generated video back.

Run:  python bot.py   (after filling in .env — see README.md)
"""

from __future__ import annotations

import io
import logging
import os

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import videogen

log = logging.getLogger("videobot")

STATUS_TEXT = {
    "Queued": "⏳ الطلب في قائمة الانتظار…",
    "InProgress": "🎬 جاري توليد الفيديو… (قد يأخذ من 1 إلى 5 دقائق)",
    "Completed": "✅ اكتمل التوليد، جاري الإرسال…",
}

HELP = (
    "اكتب وصف الفيديو في رسالة وسأولّده لك.\n\n"
    "الأوامر:\n"
    "/model — اختيار موديل التوليد\n"
    "/aspect — مقاس الفيديو (أفقي 16:9 أو عمودي 9:16)\n"
    "/id — عرض رقم حسابك في تيليجرام\n\n"
    "نصيحة: الوصف بالإنجليزية وبتفاصيل واضحة (المشهد، الحركة، الإضاءة، زاوية الكاميرا) "
    "يعطي نتيجة أفضل."
)


def parse_allowed_ids(raw: str | None) -> set[int]:
    """Parse ALLOWED_USER_IDS ("123, 456") into a set of Telegram user ids."""
    return {int(part) for part in (raw or "").replace(";", ",").split(",") if part.strip().isdigit()}


def is_allowed(user_id: int | None, allowed: set[int]) -> bool:
    """Only listed users may generate: every video is billed to the owner's fal balance."""
    return user_id is not None and user_id in allowed


def _settings(context: ContextTypes.DEFAULT_TYPE) -> dict:
    data = context.user_data
    data.setdefault("model", os.environ.get("DEFAULT_MODEL", videogen.DEFAULT_MODEL))
    data.setdefault("aspect", "16:9")
    return data


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if is_allowed(user.id if user else None, context.bot_data["allowed"]):
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            "هذا بوت خاص. إن كنت صاحبه، أضف رقمك إلى ALLOWED_USER_IDS في ملف .env "
            f"ثم أعد تشغيل البوت.\nرقمك: {user.id if user else 'غير معروف'}"
        )
    return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        await update.message.reply_text("أهلاً 👋\n" + HELP)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"رقم حسابك: {update.effective_user.id}")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    settings = _settings(context)
    if context.args:  # /model <short name or full fal endpoint id>
        try:
            videogen.resolve_endpoint(context.args[0])
        except videogen.VideoGenError as exc:
            await update.message.reply_text(str(exc))
            return
        settings["model"] = context.args[0]
        await update.message.reply_text(f"تم اختيار الموديل: {context.args[0]}")
        return
    buttons = [
        [InlineKeyboardButton(("● " if key == settings["model"] else "") + label, callback_data=f"model:{key}")]
        for key, (_, label) in videogen.MODELS.items()
    ]
    await update.message.reply_text(
        f"الموديل الحالي: {settings['model']}\nاختر موديل، أو اكتب /model متبوعاً بمعرّف endpoint من fal:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_aspect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    labels = {"16:9": "أفقي 16:9 (يوتيوب)", "9:16": "عمودي 9:16 (Reels / Shorts)"}
    buttons = [[InlineKeyboardButton(labels[a], callback_data=f"aspect:{a}")] for a in videogen.ASPECT_RATIOS]
    await update.message.reply_text(
        f"المقاس الحالي: {_settings(context)['aspect']}", reply_markup=InlineKeyboardMarkup(buttons)
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_allowed(query.from_user.id, context.bot_data["allowed"]):
        return
    kind, _, value = (query.data or "").partition(":")
    settings = _settings(context)
    if kind == "model" and value in videogen.MODELS:
        settings["model"] = value
        await query.edit_message_text(f"تم اختيار الموديل: {videogen.MODELS[value][1]}")
    elif kind == "aspect" and value in videogen.ASPECT_RATIOS:
        settings["aspect"] = value
        await query.edit_message_text(f"تم اختيار المقاس: {value}")


async def on_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    message = update.message
    settings = _settings(context)
    status_msg = await message.reply_text("📨 تم استلام الوصف، جاري إرسال الطلب…")

    async def on_status(name: str) -> None:
        text = STATUS_TEXT.get(name)
        if text:
            try:
                await status_msg.edit_text(text)
            except Exception:  # a failed status edit must never abort the generation
                log.debug("status edit failed", exc_info=True)

    try:
        result = await videogen.generate(message.text, settings["model"], settings["aspect"], on_status)
    except videogen.VideoGenError as exc:
        await status_msg.edit_text(f"❌ {exc}")
        return
    except Exception:
        log.exception("generation failed")
        await status_msg.edit_text("❌ حدث خطأ غير متوقع أثناء التوليد. راجع سجل البوت.")
        return

    caption = message.text if len(message.text) <= 900 else message.text[:900] + "…"
    try:
        await message.chat.send_action(ChatAction.UPLOAD_VIDEO)
        data = await videogen.download(result.url)
        if data is None:
            await message.reply_text(f"الفيديو أكبر من حد تيليجرام (50MB). رابط التحميل:\n{result.url}")
        else:
            await message.reply_video(
                video=io.BytesIO(data),
                filename="video.mp4",
                caption=caption,
                supports_streaming=True,
                read_timeout=300,
                write_timeout=300,
            )
        await status_msg.delete()
    except Exception:
        log.exception("delivery failed")
        await status_msg.edit_text(f"تم توليد الفيديو لكن تعذّر رفعه. رابط التحميل:\n{result.url}")


def build_app() -> Application:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and fill it in.")
    if not os.environ.get("FAL_KEY"):
        raise SystemExit("FAL_KEY is missing. Copy .env.example to .env and fill it in.")
    allowed = parse_allowed_ids(os.environ.get("ALLOWED_USER_IDS"))
    if not allowed:
        log.warning("ALLOWED_USER_IDS is empty: nobody can generate yet. Send /id to the bot to get your id.")

    # concurrent_updates: a multi-minute generation must not block the next message.
    app = ApplicationBuilder().token(token).concurrent_updates(True).build()
    app.bot_data["allowed"] = allowed
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("aspect", cmd_aspect))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_prompt))
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include the bot token in URLs
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)
