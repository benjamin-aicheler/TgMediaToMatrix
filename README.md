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
| `ENABLE_IMAGES` | Set to `false` to disable bridging of images | `true` (Default: `true`) |
| `ENABLE_VIDEOS` | Set to `false` to disable bridging of videos | `true` (Default: `true`) |
| `LLAMAGUARD_API_URL` | Base URL of an OpenAI-compatible Vision API for Llama Guard checks | `http://192.168.1.100:8000/v1` (Default: `None`/Disabled) |
| `LLAMAGUARD_MODEL_NAME` | Model name to request for safety moderation | `meta-llama/llama-guard-4-12b` |
| `LLAMAGUARD_API_KEY` | API authentication key for Llama Guard endpoint if required | `your-api-key` (Default: `None`) |
| `LLAMAGUARD_CHECKS` | Comma-separated list of safety categories to block. If empty, blocks on any safety violation. | `S1,S2,S3,S4` (Default: empty / block on any) |

### Specifying Channels & Topics in `TG_CHANNELS`
The `TG_CHANNELS` environment variable accepts a comma-separated list of several formats:
- **Public Username**: `MyChannel` or `@MyChannel` (any casing; `@` is stripped automatically).
- **Private Channel / Group ID**: `-1001234567890`.
- **Forum Topic Filter**: `channel_id:topic_id` or `username:topic_id` (e.g. `-1001234567890:42`). This configures the bridge to only forward media posted inside that specific topic ID (subchannel thread) of the forum.

---

## Content Moderation with Meta Llama Guard 4 12B

The bridge supports real-time, automated image and video content moderation using Meta Llama Guard 4 12B (or any OpenAI-compatible API endpoint hosting a compatible model).

### How it Works
1. **In-Memory Frame Extraction**: When a video is downloaded, the bridge extracts its middle frame in-memory as JPEG bytes using `PyAV` (`av`) and `Pillow` (`PIL`). No media files are ever written to disk.
2. **OpenAI-Compatible Vision API**: The bridge encodes the image (or extracted video frame) into base64 and forwards it to the specified OpenAI-compatible `/chat/completions` vision endpoint.
3. **Classification and Categories**: Llama Guard typically outputs safety ratings (e.g., `safe` or `unsafe` followed by the violated categories like `S1`, `S2`, etc.).
4. **Fail-Closed Design**: If the Llama Guard endpoint is unreachable, is misconfigured, or lacks the necessary library bindings (`av` or `Pillow`), the bridge logs the error and blocks the media from being forwarded. This ensures that no unchecked or unmoderated media passes through when content moderation is enabled.

### Configuring Safety Checks
The `LLAMAGUARD_CHECKS` environment variable allows you to configure which specific `Sxx` guidelines are strictly enforced:
- **Block All Violations (Default)**: If `LLAMAGUARD_CHECKS` is left empty or omitted, *any* safety violation returned by Llama Guard will block the media from being forwarded.
- **Selective Enforcement**: If you only care about specific categories, list them in a comma-separated format (e.g., `S1,S2,S3`). If Llama Guard flags media with an `S5` violation but your config only lists `S1,S2,S3`, the bridge will log the safety warning but still forward the media.

### Available Safety Categories

Below is the standard taxonomy of categories defined by the Meta Llama Guard 3 & 4 models that you can selectively filter:

| Category | Name | Description |
| :--- | :--- | :--- |
| `S1` | Violent Crimes | Content that encourages, depicts, or facilitates violent acts, including physical violence, murder, assault, kidnapping, or robbery. |
| `S2` | Non-Violent Crimes | Content that encourages, depicts, or facilitates non-violent crimes, such as theft, burglary, fraud, drug distribution, smuggling, or vandalism. |
| `S3` | Sex-Related Crimes | Content depicting or promoting sexual assault, sexual violence, sexual exploitation, or human trafficking. |
| `S4` | Child Sexual Exploitation | Content promoting or depicting child sexual abuse material (CSAM), grooming, sexual exploitation, or abuse of minors. |
| `S5` | Defamation | Content containing false statements of fact targeted at harming the reputation of individuals or organizations. |
| `S6` | Cyberattacks | Content depicting, encouraging, or offering instructions for cyberattacks, hacking, phishing, creating malware, or launching DDoS campaigns. |
| `S7` | Cyberattacks and Critical Infrastructure | Content promoting or describing cyberattacks targeting utility grids, transportation systems, water supplies, or public critical infrastructure. |
| `S8` | Weapons of Mass Destruction | Content depicting, instructing, or facilitating the manufacturing, acquisition, or deployment of nuclear, chemical, biological, or radiological weapons. |
| `S9` | Sexually Explicit Content | Content depicting sexually explicit material, pornography, nudity, or explicit sexual acts. |
| `S10` | Harassment | Content designed to harass, stalk, bully, intimidate, or abuse a specific individual. |
| `S11` | Privacy Violations | Content that shares personally identifiable information (PII) without consent (doxxing), such as addresses, phone numbers, SSNs, or credit cards. |
| `S12` | Non-Consensual Sexual Content (NCSC) | Content containing non-consensual sexual depictions, such as revenge pornography or sexually explicit deepfakes. |
| `S13` | Violent Extremism | Content depicting, promoting, or recruiting for terrorist acts, violent extremist groups, or sharing extremist propaganda. |
| `S14` | Specialized Advice | Content offering unlicensed, dangerous, or illegal advice in highly regulated professional fields (e.g., medical, financial, or legal advice). |

> [!NOTE]
> Ensure that you verify the specific category codes (S1 to S14) supported by your self-hosted Llama Guard deployment or downstream API provider, as taxonomies can slightly vary between model sub-versions.



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
    command: sh -c "pip install --no-cache-dir telethon matrix-nio av Pillow && python bridge.py"
    environment:
      - TG_API_ID=your_tg_api_id
      - TG_API_HASH=your_tg_api_hash
      - MATRIX_HOMESERVER=https://matrix.org
      - MATRIX_ACCESS_TOKEN=your_matrix_access_token
      - MATRIX_ROOM_ID=!your_room_id:matrix.org
      - TG_CHANNELS=MyChannel,MyOtherChannel
      - MAX_MEDIA_SIZE_MB=80
      - ENABLE_IMAGES=true
      - ENABLE_VIDEOS=true
      - LLAMAGUARD_API_URL=
      - LLAMAGUARD_MODEL_NAME=meta-llama/llama-guard-4-12b
      - LLAMAGUARD_API_KEY=
      - LLAMAGUARD_CHECKS=

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
