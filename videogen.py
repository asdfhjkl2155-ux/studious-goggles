"""Core video generation: text prompt -> AI video, via fal.ai.

Usable as a library (the Telegram bot imports it) or directly from the terminal:

    python videogen.py "a red fox running through snow at sunrise" --aspect 9:16
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import fal_client
import httpx

# Short name -> (fal endpoint id, label shown to the user).
# Any other fal text-to-video endpoint id can be used as-is in place of a short name.
MODELS: dict[str, tuple[str, str]] = {
    "veo": ("fal-ai/veo3.1/lite", "Veo 3.1 Lite — جودة عالية مع صوت"),
    "kling": ("kling/v4/flash/text-to-video", "Kling 4.0 Flash — سريع"),
    "wan": ("wan/v2.6/text-to-video", "Wan 2.6 — اقتصادي"),
}
DEFAULT_MODEL = "veo"
ASPECT_RATIOS = ("16:9", "9:16")
MAX_DOWNLOAD_BYTES = 49 * 1024 * 1024  # Telegram bots can upload files up to 50 MB.

StatusCallback = Callable[[str], Awaitable[None]]


class VideoGenError(Exception):
    """A generation failed; the message is safe to show to the user."""


@dataclass(frozen=True)
class VideoResult:
    url: str
    endpoint: str


def resolve_endpoint(model: str) -> str:
    """Turn a short name ("veo") or a raw endpoint id into a fal endpoint id."""
    model = model.strip()
    if model in MODELS:
        return MODELS[model][0]
    if "/" in model:
        return model
    raise VideoGenError(
        f"موديل غير معروف: {model}. المتاح: {', '.join(MODELS)} أو معرّف endpoint كامل من fal."
    )


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
    on_status: StatusCallback | None = None,
    client: Any | None = None,
) -> VideoResult:
    """Generate a video and return its URL. Raises VideoGenError on failure."""
    prompt = prompt.strip()
    if not prompt:
        raise VideoGenError("الوصف فارغ. اكتب وصف الفيديو المطلوب.")
    if aspect_ratio not in ASPECT_RATIOS:
        raise VideoGenError(f"مقاس غير مدعوم: {aspect_ratio}. المتاح: {', '.join(ASPECT_RATIOS)}")
    endpoint = resolve_endpoint(model)

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

    try:
        result = await client.subscribe(
            endpoint,
            arguments={"prompt": prompt, "aspect_ratio": aspect_ratio},
            on_queue_update=on_queue_update,
            client_timeout=900,
        )
    except fal_client.client.FalClientTimeoutError as exc:
        raise VideoGenError("انتهت مهلة التوليد (15 دقيقة). حاول مرة أخرى.") from exc
    except fal_client.client.FalClientError as exc:
        raise VideoGenError(f"رفضت خدمة التوليد الطلب: {exc}") from exc

    url = extract_video_url(result)
    if not url:
        raise VideoGenError("انتهى التوليد لكن الخدمة لم ترجع رابط فيديو.")
    return VideoResult(url=url, endpoint=endpoint)


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
    parser.add_argument("--out", default="video.mp4", help="Where to save the video")
    args = parser.parse_args()

    async def show(status: str) -> None:
        print(f"[{status}]", file=sys.stderr)

    try:
        result = await generate(args.prompt, args.model, args.aspect, on_status=show)
    except VideoGenError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(result.url)
    data = await download(result.url, max_bytes=2**31)
    if data:
        with open(args.out, "wb") as fh:
            fh.write(data)
        print(f"Saved to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_cli()))
