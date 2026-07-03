import os
import io
import asyncio
import logging
from telethon import TelegramClient, events
from nio import AsyncClient, UploadResponse, RoomSendResponse, RoomSendError

# Set up professional logging format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Load and validate environment variables
def get_env_or_raise(key: str) -> str:
    val = os.environ.get(key)
    if not val or not val.strip():
        raise ValueError(f"Required environment variable '{key}' is missing or empty.")
    return val.strip()

try:
    TG_API_ID = int(get_env_or_raise("TG_API_ID"))
    TG_API_HASH = get_env_or_raise("TG_API_HASH")
    MATRIX_HOMESERVER = get_env_or_raise("MATRIX_HOMESERVER").rstrip('/')
    MATRIX_ACCESS_TOKEN = get_env_or_raise("MATRIX_ACCESS_TOKEN")
    MATRIX_ROOM_ID = get_env_or_raise("MATRIX_ROOM_ID")

    MAX_MEDIA_SIZE_MB = int(os.environ.get("MAX_MEDIA_SIZE_MB", 50))
    MAX_MEDIA_SIZE_BYTES = MAX_MEDIA_SIZE_MB * 1024 * 1024

    # Extract channels, ignoring empty elements, supporting channel_id:topic_id formats
    TG_CHANNELS_RAW = get_env_or_raise("TG_CHANNELS")
    TG_CHANNELS = []
    TG_TOPIC_FILTERS = {}  # maps base_chat_id -> set of topic_ids (ints)
    TG_UNFILTERED_CHANNELS = set()  # tracks channels configured to allow all topics

    for item in TG_CHANNELS_RAW.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            parts = item.split(":", 1)
            chan_part = parts[0].strip()
            topic_part = parts[1].strip()

            if chan_part.replace('-', '').isdigit():
                chan_id = int(chan_part)
            else:
                chan_id = chan_part

            if topic_part.isdigit():
                topic_id = int(topic_part)
            else:
                raise ValueError(f"Invalid topic ID in '{item}'")

            TG_CHANNELS.append(chan_id)
            if chan_id not in TG_TOPIC_FILTERS:
                TG_TOPIC_FILTERS[chan_id] = set()
            TG_TOPIC_FILTERS[chan_id].add(topic_id)
        else:
            if item.replace('-', '').isdigit():
                chan_id = int(item)
            else:
                chan_id = item
            TG_CHANNELS.append(chan_id)
            TG_UNFILTERED_CHANNELS.add(chan_id)

    # De-duplicate TG_CHANNELS list while preserving order
    unique_channels = []
    for c in TG_CHANNELS:
        if c not in unique_channels:
            unique_channels.append(c)
    TG_CHANNELS = unique_channels

    if not TG_CHANNELS:
        raise ValueError("TG_CHANNELS must contain at least one valid channel identifier.")

except Exception as init_err:
    logging.critical(f"Configuration initialization failed: {init_err}")
    raise

# Cache for already processed album IDs
PROCESSED_ALBUMS = set()

# Cache for resolved topic names
TOPIC_NAMES_CACHE = {}

async def get_topic_name(client, chat_entity, topic_id):
    if not topic_id:
        return None
    cache_key = (chat_entity.id, topic_id)
    if cache_key in TOPIC_NAMES_CACHE:
        return TOPIC_NAMES_CACHE[cache_key]
    
    try:
        from telethon.tl.functions.channels import GetForumTopicsByIDRequest
        result = await client(GetForumTopicsByIDRequest(
            channel=chat_entity,
            topics=[topic_id]
        ))
        if result and result.topics:
            name = result.topics[0].title
            TOPIC_NAMES_CACHE[cache_key] = name
            return name
    except Exception as e:
        logging.debug(f"Could not fetch topic name for topic {topic_id} in {chat_entity.id}: {e}")
        
    return f"Topic {topic_id}"

tg_client = TelegramClient(
    'session/tgmabr', 
    TG_API_ID, 
    TG_API_HASH,
    device_model="Desktop",
    system_version="Windows 10",
    app_version="4.8.4"
)

# Initialize global Matrix client session
matrix_client = AsyncClient(MATRIX_HOMESERVER)
matrix_client.access_token = MATRIX_ACCESS_TOKEN

