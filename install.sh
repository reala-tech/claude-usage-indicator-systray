#!/usr/bin/env bash
# Installs a launcher + KDE autostart entry for the tray app, then (re)starts it.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
chmod +x "$here/claude_usage_tray.py" "$here/add-account.sh"
desktop="[Desktop Entry]
Type=Application
Name=Claude Usage Tray
Comment=Show Claude plan usage in the system tray
Exec=/usr/bin/python3 \"$here/claude_usage_tray.py\"
Icon=utilities-system-monitor
Terminal=false
Categories=Utility;
X-KDE-autostart-after=panel"
mkdir -p ~/.local/share/applications ~/.config/autostart
echo "$desktop" > ~/.local/share/applications/claude-usage-tray.desktop
echo "$desktop" > ~/.config/autostart/claude-usage-tray.desktop

# Restart: stop any running copy, then launch fully detached from this terminal
# (no TTY, own session) so shell job control can never freeze it.
pkill -f "^[^ ]*python3 [^ ]*claude_usage_tray\.py" 2>/dev/null && sleep 1 || true
setsid -f /usr/bin/python3 "$here/claude_usage_tray.py" </dev/null >/dev/null 2>&1
sleep 2
if pgrep -f "^[^ ]*python3 [^ ]*claude_usage_tray\.py" >/dev/null; then
  echo "Installed and running. It also starts automatically at login."
else
  echo "Installed, but the tray failed to start. Run it in the foreground to see why:" >&2
  echo "  python3 \"$here/claude_usage_tray.py\"" >&2
  exit 1
fi
