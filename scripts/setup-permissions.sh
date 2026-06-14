#!/usr/bin/env bash
#
# setup-permissions.sh — one-time, OPTIONAL adb bootstrap for the phone-mcp grants.
#
# The MCP transport is KDE Connect, not adb. But several KDE Connect plugins need
# Android permissions that are otherwise granted by hand (KDE Connect app -> tap
# the desktop -> "Some Plugins need permissions"). This script grants them all in
# one shot over adb so a fresh phone is MCP-ready without the tapping.
#
# Requirements: `adb` on PATH and the phone reachable (USB, or Wireless debugging
# paired+connected). Pick a specific device with:  ADB_SERIAL=<serial> ./setup-permissions.sh
#
# Idempotent and non-destructive: it does NOT switch your active keyboard and does
# NOT touch the connection/pairing. Re-run it any time (e.g. after reinstalling
# KDE Connect).

set -euo pipefail

PKG="org.kde.kdeconnect_tp"
ADB=(adb)
[[ -n "${ADB_SERIAL:-}" ]] && ADB=(adb -s "$ADB_SERIAL")

if ! command -v adb >/dev/null 2>&1; then
  echo "error: adb not found on PATH" >&2; exit 1
fi
if ! "${ADB[@]}" get-state >/dev/null 2>&1; then
  echo "error: no reachable device. Connect USB, or enable Wireless debugging and" >&2
  echo "       \`adb pair <ip:port> <code>\` then \`adb connect <ip:port>\`." >&2
  exit 1
fi
if ! "${ADB[@]}" shell pm list packages 2>/dev/null | grep -q "$PKG"; then
  echo "error: KDE Connect ($PKG) is not installed on the device." >&2; exit 1
fi

say() { printf '  %-26s %s\n' "$1" "$2"; }

echo "== granting runtime permissions =="
for perm in CAMERA \
            READ_SMS SEND_SMS RECEIVE_SMS RECEIVE_MMS \
            READ_PHONE_STATE READ_CONTACTS READ_CALL_LOG POST_NOTIFICATIONS; do
  "${ADB[@]}" shell pm grant "$PKG" "android.permission.$perm" 2>/dev/null \
    && say "$perm" granted || say "$perm" "skipped (not applicable)"
done

echo "== notification access (EARS + media tools) =="
"${ADB[@]}" shell cmd notification allow_listener \
  "$PKG/org.kde.kdeconnect.plugins.notifications.NotificationReceiver" >/dev/null
say "notification listener" granted

echo "== remote keyboard (kde_send_text) =="
# Enable the IME so it's AVAILABLE. We intentionally do NOT make it the active
# keyboard (that would displace Gboard for normal typing). Activate it only while
# you want remote typing, then switch back:
#   adb shell ime set $PKG/org.kde.kdeconnect.plugins.remotekeyboard.RemoteKeyboardService
#   adb shell ime set com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME
"${ADB[@]}" shell ime enable \
  "$PKG/org.kde.kdeconnect.plugins.remotekeyboard.RemoteKeyboardService" >/dev/null
say "remote keyboard IME" "enabled (not active — toggle on demand)"

echo "== cleanup: stray accessibility floating button =="
# KDE Connect's remote-input plugin can leave an accessibility floating button
# pinned to the screen edge even when the service is off. Clear it if present.
targets=$("${ADB[@]}" shell settings get secure accessibility_button_targets 2>/dev/null | tr -d '\r')
if [[ "$targets" == *"$PKG"* ]]; then
  "${ADB[@]}" shell settings delete secure accessibility_button_targets >/dev/null
  say "floating a11y button" "cleared"
else
  say "floating a11y button" "none"
fi

echo
echo "Done. Permissions persist across reboots. KDE Connect must still be open on"
echo "the phone, on home Wi-Fi, and paired for the MCP tools to reach it."
