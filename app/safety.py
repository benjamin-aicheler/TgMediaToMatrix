"""Llama Guard classification and the verdict aggregation for images and videos."""

import asyncio
import logging
from enum import Enum

import config
import media

logger = logging.getLogger("tgmabr")


class SafetyVerdict(Enum):
    """Outcome of a safety classification.

    BLOCKED and UNAVAILABLE both keep the media off Matrix, but for different reasons:
    BLOCKED is a deliberate verdict, so the content is never retried; UNAVAILABLE means the
    classifier produced no verdict at all - fail-closed for this attempt, but a repost may
    well succeed later.
    """

    SAFE = "safe"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


async def get_image_safety_classification(image_bytes: bytes, source_chat: str, filename: str, index_label: str | None = None, mime_type: str = "image/jpeg") -> tuple[bool | None, list[str]]:
    """
    Calls Llama Guard API for a single image/frame.
    Returns (is_safe, violated_categories) where:
    - is_safe is True if 'safe', False if 'unsafe', or None if there was an error.
    - violated_categories is a list of violated Sxx categories.
    """
    label_prefix = f" [{index_label}]" if index_label else ""
    logger.info(f"[{source_chat}]{label_prefix} Calling Llama Guard API ({config.LLAMAGUARD_MODEL_NAME}) for safety classification of {filename}...")
    
    try:
        import base64
        import json
        import urllib.request

        b64_data = base64.b64encode(image_bytes).decode('utf-8')
        # The data URL must describe the actual encoding: announcing PNG/WebP bytes as
        # image/jpeg makes the vision model decode garbage or reject the request.
        safe_mime = mime_type if mime_type and mime_type.startswith("image/") else "image/jpeg"
        image_url = f"data:{safe_mime};base64,{b64_data}"
        
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
            "model": config.LLAMAGUARD_MODEL_NAME,
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
        if config.LLAMAGUARD_API_KEY:
            headers["Authorization"] = f"Bearer {config.LLAMAGUARD_API_KEY}"
            
        api_url_base = config.LLAMAGUARD_API_URL.rstrip('/')
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
            logger.error(f"[{source_chat}]{label_prefix} Unexpected Llama Guard API response structure: {resp_json}")
            return None, []
            
        response_text = resp_json["choices"][0]["message"]["content"].strip()
        logger.info(f"[{source_chat}]{label_prefix} Llama Guard classification result: {response_text}")
        
        lines = response_text.split()
        if not lines:
            logger.error(f"[{source_chat}]{label_prefix} Llama Guard returned empty response.")
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
            logger.error(f"[{source_chat}]{label_prefix} Llama Guard returned unexpected status '{status}'.")
            return None, []
            
    except Exception as e:  # noqa: BLE001 - any classifier failure must fail closed
        logger.error(f"[{source_chat}]{label_prefix} Error during Llama Guard safety check: {e}")
        return None, []


async def check_single_image_safety(image_bytes: bytes, source_chat: str, filename: str, index_label: str | None = None, mime_type: str = "image/jpeg") -> SafetyVerdict:
    """
    Checks the safety of a single image/frame against the Llama Guard API.
    Returns SAFE if the media may pass, BLOCKED if policy rejects it, UNAVAILABLE if the
    classifier could not be reached (fail-closed, retryable).
    """
    is_safe, violated = await get_image_safety_classification(image_bytes, source_chat, filename, index_label, mime_type)
    if is_safe is None:
        return SafetyVerdict.UNAVAILABLE
        
    label_prefix = f" [{index_label}]" if index_label else ""
    
    if not is_safe:
        # 1. Block-list check (LLAMAGUARD_CHECKS)
        if config.LLAMAGUARD_CHECKS:
            overlap = [c for c in violated if c in config.LLAMAGUARD_CHECKS]
            if overlap:
                logger.warning(f"[{source_chat}]{label_prefix} BLOCKING media {filename}! Violates configured Llama Guard categories: {overlap}")
                return SafetyVerdict.BLOCKED

        # 2. Required-list check (LLAMAGUARD_REQUIRE_CHECKS)
        if config.LLAMAGUARD_REQUIRE_CHECKS:
            required_overlap = [c for c in violated if c in config.LLAMAGUARD_REQUIRE_CHECKS]
            if not required_overlap:
                logger.warning(f"[{source_chat}]{label_prefix} BLOCKING media {filename}! Classified as unsafe ({violated}), but does not match any required categories: {config.LLAMAGUARD_REQUIRE_CHECKS}")
                return SafetyVerdict.BLOCKED
            else:
                logger.info(f"[{source_chat}]{label_prefix} Media matches required safety check: {required_overlap}. Passing.")
                return SafetyVerdict.SAFE

        # 3. Default behavior if no required-list and no block-list overlap
        if config.LLAMAGUARD_CHECKS:
            logger.info(f"[{source_chat}]{label_prefix} Media classified as unsafe ({violated}), but none are in configured important checks ({config.LLAMAGUARD_CHECKS}). Passing.")
            return SafetyVerdict.SAFE
        else:
            logger.warning(f"[{source_chat}]{label_prefix} BLOCKING media {filename}! Violates Llama Guard categories: {violated}")
            return SafetyVerdict.BLOCKED
            
    else:  # safe
        if config.LLAMAGUARD_REQUIRE_CHECKS:
            logger.warning(f"[{source_chat}]{label_prefix} BLOCKING media {filename}! Classified as safe, but does not match any required unsafe categories: {config.LLAMAGUARD_REQUIRE_CHECKS}")
            return SafetyVerdict.BLOCKED
        return SafetyVerdict.SAFE


