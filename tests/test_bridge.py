"""Tests for the logic touched by the review fixes (PRs #4-#12).

Everything here runs without network access, ffmpeg, PyAV or Pillow: those paths are
covered by hand on the real container instead.
"""

import asyncio
import importlib.util
import json
import logging
import shutil
import subprocess
import sys
from types import SimpleNamespace

import nio
import pytest


def run(coro):
    return asyncio.run(coro)


# --- MediaDeduplicator ---------------------------------------------------------------


def test_dedup_marks_key_as_duplicate(bridge):
    dedup = bridge.MediaDeduplicator()
    assert dedup.is_duplicate("tg_photo_1") is False
    dedup.mark_seen("tg_photo_1")
    assert dedup.is_duplicate("tg_photo_1") is True


def test_dedup_unmark_releases_key(bridge):
    dedup = bridge.MediaDeduplicator()
    dedup.mark_seen("tg_doc_1")
    dedup.unmark("tg_doc_1")
    assert dedup.is_duplicate("tg_doc_1") is False


def test_dedup_disabled_never_tracks(bridge):
    dedup = bridge.MediaDeduplicator(enabled=False)
    dedup.mark_seen("tg_doc_2")
    assert dedup.is_duplicate("tg_doc_2") is False


def test_dedup_ignores_missing_key(bridge):
    dedup = bridge.MediaDeduplicator()
    dedup.mark_seen(None)
    assert dedup.is_duplicate(None) is False


def test_dedup_entry_expires_after_ttl(bridge, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(bridge.time, "time", lambda: clock["now"])

    dedup = bridge.MediaDeduplicator(ttl_minutes=1)
    dedup.mark_seen("tg_photo_2")

    clock["now"] += 59
    assert dedup.is_duplicate("tg_photo_2") is True

    clock["now"] += 2
    assert dedup.is_duplicate("tg_photo_2") is False


def test_dedup_evicts_oldest_entry_at_capacity(bridge):
    dedup = bridge.MediaDeduplicator(max_size=2)
    for key in ("a", "b", "c"):
        dedup.mark_seen(key)

    assert dedup.is_duplicate("a") is False
    assert dedup.is_duplicate("b") is True
    assert dedup.is_duplicate("c") is True


# --- Album guard ---------------------------------------------------------------------


def test_album_guard_window(bridge, monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock["now"])

    assert bridge.album_already_processed("album-1") is False

    bridge.PROCESSED_ALBUMS["album-1"] = clock["now"]
    assert bridge.album_already_processed("album-1") is True

    clock["now"] += bridge.ALBUM_GUARD_TTL_SECONDS + 1
    assert bridge.album_already_processed("album-1") is False
    assert "album-1" not in bridge.PROCESSED_ALBUMS


# --- Topic name cache ----------------------------------------------------------------


class FakeTopicClient:
    def __init__(self, names):
        self.names = names
        self.calls = []

    async def __call__(self, request):
        self.calls.append(request)
        topic_id = request.topics[0]
        return SimpleNamespace(topics=[SimpleNamespace(title=self.names[topic_id])])


def test_topic_name_is_cached(bridge):
    client = FakeTopicClient({7: "General"})
    chat = SimpleNamespace(id=100)

    assert run(bridge.get_topic_name(client, chat, 7)) == "General"
    assert run(bridge.get_topic_name(client, chat, 7)) == "General"
    assert len(client.calls) == 1


def test_topic_name_without_topic_id_returns_none(bridge):
    client = FakeTopicClient({})
    assert run(bridge.get_topic_name(client, SimpleNamespace(id=100), None)) is None
    assert client.calls == []


def test_topic_name_falls_back_without_caching(bridge):
    class FailingClient:
        def __init__(self):
            self.calls = 0

        async def __call__(self, request):
            self.calls += 1
            raise RuntimeError("API unavailable")

    client = FailingClient()
    chat = SimpleNamespace(id=100)

    assert run(bridge.get_topic_name(client, chat, 42)) == "Topic 42"
    assert run(bridge.get_topic_name(client, chat, 42)) == "Topic 42"
    assert client.calls == 2
    assert (100, 42) not in bridge.TOPIC_NAMES_CACHE


def test_topic_cache_evicts_oldest_entry_at_capacity(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "TOPIC_NAMES_CACHE_SIZE", 2)
    client = FakeTopicClient({1: "One", 2: "Two", 3: "Three"})
    chat = SimpleNamespace(id=100)

    for topic_id in (1, 2, 3):
        run(bridge.get_topic_name(client, chat, topic_id))

    assert len(bridge.TOPIC_NAMES_CACHE) == 2
    assert (100, 1) not in bridge.TOPIC_NAMES_CACHE
    assert (100, 2) in bridge.TOPIC_NAMES_CACHE
    assert (100, 3) in bridge.TOPIC_NAMES_CACHE


def test_topic_cache_keeps_recently_used_entry(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "TOPIC_NAMES_CACHE_SIZE", 2)
    client = FakeTopicClient({1: "One", 2: "Two", 3: "Three"})
    chat = SimpleNamespace(id=100)

    run(bridge.get_topic_name(client, chat, 1))
    run(bridge.get_topic_name(client, chat, 2))
    run(bridge.get_topic_name(client, chat, 1))  # refresh entry 1
    run(bridge.get_topic_name(client, chat, 3))  # evicts the least recently used

    assert (100, 1) in bridge.TOPIC_NAMES_CACHE
    assert (100, 2) not in bridge.TOPIC_NAMES_CACHE
    assert (100, 3) in bridge.TOPIC_NAMES_CACHE


# --- Safety verdicts -----------------------------------------------------------------


def test_safety_is_skipped_without_api_url(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "LLAMAGUARD_API_URL", None)
    verdict = run(bridge.check_media_safety(b"bytes", "image/jpeg", "chat", "a.jpg"))
    assert verdict is bridge.SafetyVerdict.SAFE


def test_video_without_extractable_frames_is_unavailable(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "LLAMAGUARD_API_URL", "https://guard.test.invalid/v1")
    monkeypatch.setattr(bridge, "extract_video_frames", lambda *a, **kw: [])
    verdict = run(bridge.check_media_safety(b"bytes", "video/mp4", "chat", "a.mp4"))
    assert verdict is bridge.SafetyVerdict.UNAVAILABLE


def test_failed_frame_classification_is_unavailable(bridge, monkeypatch):
    async def failing_classifier(*args, **kwargs):
        return None, []

    monkeypatch.setattr(bridge, "LLAMAGUARD_API_URL", "https://guard.test.invalid/v1")
    monkeypatch.setattr(bridge, "extract_video_frames", lambda *a, **kw: [b"frame1", b"frame2"])
    monkeypatch.setattr(bridge, "get_image_safety_classification", failing_classifier)

    verdict = run(bridge.check_media_safety(b"bytes", "video/mp4", "chat", "a.mp4"))
    assert verdict is bridge.SafetyVerdict.UNAVAILABLE


def test_unsafe_frame_blocks_by_default(bridge, monkeypatch):
    async def unsafe_classifier(*args, **kwargs):
        return False, ["VIOLENCE"]

    monkeypatch.setattr(bridge, "LLAMAGUARD_API_URL", "https://guard.test.invalid/v1")
    monkeypatch.setattr(bridge, "extract_video_frames", lambda *a, **kw: [b"frame1"])
    monkeypatch.setattr(bridge, "get_image_safety_classification", unsafe_classifier)
    monkeypatch.setattr(bridge, "LLAMAGUARD_CHECKS", set())
    monkeypatch.setattr(bridge, "LLAMAGUARD_REQUIRE_CHECKS", set())

    verdict = run(bridge.check_media_safety(b"bytes", "video/mp4", "chat", "a.mp4"))
    assert verdict is bridge.SafetyVerdict.BLOCKED


def test_whitelist_passes_when_one_frame_matches(bridge, monkeypatch):
    async def mixed_classifier(image_bytes, *args, **kwargs):
        if image_bytes == b"dog":
            return False, ["DOGS"]
        return True, []

    monkeypatch.setattr(bridge, "LLAMAGUARD_API_URL", "https://guard.test.invalid/v1")
    monkeypatch.setattr(bridge, "extract_video_frames", lambda *a, **kw: [b"dog", b"park"])
    monkeypatch.setattr(bridge, "get_image_safety_classification", mixed_classifier)
    monkeypatch.setattr(bridge, "LLAMAGUARD_CHECKS", set())
    monkeypatch.setattr(bridge, "LLAMAGUARD_REQUIRE_CHECKS", {"DOGS"})

    verdict = run(bridge.check_media_safety(b"bytes", "video/mp4", "chat", "a.mp4"))
    assert verdict is bridge.SafetyVerdict.SAFE


def test_whitelist_blocks_when_no_frame_matches(bridge, monkeypatch):
    async def safe_classifier(*args, **kwargs):
        return True, []

    monkeypatch.setattr(bridge, "LLAMAGUARD_API_URL", "https://guard.test.invalid/v1")
    monkeypatch.setattr(bridge, "extract_video_frames", lambda *a, **kw: [b"frame1", b"frame2"])
    monkeypatch.setattr(bridge, "get_image_safety_classification", safe_classifier)
    monkeypatch.setattr(bridge, "LLAMAGUARD_CHECKS", set())
    monkeypatch.setattr(bridge, "LLAMAGUARD_REQUIRE_CHECKS", {"DOGS"})

    verdict = run(bridge.check_media_safety(b"bytes", "video/mp4", "chat", "a.mp4"))
    assert verdict is bridge.SafetyVerdict.BLOCKED


# --- MIME type forwarded to the classifier -------------------------------------------


def test_image_mime_type_is_passed_through(bridge, monkeypatch):
    seen = {}

    async def capture(image_bytes, source_chat, filename, index_label=None, mime_type="image/jpeg"):
        seen["mime"] = mime_type
        return bridge.SafetyVerdict.SAFE

    monkeypatch.setattr(bridge, "LLAMAGUARD_API_URL", "https://guard.test.invalid/v1")
    monkeypatch.setattr(bridge, "check_single_image_safety", capture)

    run(bridge.check_media_safety(b"png-bytes", "image/png", "chat", "a.png"))
    assert seen["mime"] == "image/png"


def test_video_frame_is_always_jpeg(bridge, monkeypatch):
    seen = {}

    async def capture(image_bytes, source_chat, filename, index_label=None, mime_type="image/jpeg"):
        seen["mime"] = mime_type
        return bridge.SafetyVerdict.SAFE

    monkeypatch.setattr(bridge, "LLAMAGUARD_API_URL", "https://guard.test.invalid/v1")
    monkeypatch.setattr(bridge, "extract_video_frames", lambda *a, **kw: [b"frame"])
    monkeypatch.setattr(bridge, "check_single_image_safety", capture)

    run(bridge.check_media_safety(b"mp4-bytes", "video/mp4", "chat", "a.mp4"))
    assert seen["mime"] == "image/jpeg"


# --- Frame extraction failures (issue #47) -------------------------------------------
#
# The suite runs without PyAV and Pillow, so a stand-in `av` module replaces the decoder.
# It decodes `good_frames` frames and then raises, like PyAV does on a corrupted packet.


class FakeDecoderError(Exception):
    """Stands in for av.error.InvalidDataError."""


class FakeImage:
    def __init__(self, index):
        self.index = index

    def save(self, buf, format):
        buf.write(f"jpeg-{self.index}".encode())


class FakeFrame:
    def __init__(self, index):
        self.index = index

    def to_image(self):
        return FakeImage(self.index)


class FakeContainer:
    def __init__(self, total_frames, good_frames):
        self.total_frames = total_frames
        self.good_frames = good_frames
        stream = SimpleNamespace(frames=total_frames, duration=None, time_base=None, average_rate=None)
        self.streams = SimpleNamespace(video=[stream])
        self.closed = False

    def decode(self, video=0):
        for i in range(self.total_frames):
            if self.good_frames is not None and i >= self.good_frames:
                raise FakeDecoderError("Invalid data found when processing input")
            yield FakeFrame(i)

    def seek(self, offset):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def fake_av(monkeypatch):
    """Installs a stand-in `av` module; returns a function that configures the next clip."""
    containers = []

    def configure(total_frames, good_frames=None):
        container = FakeContainer(total_frames, good_frames)
        containers.append(container)
        return container

    fake_module = SimpleNamespace(open=lambda _buf: containers[-1])
    monkeypatch.setitem(sys.modules, "av", fake_module)

    real_find_spec = importlib.util.find_spec

    def find_spec(name, *args, **kwargs):
        if name == "PIL":
            return object()
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    return configure


@pytest.mark.parametrize("total_frames", [0, 10], ids=["unknown-length", "indexed"])
def test_decoder_failure_after_partial_extraction_discards_frames(bridge, fake_av, total_frames):
    # total_frames=0 takes the "decode the first frames" path; 10 takes the indexed sampling
    # path. In both, two frames decode before the decoder raises.
    container = fake_av(total_frames=total_frames or 10, good_frames=2)
    container.streams.video[0].frames = total_frames

    frames = bridge.extract_video_frames(b"damaged", max_frames=5, use_random=False)

    assert frames == []
    assert container.closed


def test_clean_video_sampling_still_succeeds(bridge, fake_av):
    fake_av(total_frames=10)
    frames = bridge.extract_video_frames(b"clean", max_frames=5, use_random=False)
    assert len(frames) == 5


def test_clean_video_shorter_than_sample_count_still_succeeds(bridge, fake_av):
    fake_av(total_frames=3)
    frames = bridge.extract_video_frames(b"short", max_frames=5, use_random=False)
    assert frames == [b"jpeg-0", b"jpeg-1", b"jpeg-2"]


def test_partial_extraction_is_unavailable_never_safe(bridge, fake_av, monkeypatch):
    async def safe_classifier(*args, **kwargs):
        return True, []

    fake_av(total_frames=10, good_frames=2)
    monkeypatch.setattr(bridge, "LLAMAGUARD_API_URL", "https://guard.test.invalid/v1")
    monkeypatch.setattr(bridge, "get_image_safety_classification", safe_classifier)

    verdict = run(bridge.check_media_safety(b"damaged", "video/mp4", "chat", "a.mp4"))
    assert verdict is bridge.SafetyVerdict.UNAVAILABLE


def test_partial_extraction_sends_nothing_and_releases_dedup_marks(bridge, fake_av, monkeypatch):
    async def safe_classifier(*args, **kwargs):
        return True, []

    class NoSendClient:
        async def upload(self, *args, **kwargs):
            raise AssertionError("nothing may be uploaded for an unclassified video")

        async def room_send(self, *args, **kwargs):
            raise AssertionError("no Matrix event may be sent for an unclassified video")

    video_bytes = b"damaged-video-issue-47"

    async def download_media(file=None):
        return video_bytes

    message = SimpleNamespace(
        id=4747,
        photo=None,
        video=SimpleNamespace(),
        document=SimpleNamespace(id=474747),
        file=SimpleNamespace(mime_type="video/mp4", name="damaged.mp4", size=len(video_bytes)),
        download_media=download_media,
    )

    fake_av(total_frames=10, good_frames=2)
    monkeypatch.setattr(bridge, "LLAMAGUARD_API_URL", "https://guard.test.invalid/v1")
    monkeypatch.setattr(bridge, "ENABLE_VIDEOS", True)
    monkeypatch.setattr(bridge, "get_image_safety_classification", safe_classifier)
    monkeypatch.setattr(bridge, "matrix_client", NoSendClient())

    run(bridge.process_and_upload_media(message, "chat", "chan"))

    assert bridge.SAFETY_STATS == {"blocked": 0, "unavailable": 1}
    content_key = f"sha256_{bridge.compute_sha256(video_bytes)}"
    assert bridge.media_deduplicator.is_duplicate("tg_doc_474747") is False
    assert bridge.media_deduplicator.is_duplicate(content_key) is False


# --- Classifier response parsing (issue #48) -----------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("safe", (True, [])),
        ("Safe", (True, [])),
        ("unsafe\nS4", (False, ["S4"])),
        ("unsafe\nS1,S10", (False, ["S1", "S10"])),
        ("unsafe\nS1, S10", (False, ["S1", "S10"])),
        ("unsafe\nS4; S12", (False, ["S4", "S12"])),
        ("unsafe\nS4.", (False, ["S4"])),
        ("unsafe\nS12, S4.", (False, ["S12", "S4"])),
        ("unsafe s4", (False, ["S4"])),
        ("unsafe\nS4,S4", (False, ["S4"])),
    ],
)
def test_parse_classification_accepts_complete_responses(bridge, text, expected):
    assert bridge.parse_classification(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "unsafe",
        "unsafe\n.",
        "unsafe\nS15",
        "unsafe\nS0",
        "unsafe\nS4: Child Sexual Exploitation",
        "unsafe\nS12 S4?",
        "unsafe\nS4..",
        "unsafe\nS12, and S4",
        "safe\nS4",
        "unknown\nS4",
    ],
)
def test_parse_classification_rejects_incomplete_responses(bridge, text):
    with pytest.raises(bridge.ClassificationParseError):
        bridge.parse_classification(text)


