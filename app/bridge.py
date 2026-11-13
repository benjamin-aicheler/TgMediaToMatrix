"""Entry point: wires the Telegram handler and the Matrix sync supervisor."""

import asyncio
import logging
import time

import commands
import config
import handler  # noqa: F401 - importing it registers the Telegram event handler
import state
from nio import (
    RoomMessageText,
    SyncError,
)

logger = logging.getLogger("tgmabr")


# --- Matrix sync supervision -------------------------------------------------------------
# nio handles HTTP errors by returning SyncError objects instead of raising them, so without a
# response callback a rejected access token would silently disable the command listener while
# sync_forever keeps looping. Transport errors, on the other hand, propagate out of
# sync_forever and end the task for good. Both cases are handled here.
MATRIX_SYNC_INITIAL_BACKOFF_SECONDS = 5
MATRIX_SYNC_MAX_BACKOFF_SECONDS = 300
MATRIX_SYNC_STABLE_SECONDS = 60

# Set once the homeserver rejected our access token; retrying cannot fix that.
MATRIX_SYNC_AUTH_FAILED = False

# Set when sync_forever was stopped because of a sync error, so the supervisor knows the
# return was deliberate and backs off before restarting.
MATRIX_SYNC_ERROR_STOP = False

# nio's ErrorResponse.from_dict() stores the Matrix errcode in status_code (never the HTTP
# status) and the human-readable text in message. The errcode is what identifies the error.
MATRIX_AUTH_ERRCODES = frozenset({"M_UNKNOWN_TOKEN", "M_MISSING_TOKEN"})
MATRIX_RATE_LIMIT_ERRCODE = "M_LIMIT_EXCEEDED"


def _http_status(response) -> int | None:
    """HTTP status of the underlying transport response, if nio kept one."""
    return getattr(getattr(response, "transport_response", None), "status", None)


def classify_sync_error(response) -> str:
    """Returns 'auth', 'rate_limited' or 'transient' for a SyncError."""
    errcode = response.status_code
    http_status = _http_status(response)
    if (
        errcode in MATRIX_AUTH_ERRCODES
        or getattr(response, "soft_logout", False)
        # A proxy answering 401 with an HTML page leaves no errcode behind (nio parses the
        # body to {} and reports "unknown error"); the HTTP status still identifies it.
        or http_status == 401
    ):
        return "auth"
    if errcode == MATRIX_RATE_LIMIT_ERRCODE or http_status == 429:
        return "rate_limited"
    return "transient"


def _describe_sync_error(response) -> str:
    http_status = _http_status(response)
    parts = [str(response.status_code or "no errcode")]
    if http_status is not None:
        parts.append(f"HTTP {http_status}")
    parts.append(str(response.message or "").strip() or "no message")
    return ", ".join(parts)


async def on_matrix_response(response):
    """Surface sync errors; stop the loop for a rejected token or a failed sync.

    nio's sync_forever() does not pause after an error response: without loop_sleep_time it
    calls /sync again immediately. Every error therefore has to leave the loop here, or a
    persistent failure turns into a tight request loop and a log line per iteration.
    """
    global MATRIX_SYNC_AUTH_FAILED, MATRIX_SYNC_ERROR_STOP

    if not isinstance(response, SyncError):
        return

    kind = classify_sync_error(response)
    description = _describe_sync_error(response)

    if kind == "rate_limited":
        # nio's _send() already sleeps retry_after_ms and retries the same request; it only
        # hands us the response for information. Stopping here would add a needless restart.
        logger.debug(f"Matrix sync rate-limited ({description}); nio retries after the requested delay.")
        return

    if kind == "transient":
        logger.error(f"Matrix sync error ({description}). Restarting the sync after a backoff.")
        MATRIX_SYNC_ERROR_STOP = True
        state.matrix_client.stop_sync_forever()
        return

    if MATRIX_SYNC_AUTH_FAILED:
        return

    MATRIX_SYNC_AUTH_FAILED = True
    logger.critical(
        f"Matrix rejected the access token ({description}). Interactive commands stay "
        f"disabled until MATRIX_ACCESS_TOKEN is replaced and the bridge is restarted."
    )
    # Retrying cannot fix a rejected token, so the supervisor stops for good.
    state.matrix_client.stop_sync_forever()


