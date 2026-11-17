import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import videogen  # noqa: E402

fal = videogen.fal_client.client
OK = {"video": {"url": "https://x/v.mp4"}}


class FakeClient:
    """Stands in for fal: returns queued results/errors in order, then repeats the last."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    async def subscribe(self, endpoint, arguments, on_queue_update=None, **kwargs):
        self.calls.append((endpoint, arguments))
        if on_queue_update:
            for status in (fal.Queued(position=0), fal.InProgress(logs=None), fal.InProgress(logs=None)):
                await on_queue_update(status)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def http_error(status):
    return fal.FalClientHTTPError("bad input", status, {}, None)


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


@pytest.mark.parametrize("duration, quality, endpoint, arguments", [
    (8, "720p", "fal-ai/veo3.1/lite", {"duration": "8s", "resolution": "720p"}),
    (8, "1080p", "fal-ai/veo3.1/lite", {"duration": "8s", "resolution": "1080p"}),
    (5, "720p", "wan/v2.6/text-to-video", {"duration": "5", "resolution": "720p"}),
    (15, "1080p", "wan/v2.6/text-to-video", {"duration": "15", "resolution": "1080p"}),
    (20, "720p", "kling/v4/flash/text-to-video", {"duration": 20}),
    (12, "720p", "kling/v4/flash/text-to-video", {"duration": 12}),
])
def test_auto_picks_model_that_fits_exactly(duration, quality, endpoint, arguments):
    plan = videogen.make_plan("auto", duration, quality)
    assert (plan.endpoint, plan.arguments, plan.notes) == (endpoint, arguments, ())
    assert (plan.duration, plan.quality) == (duration, quality)


def test_auto_keeps_quality_and_shortens_duration_with_a_note():
    plan = videogen.make_plan("auto", 20, "1080p")
    assert plan.endpoint == "wan/v2.6/text-to-video"
    assert (plan.duration, plan.quality) == (15, "1080p")
    assert len(plan.notes) == 1 and "15" in plan.notes[0]


def test_fixed_model_adjusts_unsupported_duration_and_quality():
    plan = videogen.make_plan("veo", 20, "720p")
    assert plan.arguments == {"duration": "8s", "resolution": "720p"} and len(plan.notes) == 1
    plan = videogen.make_plan("kling", 10, "1080p")
    assert plan.arguments == {"duration": 10} and plan.quality == "720p" and len(plan.notes) == 1


def test_custom_endpoint_uses_model_defaults():
    plan = videogen.make_plan("fal-ai/some/model", 10, "1080p")
    assert (plan.endpoint, plan.arguments, plan.duration) == ("fal-ai/some/model", {}, None)
    assert plan.notes


@pytest.mark.parametrize("args", [("nope", 8, "720p"), ("auto", 2, "720p"), ("auto", 21, "720p"), ("auto", 8, "4k")])
def test_make_plan_rejects_bad_input(args):
    with pytest.raises(videogen.VideoGenError):
        videogen.make_plan(*args)


def test_generate_sends_settings_and_reports_each_status_once():
    client = FakeClient(OK)
    seen = []

    async def on_status(name):
        seen.append(name)

    result = run(videogen.generate("  a fox  ", "veo", "9:16", 6, "1080p", on_status, client=client))
    assert result.url == "https://x/v.mp4" and result.notes == ()
    assert client.calls == [("fal-ai/veo3.1/lite", {
        "prompt": "a fox", "aspect_ratio": "9:16", "duration": "6s", "resolution": "1080p"})]
    assert seen == ["Queued", "InProgress"]


def test_generate_retries_on_model_defaults_when_settings_are_rejected():
    client = FakeClient(http_error(422), OK)
    result = run(videogen.generate("a fox", "veo", client=client))
    assert [call[1] for call in client.calls] == [
        {"prompt": "a fox", "aspect_ratio": "16:9", "duration": "8s", "resolution": "720p"},
        {"prompt": "a fox", "aspect_ratio": "16:9"},
    ]
    assert len(result.notes) == 1


def test_generate_does_not_retry_other_errors():
    client = FakeClient(http_error(403))
    with pytest.raises(videogen.VideoGenError, match="bad input"):
        run(videogen.generate("a fox", client=client))
    assert len(client.calls) == 1
    # a 422 with nothing optional to drop is a real rejection (e.g. the prompt itself)
    client = FakeClient(http_error(422))
    with pytest.raises(videogen.VideoGenError):
        run(videogen.generate("a fox", "fal-ai/some/model", client=client))
    assert len(client.calls) == 1


@pytest.mark.parametrize("kwargs", [
    {"prompt": "   "},
    {"prompt": "a fox", "model": "nope"},
    {"prompt": "a fox", "aspect_ratio": "4:5"},
    {"prompt": "a fox", "duration": 60},
])
def test_generate_rejects_bad_input_without_calling_api(kwargs):
    client = FakeClient(OK)
    with pytest.raises(videogen.VideoGenError):
        run(videogen.generate(client=client, **kwargs))
    assert client.calls == []


def test_generate_reports_missing_video_url():
    with pytest.raises(videogen.VideoGenError):
        run(videogen.generate("x", client=FakeClient({"seed": 1})))


def test_describe_shows_chosen_model_and_adjustments():
    text = bot.describe({"model": "auto", "aspect": "16:9", "duration": 20, "quality": "1080p"})
    assert "Wan 2.6" in text and "⚠️" in text
    text = bot.describe({"model": "auto", "aspect": "16:9", "duration": 8, "quality": "720p"})
    assert "Veo" in text and "⚠️" not in text


def test_access_control():
    allowed = bot.parse_allowed_ids(" 111, 222 ;333, abc,")
    assert allowed == {111, 222, 333}
    assert bot.is_allowed(111, allowed)
    assert not bot.is_allowed(999, allowed)
    assert not bot.is_allowed(None, allowed)
    assert not bot.is_allowed(111, bot.parse_allowed_ids(None))