async def check_media_safety(media_bytes: bytes, mime_type: str, source_chat: str, filename: str) -> SafetyVerdict:
    """
    Checks the safety of the media using Meta Llama Guard via an OpenAI-compatible Vision API.
    Returns SAFE if the media may pass, BLOCKED if policy rejects it, UNAVAILABLE if the
    classification could not be performed (fail-closed, retryable).
    """
    if not config.LLAMAGUARD_API_URL:
        return SafetyVerdict.SAFE # Disabled, pass by default

    image_list = []
    if mime_type.startswith("image/"):
        image_list = [media_bytes]
    elif mime_type.startswith("video/"):
        logger.info(f"[{source_chat}] Extracting up to {config.LLAMAGUARD_VIDEO_FRAMES} frames from video {filename} for Llama Guard safety check...")
        image_list = await asyncio.to_thread(media.extract_video_frames, media_bytes, config.LLAMAGUARD_VIDEO_FRAMES, config.LLAMAGUARD_RANDOM_FRAMES)
        if not image_list:
            logger.error(f"[{source_chat}] Could not extract any video frames for safety check. Blocking media due to check failure.")
            return SafetyVerdict.UNAVAILABLE

    if not image_list:
        logger.error(f"[{source_chat}] No image bytes available for safety check. Blocking media due to check failure.")
        return SafetyVerdict.UNAVAILABLE

    if len(image_list) == 1:
        # Extracted video frames are always JPEG, whatever the container's mime type says.
        single_mime = mime_type if mime_type.startswith("image/") else "image/jpeg"
        return await check_single_image_safety(image_list[0], source_chat, filename, mime_type=single_mime)

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
            logger.error(f"[{source_chat}] One or more video frames failed classification. Blocking video due to fail-closed safety policy.")
            return SafetyVerdict.UNAVAILABLE

    # 2. Block-list check (LLAMAGUARD_CHECKS)
    # If ANY frame contains a category in LLAMAGUARD_CHECKS, we block the entire video.
    if config.LLAMAGUARD_CHECKS:
        for i, (is_safe, violated) in enumerate(results):
            if not is_safe:
                overlap = [c for c in violated if c in config.LLAMAGUARD_CHECKS]
                if overlap:
                    logger.warning(f"[{source_chat}] BLOCKING video {filename}! Frame {i+1} violates configured Llama Guard categories: {overlap}")
                    return SafetyVerdict.BLOCKED

    # 3. Required-list check (LLAMAGUARD_REQUIRE_CHECKS)
    # If whitelisting is active:
    # - At least ONE frame must match one of the required categories.
    # - Safe frames do not block the video in this context (since it's a video, and we only need the video to contain the required content somewhere).
    if config.LLAMAGUARD_REQUIRE_CHECKS:
        matched_required = False
        all_violated_matched = []
        for is_safe, violated in results:
            if not is_safe:
                required_overlap = [c for c in violated if c in config.LLAMAGUARD_REQUIRE_CHECKS]
                if required_overlap:
                    matched_required = True
                    all_violated_matched.extend(required_overlap)
                    
        if not matched_required:
            logger.warning(f"[{source_chat}] BLOCKING video {filename}! No frames matched any required categories: {config.LLAMAGUARD_REQUIRE_CHECKS}")
            return SafetyVerdict.BLOCKED
        else:
            logger.info(f"[{source_chat}] Video matches required safety check (found required categories {list(set(all_violated_matched))}). Passing.")
            return SafetyVerdict.SAFE

    # 4. Default check if no required-list is configured
    # Under the default policy (or when only LLAMAGUARD_CHECKS is set), a video is allowed if no frames violate the block-list.
    # If LLAMAGUARD_CHECKS is empty and LLAMAGUARD_REQUIRE_CHECKS is empty:
    # - Any unsafe frame blocks the video by default.
    if not config.LLAMAGUARD_CHECKS:
        # LLAMAGUARD_CHECKS is empty, and LLAMAGUARD_REQUIRE_CHECKS is empty:
        # This is the "block all unsafe content" mode. Any unsafe frame blocks the video.
        for i, (is_safe, violated) in enumerate(results):
            if not is_safe:
                logger.warning(f"[{source_chat}] BLOCKING video {filename}! Frame {i+1} violates Llama Guard categories: {violated}")
                return SafetyVerdict.BLOCKED

    logger.info(f"[{source_chat}] Video {filename} successfully passed safety checks.")
    return SafetyVerdict.SAFE

