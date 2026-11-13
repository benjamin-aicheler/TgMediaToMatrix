"""Telegram event handler: channel/topic filtering and media processing."""

import asyncio
import io
import logging
import time

import config
import matrix_io
import media
import safety
import state
from nio import UploadResponse
from telethon import events

logger = logging.getLogger("tgmabr")


async def upload_thumbnail(
    thumb_bytes: bytes,
    mime_type: str | None,
    width: int | None,
    height: int | None,
    filename: str,
    source_chat: str
) -> tuple[str, dict] | None:
    """Uploads thumbnail bytes and returns (mxc_uri, info_dict), or None if it was rejected.

    Thumbnails are optional in every call site: a failure here degrades the preview, never
    the media event itself.
    """
    if not thumb_bytes or not mime_type:
        return None

    try:
        resp, _ = await state.matrix_client.upload(
            io.BytesIO(thumb_bytes),
            content_type=mime_type,
            filename=filename,
            filesize=len(thumb_bytes)
        )
    except Exception as e:  # noqa: BLE001 - thumbnails are optional, the media is sent either way
        logger.debug(f"[{source_chat}] Thumbnail upload for {filename} failed: {e}")
        return None

    if not isinstance(resp, UploadResponse):
        logger.debug(f"[{source_chat}] Matrix rejected the thumbnail upload for {filename}: {resp}")
        return None

    return resp.content_uri, {
        "mimetype": mime_type,
        "size": len(thumb_bytes),
        "w": width,
        "h": height
    }


def is_channel_and_topic_allowed(chat_id, chat_entity, topic_id=None):
    """Check if a given channel ID and topic ID match the configured filtering rules."""
    chat_username = getattr(chat_entity, 'username', None) if chat_entity else None
    chat_username_lower = chat_username.lower() if chat_username else None

    # If the channel itself was configured without any topic filter, allow all topics.
    if chat_id in config.TG_UNFILTERED_CHANNELS or (chat_username_lower and chat_username_lower in config.TG_UNFILTERED_CHANNELS):
        return True

    allowed_topics = None
    if chat_id in config.TG_TOPIC_FILTERS:
        allowed_topics = config.TG_TOPIC_FILTERS[chat_id]
    elif chat_username_lower and chat_username_lower in config.TG_TOPIC_FILTERS:
        allowed_topics = config.TG_TOPIC_FILTERS[chat_username_lower]

    if allowed_topics is not None:
        return topic_id in allowed_topics

    return True

# Telegram's General topic always has ID 1; its messages carry no forum reply header.
GENERAL_TOPIC_ID = 1


def resolve_topic_id(message, chat_entity) -> int | None:
    """Returns the forum topic a message belongs to, or None outside forums.

    In a messageReplyHeader with forum_topic set, reply_to_top_id is the topic when the
    message replies to another message inside the topic; reply_to_msg_id is then the
    replied-to message (possibly in another chat). A post directly in the topic has no
    top_id and its reply_to_msg_id is the topic itself. Messages in the General topic carry
    no forum_topic flag at all, so a message in a forum without one belongs to topic 1.
    See https://core.telegram.org/api/forum.
    """
    reply_to = getattr(message, 'reply_to', None)
    if reply_to is not None and getattr(reply_to, 'forum_topic', False):
        top_id = getattr(reply_to, 'reply_to_top_id', None)
        return top_id or getattr(reply_to, 'reply_to_msg_id', None)
    if getattr(chat_entity, 'forum', False):
        return GENERAL_TOPIC_ID
    return None


async def get_topic_name(client, chat_entity, topic_id):
    if not topic_id:
        return None
    cache_key = (chat_entity.id, topic_id)
    if cache_key in state.TOPIC_NAMES_CACHE:
        state.TOPIC_NAMES_CACHE.move_to_end(cache_key)
        return state.TOPIC_NAMES_CACHE[cache_key]

    try:
        from telethon.tl.functions.messages import GetForumTopicsByIDRequest
        result = await client(GetForumTopicsByIDRequest(
            peer=chat_entity,
            topics=[topic_id]
        ))
        if result and result.topics:
            name = result.topics[0].title
            state.TOPIC_NAMES_CACHE[cache_key] = name
            state.TOPIC_NAMES_CACHE.move_to_end(cache_key)
            while len(state.TOPIC_NAMES_CACHE) > state.TOPIC_NAMES_CACHE_SIZE:
                state.TOPIC_NAMES_CACHE.popitem(last=False)
            return name
    except Exception as e:  # noqa: BLE001 - Telethon RPC errors share no narrow base; a missing name is not fatal
        logger.debug(f"Could not fetch topic name for topic {topic_id} in {chat_entity.id}: {e}")
        
    return f"Topic {topic_id}"


