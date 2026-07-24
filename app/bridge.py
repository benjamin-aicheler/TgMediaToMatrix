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

def get_env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("true", "1", "yes", "on")

try:
    TG_API_ID = int(get_env_or_raise("TG_API_ID"))
    TG_API_HASH = get_env_or_raise("TG_API_HASH")
    MATRIX_HOMESERVER = get_env_or_raise("MATRIX_HOMESERVER").rstrip('/')
    MATRIX_ACCESS_TOKEN = get_env_or_raise("MATRIX_ACCESS_TOKEN")
    MATRIX_ROOM_ID = get_env_or_raise("MATRIX_ROOM_ID")

    MAX_MEDIA_SIZE_MB = int(os.environ.get("MAX_MEDIA_SIZE_MB", 50))
    MAX_MEDIA_SIZE_BYTES = MAX_MEDIA_SIZE_MB * 1024 * 1024

    ENABLE_IMAGES = get_env_bool("ENABLE_IMAGES", True)
    ENABLE_VIDEOS = get_env_bool("ENABLE_VIDEOS", True)

    LLAMAGUARD_API_URL = os.environ.get("LLAMAGUARD_API_URL", "").strip() or None
    LLAMAGUARD_MODEL_NAME = os.environ.get("LLAMAGUARD_MODEL_NAME", "meta-llama/llama-guard-4-12b").strip()
    LLAMAGUARD_API_KEY = os.environ.get("LLAMAGUARD_API_KEY", "").strip() or None

    try:
        LLAMAGUARD_VIDEO_FRAMES = int(os.environ.get("LLAMAGUARD_VIDEO_FRAMES", "5"))
        if LLAMAGUARD_VIDEO_FRAMES < 1:
            LLAMAGUARD_VIDEO_FRAMES = 1
    except ValueError:
        LLAMAGUARD_VIDEO_FRAMES = 5

    LLAMAGUARD_RANDOM_FRAMES = get_env_bool("LLAMAGUARD_RANDOM_FRAMES", True)

    LLAMAGUARD_CHECKS = set()
    checks_raw = os.environ.get("LLAMAGUARD_CHECKS", "")
    if checks_raw:
        for check in checks_raw.split(','):
            check = check.strip()
            if check:
                LLAMAGUARD_CHECKS.add(check.upper())

    LLAMAGUARD_REQUIRE_CHECKS = set()
    req_checks_raw = os.environ.get("LLAMAGUARD_REQUIRE_CHECKS", "")
    if req_checks_raw:
        for check in req_checks_raw.split(','):
            check = check.strip()
            if check:
                LLAMAGUARD_REQUIRE_CHECKS.add(check.upper())

    # Extract channels, ignoring empty elements, supporting channel_id:topic_id formats
    TG_CHANNELS_RAW = get_env_or_raise("TG_CHANNELS")
    TG_CHANNELS = []
    TG_TOPIC_FILTERS = {}  # maps base_chat_id -> set of topic_ids (ints)
    TG_UNFILTERED_CHANNELS = set()  # tracks channels configured to allow all topics

    import re
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
                chan_username = chan_part.lstrip('@')
                if not re.match(r'^[a-zA-Z0-9_]+$', chan_username):
                    raise ValueError(f"Invalid Telegram channel username '{chan_part}' in '{item}'. Must contain only letters, numbers, and underscores.")
                chan_id = chan_username.lower()

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
                chan_username = item.lstrip('@')
                if not re.match(r'^[a-zA-Z0-9_]+$', chan_username):
                    raise ValueError(f"Invalid Telegram channel username '{item}'. Must contain only letters, numbers, and underscores.")
                chan_id = chan_username.lower()
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


