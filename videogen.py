"""Core video generation: text prompt -> AI video, via fal.ai.

Usable as a library (the Telegram bot imports it) or directly from the terminal:

    python videogen.py "a red fox running through snow at sunrise" --duration 10 --quality 1080p
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import fal_client
import httpx


@dataclass(frozen=True)
class ModelSpec:
    endpoint: str
    label: str
    durations: tuple[int, ...]  # clip lengths (seconds) the model accepts
    qualities: tuple[str, ...]  # output resolutions the model can produce
    format_duration: Callable[[int], Any]  # how the endpoint expects the duration value
    send_resolution: bool = True  # False when the model has one fixed resolution


# Each model's duration/quality options, as documented by fal at the time of writing.
# If fal changes a schema, generate() falls back to the model defaults and says so.
MODELS: dict[str, ModelSpec] = {
    "veo": ModelSpec(
        "fal-ai/veo3.1/lite", "Veo 3.1 Lite — جودة عالية مع صوت",
        durations=(4, 6, 8), qualities=("720p", "1080p"), format_duration=lambda s: f"{s}s",
    ),
    "wan": ModelSpec(
        "wan/v2.6/text-to-video", "Wan 2.6 — حتى 15 ثانية",
        durations=(5, 10, 15), qualities=("720p", "1080p"), format_duration=str,
    ),
    "kling": ModelSpec(
        "kling/v4/flash/text-to-video", "Kling 4.0 Flash — حتى 20 ثانية",
        durations=tuple(range(3, 21)), qualities=("720p",), format_duration=int,
        send_resolution=False,
    ),
}
AUTO = "auto"  # pick the model that best fits the requested duration and quality
DEFAULT_MODEL = AUTO
ASPECT_RATIOS = ("16:9", "9:16")
QUALITIES = ("720p", "1080p")
DEFAULT_QUALITY = "720p"
DURATION_CHOICES = (5, 8, 10, 15, 20)  # offered as buttons; any 3-20 value is accepted
DEFAULT_DURATION = 8
MIN_DURATION, MAX_DURATION = 3, 20
MAX_DOWNLOAD_BYTES = 49 * 1024 * 1024  # Telegram bots can upload files up to 50 MB.

StatusCallback = Callable[[str], Awaitable[None]]


class VideoGenError(Exception):
    """A generation failed; the message is safe to show to the user."""


@dataclass(frozen=True)
class Plan:
    """What will actually be requested, after fitting the wishes to a model."""

    endpoint: str
    label: str
    arguments: dict[str, Any]  # duration/resolution arguments, without prompt and aspect ratio
    duration: int | None  # None = left to the model's default
    quality: str | None
    notes: tuple[str, ...] = ()  # adjustments the user should be told about


@dataclass(frozen=True)
class VideoResult:
    url: str
    plan: Plan
    notes: tuple[str, ...] = field(default=())


def _nearest(options: tuple[int, ...], wanted: int) -> int:
    return min(options, key=lambda option: (abs(option - wanted), option))


def is_known_model(model: str) -> bool:
    return model == AUTO or model in MODELS or "/" in model


def make_plan(model: str, duration: int, quality: str) -> Plan:
    """Fit the requested duration and quality to a model. Quality wins over duration."""
    model = model.strip()
    if quality not in QUALITIES:
        raise VideoGenError(f"جودة غير مدعومة: {quality}. المتاح: {', '.join(QUALITIES)}")
    if not MIN_DURATION <= duration <= MAX_DURATION:
        raise VideoGenError(f"المدة يجب أن تكون بين {MIN_DURATION} و {MAX_DURATION} ثانية.")

    if model not in MODELS and model != AUTO:
        if "/" not in model:
            raise VideoGenError(
                f"موديل غير معروف: {model}. المتاح: {AUTO}, {', '.join(MODELS)} أو معرّف endpoint كامل من fal."
            )
        # A raw endpoint id: its schema is unknown, so leave duration/quality to its defaults.
        return Plan(model, model, {}, None, None,
                    ("موديل مخصص: المدة والجودة على افتراضي الموديل.",))

    if model == AUTO:
        candidates = [s for s in MODELS.values() if quality in s.qualities]
        spec = min(candidates, key=lambda s: abs(_nearest(s.durations, duration) - duration))
    else:
        spec = MODELS[model]

    notes: list[str] = []
    actual_quality = quality
    if quality not in spec.qualities:
        actual_quality = spec.qualities[-1]
        notes.append(f"هذا الموديل لا يدعم {quality}، تم استخدام {actual_quality}.")
    actual_duration = _nearest(spec.durations, duration)
    if actual_duration != duration:
        limit = f"أقصى مدة بجودة {actual_quality} هي {max(spec.durations)} ثانية" if duration > max(
            spec.durations) else f"المدد المتاحة: {', '.join(map(str, spec.durations))} ثانية"
        notes.append(f"تم تعديل المدة من {duration} إلى {actual_duration} ثانية ({limit}).")

    arguments: dict[str, Any] = {"duration": spec.format_duration(actual_duration)}
    if spec.send_resolution:
        arguments["resolution"] = actual_quality
    return Plan(spec.endpoint, spec.label, arguments, actual_duration, actual_quality, tuple(notes))


def extract_video_url(result: Any) -> str | None:
    """Find the video URL in a fal result. Models differ slightly in output shape."""
    if isinstance(result, dict):
        video = result.get("video")
        if isinstance(video, dict) and isinstance(video.get("url"), str):
            return video["url"]
        if isinstance(video, str) and video.startswith("http"):
            return video
        videos = result.get("videos")
        if isinstance(videos, list):
            for item in videos:
                found = extract_video_url({"video": item})
                if found:
                    return found
        for value in result.values():
            if isinstance(value, (dict, list)):
                found = extract_video_url(value)
                if found:
                    return found
    elif isinstance(result, list):
        for item in result:
            found = extract_video_url(item)
            if found:
                return found
    return None


async def generate(
    prompt: str,
    model: str = DEFAULT_MODEL,
    aspect_ratio: str = "16:9",
    duration: int = DEFAULT_DURATION,
    quality: str = DEFAULT_QUALITY,
    on_status: StatusCallback | None = None,
    client: Any | None = None,
) -> VideoResult:
    """Generate a video and return its URL. Raises VideoGenError on failure."""
    prompt = prompt.strip()
    if not prompt:
        raise VideoGenError("الوصف فارغ. اكتب وصف الفيديو المطلوب.")
    if aspect_ratio not in ASPECT_RATIOS:
        raise VideoGenError(f"مقاس غير مدعوم: {aspect_ratio}. المتاح: {', '.join(ASPECT_RATIOS)}")
    plan = make_plan(model, duration, quality)

    if client is None:
        if not os.environ.get("FAL_KEY"):
            raise VideoGenError("المفتاح FAL_KEY غير موجود في ملف .env")
        client = fal_client.AsyncClient()

    last_status: str | None = None

    async def on_queue_update(status: Any) -> None:
        nonlocal last_status
        name = type(status).__name__
        if on_status and name != last_status:
            last_status = name
            await on_status(name)

    async def run(extra: dict[str, Any]) -> Any:
        return await client.subscribe(
            plan.endpoint,
            arguments={"prompt": prompt, "aspect_ratio": aspect_ratio, **extra},
            on_queue_update=on_queue_update,
            client_timeout=900,
        )

    notes = list(plan.notes)
    try:
        try:
            result = await run(plan.arguments)
        except fal_client.client.FalClientHTTPError as exc:
            # 422 = the endpoint rejected an argument. If that can be the duration/quality
            # values, retry on the model's defaults rather than failing, and tell the user.
            if exc.status_code != 422 or not plan.arguments:
                raise
            result = await run({})
            notes.append("رفض الموديل إعداد المدة/الجودة، فتم التوليد بالإعدادات الافتراضية له.")
    except fal_client.client.FalClientTimeoutError as exc:
        raise VideoGenError("انتهت مهلة التوليد (15 دقيقة). حاول مرة أخرى.") from exc
    except fal_client.client.FalClientError as exc:
        raise VideoGenError(f"رفضت خدمة التوليد الطلب: {exc}") from exc

    url = extract_video_url(result)
    if not url:
        raise VideoGenError("انتهى التوليد لكن الخدمة لم ترجع رابط فيديو.")
    return VideoResult(url=url, plan=plan, notes=tuple(notes))


async def download(url: str, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes | None:
    """Download the video. Returns None when it is larger than max_bytes."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as http:
        async with http.stream("GET", url) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    return None
                chunks.append(chunk)
    return b"".join(chunks)


async def _cli() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="Generate an AI video from a text description.")
    parser.add_argument("prompt", help="Description of the video")
    parser.add_argument("--model", default=os.environ.get("DEFAULT_MODEL", DEFAULT_MODEL))
    parser.add_argument("--aspect", default="16:9", choices=ASPECT_RATIOS)
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION, help="Seconds (3-20)")
    parser.add_argument("--quality", default=DEFAULT_QUALITY, choices=QUALITIES)
    parser.add_argument("--out", default="video.mp4", help="Where to save the video")
    args = parser.parse_args()

    async def show(status: str) -> None:
        print(f"[{status}]", file=sys.stderr)

    try:
        result = await generate(args.prompt, args.model, args.aspect, args.duration, args.quality, on_status=show)
    except VideoGenError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    for note in result.notes:
        print(f"Note: {note}", file=sys.stderr)
    print(result.url)
    data = await download(result.url, max_bytes=2**31)
    if data:
        with open(args.out, "wb") as fh:
            fh.write(data)
        print(f"Saved to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_cli()))