class FakeGuardResponse:
    def __init__(self, content):
        self.body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


@pytest.fixture
def guard_replies(bridge, monkeypatch):
    """Replaces only the HTTP call; returns a function that sets the replies, one per call."""
    import urllib.request

    replies = []

    def urlopen(request, timeout=None):
        return FakeGuardResponse(replies.pop(0))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(bridge, "LLAMAGUARD_API_URL", "https://guard.test.invalid/v1")

    def set_replies(*contents):
        replies[:] = list(contents)

    return set_replies


@pytest.mark.parametrize(
    "reply, expected",
    [
        # The issue's reproductions: a missing or dropped S4 used to come back SAFE.
        ("unsafe", "UNAVAILABLE"),
        ("unsafe\nS4.", "BLOCKED"),
        ("unsafe\nS4; S12", "BLOCKED"),
        ("unsafe\nS4: Child Sexual Exploitation", "UNAVAILABLE"),
        ("unsafe\nS15", "UNAVAILABLE"),
        # A complete response outside the block list still follows the selective policy.
        ("unsafe\nS12", "SAFE"),
        ("unsafe\nS4", "BLOCKED"),
        ("safe", "SAFE"),
    ],
)
def test_image_block_list_verdicts(bridge, guard_replies, monkeypatch, reply, expected):
    monkeypatch.setattr(bridge, "LLAMAGUARD_CHECKS", {"S4"})
    guard_replies(reply)
    verdict = run(bridge.check_media_safety(b"img", "image/jpeg", "chat", "a.jpg"))
    assert verdict is bridge.SafetyVerdict[expected]


@pytest.mark.parametrize(
    "reply, expected",
    [
        # README example (require S12, block S4): S4 must never be lost behind S12.
        ("unsafe\nS12, S4.", "BLOCKED"),
        ("unsafe\nS4; S12", "BLOCKED"),
        ("unsafe\nS12 S4?", "UNAVAILABLE"),
        ("unsafe\nS12", "SAFE"),
        ("unsafe", "UNAVAILABLE"),
        ("safe", "BLOCKED"),
    ],
)
def test_image_whitelist_verdicts(bridge, guard_replies, monkeypatch, reply, expected):
    monkeypatch.setattr(bridge, "LLAMAGUARD_CHECKS", {"S4"})
    monkeypatch.setattr(bridge, "LLAMAGUARD_REQUIRE_CHECKS", {"S12"})
    guard_replies(reply)
    verdict = run(bridge.check_media_safety(b"img", "image/jpeg", "chat", "a.jpg"))
    assert verdict is bridge.SafetyVerdict[expected]


def test_unsafe_without_category_still_blocks_by_default(bridge, guard_replies):
    """With both lists empty, any complete 'unsafe' blocks; an incomplete one is unavailable."""
    guard_replies("unsafe\nS12")
    assert run(bridge.check_media_safety(b"img", "image/jpeg", "chat", "a.jpg")) is bridge.SafetyVerdict.BLOCKED
    guard_replies("unsafe")
    assert run(bridge.check_media_safety(b"img", "image/jpeg", "chat", "a.jpg")) is bridge.SafetyVerdict.UNAVAILABLE


@pytest.mark.parametrize("require", [set(), {"S12"}], ids=["block-list", "whitelist"])
def test_video_with_one_incomplete_frame_is_unavailable(bridge, guard_replies, monkeypatch, require):
    # The other frame alone would pass (block-list) or satisfy the whitelist.
    monkeypatch.setattr(bridge, "LLAMAGUARD_CHECKS", {"S4"})
    monkeypatch.setattr(bridge, "LLAMAGUARD_REQUIRE_CHECKS", require)
    monkeypatch.setattr(bridge, "extract_video_frames", lambda *a, **kw: [b"frame1", b"frame2"])
    guard_replies("unsafe\nS12", "unsafe\nS12; S4:")
    verdict = run(bridge.check_media_safety(b"vid", "video/mp4", "chat", "a.mp4"))
    assert verdict is bridge.SafetyVerdict.UNAVAILABLE


