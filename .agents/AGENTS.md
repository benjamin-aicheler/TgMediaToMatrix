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

---

## 2. Configuration & Validation

### Startup Verification
- Any configuration loaded from environment variables (e.g., `TG_API_ID`, `TG_CHANNELS`, `MATRIX_ROOM_ID`) must be validated at script startup.
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

### Dependency Isolation
- Isolate the imports of optional binary/C-bound packages (like `av` and `PIL`) inside the functions that use them, catching `ImportError` gracefully, to keep the main service core runnable even if those specific packages are not present.

