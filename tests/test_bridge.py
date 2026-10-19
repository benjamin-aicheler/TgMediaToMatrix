"""Tests for the logic touched by the review fixes (PRs #4-#12).

Everything here runs without network access, ffmpeg, PyAV or Pillow: those paths are
covered by hand on the real container instead.
"""

import asyncio
from types import SimpleNamespace

import nio


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
