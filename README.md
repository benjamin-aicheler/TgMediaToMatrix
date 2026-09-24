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
  - Generates subtle inline captions (`formatted_body`) showing media dimensions (e.g. `1920x1080`) and human-readable file sizes (`MB`/`KB`) without cluttering the chat view.
  - Attaches the source Telegram channel and topic name to a custom event property (`content.source`), allowing event inspection without cluttering chat messages.
- **Dynamic In-Memory Image Thumbnailing**: Generates high-quality image thumbnails directly in-memory using Pillow (up to 800×800 bounding box with `LANCZOS` resampling), bypassing redundant network downloads from Telegram. If an image is already smaller than the target bounds, its original bytes and dimensions are returned immediately to avoid unnecessary re-encoding.
- **Full Metadata Extraction & Video Thumbnailing**: Extracts exact width, height, and duration dimensions from video documents, downloading high-resolution video thumbnails directly from Telegram and probing their parameters with `Pillow`.
- **Automatic Blurhash Previews (MSC2448)**: Generates and attaches Blurhash placeholders (`xyz.amorgan.blurhash` inside the `info` metadata) to image and video events in-memory, allowing compatible Matrix clients to show beautiful, low-fidelity blurred placeholders while the actual media is loading.
- **Caption Privacy Limit**: Discards original Telegram captions entirely—forwarding only the channel name/topic display name prefix and the media file name to avoid clutter.
- **Multi-Room Forwarding (Single Upload)**: Forward media to multiple Matrix rooms without duplicate network traffic—media and thumbnails are uploaded to the homeserver only once and dispatched as lightweight events across all configured rooms.
- **Duplicate Media Safety (Two-Tier Deduplication)**: Prevents duplicate media forwarding when the same photo or video is posted across multiple channels or reposted in the same channel. Features a two-tier in-memory cache:
  - *Pre-Download Check*: Matches Telegram internal media IDs (`photo.id` / `document.id`) before download to conserve network bandwidth.
  - *Post-Download Check*: Calculates an exact SHA-256 hash of downloaded bytes before Llama Guard analysis and Matrix uploading to catch separate manual uploads of the identical file.
  - *Time & Memory Bounds*: Uses a bounded LRU/FIFO cache with automatic time-based expiry (default 15-minute window).
- **Automatic File Size Limiting & Oversized Video Handling**:
  - Rejects media smaller than configured minimum thresholds (`MIN_IMAGE_SIZE_KB` / `MIN_VIDEO_SIZE_KB`).
  - When a video exceeds `MAX_MEDIA_SIZE_MB`, the bridge can automatically process it instead of dropping it:
    - **Lossless Stream Splitting (`split`, default)**: Slices the video at keyframes using FFmpeg stream copy (`-c copy`) in RAM (`/dev/shm`). Operates in sub-seconds with **~0% CPU load and 100% original quality**, making it perfect for low-power devices like the **Raspberry Pi 4**.
    - **Fast Low-Resource Compression (`compress`)**: Re-encodes videos down to 720p with `libx264` fast presets and audio stream passthrough.
    - **Ceiling Protection**: Ignores videos larger than `OVERSIZED_VIDEO_MAX_INPUT_MB` (e.g. 250 MB) before downloading to prevent OOM.
- **Dynamic Interactive Chat Commands**: When `ADMIN_MATRIX_USER_ID` is set, the authorized admin user can send `!tmmb` commands in Matrix to query status or dynamically toggle image and video bridging on the fly.

---

## Prerequisites

