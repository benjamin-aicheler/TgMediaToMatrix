"""The !tmmb command handler."""

import html
import logging

import config
import matrix_io
import state
from nio import RoomMessageText

logger = logging.getLogger("tgmabr")


async def on_matrix_message(room, event: RoomMessageText):
    """Matrix command handler for admin/room users to toggle image/video bridging dynamically."""

    if not (config.ADMIN_MATRIX_USER_ID or config.ALLOW_NON_ADMIN_STOP):
        return

    # Only process commands sent from configured destination rooms
    if room.room_id not in config.MATRIX_ROOM_IDS:
        return

    # Ignore old messages sent before startup to prevent re-triggering historical commands
    if hasattr(event, 'server_timestamp') and event.server_timestamp and event.server_timestamp < config.STARTUP_TIMESTAMP_MS:
        return

    body = event.body.strip()
    if not body.lower().startswith("!tmmb"):
        return

    is_admin = bool(config.ADMIN_MATRIX_USER_ID and event.sender == config.ADMIN_MATRIX_USER_ID)
    is_stop_allowed = is_admin or config.ALLOW_NON_ADMIN_STOP

    # Check authorization to issue commands
    if not is_stop_allowed:
        logger.warning(f"[{room.room_id}] Unauthorized command attempt by invalid sender '{event.sender}': '{body}'")
        return

    logger.info(f"[{room.room_id}] Command received from user '{event.sender}': '{body}'")

    parts = body.split()
    if len(parts) == 1 or parts[1].lower() in ("help", "-h", "--help"):
        help_plain = (
            "[TgMediaToMatrix] Command Usage:\n"
            "- !tmmb start : Enable both image and video bridging (Admin only)\n"
            "- !tmmb stop : Disable both image and video bridging\n"
            "- !tmmb image enable/disable : Enable (Admin only) or disable image bridging\n"
            "- !tmmb video enable/disable : Enable (Admin only) or disable video bridging\n"
            "- !tmmb status : View current bridging status"
        )
        help_html = (
            "<strong>[TgMediaToMatrix] Command Usage:</strong><br/>"
            "• <code>!tmmb start</code> : Enable both image and video bridging <em>(Admin only)</em><br/>"
            "• <code>!tmmb stop</code> : Disable both image and video bridging<br/>"
            "• <code>!tmmb image enable/disable</code> : Enable <em>(Admin only)</em> or disable image bridging<br/>"
            "• <code>!tmmb video enable/disable</code> : Enable <em>(Admin only)</em> or disable video bridging<br/>"
            "• <code>!tmmb status</code> : View current bridging status"
        )
        await matrix_io.send_matrix_notice(room.room_id, help_plain, help_html)
        return

    subcmd = parts[1].lower()

    # START / ENABLE (Both) -> Requires Admin
    if subcmd in ("start", "enable") and len(parts) == 2:
        if not is_admin:
            logger.warning(f"[{room.room_id}] Unauthorized start attempt by non-admin '{event.sender}': '{body}'")
            notice_plain = "[TgMediaToMatrix] Only the configured admin user can start/enable media bridging."
            notice_html = "<strong>[TgMediaToMatrix]</strong> Only the configured admin user can start/enable media bridging."
            await matrix_io.send_matrix_notice(room.room_id, notice_plain, notice_html)
            return

        config.ENABLE_IMAGES = True
        config.ENABLE_VIDEOS = True
        logger.info(f"[{room.room_id}] User '{event.sender}' STARTED (ENABLED) both image and video bridging.")
        msg_plain = "[TgMediaToMatrix] Both image and video bridging have been STARTED (ENABLED)."
        msg_html = "<strong>[TgMediaToMatrix]</strong> Both image and video bridging have been <code>STARTED</code> (ENABLED)."
        await matrix_io.send_matrix_notice(room.room_id, msg_plain, msg_html)
        return

    # STOP / DISABLE (Both) -> Admin or ALLOW_NON_ADMIN_STOP
    if subcmd in ("stop", "disable") and len(parts) == 2:
        config.ENABLE_IMAGES = False
        config.ENABLE_VIDEOS = False
        logger.info(f"[{room.room_id}] User '{event.sender}' STOPPED (DISABLED) both image and video bridging.")
        msg_plain = "[TgMediaToMatrix] Both image and video bridging have been STOPPED (DISABLED)."
        msg_html = "<strong>[TgMediaToMatrix]</strong> Both image and video bridging have been <code>STOPPED</code> (DISABLED)."
        await matrix_io.send_matrix_notice(room.room_id, msg_plain, msg_html)
        return

    # STATUS -> Admin or ALLOW_NON_ADMIN_STOP
    if subcmd == "status":
        status_plain = (
            f"[TgMediaToMatrix] Current Status:\n"
            f"- Images: {'ENABLED' if config.ENABLE_IMAGES else 'DISABLED'}\n"
            f"- Videos: {'ENABLED' if config.ENABLE_VIDEOS else 'DISABLED'}\n"
            f"- Oversized Videos: {config.OVERSIZED_VIDEO_ACTION.upper()} (Limit: {config.MAX_MEDIA_SIZE_MB}MB, Ceiling: {config.OVERSIZED_VIDEO_MAX_INPUT_MB}MB)\n"
            f"- Alternate Renditions: {'ENABLED' if config.PREFER_ALTERNATE_RENDITION else 'DISABLED'}\n"
            f"- Deduplication: {'ENABLED' if config.DEDUPLICATION_ENABLED else 'DISABLED'} (TTL: {config.DEDUPLICATION_TTL_MINUTES}m, Cache: {len(state.media_deduplicator._cache)}/{config.DEDUPLICATION_CACHE_SIZE})\n"
            f"- Safety: {'ENABLED' if config.LLAMAGUARD_API_URL else 'DISABLED'} (Blocked: {state.SAFETY_STATS['blocked']}, Unavailable: {state.SAFETY_STATS['unavailable']})\n"
            f"- Delivery: {state.DELIVERY_STATS['partial']} partial, {state.DELIVERY_STATS['failed']} failed (since start)"
        )
        status_html = (
            f"<strong>[TgMediaToMatrix] Current Status:</strong><br/>"
            f"• Images: <code>{'ENABLED' if config.ENABLE_IMAGES else 'DISABLED'}</code><br/>"
            f"• Videos: <code>{'ENABLED' if config.ENABLE_VIDEOS else 'DISABLED'}</code><br/>"
            f"• Oversized Videos: <code>{config.OVERSIZED_VIDEO_ACTION.upper()}</code> (Limit: {config.MAX_MEDIA_SIZE_MB}MB, Ceiling: {config.OVERSIZED_VIDEO_MAX_INPUT_MB}MB)<br/>"
            f"• Alternate Renditions: <code>{'ENABLED' if config.PREFER_ALTERNATE_RENDITION else 'DISABLED'}</code><br/>"
            f"• Deduplication: <code>{'ENABLED' if config.DEDUPLICATION_ENABLED else 'DISABLED'}</code> (TTL: {config.DEDUPLICATION_TTL_MINUTES}m, Cache: {len(state.media_deduplicator._cache)}/{config.DEDUPLICATION_CACHE_SIZE})<br/>"
            f"• Safety: <code>{'ENABLED' if config.LLAMAGUARD_API_URL else 'DISABLED'}</code> (Blocked: {state.SAFETY_STATS['blocked']}, Unavailable: {state.SAFETY_STATS['unavailable']})<br/>"
            f"• Delivery: {state.DELIVERY_STATS['partial']} partial, {state.DELIVERY_STATS['failed']} failed (since start)"
        )
        await matrix_io.send_matrix_notice(room.room_id, status_plain, status_html)
        return

    # IMAGE COMMANDS
    if len(parts) >= 3 and subcmd in ("image", "images"):
        action = parts[2].lower()
        if action in ("enable", "on", "true", "1"):
            if not is_admin:
                logger.warning(f"[{room.room_id}] Unauthorized image enable attempt by non-admin '{event.sender}': '{body}'")
                notice_plain = "[TgMediaToMatrix] Only the configured admin user can enable image bridging."
                notice_html = "<strong>[TgMediaToMatrix]</strong> Only the configured admin user can enable image bridging."
                await matrix_io.send_matrix_notice(room.room_id, notice_plain, notice_html)
                return

            config.ENABLE_IMAGES = True
            logger.info(f"[{room.room_id}] User '{event.sender}' ENABLED image bridging.")
            msg_plain = "[TgMediaToMatrix] Image bridging has been ENABLED."
            msg_html = "<strong>[TgMediaToMatrix]</strong> Image bridging has been <code>ENABLED</code>."
            await matrix_io.send_matrix_notice(room.room_id, msg_plain, msg_html)
        elif action in ("disable", "off", "false", "0"):
            config.ENABLE_IMAGES = False
            logger.info(f"[{room.room_id}] User '{event.sender}' DISABLED image bridging.")
            msg_plain = "[TgMediaToMatrix] Image bridging has been DISABLED."
            msg_html = "<strong>[TgMediaToMatrix]</strong> Image bridging has been <code>DISABLED</code>."
            await matrix_io.send_matrix_notice(room.room_id, msg_plain, msg_html)
        else:
            invalid_plain = f"[TgMediaToMatrix] Unknown action '{action}'. Use 'enable' or 'disable'."
            invalid_html = f"<strong>[TgMediaToMatrix]</strong> Unknown action <code>{html.escape(action)}</code>. Use <code>enable</code> or <code>disable</code>."
            await matrix_io.send_matrix_notice(room.room_id, invalid_plain, invalid_html)
        return

    # VIDEO COMMANDS
    if len(parts) >= 3 and subcmd in ("video", "videos"):
        action = parts[2].lower()
        if action in ("enable", "on", "true", "1"):
            if not is_admin:
                logger.warning(f"[{room.room_id}] Unauthorized video enable attempt by non-admin '{event.sender}': '{body}'")
                notice_plain = "[TgMediaToMatrix] Only the configured admin user can enable video bridging."
                notice_html = "<strong>[TgMediaToMatrix]</strong> Only the configured admin user can enable video bridging."
                await matrix_io.send_matrix_notice(room.room_id, notice_plain, notice_html)
                return

            config.ENABLE_VIDEOS = True
            logger.info(f"[{room.room_id}] User '{event.sender}' ENABLED video bridging.")
            msg_plain = "[TgMediaToMatrix] Video bridging has been ENABLED."
            msg_html = "<strong>[TgMediaToMatrix]</strong> Video bridging has been <code>ENABLED</code>."
            await matrix_io.send_matrix_notice(room.room_id, msg_plain, msg_html)
        elif action in ("disable", "off", "false", "0"):
            config.ENABLE_VIDEOS = False
            logger.info(f"[{room.room_id}] User '{event.sender}' DISABLED video bridging.")
            msg_plain = "[TgMediaToMatrix] Video bridging has been DISABLED."
            msg_html = "<strong>[TgMediaToMatrix]</strong> Video bridging has been <code>DISABLED</code>."
            await matrix_io.send_matrix_notice(room.room_id, msg_plain, msg_html)
        else:
            invalid_plain = f"[TgMediaToMatrix] Unknown action '{action}'. Use 'enable' or 'disable'."
            invalid_html = f"<strong>[TgMediaToMatrix]</strong> Unknown action <code>{html.escape(action)}</code>. Use <code>enable</code> or <code>disable</code>."
            await matrix_io.send_matrix_notice(room.room_id, invalid_plain, invalid_html)
        return

    unknown_plain = f"[TgMediaToMatrix] Unknown command '{body}'. Type '!tmmb help' for usage."
    unknown_html = f"<strong>[TgMediaToMatrix]</strong> Unknown command <code>{html.escape(body)}</code>. Type <code>!tmmb help</code> for usage."
    await matrix_io.send_matrix_notice(room.room_id, unknown_plain, unknown_html)

