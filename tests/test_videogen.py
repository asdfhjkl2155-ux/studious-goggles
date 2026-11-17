import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import videogen  # noqa: E402


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    async def subscribe(self, endpoint, arguments, on_queue_update=None, **kwargs):
        self.calls.append((endpoint, arguments))
        if on_queue_update:
            for status in (videogen.fal_client.client.Queued(position=0),
                           videogen.fal_client.client.InProgress(logs=None),
                           videogen.fal_client.client.InProgress(logs=None)):
                await on_queue_update(status)
        if self.error:
            raise self.error
        return self.result


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("result", [
    {"video": {"url": "https://x/v.mp4"}},
    {"video": "https://x/v.mp4"},
    {"videos": [{"url": "https://x/v.mp4"}]},
    {"data": {"video": {"url": "https://x/v.mp4", "content_type": "video/mp4"}}},
])
def test_extract_video_url_shapes(result):
    assert videogen.extract_video_url(result) == "https://x/v.mp4"


def test_extract_video_url_missing():
    assert videogen.extract_video_url({"seed": 1, "images": []}) is None


def test_generate_sends_prompt_and_reports_each_status_once():
    client = FakeClient(result={"video": {"url": "https://x/v.mp4"}})
    seen = []

    async def on_status(name):
        seen.append(name)

    result = run(videogen.generate("  a fox  ", "veo", "9:16", on_status, client=client))
    assert result.url == "https://x/v.mp4"
    assert client.calls == [("fal-ai/veo3.1/lite", {"prompt": "a fox", "aspect_ratio": "9:16"})]
    assert seen == ["Queued", "InProgress"]


def test_generate_accepts_raw_endpoint_id():
    client = FakeClient(result={"video": {"url": "https://x/v.mp4"}})
    result = run(videogen.generate("a fox", "fal-ai/some/model", client=client))
    assert result.endpoint == "fal-ai/some/model"


@pytest.mark.parametrize("kwargs", [
    {"prompt": "   "},
    {"prompt": "a fox", "model": "nope"},
    {"prompt": "a fox", "aspect_ratio": "4:5"},
])
def test_generate_rejects_bad_input_without_calling_api(kwargs):
    client = FakeClient(result={})
    with pytest.raises(videogen.VideoGenError):
        run(videogen.generate(client=client, **kwargs))
    assert client.calls == []


def test_generate_wraps_provider_errors_and_missing_url():
    with pytest.raises(videogen.VideoGenError, match="content policy"):
        run(videogen.generate("x", client=FakeClient(error=videogen.fal_client.client.FalClientError("content policy"))))
    with pytest.raises(videogen.VideoGenError):
        run(videogen.generate("x", client=FakeClient(result={"seed": 1})))


def test_access_control():
    allowed = bot.parse_allowed_ids(" 111, 222 ;333, abc,")
    assert allowed == {111, 222, 333}
    assert bot.is_allowed(111, allowed)
    assert not bot.is_allowed(999, allowed)
    assert not bot.is_allowed(None, allowed)
    assert not bot.is_allowed(111, bot.parse_allowed_ids(None))
