"""Tests for the logic touched by the review fixes (PRs #4-#12).

Everything here runs without network access, ffmpeg, PyAV or Pillow: those paths are
covered by hand on the real container instead.
"""

import asyncio
import logging
import shutil
import subprocess
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


# --- Delivery semantics --------------------------------------------------------------


class FakeMatrixClient:
    def __init__(self, upload_ok=True, room_results=None):
        self.upload_ok = upload_ok
        self.room_results = list(room_results or [])

    async def upload(self, *args, **kwargs):
        if not self.upload_ok:
            return object(), {}
        return nio.UploadResponse(content_uri="mxc://test.invalid/upload1"), {}

    async def room_send(self, room_id, message_type, content=None):
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
