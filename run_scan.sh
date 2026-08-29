#!/bin/bash
# AI Pivot Monitor - scheduled scan wrapper
# Scans Mon-Fri 4:00 AM - 10:00 PM ET (pre-market through late after-hours)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python3"
SCRIPT="$SCRIPT_DIR/edgar_ai_pivot_monitor.py"
LOG="$SCRIPT_DIR/scan.log"

# Set your OpenAI API key in your environment or replace this line:
# export OPENAI_API_KEY="sk-..."
if [[ -z "$OPENAI_API_KEY" ]]; then
    echo "$(date): ERROR — OPENAI_API_KEY not set. Exiting." >> "$LOG"
    exit 1
fi
export OPENAI_MODEL="gpt-4o"

DOW=$(TZ="America/New_York" date +%u)
HOUR=$((10#$(TZ="America/New_York" date +%H)))

if [[ "$DOW" -ge 1 && "$DOW" -le 5 && "$HOUR" -ge 4 && "$HOUR" -lt 22 ]]; then
    echo "$(date): Running scan..." >> "$LOG"
    "$PYTHON" "$SCRIPT" --days 1 >> "$LOG" 2>&1
    echo "$(date): Scan complete." >> "$LOG"
fi