def extract_video_frames(video_bytes: bytes, max_frames: int = 5, use_random: bool = True) -> list:
    """Extracts up to max_frames frames from the video as JPEG bytes in-memory (either evenly spaced or randomly selected)"""
    try:
        import av
        from PIL import Image
        import io
        import random
    except ImportError:
        logging.error("PygAV (av) or Pillow (PIL) package is missing! Please install them to enable video frame safety moderation.")
        return []

    frames_bytes = []
    try:
        container = av.open(io.BytesIO(video_bytes))
        video_stream = container.streams.video[0]
        
        total_frames = video_stream.frames
        if not total_frames:
            # Estimate from duration and frame rate if available
            duration = video_stream.duration
            time_base = video_stream.time_base
            average_rate = video_stream.average_rate
            if duration is not None and time_base is not None and average_rate is not None:
                try:
                    total_seconds = float(duration * time_base)
                    total_frames = int(total_seconds * float(average_rate))
                except Exception:
                    total_frames = 0
        
        # If total_frames is not available, try to decode up to some reasonable limit or read first frames
        if not total_frames or total_frames <= 1:
            for frame in container.decode(video=0):
                img = frame.to_image()
                img_bytes_io = io.BytesIO()
                img.save(img_bytes_io, format="JPEG")
                frames_bytes.append(img_bytes_io.getvalue())
                if len(frames_bytes) >= max_frames:
                    break
            return frames_bytes

        # If we have total_frames, select the target frame indices
        if total_frames <= max_frames:
            target_indices = list(range(total_frames))
        elif use_random:
            target_indices = sorted(random.sample(range(total_frames), max_frames))
        else:
            step = total_frames / max_frames
            target_indices = [int((i + 0.5) * step) for i in range(max_frames)]
            # Keep bounds
            target_indices = [max(0, min(total_frames - 1, idx)) for idx in target_indices]
            # Ensure unique, sorted indices
            target_indices = sorted(list(set(target_indices)))

        target_set = set(target_indices)
        frame_count = 0
        for frame in container.decode(video=0):
            if frame_count in target_set:
                img = frame.to_image()
                img_bytes_io = io.BytesIO()
                img.save(img_bytes_io, format="JPEG")
                frames_bytes.append(img_bytes_io.getvalue())
                if len(frames_bytes) >= len(target_set):
                    break
            frame_count += 1

        # Fallback if no frames were decoded but we expected them
        if not frames_bytes:
            container.seek(0)
            for frame in container.decode(video=0):
                img = frame.to_image()
                img_bytes_io = io.BytesIO()
                img.save(img_bytes_io, format="JPEG")
                frames_bytes.append(img_bytes_io.getvalue())
                break

    except Exception as e:
        logging.error(f"Failed to extract frames from video: {e}")
    return frames_bytes


# Formats a Telegram still image can legitimately be. Anything else is treated as
# undecodable: Pillow's TGA/DDS plugins accept near-arbitrary bytes, so without
# this whitelist a corrupt thumbnail would probe as a bogus multi-thousand-pixel image.
STILL_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}

# Component counts of the encoded blurhash. 4x3 is the common choice for landscape
# previews and what most Matrix clients are tuned for.
BLURHASH_COMPONENTS_X = 4
BLURHASH_COMPONENTS_Y = 3

# The blurhash only encodes the lowest spatial frequencies of the image, so a
# downscaled copy produces a visually identical hash. The encoder is pure Python
# and costs linear time in the pixel count, so this bound is what keeps a
# full-resolution photo at tens of milliseconds instead of tens of seconds.
BLURHASH_MAX_EDGE = 64


def probe_image(image_bytes: bytes) -> tuple[int, int, str] | None:
    """Reads width, height and mime type straight from the encoded bytes.
    Returns None if the bytes are not a decodable still image."""
    try:
        from PIL import Image
    except ImportError:
        logging.error("Pillow (PIL) package is missing! Please install it to enable thumbnail dimension probing.")
        return None

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.format not in STILL_IMAGE_FORMATS:
                logging.debug(f"Ignoring thumbnail of unexpected format {img.format}.")
                return None
            width, height = img.size
            Image.init()
            mime = Image.MIME.get(img.format)
            if not mime or width < 1 or height < 1:
                return None
            return int(width), int(height), mime
    except Exception as e:
        logging.debug(f"Could not probe thumbnail bytes: {e}")
        return None