def describe_rendition_choice(primary_bytes: int, rendition, mode: str | None) -> str:
    """One log line explaining why a rendition is used instead of the primary document."""
    primary_mb = round(primary_bytes / (1024 * 1024), 2)
    rendition_mb = round((getattr(rendition, 'size', 0) or 0) / (1024 * 1024), 2)
    if mode == media.DIRECT_UPLOAD:
        consequence = "fits the upload limit, so it is sent without processing"
    else:
        consequence = f"still needs processing ({config.OVERSIZED_VIDEO_ACTION})"
    return (
        f"Primary file is {primary_mb} MB, above the {config.MAX_MEDIA_SIZE_MB} MB upload limit. "
        f"Using Telegram's {rendition_mb} MB rendition instead: {consequence}."
    )


async def process_and_upload_media(message, source_chat, channel_name, download_document=None):
    """Process a single Telegram message and stream the media to Matrix.

    ``download_document`` optionally names an alternative rendition of the video to download
    instead of the primary document (see ``media.select_alternate_rendition``).
    """
    mime_type = message.file.mime_type if message.file else None
    filename = message.file.name if message.file else None
    
    if message.photo and not mime_type:
        mime_type = "image/jpeg"
        filename = f"telegram_photo_{message.id}.jpg"
    elif not mime_type:
        return

    is_image = mime_type.startswith("image/")
    is_video = mime_type.startswith("video/")

    if not (is_image or is_video):
        return

    # Enablement is per destination room, so the download is only skipped when no room at
    # all accepts this kind of media; the fan-out in matrix_io then filters the rest.
    kind = state.media_kind(mime_type)
    if not state.room_toggles.any_enabled(kind):
        logger.info(f"[{source_chat}] Skipping {kind} media because no destination room has them enabled.")
        return

    if not filename:
        ext = "mp4" if mime_type.startswith("video/") else "jpg"
        filename = f"telegram_media_{message.id}.{ext}"

    # Telethon reports None for the size of some media; treat it as unknown instead of
    # letting the later ">" comparison raise TypeError.
    file_size = (message.file.size if message.file else 0) or 0
    if is_image and config.MIN_IMAGE_SIZE_BYTES > 0 and file_size < config.MIN_IMAGE_SIZE_BYTES:
        logger.info(f"[{source_chat}] Skipping image {filename}: File size ({round(file_size / 1024, 2)} KB) is below minimum required size of {config.MIN_IMAGE_SIZE_KB} KB.")
        return

    if is_video and config.MIN_VIDEO_SIZE_BYTES > 0 and file_size < config.MIN_VIDEO_SIZE_BYTES:
        logger.info(f"[{source_chat}] Skipping video {filename}: File size ({round(file_size / 1024, 2)} KB) is below minimum required size of {config.MIN_VIDEO_SIZE_KB} KB.")
        return

    # Pre-download deduplication check (Telegram Media ID)
    tg_media_id = media.get_telegram_media_id(message)
    if tg_media_id and state.media_deduplicator.is_duplicate(tg_media_id):
        logger.info(f"[{source_chat}] Skipping duplicate media {filename}: Telegram media ID ({tg_media_id}) was already forwarded within {config.DEDUPLICATION_TTL_MINUTES}m.")
        return

    if tg_media_id:
        state.media_deduplicator.mark_seen(tg_media_id)

    logger.info(f"[{source_chat}] Processing media: {filename} ({mime_type}). Starting Telegram download...")
    
    try:
        if download_document is not None:
            media_bytes = await state.tg_client.download_media(download_document, file=bytes)
        else:
            media_bytes = await message.download_media(file=bytes)
        if not media_bytes:
            logger.error(f"[{source_chat}] Telegram download failed for: {filename}")
            if tg_media_id:
                state.media_deduplicator.unmark(tg_media_id)
            return
    except Exception as e:  # noqa: BLE001 - download errors must release the deduplication marks
        logger.error(f"[{source_chat}] Critical error during Telegram download: {e}")
        if tg_media_id:
            state.media_deduplicator.unmark(tg_media_id)
        return

    logger.info(f"[{source_chat}] Download complete ({len(media_bytes)} bytes).")

    # Post-download deduplication check (Content SHA-256)
    content_hash = await asyncio.to_thread(media.compute_sha256, media_bytes)
    content_key = f"sha256_{content_hash}"
    if state.media_deduplicator.is_duplicate(content_key):
        logger.info(f"[{source_chat}] Skipping duplicate media {filename}: Content hash ({content_hash[:12]}...) was already forwarded within {config.DEDUPLICATION_TTL_MINUTES}m.")
        # This media ID was only skipped because of the content mark, which may belong to an
        # attempt still in flight. Tie the ID to that mark: if that attempt fails and releases
        # the mark, this ID is released with it instead of blocking a repost for the TTL.
        state.media_deduplicator.add_alias(content_key, tg_media_id)
        return
    state.media_deduplicator.mark_seen(content_key)

    # Marks are set before processing on purpose: duplicates usually arrive seconds after
    # the original, so releasing the mark only at the very end would let a repost that
    # lands mid-processing slip through. The flip side is that a technical failure would
    # suppress the media for the whole TTL, so every failure path below releases both
    # marks again. Deliberate blocks (Llama Guard verdicts) keep them, so blocked content
    # is not retried.
    def release_dedup_marks():
        if tg_media_id:
            state.media_deduplicator.unmark(tg_media_id)
        state.media_deduplicator.unmark(content_key)

    # Llama Guard Safety Check
    if config.LLAMAGUARD_API_URL:
        verdict = await safety.check_media_safety(media_bytes, mime_type, source_chat, filename)

        if verdict is safety.SafetyVerdict.UNAVAILABLE:
            # Fail-closed for this attempt, but this is a technical failure rather than a
            # policy decision: release the marks so a repost can be classified later.
            state.SAFETY_STATS["unavailable"] += 1
            logger.warning(
                f"[{source_chat}] Media {filename} not forwarded: safety classification unavailable "
                f"(fail-closed). Releasing deduplication marks so a repost can be retried."
            )
            release_dedup_marks()
            return

        if verdict is safety.SafetyVerdict.BLOCKED:
            # Deliberate verdict: the marks stay set so the content is not retried.
            state.SAFETY_STATS["blocked"] += 1
            logger.warning(f"[{source_chat}] Media {filename} blocked by the safety policy. Not forwarded.")
            return

    # Thumbnail preparation for videos and images
    msg_type = "m.image" if mime_type.startswith("image/") else "m.video"
    thumb_url = None
    thumb_info = None
    blurhash_bytes = None

    if msg_type in ("m.video", "m.image"):
        try:
            thumb_bytes = None
            thumb_w = None
            thumb_h = None
            thumb_mime = None

            if msg_type == "m.image":
                # Generate thumbnail locally in-memory to save bandwidth and get optimal dimensions
                res = await asyncio.to_thread(media.generate_image_thumbnail, media_bytes)
                if res:
                    thumb_bytes, thumb_w, thumb_h, thumb_mime = res
            else:
                # For videos, download the high-resolution thumbnail from Telegram
                t_bytes = await message.download_media(thumb=-1, file=bytes)
                if t_bytes:
                    probed = await asyncio.to_thread(media.probe_image, t_bytes)
                    if probed:
                        thumb_bytes = t_bytes
                        thumb_w, thumb_h, thumb_mime = probed

            if thumb_bytes:
                blurhash_bytes = thumb_bytes
                thumb_ext = thumb_mime.split('/', 1)[1]
                uploaded = await upload_thumbnail(
                    thumb_bytes, thumb_mime, thumb_w, thumb_h, f"thumbnail.{thumb_ext}", source_chat
                )
                if uploaded:
                    thumb_url, thumb_info = uploaded
        except Exception as thumb_err:  # noqa: BLE001 - thumbnails are optional, the media is sent either way
            logger.debug(f"[{source_chat}] Thumbnail skipped: {thumb_err}")

    if blurhash_bytes is None and is_image:
        blurhash_bytes = media_bytes

    blurhash_str = None
    if blurhash_bytes is not None:
        blurhash_str = await asyncio.to_thread(media.compute_blurhash, blurhash_bytes)

    # Extract additional metadata from Telegram attributes
    orig_duration = None
    orig_w = None
    orig_h = None
    # Dimensions and duration must describe the file actually sent, which is the rendition
    # when one was selected instead of the primary document.
    metadata_document = download_document or message.document
    if metadata_document and metadata_document.attributes:
        for attr in metadata_document.attributes:
            if hasattr(attr, 'duration') and attr.duration is not None:
                orig_duration = float(attr.duration)
            if hasattr(attr, 'w') and attr.w is not None and hasattr(attr, 'h') and attr.h is not None:
                orig_w = int(attr.w)
                orig_h = int(attr.h)
    elif message.photo and message.photo.sizes:
        largest = message.photo.sizes[-1]
        if hasattr(largest, 'w') and largest.w is not None and hasattr(largest, 'h') and largest.h is not None:
            orig_w = int(largest.w)
            orig_h = int(largest.h)

    # Handle oversized videos (if above configured limit)
    if is_video and len(media_bytes) > config.MAX_MEDIA_SIZE_BYTES:
        logger.info(
            f"[{source_chat}] Oversized video ({round(len(media_bytes) / (1024 * 1024), 2)} MB > "
            f"{config.MAX_MEDIA_SIZE_MB} MB). Action: {config.OVERSIZED_VIDEO_ACTION}..."
        )

        # Scratch data must stay in RAM: wait for room, and skip the media rather than
        # falling back to a disk directory.
        if config.OVERSIZED_VIDEO_ACTION in ("split", "compress"):
            required_scratch = media.shm_required_bytes(len(media_bytes))
            if not await media.reserve_shm_space(required_scratch, source_chat, filename):
                release_dedup_marks()
                return

        if config.OVERSIZED_VIDEO_ACTION == "split":
            parts = await asyncio.to_thread(
                media.split_video_lossless,
                media_bytes,
                filename,
                config.MAX_MEDIA_SIZE_BYTES,
                source_chat,
                known_duration=orig_duration
            )
            if parts:
                # The parts must appear in each room with nothing in between, so everything
                # is uploaded first and the events are posted back to back afterwards. The
                # target rooms are fixed once, so a toggle flipping mid-way cannot split a set.
                target_rooms = state.room_toggles.enabled_rooms(state.media_kind(mime_type))
                if not target_rooms:
                    logger.info(f"[{source_chat}] {filename} not uploaded: no room has videos enabled right now.")
                    return

                prepared_parts = []
                for p in parts:
                    part_info = {
                        "size": p["size"],
                        "mimetype": mime_type,
                        "filename": p["filename"]
                    }
                    if p.get("w") and p.get("h"):
                        part_info["w"] = p["w"]
                        part_info["h"] = p["h"]
                    elif orig_w and orig_h:
                        part_info["w"] = orig_w
                        part_info["h"] = orig_h

                    if p.get("duration_ms"):
                        part_info["duration"] = p["duration_ms"]
                    elif orig_duration:
                        part_info["duration"] = int((orig_duration / len(parts)) * 1000)

                    # Every part previews its own frame; the primary document's thumbnail
                    # (shown at the head of the whole video) stays the fallback when the
                    # per-part extraction or upload did not work out.
                    part_thumb_url = thumb_url
                    part_thumb_info = thumb_info
                    part_blurhash = blurhash_str
                    part_thumb = p.get("thumbnail")
                    if part_thumb:
                        uploaded = await upload_thumbnail(
                            part_thumb,
                            "image/jpeg",
                            p.get("thumbnail_w"),
                            p.get("thumbnail_h"),
                            f"thumbnail_part{p['part_index']}.jpg",
                            source_chat
                        )
                        if uploaded:
                            part_thumb_url, part_thumb_info = uploaded
                            part_blurhash = await asyncio.to_thread(media.compute_blurhash, part_thumb) or blurhash_str

                    if part_thumb_url:
                        part_info["thumbnail_url"] = part_thumb_url
                        part_info["thumbnail_info"] = part_thumb_info

                    prepared = await matrix_io.upload_matrix_media(
                        media_bytes=p["bytes"],
                        mime_type=mime_type,
                        filename=p["filename"],
                        msg_type="m.video",
                        info_dict=part_info,
                        source_chat=source_chat,
                        channel_name=channel_name,
                        caption_prefix=f"Part {p['part_index']}/{p['total_parts']}",
                        blurhash_str=part_blurhash
                    )
                    if prepared is None:
                        # Posting the other parts would leave a gap that a repost then fills
                        # with duplicates, so the split is sent completely or not at all.
                        state.DELIVERY_STATS["failed"] += 1
                        logger.warning(
                            f"[{source_chat}] Upload of split part {p['part_index']}/{p['total_parts']} failed; "
                            f"posting no part and releasing deduplication marks so a repost can be retried."
                        )
                        release_dedup_marks()
                        return
                    prepared_parts.append(prepared)

                results = await matrix_io.post_media_sequence(prepared_parts, target_rooms, source_chat)
                if not all(results):
                    logger.warning(
                        f"[{source_chat}] At least one split part failed to reach Matrix; "
                        f"releasing deduplication marks so a repost can be retried."
                    )
                    release_dedup_marks()
                return

            if config.OVERSIZED_VIDEO_FALLBACK_COMPRESS:
                logger.info(
                    f"[{source_chat}] Lossless split could not produce parts within {config.MAX_MEDIA_SIZE_MB} MB; "
                    f"falling back to compression..."
                )
                compressed = await asyncio.to_thread(
                    media.compress_video,
                    media_bytes,
                    filename,
                    config.MAX_MEDIA_SIZE_BYTES,
                    source_chat,
                    max_height=config.VIDEO_COMPRESSION_MAX_HEIGHT,
                    preset=config.VIDEO_COMPRESSION_PRESET,
                    crf=config.VIDEO_COMPRESSION_CRF,
                    threads=config.VIDEO_COMPRESSION_THREADS
                )
                if compressed and compressed["size"] <= config.MAX_MEDIA_SIZE_BYTES:
                    media_bytes = compressed["bytes"]
                    filename = compressed["filename"]
                    mime_type = "video/mp4"
                    if compressed.get("duration_ms"):
                        orig_duration = compressed["duration_ms"] / 1000.0
                    if compressed.get("w") and compressed.get("h"):
                        orig_w = compressed["w"]
                        orig_h = compressed["h"]
                else:
                    logger.warning(f"[{source_chat}] Video compression fallback failed or result still exceeds {config.MAX_MEDIA_SIZE_MB} MB. Skipping.")
                    release_dedup_marks()
                    return
            else:
                logger.warning(f"[{source_chat}] Video could not be split below {config.MAX_MEDIA_SIZE_MB} MB. Skipping.")
                release_dedup_marks()
                return

        elif config.OVERSIZED_VIDEO_ACTION == "compress":
            compressed = await asyncio.to_thread(
                media.compress_video,
                media_bytes,
                filename,
                config.MAX_MEDIA_SIZE_BYTES,
                source_chat,
                max_height=config.VIDEO_COMPRESSION_MAX_HEIGHT,
                preset=config.VIDEO_COMPRESSION_PRESET,
                crf=config.VIDEO_COMPRESSION_CRF,
                threads=config.VIDEO_COMPRESSION_THREADS
            )
            if compressed and compressed["size"] <= config.MAX_MEDIA_SIZE_BYTES:
                media_bytes = compressed["bytes"]
                filename = compressed["filename"]
                mime_type = "video/mp4"
                if compressed.get("duration_ms"):
                    orig_duration = compressed["duration_ms"] / 1000.0
                if compressed.get("w") and compressed.get("h"):
                    orig_w = compressed["w"]
                    orig_h = compressed["h"]
            else:
                logger.warning(f"[{source_chat}] Video compression failed or result still exceeds {config.MAX_MEDIA_SIZE_MB} MB. Skipping.")
                release_dedup_marks()
                return

        elif config.OVERSIZED_VIDEO_ACTION == "skip":
            logger.warning(f"[{source_chat}] Media skipped: Video ({round(len(media_bytes) / (1024 * 1024), 2)} MB) exceeds limit of {config.MAX_MEDIA_SIZE_MB} MB.")
            return

    # Standard upload for single media item
    info_dict = {
        "size": len(media_bytes),
        "mimetype": mime_type,
        "filename": filename
    }
    if orig_duration is not None:
        info_dict["duration"] = int(orig_duration * 1000)
    if orig_w is not None and orig_h is not None:
        info_dict["w"] = orig_w
        info_dict["h"] = orig_h
    if thumb_url:
        info_dict["thumbnail_url"] = thumb_url
        info_dict["thumbnail_info"] = thumb_info

    sent = await matrix_io.send_matrix_media_event(
        media_bytes=media_bytes,
        mime_type=mime_type,
        filename=filename,
        msg_type=msg_type,
        info_dict=info_dict,
        source_chat=source_chat,
        channel_name=channel_name,
        blurhash_str=blurhash_str
    )
    if not sent:
        logger.warning(
            f"[{source_chat}] Media {filename} could not be delivered to Matrix; "
            f"releasing deduplication marks so a repost can be retried."
        )
        release_dedup_marks()


