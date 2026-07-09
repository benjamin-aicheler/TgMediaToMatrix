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
- **Full Metadata Extraction & Thumbnail Probing**: Extracts exact width, height, and duration dimensions from video documents. Uses `Pillow` to robustly probe thumbnail dimensions and formats directly from downloaded image bytes, ensuring Matrix metadata matches the actual media file precisely.
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
| `LLAMAGUARD_REQUIRE_CHECKS` | Comma-separated list of required safety categories (whitelist mode). If set, safe content and any content not matching these categories is blocked. | `S12` (Default: empty / disable whitelist mode) |
| `LLAMAGUARD_VIDEO_FRAMES` | Number of frames to extract and check concurrently from each video | `5` (Default: `5`) |
| `LLAMAGUARD_RANDOM_FRAMES` | Extract frames randomly throughout the video duration instead of evenly spaced. Set to `false` for evenly spaced selection. | `true` (Default: `true`) |


### Specifying Channels & Topics in `TG_CHANNELS`
The `TG_CHANNELS` environment variable accepts a comma-separated list of several formats:
- **Public Username**: `MyChannel` or `@MyChannel` (any casing; `@` is stripped automatically).
- **Private Channel / Group ID**: `-1001234567890`.
- **Forum Topic Filter**: `channel_id:topic_id` or `username:topic_id` (e.g. `-1001234567890:42`). This configures the bridge to only forward media posted inside that specific topic ID (subchannel thread) of the forum.

---

## Content Moderation with Meta Llama Guard 4 12B

The bridge supports real-time, automated image and video content moderation using Meta Llama Guard 4 12B (or any OpenAI-compatible API endpoint hosting a compatible model).

### How it Works
1. **In-Memory Frame Extraction**: When a video is downloaded, the bridge extracts multiple frames (configurable via `LLAMAGUARD_VIDEO_FRAMES`, defaults to `5`) in-memory using `PyAV` (`av`) and `Pillow` (`PIL`). By default, frames are selected randomly from across the video duration for maximum safety coverage (or evenly spaced if `LLAMAGUARD_RANDOM_FRAMES` is set to `false`). No media files are ever written to disk.
2. **Concurrent Safety Queries**: The bridge encodes each extracted frame into base64 and schedules Llama Guard safety API calls for all of them concurrently (in parallel) using Python's `asyncio.gather` for maximum throughput and near-instant audit times. If any frame fails to check due to an API error, the video is blocked (fail-closed). When block-lists or required-lists (whitelists) are configured, results are aggregated dynamically: if *any single frame* contains a category on your block-list (`LLAMAGUARD_CHECKS`), the video is strictly blocked. If whitelist mode is active (`LLAMAGUARD_REQUIRE_CHECKS`), at least *one frame* must match the required categories (and no frames must violate the block-list) for the video to be forwarded.
3. **Classification and Categories**: Llama Guard typically outputs safety ratings (e.g., `safe` or `unsafe` followed by the violated categories like `S1`, `S2`, etc.).
4. **Fail-Closed Design**: If the Llama Guard endpoint is unreachable, is misconfigured, or lacks the necessary library bindings (`av` or `Pillow`), the bridge logs the error and blocks the media from being forwarded. This ensures that no unchecked or unmoderated media passes through when content moderation is enabled.

### Configuring Safety Checks
The `LLAMAGUARD_CHECKS` environment variable allows you to configure which specific `Sxx` guidelines are strictly enforced:
- **Block All Violations (Default)**: If `LLAMAGUARD_CHECKS` is left empty or omitted, *any* safety violation returned by Llama Guard will block the media from being forwarded.
- **Selective Enforcement**: If you only care about specific categories, list them in a comma-separated format (e.g., `S1,S2,S3`). If Llama Guard flags media with an `S5` violation but your config only lists `S1,S2,S3`, the bridge will log the safety warning but still forward the media.

### Whitelist Mode (Required Categories)
If you want to *only* forward media that falls under a specific safety category (e.g. you are bridging an adult/sexual content channel and want to block generic safe content, while strictly filtering out illegal categories like Child Exploitation `S4`), you can use `LLAMAGUARD_REQUIRE_CHECKS`:
- **`LLAMAGUARD_REQUIRE_CHECKS`**: Set this to the categories that media *must* be classified under to be forwarded. If configured, any completely `safe` content or content violating other unlisted categories is blocked.
- **Co-existing with Block List**: You can combine this with `LLAMAGUARD_CHECKS`. If an item matches a required category (like `S12` Sexual Content) but *also* contains a blocked category (like `S4` Child Exploitation), it will be strictly **blocked**.

**Example**: Allow only Adult Content (`S12`), but strictly block Child Exploitation (`S4`) and normal safe content:
```yaml
LLAMAGUARD_REQUIRE_CHECKS: "S12"
LLAMAGUARD_CHECKS: "S4"
```

### Available Safety Categories

Below is the standard taxonomy of categories defined by the Meta Llama Guard 3 & 4 models that you can selectively filter:

| Category | Name | Description |
| :--- | :--- | :--- |
| `S1` | Violent Crimes | Content that encourages, depicts, or facilitates violent acts, including physical violence, murder, assault, kidnapping, or robbery. |
| `S2` | Non-Violent Crimes | Content that encourages, depicts, or facilitates non-violent crimes, such as theft, burglary, fraud, drug distribution, smuggling, or vandalism. |
| `S3` | Sex-Related Crimes | Content depicting or promoting sexual assault, sexual violence, sexual exploitation, or human trafficking. |
| `S4` | Child Sexual Exploitation | Content promoting or depicting child sexual abuse material (CSAM), grooming, sexual exploitation, or abuse of minors. |
| `S5` | Defamation | Content containing false statements of fact targeted at harming the reputation of individuals or organizations. |
| `S6` | Specialized Advice | Content offering unlicensed, dangerous, or illegal advice in highly regulated professional fields (e.g., medical, financial, or legal advice). |
| `S7` | Privacy | Content that shares personally identifiable information (PII) without consent (doxxing), such as addresses, phone numbers, or private documents. |
| `S8` | Intellectual Property | Content that promotes or facilitates copyright, trademark, or patent infringement. |
| `S9` | Indiscriminate Weapons | Content promoting or describing the creation, acquisition, or deployment of chemical, biological, nuclear, or other weapons of mass destruction. |
| `S10` | Hate | Content promoting hatred, discrimination, disparagement, or violence against individuals or groups based on protected characteristics. |
| `S11` | Suicide & Self-Harm | Content depicting, encouraging, or instructing individuals to commit suicide or engage in self-harm. |
| `S12` | Sexual Content | Content depicting sexually explicit material, pornography, nudity, or explicit sexual acts. |
| `S13` | Elections | Content that aims to interfere with election processes, spread voter suppression, or promote fraudulent voter registration. |
| `S14` | Code Interpreter Abuse | Content attempting to exploit, breach, or abuse runtime execution or code interpreter sandboxes. |

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
    image: python:3.14-slim
    container_name: tg_matrix_media_bridge
    restart: unless-stopped
    volumes:
      - ./app:/app
      - ./session_data:/app/session
    working_dir: /app
    command: sh -c "pip install --no-cache-dir telethon matrix-nio av Pillow blurhash && python bridge.py"
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
      - LLAMAGUARD_REQUIRE_CHECKS=

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

---

## AI Disclosure

This project was built and designed in partnership with **Antigravity**, a powerful agentic AI coding assistant designed by Google DeepMind. All architectural choices, code styles, and advanced features (such as Llama Guard whitelisting/filtering and in-memory frame extraction) were developed with AI pair programming.
