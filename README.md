# TgMediaToMatrix

An asynchronous, lightweight Python-based bridge that automatically forwards media (images and videos) from Telegram channels or channel forum topics directly into a specified Matrix room.

It extracts, uploads, and structures files cleanly to provide an optimized viewing experience on Matrix clients with fully populated metadata (dimensions, duration, thumbnails) and robust caption layouts.

---

## Key Features

- **Asynchronous Flow**: Built fully on Python's `asyncio` standard library, leveraging `Telethon` for Telegram and `matrix-nio` for Matrix API communication without blocking I/O.
- **Album / Group Support**: Automatically detects and batches group media/albums (grouped messages) to wait for complete reception before forwarding, preserving chronological ordering.
- **Forum Topics / Subchannel Filtering**: Supports monitoring specific subtopics inside Telegram forums using a custom `channel_id:topic_id` configuration format.
- **Case-Insensitive Username Support**: Accepts both public channel usernames (e.g. `MyChannel` or `@MyChannel`) and integer chat IDs, with fully case-insensitive lookup matching.
- **In-Memory Streaming**: Avoids writing files to disk; downloads and uploads media bytes directly through in-memory streams (`io.BytesIO`).
- **Enhanced Client Compatibility**:
  - Populates standard `body`, `filename`, and `info.filename` metadata so both legacy and modern Matrix clients render the filename cleanly.
  - Generates beautiful rich HTML captions (`formatted_body`) adhering to **MSC2530 (Media Captions)** so that clients like Element, Cinny, and SchildiChat render the channel display prefix and filename as clean inline subtexts underneath image/video views.
- **Full Metadata Extraction**: Extracts exact width, height, and duration dimensions from video documents and photo sizes, automatically retrieving and uploading matching video/image thumbnails.
- **Caption Privacy Limit**: Discards original Telegram captions entirely—forwarding only the channel name/topic display name prefix and the media file name to avoid clutter.
- **Automatic File Size Limiting**: Rejects media larger than the configured limit (e.g., 50MB) to preserve resources and server bandwidth.

---

## Prerequisites

Before starting, you need:
1. **Telegram API Credentials**: An `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org/).
2. **Matrix User Account**: A dedicated user account on your Matrix homeserver.
3. **Matrix Access Token**: An access token for that user (can be fetched from client settings such as Element under *All Settings* -> *Help & About* -> *Advanced* -> *Access Token*).
4. **Matrix Room ID**: The internal room ID of the target channel/room where the media should be posted (e.g., `!abcde12345:matrix.org`).

---

## Configuration (`docker-compose.yml`)

The bridge is configured via environment variables in the `docker-compose.yml` file:

| Environment Variable | Description | Example / Default |
| :--- | :--- | :--- |
| `TG_API_ID` | Telegram API ID | `123456` |
| `TG_API_HASH` | Telegram API Hash | `abcdef0123456789abcdef0123456789` |
| `MATRIX_HOMESERVER` | Your Matrix Homeserver URL | `https://matrix.org` |
| `MATRIX_ACCESS_TOKEN` | Access token for the Matrix account | `syt_dW...` |
| `MATRIX_ROOM_ID` | Internal room ID of the destination room | `!abcde12345:matrix.org` |
| `TG_CHANNELS` | Comma-separated list of target channels and topic filters | `MyChannel, -1001234567890:42, @MyChannel` |
| `MAX_MEDIA_SIZE_MB` | Maximum size in MB to download and bridge | `80` (Default: `50`) |

### Specifying Channels & Topics in `TG_CHANNELS`
The `TG_CHANNELS` environment variable accepts a comma-separated list of several formats:
- **Public Username**: `MyChannel` or `@MyChannel` (any casing; `@` is stripped automatically).
- **Private Channel / Group ID**: `-1001234567890`.
- **Forum Topic Filter**: `channel_id:topic_id` or `username:topic_id` (e.g. `-1001234567890:42`). This configures the bridge to only forward media posted inside that specific topic ID (subchannel thread) of the forum.

---

## Deployment & Running

The easiest way to run the bridge is via **Docker Compose**:

### 1. Configure
Edit the `docker-compose.yml` file and insert your configuration credentials:
```yaml
services:
  telegram-matrix-bridge:
    image: python:3.11-slim
    container_name: tg_matrix_media_bridge
    restart: unless-stopped
    volumes:
      - ./app:/app
      - ./session_data:/app/session
    working_dir: /app
    command: sh -c "pip install --no-cache-dir telethon matrix-nio && python bridge.py"
    environment:
      - TG_API_ID=your_tg_api_id
      - TG_API_HASH=your_tg_api_hash
      - MATRIX_HOMESERVER=https://matrix.org
      - MATRIX_ACCESS_TOKEN=your_matrix_access_token
      - MATRIX_ROOM_ID=!your_room_id:matrix.org
      - TG_CHANNELS=MyChannel,MyOtherChannel
      - MAX_MEDIA_SIZE_MB=80
```

### 2. First-Run Session Authentication
On the very first run, Telethon needs to authenticate with your Telegram account (using your phone number and login code). To do this interactively, run the container with an interactive shell:

```bash
docker compose run --entrypoint python telegram-matrix-bridge bridge.py
```

1. Enter your **phone number** (including country code, e.g. `+1234567890`).
2. Enter the **login code** sent to your Telegram app.
3. If two-factor authentication (2FA) is enabled, enter your **password**.

Once authenticated, the session file will be generated and stored under the `./session_data` directory. Since this folder is mounted into the container, you won't need to re-authenticate when restarting the service.

### 3. Run in Background
Once session authentication is complete, you can stop the interactive container (`Ctrl+C`) and start the bridge in detached/background mode:

```bash
docker compose up -d
```

To view the live logs:
```bash
docker compose logs -f tg_matrix_media_bridge
```

---

## File Structure

```bash
TgMediaToMatrix/
├── app/
│   └── bridge.py        # Core bridge python application
├── session_data/        # Persistent Telegram session database (generated)
├── docker-compose.yml   # Multi-container orchestration definition
└── README.md            # This documentation file
```

---

## Architecture and Guidelines Compliance

This bridge strictly follows the project-specific developer guidelines:
- **No Disk writes**: All media file and thumbnail streams are managed entirely in-memory using `io.BytesIO`.
- **Consistent Log Entries**: All log records are context-aware and prefix entries with the source chat's resolved name/ID: `[{source_chat}]`.
- **Descriptive Error Handling**: Startup verification processes make sure that all required configurations are thoroughly validated at runtime. Any failure will output a clean descriptive traceback, preventing silent failure.
- **Graceful Fault Tolerance**: Errors inside individual album elements or single uploads won't crash the container—the loop will handle, log, and skip gracefully.
