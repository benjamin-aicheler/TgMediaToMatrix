# TgMediaToMatrix Project Guidelines

This file contains development rules and architectural guidelines for AI agents working on the **TgMediaToMatrix** project.

---

## 1. Code Style & Architecture

### Asynchronous Operations
- Always use asynchronous coding practices with Python's standard `asyncio` library.
- Avoid blocking I/O calls. Use appropriate async methods from `Telethon` and `matrix-nio`.
- Offload CPU-bound, image/video-processing, or blocking synchronous function calls (like PIL image open/probe/resize or PyAV video frame extraction) to background threads using `asyncio.to_thread` or a thread pool executor to prevent stalling the main event loop.

### Matrix Integration
- Prefer `matrix-nio` native library functions over custom HTTP requests.
- Use `matrix_client.upload()` with in-memory `io.BytesIO` streams rather than writing media to disk or utilizing external libraries (e.g., `httpx`) unless specifically requested.
- Maintain a single, globally-reused `AsyncClient` instance for room communication and media uploads. Ensure it is closed gracefully when the main program stops.
- Interactive Matrix commands (`!tmmb`) must be authorized strictly against `ADMIN_MATRIX_USER_ID` (with optional non-admin `stop`/`disable` permissions if `ALLOW_NON_ADMIN_STOP` is set) and ignore historical events received prior to startup.
- Image/video enablement is per destination room (`state.room_toggles`). A command without the trailing `all` may only touch the room it was sent in; every `all` variant — including `status all`, so the room list is not disclosed — is reserved for the admin. Without an override a room follows `ENABLE_IMAGES` / `ENABLE_VIDEOS`, so a restart returns to the environment defaults.

---

## 2. Configuration & Validation

### Startup Verification
- Any configuration loaded from environment variables (e.g., `TG_API_ID`, `TG_CHANNELS`, `MATRIX_ROOM_IDS` / `MATRIX_ROOM_ID`) must be validated at script startup.
- Do not let the program fail silently or with unclear tracebacks later in the process. Raise descriptive exceptions if any required variables are missing or empty.
- Ensure that comma-separated values (like `TG_CHANNELS`) are correctly stripped of extra spaces, and empty elements (such as trailing commas) are ignored.

### Session Persistence
- The Telethon session file must be stored under the `session/` folder.
- Ensure that the local `session_data` directory (which holds these sessions) is mounted inside the Docker container to prevent the need for re-authentication when restarting.
- Do not commit any Telethon session files to version control.

---

## 3. Logging & Error Handling

- Implement consistent and context-aware logging. Always prefix log entries with the source chat username or ID (e.g. `[{source_chat}]`).
- Handle errors in media download and upload gracefully. If an individual file in an album fails to download or upload, log it clearly but do not allow it to crash the process or prevent other media items in the album from being processed.

---

## 4. Content Moderation & Media Processing

### In-Memory Operations
- Video frame extraction (for content safety moderation) must be done entirely in-memory (e.g. using `av` and `PIL`). Never write extracted frames or intermediate video clips to disk.

### Fail-Closed Design
- When content moderation is configured/enabled, any processing error, frame extraction failure, or external API communication failure MUST result in a fail-closed response (i.e., block the media from being forwarded). Unmoderated content must never be allowed to pass.

### Filter Adherence
- Fully respect user-configured safety categories in `LLAMAGUARD_CHECKS`. If the content is classified as unsafe but none of the violated categories overlap with the user's configured important checks, the media must be permitted to pass.

### Media Thumbnailing
- Image thumbnails must be generated dynamically in-memory from full-sized media bytes using Pillow rather than making redundant network requests to Telegram.
- Check if the original image is already smaller than the max thumbnail bounds (e.g. 800x800) and return early with the original bytes to avoid unnecessary re-encoding.

### Video Splitting & RAM-Disk Processing
- When handling oversized videos that exceed Matrix homeserver limits, prioritize lossless stream-copy splitting (`-c copy`) over re-encoding to minimize CPU and RAM consumption on low-power devices like Raspberry Pi 4.
- Because FFmpeg requires seekable filesystem paths to write MP4 `moov` index headers and multi-file segments, video splitting operations must use `/dev/shm` (Linux tmpfs RAM disk) rather than physical disk or microSD storage. Ensure temporary chunk directories are deleted immediately in `finally` blocks.
- Each split part previews itself: `media.extract_video_thumbnail()` samples a frame in-memory from a little past the middle of that part (a seek lands on the preceding keyframe, then decoding closes the distance) and `handler.process_and_upload_media` uploads one thumbnail per part, computing the part's Blurhash from that same frame. Never hardcode the primary document's thumbnail onto every part. If extraction or the per-part upload fails, fall back to the primary document's Telegram thumbnail instead of sending the part without one.

