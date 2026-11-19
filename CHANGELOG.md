# Changelog

## 2.5.0 — 2026-11-20

- Memory index rule: under 200 lines (Claude Code's read limit), merge duplicates, never delete a memory
  just to save space (was: under 60 lines, prune — risky once `memory_dir` points at a shared dir)
- **Status reactions**: 👀 seen → 👨‍💻 working → 👍 done, 💔 failed / stopped / dropped
- **Reply = resume**: replying to an older answer continues that answer's session for the round;
  the current session, its context counter and rotation are left alone (`threads.json`, last 500)
- **`!` = heavy model**: every message runs on `auto_model.light`; `!message` on `auto_model.heavy`
  (was: a keyword guess once per session). `/model` overrides light
- **One Claude at a time**: rounds, `--ask` and heartbeat hold an flock on
  `~/.config/superbot/claude.lock`; other scripts can share it
- Removed (unused): `--profile` / `superbot@`, groups and
  bot-to-bot (`group_reply`, `allow_bots`, `bot_loop_guard`), `team_context_file`, `--ask --to/--from`,
  `/kantor` (`office_url`, `office_token_file`), `/provider` and `providers` / `fallback_provider`.
  The bot answers private messages from the owner only
- Template AGENTS.md: repeatable procedures are saved as `howto-<topic>` memories


- systemd units cap RAM (`MemoryHigh=900M`, `MemoryMax=1200M`, `MemorySwapMax=256M`): a runaway
  Claude round is killed instead of swapping a 4 GB server into a hang
- Example config uses the lean preset (`--setting-sources local --strict-mcp-config`): no MCP
  servers per Telegram round
- CI: self-checks on every push and PR (GitHub-hosted, no self-hosted runner)
- Approvals ask only for what is hard to undo (new example `confirm_patterns`, checked by a test);
  restarts, `rm -rf node_modules`, plain `git push`, `ufw status` no longer ask
- A denied or unanswered approval skips only that command: Claude finishes the rest and lists the
  skipped step in its final report (was: the whole round stopped to ask)
- `confirm_timeout` 300 → 600 s, round timeout 900 → 1800 s; unread approval files older than 1 h are swept
- Template AGENTS.md / SOUL.md: work a task to the end and report once; no asking before normal steps
- Tests run in their own HOME with the network off (they used to write into the live
  `~/.config/superbot` and call Telegram)

## 2.3.0 — 2026-11-07

- `auto_model`: model picked once per new session (light for chat, heavy for coding/long/file tasks);
  `/model` still overrides; never leaks into the fallback provider
- `quick_commands`: owner shell shortcuts without a Claude round (0 tokens); `/menu` = reply keyboard
- `whisper_model`: voice notes transcribed locally with faster-whisper (ffmpeg decode, no PyAV)

## 2.2.0 — 2026-11-07

- `/kantor`: one-time HMAC login link to the 3D office (Claw3D + `body/` login patch), private chat
  only, link preview off so Telegram cannot burn it; config `office_url`, `office_token_file`

## 2.1.0 — 2026-11-06

- **Virtual office**: `--profile NAME` runs extra agents (own token, SOUL.md, memory, session in
  `~/.config/superbot-NAME`), systemd template `superbot@NAME`, `install.sh --profile NAME`
- Group chats: owner-only, answers when @mentioned or replied to (`group_reply: "all"` → every message)
- Delegation CLI: `--ask … --to CHAT --from AGENT` posts under the agent's own bot, prints the
  answer for the caller, and marks the task as non-delegable (anti-loop)
- Bot-to-bot in groups (Telegram bot-to-bot mode): `allow_bots` allowlist, explicit @mention only
  (bot replies ignored), mentions of other bots only are skipped, per-chat `bot_loop_guard`
  sliding window + cooldown; bot messages framed as untrusted colleague requests
- Profile-aware approval hook and `--send`/`--ask` instructions; team context cap 4000 chars

## 2.0.0 — 2026-11-06

- **Third-party APIs**: any Anthropic-compatible endpoint (z.ai, Kimi, DeepSeek, OpenRouter…).
  `env_file` now reads `ANTHROPIC_*`, `CLAUDE_CODE_*`, `DISABLE_*`, `API_TIMEOUT_MS`,
  with or without `export`, single/double/no quotes; `claude_env` overrides it
- `providers` presets + `/provider` switch + `fallback_provider` (retry on 429/5xx)
- **Workspace**: `SOUL.md`, `USER.md`, `AGENTS.md`, `HEARTBEAT.md`, `HANDOFF.md`, seeded
  from `templates/`; injected via `--append-system-prompt` on every call (always current,
  survives compaction) instead of the first message
- **Learning loop without token bloat**: auto session rotation on `rotate_tokens` /
  `idle_hours` with a cheap reflection pass (lessons → memory, summary → `HANDOFF.md`);
  `/new` reflects too
- `claude_args` passthrough (lean preset measured 30k → 13.5k tokens/message)
- Live progress (`🔧 Bash: …`) via stream-json, `/stop`, background worker; messages sent
  while busy get a 👀 and are merged into one round
- Optional LLM heartbeat (`heartbeat_minutes`): silent `HEARTBEAT_OK`, zero cost when empty
- CLI: `bot.py --send` / `--ask` for cron, timers and the bot's own reminders
- Voice notes / audio / video accepted as files; media auto-cleanup (`media_days`)
- `/usage` shows tokens; no fake `$` on third-party endpoints
- **Approval buttons**: `confirm_patterns` → PreToolUse hook asks ✅/⛔ in Telegram,
  fail-closed on timeout / unreachable (works under `bypassPermissions`)
- Secret redaction on every outgoing message/file (bot token, *KEY/*TOKEN/*SECRET env, provider keys)
- `/model` quick switch (persists, works per provider)
- Fixes: stale session id no longer bricks the bot (auto fresh session); prompt passed via
  stdin (a message starting with `-` was parsed as a CLI flag); config.json chmod 600
- Installer: asks for a third-party API (URL + key, tested) when Claude Code isn't logged in,
  links npm/nvm `claude` into `~/.local/bin` for the service, prints the `/start <code>` line

## 1.2.0 — 2026-11-05

- Documents in: send any file (≤ 20 MB) and Claude reads it — sanitized local path,
  friendly refusal for oversize files
- `/health` — on-demand disk/systemd/docker check reusing `alert_check`
  (read-only: alert dedup state untouched)
- `/usage` — cost, turns and duration per claude round recorded to `usage.jsonl`,
  aggregated as today / 7 days / total (file auto-resets at 1 MB)
- Bot watchdog: heartbeat file touched every 30 s; the 15-minute alert timer now
  also fires when superbot itself is hung > 10 min (and when it recovers)

## 1.1.0 — 2026-11-05

- New optional config keys (all default off):
  - `memory_dir` — share long-term memory with your terminal Claude Code
  - `env_file` — parse `ANTHROPIC_*` exports from a shell rc instead of `claude_env`
  - `team_context_file` — inject a team/agent registry file into every new session

## 1.0.0 — 2026-11-05

First public release.

- Full Claude Code bridge for Telegram: persistent session (`--resume`), tools,
  MCP servers, photos in, files out
- Shared brain: continue the Telegram session in your terminal
- Long-term memory (one fact per file, index injection, `/brain`, `/forget`)
- Rich-text delivery: native rich messages, chunked-HTML fallback
- Proactive alerts every 15 min: disk ≥85%, failed systemd units, dead docker
  containers (deduped: alert + recovery)
- Pairing-code ownership claim, single-owner lock
- Idempotent installer, systemd units, self-check test suite, CI
