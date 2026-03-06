#!/bin/bash
# fix-python-lan-uuid.sh
#
# Fixes macOS Sequoia silently blocking Python's Local Network access after
# Homebrew upgrades. When brew updates Python, the binary's Mach-O UUID changes.
# macOS ties TCC local network grants to this UUID, so the old grant no longer
# matches and LAN connections silently fail (errno 65: No route to host).
#
# This script:
#   1. Reads the current UUID from the Homebrew Python binary
#   2. Checks if that UUID is already in the networkextension UUID cache
#   3. If not, adds it under org.python.python
#   4. Tells you to re-toggle Local Network permission in System Settings
#
# Run with sudo after any `brew upgrade python`.
# Can also be wired into a brew post-upgrade hook.

set -euo pipefail

PLIST="/Library/Preferences/com.apple.networkextension.uuidcache.plist"
BUNDLE_ID="org.python.python"

# Find the Homebrew Python binary (resolve all symlinks)
PYTHON_BIN="$(readlink -f "$(brew --prefix)"/bin/python3 2>/dev/null || true)"

if [[ -z "$PYTHON_BIN" || ! -f "$PYTHON_BIN" ]]; then
    echo "ERROR: Could not find Homebrew Python binary."
    echo "       Tried: $(brew --prefix)/bin/python3"
    exit 1
fi

echo "Python binary: $PYTHON_BIN"
echo "Python version: $("$PYTHON_BIN" --version 2>&1)"

# Extract the Mach-O LC_UUID
CURRENT_UUID="$(otool -l "$PYTHON_BIN" 2>/dev/null | awk '/LC_UUID/{getline; getline; print $2}')"

if [[ -z "$CURRENT_UUID" ]]; then
    echo "ERROR: Could not extract LC_UUID from $PYTHON_BIN"
    exit 1
fi

echo "Current UUID:  $CURRENT_UUID"

# Check if we need sudo
if [[ $EUID -ne 0 ]]; then
    echo ""
    echo "ERROR: This script must be run with sudo."
    echo "  sudo $0"
    exit 1
fi

# Check if the UUID is already in the plist
ALREADY_PRESENT="$(python3 -c "
import plistlib, uuid, sys

with open('$PLIST', 'rb') as f:
    plist = plistlib.load(f)

mappings = plist.get('uuid-mappings', {})
entries = mappings.get('$BUNDLE_ID', [])

target = uuid.UUID('$CURRENT_UUID')
for entry in entries:
    if uuid.UUID(bytes=entry) == target:
        print('yes')
        sys.exit(0)

print('no')
" 2>/dev/null)"

if [[ "$ALREADY_PRESENT" == "yes" ]]; then
    echo ""
    echo "UUID is already registered in the cache. No changes needed."
    echo ""
    echo "If LAN access is still broken, try:"
    echo "  1. System Settings > Privacy & Security > Local Network"
    echo "  2. Toggle Python OFF, then back ON"
    echo "  3. Restart your Python services"
    exit 0
fi

# Add the new UUID to the plist
echo ""
echo "UUID not found in cache. Adding it now..."

python3 -c "
import plistlib, uuid

with open('$PLIST', 'rb') as f:
    plist = plistlib.load(f)

mappings = plist.setdefault('uuid-mappings', {})
entries = mappings.setdefault('$BUNDLE_ID', [])

new_uuid = uuid.UUID('$CURRENT_UUID')
entries.append(new_uuid.bytes)

with open('$PLIST', 'wb') as f:
    plistlib.dump(plist, f)

print('Added UUID $CURRENT_UUID to $BUNDLE_ID')
"

echo ""
echo "UUID cache updated. Now you need to:"
echo ""
echo "  1. Open System Settings > Privacy & Security > Local Network"
echo "  2. Find Python in the list"
echo "  3. Toggle it OFF, then back ON"
echo "  4. Restart your Python services:"
echo "     launchctl kickstart -k gui/\$(id -u)/com.jarvis.test.backend"
echo "     launchctl kickstart -k gui/\$(id -u)/com.kukuibot.server"
echo ""
echo "Done."