### Alternate Renditions for Oversized Videos
- Before a video is skipped for exceeding `OVERSIZED_VIDEO_MAX_INPUT_BYTES`, prefer one of Telegram's renditions from `MessageMediaDocument.alt_documents` when `PREFER_ALTERNATE_RENDITION` is enabled: the highest resolution that fits `MAX_MEDIA_SIZE_BYTES` is sent as-is, otherwise the highest that fits the ceiling goes through `OVERSIZED_VIDEO_ACTION`. Selection lives in `media.select_alternate_rendition()`; never select a document that `media.is_rendition_document()` rejects — those are preview/scrub clips of a few dozen KB.
- A rendition is a Telegram transcode, not the original upload. Never describe it as lossless, and keep the `-c copy` and RAM-disk rules for the processing path.
- The pre-download deduplication key must always come from the **primary** document (`media.get_telegram_media_id()` reads `message.document`). Deriving it from the selected rendition would break duplicate suppression silently: the same repost would produce a different key depending on which rendition was chosen.

### Dependency Isolation
- Isolate the imports of optional binary/C-bound packages (like `av` and `PIL`) inside the functions that use them, catching `ImportError` gracefully, to keep the main service core runnable even if those specific packages are not present.

---

## 5. Deployment, Containerization & CI/CD

### Python Base Image & Architecture Support
- The base image is pinned by its multi-arch index digest on the **3.13 line** (`python:3.13-slim@sha256:…`). Patch-level digest bumps are routine; a Python **series** jump (3.14, 3.15, …) is a deliberate change that moves the `Dockerfile` and the `ci.yml` test version together and must be backed by a real test run on the new series. Dependabot ignores series jumps on purpose (`.github/dependabot.yml`).
- Stay on a stable production Python release so pre-compiled `linux/arm64` binary wheels are readily available on PyPI. Avoid pre-release Python versions that force expensive C/Rust source compilation during builds on ARM devices.
- Container builds must be multi-architecture (`linux/amd64` and `linux/arm64`) to run natively on both servers and Raspberry Pi 4.

### Image Tagging & Release Workflow
- Automated container builds via GitHub Actions should trigger on pushes to `main` and tag images with `:main` (`ghcr.io/benjamin-aicheler/tgmediatomatrix:main`).
- Production `docker-compose.yml` should reference the published `:main` image directly (without `build: .`) so target devices like Raspberry Pi pull the prebuilt image instead of building locally.

### Resource Allocation
- Always configure adequate shared memory (`shm_size: 1g`) in Docker Compose to ensure in-memory video segmenting in `/dev/shm` does not encounter "No space left on device" errors.

---

## 6. Dependencies & Checks

### Locked requirements
- `requirements.in` and `requirements-dev.in` hold the direct dependencies and are the only requirement files edited by hand.
- `requirements.txt` and `requirements-dev.txt` are generated locks: every transitive package is pinned there. **Never edit them by hand** — regenerate instead:
  ```bash
  uv pip compile requirements.in      --python-version 3.13 -o requirements.txt
  uv pip compile requirements-dev.in  --python-version 3.13 -o requirements-dev.txt
  ```
- Adding a dependency means: add it to the `.in` file, regenerate, commit the `.in` and the lock together. A new runtime dependency must have `linux/arm64` wheels, otherwise the multi-arch build starts compiling from source.
- Dependabot maintains the pins (transitives included) and the CI actions weekly. Never hand-edit a pin to fix a Dependabot PR — regenerate the lock so the `.in` stays the source of truth.

### Before proposing a change
- `ruff check --extend-select B,LOG,BLE` must pass; that is exactly what CI runs.
- `pytest` must pass. The suite needs no network and no ffmpeg.
- The `docker` CI job builds the image and asserts the runtime UID is `10001`. A change that breaks the unprivileged user or the ownership of `/app/session` is a blocker.
