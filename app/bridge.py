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

    # Extract channels, ignoring empty elements
    TG_CHANNELS_RAW = get_env_or_raise("TG_CHANNELS")
    TG_CHANNELS = [
        int(i.strip()) if i.strip().replace('-', '').isdigit() else i.strip()
        for i in TG_CHANNELS_RAW.split(",")
        if i.strip()
    ]
    if not TG_CHANNELS:
        raise ValueError("TG_CHANNELS must contain at least one valid channel identifier.")

except Exception as init_err:
    logging.critical(f"Configuration initialization failed: {init_err}")
    raise

# Cache for already processed album IDs
PROCESSED_ALBUMS = set()

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
            filename=filename
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

        # Thumbnail upload for videos
        if msg_type == "m.video":
            try:
                thumb_bytes = await message.download_media(thumb=-1, file=bytes)
                if thumb_bytes:
                    thumb_resp, _ = await matrix_client.upload(
                        io.BytesIO(thumb_bytes),
                        content_type="image/jpeg",
                        filename="thumbnail.jpg"
                    )
                    if isinstance(thumb_resp, UploadResponse):
                        info_dict["thumbnail_url"] = thumb_resp.content_uri
                        info_dict["thumbnail_info"] = {
                            "mimetype": "image/jpeg",
                            "size": len(thumb_bytes)
                        }
            except Exception as thumb_err:
                logging.debug(f"[{source_chat}] Thumbnail skipped: {thumb_err}")

        caption = message.message.strip() if message.message else ""
        display_text = caption if caption else filename
        body_text = f"[{channel_name}] {display_text}"

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

    chat_identifier = event.chat.username if event.chat.username else str(event.chat_id)

    channel_name = str(event.chat_id)
    if event.chat:
        if hasattr(event.chat, 'title') and event.chat.title:
            channel_name = event.chat.title
        elif hasattr(event.chat, 'username') and event.chat.username:
            channel_name = event.chat.username

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
                    
                await process_and_upload_media(msg, chat_identifier, channel_name)
        except Exception as e:
            logging.error(f"[{chat_identifier}] Error loading album {album_id}: {e}")
            
        await asyncio.sleep(10)
        PROCESSED_ALBUMS.discard(album_id)
    else:
        await process_and_upload_media(event.message, chat_identifier, channel_name)


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