def test_video_with_complete_frames_follows_the_policy(bridge, guard_replies, monkeypatch):
    monkeypatch.setattr(bridge, "LLAMAGUARD_CHECKS", {"S4"})
    monkeypatch.setattr(bridge, "extract_video_frames", lambda *a, **kw: [b"frame1", b"frame2"])
    guard_replies("unsafe\nS12", "safe")
    assert run(bridge.check_media_safety(b"vid", "video/mp4", "chat", "a.mp4")) is bridge.SafetyVerdict.SAFE
    guard_replies("unsafe\nS12", "unsafe\nS12; S4.")
    assert run(bridge.check_media_safety(b"vid", "video/mp4", "chat", "a.mp4")) is bridge.SafetyVerdict.BLOCKED


def test_incomplete_classification_sends_nothing_and_releases_dedup_marks(bridge, guard_replies, monkeypatch):
    class NoSendClient:
        async def upload(self, *args, **kwargs):
            raise AssertionError("nothing may be uploaded for an unclassified image")

        async def room_send(self, *args, **kwargs):
            raise AssertionError("no Matrix event may be sent for an unclassified image")

    image_bytes = b"image-with-incomplete-classification-issue-48"

    async def download_media(file=None):
        return image_bytes

    message = SimpleNamespace(
        id=4848,
        photo=SimpleNamespace(id=484848),
        video=None,
        document=None,
        file=SimpleNamespace(mime_type="image/jpeg", name="a.jpg", size=len(image_bytes)),
        download_media=download_media,
    )

    monkeypatch.setattr(bridge, "LLAMAGUARD_CHECKS", {"S4"})
    monkeypatch.setattr(bridge, "ENABLE_IMAGES", True)
    monkeypatch.setattr(bridge, "matrix_client", NoSendClient())
    guard_replies("unsafe")

    run(bridge.process_and_upload_media(message, "chat", "chan"))

    assert bridge.SAFETY_STATS == {"blocked": 0, "unavailable": 1}
    content_key = f"sha256_{bridge.compute_sha256(image_bytes)}"
    assert bridge.media_deduplicator.is_duplicate("tg_photo_484848") is False
    assert bridge.media_deduplicator.is_duplicate(content_key) is False


# --- Delivery semantics --------------------------------------------------------------


class FakeMatrixClient:
    def __init__(self, upload_ok=True, room_results=None):
        self.upload_ok = upload_ok
        self.room_results = list(room_results or [])
        self.sent_rooms = []
        self.upload_count = 0

    async def upload(self, *args, **kwargs):
        self.upload_count += 1
        if not self.upload_ok:
            return object(), {}
        return nio.UploadResponse(content_uri="mxc://test.invalid/upload1"), {}

    async def room_send(self, room_id, message_type, content=None):
        self.sent_rooms.append(room_id)
        return self.room_results.pop(0)


def test_delivery_reports_success_for_all_rooms(bridge, monkeypatch):
    client = FakeMatrixClient(
        room_results=[nio.RoomSendResponse(event_id="$1", room_id="!a:test.invalid")]
    )
    monkeypatch.setattr(bridge, "matrix_client", client)

    ok = run(
        bridge.send_matrix_media_event(
            b"data", "image/jpeg", "a.jpg", "m.image", {"w": 1, "h": 1}, "chat", "chan"
        )
    )

    assert ok is True
    assert bridge.DELIVERY_STATS == {"failed": 0, "partial": 0}


def test_delivery_flags_partial_fan_out(bridge, monkeypatch):
    client = FakeMatrixClient(
        room_results=[
            nio.RoomSendResponse(event_id="$1", room_id="!a:test.invalid"),
            nio.RoomSendError(message="forbidden", status_code="403", room_id="!b:test.invalid"),
        ]
    )
    monkeypatch.setattr(bridge, "matrix_client", client)
    monkeypatch.setattr(bridge, "MATRIX_ROOM_IDS", ["!a:test.invalid", "!b:test.invalid"])

    ok = run(
        bridge.send_matrix_media_event(
            b"data", "image/jpeg", "a.jpg", "m.image", {"w": 1, "h": 1}, "chat", "chan"
        )
    )

    assert ok is True
    assert bridge.DELIVERY_STATS == {"failed": 0, "partial": 1}


def test_delivery_reports_total_failure(bridge, monkeypatch):
    client = FakeMatrixClient(
        room_results=[
            nio.RoomSendError(message="forbidden", status_code="403", room_id="!a:test.invalid"),
        ]
    )
    monkeypatch.setattr(bridge, "matrix_client", client)

    ok = run(
        bridge.send_matrix_media_event(
            b"data", "image/jpeg", "a.jpg", "m.image", {"w": 1, "h": 1}, "chat", "chan"
        )
    )

    assert ok is False
    assert bridge.DELIVERY_STATS == {"failed": 1, "partial": 0}


def test_delivery_fails_when_upload_is_rejected(bridge, monkeypatch):
    client = FakeMatrixClient(upload_ok=False)
    monkeypatch.setattr(bridge, "matrix_client", client)

    ok = run(
        bridge.send_matrix_media_event(
            b"data", "image/jpeg", "a.jpg", "m.image", {"w": 1, "h": 1}, "chat", "chan"
        )
    )

    assert ok is False


# --- Command reply escaping ----------------------------------------------------------


def test_unknown_command_html_is_escaped(bridge, monkeypatch):
    sent = {}

    async def capture(room_id, plain_text, html_text):
        sent["plain"] = plain_text
        sent["html"] = html_text

    monkeypatch.setattr(bridge, "send_matrix_notice", capture)
    monkeypatch.setattr(bridge, "ADMIN_MATRIX_USER_ID", "@admin:test.invalid")

    room = SimpleNamespace(room_id=bridge.MATRIX_ROOM_IDS[0])
    event = SimpleNamespace(
        body="!tmmb <script>alert(1)</script>",
        sender="@admin:test.invalid",
        server_timestamp=None,
    )

    run(bridge.on_matrix_message(room, event))

    assert "&lt;script&gt;" in sent["html"]
    assert "<script>" not in sent["html"]


# --- Ramdisk scratch space -----------------------------------------------------------


def test_shm_required_bytes_covers_input_plus_parts(bridge):
    media_size = 10 * 1024 * 1024
    assert bridge.shm_required_bytes(media_size) == 2 * media_size + bridge.SHM_HEADROOM_BYTES


def test_reserve_shm_fails_without_ramdisk(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "shm_space", lambda: None)
    assert run(bridge.reserve_shm_space(4096, "chat", "a.mp4")) is False


def test_reserve_shm_rejects_media_larger_than_ramdisk(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "shm_space", lambda: (1024, 1024))
    assert run(bridge.reserve_shm_space(4096, "chat", "a.mp4")) is False


def test_reserve_shm_succeeds_when_enough_is_free(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "shm_space", lambda: (10**9, 10**9))
    assert run(bridge.reserve_shm_space(4096, "chat", "a.mp4")) is True


def test_reserve_shm_gives_up_after_timeout(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "shm_space", lambda: (10**9, 0))
    monkeypatch.setattr(bridge, "SHM_WAIT_TIMEOUT_SECONDS", 0)
    assert run(bridge.reserve_shm_space(4096, "chat", "a.mp4")) is False


# --- ffprobe helpers -----------------------------------------------------------------


def make_fake_ffprobe(tmp_path, output, exit_code=0):
    """Writes an executable stub that behaves like `ffprobe -v error ...` for one case."""
    script = tmp_path / "ffprobe"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\nexit {exit_code}\n")
    script.chmod(0o755)
    return script


def test_find_ffprobe_prefers_path(bridge, monkeypatch):
    monkeypatch.setattr(bridge.shutil, "which", lambda name: "/usr/bin/ffprobe")
    assert bridge.find_ffprobe("/usr/bin/ffmpeg") == "/usr/bin/ffprobe"


def test_find_ffprobe_uses_ffmpeg_directory(bridge, monkeypatch, tmp_path):
    monkeypatch.setattr(bridge.shutil, "which", lambda name: None)
    ffmpeg_bin = tmp_path / "ffmpeg"
    ffmpeg_bin.write_text("#!/bin/sh\n")
    ffprobe_bin = tmp_path / "ffprobe"
    ffprobe_bin.write_text("#!/bin/sh\n")

    assert bridge.find_ffprobe(str(ffmpeg_bin)) == str(ffprobe_bin)


def test_find_ffprobe_returns_none_when_missing(bridge, monkeypatch):
    monkeypatch.setattr(bridge.shutil, "which", lambda name: None)
    assert bridge.find_ffprobe(None) is None
    assert bridge.find_ffprobe("/nonexistent/ffmpeg") is None


def test_probe_duration_parses_ffprobe_output(bridge, monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "find_ffprobe", lambda ffmpeg_bin: str(make_fake_ffprobe(tmp_path, "2.5")))
    assert bridge.probe_video_duration("a.mp4", None) == pytest.approx(2.5)


def test_probe_duration_ignores_unparsable_output(bridge, monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "find_ffprobe", lambda ffmpeg_bin: str(make_fake_ffprobe(tmp_path, "N/A")))
    assert bridge.probe_video_duration("a.mp4", None) is None


def test_probe_duration_ignores_failed_ffprobe(bridge, monkeypatch, tmp_path):
    script = make_fake_ffprobe(tmp_path, "2.5", exit_code=1)
    monkeypatch.setattr(bridge, "find_ffprobe", lambda ffmpeg_bin: str(script))
    assert bridge.probe_video_duration("a.mp4", None) is None


def test_probe_dimensions_parses_ffprobe_output(bridge, monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "find_ffprobe", lambda ffmpeg_bin: str(make_fake_ffprobe(tmp_path, "320x240")))
    assert bridge.probe_video_dimensions("a.mp4", None) == (320, 240)


def test_probe_dimensions_ignores_unparsable_output(bridge, monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "find_ffprobe", lambda ffmpeg_bin: str(make_fake_ffprobe(tmp_path, "nonsense")))
    assert bridge.probe_video_dimensions("a.mp4", None) is None

    monkeypatch.setattr(bridge, "find_ffprobe", lambda ffmpeg_bin: str(make_fake_ffprobe(tmp_path, "320")))
    assert bridge.probe_video_dimensions("a.mp4", None) is None


@pytest.mark.skipif(
    shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None,
    reason="needs a real ffmpeg/ffprobe installation",
)
def test_probe_against_a_real_video(bridge, tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc=duration=2:size=320x240:rate=10",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", str(clip), "-y",
        ],
        check=True,
        timeout=60,
    )

    duration = bridge.probe_video_duration(str(clip), shutil.which("ffmpeg"))
    assert duration == pytest.approx(2.0, abs=0.1)
    assert bridge.probe_video_dimensions(str(clip), shutil.which("ffmpeg")) == (320, 240)


# --- Module logger -------------------------------------------------------------------


