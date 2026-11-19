#!/usr/bin/env bash
# Edit here, then run this: self-checks -> copy code to the live dir -> restart. Config/workspace untouched.
set -euo pipefail
cd "$(dirname "$0")"
python3 test_bot.py | tail -1
rsync -a --exclude __pycache__ --exclude .git --exclude .github ./ "$HOME/.local/share/superbot/"
systemctl --user restart superbot.service
sleep 2; systemctl --user is-active superbot.service
