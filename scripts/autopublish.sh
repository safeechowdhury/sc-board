#!/bin/zsh
# Turn the scheduled publish on or off on this Mac (macOS launchd).
#
#   scripts/autopublish.sh install     publish and upload on the schedule below
#   scripts/autopublish.sh uninstall   stop
#   scripts/autopublish.sh status      is it on, and the end of its log
#   scripts/autopublish.sh run         one scheduled run now, as launchd would do it
#
# Each run is `python -m props publish --upload`: refresh the logs (once a day), build every
# board, grade (once a day), and send what changed to the host over FTPS (FTP_* in .env).
# A run where most books fail (a VPN on, the network down) uploads nothing.
# A run missed while the Mac sleeps happens when it wakes. Times are this Mac's local time.
set -euo pipefail

LABEL="ca.scboard.publish"
REPO="${0:A:h:h}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/sc-board-publish.log"
# 5:07 AM for last night's results, then hourly at :07 from 11:07 AM to 1:07 AM
HOURS=(5 11 12 13 14 15 16 17 18 19 20 21 22 23 0 1)
MINUTE=7

case "${1:-}" in
  install)
    grep -q '^FTP_SERVER=' "$REPO/.env" 2>/dev/null || { echo "Add FTP_SERVER, FTP_USERNAME and FTP_PASSWORD to $REPO/.env first"; exit 1; }
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    {
      echo '<?xml version="1.0" encoding="UTF-8"?>'
      echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
      echo '<plist version="1.0"><dict>'
      echo "  <key>Label</key><string>$LABEL</string>"
      echo "  <key>ProgramArguments</key><array><string>$REPO/scripts/autopublish.sh</string><string>run</string></array>"
      echo "  <key>WorkingDirectory</key><string>$REPO</string>"
      echo "  <key>StandardOutPath</key><string>$LOG</string>"
      echo "  <key>StandardErrorPath</key><string>$LOG</string>"
      echo '  <key>StartCalendarInterval</key><array>'
      for hour in $HOURS; do echo "    <dict><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>$MINUTE</integer></dict>"; done
      echo '  </array>'
      echo '</dict></plist>'
    } > "$PLIST"
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$PLIST"
    echo "On: publishing at :$MINUTE past hours ${HOURS[*]}. Log: $LOG"
    ;;
  uninstall)
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Off."
    ;;
  status)
    if launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1; then echo "On."; else echo "Off."; fi
    [[ -f "$LOG" ]] && tail -n 25 "$LOG"
    ;;
  run)
    cd "$REPO"
    export PYTHONUNBUFFERED=1   # the log shows each line as it happens
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') publish"
    "$REPO/.venv/bin/python" -m props publish --upload
    ;;
  *)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
