#!/usr/bin/env python3
"""MCP server giving Claude senses on a Pixel over KDE Connect.

KDE Connect is the whole transport: a persistent, cert-pinned link held warm by
the kdeconnectd daemon. Pair once on the phone and it survives drops and
reboots; each tool call is a fast local hop into the daemon — no per-call
rediscovery, no Wireless-debugging toggling. (adb was dropped: it's flaky and
really an app-dev tool, and its only unique tricks — screencap and raw shell —
aren't what this is for.)

Capabilities map to senses: EYES (camera capture), EARS (notifications),
VOICE/HANDS (SMS, remote typing, file push), plus battery/connectivity, ping,
and ring.

Run (stdio, for Claude Desktop / Claude Code):
    python3 pixel_mcp.py

Register with Claude Code:
    claude mcp add pixel --scope user -- python3 /path/to/phone-mcp/pixel_mcp.py

By default the first paired+reachable KDE Connect device is used; override with
KDECONNECT_NAME / KDECONNECT_DEVICE / PIXEL_PULL_DIR (see kdeconnect.py).
"""

import json

from mcp.server.fastmcp import FastMCP, Image

from kdeconnect import KDEConnect

mcp = FastMCP("pixel-kdeconnect")


def _kc():
    return KDEConnect()


@mcp.resource("pixel://status", mime_type="application/json",
              name="pixel-status")
def pixel_status_resource() -> str:
    return json.dumps(_kc().status(), indent=2)


@mcp.tool()
def kde_status() -> dict:
    """Snapshot over KDE Connect: target device, paired?, reachable?, battery
    charge + charging, and cellular connectivity. Read this first for context.
    Same content as the pixel://status resource. If it reports not
    paired/reachable, the phone needs KDE Connect open on the home Wi-Fi (and a
    one-time pair). Queries only.
    """
    return _kc().status()


@mcp.tool(structured_output=False)  # returns [dict, Image]
def kde_photo(timeout: int = 180) -> list:
    """EYES, on demand: open the phone's camera, wait for the user to shoot,
    transfer the photo to the host (~/Pictures/pixel), and return it as an
    image to view directly. Blocks until the photo is taken (or `timeout` s).

    This is the "take a snap now" move — a fresh capture over the stable link.
    Tell the user the camera is open and to take the shot.
    """
    local = _kc().photo(timeout=timeout)
    return [{"saved_to": str(local), "name": local.name}, Image(path=str(local))]


@mcp.tool()
def kde_notifications() -> str:
    """EARS: list the phone's active notifications (texts, app alerts, alarms)
    as text. Use to see what just arrived on the phone.
    """
    return _kc().notifications()


@mcp.tool()
def kde_send_sms(message: str, destination: str) -> str:
    """VOICE: send an SMS from the phone to `destination` (a phone number).
    The message goes out over the phone's real cellular line.
    """
    return _kc().send_sms(message, destination)


@mcp.tool()
def kde_send_text(text: str) -> str:
    """HANDS: type `text` into whatever field is focused on the phone (KDE
    Connect remote keyboard). Use to fill inputs or drive on-screen typing.
    """
    return _kc().send_text(text)


@mcp.tool()
def kde_share(path_or_url: str) -> str:
    """Push a local file path or a URL to the phone (opens/saves it there)."""
    return _kc().share(path_or_url)


@mcp.tool()
def kde_clipboard() -> str:
    """Send the desktop clipboard to the phone's clipboard."""
    return _kc().send_clipboard()


@mcp.tool()
def kde_ping(message: str | None = None) -> str:
    """Buzz the phone with a notification ping (optional message) — a quick
    "are you there / look at me" nudge to the user.
    """
    return _kc().ping(message)


@mcp.tool()
def kde_ring() -> str:
    """Ring the phone loudly to locate it (findmyphone), even if silenced."""
    return _kc().ring()


@mcp.tool()
def kde_now_playing() -> dict:
    """EARS: what's playing on the phone now — player, title/artist/album,
    playing?, position/length, and the list of available players.
    """
    return _kc().now_playing()


@mcp.tool()
def kde_media_control(action: str, player: str | None = None) -> str:
    """HANDS: control phone media playback. `action` is one of play, pause,
    playpause, next, previous, stop. Optionally target a specific `player`
    (from kde_now_playing's player list); default is the current player.
    """
    return _kc().media_control(action, player)


@mcp.tool()
def kde_media_players() -> list:
    """List the media players currently available to control on the phone."""
    return _kc().media_players()


if __name__ == "__main__":
    mcp.run()
