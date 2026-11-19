"""Core video generation: text prompt -> AI video, via fal.ai.

Usable as a library (the Telegram bot imports it) or directly from the terminal:

    python videogen.py "a red fox running through snow at sunrise" --duration 10 --quality 1080p
    python videogen.py "a red fox running through snow at sunrise" --model local
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
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
    # USD per second of video, by resolution, from fal's model pages. Unknown = not compared.
    prices: dict[str, float] = field(default_factory=dict)


# Each model's duration/quality options, as documented by fal at the time of writing.
# If fal changes a schema, generate() falls back to the model defaults and says so.
MODELS: dict[str, ModelSpec] = {
    "veo": ModelSpec(
        "fal-ai/veo3.1/lite", "Veo 3.1 Lite — جودة عالية مع صوت",
        durations=(4, 6, 8), qualities=("720p", "1080p"), format_duration=lambda s: f"{s}s",
        prices={"720p": 0.05, "1080p": 0.08},
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
    "ltx": ModelSpec(
        "fal-ai/ltx-2.3/text-to-video/fast", "LTX 2.3 Fast — الأرخص، 1080p مع صوت",
        durations=(6, 8, 10), qualities=("1080p",), format_duration=int,
        prices={"1080p": 0.04},
    ),
}
AUTO = "auto"  # pick the fal model that best fits the requested duration and quality

# "local": LTX-2 running on this Mac through mlx-video (Apple Silicon only, see setup_mac.sh).
# Free and unmetered, but slower and lower-resolution than the fal models.
LOCAL = "local"
LOCAL_LABEL = "LTX-2 محلي على الماك — مجاني"
LOCAL_FPS = 24
LOCAL_DURATIONS = tuple(range(3, 11))  # memory use grows with length; 10s is a safe ceiling
LOCAL_SIZES = {"720p": (768, 448), "1080p": (1280, 704)}  # (width, height), multiples of 64
LOCAL_MODEL_REPO = "prince-canuma/LTX-2-distilled"  # weights pre-converted for MLX
_local_lock = asyncio.Lock()  # one local generation at a time: two would exhaust memory
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
    plan: Plan
    url: str | None = None  # fal models: where the video can be downloaded
    path: str | None = None  # local model: the generated file on this machine
    notes: tuple[str, ...] = field(default=())


def _nearest(options: tuple[int, ...], wanted: int) -> int:
    return min(options, key=lambda option: (abs(option - wanted), option))


def is_known_model(model: str) -> bool:
    return model in (AUTO, LOCAL) or model in MODELS or "/" in model


def _pick_auto(duration: int, quality: str) -> tuple[ModelSpec, str]:
    """Pick the model closest to the wanted duration, then the cheapest clip.

    A 1080p-only model may serve a 720p request when its 1080p clip costs less than the
    720p alternatives: better picture for less money.
    """
    options = []
    for spec in MODELS.values():
        if quality in spec.qualities:
            used = quality
        elif quality == "720p" and "1080p" in spec.prices:
            used = "1080p"
        else:
            continue
        actual = _nearest(spec.durations, duration)
        cost = spec.prices.get(used, float("inf")) * actual
        options.append((abs(actual - duration), cost, spec, used))
    _, _, spec, used = min(options, key=lambda o: (o[0], o[1]))
    return spec, used


def estimate_cost(plan: "Plan") -> float | None:
    """Approximate USD cost of a plan, when the model's price is known."""
    for spec in MODELS.values():
        if spec.endpoint == plan.endpoint and plan.duration and plan.quality in spec.prices:
            return round(spec.prices[plan.quality] * plan.duration, 2)
    return None


