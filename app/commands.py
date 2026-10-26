"""The !tmmb command handler.

Every toggle is scoped to a room: a command without an argument changes only the room it
was sent in, while the trailing ``all`` applies it to every configured room. The ``all``
scope is reserved for the admin so that ordinary users cannot enumerate the rooms the
bridge posts to.
"""

import html
import logging

import config
import matrix_io
import state
from nio import RoomMessageText

logger = logging.getLogger("tgmabr")

ALL_SCOPE = "all"

ENABLE_ACTIONS = ("enable", "on", "true", "1")
DISABLE_ACTIONS = ("disable", "off", "false", "0")

HELP_PLAIN = (
    "[TgMediaToMatrix] Command Usage:\n"
    "- !tmmb start [all] : Enable both image and video bridging in this room, or in all rooms (Admin only)\n"
    "- !tmmb stop [all] : Disable both image and video bridging in this room, or in all rooms (Admin only with 'all')\n"
    "- !tmmb image enable/disable [all] : Enable (Admin only) or disable image bridging\n"
    "- !tmmb video enable/disable [all] : Enable (Admin only) or disable video bridging\n"
    "- !tmmb status [all] : View the bridging status of this room, or of all rooms (Admin only with 'all')\n"
    "Without 'all' a command applies to the room it was sent in."
)
HELP_HTML = (
    "<strong>[TgMediaToMatrix] Command Usage:</strong><br/>"
    "• <code>!tmmb start [all]</code> : Enable both image and video bridging in this room, or in all rooms <em>(Admin only)</em><br/>"
    "• <code>!tmmb stop [all]</code> : Disable both image and video bridging in this room, or in all rooms <em>(Admin only with 'all')</em><br/>"
    "• <code>!tmmb image enable/disable [all]</code> : Enable <em>(Admin only)</em> or disable image bridging<br/>"
    "• <code>!tmmb video enable/disable [all]</code> : Enable <em>(Admin only)</em> or disable video bridging<br/>"
    "• <code>!tmmb status [all]</code> : View the bridging status of this room, or of all rooms <em>(Admin only with 'all')</em><br/>"
    "Without <code>all</code> a command applies to the room it was sent in."
)


def parse_scope(parts: list[str], index: int) -> tuple[bool, str | None]:
    """Read the optional trailing ``all`` argument.

    Returns ``(targets_all_rooms, invalid_token)``. Extra arguments are reported instead of
    ignored, so a typo cannot silently widen a command to every room.
    """
    extra = parts[index:]
    if not extra:
        return False, None
    if len(extra) == 1 and extra[0].lower() == ALL_SCOPE:
        return True, None
    return False, extra[0]


def scope_rooms(room_id: str, all_rooms: bool) -> list[str]:
    return list(config.MATRIX_ROOM_IDS) if all_rooms else [room_id]


def status_word(enabled: bool) -> str:
    return "ENABLED" if enabled else "DISABLED"


def room_status_line(room_id: str, with_id: bool) -> tuple[str, str]:
    """Plain and HTML status of one room."""
    images = status_word(state.room_toggles.is_enabled(room_id, "images"))
    videos = status_word(state.room_toggles.is_enabled(room_id, "videos"))
    plain_prefix = f"{room_id} : " if with_id else ""
    html_prefix = f"<code>{html.escape(room_id)}</code> : " if with_id else ""
    plain = f"{plain_prefix}images {images}, videos {videos}"
    html_text = f"{html_prefix}images <code>{images}</code>, videos <code>{videos}</code>"
    return plain, html_text