def test_log_records_use_the_module_logger(bridge, caplog):
    class FailingClient:
        async def __call__(self, request):
            raise RuntimeError("API unavailable")

    with caplog.at_level(logging.DEBUG):
        run(bridge.get_topic_name(FailingClient(), SimpleNamespace(id=100), 5))

    module_records = [r for r in caplog.records if r.name == "tgmabr"]
    assert module_records, [r.name for r in caplog.records]
    assert any("Could not fetch topic name" in r.getMessage() for r in module_records)


def test_ffprobe_stdout_returns_none_on_timeout(bridge, tmp_path):
    script = tmp_path / "ffprobe"
    script.write_text("#!/bin/sh\nsleep 2\n")
    script.chmod(0o755)

    assert bridge.ffprobe_stdout(str(script), ["-version"], timeout=0.2) is None


def test_ffprobe_stdout_surfaces_programming_errors(bridge, monkeypatch):
    """The narrowed except must not turn a bug into a silent "no output"."""

    def broken_run(*args, **kwargs):
        raise TypeError("ffprobe_bin must be a string")

    monkeypatch.setattr(bridge.subprocess, "run", broken_run)

    with pytest.raises(TypeError):
        bridge.ffprobe_stdout("/usr/bin/ffprobe", ["-version"])


# --- Wiring --------------------------------------------------------------------------


def test_entry_point_registers_the_telegram_handler(bridge):
    """Importing bridge must register the Telegram listener, or nothing would be bridged."""
    handlers = bridge.tg_client.list_event_handlers()
    assert handlers, "no Telegram event handler registered"


# --- Session file permissions --------------------------------------------------------


def test_session_file_is_not_world_readable(bridge):
    """The Telethon session holds the account auth key; 0644 on a shared host is a leak."""
    mode = bridge.session_file.stat().st_mode & 0o777
    assert oct(mode) == "0o600", f"session file mode is {oct(mode)}"


def test_restrict_session_permissions_tightens_an_existing_file(bridge, tmp_path):
    session = tmp_path / "tgmabr"
    session_file = tmp_path / "tgmabr.session"
    session_file.write_text("auth key")
    session_file.chmod(0o644)

    bridge.state.restrict_session_permissions(str(session))

    assert oct(session_file.stat().st_mode & 0o777) == "0o600"


def test_restrict_session_permissions_ignores_missing_file(bridge, tmp_path):
    bridge.state.restrict_session_permissions(str(tmp_path / "absent"))


# --- Alternative video qualities ------------------------------------------------------

def _rendition(size_mb, width, height, duration=60.0, video_start_ts=None):
    attr = SimpleNamespace(w=width, h=height, duration=duration, video_start_ts=video_start_ts)
    return SimpleNamespace(size=int(size_mb * 1024 * 1024), attributes=[attr])


def _message_with_alternatives(alternatives):
    return SimpleNamespace(media=SimpleNamespace(alt_documents=alternatives))


UPLOAD_LIMIT = 80 * 1024 * 1024
PROCESSING_LIMIT = 200 * 1024 * 1024


def test_renditions_are_grouped_by_resolution_and_labelled_against_both_limits(bridge):
    # Mirrors a real log line: two 720p renditions, two 480p renditions and the preview
    # clips Telegram attaches to each of them.
    message = _message_with_alternatives([
        _rendition(111.58, 1280, 720),
        _rendition(0.02, 144, 80, duration=0.0),
        _rendition(150.33, 1280, 720),
        _rendition(0.02, 144, 80, duration=0.0),
        _rendition(57.89, 852, 480),
        _rendition(0.02, 144, 80, duration=0.0),
        _rendition(73.62, 852, 480),
        SimpleNamespace(size=140000, attributes=[]),
    ])

    described = bridge.describe_alt_documents(message, UPLOAD_LIMIT, PROCESSING_LIMIT)

    assert "4 usable rendition(s) in 2 resolution(s)" in described
    assert "4 preview clip(s) ignored" in described
    assert "1280x720: 150.33 MB, 111.58 MB (above the upload limit, small enough for processing)" in described
    assert "852x480: 73.62 MB, 57.89 MB (largest fitting the upload limit: 73.62 MB)" in described


def test_resolution_group_above_the_processing_limit_is_labelled_as_such(bridge):
    message = _message_with_alternatives([_rendition(863.43, 1920, 1080)])

    described = bridge.describe_alt_documents(message, UPLOAD_LIMIT, PROCESSING_LIMIT)

    assert "above the processing limit as well" in described


def test_preview_clips_are_not_treated_as_renditions(bridge):
    assert not bridge.is_rendition_document(_rendition(0.02, 144, 80, duration=0.0))
    assert not bridge.is_rendition_document(_rendition(20, 1280, 720, video_start_ts=12.0))
    assert not bridge.is_rendition_document(SimpleNamespace(size=140000, attributes=[]))
    assert bridge.is_rendition_document(_rendition(20, 1280, 720))


def test_only_preview_clips_is_reported_separately(bridge):
    message = _message_with_alternatives([_rendition(0.02, 144, 80, duration=0.0)])

    described = bridge.describe_alt_documents(message, UPLOAD_LIMIT, PROCESSING_LIMIT)

    assert "none of them a usable rendition" in described


def test_selector_prefers_highest_resolution_that_fits_the_upload_limit(bridge):
    message = _message_with_alternatives([
        _rendition(150.33, 1280, 720),
        _rendition(111.58, 1280, 720),
        _rendition(73.62, 852, 480),
        _rendition(57.89, 852, 480),
    ])

    document, mode = bridge.select_alternate_rendition(message, UPLOAD_LIMIT, PROCESSING_LIMIT)

    assert mode == bridge.DIRECT_UPLOAD
    assert document.size == int(73.62 * 1024 * 1024)


def test_selector_falls_back_to_the_best_processing_candidate(bridge):
    message = _message_with_alternatives([
        _rendition(250, 1920, 1080),
        _rendition(150.33, 1280, 720),
        _rendition(111.58, 1280, 720),
    ])

    document, mode = bridge.select_alternate_rendition(message, UPLOAD_LIMIT, PROCESSING_LIMIT)

    assert mode == bridge.NEEDS_PROCESSING
    assert document.size == int(150.33 * 1024 * 1024)


def test_selector_returns_nothing_when_every_rendition_is_too_large(bridge):
    message = _message_with_alternatives([_rendition(863.43, 1920, 1080)])

    assert bridge.select_alternate_rendition(message, UPLOAD_LIMIT, PROCESSING_LIMIT) == (None, None)


def test_selector_never_picks_a_preview_clip(bridge):
    message = _message_with_alternatives([_rendition(0.02, 144, 80, duration=0.0)])

    assert bridge.select_alternate_rendition(message, UPLOAD_LIMIT, PROCESSING_LIMIT) == (None, None)


def test_rendition_choice_log_names_primary_size_and_rendition(bridge):
    line = bridge.describe_rendition_choice(
        863 * 1024 * 1024, _rendition(73.62, 852, 480), bridge.DIRECT_UPLOAD
    )

    assert "863.0 MB" in line
    assert "73.62 MB" in line
    assert f"{bridge.MAX_MEDIA_SIZE_MB} MB upload limit" in line
    assert "without processing" in line


def test_alternatives_are_reported_as_absent(bridge):
    expected = "Telegram lists no alternative qualities for this video."

    assert bridge.describe_alt_documents(_message_with_alternatives([]), UPLOAD_LIMIT, PROCESSING_LIMIT) == expected
    assert bridge.describe_alt_documents(_message_with_alternatives(None), UPLOAD_LIMIT, PROCESSING_LIMIT) == expected
    assert bridge.describe_alt_documents(SimpleNamespace(media=None), UPLOAD_LIMIT, PROCESSING_LIMIT) == expected


# --- Per-part thumbnails -------------------------------------------------------------


class RecordingMatrixClient:
    """Records uploads and room events so individual thumbnails can be told apart."""

    def __init__(self):
        self.uploaded_filenames = []
        self.events = []

    async def upload(self, stream, content_type=None, filename=None, filesize=None):
        self.uploaded_filenames.append(filename)
        return nio.UploadResponse(content_uri=f"mxc://test.invalid/{filename}"), {}

    async def room_send(self, room_id, message_type, content=None):
        self.events.append(content)
        return nio.RoomSendResponse(event_id=f"${len(self.events)}", room_id=room_id)


class FakeVideoMessage:
    def __init__(self, media_bytes, thumb_bytes=b"tg-thumb"):
        self.id = 7
        self.file = SimpleNamespace(mime_type="video/mp4", name="clip.mp4", size=len(media_bytes))
        self.photo = None
        self.document = None
        self._media_bytes = media_bytes
        self._thumb_bytes = thumb_bytes

    async def download_media(self, file=None, thumb=None):
        return self._thumb_bytes if thumb == -1 else self._media_bytes


def split_part(index, total, thumbnail, w=320, h=240):
    return {
        "bytes": f"part-{index}".encode(),
        "filename": f"clip_part{index}_of_{total}.mp4",
        "part_index": index,
        "total_parts": total,
        "duration_ms": 5000,
        "w": w,
        "h": h,
        "size": 500,
        "thumbnail": thumbnail,
        "thumbnail_w": w if thumbnail else None,
        "thumbnail_h": h if thumbnail else None,
    }


def prepare_split(bridge, monkeypatch, parts):
    """Wires a split run: one room, a small upload limit and pre-split parts."""
    client = RecordingMatrixClient()
    monkeypatch.setattr(bridge, "matrix_client", client)
    monkeypatch.setattr(bridge, "MAX_MEDIA_SIZE_BYTES", 1000)
    monkeypatch.setattr(bridge, "LLAMAGUARD_API_URL", "")
    monkeypatch.setattr(bridge, "split_video_lossless", lambda *args, **kwargs: parts)
    monkeypatch.setattr(bridge, "probe_image", lambda data: (640, 480, "image/jpeg"))
    monkeypatch.setattr(bridge, "compute_blurhash", lambda data: "bh-" + data.decode())

    async def enough_ramdisk(*args, **kwargs):
        return True

    monkeypatch.setattr(bridge, "reserve_shm_space", enough_ramdisk)
    return client