async def process_and_upload_media(message, source_chat, channel_name):
    """Process a single Telegram message and stream the media to Matrix"""
    mime_type = message.file.mime_type if message.file else None
    filename = message.file.name if message.file else None
    
    if message.photo and not mime_type:
        mime_type = "image/jpeg"
        filename = f"telegram_photo_{message.id}.jpg"
    elif not mime_type:
        return

    if not (mime_type.startswith("image/") or mime_type.startswith("video/")):
        return

    if not filename:
        ext = "mp4" if mime_type.startswith("video/") else "jpg"
        filename = f"telegram_media_{message.id}.{ext}"

    logging.info(f"[{source_chat}] Processing media: {filename} ({mime_type}). Starting Telegram download...")
    
    try:
        media_bytes = await message.download_media(file=bytes)
        if not media_bytes:
            logging.error(f"[{source_chat}] Telegram download failed for: {filename}")
            return
    except Exception as e:
        logging.error(f"[{source_chat}] Critical error during Telegram download: {e}")
        return

    logging.info(f"[{source_chat}] Download complete ({len(media_bytes)} bytes). Uploading to Matrix homeserver...")
    
    try:
        # Upload main media using matrix-nio's built-in client method
        upload_resp, _ = await matrix_client.upload(
            io.BytesIO(media_bytes),
            content_type=mime_type,
            filename=filename,
            filesize=len(media_bytes)
        )
        
        if not isinstance(upload_resp, UploadResponse):
            logging.error(f"[{source_chat}] Matrix server rejected upload! Response: {upload_resp}")
            return
            
        content_uri = upload_resp.content_uri
        logging.info(f"[{source_chat}] Upload successful! MXC-URI: {content_uri}. Sending room message...")
        
        msg_type = "m.image" if mime_type.startswith("image/") else "m.video"

        # Populate base info object with the exact file size of downloaded bytes
        info_dict = {
            "size": len(media_bytes),
            "mimetype": mime_type
        }

        # Extract additional metadata from Telegram attributes
        if message.document and message.document.attributes:
            for attr in message.document.attributes:
                if hasattr(attr, 'duration') and attr.duration is not None:
                    info_dict["duration"] = int(attr.duration * 1000)
                if hasattr(attr, 'w') and attr.w is not None and hasattr(attr, 'h') and attr.h is not None:
                    info_dict["w"] = int(attr.w)
                    info_dict["h"] = int(attr.h)
        elif message.photo and message.photo.sizes:
            largest = message.photo.sizes[-1]
            if hasattr(largest, 'w') and largest.w is not None and hasattr(largest, 'h') and largest.h is not None:
                info_dict["w"] = int(largest.w)
                info_dict["h"] = int(largest.h)

        # Thumbnail upload for videos and images
        if msg_type in ("m.video", "m.image"):
            try:
                thumb_idx = -1 if msg_type == "m.video" else 0
                thumb_bytes = await message.download_media(thumb=thumb_idx, file=bytes)
                if thumb_bytes:
                    thumb_resp, _ = await matrix_client.upload(
                        io.BytesIO(thumb_bytes),
                        content_type="image/jpeg",
                        filename="thumbnail.jpg",
                        filesize=len(thumb_bytes)
                    )
                    if isinstance(thumb_resp, UploadResponse):
                        info_dict["thumbnail_url"] = thumb_resp.content_uri
                        info_dict["thumbnail_info"] = {
                            "mimetype": "image/jpeg",
                            "size": len(thumb_bytes)
                        }
            except Exception as thumb_err:
                logging.debug(f"[{source_chat}] Thumbnail skipped: {thumb_err}")

        body_text = f"[{channel_name}] {filename}"

        matrix_content = {
            "msgtype": msg_type,
            "body": body_text,
            "url": content_uri
        }
        if info_dict:
            matrix_content["info"] = info_dict

        send_response = await matrix_client.room_send(
            room_id=MATRIX_ROOM_ID,
            message_type="m.room.message",
            content=matrix_content
        )
        if isinstance(send_response, RoomSendResponse):
            logging.info(f"[{source_chat}] Event successfully posted in Matrix room (Event ID: {send_response.event_id})")
        elif isinstance(send_response, RoomSendError):
            logging.error(f"[{source_chat}] Failed to post event to Matrix room: {send_response.message} (status code: {send_response.status_code})")
        else:
            logging.error(f"[{source_chat}] Unknown response type when posting event to Matrix room: {send_response}")
            
    except Exception as e:
        logging.error(f"[{source_chat}] General error during Matrix transfer of {filename}: {e}")