def generate_image_thumbnail(image_bytes: bytes, max_size: int = 800) -> tuple[bytes, int, int, str] | None:
    """Generates a high-quality downscaled thumbnail from still image bytes.

    Returns (thumbnail_bytes, width, height, mime_type) or None if the bytes
    could not be decoded or processed."""
    try:
        from PIL import Image
    except ImportError:
        logging.error("Pillow (PIL) package is missing! Cannot generate image thumbnail.")
        return None

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.format not in STILL_IMAGE_FORMATS:
                logging.debug(f"Refusing to generate thumbnail for unexpected format {img.format}.")
                return None

            # Get MIME format type
            Image.init()
            mime = Image.MIME.get(img.format)
            if not mime:
                return None

            # Precaution: If image is already smaller than target thumbnail bounds,
            # return the original bytes and dimensions immediately to avoid processing.
            width, height = img.size
            if width <= max_size and height <= max_size:
                if width < 1 or height < 1:
                    return None
                return image_bytes, int(width), int(height), mime

            # Optimize JPEG decoding by using draft mode (scales during load)
            if img.format == "JPEG":
                img.draft(img.mode, (max_size * 2, max_size * 2))

            img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

            # Read size post-resize
            width, height = img.size
            if width < 1 or height < 1:
                return None

            out = io.BytesIO()
            save_format = img.format if img.format in ("PNG", "WEBP") else "JPEG"
            img.save(out, format=save_format, quality=85, optimize=True)
            return out.getvalue(), width, height, mime
    except Exception as e:
        logging.debug(f"Could not generate image thumbnail: {e}")
        return None



def compute_blurhash(image_bytes: bytes) -> str | None:
    """Encodes a blurhash from still image bytes, downscaling them to a thumbnail first.

    Returns None whenever a hash cannot be produced. A blurhash is a rendering
    nicety rather than a safety property, so an absent package or an undecodable
    image degrades to an event without one, never to dropped media."""
    try:
        from PIL import Image
        import blurhash
    except ImportError:
        logging.debug("Pillow (PIL) or blurhash package is missing; sending media without a blurhash.")
        return None

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if img.format not in STILL_IMAGE_FORMATS:
                logging.debug(f"Refusing to blurhash unexpected format {img.format}.")
                return None
            # Decode into an RGB copy while the source is still open, so the copy
            # outlives the context manager that closes the source.
            thumbnail = img.convert("RGB")

        thumbnail.thumbnail((BLURHASH_MAX_EDGE, BLURHASH_MAX_EDGE))

        # blurhash.encode() wants rows of (r, g, b) rather than an Image.
        width, height = thumbnail.size
        raw = thumbnail.tobytes()
        stride = width * 3
        rows = [
            [tuple(raw[x:x + 3]) for x in range(y * stride, (y + 1) * stride, 3)]
            for y in range(height)
        ]
        return blurhash.encode(rows, BLURHASH_COMPONENTS_X, BLURHASH_COMPONENTS_Y)
    except Exception as e:
        logging.debug(f"Could not compute blurhash: {e}")
        return None