def global_status_lines() -> tuple[list[str], list[str]]:
    """Instance-wide values that are not switchable per room."""
    plain = [
        f"- Oversized Videos: {config.OVERSIZED_VIDEO_ACTION.upper()} (Limit: {config.MAX_MEDIA_SIZE_MB}MB, Ceiling: {config.OVERSIZED_VIDEO_MAX_INPUT_MB}MB)",
        f"- Alternate Renditions: {status_word(config.PREFER_ALTERNATE_RENDITION)}",
        f"- Deduplication: {status_word(config.DEDUPLICATION_ENABLED)} (TTL: {config.DEDUPLICATION_TTL_MINUTES}m, Cache: {len(state.media_deduplicator._cache)}/{config.DEDUPLICATION_CACHE_SIZE})",
        f"- Safety: {status_word(bool(config.LLAMAGUARD_API_URL))} (Blocked: {state.SAFETY_STATS['blocked']}, Unavailable: {state.SAFETY_STATS['unavailable']})",
        f"- Delivery: {state.DELIVERY_STATS['partial']} partial, {state.DELIVERY_STATS['failed']} failed (since start)",
    ]
    html_lines = [
        f"• Oversized Videos: <code>{config.OVERSIZED_VIDEO_ACTION.upper()}</code> (Limit: {config.MAX_MEDIA_SIZE_MB}MB, Ceiling: {config.OVERSIZED_VIDEO_MAX_INPUT_MB}MB)",
        f"• Alternate Renditions: <code>{status_word(config.PREFER_ALTERNATE_RENDITION)}</code>",
        f"• Deduplication: <code>{status_word(config.DEDUPLICATION_ENABLED)}</code> (TTL: {config.DEDUPLICATION_TTL_MINUTES}m, Cache: {len(state.media_deduplicator._cache)}/{config.DEDUPLICATION_CACHE_SIZE})",
        f"• Safety: <code>{status_word(bool(config.LLAMAGUARD_API_URL))}</code> (Blocked: {state.SAFETY_STATS['blocked']}, Unavailable: {state.SAFETY_STATS['unavailable']})",
        f"• Delivery: {state.DELIVERY_STATS['partial']} partial, {state.DELIVERY_STATS['failed']} failed (since start)",
    ]
    return plain, html_lines


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
    subcmd = parts[1].lower() if len(parts) > 1 else "help"

    async def notice(plain: str, html_text: str):
        await matrix_io.send_matrix_notice(room.room_id, plain, html_text)

    async def refuse_non_admin(what: str):
        logger.warning(f"[{room.room_id}] Refused admin-only action to '{event.sender}': {what}")
        await notice(
            f"[TgMediaToMatrix] Only the configured admin user can {what}.",
            f"<strong>[TgMediaToMatrix]</strong> Only the configured admin user can {html.escape(what)}."
        )

    async def refuse_scope(invalid_token: str, usage: str):
        logger.warning(f"[{room.room_id}] Unknown command argument from '{event.sender}': '{invalid_token}'")
        await notice(
            f"[TgMediaToMatrix] Unknown argument '{invalid_token}'. Usage: {usage}",
            f"<strong>[TgMediaToMatrix]</strong> Unknown argument <code>{html.escape(invalid_token)}</code>. Usage: <code>{html.escape(usage)}</code>"
        )

    def apply(kind: str | None, enabled: bool, all_rooms: bool) -> list[str]:
        rooms = scope_rooms(room.room_id, all_rooms)
        kinds = state.ROOM_MEDIA_KINDS if kind is None else (kind,)
        for target_room in rooms:
            for target_kind in kinds:
                state.room_toggles.set(target_room, target_kind, enabled)
        return rooms

    def scope_suffix(rooms: list[str]) -> str:
        return f"in all {len(rooms)} rooms" if len(rooms) > 1 else "in this room"

    if subcmd in ("help", "-h", "--help"):
        await notice(HELP_PLAIN, HELP_HTML)
        return

    # START / ENABLE (Both) -> Requires Admin
    if subcmd in ("start", "enable"):
        all_rooms, invalid = parse_scope(parts, 2)
        if invalid is not None:
            await refuse_scope(invalid, "!tmmb start [all]")
            return
        if not is_admin:
            await refuse_non_admin("start/enable media bridging")
            return

        rooms = apply(None, True, all_rooms)
        logger.info(
            f"[{room.room_id}] User '{event.sender}' STARTED (ENABLED) image and video bridging "
            f"for {len(rooms)} room(s): {', '.join(rooms)}."
        )
        await notice(
            f"[TgMediaToMatrix] Image and video bridging have been STARTED (ENABLED) {scope_suffix(rooms)}.",
            f"<strong>[TgMediaToMatrix]</strong> Image and video bridging have been <code>STARTED</code> (ENABLED) {scope_suffix(rooms)}."
        )
        return

    # STOP / DISABLE (Both) -> Admin, or ALLOW_NON_ADMIN_STOP for the own room
    if subcmd in ("stop", "disable"):
        all_rooms, invalid = parse_scope(parts, 2)
        if invalid is not None:
            await refuse_scope(invalid, "!tmmb stop [all]")
            return
        if all_rooms and not is_admin:
            await refuse_non_admin("stop media bridging in all rooms")
            return

        rooms = apply(None, False, all_rooms)
        logger.info(
            f"[{room.room_id}] User '{event.sender}' STOPPED (DISABLED) image and video bridging "
            f"for {len(rooms)} room(s): {', '.join(rooms)}."
        )
        await notice(
            f"[TgMediaToMatrix] Image and video bridging have been STOPPED (DISABLED) {scope_suffix(rooms)}.",
            f"<strong>[TgMediaToMatrix]</strong> Image and video bridging have been <code>STOPPED</code> (DISABLED) {scope_suffix(rooms)}."
        )
        return

    # STATUS -> own room for authorized users, every room for the admin only
    if subcmd == "status":
        all_rooms, invalid = parse_scope(parts, 2)
        if invalid is not None:
            await refuse_scope(invalid, "!tmmb status [all]")
            return
        if all_rooms and not is_admin:
            await refuse_non_admin("show the status of all rooms")
            return

        heading = "Current Status (all rooms)" if all_rooms else "Current Status (this room)"
        plain_lines = [f"[TgMediaToMatrix] {heading}:"]
        html_lines = [f"<strong>[TgMediaToMatrix] {heading}:</strong><br/>"]
        for target_room in (config.MATRIX_ROOM_IDS if all_rooms else [room.room_id]):
            room_plain, room_html = room_status_line(target_room, with_id=all_rooms)
            plain_lines.append(f"- {room_plain}")
            html_lines.append(f"• {room_html}<br/>")

        plain_globals, html_globals = global_status_lines()
        plain_lines.extend(plain_globals)
        html_lines.extend(html_globals)
        await notice("\n".join(plain_lines), "".join(html_lines))
        return

    # IMAGE COMMANDS
    if subcmd in ("image", "images"):
        if len(parts) < 3:
            await refuse_scope(subcmd, "!tmmb image enable/disable [all]")
            return
        action = parts[2].lower()
        all_rooms, invalid = parse_scope(parts, 3)
        if invalid is not None:
            await refuse_scope(invalid, f"!tmmb image {action} [all]")
            return

        if action in ENABLE_ACTIONS:
            if not is_admin:
                await refuse_non_admin("enable image bridging")
                return
            rooms = apply("images", True, all_rooms)
            logger.info(f"[{room.room_id}] User '{event.sender}' ENABLED image bridging for {len(rooms)} room(s).")
            await notice(
                f"[TgMediaToMatrix] Image bridging has been ENABLED {scope_suffix(rooms)}.",
                f"<strong>[TgMediaToMatrix]</strong> Image bridging has been <code>ENABLED</code> {scope_suffix(rooms)}."
            )
        elif action in DISABLE_ACTIONS:
            if all_rooms and not is_admin:
                await refuse_non_admin("disable image bridging in all rooms")
                return
            rooms = apply("images", False, all_rooms)
            logger.info(f"[{room.room_id}] User '{event.sender}' DISABLED image bridging for {len(rooms)} room(s).")
            await notice(
                f"[TgMediaToMatrix] Image bridging has been DISABLED {scope_suffix(rooms)}.",
                f"<strong>[TgMediaToMatrix]</strong> Image bridging has been <code>DISABLED</code> {scope_suffix(rooms)}."
            )
        else:
            await notice(
                f"[TgMediaToMatrix] Unknown action '{action}'. Use 'enable' or 'disable'.",
                f"<strong>[TgMediaToMatrix]</strong> Unknown action <code>{html.escape(action)}</code>. Use <code>enable</code> or <code>disable</code>."
            )
        return

    # VIDEO COMMANDS
    if subcmd in ("video", "videos"):
        if len(parts) < 3:
            await refuse_scope(subcmd, "!tmmb video enable/disable [all]")
            return
        action = parts[2].lower()
        all_rooms, invalid = parse_scope(parts, 3)
        if invalid is not None:
            await refuse_scope(invalid, f"!tmmb video {action} [all]")
            return

        if action in ENABLE_ACTIONS:
            if not is_admin:
                await refuse_non_admin("enable video bridging")
                return
            rooms = apply("videos", True, all_rooms)
            logger.info(f"[{room.room_id}] User '{event.sender}' ENABLED video bridging for {len(rooms)} room(s).")
            await notice(
                f"[TgMediaToMatrix] Video bridging has been ENABLED {scope_suffix(rooms)}.",
                f"<strong>[TgMediaToMatrix]</strong> Video bridging has been <code>ENABLED</code> {scope_suffix(rooms)}."
            )
        elif action in DISABLE_ACTIONS:
            if all_rooms and not is_admin:
                await refuse_non_admin("disable video bridging in all rooms")
                return
            rooms = apply("videos", False, all_rooms)
            logger.info(f"[{room.room_id}] User '{event.sender}' DISABLED video bridging for {len(rooms)} room(s).")
            await notice(
                f"[TgMediaToMatrix] Video bridging has been DISABLED {scope_suffix(rooms)}.",
                f"<strong>[TgMediaToMatrix]</strong> Video bridging has been <code>DISABLED</code> {scope_suffix(rooms)}."
            )
        else:
            await notice(
                f"[TgMediaToMatrix] Unknown action '{action}'. Use 'enable' or 'disable'.",
                f"<strong>[TgMediaToMatrix]</strong> Unknown action <code>{html.escape(action)}</code>. Use <code>enable</code> or <code>disable</code>."
            )
        return

    unknown_plain = f"[TgMediaToMatrix] Unknown command '{body}'. Type '!tmmb help' for usage."
    unknown_html = f"<strong>[TgMediaToMatrix]</strong> Unknown command <code>{html.escape(body)}</code>. Type <code>!tmmb help</code> for usage."
    await notice(unknown_plain, unknown_html)