def make_plan(model: str, duration: int, quality: str) -> Plan:
    """Fit the requested duration and quality to a model. Quality wins over duration."""
    model = model.strip()
    if quality not in QUALITIES:
        raise VideoGenError(f"جودة غير مدعومة: {quality}. المتاح: {', '.join(QUALITIES)}")
    if not MIN_DURATION <= duration <= MAX_DURATION:
        raise VideoGenError(f"المدة يجب أن تكون بين {MIN_DURATION} و {MAX_DURATION} ثانية.")

    if model == LOCAL:
        notes = []
        actual = _nearest(LOCAL_DURATIONS, duration)
        if actual != duration:
            notes.append(f"تم تعديل المدة من {duration} إلى {actual} ثانية "
                         f"(أقصى مدة للموديل المحلي {max(LOCAL_DURATIONS)} ثوانٍ).")
        width, height = LOCAL_SIZES[quality]
        # LTX-2 needs 1 + 8*k frames.
        frames = round(actual * LOCAL_FPS / 8) * 8 + 1
        return Plan(LOCAL, f"{LOCAL_LABEL} ({width}×{height})",
                    {"width": width, "height": height, "num_frames": frames}, actual, quality, tuple(notes))

    if model not in MODELS and model != AUTO:
        if "/" not in model:
            raise VideoGenError(
                f"موديل غير معروف: {model}. المتاح: {AUTO}, {LOCAL}, {', '.join(MODELS)} "
                "أو معرّف endpoint كامل من fal."
            )
        # A raw endpoint id: its schema is unknown, so leave duration/quality to its defaults.
        return Plan(model, model, {}, None, None,
                    ("موديل مخصص: المدة والجودة على افتراضي الموديل.",))

    notes: list[str] = []
    if model == AUTO:
        spec, actual_quality = _pick_auto(duration, quality)
        if actual_quality != quality:
            notes.append(f"تم استخدام {actual_quality} بدل {quality} لأنها أرخص هنا "
                         f"({spec.prices[actual_quality]} دولار للثانية).")
    else:
        spec = MODELS[model]
        actual_quality = quality
    if actual_quality not in spec.qualities:
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


def local_command(prompt: str, plan: Plan, aspect_ratio: str, output_path: str) -> list[str]:
    """The mlx-video command line for one local generation."""
    width, height = plan.arguments["width"], plan.arguments["height"]
    if aspect_ratio == "9:16":
        width, height = height, width
    command = [
        os.environ.get("LOCAL_PYTHON") or sys.executable, "-m", "mlx_video.models.ltx_2.generate",
        "--prompt", prompt,
        "--width", str(width), "--height", str(height),
        "--num-frames", str(plan.arguments["num_frames"]), "--fps", str(LOCAL_FPS),
        "--model-repo", os.environ.get("LOCAL_MODEL_REPO") or LOCAL_MODEL_REPO,
        "--output-path", output_path,
    ]
    if os.environ.get("LOCAL_AUDIO") == "1":
        command.append("--audio")
    return command


async def _generate_local(
    prompt: str, plan: Plan, aspect_ratio: str, on_status: StatusCallback | None
) -> VideoResult:
    output_path = os.path.join(tempfile.mkdtemp(prefix="videobot-"), "video.mp4")
    command = local_command(prompt, plan, aspect_ratio, output_path)
    timeout = int(os.environ.get("LOCAL_TIMEOUT_SECONDS") or 3600)
    if on_status and _local_lock.locked():
        await on_status("LocalQueued")
    async with _local_lock:
        if on_status:
            await on_status("LocalInProgress")
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
        except OSError as exc:
            raise VideoGenError(f"تعذّر تشغيل الموديل المحلي: {exc}") from exc
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise VideoGenError(f"تجاوز التوليد المحلي المهلة ({timeout // 60} دقيقة).") from exc
    if process.returncode != 0 or not os.path.isfile(output_path) or not os.path.getsize(output_path):
        tail = (stderr or b"").decode(errors="replace").strip()[-600:]
        hint = " شغّل setup_mac.sh أولاً." if "No module named" in tail else ""
        raise VideoGenError(f"فشل التوليد المحلي.{hint}\n{tail}".strip())
    return VideoResult(plan=plan, path=output_path, notes=plan.notes)


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
    if plan.endpoint == LOCAL:
        return await _generate_local(prompt, plan, aspect_ratio, on_status)

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
    return VideoResult(plan=plan, url=url, notes=tuple(notes))


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
    if result.path:
        os.replace(result.path, args.out)
    else:
        print(result.url)
        data = await download(result.url, max_bytes=2**31)
        if not data:
            return 1
        with open(args.out, "wb") as fh:
            fh.write(data)
    print(f"Saved to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_cli()))