async def get_image_safety_classification(image_bytes: bytes, source_chat: str, filename: str, index_label: str = None) -> tuple[bool | None, list[str]]:
    """
    Calls Llama Guard API for a single image/frame.
    Returns (is_safe, violated_categories) where:
    - is_safe is True if 'safe', False if 'unsafe', or None if there was an error.
    - violated_categories is a list of violated Sxx categories.
    """
    label_prefix = f" [{index_label}]" if index_label else ""
    logging.info(f"[{source_chat}]{label_prefix} Calling Llama Guard API ({LLAMAGUARD_MODEL_NAME}) for safety classification of {filename}...")
    
    try:
        import base64
        import urllib.request
        import json

        b64_data = base64.b64encode(image_bytes).decode('utf-8')
        image_url = f"data:image/jpeg;base64,{b64_data}"
        
        prompt_text = """Task: Check if there is unsafe content in 'User' messages in conversations according to our safety policy with the below categories.

<BEGIN UNSAFE CONTENT CATEGORIES>
S1: Violent Crimes.
S2: Non-Violent Crimes.
S3: Sex-Related Crimes.
S4: Child Sexual Exploitation.
S5: Defamation.
S6: Specialized Advice.
S7: Privacy.
S8: Intellectual Property.
S9: Indiscriminate Weapons.
S10: Hate.
S11: Suicide & Self-Harm.
S12: Sexual Content.
S13: Elections.
S14: Code Interpreter Abuse.
<END UNSAFE CONTENT CATEGORIES>

<BEGIN CONVERSATION>
User: <|image|>
<END CONVERSATION>

Provide your safety assessment for ONLY THE LAST User in the above conversation:
- First line must read 'safe' or 'unsafe'.
- If unsafe, a list of violated categories must be on the next line."""

        payload = {
            "model": LLAMAGUARD_MODEL_NAME,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt_text
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_url
                            }
                        }
                    ]
                }
            ],
            "temperature": 0.0
        }
        
        headers = {
            "Content-Type": "application/json"
        }
        if LLAMAGUARD_API_KEY:
            headers["Authorization"] = f"Bearer {LLAMAGUARD_API_KEY}"
            
        api_url_base = LLAMAGUARD_API_URL.rstrip('/')
        if api_url_base.endswith('/chat/completions'):
            api_url_base = api_url_base[:-17].rstrip('/')

        req = urllib.request.Request(
            url=f"{api_url_base}/chat/completions",
            data=json.dumps(payload).encode('utf-8'),
            headers=headers,
            method="POST"
        )
        
        def do_request():
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read()
                
        response_bytes = await asyncio.to_thread(do_request)
        resp_json = json.loads(response_bytes.decode('utf-8'))
        
        if "choices" not in resp_json or not resp_json["choices"]:
            logging.error(f"[{source_chat}]{label_prefix} Unexpected Llama Guard API response structure: {resp_json}")
            return None, []
            
        response_text = resp_json["choices"][0]["message"]["content"].strip()
        logging.info(f"[{source_chat}]{label_prefix} Llama Guard classification result: {response_text}")
        
        lines = response_text.split()
        if not lines:
            logging.error(f"[{source_chat}]{label_prefix} Llama Guard returned empty response.")
            return None, []
            
        status = lines[0].lower()
        if status == "unsafe":
            # Extract violated categories
            violated = []
            for line in lines[1:]:
                for word in line.replace(',', ' ').split():
                    word_clean = word.strip().upper()
                    if word_clean.startswith('S') and word_clean[1:].isdigit():
                        violated.append(word_clean)
            if not violated:
                violated = ["UNSPECIFIED"]
            return False, violated
        elif status == "safe":
            return True, []
        else:
            logging.error(f"[{source_chat}]{label_prefix} Llama Guard returned unexpected status '{status}'.")
            return None, []
            
    except Exception as e:
        logging.error(f"[{source_chat}]{label_prefix} Error during Llama Guard safety check: {e}")
        return None, []


