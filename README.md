# Superbot 🤖

**Your own Claude Code, in your pocket.** A self-hosted Telegram bot that gives you full,
private access to Claude Code from anywhere — with tools, persistent memory, and proactive
server alerts. One bot, one owner, your machine.

```
You (Telegram) ──▶ Superbot ──▶ claude -p --resume <session> ──▶ your machine
                     │                                                (Bash, files,
                     │                                                 MCP, anything)
                     ▼
              rich-text replies, photos in, files out
```

## Why

- **Full power**: unlike chat-app wrappers, Superbot runs Claude Code with real tools —
  it can read/edit files, run commands, and use your MCP servers, from your phone.
- **One brain**: the session is a real Claude Code session. Continue the *same*
  Telegram conversation in your terminal:
  `claude --resume $(cat ~/.config/superbot/session)`
- **It learns — without bloating tokens**: a workspace of `SOUL.md` (persona),
  `USER.md` (you), `AGENTS.md` (operating rules) plus long-term memory. Sessions
  auto-rotate when they grow large or go idle: a cheap reflection pass saves lessons to
  memory and leaves a short `HANDOFF.md` for the next session. Audit with `/brain`,
  remove with `/forget <name>`.
- **Live remote shell**: see what Claude is doing (`🔧 Bash: docker ps`) as it works,
  `/stop` it any time, keep typing while it works (messages are merged into one round).
- **Status at a glance**: every message gets a reaction: 👀 seen → 👨‍💻 working → 👍 done
  (💔 failed, stopped or dropped by `/stop`). Messages merged into one round share the status.
- **Threads by reply**: reply to an older answer and that round continues *that* task's
  session; the current conversation is left alone. Several tasks, one chat, no profiles.
- **Cheap by default**: every message runs on the light model (`auto_model.light`, e.g.
  Sonnet); start a message with `!` for the heavy one (e.g. Opus).
- **Any Anthropic-compatible API**: Anthropic, or z.ai GLM, Kimi, DeepSeek, OpenRouter… via
  `claude_env` / `env_file`.
- **It warns you**: a 15-minute timer checks disk usage, failed systemd units and dead
  docker containers, and messages you when things break — and when they recover.
  A heartbeat watchdog also alerts you if the bot itself goes silent for 10 minutes.
- **Private by design**: single owner, pairing-code claim, no third-party servers,
  no telemetry. Python stdlib only — zero pip dependencies.

## Requirements