# --- THE CENTRAL HANDLER FOR EVERYTHING ---
@state.tg_client.on(events.NewMessage(chats=config.TG_CHANNELS))
async def master_handler(event):
    if not event.message.media:
        return

    # event.chat is None when Telethon has not cached the entity yet. Without it neither a
    # username filter nor the General topic (which needs chat.forum) could be resolved.
    chat = event.chat
    if chat is None:
        try:
            chat = await event.get_chat()
        except Exception as e:  # noqa: BLE001 - Telethon RPC errors share no narrow base; filter on the ID alone
            logger.debug(f"[{event.chat_id}] Could not load the chat entity: {e}")

    # Check for topic / thread details (subchannels in forums)
    topic_id = resolve_topic_id(event.message, chat)

    # Validate channel / topic filter match
    if not is_channel_and_topic_allowed(event.chat_id, chat, topic_id):
        return

    # Extract clean display name for channel and topic
    channel_name = str(event.chat_id)
    if chat:
        if getattr(chat, 'title', None):
            channel_name = chat.title
        elif getattr(chat, 'username', None):
            channel_name = chat.username

    topic_name = None
    if topic_id and chat is not None:
        topic_name = await get_topic_name(state.tg_client, chat, topic_id)

    channel_display = channel_name
    if topic_name:
        channel_display = f"{channel_name} - {topic_name}"

    chat_identifier = f"{channel_display} ({event.chat_id})"

    file_size = (event.message.file.size if event.message.file else 0) or 0
    msg_is_video = media.is_video_message(event.message)

    # Prefer a lower-resolution rendition over skipping the video outright. The rendition
    # becomes the file the rest of the pipeline works with, so the size gates below then
    # apply to what is actually downloaded.
    download_document = None
    if msg_is_video and config.PREFER_ALTERNATE_RENDITION and file_size > config.MAX_MEDIA_SIZE_BYTES:
        rendition, mode = media.select_alternate_rendition(
            event.message, config.MAX_MEDIA_SIZE_BYTES, config.OVERSIZED_VIDEO_MAX_INPUT_BYTES
        )
        if rendition is not None:
            logger.info(f"[{chat_identifier}] {describe_rendition_choice(file_size, rendition, mode)}")
            download_document = rendition
            file_size = rendition.size or 0

    if file_size > config.MAX_MEDIA_SIZE_BYTES:
        if msg_is_video and config.OVERSIZED_VIDEO_ACTION in ("split", "compress"):
            if file_size > config.OVERSIZED_VIDEO_MAX_INPUT_BYTES:
                logger.warning(
                    f"[{chat_identifier}] Video skipped: File with {round(file_size / (1024 * 1024), 2)} MB "
                    f"exceeds maximum allowed size for processing ({config.OVERSIZED_VIDEO_MAX_INPUT_MB} MB). "
                    f"{media.describe_alt_documents(event.message, config.MAX_MEDIA_SIZE_BYTES, config.OVERSIZED_VIDEO_MAX_INPUT_BYTES)}"
                )
                return
            logger.info(
                f"[{chat_identifier}] Oversized video detected ({round(file_size / (1024 * 1024), 2)} MB > "
                f"{config.MAX_MEDIA_SIZE_MB} MB). Will {config.OVERSIZED_VIDEO_ACTION} after download."
            )
        else:
            logger.warning(f"[{chat_identifier}] Media skipped: File with {round(file_size / (1024 * 1024), 2)} MB exceeds limit of {config.MAX_MEDIA_SIZE_MB} MB.")
            return

    if event.message.grouped_id is not None:
        album_id = event.message.grouped_id

        if state.album_already_processed(album_id):
            return

        state.PROCESSED_ALBUMS[album_id] = time.monotonic()
        logger.info(f"[{chat_identifier}] New album detected (Grouped ID: {album_id}). Waiting for complete reception...")

        # Short grace period so Telegram can deliver the remaining album items. The guard
        # entry is deliberately not released afterwards: it expires via ALBUM_GUARD_TTL_SECONDS,
        # which keeps a lagging album message from starting a second pass without stalling
        # every other update for ten seconds.
        await asyncio.sleep(2.5)

        try:
            album_messages = await state.tg_client.get_messages(
                event.chat_id,
                min_id=event.message.id - 15,
                max_id=event.message.id + 15,
                limit=30
            )
            filtered_messages = [m for m in album_messages if m.grouped_id == album_id]

            logger.info(f"[{chat_identifier}] Processing {len(filtered_messages)} items from album {album_id}...")
            for msg in reversed(filtered_messages):
                album_download_document = None
                if msg.media:
                    exact_size = (msg.file.size if msg.file else 0) or 0
                    item_is_video = media.is_video_message(msg)
                    if item_is_video and config.PREFER_ALTERNATE_RENDITION and exact_size > config.MAX_MEDIA_SIZE_BYTES:
                        rendition, mode = media.select_alternate_rendition(
                            msg, config.MAX_MEDIA_SIZE_BYTES, config.OVERSIZED_VIDEO_MAX_INPUT_BYTES
                        )
                        if rendition is not None:
                            logger.info(f"[{chat_identifier}] {describe_rendition_choice(exact_size, rendition, mode)}")
                            album_download_document = rendition
                            exact_size = rendition.size or 0
                    if exact_size > config.MAX_MEDIA_SIZE_BYTES:
                        if item_is_video and config.OVERSIZED_VIDEO_ACTION in ("split", "compress"):
                            if exact_size > config.OVERSIZED_VIDEO_MAX_INPUT_BYTES:
                                logger.warning(
                                    f"[{chat_identifier}] Item in album skipped: Actual size ({round(exact_size / (1024 * 1024), 2)} MB) "
                                    f"exceeds maximum allowed size for processing ({config.OVERSIZED_VIDEO_MAX_INPUT_MB} MB). "
                                    f"{media.describe_alt_documents(msg, config.MAX_MEDIA_SIZE_BYTES, config.OVERSIZED_VIDEO_MAX_INPUT_BYTES)}"
                                )
                                continue
                            logger.info(
                                f"[{chat_identifier}] Oversized video in album ({round(exact_size / (1024 * 1024), 2)} MB > "
                                f"{config.MAX_MEDIA_SIZE_MB} MB). Will {config.OVERSIZED_VIDEO_ACTION} after download."
                            )
                        else:
                            logger.warning(f"[{chat_identifier}] Item in album skipped: Actual size ({round(exact_size / (1024 * 1024), 2)} MB) exceeds limit ({config.MAX_MEDIA_SIZE_MB} MB)")
                            continue

                await process_and_upload_media(msg, chat_identifier, channel_display, album_download_document)
        except Exception as e:  # noqa: BLE001 - one broken album must not kill the update loop
            logger.error(f"[{chat_identifier}] Error loading album {album_id}: {e}")
    else:
        await process_and_upload_media(event.message, chat_identifier, channel_display, download_document)

