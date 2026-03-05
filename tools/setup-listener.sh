#!/bin/bash
# KukuiBot Wake Listener Setup for Room Macs
# Usage: ./setup-listener.sh "Room Name" "https://kukuibot-url:7000"
set -e
ROOM="${1:?Usage: $0 \"Room Name\" \"https://kukuibot-url:7000\"}"
KUKUIBOT_URL="${2:?Usage: $0 \"Room Name\" \"https://kukuibot-url:7000\"}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== KukuiBot Wake Listener Setup ==="
echo "Room: $ROOM"
echo "KukuiBot: $KUKUIBOT_URL"
echo ""

# Check Apple Silicon
if [[ "$(uname -m)" != "arm64" ]]; then
    echo "ERROR: This script requires Apple Silicon (M1/M2/M3/M4)"
    exit 1
fi

# Install Python deps
echo "Installing Python dependencies..."
pip3 install --user mlx-qwen3-asr vosk numpy pyaudio 2>/dev/null || {
    echo "WARNING: Some packages failed. Trying with brew python..."
    /opt/homebrew/bin/pip3 install mlx-qwen3-asr vosk numpy pyaudio
}

# Pre-download ASR model
echo "Pre-downloading Qwen3-ASR model (~1.8 GB)..."
python3 -c "from mlx_qwen3_asr import Session; Session(model='Qwen/Qwen3-ASR-0.6B'); print('ASR model ready')"

# Download Vosk model if not present
VOSK_MODEL="$HOME/jarvis-voice/models/vosk-model-small-en-us-0.15"
if [ ! -d "$VOSK_MODEL" ]; then
    echo "Downloading Vosk model..."
    mkdir -p "$HOME/jarvis-voice/models"
    cd "$HOME/jarvis-voice/models"
    curl -LO https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
    unzip vosk-model-small-en-us-0.15.zip
    rm vosk-model-small-en-us-0.15.zip
fi

# Create launchd plist
PLIST="$HOME/Library/LaunchAgents/com.kukuibot.wake-listener.plist"
echo "Creating launchd plist: $PLIST"
cat > "$PLIST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kukuibot.wake-listener</string>
    <key>ProgramArguments</key>
    <array>
        <string>python3</string>
        <string>${SCRIPT_DIR}/wake-listener.py</string>
        <string>--room</string>
        <string>${ROOM}</string>
        <string>--kukuibot-url</string>
        <string>${KUKUIBOT_URL}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${HOME}/Library/Logs/kukuibot-wake-listener.log</string>
    <key>StandardErrorPath</key>
    <string>${HOME}/Library/Logs/kukuibot-wake-listener.log</string>
</dict>
</plist>
PLIST_EOF

echo ""
echo "=== Setup Complete ==="
echo "Listener plist created: $PLIST"
echo "To start: launchctl load $PLIST"
echo "Logs: ~/Library/Logs/kukuibot-wake-listener.log"
echo "To update: cd $(dirname "$SCRIPT_DIR") && git pull"