async def matrix_sync_loop():
    """Run sync_forever, restarting it with exponential backoff after a failure.

    A restart resumes from the client's next_batch token, so no events are lost.
    """
    global MATRIX_SYNC_ERROR_STOP
    backoff = MATRIX_SYNC_INITIAL_BACKOFF_SECONDS

    while True:
        started = time.monotonic()
        MATRIX_SYNC_ERROR_STOP = False
        try:
            await state.matrix_client.sync_forever(timeout=30000, full_state=False)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the sync loop must survive arbitrary network and library errors
            logger.error(f"Matrix sync stopped with an error: {e!r}")
        else:
            if MATRIX_SYNC_AUTH_FAILED:
                logger.error("Matrix sync supervisor stopping: the access token was rejected.")
                return
            if not MATRIX_SYNC_ERROR_STOP:
                logger.warning("Matrix sync loop returned unexpectedly.")

        ran_for = time.monotonic() - started
        if ran_for >= MATRIX_SYNC_STABLE_SECONDS:
            backoff = MATRIX_SYNC_INITIAL_BACKOFF_SECONDS

        logger.info(f"Restarting Matrix sync in {backoff}s (last run lasted {ran_for:.0f}s).")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MATRIX_SYNC_MAX_BACKOFF_SECONDS)


async def main():
    logger.info("Starting Telegram client...")
    await state.tg_client.start()
    logger.info(f"Bridge successfully started and active for channels: {config.TG_CHANNELS}")
    logger.info(f"Target Matrix rooms: {config.MATRIX_ROOM_IDS}")
    logger.info(f"Configured max media limit: {config.MAX_MEDIA_SIZE_MB} MB")
    logger.info(
        f"Oversized video action: {config.OVERSIZED_VIDEO_ACTION.upper()} "
        f"(Max input: {config.OVERSIZED_VIDEO_MAX_INPUT_MB} MB, Fallback compress: {config.OVERSIZED_VIDEO_FALLBACK_COMPRESS})"
    )
    logger.info(f"Configured min image limit: {config.MIN_IMAGE_SIZE_KB} KB ({round(config.MIN_IMAGE_SIZE_KB / 1024, 2)} MB)" if config.MIN_IMAGE_SIZE_KB > 0 else "Configured min image limit: None")
    logger.info(f"Configured min video limit: {config.MIN_VIDEO_SIZE_KB} KB ({round(config.MIN_VIDEO_SIZE_KB / 1024, 2)} MB)" if config.MIN_VIDEO_SIZE_KB > 0 else "Configured min video limit: None")
    logger.info(f"Images enabled by default (ENABLE_IMAGES): {config.ENABLE_IMAGES}")
    logger.info(f"Videos enabled by default (ENABLE_VIDEOS): {config.ENABLE_VIDEOS}")
    logger.info(f"Media deduplication: {'Enabled' if config.DEDUPLICATION_ENABLED else 'Disabled'} (TTL: {config.DEDUPLICATION_TTL_MINUTES} min, Max cache: {config.DEDUPLICATION_CACHE_SIZE})")

    matrix_sync_task = None
    if config.ADMIN_MATRIX_USER_ID or config.ALLOW_NON_ADMIN_STOP:
        logger.info(f"Matrix command listener active (Admin User: {config.ADMIN_MATRIX_USER_ID or 'None'}, Allow Non-Admin Stop: {config.ALLOW_NON_ADMIN_STOP})")
        state.matrix_client.add_event_callback(commands.on_matrix_message, RoomMessageText)
        state.matrix_client.add_response_callback(on_matrix_response, SyncError)
        matrix_sync_task = asyncio.create_task(matrix_sync_loop())
    else:
        logger.info("Admin Matrix user (ADMIN_MATRIX_USER_ID) and ALLOW_NON_ADMIN_STOP not configured. Dynamic chat commands disabled.")

    logger.info(f"Llama Guard Moderation: {'Enabled' if config.LLAMAGUARD_API_URL else 'Disabled'}")
    if config.LLAMAGUARD_API_URL:
        logger.info(f"Llama Guard Model: {config.LLAMAGUARD_MODEL_NAME}")
        logger.info(f"Llama Guard Video Frames: {config.LLAMAGUARD_VIDEO_FRAMES} (Random Selection: {config.LLAMAGUARD_RANDOM_FRAMES})")
        logger.info(f"Llama Guard Checks Filter (Block List): {list(config.LLAMAGUARD_CHECKS) if config.LLAMAGUARD_CHECKS else 'ALL categories'}")
        logger.info(f"Llama Guard Required Checks (Whitelist): {list(config.LLAMAGUARD_REQUIRE_CHECKS) if config.LLAMAGUARD_REQUIRE_CHECKS else 'None (safe content allowed)'}")
    try:
        await state.tg_client.run_until_disconnected()
    finally:
        if matrix_sync_task and not matrix_sync_task.done():
            matrix_sync_task.cancel()
        await state.matrix_client.close()

if __name__ == '__main__':
    asyncio.run(main())