# --- THE CENTRAL HANDLER FOR EVERYTHING ---
@tg_client.on(events.NewMessage(chats=TG_CHANNELS))
async def master_handler(event):
    if not event.message.media:
        return

    # Check for topic / thread details (subchannels in forums)
    topic_id = None
    r = event.message.reply_to
    if r and getattr(r, 'forum_topic', False):
        topic_id = r.reply_to_top_id if r.reply_to_top_id is not None else r.reply_to_msg_id

    # Check if we should filter by topic
    chat_id = event.chat_id
    chat_username = event.chat.username if event.chat else None

    allowed_topics = None
    # If the channel itself was configured without any topic filter, allow all topics.
    if chat_id in TG_UNFILTERED_CHANNELS or chat_username in TG_UNFILTERED_CHANNELS:
        pass
    else:
        if chat_id in TG_TOPIC_FILTERS:
            allowed_topics = TG_TOPIC_FILTERS[chat_id]
        elif chat_username in TG_TOPIC_FILTERS:
            allowed_topics = TG_TOPIC_FILTERS[chat_username]

    if allowed_topics is not None:
        if topic_id not in allowed_topics:
            return

    # Resolve channel name and optional topic name
    channel_name = str(event.chat_id)
    if event.chat:
        if hasattr(event.chat, 'title') and event.chat.title:
            channel_name = event.chat.title
        elif hasattr(event.chat, 'username') and event.chat.username:
            channel_name = event.chat.username

    topic_name = None
    if topic_id:
        topic_name = await get_topic_name(tg_client, event.chat, topic_id)

    channel_display = channel_name
    if topic_name:
        channel_display = f"{channel_name} - {topic_name}"

    chat_identifier = f"{channel_display} ({event.chat_id})"

    file_size = event.message.file.size if event.message.file else 0
    if file_size > MAX_MEDIA_SIZE_BYTES:
        logging.warning(f"[{chat_identifier}] Media skipped: File with {round(file_size / (1024 * 1024), 2)} MB exceeds limit of {MAX_MEDIA_SIZE_MB} MB.")
        return

    if event.message.grouped_id is not None:
        album_id = event.message.grouped_id
        
        if album_id in PROCESSED_ALBUMS:
            return
            
        PROCESSED_ALBUMS.add(album_id)
        logging.info(f"[{chat_identifier}] New album detected (Grouped ID: {album_id}). Waiting for complete reception...")
        
        await asyncio.sleep(2.5)
        
        try:
            album_messages = await tg_client.get_messages(
                event.chat_id, 
                min_id=event.message.id - 15, 
                max_id=event.message.id + 15,
                limit=30
            )
            filtered_messages = [m for m in album_messages if m.grouped_id == album_id]
            
            logging.info(f"[{chat_identifier}] Processing {len(filtered_messages)} items from album {album_id}...")
            for msg in reversed(filtered_messages):
                if msg.media:
                    exact_size = msg.file.size if msg.file else 0
                    if exact_size > MAX_MEDIA_SIZE_BYTES:
                        logging.warning(f"[{chat_identifier}] Item in album skipped: Actual size ({round(exact_size / (1024 * 1024), 2)} MB) exceeds limit ({MAX_MEDIA_SIZE_MB} MB)")
                        continue
                    
                await process_and_upload_media(msg, chat_identifier, channel_display)
        except Exception as e:
            logging.error(f"[{chat_identifier}] Error loading album {album_id}: {e}")
            
        await asyncio.sleep(10)
        PROCESSED_ALBUMS.discard(album_id)
    else:
        await process_and_upload_media(event.message, chat_identifier, channel_display)


async def main():
    logging.info("Starting Telegram client...")
    await tg_client.start()
    logging.info(f"Bridge successfully started and active for channels: {TG_CHANNELS}")
    logging.info(f"Configured media limit: {MAX_MEDIA_SIZE_MB} MB")
    try:
        await tg_client.run_until_disconnected()
    finally:
        await matrix_client.close()

if __name__ == '__main__':
    asyncio.run(main())
