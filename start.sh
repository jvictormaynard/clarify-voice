#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_dir"

if [[ "$(uname -s)" == "Linux" ]]; then
  missing=()
  for command in sox xclip xdotool; do
    command -v "$command" >/dev/null 2>&1 || missing+=("$command")
  done
  if ((${#missing[@]})); then
    echo "Missing system tools: ${missing[*]}"
    echo "On Debian or Ubuntu: sudo apt install sox libsox-fmt-all xclip xdotool python3-tk portaudio19-dev"
    exit 1
  fi
fi

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

exec .venv/bin/python app.py
