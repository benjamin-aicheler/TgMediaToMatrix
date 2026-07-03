import os
import asyncio
import logging
import httpx
from telethon import TelegramClient, events
from nio import AsyncClient

# Set up professional logging format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Load environment variables
TG_API_ID = int(os.environ.get("TG_API_ID"))
TG_API_HASH = os.environ.get("TG_API_HASH")
MATRIX_HOMESERVER = os.environ.get("MATRIX_HOMESERVER").rstrip('/')
MATRIX_ACCESS_TOKEN = os.environ.get("MATRIX_ACCESS_TOKEN")
MATRIX_ROOM_ID = os.environ.get("MATRIX_ROOM_ID")

MAX_MEDIA_SIZE_MB = int(os.environ.get("MAX_MEDIA_SIZE_MB", 50))
MAX_MEDIA_SIZE_BYTES = MAX_MEDIA_SIZE_MB * 1024 * 1024

# Extract channels
TG_CHANNELS = [int(i.strip()) if i.strip().replace('-', '').isdigit() else i.strip() for i in os.environ.get("TG_CHANNELS").split(",")]

# Cache for already processed album IDs
PROCESSED_ALBUMS = set()

tg_client = TelegramClient(
    'session/tg_matrix_bridge', 
    TG_API_ID, 
    TG_API_HASH,
    device_model="Desktop",
    system_version="Windows 10",
    app_version="4.8.4"
)

async def process_and_upload_media(message, source_chat):
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

    logging.info(f"[{source_chat}] Download complete ({len(media_bytes)} bytes). Starting HTTPX upload to Matrix...")
    
    upload_url = f"{MATRIX_HOMESERVER}/_matrix/media/v3/upload"
    headers = {
        "Authorization": f"Bearer {MATRIX_ACCESS_TOKEN}",
        "Content-Type": mime_type,
        "Content-Length": str(len(media_bytes))
    }
    params = {"filename": filename}

    try:
        async with httpx.AsyncClient(timeout=60.0) as http_client:
            response = await http_client.post(
                upload_url,
                content=media_bytes,
                headers=headers,
                params=params
            )
        
        if response.status_code != 200:
            logging.error(f"[{source_chat}] Matrix server rejected upload! HTTP Status: {response.status_code} | Response: {response.text}")
            return
            
        data = response.json()
        content_uri = data.get("content_uri")
        
        if not content_uri:
            return

        logging.info(f"[{source_chat}] Upload successful! MXC-URI: {content_uri}. Sending room message via matrix-nio...")
        
        matrix_client = AsyncClient(MATRIX_HOMESERVER)
        matrix_client.access_token = MATRIX_ACCESS_TOKEN
        
        try:
            msg_type = "m.image" if mime_type.startswith("image/") else "m.video"

            # Populate base info object with the exact file size of downloaded bytes
            info_dict = {
                "size": len(media_bytes),
                "mimetype": mime_type
            }

            # Extract additional metadata from Telegram attributes
            if message.document and message.document.attributes:
                for attr in message.document.attributes:
                    if hasattr(attr, 'duration'):
                        info_dict["duration"] = attr.duration * 1000
                    if hasattr(attr, 'w') and hasattr(attr, 'h'):
                        info_dict["w"] = attr.w
                        info_dict["h"] = attr.h
            elif message.photo and message.photo.sizes:
                largest = message.photo.sizes[-1]
                if hasattr(largest, 'w') and hasattr(largest, 'h'):
                    info_dict["w"] = largest.w
                    info_dict["h"] = largest.h

            # Thumbnail upload for videos
            if msg_type == "m.video":
                try:
                    thumb_bytes = await message.download_media(thumb=-1, file=bytes)
                    if thumb_bytes:
                        async with httpx.AsyncClient(timeout=30.0) as http_client:
                            thumb_res = await http_client.post(
                                f"{MATRIX_HOMESERVER}/_matrix/media/v3/upload",
                                content=thumb_bytes,
                                headers={
                                    "Authorization": f"Bearer {MATRIX_ACCESS_TOKEN}",
                                    "Content-Type": "image/jpeg",
                                    "Content-Length": str(len(thumb_bytes))
                                },
                                params={"filename": "thumbnail.jpg"}
                            )
                        if thumb_res.status_code == 200:
                            thumb_data = thumb_res.json()
                            if "content_uri" in thumb_data:
                                info_dict["thumbnail_url"] = thumb_data["content_uri"]
                                info_dict["thumbnail_info"] = {
                                    "mimetype": "image/jpeg",
                                    "size": len(thumb_bytes)
                                }
                except Exception as thumb_err:
                    logging.debug(f"[{source_chat}] Thumbnail skipped: {thumb_err}")

            matrix_content = {
                "msgtype": msg_type,
                "body": filename,
                "url": content_uri
            }
            if info_dict:
                matrix_content["info"] = info_dict

            send_response = await matrix_client.room_send(
                room_id=MATRIX_ROOM_ID,
                message_type="m.room.message",
                content=matrix_content
            )
            logging.info(f"[{source_chat}] Event successfully posted in Matrix room (Event ID: {getattr(send_response, 'event_id', 'Unknown')})")
        finally:
            await matrix_client.close()
            
    except Exception as e:
        logging.error(f"[{source_chat}] General error during Matrix transfer of {filename}: {e}")


# --- THE CENTRAL HANDLER FOR EVERYTHING ---
@tg_client.on(events.NewMessage(chats=TG_CHANNELS))
async def master_handler(event):
    if not event.message.media:
        return

    chat_identifier = event.chat.username if event.chat.username else str(event.chat_id)

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
            album_messages = await tg_client.get_messages(event.chat_id, min_id=event.message.id - 15, max_id=event.message.id + 15)
            filtered_messages = [m for m in album_messages if m.grouped_id == album_id]
            
            logging.info(f"[{chat_identifier}] Processing {len(filtered_messages)} items from album {album_id}...")
            for msg in reversed(filtered_messages):
                try:
                    if msg.media:
                        file_info = await tg_client.get_file_info(msg.media)
                        exact_size = file_info.size if hasattr(file_info, 'size') else (msg.file.size if msg.file else 0)
                        
                        if exact_size > MAX_MEDIA_SIZE_BYTES:
                            logging.warning(f"[{chat_identifier}] Item in album skipped: Actual size ({round(exact_size / (1024 * 1024), 2)} MB) exceeds limit ({MAX_MEDIA_SIZE_MB} MB)")
                            continue
                except Exception as size_err:
                    logging.debug(f"[{chat_identifier}] Could not check exact size via API: {size_err}")
                    if msg.file and msg.file.size > MAX_MEDIA_SIZE_BYTES:
                        continue
                    
                await process_and_upload_media(msg, chat_identifier)
        except Exception as e:
            logging.error(f"[{chat_identifier}] Error loading album {album_id}: {e}")
            
        await asyncio.sleep(10)
        PROCESSED_ALBUMS.discard(album_id)
    else:
        await process_and_upload_media(event.message, chat_identifier)


async def main():
    logging.info("Starting Telegram client...")
    await tg_client.start()
    logging.info(f"Bridge successfully started and active for channels: {TG_CHANNELS}")
    logging.info(f"Configured media limit: {MAX_MEDIA_SIZE_MB} MB")
    await tg_client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
