"""Matrix side: notices to rooms and media events with fan-out accounting."""

import io
import logging
from dataclasses import dataclass

import state
from nio import RoomSendError, RoomSendResponse, UploadResponse

logger = logging.getLogger("tgmabr")


@dataclass
class PreparedMediaEvent:
    """An uploaded media file and the room event that will announce it."""

    filename: str
    content: dict


async def send_matrix_notice(room_id: str, plain_text: str, html_text: str):
    """Send a status/command response notice back to the Matrix room"""
    try:
        content = {
            "msgtype": "m.notice",
            "body": plain_text,
            "format": "org.matrix.custom.html",
            "formatted_body": html_text
        }
        # Every room send takes the lock, so a notice cannot land between split parts.
        async with state.ROOM_SEND_LOCK:
            await state.matrix_client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content
            )
    except Exception as e:  # noqa: BLE001 - a failed notice must not break command handling
        logger.error(f"Failed to send Matrix command response notice: {e}")


async def upload_matrix_media(
    media_bytes: bytes,
    mime_type: str,
    filename: str,
    msg_type: str,
    info_dict: dict,
    source_chat: str,
    channel_name: str,
    caption_prefix: str | None = None,
    blurhash_str: str | None = None
) -> PreparedMediaEvent | None:
    """Uploads media bytes and builds the room event for them without posting it.

    Returns None (after logging) if the homeserver rejected the upload.
    """
    logger.info(f"[{source_chat}] Uploading to Matrix homeserver: {filename} ({len(media_bytes)} bytes)...")
    try:
        upload_resp, _ = await state.matrix_client.upload(
            io.BytesIO(media_bytes),
            content_type=mime_type,
            filename=filename,
            filesize=len(media_bytes)
        )
    except Exception as e:  # noqa: BLE001 - the upload path reports failure to its caller instead of raising
        logger.error(f"[{source_chat}] General error during Matrix upload of {filename}: {e}")
        return None
    if not isinstance(upload_resp, UploadResponse):
        logger.error(f"[{source_chat}] Matrix server rejected upload for {filename}! Response: {upload_resp}")
        return None

    content_uri = upload_resp.content_uri
    logger.info(f"[{source_chat}] Upload successful! MXC-URI: {content_uri}.")

    size_bytes = len(media_bytes)
    if size_bytes < 1024:
        size_str = f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        size_str = f"{size_bytes / 1024:.2f} KB"
    else:
        size_str = f"{size_bytes / (1024 * 1024):.2f} MB"

    if info_dict and "w" in info_dict and "h" in info_dict:
        meta_line = f"{info_dict['w']}x{info_dict['h']} {size_str}"
    else:
        meta_line = f"{size_str}"

    if caption_prefix:
        meta_line = f"{caption_prefix} • {meta_line}"

    matrix_content = {
        "msgtype": msg_type,
        "body": meta_line,
        "url": content_uri,
        "filename": filename,
        "format": "org.matrix.custom.html",
        "formatted_body": f'<font color="#888888"><small>{meta_line}</small></font>',
        "source": channel_name
    }
    if blurhash_str:
        info_dict["xyz.amorgan.blurhash"] = blurhash_str
    if info_dict:
        matrix_content["info"] = info_dict
    return PreparedMediaEvent(filename=filename, content=matrix_content)


async def _post_media_event(prepared: PreparedMediaEvent, target_rooms: list[str], source_chat: str) -> bool:
    """Posts one prepared event to every target room. The caller holds ROOM_SEND_LOCK."""
    filename = prepared.filename
    delivered_rooms = []
    failed_rooms = []
    for target_room_id in target_rooms:
        try:
            send_response = await state.matrix_client.room_send(
                room_id=target_room_id,
                message_type="m.room.message",
                content=prepared.content
            )
            if isinstance(send_response, RoomSendResponse):
                delivered_rooms.append(target_room_id)
                logger.info(f"[{source_chat}] Event successfully posted in Matrix room '{target_room_id}' (Event ID: {send_response.event_id})")
            elif isinstance(send_response, RoomSendError):
                failed_rooms.append(target_room_id)
                logger.error(f"[{source_chat}] Failed to post event to Matrix room '{target_room_id}': {send_response.message} (status code: {send_response.status_code})")
            else:
                failed_rooms.append(target_room_id)
                logger.error(f"[{source_chat}] Unknown response type when posting event to Matrix room '{target_room_id}': {send_response}")
        except Exception as room_err:  # noqa: BLE001 - one broken room must not stop the remaining rooms
            failed_rooms.append(target_room_id)
            logger.error(f"[{source_chat}] Error sending event to Matrix room '{target_room_id}': {room_err}")

    # Report the real outcome: the caller uses this to decide whether a repost may be retried.
    if not delivered_rooms:
        state.DELIVERY_STATS["failed"] += 1
        logger.error(
            f"[{source_chat}] {filename} reached none of the {len(target_rooms)} addressed room(s) "
            f"({', '.join(failed_rooms) if failed_rooms else 'no rooms configured'}). "
            f"Reporting failure so the caller can release the deduplication marks."
        )
        return False

    if failed_rooms:
        state.DELIVERY_STATS["partial"] += 1
        logger.warning(
            f"[{source_chat}] {filename} reached {len(delivered_rooms)}/{len(target_rooms)} rooms; "
            f"failed: {', '.join(failed_rooms)}."
        )
    return True


async def post_media_sequence(
    prepared_events: list[PreparedMediaEvent], target_rooms: list[str], source_chat: str
) -> list[bool]:
    """Posts already uploaded events back to back, with no other bridge event in between.

    Holds ROOM_SEND_LOCK for the whole sequence; every other room send takes the same lock,
    so the events stay contiguous in each room. Only room sends happen under the lock,
    never uploads, so other media waits for a few requests at most.
    """
    async with state.ROOM_SEND_LOCK:
        return [await _post_media_event(event, target_rooms, source_chat) for event in prepared_events]


async def send_matrix_media_event(
    media_bytes: bytes,
    mime_type: str,
    filename: str,
    msg_type: str,
    info_dict: dict,
    source_chat: str,
    channel_name: str,
    caption_prefix: str | None = None,
    blurhash_str: str | None = None
) -> bool:
    """Uploads in-memory media bytes to Matrix homeserver and dispatches room messages across MATRIX_ROOM_IDS.

    Only rooms that currently accept this media kind receive the event; rooms where it is
    switched off with ``!tmmb`` are skipped silently and do not count as a delivery failure.
    """
    kind = state.media_kind(mime_type)
    target_rooms = state.room_toggles.enabled_rooms(kind)
    if not target_rooms:
        # Every room has this kind switched off (the handler normally drops the media before
        # downloading, so this only happens when a toggle flips mid-flight). Nothing was
        # attempted, so the caller must not release the deduplication marks for it.
        logger.info(f"[{source_chat}] {filename} not uploaded: no room has {kind} enabled right now.")
        return True

    prepared = await upload_matrix_media(
        media_bytes, mime_type, filename, msg_type, info_dict, source_chat, channel_name,
        caption_prefix=caption_prefix, blurhash_str=blurhash_str
    )
    if prepared is None:
        return False
    async with state.ROOM_SEND_LOCK:
        return await _post_media_event(prepared, target_rooms, source_chat)