def test_split_parts_get_their_own_thumbnail_and_blurhash(bridge, monkeypatch):
    parts = [split_part(1, 3, b"frame-1"), split_part(2, 3, b"frame-2"), split_part(3, 3, None)]
    client = prepare_split(bridge, monkeypatch, parts)

    run(bridge.process_and_upload_media(FakeVideoMessage(b"x" * 2000), "chat", "chan"))

    assert len(client.events) == 3
    thumbnails = [event["info"]["thumbnail_url"] for event in client.events]
    assert thumbnails == [
        "mxc://test.invalid/thumbnail_part1.jpg",
        "mxc://test.invalid/thumbnail_part2.jpg",
        "mxc://test.invalid/thumbnail.jpeg",
    ]
    # The part without an extractable frame keeps the primary document's Telegram thumbnail.
    assert client.events[2]["info"]["thumbnail_info"] == {
        "mimetype": "image/jpeg",
        "size": len(b"tg-thumb"),
        "w": 640,
        "h": 480,
    }
    assert client.events[0]["info"]["thumbnail_info"] == {
        "mimetype": "image/jpeg",
        "size": len(b"frame-1"),
        "w": 320,
        "h": 240,
    }
    assert [event["info"]["xyz.amorgan.blurhash"] for event in client.events] == [
        "bh-frame-1",
        "bh-frame-2",
        "bh-tg-thumb",
    ]


def test_rejected_part_thumbnail_falls_back_to_the_shared_one(bridge, monkeypatch):
    class RejectingPartThumbnails(RecordingMatrixClient):
        async def upload(self, stream, content_type=None, filename=None, filesize=None):
            if filename and filename.startswith("thumbnail_part"):
                return object(), {}
            return await super().upload(stream, content_type, filename, filesize)

    parts = [split_part(1, 2, b"frame-1"), split_part(2, 2, b"frame-2")]
    client = prepare_split(bridge, monkeypatch, parts)
    monkeypatch.setattr(client, "upload", RejectingPartThumbnails().upload)

    # Distinct bytes: the content-hash deduplicator is module state and would otherwise
    # suppress this video as a repost of the one a previous test uploaded.
    run(bridge.process_and_upload_media(FakeVideoMessage(b"y" * 2000), "chat", "chan"))

    assert [event["info"]["thumbnail_url"] for event in client.events] == [
        "mxc://test.invalid/thumbnail.jpeg",
        "mxc://test.invalid/thumbnail.jpeg",
    ]
    assert [event["info"]["xyz.amorgan.blurhash"] for event in client.events] == ["bh-tg-thumb", "bh-tg-thumb"]


def test_upload_thumbnail_reports_uri_and_info(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "matrix_client", RecordingMatrixClient())

    result = run(bridge.upload_thumbnail(b"jpeg", "image/jpeg", 320, 240, "thumb.jpg", "chat"))

    assert result == (
        "mxc://test.invalid/thumb.jpg",
        {"mimetype": "image/jpeg", "size": 4, "w": 320, "h": 240},
    )


def test_upload_thumbnail_returns_none_when_the_homeserver_rejects_it(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "matrix_client", FakeMatrixClient(upload_ok=False))

    assert run(bridge.upload_thumbnail(b"jpeg", "image/jpeg", 1, 1, "thumb.jpg", "chat")) is None


def test_video_thumbnail_ignores_undecodable_bytes(bridge):
    assert bridge.extract_video_thumbnail(b"not a video") is None


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or importlib.util.find_spec("av") is None or importlib.util.find_spec("PIL") is None,
    reason="needs ffmpeg plus PyAV and Pillow",
)
def test_video_thumbnail_is_a_small_jpeg_of_the_sampled_frame(bridge, tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc=duration=4:size=1280x720:rate=10",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", str(clip), "-y",
        ],
        check=True,
        timeout=60,
    )
    video_bytes = clip.read_bytes()

    start = bridge.extract_video_thumbnail(video_bytes, position=0.1)
    middle = bridge.extract_video_thumbnail(video_bytes, position=0.9)

    assert start is not None and middle is not None
    jpeg_bytes, width, height, mime = middle
    assert mime == "image/jpeg"
    assert jpeg_bytes.startswith(b"\xff\xd8")
    assert width == 800 and height == 450
    assert (width, height) == (start[1], start[2])
    # The testsrc pattern changes over time, so two positions must not yield the same frame.
    assert start[0] != middle[0]


# --- Per-room toggles ----------------------------------------------------------------

ROOM_A = "!a:test.invalid"
ROOM_B = "!b:test.invalid"
ADMIN = "@admin:test.invalid"
USER = "@user:test.invalid"


def send_command(bridge, monkeypatch, body, sender=ADMIN, room_id=ROOM_A):
    """Runs a !tmmb command and returns the notice it answered with."""
    sent = {}

    async def capture(room_id_sent, plain_text, html_text):
        sent["room"] = room_id_sent
        sent["plain"] = plain_text
        sent["html"] = html_text

    monkeypatch.setattr(bridge, "send_matrix_notice", capture)
    room = SimpleNamespace(room_id=room_id)
    event = SimpleNamespace(body=body, sender=sender, server_timestamp=None)
    run(bridge.on_matrix_message(room, event))
    return sent


def two_rooms(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "MATRIX_ROOM_IDS", [ROOM_A, ROOM_B])
    monkeypatch.setattr(bridge, "ADMIN_MATRIX_USER_ID", ADMIN)


def test_room_toggle_follows_the_environment_default(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "ENABLE_IMAGES", False)
    monkeypatch.setattr(bridge, "ENABLE_VIDEOS", True)

    assert bridge.room_toggles.is_enabled(ROOM_A, "images") is False
    assert bridge.room_toggles.is_enabled(ROOM_A, "videos") is True


def test_room_toggle_override_applies_to_one_room_only(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)
    bridge.room_toggles.set(ROOM_A, "images", False)

    assert bridge.room_toggles.is_enabled(ROOM_A, "images") is False
    assert bridge.room_toggles.is_enabled(ROOM_B, "images") is True


def test_room_toggle_reset_returns_to_the_environment_default(bridge):
    bridge.room_toggles.set(ROOM_A, "videos", False)
    bridge.room_toggles.reset()

    assert bridge.room_toggles.is_enabled(ROOM_A, "videos") is True


def test_enabled_rooms_follow_the_configuration_order(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)
    bridge.room_toggles.set(ROOM_B, "images", False)

    assert bridge.room_toggles.enabled_rooms("images") == [ROOM_A]


def test_media_kind_maps_mime_types(bridge):
    assert bridge.media_kind("image/jpeg") == "images"
    assert bridge.media_kind("video/mp4") == "videos"


# --- Per-room fan-out ----------------------------------------------------------------


def test_delivery_skips_the_room_with_the_kind_disabled(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)
    bridge.room_toggles.set(ROOM_B, "images", False)
    client = FakeMatrixClient(room_results=[nio.RoomSendResponse(event_id="$1", room_id=ROOM_A)])
    monkeypatch.setattr(bridge, "matrix_client", client)

    ok = run(
        bridge.send_matrix_media_event(
            b"data", "image/jpeg", "a.jpg", "m.image", {"w": 1, "h": 1}, "chat", "chan"
        )
    )

    assert ok is True
    assert client.sent_rooms == [ROOM_A]
    assert bridge.DELIVERY_STATS == {"failed": 0, "partial": 0}


def test_delivery_without_any_enabled_room_does_not_upload_or_fail(bridge, monkeypatch):
    bridge.room_toggles.set(bridge.MATRIX_ROOM_IDS[0], "videos", False)
    client = FakeMatrixClient()
    monkeypatch.setattr(bridge, "matrix_client", client)

    ok = run(
        bridge.send_matrix_media_event(
            b"data", "video/mp4", "a.mp4", "m.video", {}, "chat", "chan"
        )
    )

    assert ok is True
    assert client.upload_count == 0
    assert client.sent_rooms == []
    assert bridge.DELIVERY_STATS == {"failed": 0, "partial": 0}


def test_delivery_still_sends_videos_when_only_images_are_disabled(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)
    for room_id in (ROOM_A, ROOM_B):
        bridge.room_toggles.set(room_id, "images", False)
    client = FakeMatrixClient(
        room_results=[
            nio.RoomSendResponse(event_id="$1", room_id=ROOM_A),
            nio.RoomSendResponse(event_id="$2", room_id=ROOM_B),
        ]
    )
    monkeypatch.setattr(bridge, "matrix_client", client)

    ok = run(
        bridge.send_matrix_media_event(
            b"data", "video/mp4", "a.mp4", "m.video", {}, "chat", "chan"
        )
    )

    assert ok is True
    assert client.sent_rooms == [ROOM_A, ROOM_B]


# --- Command scope -------------------------------------------------------------------


def test_start_without_scope_changes_only_the_sending_room(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)
    monkeypatch.setattr(bridge, "ENABLE_IMAGES", False)
    monkeypatch.setattr(bridge, "ENABLE_VIDEOS", False)

    sent = send_command(bridge, monkeypatch, "!tmmb start", room_id=ROOM_A)

    assert bridge.room_toggles.is_enabled(ROOM_A, "images") is True
    assert bridge.room_toggles.is_enabled(ROOM_A, "videos") is True
    assert bridge.room_toggles.is_enabled(ROOM_B, "images") is False
    assert "in this room" in sent["plain"]


def test_start_all_changes_every_room(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)
    monkeypatch.setattr(bridge, "ENABLE_IMAGES", False)
    monkeypatch.setattr(bridge, "ENABLE_VIDEOS", False)

    sent = send_command(bridge, monkeypatch, "!tmmb start all", room_id=ROOM_A)

    assert bridge.room_toggles.is_enabled(ROOM_B, "images") is True
    assert bridge.room_toggles.is_enabled(ROOM_B, "videos") is True
    assert "in all rooms" in sent["plain"]


def test_all_scope_confirmation_never_reveals_the_room_count(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)

    for body in ("!tmmb start all", "!tmmb stop all", "!tmmb image disable all", "!tmmb video disable all"):
        sent = send_command(bridge, monkeypatch, body, room_id=ROOM_A)
        # The notice lands in the room, so a count would be visible to every member.
        assert "2" not in sent["plain"], body
        assert "in all rooms" in sent["plain"], body


def test_stop_without_scope_is_allowed_for_non_admins(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)
    monkeypatch.setattr(bridge, "ALLOW_NON_ADMIN_STOP", True)

    send_command(bridge, monkeypatch, "!tmmb stop", sender=USER, room_id=ROOM_A)

    assert bridge.room_toggles.is_enabled(ROOM_A, "videos") is False
    assert bridge.room_toggles.is_enabled(ROOM_B, "videos") is True


def test_stop_all_is_refused_for_non_admins(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)
    monkeypatch.setattr(bridge, "ALLOW_NON_ADMIN_STOP", True)

    sent = send_command(bridge, monkeypatch, "!tmmb stop all", sender=USER, room_id=ROOM_A)

    assert bridge.room_toggles.is_enabled(ROOM_A, "videos") is True
    assert bridge.room_toggles.is_enabled(ROOM_B, "videos") is True
    assert "admin" in sent["plain"].lower()