Before starting, you need:
1. **Telegram API Credentials**: An `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org/).
2. **Matrix User Account**: A dedicated user account on your Matrix homeserver.
3. **Matrix Access Token**: An access token for that user (can be fetched from client settings such as Element under *All Settings* -> *Help & About* -> *Advanced* -> *Access Token*).
4. **Matrix Room IDs**: One or more internal room IDs where the media should be posted (e.g., `!abcde12345:matrix.org, !fghij67890:matrix.org`).

---

## Configuration (`docker-compose.yml`)

The bridge is configured via environment variables in the `docker-compose.yml` file:

| Environment Variable | Description | Example / Default |
| :--- | :--- | :--- |
| `TG_API_ID` | Telegram API ID | `123456` |
| `TG_API_HASH` | Telegram API Hash | `abcdef0123456789abcdef0123456789` |
| `MATRIX_HOMESERVER` | Your Matrix Homeserver URL | `https://matrix.org` |
| `MATRIX_ACCESS_TOKEN` | Access token for the Matrix account | `syt_dW...` |
| `MATRIX_ROOM_IDS` | Comma-separated internal room IDs of destination rooms | `!abcde12345:matrix.org, !fghij67890:matrix.org` |
| `MATRIX_ROOM_ID` | *(Deprecated)* Single destination room ID. Ignored if `MATRIX_ROOM_IDS` is set. | `!abcde12345:matrix.org` |
| `ADMIN_MATRIX_USER_ID` | Authorized Matrix user ID for dynamic chat commands | `@admin:matrix.org` (Default: `None` / Disabled) |
| `ALLOW_NON_ADMIN_STOP` | Set to `true` to allow non-admin users to issue `stop` and `disable` commands | `false` (Default: `false`) |
| `TG_CHANNELS` | Comma-separated list of target channels and topic filters | `MyChannel, -1001234567890:42, @MyChannel` |
| `MAX_MEDIA_SIZE_MB` | Maximum size in MB to download and bridge directly | `80` (Default: `50`) |
| `OVERSIZED_VIDEO_ACTION` | Action for videos exceeding `MAX_MEDIA_SIZE_MB`: `split` (lossless stream copy), `compress` (transcode), or `skip` | `split` (Default: `split`) |
| `OVERSIZED_VIDEO_MAX_INPUT_MB` | Maximum file size in MB to download for oversized video processing | `250` (Default: `250`) |
| `OVERSIZED_VIDEO_FALLBACK_COMPRESS` | Fallback to compression if lossless splitting cannot find keyframes to split below limit | `false` (Default: `false`) |
| `VIDEO_COMPRESSION_MAX_HEIGHT` | Maximum vertical resolution when compressing oversized videos | `720` (Default: `720`) |
| `VIDEO_COMPRESSION_PRESET` | FFmpeg x264 preset (`ultrafast`, `veryfast`, `fast`, etc.) | `veryfast` (Default: `veryfast`) |
| `VIDEO_COMPRESSION_CRF` | Constant Rate Factor for video re-encoding (26–30 recommended) | `28` (Default: `28`) |
| `VIDEO_COMPRESSION_THREADS` | Number of CPU threads allocated to FFmpeg | `2` (Default: `2`) |
| `MIN_IMAGE_SIZE_KB` | Minimum file size in KB for images to be forwarded | `100` (Default: `0` / Disabled) |
| `MIN_VIDEO_SIZE_KB` | Minimum file size in KB for videos to be forwarded (accepts `MIN_VIDEO_SIZE_MB`) | `1024` (Default: `0` / Disabled) |
| `ENABLE_IMAGES` | Set to `false` to disable bridging of images | `true` (Default: `true`) |
| `ENABLE_VIDEOS` | Set to `false` to disable bridging of videos | `true` (Default: `true`) |
| `DEDUPLICATION_ENABLED` | Enable prevention of duplicate media forwarding | `true` (Default: `true`) |
| `DEDUPLICATION_TTL_MINUTES` | Expiration window in minutes for duplicate suppression | `15` (Default: `15`) |
| `DEDUPLICATION_CACHE_SIZE` | Maximum number of recently processed media hashes/IDs to remember | `2000` (Default: `2000`) |
| `LLAMAGUARD_API_URL` | Base URL of an OpenAI-compatible Vision API for Llama Guard checks | `http://192.168.1.100:8000/v1` (Default: `None`/Disabled) |
| `LLAMAGUARD_MODEL_NAME` | Model name to request for safety moderation | `meta-llama/llama-guard-4-12b` |
| `LLAMAGUARD_API_KEY` | API authentication key for Llama Guard endpoint if required | `your-api-key` (Default: `None`) |
| `LLAMAGUARD_CHECKS` | Comma-separated list of safety categories to block. If empty, blocks on any safety violation. | `S1,S2,S3,S4` (Default: empty / block on any) |
| `LLAMAGUARD_REQUIRE_CHECKS` | Comma-separated list of required safety categories (whitelist mode). If set, safe content and any content not matching these categories is blocked. | `S12` (Default: empty / disable whitelist mode) |
| `LLAMAGUARD_VIDEO_FRAMES` | Number of frames to extract and check concurrently from each video | `5` (Default: `5`) |
| `LLAMAGUARD_RANDOM_FRAMES` | Extract frames randomly throughout the video duration instead of evenly spaced. Set to `false` for evenly spaced selection. | `true` (Default: `true`) |

---

## Dynamic Chat Commands (`!tmmb`)

When `ADMIN_MATRIX_USER_ID` is configured, the bridge listens for real-time control commands issued by that specific Matrix user ID in the Matrix room:

| Command | Description |
| :--- | :--- |
| `!tmmb start` | Dynamically enable both image and video bridging simultaneously |
| `!tmmb stop` | Dynamically disable both image and video bridging simultaneously |
| `!tmmb image enable` / `disable` | Dynamically turn image bridging on or off in memory until container restart or next command |
| `!tmmb video enable` / `disable` | Dynamically turn video bridging on or off in memory until container restart or next command |
| `!tmmb status` | View the current runtime enablement state of image and video bridging |
| `!tmmb help` | Show command usage and instructions |