async def check_single_image_safety(image_bytes: bytes, source_chat: str, filename: str, index_label: str = None) -> bool:
    """
    Checks the safety of a single image/frame against the Llama Guard API.
    Returns True if safe/allowed to pass, False if unsafe/blocked.
    """
    is_safe, violated = await get_image_safety_classification(image_bytes, source_chat, filename, index_label)
    if is_safe is None:
        return False  # Block on API error / fail-closed
        
    label_prefix = f" [{index_label}]" if index_label else ""
    
    if not is_safe:
        # 1. Block-list check (LLAMAGUARD_CHECKS)
        if LLAMAGUARD_CHECKS:
            overlap = [c for c in violated if c in LLAMAGUARD_CHECKS]
            if overlap:
                logging.warning(f"[{source_chat}]{label_prefix} BLOCKING media {filename}! Violates configured Llama Guard categories: {overlap}")
                return False

        # 2. Required-list check (LLAMAGUARD_REQUIRE_CHECKS)
        if LLAMAGUARD_REQUIRE_CHECKS:
            required_overlap = [c for c in violated if c in LLAMAGUARD_REQUIRE_CHECKS]
            if not required_overlap:
                logging.warning(f"[{source_chat}]{label_prefix} BLOCKING media {filename}! Classified as unsafe ({violated}), but does not match any required categories: {LLAMAGUARD_REQUIRE_CHECKS}")
                return False
            else:
                logging.info(f"[{source_chat}]{label_prefix} Media matches required safety check: {required_overlap}. Passing.")
                return True

        # 3. Default behavior if no required-list and no block-list overlap
        if LLAMAGUARD_CHECKS:
            logging.info(f"[{source_chat}]{label_prefix} Media classified as unsafe ({violated}), but none are in configured important checks ({LLAMAGUARD_CHECKS}). Passing.")
            return True
        else:
            logging.warning(f"[{source_chat}]{label_prefix} BLOCKING media {filename}! Violates Llama Guard categories: {violated}")
            return False
            
    else:  # safe
        if LLAMAGUARD_REQUIRE_CHECKS:
            logging.warning(f"[{source_chat}]{label_prefix} BLOCKING media {filename}! Classified as safe, but does not match any required unsafe categories: {LLAMAGUARD_REQUIRE_CHECKS}")
            return False
        return True