def test_start_all_is_refused_for_non_admins(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)
    monkeypatch.setattr(bridge, "ALLOW_NON_ADMIN_STOP", True)
    monkeypatch.setattr(bridge, "ENABLE_IMAGES", False)

    send_command(bridge, monkeypatch, "!tmmb start all", sender=USER, room_id=ROOM_A)

    assert bridge.room_toggles.is_enabled(ROOM_B, "images") is False


def test_image_disable_all_is_refused_for_non_admins(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)
    monkeypatch.setattr(bridge, "ALLOW_NON_ADMIN_STOP", True)

    send_command(bridge, monkeypatch, "!tmmb image disable all", sender=USER, room_id=ROOM_A)

    assert bridge.room_toggles.is_enabled(ROOM_A, "images") is True
    assert bridge.room_toggles.is_enabled(ROOM_B, "images") is True


def test_status_only_reports_the_sending_room(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)
    monkeypatch.setattr(bridge, "ALLOW_NON_ADMIN_STOP", True)
    bridge.room_toggles.set(ROOM_A, "videos", False)

    sent = send_command(bridge, monkeypatch, "!tmmb status", sender=USER, room_id=ROOM_A)

    assert "images ENABLED, videos DISABLED" in sent["plain"]
    assert ROOM_B not in sent["plain"]


def test_status_all_hides_the_other_rooms_from_non_admins(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)
    monkeypatch.setattr(bridge, "ALLOW_NON_ADMIN_STOP", True)

    sent = send_command(bridge, monkeypatch, "!tmmb status all", sender=USER, room_id=ROOM_A)

    assert ROOM_A not in sent["plain"]
    assert ROOM_B not in sent["plain"]
    assert "admin" in sent["plain"].lower()


def test_status_all_lists_every_room_for_admins(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)
    bridge.room_toggles.set(ROOM_B, "images", False)

    sent = send_command(bridge, monkeypatch, "!tmmb status all", room_id=ROOM_A)

    assert f"{ROOM_A} : images ENABLED, videos ENABLED" in sent["plain"]
    assert f"{ROOM_B} : images DISABLED, videos ENABLED" in sent["plain"]


def test_unknown_scope_argument_changes_nothing(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)
    monkeypatch.setattr(bridge, "ENABLE_IMAGES", False)

    sent = send_command(bridge, monkeypatch, "!tmmb start everyone", room_id=ROOM_A)

    assert bridge.room_toggles.is_enabled(ROOM_A, "images") is False
    assert "Usage" in sent["plain"]


def test_help_mentions_the_all_scope(bridge, monkeypatch):
    two_rooms(bridge, monkeypatch)

    sent = send_command(bridge, monkeypatch, "!tmmb help", room_id=ROOM_A)

    assert "!tmmb start [all]" in sent["plain"]
    assert "!tmmb status [all]" in sent["plain"]


# --- Matrix sync error handling (issue #49) ------------------------------------------
#
# nio's SyncError.from_dict() stores the Matrix errcode in status_code and the readable text
# in message, so these tests build their errors through from_dict(), as nio does.


class StopRecordingClient:
    def __init__(self):
        self.stops = 0

    def stop_sync_forever(self):
        self.stops += 1


@pytest.fixture
def sync_bridge(bridge, monkeypatch):
    """The entry-point module itself (not part of the facade), with its sync flags reset."""
    module = sys.modules["bridge"]
    monkeypatch.setattr(module, "MATRIX_SYNC_AUTH_FAILED", False)
    monkeypatch.setattr(module, "MATRIX_SYNC_ERROR_STOP", False)
    return module


def sync_error(body, http_status=None):
    error = nio.SyncError.from_dict(body)
    if http_status is not None:
        error.transport_response = SimpleNamespace(status=http_status)
    return error


@pytest.mark.parametrize(
    "body, http_status",
    [
        ({"errcode": "M_UNKNOWN_TOKEN", "error": "Unrecognised access token"}, 401),
        ({"errcode": "M_UNKNOWN_TOKEN", "error": "Unrecognised access token"}, None),
        ({"errcode": "M_MISSING_TOKEN", "error": "Missing access token"}, 401),
        ({"errcode": "M_UNKNOWN_TOKEN", "error": "Session expired", "soft_logout": True}, 401),
        # A proxy's HTML 401 page: nio parses the body to {} and reports "unknown error".
        ({}, 401),
    ],
    ids=["unknown-token", "unknown-token-no-transport", "missing-token", "soft-logout", "html-401"],
)
def test_token_rejection_stops_sync_for_good(sync_bridge, monkeypatch, body, http_status):
    client = StopRecordingClient()
    monkeypatch.setattr(sync_bridge.state, "matrix_client", client)

    run(sync_bridge.on_matrix_response(sync_error(body, http_status)))
    run(sync_bridge.on_matrix_response(sync_error(body, http_status)))

    assert sync_bridge.MATRIX_SYNC_AUTH_FAILED is True
    assert client.stops == 1


def test_rate_limit_is_neither_fatal_nor_a_restart(sync_bridge, monkeypatch):
    client = StopRecordingClient()
    monkeypatch.setattr(sync_bridge.state, "matrix_client", client)
    error = sync_error({"errcode": "M_LIMIT_EXCEEDED", "error": "Too many requests", "retry_after_ms": 2000}, 429)

    run(sync_bridge.on_matrix_response(error))

    assert sync_bridge.MATRIX_SYNC_AUTH_FAILED is False
    assert sync_bridge.MATRIX_SYNC_ERROR_STOP is False
    assert client.stops == 0


@pytest.mark.parametrize(
    "body, http_status",
    [
        ({"errcode": "M_UNKNOWN", "error": "Internal server error"}, 500),
        ({}, 502),
        # 403 on /sync is not a token rejection (e.g. M_FORBIDDEN); it must not end sync for good.
        ({"errcode": "M_FORBIDDEN", "error": "Forbidden"}, 403),
    ],
    ids=["500", "html-502", "403-forbidden"],
)
def test_other_sync_errors_restart_with_backoff(sync_bridge, monkeypatch, body, http_status):
    client = StopRecordingClient()
    monkeypatch.setattr(sync_bridge.state, "matrix_client", client)

    run(sync_bridge.on_matrix_response(sync_error(body, http_status)))

    assert sync_bridge.MATRIX_SYNC_AUTH_FAILED is False
    assert sync_bridge.MATRIX_SYNC_ERROR_STOP is True
    assert client.stops == 1


def test_non_sync_responses_are_ignored(sync_bridge, monkeypatch):
    client = StopRecordingClient()
    monkeypatch.setattr(sync_bridge.state, "matrix_client", client)
    run(sync_bridge.on_matrix_response(nio.LoginError.from_dict({"errcode": "M_UNKNOWN_TOKEN", "error": "x"})))
    assert client.stops == 0


async def run_supervisor_against(sync_bridge, monkeypatch, status, body, content_type, duration):
    """Runs the real supervisor and nio sync_forever against a loopback homeserver stub.

    Returns (requests_served, supervisor_finished).
    """
    from aiohttp import web

    requests = []

    async def sync_handler(request):
        requests.append(request.path)
        return web.Response(status=status, text=body, content_type=content_type)

    app = web.Application()
    app.router.add_get("/_matrix/client/v3/sync", sync_handler)
    app.router.add_get("/_matrix/client/r0/sync", sync_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    client = nio.AsyncClient(f"http://127.0.0.1:{port}", "@bot:test.invalid")
    client.access_token = "revoked-token"
    client.add_response_callback(sync_bridge.on_matrix_response, nio.SyncError)
    monkeypatch.setattr(sync_bridge.state, "matrix_client", client)

    task = asyncio.create_task(sync_bridge.matrix_sync_loop())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=duration)
        finished = True
    except TimeoutError:
        finished = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await client.close()
        await runner.cleanup()
    return len(requests), finished


@pytest.mark.parametrize(
    "body, content_type",
    [
        (json.dumps({"errcode": "M_UNKNOWN_TOKEN", "error": "Unrecognised access token"}), "application/json"),
        (json.dumps({"errcode": "M_MISSING_TOKEN", "error": "Missing access token"}), "application/json"),
        ("<html><body>401 Authorization Required</body></html>", "text/html"),
    ],
    ids=["unknown-token", "missing-token", "html-401"],
)
def test_revoked_token_ends_the_sync_loop_after_one_request(sync_bridge, monkeypatch, body, content_type):
    served, finished = run(run_supervisor_against(sync_bridge, monkeypatch, 401, body, content_type, duration=5))

    assert finished, "the supervisor must stop on its own after a token rejection"
    assert served == 1
    assert sync_bridge.MATRIX_SYNC_AUTH_FAILED is True


def test_persistent_server_error_backs_off_instead_of_hot_looping(sync_bridge, monkeypatch):
    monkeypatch.setattr(sync_bridge, "MATRIX_SYNC_INITIAL_BACKOFF_SECONDS", 0.2)
    monkeypatch.setattr(sync_bridge, "MATRIX_SYNC_MAX_BACKOFF_SECONDS", 0.2)
    body = json.dumps({"errcode": "M_UNKNOWN", "error": "Internal server error"})

    served, finished = run(run_supervisor_against(sync_bridge, monkeypatch, 500, body, "application/json", duration=1.0))

    # One request per backoff period (~5 in 1s). Without the stop, nio re-polls immediately
    # and a loopback server answers hundreds of requests in the same second.
    assert not finished
    assert 2 <= served <= 8
    assert sync_bridge.MATRIX_SYNC_AUTH_FAILED is False


# --- Llama Guard category configuration (issue #60) ----------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, set()),
        ("", set()),
        (" , ,", set()),
        ("S4", {"S4"}),
        ("S1,S4", {"S1", "S4"}),
        (" s4 , S12 ,", {"S4", "S12"}),
        ("S1,S14", {"S1", "S14"}),
        ("S4,S4", {"S4"}),
    ],
)
def test_category_list_accepts_valid_entries(bridge, raw, expected):
    assert bridge.parse_category_list("LLAMAGUARD_CHECKS", raw) == expected


@pytest.mark.parametrize(
    "raw, bad",
    [
        ("S04", ["S04"]),
        ("4", ["4"]),
        ("S15", ["S15"]),
        ("S0", ["S0"]),
        ("S4 S12", ["S4 S12"]),
        ("S-12", ["S-12"]),
        ("S1,S04,S4,X", ["S04", "X"]),
    ],
)
def test_category_list_rejects_unknown_entries(bridge, raw, bad):
    with pytest.raises(ValueError) as excinfo:
        bridge.parse_category_list("LLAMAGUARD_REQUIRE_CHECKS", raw)
    message = str(excinfo.value)
    assert "LLAMAGUARD_REQUIRE_CHECKS" in message
    assert repr(bad) in message
    assert "S1 to S14" in message