- Linux with systemd (user session)
- Python ≥ 3.10
- [Claude Code CLI](https://claude.com/claude-code) — either logged in (`claude login`)
  or an Anthropic-compatible API key (z.ai, Kimi, DeepSeek, OpenRouter…); the installer asks
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Install

```bash
git clone https://github.com/zalldz/superbot && cd superbot
bash install.sh
```

The installer checks dependencies, asks for your bot token (and, if Claude Code isn't
logged in, for a third-party API URL + key, which it tests), installs systemd units,
runs the self-checks, starts the bot and prints your pairing command. Re-running it upgrades the code — your
config, memory and session are never touched.

**Claim ownership**: send the `/start <code>` line the installer prints to your bot in
Telegram (lost it? `cat ~/.config/superbot/pairing`). Whoever holds that code (i.e. whoever
can read the machine's journal) becomes the owner; everyone else is refused.

## Commands

| Command | What it does |
|---|---|
| `/new` | reflect (save lessons) → fresh session (long-term memory kept) |
| `/stop` | kill the running task and drop queued messages |
| `/model [name]` | show / switch model (`opus`, `sonnet`, `haiku`, `glm-4.6`…, `default`) |
| `/brain` | show the bot's long-term memory index |
| `/forget <name>` | remove one memory |
| `/id` | chat id, session id, model, context size, how to continue in terminal |
| `/health` | on-demand server check: disk, systemd units, docker |
| `/usage` | cost, turns, duration, tokens (today / 7 days / total) |
| `!message` | run this message on the heavy model (`auto_model.heavy`) |
| reply to an old answer | continue that task's session instead of the current one |
| send a photo | Claude looks at it (vision) |
| send a document | Claude reads any file you send (≤ 20 MB) |
| send a voice note | Claude transcribes it with a local whisper, if installed |

From scripts, cron or Claude itself:

```bash
python3 ~/.local/share/superbot/bot.py --send "backup done ✅"     # push a message
python3 ~/.local/share/superbot/bot.py --ask "summarize today's journal errors"  # AI task → answer sent to you
# e.g. a daily briefing at 07:00:
systemd-run --user --on-calendar='*-*-* 07:00' python3 ~/.local/share/superbot/bot.py --ask "morning brief: disk, docker, failed units"
```

Just ask the bot "remind me at 5pm to …" — it knows these commands. Schedule `--ask` with
`systemd-run`, not `at`: `at` copies the bot's environment and would skip the one-Claude lock.

## Workspace — `~/.config/superbot/`

Seeded on first run from `templates/` (never overwritten). Edit them freely — or let the
bot edit them; changes apply on the next message. Each is capped at 2000 chars because it
is sent with every message.

| File | Purpose |
|---|---|
| `SOUL.md` | persona, tone, boundaries |
| `USER.md` | who you are — the bot updates it as it learns stable facts |
| `AGENTS.md` | operating rules: learning loop, token discipline, shell etiquette |
| `HEARTBEAT.md` | optional proactive checklist (see `heartbeat_minutes`) |
| `HANDOFF.md` | written by the bot at session end — carried into the next session |
| `memory/` | long-term memory: one fact per file + `MEMORY.md` index |

### How it keeps tokens low

1. Workspace files and the memory **index** go into the system prompt (prompt-cached);
   memory bodies are only read when relevant.
2. Every message re-sends the whole session, so sessions are kept short: above
   `rotate_tokens` context or after `idle_hours` idle, a reflection on `reflect_model`
   (cheap) saves lessons + writes the handoff, then a fresh session starts.
3. Messages sent while Claude works are merged into one round.
4. `claude_args` can trim Claude Code's own baseline (measured: 30k → 13.5k tokens per
   message with the lean preset — see Configuration). The example config ships with it on.
5. Heartbeat is off by default, runs on a cheap model in a throwaway session, costs zero
   while `HEARTBEAT.md` has no items, and stays silent when all is well.

## Configuration — `~/.config/superbot/config.json`

```json
{
  "bot_name": "Superbot",
  "lang": "en",
  "extra_prompt": "You are sarcastic but helpful.",
  "permission_mode": "bypassPermissions",
  "claude_env": {},
  "model": "",
  "memory_dir": "",
  "env_file": "",
  "claude_args": ["--setting-sources", "local", "--strict-mcp-config", "--disable-slash-commands"],
  "progress": true,
  "rotate_tokens": 60000,
  "idle_hours": 8,
  "reflect_model": "haiku",
  "heartbeat_minutes": 0,
  "heartbeat_model": "haiku",
  "media_days": 7,
  "confirm_patterns": ["\\b(mkfs\\S*|wipefs|fdisk|parted|sgdisk)\\b", "…"],
  "confirm_timeout": 600,
  "auto_model": {"light": "sonnet", "heavy": "opus"}
}
```

| Key | Meaning |
|---|---|
| `model` | passed as `--model` (empty = Claude Code default) |
| `auto_model` | `light` runs every message, `heavy` runs messages starting with `!`; `/model` overrides `light` |
| `claude_args` | extra CLI flags. `--disable-slash-commands` drops the skills list (≈ -35% tokens). Lean preset: `["--setting-sources", "local", "--strict-mcp-config", "--disable-slash-commands"]` (≈ -55%; also drops user-level plugins, hooks and MCP — add `"--mcp-config", "~/mcp.json"` to keep the servers you need) |
| `progress` | live tool-status message while Claude works |
| `rotate_tokens` / `idle_hours` | when to reflect + start a fresh session |
| `reflect_model` / `heartbeat_model` | cheap model for background passes |
| `heartbeat_minutes` | run `HEARTBEAT.md` every N minutes (0 = off) |
| `media_days` | delete downloaded files older than this |
| `confirm_patterns` / `confirm_timeout` | regexes on Bash commands that need your ✅ first (see Security) |

`lang` also accepts `"id"` (Bahasa Indonesia). `claude_env` is passed to the claude
process — use it for `ANTHROPIC_API_KEY` etc. if you don't use `claude login`.
`model` is passed as `--model` (empty = Claude Code's default).

### Third-party Anthropic-compatible APIs (z.ai GLM, Kimi, DeepSeek, OpenRouter, …)

Any endpoint that speaks the Anthropic Messages API works — point Claude Code at it:

```json
"claude_env": {
  "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
  "ANTHROPIC_AUTH_TOKEN": "your-zai-key",
  "API_TIMEOUT_MS": "3000000",
  "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-4.6",
  "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-4.6",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-4.5-air"
}
```

Or keep the keys in your shell rc / `.env` and set `"env_file": "~/.zshrc"` —
`ANTHROPIC_*`, `CLAUDE_CODE_*`, `DISABLE_*` and `API_TIMEOUT_MS` lines are picked up
(`export` optional, quoted or not). `claude_env` overrides `env_file`.

Notes: the startup log shows which endpoint is active (`journalctl --user -u superbot -n 5`).
On a non-Anthropic endpoint `/usage` counts tokens but no `$` (Claude Code prices
everything at Anthropic rates).

Optional integrations (all default off):

- **`memory_dir`** — custom memory dir (default `~/.config/superbot/memory`). Point it
  at your terminal Claude Code memory dir (e.g. `~/.claude/projects/<project>/memory`)
  and the bot shares one brain with your terminal sessions: memories written from
  Telegram appear in the terminal and vice versa.
- **`env_file`** — a shell rc / `.env` file (e.g. `"~/.zshrc"`) to parse Claude Code
  env lines from (see above), so you don't duplicate keys in `claude_env`.

## Optional: vector memory

Memory files stay plain Markdown (the index is sent with every message). For semantic recall over
hundreds of them, mirror the directory into any local vector store with a CLI, and tell the bot how
to query it in `AGENTS.md`, e.g.:

```markdown
- For past context the index doesn't cover, run `memrecall "<question>"` before saying you don't know.
```

Keep the embedding model small on a 4 GB box (MiniLM-class, ~384 dims) and run the search in its
own capped scope (`systemd-run --user --scope -p MemoryMax=900M …`) so it never counts against the
bot's round cap. Index new files with a nightly timer that takes the same `claude.lock`.

## Small servers (4 GB RAM)

One Claude Code round is ~400 MB, and every MCP server it loads adds 50–120 MB more. Without
limits a busy bot plus a terminal session can push a small box into swap until it hangs.

- The lean preset (`--strict-mcp-config`, in the example config) starts **no** MCP servers in
  Telegram rounds. Add only the ones you need: `"--mcp-config", "~/mcp-telegram.json"`.
- The systemd unit caps the bot and everything it spawns (`MemoryHigh=900M`, `MemoryMax=1200M`,
  `MemorySwapMax=256M`). Over the cap only that Claude round is killed; the bot and the
  server stay up. Raise the numbers on bigger machines (`systemctl --user edit superbot`).
- One Claude at a time: every round holds an flock on `~/.config/superbot/claude.lock`, and
  `--ask` waits for it too. Other scripts that run `claude` should take the same lock
  (`flock ~/.config/superbot/claude.lock claude -p …`), so the bot and your scripts never
  load two sessions at once.
- Use one session, not two: continue the Telegram session in the terminal
  (`claude --resume $(cat ~/.config/superbot/session)`) instead of a second `claude`.
- `whisper_model` loads per voice note (~150–300 MB while it runs); set it to `""` if you
  don't send voice notes.

## ⚠️ Security — read this

**Approval buttons.** Bash commands matching `confirm_patterns` pause and send you
`⚠️ Claude wants to run: …` with ✅ Run / ⛔ Deny buttons. No answer within
`confirm_timeout`, or Telegram unreachable → that command is not run (fail-closed), but the
round goes on: Claude skips the step, finishes the rest and lists what it skipped in its final
report. It is a PreToolUse hook, so it works even under `bypassPermissions`.

The example config asks only for what is hard to undo: `rm -r` on `/`, `~`, a home or top-level
dir, mkfs/wipefs/fdisk, `dd of=/dev/…`, reboot/poweroff, docker prune / volume removal,
force-push / `reset --hard` / `clean -f`, `curl | sh`, `DROP TABLE`, `crontab -r`, `chmod -R /`,
package removal and firewall changes. Everyday work (service restarts, `rm -rf node_modules`,
plain `git push`, reads like `ufw status`) never asks; `test_bot.py` checks both lists.
Regexes are a guard rail, not a sandbox.

**Secret redaction.** Every outgoing message and file is scrubbed of the bot token and of
any env value whose name contains KEY / TOKEN / SECRET / PASSWORD.

**Your Telegram chat is a root shell on this machine.** The bot runs Claude Code with
`bypassPermissions`: any message you send can execute commands with your user's rights.
That is the point — but treat your Telegram account accordingly (enable 2FA, check
active sessions).

- Ownership is locked to one chat id, claimed via a pairing code printed only in the
  machine's journal.
- Only one machine may poll a given bot token at a time (Telegram limitation) — running
  the bot on two hosts against one token causes conflicts.
- For extra isolation, run the service under a dedicated user or in a container, and
  set `permission_mode` in the config to something tighter if you prefer.
- All data (session, memory, media) lives in `~/.config/superbot/` — delete it and the
  bot forgets everything.

## Moving to another machine

1. On the old machine: `systemctl --user disable --now superbot superbot-alert.timer`
2. Copy `~/.config/superbot/` (token, owner, session, memory) to the new machine
3. Clone & `bash install.sh` — same bot identity, same brain.

## FAQ

**Do I need a new bot for each machine?** Yes, if you want them running *simultaneously*
(one token = one polling process). To *move*, just carry the config dir.

**Does it cost anything?** Superbot is free; conversations use your Claude plan/API
credits like any Claude Code session.

**Voice notes?** Sent to Claude as a file with an instruction to transcribe it — works
when a local speech-to-text CLI is installed (`pipx install openai-whisper` or
`faster-whisper`).

## Roadmap

- [x] Tool-approval buttons (approve dangerous commands from Telegram)
- [x] Built-in voice transcription (faster-whisper) · [ ] TTS replies
- [ ] Memory sync across devices

## License

MIT — see [LICENSE](LICENSE).
