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
    "اكتب وصف الفيديو في رسالة وسأولّده لك بالإعدادات الحالية.\n\n"
    "الأوامر:\n"
    "/duration — مدة الفيديو (من 3 إلى 20 ثانية)\n"
    "/quality — الجودة (720p أو 1080p)\n"
    "/aspect — المقاس (أفقي 16:9 أو عمودي 9:16)\n"
    "/model — الموديل (الافتراضي: اختيار تلقائي حسب المدة والجودة)\n"
    "/settings — عرض الإعدادات الحالية\n"
    "/id — عرض رقم حسابك في تيليجرام\n\n"
    "نصيحة: الوصف بالإنجليزية وبتفاصيل واضحة (المشهد، الحركة، الإضاءة، زاوية الكاميرا) "
    "يعطي نتيجة أفضل."
)

AUTO_LABEL = "تلقائي — الأنسب للمدة والجودة"


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
    data.setdefault("duration", videogen.DEFAULT_DURATION)
    data.setdefault("quality", videogen.DEFAULT_QUALITY)
    return data


def describe(settings: dict) -> str:
    """The current settings plus what they will actually produce."""
    lines = [
        f"المدة: {settings['duration']} ثانية",
        f"الجودة: {settings['quality']}",
        f"المقاس: {settings['aspect']}",
        f"الموديل: {AUTO_LABEL if settings['model'] == videogen.AUTO else settings['model']}",
    ]
    try:
        plan = videogen.make_plan(settings["model"], settings["duration"], settings["quality"])
    except videogen.VideoGenError as exc:
        return "\n".join(lines + [f"⚠️ {exc}"])
    if settings["model"] == videogen.AUTO:
        lines.append(f"سيُستخدم: {plan.label}")
    return "\n".join(lines + [f"⚠️ {note}" for note in plan.notes])


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


def _keyboard(kind: str, options: list[tuple[str, str]], current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("● " if value == current else "") + label, callback_data=f"{kind}:{value}")]
        for value, label in options
    ])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        await update.message.reply_text(f"أهلاً 👋\n{HELP}\n\nالإعدادات الحالية:\n{describe(_settings(context))}")


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _guard(update, context):
        await update.message.reply_text(describe(_settings(context)))


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"رقم حسابك: {update.effective_user.id}")


async def cmd_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    settings = _settings(context)
    if context.args:  # /duration 12
        value = context.args[0]
        if not value.isdigit() or not videogen.MIN_DURATION <= int(value) <= videogen.MAX_DURATION:
            await update.message.reply_text(
                f"اكتب رقماً بين {videogen.MIN_DURATION} و {videogen.MAX_DURATION}، مثال: /duration 12"
            )
            return
        settings["duration"] = int(value)
        await update.message.reply_text(describe(settings))
        return
    options = [(str(d), f"{d} ثانية") for d in videogen.DURATION_CHOICES]
    await update.message.reply_text(
        "اختر مدة الفيديو، أو اكتب /duration متبوعاً بعدد الثواني:",
        reply_markup=_keyboard("duration", options, str(settings["duration"])),
    )


async def cmd_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    options = [("720p", "720p — HD، أسرع وأرخص"), ("1080p", "1080p — Full HD")]
    await update.message.reply_text(
        "اختر الجودة:", reply_markup=_keyboard("quality", options, _settings(context)["quality"])
    )


async def cmd_aspect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    options = [("16:9", "أفقي 16:9 (يوتيوب)"), ("9:16", "عمودي 9:16 (Reels / Shorts)")]
    await update.message.reply_text(
        "اختر المقاس:", reply_markup=_keyboard("aspect", options, _settings(context)["aspect"])
    )


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    settings = _settings(context)
    if context.args:  # /model <short name or full fal endpoint id>
        if not videogen.is_known_model(context.args[0]):
            await update.message.reply_text(
                f"موديل غير معروف. المتاح: {videogen.AUTO}, {', '.join(videogen.MODELS)} أو معرّف endpoint من fal."
            )
            return
        settings["model"] = context.args[0]
        await update.message.reply_text(describe(settings))
        return
    options = [(videogen.AUTO, AUTO_LABEL)] + [(key, spec.label) for key, spec in videogen.MODELS.items()]
    await update.message.reply_text(
        "اختر موديل، أو اكتب /model متبوعاً بمعرّف endpoint من fal:",
        reply_markup=_keyboard("model", options, settings["model"]),
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not is_allowed(query.from_user.id, context.bot_data["allowed"]):
        return
    kind, _, value = (query.data or "").partition(":")
    settings = _settings(context)
    if kind == "model" and (value == videogen.AUTO or value in videogen.MODELS):
        settings["model"] = value
    elif kind == "aspect" and value in videogen.ASPECT_RATIOS:
        settings["aspect"] = value
    elif kind == "quality" and value in videogen.QUALITIES:
        settings["quality"] = value
    elif kind == "duration" and value.isdigit() and int(value) in videogen.DURATION_CHOICES:
        settings["duration"] = int(value)
    else:
        return
    await query.edit_message_text("تم الحفظ ✅\n" + describe(settings))


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
        result = await videogen.generate(
            message.text, settings["model"], settings["aspect"],
            settings["duration"], settings["quality"], on_status,
        )
    except videogen.VideoGenError as exc:
        await status_msg.edit_text(f"❌ {exc}")
        return
    except Exception:
        log.exception("generation failed")
        await status_msg.edit_text("❌ حدث خطأ غير متوقع أثناء التوليد. راجع سجل البوت.")
        return

    prompt = message.text if len(message.text) <= 700 else message.text[:700] + "…"
    caption = "\n".join([prompt, f"— {result.plan.label}", *(f"⚠️ {note}" for note in result.notes)])
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
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("duration", cmd_duration))
    app.add_handler(CommandHandler("quality", cmd_quality))
    app.add_handler(CommandHandler("aspect", cmd_aspect))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_prompt))
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include the bot token in URLs
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)