### Specifying Channels & Topics in `TG_CHANNELS`
The `TG_CHANNELS` environment variable accepts a comma-separated list of several formats:
- **Public Username**: `MyChannel` or `@MyChannel` (any casing; `@` is stripped automatically).
- **Private Channel / Group ID**: `-1001234567890`.
- **Forum Topic Filter**: `channel_id:topic_id` or `username:topic_id` (e.g. `-1001234567890:42`). This configures the bridge to only forward media posted inside that specific topic ID (subchannel thread) of the forum.

---

## Two-Tier Media Deduplication

To prevent duplicate media messages when identical content is posted across multiple monitored channels or re-posted in the same channel, the bridge provides an integrated, in-memory two-tier deduplication engine:

1. **Tier 1 (Pre-Download / Telegram Media ID)**:
   - When a new Telegram message arrives, the bridge immediately inspects Telegram's internal media identifier (`message.photo.id` or `message.document.id`).
   - If this Telegram media ID was forwarded within the deduplication window (`DEDUPLICATION_TTL_MINUTES`, default: `15` minutes), the message is skipped **immediately without downloading any bytes**, conserving server network bandwidth.
2. **Tier 2 (Post-Download / SHA-256 Content Hash)**:
   - If the Telegram media ID is novel (such as when an image or video is manually uploaded separately to different channels instead of forwarded), the media bytes are downloaded.
   - Immediately after download, the bridge computes an exact `SHA-256` content hash of the raw bytes in a background thread.
   - If the content hash was already forwarded within the deduplication window, the media is discarded **before** running Llama Guard safety moderation, thumbnail generation, or Matrix upload.
3. **Album Integrity**:
   - For Telegram albums (grouped media), deduplication runs per item. If an album contains a mix of previously seen media and new media, the novel items are forwarded cleanly while duplicates are suppressed.
4. **Bounded Memory**:
   - Deduplication is tracked in an LRU/FIFO `OrderedDict` limited to `DEDUPLICATION_CACHE_SIZE` (default: `2000` items) with automatic time-based purging, maintaining a negligible memory footprint (< 1 MB) over indefinite uptimes.

---

## Oversized Video Processing (Lossless Splitting & Compression)

When Telegram channels post videos that exceed your configured `MAX_MEDIA_SIZE_MB` (e.g. 80MB), the bridge can automatically handle them instead of discarding them.

### Modes (`OVERSIZED_VIDEO_ACTION`)

1. **`split` (Default & Strongly Recommended for Raspberry Pi 4)**:
   - **Zero Re-Encoding**: Uses FFmpeg stream copying (`-c copy`) to slice the video at keyframes into sequential parts (`[Part 1/2]`, `[Part 2/2]`).
   - **Sub-Second Performance**: Takes only ~0.1s – 0.4s to execute, even on low-power ARM CPUs like the **Raspberry Pi 4 Model B**.
   - **Zero Quality Loss**: The video and audio bitstreams remain 100% untouched and identical to the original upload.
   - **Zero CPU Heat**: CPU usage remains near 0%, avoiding thermal throttling.
   - **Matrix Presentation**: Each part is posted into the Matrix room with updated captions and individual metadata (e.g. `Part 1/2 • 1920x1080 58.4 MB`), with thumbnails and Blurhashes attached.

2. **`compress`**:
   - Re-encodes oversized videos down to a maximum resolution (`VIDEO_COMPRESSION_MAX_HEIGHT`, default `720p`) using `libx264` with fast presets (`VIDEO_COMPRESSION_PRESET`, default `veryfast`) and audio stream copying (`-c:a copy`).
   - Preserves a single video file, but consumes CPU cycles during encoding.

3. **`skip`**:
   - Legacy behavior: logs a warning and drops any video exceeding `MAX_MEDIA_SIZE_MB`.

### In-Memory RAM Disk (`/dev/shm`)
To prevent SD card wear and ensure high-speed operations on single-board computers like the Raspberry Pi, intermediate temporary video files are processed directly in shared memory (`/dev/shm`, configured with `shm_size: 1g` in `docker-compose.yml`) and immediately purged after upload.

### Pre-Download Ceiling Protection
To prevent downloading massive multi-gigabyte files that could exhaust RAM, the bridge checks the video's size from Telegram metadata *before* starting the download. Any video exceeding `OVERSIZED_VIDEO_MAX_INPUT_MB` (default `250` MB) is skipped immediately.

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
      - DEDUPLICATION_ENABLED=true
      - DEDUPLICATION_TTL_MINUTES=15
      - DEDUPLICATION_CACHE_SIZE=2000
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
