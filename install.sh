#!/usr/bin/env bash
# Superbot installer — idempotent (re-run to upgrade; config & memory untouched).
set -euo pipefail

# usage: ./install.sh
CODE_DIR="$HOME/.local/share/superbot"
CFG_DIR="$HOME/.config/superbot"
SVC="superbot"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- dependencies ---------------------------------------------------------
command -v python3 >/dev/null || die "python3 not found — install Python 3.10+"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || die "python3 too old (need >= 3.10)"
command -v claude >/dev/null \
  || die "claude CLI not found — install Claude Code: https://claude.com/claude-code"
systemctl --user status >/dev/null 2>&1 || die "systemd user session not available"

# the service runs with a fixed PATH — make claude reachable from ~/.local/bin (npm / nvm installs)
CLAUDE_BIN="$(command -v claude)"
if [ "$CLAUDE_BIN" != "$HOME/.local/bin/claude" ] && [ ! -e "$HOME/.local/bin/claude" ]; then
  mkdir -p "$HOME/.local/bin"
  ln -s "$CLAUDE_BIN" "$HOME/.local/bin/claude"
  say "Linked $CLAUDE_BIN → ~/.local/bin/claude (so the service can find it)"
fi

# ---- install code ---------------------------------------------------------
say "Installing code to $CODE_DIR"
mkdir -p "$CODE_DIR"
cp -r "$SRC_DIR"/. "$CODE_DIR"/
rm -rf "$CODE_DIR/.git" "$CODE_DIR/.github"

# ---- config ---------------------------------------------------------------
mkdir -p "$CFG_DIR"
if [ ! -f "$CFG_DIR/config.json" ]; then
  cp "$SRC_DIR/config.example.json" "$CFG_DIR/config.json"
  say "Config written: $CFG_DIR/config.json (bot_name, lang, extra_prompt)"
fi
chmod 600 "$CFG_DIR/config.json"   # may hold API keys (claude_env)

# ---- API access -----------------------------------------------------------
# configured already (claude_env)?  else: claude login works?  else: offer a 3rd-party API
HAS_API=$(python3 -c "import json,sys; c=json.load(open(sys.argv[1])); print(int(bool(c.get('claude_env'))))" "$CFG_DIR/config.json")
if [ "$HAS_API" = 0 ] && ! claude -p "ok" >/dev/null 2>&1 </dev/null; then
  say "Claude Code isn't logged in. Choose how the bot reaches a model:"
  echo "   1) Anthropic account — run 'claude login' yourself after this installer"
  echo "   2) Anthropic-compatible API (z.ai GLM, Kimi, DeepSeek, OpenRouter, …)"
  read -r -p "Choice [1/2]: " CHOICE || CHOICE=1
  if [ "$CHOICE" = 2 ]; then
    read -r -p "Base URL (z.ai: https://api.z.ai/api/anthropic): " BASE_URL
    read -r -s -p "API key (hidden): " API_KEY; echo
    read -r -p "Model (optional, e.g. glm-4.6 — Enter to skip): " MODEL
    python3 - "$CFG_DIR/config.json" "$BASE_URL" "$API_KEY" "$MODEL" <<'EOF'
import json, sys
path, url, key, model = sys.argv[1:]
c = json.load(open(path))
c["claude_env"] = {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_AUTH_TOKEN": key, "API_TIMEOUT_MS": "3000000"}
if model:
    c["model"] = model
json.dump(c, open(path, "w"), indent=2)
EOF
    if ANTHROPIC_BASE_URL="$BASE_URL" ANTHROPIC_AUTH_TOKEN="$API_KEY" claude -p ${MODEL:+--model "$MODEL"} "ok" >/dev/null 2>&1 </dev/null; then
      say "API works ✅ (saved to claude_env in $CFG_DIR/config.json)"
    else
      say "WARNING: test call failed — check URL/key/model in $CFG_DIR/config.json (claude_env)"
    fi
  else
    say "Remember: run 'claude login' before chatting with the bot."
  fi
fi

if [ ! -f "$CFG_DIR/token" ]; then
  say "No bot token yet."
  say "  1. Open Telegram → talk to @BotFather → /newbot"
  say "  2. Copy the API token it gives you"
  read -r -p "Paste the token here (or Ctrl-C and write it to $CFG_DIR/token later): " TOKEN
  if [ -n "$TOKEN" ]; then
    printf '%s' "$TOKEN" > "$CFG_DIR/token"
    chmod 600 "$CFG_DIR/token"
  fi
fi

# ---- systemd --------------------------------------------------------------
say "Installing systemd user units"
mkdir -p "$HOME/.config/systemd/user"
cp "$SRC_DIR"/systemd/*.service "$SRC_DIR"/systemd/*.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now "$SVC.service" superbot-alert.timer 2>/dev/null \
  || systemctl --user enable "$SVC.service" superbot-alert.timer

# ---- verify ---------------------------------------------------------------
say "Running self-checks"
(cd "$CODE_DIR" && python3 test_bot.py >/dev/null && say "self-checks passed") \
  || die "self-checks failed — inspect with: cd $CODE_DIR && python3 test_bot.py"

say "Starting bot"
systemctl --user restart "$SVC.service"
sleep 2
systemctl --user is-active --quiet "$SVC.service" || die "service failed — check: journalctl --user -u $SVC -n 30"

echo
if [ -f "$CFG_DIR/owner" ]; then
  say "🎉 Superbot is running (already paired)."
else
  for _ in 1 2 3 4 5; do [ -f "$CFG_DIR/pairing" ] && break; sleep 1; done
  if [ -f "$CFG_DIR/pairing" ]; then
    say "🎉 Superbot is running. Claim it — send this to your bot in Telegram:"
    echo "   /start $(cat "$CFG_DIR/pairing")"
  else
    say "🎉 Superbot is running. Pairing code: cat $CFG_DIR/pairing  → send /start <code>"
  fi
fi
echo "   Personalize: $CFG_DIR/USER.md · SOUL.md · config.json"
echo "   Logs: journalctl --user -u $SVC -f"