def test_configured_and_parsed_categories_share_one_definition(bridge):
    assert bridge.safety.VALID_CATEGORIES is bridge.config.LLAMAGUARD_CATEGORIES


def test_invalid_category_config_fails_at_startup():
    """A typo must stop the bridge at startup, not silently disable the check."""
    import os

    from conftest import APP_DIR, REQUIRED_ENV

    env = {**os.environ, **REQUIRED_ENV, "LLAMAGUARD_CHECKS": "S1,S04"}
    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        check=False,
        cwd=APP_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0
    assert "LLAMAGUARD_CHECKS contains unknown Llama Guard categories ['S04']" in result.stderr


# --- Forum topic resolution (issue #50) ----------------------------------------------
#
# Real Telethon reply headers and channel entities drive the real master_handler; only the
# media pipeline and the topic-name lookup are replaced.

FORUM_CHAT_ID = -1001234567890


def forum_channel(forum=True, username="forumchan"):
    from telethon.tl.types import Channel, ChatPhotoEmpty

    return Channel(
        id=1234567890, title="Forum Chan", photo=ChatPhotoEmpty(), date=None,
        megagroup=True, forum=forum, username=username,
    )


def topic_post(topic_id):
    from telethon.tl.types import MessageReplyHeader

    return MessageReplyHeader(forum_topic=True, reply_to_msg_id=topic_id)


def topic_reply(topic_id, replied_to):
    from telethon.tl.types import MessageReplyHeader

    return MessageReplyHeader(forum_topic=True, reply_to_msg_id=replied_to, reply_to_top_id=topic_id)


def general_reply(replied_to):
    from telethon.tl.types import MessageReplyHeader

    return MessageReplyHeader(reply_to_msg_id=replied_to)


@pytest.fixture
def forum_handler(bridge, monkeypatch):
    """Runs master_handler for one message; returns the display names it delivered."""
    handler_module = sys.modules["handler"]
    config_module = sys.modules["config"]
    delivered = []
    topic_lookups = []

    async def fake_process(message, source_chat, channel_name, download_document=None):
        delivered.append(channel_name)

    async def fake_topic_name(client, chat_entity, topic_id):
        topic_lookups.append(topic_id)
        return {1: "General", 42: "Memes", 7: "Offtopic"}.get(topic_id, f"Topic {topic_id}")

    monkeypatch.setattr(handler_module, "process_and_upload_media", fake_process)
    monkeypatch.setattr(handler_module, "get_topic_name", fake_topic_name)

    def deliver(reply_to, *, filters, unfiltered=(), chat=None, cached=True):
        monkeypatch.setattr(config_module, "TG_TOPIC_FILTERS", filters)
        monkeypatch.setattr(config_module, "TG_UNFILTERED_CHANNELS", set(unfiltered))
        chat = forum_channel() if chat is None else chat

        async def get_chat():
            return chat

        message = SimpleNamespace(
            id=500, media=object(), reply_to=reply_to, grouped_id=None,
            file=SimpleNamespace(size=1000, mime_type="image/jpeg"), video=None,
        )
        event = SimpleNamespace(
            message=message, chat=chat if cached else None, chat_id=FORUM_CHAT_ID, get_chat=get_chat,
        )
        delivered.clear()
        topic_lookups.clear()
        run(bridge.master_handler(event))
        return list(delivered)

    deliver.topic_lookups = topic_lookups
    return deliver


BY_ID = {FORUM_CHAT_ID: {42}}
BY_USERNAME = {"forumchan": {42}}


@pytest.mark.parametrize("filters", [BY_ID, BY_USERNAME], ids=["numeric", "username"])
def test_direct_post_and_nested_reply_in_allowed_topic_are_delivered(forum_handler, filters):
    assert forum_handler(topic_post(42), filters=filters) == ["Forum Chan - Memes"]
    # Nested reply: reply_to_msg_id is the replied-to message, reply_to_top_id the topic.
    assert forum_handler(topic_reply(42, replied_to=201), filters=filters) == ["Forum Chan - Memes"]


@pytest.mark.parametrize("filters", [BY_ID, BY_USERNAME], ids=["numeric", "username"])
def test_messages_from_other_topics_stay_rejected(forum_handler, filters):
    assert forum_handler(topic_post(7), filters=filters) == []
    assert forum_handler(topic_reply(7, replied_to=42), filters=filters) == []


@pytest.mark.parametrize("reply_to", [None, general_reply(300)], ids=["plain", "reply"])
@pytest.mark.parametrize(
    "filters",
    [{FORUM_CHAT_ID: {1}}, {"forumchan": {1}}],
    ids=["numeric", "username"],
)
def test_general_topic_matches_topic_one(forum_handler, filters, reply_to):
    assert forum_handler(reply_to, filters=filters) == ["Forum Chan - General"]


def test_general_topic_is_rejected_by_another_topic_filter(forum_handler):
    assert forum_handler(None, filters=BY_ID) == []
    assert forum_handler(general_reply(300), filters=BY_ID) == []


def test_general_topic_is_resolved_when_the_chat_is_not_cached(forum_handler):
    assert forum_handler(None, filters={"forumchan": {1}}, cached=False) == ["Forum Chan - General"]


def test_non_forum_channel_gets_no_topic(forum_handler):
    plain = forum_channel(forum=False)
    assert forum_handler(None, filters={}, unfiltered={FORUM_CHAT_ID}, chat=plain) == ["Forum Chan"]
    assert forum_handler.topic_lookups == []
    # A topic filter on a chat that is not a forum still matches nothing, as before.
    assert forum_handler(general_reply(300), filters={FORUM_CHAT_ID: {1}}, chat=plain) == []


def test_unfiltered_forum_forwards_every_topic(forum_handler):
    unfiltered = {FORUM_CHAT_ID}
    assert forum_handler(topic_reply(7, replied_to=9), filters={}, unfiltered=unfiltered) == ["Forum Chan - Offtopic"]
    assert forum_handler(None, filters={}, unfiltered=unfiltered) == ["Forum Chan - General"]


def test_resolve_topic_id_prefers_the_thread_top(bridge):
    from telethon.tl.types import PeerChannel

    # A reply to a message in another chat: reply_to_msg_id belongs to that chat.
    foreign = topic_reply(42, replied_to=99999)
    foreign.reply_to_peer_id = PeerChannel(555)
    message = SimpleNamespace(reply_to=foreign)
    assert bridge.resolve_topic_id(message, forum_channel()) == 42
    assert bridge.resolve_topic_id(SimpleNamespace(reply_to=None), None) is None


# --- Split parts that still exceed the limit (issue #51) -----------------------------
#
# ffmpeg is replaced by a stand-in that writes part files of chosen sizes into the scratch
# directory, so the real split_video_lossless() runs without ffmpeg or /dev/shm.


@pytest.fixture
def fake_segmenter(bridge, monkeypatch, tmp_path):
    """Returns a recorder; set .sizes_for(segment_time) to choose the produced part sizes."""
    media_module = sys.modules["media"]
    recorder = SimpleNamespace(segment_times=[], thumbnails=0, sizes_for=None)

    def fake_run(cmd, **kwargs):
        seconds = float(cmd[cmd.index("-segment_time") + 1])
        recorder.segment_times.append(seconds)
        pattern = cmd[-1]
        for i, size in enumerate(recorder.sizes_for(seconds)):
            with open(pattern.replace("%03d", f"{i:03d}"), "wb") as f:
                f.write(b"p" * size)
        return SimpleNamespace(returncode=0, stderr=b"")

    def fake_thumbnail(data):
        recorder.thumbnails += 1

    monkeypatch.setattr(media_module.subprocess, "run", fake_run)
    monkeypatch.setattr(media_module, "SHM_PATH", str(tmp_path))
    monkeypatch.setattr(sys.modules["config"], "FFMPEG_PATH", "/opt/fake/ffmpeg")
    monkeypatch.setattr(media_module, "probe_video_duration", lambda *a, **kw: 2.0)
    monkeypatch.setattr(media_module, "probe_video_dimensions", lambda *a, **kw: (320, 240))
    monkeypatch.setattr(media_module, "extract_video_thumbnail", fake_thumbnail)
    return recorder


def split(bridge, size=5000, limit=1000, duration=20.0):
    return bridge.split_video_lossless(b"v" * size, "clip.mp4", limit, "chat", known_duration=duration)


def test_split_within_the_limit_is_unchanged(bridge, fake_segmenter):
    fake_segmenter.sizes_for = lambda seconds: [800] * 7

    parts = split(bridge)

    assert [p["size"] for p in parts] == [800] * 7
    assert [p["filename"] for p in parts][:2] == ["clip_part1_of_7.mp4", "clip_part2_of_7.mp4"]
    assert len(fake_segmenter.segment_times) == 1


def test_oversized_part_is_retried_with_shorter_segments(bridge, fake_segmenter):
    # First pass: uneven bitrate leaves one 1500-byte part. Shorter segments fit.
    fake_segmenter.sizes_for = lambda seconds: [800, 1500, 700, 900, 600, 500] if seconds > 2 else [600] * 10

    parts = split(bridge)

    first, second = fake_segmenter.segment_times
    assert second < first
    # Scaled by the overshoot: limit * 0.85 / largest part.
    assert second == pytest.approx(round(first * 1000 * 0.85 / 1500, 2))
    assert [p["size"] for p in parts] == [600] * 10
    assert parts[0]["total_parts"] == 10


def test_split_that_cannot_fit_returns_nothing(bridge, fake_segmenter, caplog):
    # Keyframes too far apart: shorter segments produce the same oversized parts.
    fake_segmenter.sizes_for = lambda seconds: [3000, 2000]

    with caplog.at_level(logging.WARNING):
        parts = split(bridge)

    assert parts == []
    assert len(fake_segmenter.segment_times) == 2
    # Rejected before any part is read, probed or thumbnailed.
    assert fake_segmenter.thumbnails == 0
    assert any("cannot be split losslessly below" in r.getMessage() for r in caplog.records)


def test_no_size_retry_when_segments_are_already_minimal(bridge, fake_segmenter):
    fake_segmenter.sizes_for = lambda seconds: [3000, 2000]

    # 2 s of video in parts of max(1.0, ...) seconds: nothing shorter to try.
    assert split(bridge, duration=2.0) == []
    assert fake_segmenter.segment_times == [1.0]


