import os
import asyncio
import logging
import httpx
from telethon import TelegramClient, events
from nio import AsyncClient

# Professionelles Logging-Format einrichten
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Umgebungsvariablen laden
TG_API_ID = int(os.environ.get("TG_API_ID"))
TG_API_HASH = os.environ.get("TG_API_HASH")
MATRIX_HOMESERVER = os.environ.get("MATRIX_HOMESERVER").rstrip('/')
MATRIX_ACCESS_TOKEN = os.environ.get("MATRIX_ACCESS_TOKEN")
MATRIX_ROOM_ID = os.environ.get("MATRIX_ROOM_ID")

MAX_MEDIA_SIZE_MB = int(os.environ.get("MAX_MEDIA_SIZE_MB", 50))
MAX_MEDIA_SIZE_BYTES = MAX_MEDIA_SIZE_MB * 1024 * 1024

# Kanäle extrahieren
TG_CHANNELS = [int(i.strip()) if i.strip().replace('-', '').isdigit() else i.strip() for i in os.environ.get("TG_CHANNELS").split(",")]

# Cache für bereits verarbeitete Alben-IDs
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
    """Verarbeitet eine einzelne Telegram-Nachricht und streamt das Medium zu Matrix"""
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

    logging.info(f"[{source_chat}] Verarbeite Medium: {filename} ({mime_type}). Starte Download von Telegram...")
    
    try:
        media_bytes = await message.download_media(file=bytes)
        if not media_bytes:
            logging.error(f"[{source_chat}] Download von Telegram fehlgeschlagen für: {filename}")
            return
    except Exception as e:
        logging.error(f"[{source_chat}] Kritischer Fehler beim Download von Telegram: {e}")
        return

    logging.info(f"[{source_chat}] Download abgeschlossen ({len(media_bytes)} Bytes). Starte HTTPX-Upload zu Matrix...")
    
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
            logging.error(f"[{source_chat}] Matrix-Server hat den Upload abgelehnt! HTTP Status: {response.status_code} | Antwort: {response.text}")
            return
            
        data = response.json()
        content_uri = data.get("content_uri")
        
        if not content_uri:
            return

        logging.info(f"[{source_chat}] Upload erfolgreich! MXC-URI: {content_uri}. Sende Raum-Nachricht via matrix-nio...")
        
        matrix_client = AsyncClient(MATRIX_HOMESERVER)
        matrix_client.access_token = MATRIX_ACCESS_TOKEN
        
        try:
            msg_type = "m.image" if mime_type.startswith("image/") else "m.video"
            info_dict = {}
            
            # Extraktion der Telegram Metadaten
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

            # Thumbnail-Upload für Videos
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
            logging.info(f"[{source_chat}] Event erfolgreich in Matrix-Raum gepostet (Event ID: {getattr(send_response, 'event_id', 'Unbekannt')})")
        finally:
            await matrix_client.close()
            
    except Exception as e:
        logging.error(f"[{source_chat}] Allgemeiner Fehler bei der Matrix-Übertragung von {filename}: {e}")


# --- DER ZENTRALE HANDLER FÜR ALLES ---
@tg_client.on(events.NewMessage(chats=TG_CHANNELS))
async def master_handler(event):
    if not event.message.media:
        return

    chat_identifier = event.chat.username if event.chat.username else str(event.chat_id)

    file_size = event.message.file.size if event.message.file else 0
    if file_size > MAX_MEDIA_SIZE_BYTES:
        logging.warning(f"[{chat_identifier}] Medium übersprungen: Datei ist mit {round(file_size / (1024 * 1024), 2)} MB größer als das Limit von {MAX_MEDIA_SIZE_MB} MB.")
        return

    if event.message.grouped_id is not None:
        album_id = event.message.grouped_id
        
        if album_id in PROCESSED_ALBUMS:
            return
            
        PROCESSED_ALBUMS.add(album_id)
        logging.info(f"[{chat_identifier}] Neues Album (Grouped ID: {album_id}) erkannt. Warte auf vollständigen Empfang...")
        
        await asyncio.sleep(2.5)
        
        try:
            album_messages = await tg_client.get_messages(event.chat_id, min_id=event.message.id - 15, max_id=event.message.id + 15)
            filtered_messages = [m for m in album_messages if m.grouped_id == album_id]
            
            logging.info(f"[{chat_identifier}] Verarbeite {len(filtered_messages)} Elemente aus dem Album {album_id}...")
            for msg in reversed(filtered_messages):
                try:
                    if msg.media:
                        file_info = await tg_client.get_file_info(msg.media)
                        exact_size = file_info.size if hasattr(file_info, 'size') else (msg.file.size if msg.file else 0)
                        
                        if exact_size > MAX_MEDIA_SIZE_BYTES:
                            logging.warning(f"[{chat_identifier}] Element im Album übersprungen: Reale Größe ({round(exact_size / (1024 * 1024), 2)} MB) überschreitet Limit ({MAX_MEDIA_SIZE_MB} MB).")
                            continue
                except Exception as size_err:
                    logging.debug(f"[{chat_identifier}] Konnte exakte Größe nicht via API prüfen: {size_err}")
                    if msg.file and msg.file.size > MAX_MEDIA_SIZE_BYTES:
                        continue
                    
                await process_and_upload_media(msg, chat_identifier)
        except Exception as e:
            logging.error(f"[{chat_identifier}] Fehler beim Laden des Albums {album_id}: {e}")
            
        await asyncio.sleep(10)
        PROCESSED_ALBUMS.discard(album_id)
    else:
        await process_and_upload_media(event.message, chat_identifier)


async def main():
    logging.info("Starte Telegram-Client...")
    await tg_client.start()
    logging.info(f"Bridge erfolgreich gestartet und aktiv für Kanäle: {TG_CHANNELS}")
    logging.info(f"Konfiguriertes Medien-Limit: {MAX_MEDIA_SIZE_MB} MB")
    await tg_client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