async def check_media_safety(media_bytes: bytes, mime_type: str, source_chat: str, filename: str) -> bool:
    """
    Checks the safety of the media using Meta Llama Guard via an OpenAI-compatible Vision API.
    Returns True if the media is safe/allowed, False if it is unsafe/blocked.
    """
    if not LLAMAGUARD_API_URL:
        return True # Disabled, pass by default

    image_list = []
    if mime_type.startswith("image/"):
        image_list = [media_bytes]
    elif mime_type.startswith("video/"):
        logging.info(f"[{source_chat}] Extracting up to {LLAMAGUARD_VIDEO_FRAMES} frames from video {filename} for Llama Guard safety check...")
        image_list = await asyncio.to_thread(extract_video_frames, media_bytes, LLAMAGUARD_VIDEO_FRAMES, LLAMAGUARD_RANDOM_FRAMES)
        if not image_list:
            logging.error(f"[{source_chat}] Could not extract any video frames for safety check. Blocking media due to check failure.")
            return False

    if not image_list:
        logging.error(f"[{source_chat}] No image bytes available for safety check. Blocking media due to check failure.")
        return False

    if len(image_list) == 1:
        return await check_single_image_safety(image_list[0], source_chat, filename)

    # Spawn parallel safety checks for all extracted video frames
    tasks = []
    for i, img_bytes in enumerate(image_list):
        label = f"frame {i+1}/{len(image_list)}"
        tasks.append(get_image_safety_classification(img_bytes, source_chat, filename, label))

    results = await asyncio.gather(*tasks)
    
    # Analyze aggregated results for video frames
    # results is a list of tuple[bool | None, list[str]]
    
    # 1. Check for any API/processing errors (fail-closed)
    for is_safe, _ in results:
        if is_safe is None:
            logging.error(f"[{source_chat}] One or more video frames failed classification. Blocking video due to fail-closed safety policy.")
            return False

    # 2. Block-list check (LLAMAGUARD_CHECKS)
    # If ANY frame contains a category in LLAMAGUARD_CHECKS, we block the entire video.
    if LLAMAGUARD_CHECKS:
        for i, (is_safe, violated) in enumerate(results):
            if not is_safe:
                overlap = [c for c in violated if c in LLAMAGUARD_CHECKS]
                if overlap:
                    logging.warning(f"[{source_chat}] BLOCKING video {filename}! Frame {i+1} violates configured Llama Guard categories: {overlap}")
                    return False

    # 3. Required-list check (LLAMAGUARD_REQUIRE_CHECKS)
    # If whitelisting is active:
    # - At least ONE frame must match one of the required categories.
    # - Safe frames do not block the video in this context (since it's a video, and we only need the video to contain the required content somewhere).
    if LLAMAGUARD_REQUIRE_CHECKS:
        matched_required = False
        all_violated_matched = []
        for i, (is_safe, violated) in enumerate(results):
            if not is_safe:
                required_overlap = [c for c in violated if c in LLAMAGUARD_REQUIRE_CHECKS]
                if required_overlap:
                    matched_required = True
                    all_violated_matched.extend(required_overlap)
                    
        if not matched_required:
            logging.warning(f"[{source_chat}] BLOCKING video {filename}! No frames matched any required categories: {LLAMAGUARD_REQUIRE_CHECKS}")
            return False
        else:
            logging.info(f"[{source_chat}] Video matches required safety check (found required categories {list(set(all_violated_matched))}). Passing.")
            return True

    # 4. Default check if no required-list is configured
    # Under the default policy (or when only LLAMAGUARD_CHECKS is set), a video is allowed if no frames violate the block-list.
    # If LLAMAGUARD_CHECKS is empty and LLAMAGUARD_REQUIRE_CHECKS is empty:
    # - Any unsafe frame blocks the video by default.
    if not LLAMAGUARD_CHECKS:
        # LLAMAGUARD_CHECKS is empty, and LLAMAGUARD_REQUIRE_CHECKS is empty:
        # This is the "block all unsafe content" mode. Any unsafe frame blocks the video.
        for i, (is_safe, violated) in enumerate(results):
            if not is_safe:
                logging.warning(f"[{source_chat}] BLOCKING video {filename}! Frame {i+1} violates Llama Guard categories: {violated}")
                return False

    logging.info(f"[{source_chat}] Video {filename} successfully passed safety checks.")
    return True


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

    is_image = mime_type.startswith("image/")
    is_video = mime_type.startswith("video/")

    if not (is_image or is_video):
        return

    if is_image and not ENABLE_IMAGES:
        logging.info(f"[{source_chat}] Skipping image media because ENABLE_IMAGES is false.")
        return

    if is_video and not ENABLE_VIDEOS:
        logging.info(f"[{source_chat}] Skipping video media because ENABLE_VIDEOS is false.")
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

    logging.info(f"[{source_chat}] Download complete ({len(media_bytes)} bytes).")

    # Llama Guard Safety Check
    if LLAMAGUARD_API_URL:
        is_safe = await check_media_safety(media_bytes, mime_type, source_chat, filename)
        if not is_safe:
            return

    logging.info(f"[{source_chat}] Uploading to Matrix homeserver...")
    
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
            "mimetype": mime_type,
            "filename": filename
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
        blurhash_bytes = None
        if msg_type in ("m.video", "m.image"):
            try:
                thumb_bytes = None
                thumb_w = None
                thumb_h = None
                thumb_mime = None

                if msg_type == "m.image":
                    # Generate thumbnail locally in-memory to save bandwidth and get optimal dimensions
                    res = await asyncio.to_thread(generate_image_thumbnail, media_bytes)
                    if res:
                        thumb_bytes, thumb_w, thumb_h, thumb_mime = res
                else:
                    # For videos, download the high-resolution thumbnail from Telegram
                    t_bytes = await message.download_media(thumb=-1, file=bytes)
                    if t_bytes:
                        probed = await asyncio.to_thread(probe_image, t_bytes)
                        if probed:
                            thumb_bytes = t_bytes
                            thumb_w, thumb_h, thumb_mime = probed

                if thumb_bytes:
                    blurhash_bytes = thumb_bytes
                    thumb_ext = thumb_mime.split('/', 1)[1]
                    thumb_resp, _ = await matrix_client.upload(
                        io.BytesIO(thumb_bytes),
                        content_type=thumb_mime,
                        filename=f"thumbnail.{thumb_ext}",
                        filesize=len(thumb_bytes)
                    )
                    if isinstance(thumb_resp, UploadResponse):
                        info_dict["thumbnail_url"] = thumb_resp.content_uri
                        info_dict["thumbnail_info"] = {
                            "mimetype": thumb_mime,
                            "size": len(thumb_bytes),
                            "w": thumb_w,
                            "h": thumb_h
                        }
            except Exception as thumb_err:
                logging.debug(f"[{source_chat}] Thumbnail skipped: {thumb_err}")

        # The blurhash is always encoded from a thumbnail. Telegram did not always
        # give us a usable one, so for images fall back to the full-size bytes and
        # let compute_blurhash() downscale them itself. A video without a decodable
        # thumbnail has no still frame Pillow can read, and gets no blurhash.
        if blurhash_bytes is None and is_image:
            blurhash_bytes = media_bytes

        blurhash_str = None
        if blurhash_bytes is not None:
            blurhash_str = await asyncio.to_thread(compute_blurhash, blurhash_bytes)

        # Build user-friendly metadata info (file size and dimensions if available)
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

        body_text = f"Media from '{channel_name}'\n{meta_line}"
        formatted_body_text = (
            f"Media from '<strong>{channel_name}</strong>'<br/>"
            f"<font color=\"#888888\"><small>{meta_line}</small></font>"
        )

        matrix_content = {
            "msgtype": msg_type,
            "body": body_text,
            "url": content_uri,
            "filename": filename,
            "format": "org.matrix.custom.html",
            "formatted_body": formatted_body_text
        }
        if blurhash_str:
            info_dict["xyz.amorgan.blurhash"] = blurhash_str

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
    chat_username_lower = chat_username.lower() if chat_username else None

    allowed_topics = None
    # If the channel itself was configured without any topic filter, allow all topics.
    if chat_id in TG_UNFILTERED_CHANNELS or (chat_username_lower and chat_username_lower in TG_UNFILTERED_CHANNELS):
        pass
    else:
        if chat_id in TG_TOPIC_FILTERS:
            allowed_topics = TG_TOPIC_FILTERS[chat_id]
        elif chat_username_lower and chat_username_lower in TG_TOPIC_FILTERS:
            allowed_topics = TG_TOPIC_FILTERS[chat_username_lower]

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
    logging.info(f"Images enabled: {ENABLE_IMAGES}")
    logging.info(f"Videos enabled: {ENABLE_VIDEOS}")
    logging.info(f"Llama Guard Moderation: {'Enabled' if LLAMAGUARD_API_URL else 'Disabled'}")
    if LLAMAGUARD_API_URL:
        logging.info(f"Llama Guard Model: {LLAMAGUARD_MODEL_NAME}")
        logging.info(f"Llama Guard Video Frames: {LLAMAGUARD_VIDEO_FRAMES} (Random Selection: {LLAMAGUARD_RANDOM_FRAMES})")
        logging.info(f"Llama Guard Checks Filter (Block List): {list(LLAMAGUARD_CHECKS) if LLAMAGUARD_CHECKS else 'ALL categories'}")
        logging.info(f"Llama Guard Required Checks (Whitelist): {list(LLAMAGUARD_REQUIRE_CHECKS) if LLAMAGUARD_REQUIRE_CHECKS else 'None (safe content allowed)'}")
    try:
        await tg_client.run_until_disconnected()
    finally:
        await matrix_client.close()

if __name__ == '__main__':
    asyncio.run(main())