def prepare_real_split(bridge, monkeypatch, fallback, compressed_size=900):
    client = RecordingMatrixClient()
    monkeypatch.setattr(bridge, "matrix_client", client)
    monkeypatch.setattr(bridge, "MAX_MEDIA_SIZE_BYTES", 1000)
    monkeypatch.setattr(bridge, "LLAMAGUARD_API_URL", "")
    monkeypatch.setattr(bridge, "OVERSIZED_VIDEO_ACTION", "split")
    monkeypatch.setattr(bridge, "OVERSIZED_VIDEO_FALLBACK_COMPRESS", fallback)
    monkeypatch.setattr(bridge, "probe_image", lambda data: (640, 480, "image/jpeg"))
    monkeypatch.setattr(bridge, "compute_blurhash", lambda data: None)

    async def enough_ramdisk(*args, **kwargs):
        return True

    compress_calls = []

    def fake_compress(video_bytes, filename, max_bytes, source_chat, **kwargs):
        compress_calls.append((len(video_bytes), client.events[:]))
        return {"bytes": b"c" * compressed_size, "filename": "clip.mp4", "duration_ms": 20000,
                "w": 640, "h": 360, "size": compressed_size}

    monkeypatch.setattr(bridge, "reserve_shm_space", enough_ramdisk)
    monkeypatch.setattr(bridge, "compress_video", fake_compress)
    return client, compress_calls


def test_unsplittable_video_falls_back_to_compression_without_sending_parts(bridge, monkeypatch, fake_segmenter):
    fake_segmenter.sizes_for = lambda seconds: [3000, 2000]
    client, compress_calls = prepare_real_split(bridge, monkeypatch, fallback=True)

    run(bridge.process_and_upload_media(FakeVideoMessage(b"s" * 5000), "chat", "chan"))

    assert len(compress_calls) == 1
    _, events_before_compression = compress_calls[0]
    assert events_before_compression == []
    assert len(client.events) == 1
    assert "Part" not in client.events[0]["body"]
    assert client.events[0]["info"]["size"] == 900


def test_unsplittable_video_is_skipped_without_fallback(bridge, monkeypatch, fake_segmenter):
    fake_segmenter.sizes_for = lambda seconds: [3000, 2000]
    client, compress_calls = prepare_real_split(bridge, monkeypatch, fallback=False)
    video = b"n" * 5000

    run(bridge.process_and_upload_media(FakeVideoMessage(video), "chat", "chan"))

    assert compress_calls == []
    assert client.events == []
    # Only the Telegram thumbnail was uploaded; no video part.
    assert all(name.startswith("thumbnail") for name in client.uploaded_filenames)
    content_key = f"sha256_{bridge.compute_sha256(video)}"
    assert bridge.media_deduplicator.is_duplicate(content_key) is False


def test_fallback_result_still_too_large_is_skipped(bridge, monkeypatch, fake_segmenter):
    fake_segmenter.sizes_for = lambda seconds: [3000, 2000]
    client, compress_calls = prepare_real_split(bridge, monkeypatch, fallback=True, compressed_size=1500)

    run(bridge.process_and_upload_media(FakeVideoMessage(b"t" * 5000), "chat", "chan"))

    assert len(compress_calls) == 1
    assert client.events == []


@pytest.mark.skipif(
    shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None,
    reason="needs a real ffmpeg/ffprobe installation",
)
def test_real_split_with_sparse_keyframes_is_rejected(bridge, monkeypatch, tmp_path):
    """The issue's reproduction: 4 s of H.264, keyframes 2 s apart, limit a fifth of the input."""
    clip = tmp_path / "sparse.mp4"
    subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc=duration=4:size=320x240:rate=10",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-g", "20",
            "-x264-params", "scenecut=0", str(clip), "-y",
        ],
        check=True,
        timeout=60,
    )
    data = clip.read_bytes()
    scratch = tmp_path / "shm"
    scratch.mkdir()
    monkeypatch.setattr(sys.modules["media"], "SHM_PATH", str(scratch))
    monkeypatch.setattr(sys.modules["config"], "FFMPEG_PATH", shutil.which("ffmpeg"))

    assert bridge.split_video_lossless(data, "sparse.mp4", len(data) // 5, "chat", known_duration=4.0) == []
    # A limit the 2 s parts fit under still splits normally.
    parts = bridge.split_video_lossless(data, "sparse.mp4", int(len(data) * 0.8), "chat", known_duration=4.0)
    assert len(parts) >= 2
    assert all(p["size"] <= int(len(data) * 0.8) for p in parts)
    assert list(scratch.iterdir()) == []


# --- Deduplication aliases (issue #52) -----------------------------------------------


def test_unmarking_a_key_releases_its_aliases(bridge):
    dedup = bridge.MediaDeduplicator()
    dedup.mark_seen("sha256_x")
    dedup.mark_seen("tg_photo_32")
    dedup.add_alias("sha256_x", "tg_photo_32")

    dedup.unmark("sha256_x")

    assert dedup.is_duplicate("sha256_x") is False
    assert dedup.is_duplicate("tg_photo_32") is False


def test_aliases_stay_marked_while_their_key_stays_marked(bridge):
    dedup = bridge.MediaDeduplicator()
    dedup.mark_seen("sha256_x")
    dedup.mark_seen("tg_photo_32")
    dedup.add_alias("sha256_x", "tg_photo_32")

    dedup.unmark("tg_photo_31")  # an unrelated release

    assert dedup.is_duplicate("tg_photo_32") is True


def test_alias_links_are_dropped_with_their_key(bridge, monkeypatch):
    dedup = bridge.MediaDeduplicator(max_size=2)
    dedup.mark_seen("sha256_x")
    dedup.mark_seen("tg_photo_32")
    dedup.add_alias("sha256_x", "tg_photo_32")
    dedup.mark_seen("tg_photo_33")  # evicts sha256_x (capacity 2)

    assert "sha256_x" not in dedup._aliases
    # Re-marking the content later must not release the old alias on a failure.
    dedup.mark_seen("tg_photo_32")  # refresh, so re-marking sha256_x evicts tg_photo_33
    dedup.mark_seen("sha256_x")
    dedup.unmark("sha256_x")
    assert dedup.is_duplicate("tg_photo_32") is True


def test_expired_key_drops_its_alias_link(bridge, monkeypatch):
    dedup = bridge.MediaDeduplicator(ttl_minutes=1)
    now = [1000.0]
    monkeypatch.setattr(bridge.state.time, "time", lambda: now[0])
    dedup.mark_seen("sha256_x")
    dedup.add_alias("sha256_x", "tg_photo_32")

    now[0] += 61
    assert dedup.is_duplicate("sha256_x") is False
    assert "sha256_x" not in dedup._aliases


def test_alias_is_ignored_for_unknown_keys_and_when_disabled(bridge):
    dedup = bridge.MediaDeduplicator()
    dedup.add_alias("sha256_missing", "tg_photo_32")
    dedup.add_alias("sha256_x", None)
    assert dedup._aliases == {}

    disabled = bridge.MediaDeduplicator(enabled=False)
    disabled.mark_seen("sha256_x")
    disabled.add_alias("sha256_x", "tg_photo_32")
    assert disabled._aliases == {}


class GatedMatrixClient:
    """Holds the upload of one filename until the test decides how it ends."""

    def __init__(self, gated_filename):
        self.gated_filename = gated_filename
        self.uploading = asyncio.Event()
        self.release = asyncio.Event()
        self.gated_succeeds = True
        self.uploaded = []
        self.events = []

    async def upload(self, stream, content_type=None, filename=None, filesize=None):
        if filename == self.gated_filename:
            self.uploading.set()
            await self.release.wait()
            if not self.gated_succeeds:
                return object(), {}
        self.uploaded.append(filename)
        return nio.UploadResponse(content_uri=f"mxc://test.invalid/{filename}"), {}

    async def room_send(self, room_id, message_type, content=None):
        self.events.append(content)
        return nio.RoomSendResponse(event_id=f"${len(self.events)}", room_id=room_id)


def photo_message(photo_id, filename, data, downloads):
    async def download_media(file=None, thumb=None):
        downloads.append(photo_id)
        return data

    return SimpleNamespace(
        id=photo_id,
        photo=SimpleNamespace(id=photo_id, sizes=[]),
        video=None,
        document=None,
        file=SimpleNamespace(mime_type="image/jpeg", name=filename, size=len(data)),
        download_media=download_media,
    )


async def run_in_flight_duplicate(bridge, client, a_succeeds, data):
    """A uploads (held); B with another media ID but the same bytes arrives meanwhile;
    then A finishes; then B's message is reposted. Returns B's downloads and events sent."""
    downloads = []
    task_a = asyncio.create_task(
        bridge.process_and_upload_media(photo_message(31, "a.jpg", data, downloads), "chat", "chan")
    )
    await client.uploading.wait()

    await bridge.process_and_upload_media(photo_message(32, "b.jpg", data, downloads), "chat", "chan")
    events_while_in_flight = len(client.events)

    client.gated_succeeds = a_succeeds
    client.release.set()
    await task_a

    await bridge.process_and_upload_media(photo_message(32, "b.jpg", data, downloads), "chat", "chan")
    return downloads, events_while_in_flight


@pytest.fixture
def fresh_dedup(bridge, monkeypatch):
    dedup = bridge.MediaDeduplicator()
    monkeypatch.setattr(sys.modules["state"], "media_deduplicator", dedup)
    monkeypatch.setattr(bridge, "LLAMAGUARD_API_URL", "")
    monkeypatch.setattr(bridge, "ENABLE_IMAGES", True)
    monkeypatch.setattr(bridge, "generate_image_thumbnail", lambda data: None)
    monkeypatch.setattr(bridge, "compute_blurhash", lambda data: None)
    return dedup


def test_alias_is_retried_after_the_original_fails(bridge, monkeypatch, fresh_dedup):
    client = GatedMatrixClient("a.jpg")
    monkeypatch.setattr(bridge, "matrix_client", client)

    downloads, events_while_in_flight = run(run_in_flight_duplicate(bridge, client, False, b"same-bytes-fail"))

    assert events_while_in_flight == 0  # B was not sent while A was in flight
    assert downloads == [31, 32, 32]  # the repost of B was downloaded again ...
    assert client.uploaded == ["b.jpg"]  # ... and delivered once
    assert len(client.events) == 1
    assert fresh_dedup.is_duplicate("tg_photo_31") is False


def test_alias_stays_suppressed_after_the_original_succeeds(bridge, monkeypatch, fresh_dedup):
    client = GatedMatrixClient("a.jpg")
    monkeypatch.setattr(bridge, "matrix_client", client)

    downloads, events_while_in_flight = run(run_in_flight_duplicate(bridge, client, True, b"same-bytes-ok"))

    assert events_while_in_flight == 0
    assert downloads == [31, 32]  # the repost of B was suppressed before downloading
    assert client.uploaded == ["a.jpg"]
    assert len(client.events) == 1
    assert fresh_dedup.is_duplicate("tg_photo_32") is True
