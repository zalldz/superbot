#!/usr/bin/env python3
"""Superbot — self-hosted Claude Code bridge for Telegram.

One bot, one owner, your machine. Long-polling Telegram Bot API →
`claude -p --resume <session-id>` (full tools) → rich-text reply.

Shared brain: the bot session id lives in ~/.config/superbot/session —
continue the same Telegram conversation in your terminal with:
    claude --resume $(cat ~/.config/superbot/session)

Workspace (~/.config/superbot): SOUL.md (persona), USER.md (who you are),
AGENTS.md (operating rules), HEARTBEAT.md (optional proactive checklist),
HANDOFF.md (last session's summary), memory/ (one fact per file, MEMORY.md
index). All are re-read and appended to the system prompt on every call, so
edits apply instantly and survive compaction.

Token discipline: only the memory *index* is injected (bodies are read on
demand); sessions auto-rotate on size/idle after a cheap reflection pass
that saves lessons to memory and leaves a short handoff; messages that pile
up while Claude works are merged into one round.

Threads: reply to an older bot answer and that round resumes the session that answer came
from, so several tasks can run side by side without touching the current session.

Models: "light" auto_model by default; start a message with "!" for the "heavy" one.

One Claude at a time: every round holds an flock on ~/.config/superbot/claude.lock; other
scripts that run `claude` can flock the same file to share the rule (RAM on small boxes).

CLI:  bot.py --send "text"    push a message to the owner (cron, scripts)
      bot.py --ask "prompt"   run a one-off Claude task, send the answer (also printed)

Private chat with the owner only. stdlib only. Python >= 3.10.
"""
import difflib
import fcntl
import glob
import json
import os
import queue
import re
import secrets
import signal
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid

HOME = os.path.expanduser("~")
CFG_DIR = f"{HOME}/.config/superbot"
TOKEN_FILE = f"{CFG_DIR}/token"
OWNER_FILE = f"{CFG_DIR}/owner"
PAIRING_FILE = f"{CFG_DIR}/pairing"
SESSION_FILE = f"{CFG_DIR}/session"
CONFIG_FILE = f"{CFG_DIR}/config.json"
MEDIA_DIR = f"{CFG_DIR}/media"
USAGE_FILE = f"{CFG_DIR}/usage.jsonl"
HEARTBEAT_FILE = f"{CFG_DIR}/heartbeat"
LAST_POLL = [time.time()]   # bumped after every getUpdates round-trip
STATE_FILE = f"{CFG_DIR}/state.json"          # model override, last activity, context size
THREADS_FILE = f"{CFG_DIR}/threads.json"      # bot message id → session id, for reply = resume
THREADS_KEEP = 500
CLAUDE_LOCK = f"{CFG_DIR}/claude.lock"        # one claude process at a time (shared with other scripts)
SOUL_MD, USER_MD, AGENTS_MD = f"{CFG_DIR}/SOUL.md", f"{CFG_DIR}/USER.md", f"{CFG_DIR}/AGENTS.md"
HEARTBEAT_MD, HANDOFF_MD = f"{CFG_DIR}/HEARTBEAT.md", f"{CFG_DIR}/HANDOFF.md"
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
SELF = os.path.abspath(__file__)
SELF_CMD = f"python3 {SELF}"   # how scripts/agents reach this bot
APPROVALS_DIR = f"{CFG_DIR}/approvals"
FILE_CAP = 2000        # chars per workspace file in the system prompt (≈500 tokens)
API = "https://api.telegram.org/bot{token}/"
TIMEOUT = 1800         # one round may run long tasks end to end (approval waits count too)
MSG_LIMIT = 4000       # telegram max 4096, keep a buffer
DOC_LIMIT = 3500       # above this, also attach the full answer as a file
TG_DL_LIMIT = 20 * 1024 * 1024   # telegram bot API download limit (getFile)
UNDO_DIR = f"{CFG_DIR}/undo"
USAGE_MAX_BYTES = 1024 * 1024    # usage.jsonl above this → reset

DEFAULT_CONFIG = {
    "bot_name": "Superbot",
    "lang": "en",                 # "en" | "id"
    "extra_prompt": "",           # appended to the identity prompt
    "permission_mode": "bypassPermissions",   # your machine, your bot
    "claude_env": {},             # e.g. {"ANTHROPIC_API_KEY": "..."} if not using `claude login`,
                                  # or a 3rd-party endpoint: ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN
    "model": "",                  # passed as --model (e.g. "sonnet", "glm-4.6"); empty = claude default
    "claude_args": [],            # extra claude CLI flags. Lean preset (≈ -55% tokens per message):
                                  # ["--setting-sources", "local", "--strict-mcp-config",
                                  #  "--disable-slash-commands"]  (+ "--mcp-config", "file.json" to keep some MCP)
    "progress": True,             # live "🔧 Bash: …" status while Claude works
    "rotate_tokens": 60000,       # context above this → reflect + start a fresh session
    "idle_hours": 8,              # idle longer than this → reflect + fresh session on next message
    "reflect_model": "haiku",     # cheap model for the end-of-session reflection
    "heartbeat_minutes": 0,       # >0: run HEARTBEAT.md checklist every N min (costs tokens; 0 = off)
    "heartbeat_model": "haiku",
    "media_days": 7,              # delete downloaded photos/files older than this
    "confirm_patterns": [],       # regexes on Bash commands that need your ✅ in Telegram first,
                                  # e.g. ["\\brm\\s+-\\w*r", "\\b(reboot|shutdown|mkfs|dd)\\b"]
    "confirm_paths": [],          # regexes on Edit/Write paths that need your ✅ (e.g. "/\\.ssh/", "^/etc/")
    "daily_budget_usd": 0,        # >0: warn once a day when priced spend passes this
    "morning_digest": "",         # "07:00": daily health+cost digest, no AI tokens
    "undo_keep": 20,              # rounds of Edit/Write snapshots kept for /undo and /diff
    "confirm_timeout": 600,       # seconds to wait for ✅; no answer = that step is skipped, the rest goes on
    "memory_dir": "",             # custom memory dir; empty = ~/.config/superbot/memory.
                                  # set it to your terminal Claude memory dir to share one brain
    "env_file": "",               # optional shell rc (e.g. "~/.zshrc") — ANTHROPIC_* exports are
                                  # parsed from it, so you don't duplicate keys in claude_env
    "auto_model": {},             # e.g. {"light": "sonnet", "heavy": "opus"}: every message runs on "light";
                                  # start it with "!" for "heavy". /model <name> overrides "light"
    "quick_commands": {},         # {"/disk": "df -h /"}: owner-only shell shortcuts, no AI, 0 tokens. /menu = buttons
    "whisper_model": "",          # faster-whisper model ("base", "small") → voice notes transcribed locally
}

STRINGS = {
    "en": {
        "welcome": "✅ You are now the owner of this bot — locked to your chat.\n"
                   "Hey! I'm {name} — ask me anything, send photos too 🤖\n"
                   "Start a message with ! for the heavy model; reply to an old answer to continue that task.\n"
                   "Commands: /new (fresh session) · /stop · /model · /brain · /forget <name> · /id · /health · /usage · /cost · /undo · /diff · /jobs · /ro",
        "has_owner": "This bot already has an owner 👋",
        "pairing_hint": "🔒 First run: send me the pairing code shown by the installer\n"
                        "(or `cat ~/.config/superbot/pairing`)\n"
                        "as: /start <code>",
        "private": "🔒 Private bot — owner only.",
        "new_session": "🧹 New session — fresh brain (long-term memory kept).",
        "brain_empty": "(memory is empty 🧠)",
        "forget_usage": "Usage: /forget <memory-name>",
        "forgot": "🗑 Removed {name} from memory.",
        "not_found": "Not found in memory: {name}",
        "photo_fail": "(failed to download photo 😕)",
        "doc_fail": "(failed to download the file 😕)",
        "doc_too_big": "(file too large — Telegram bots can only download 20 MB 😕)",
        "health": "🩺 Health check:\n{rows}",
        "health_disk": "disk",
        "health_units": "systemd units",
        "health_docker": "docker",
        "usage_head": "📊 Usage — {name}",
        "usage_row": "{label}: {cost} · {turns} turns · {dur} · {tok} tok",
        "usage_today": "today",
        "usage_week": "7 days",
        "usage_total": "total",
        "usage_empty": "(no usage recorded yet 📊)",
        "timeout": "(claude timed out after {t}s — try again / narrow the question)",
        "oom": "💥 Round killed (signal {sig}) — most likely the RAM cap (MemoryMax). Peak: ~{peak} MB. Try a smaller task.",
        "queued": "⏳ Queued (#{n}) — will run after the current round. /stop drops the queue.",
        "find_usage": "Usage: /find <word>",
        "find_none": "Nothing found for: {q}",
        "stopped": "⏹ Stopped.",
        "nothing_running": "Nothing is running.",
        "rotating": "🧠 Saving lessons from the old session…",
        "model_show": "🧠 Model: *{active}*\nSwitch: /model <opus|sonnet|haiku|name> · /model default",
        "model_set": "🧠 Model → *{active}*",
        "ro_on": "🔒 Read-only mode ON — Claude can look but not change anything. /ro off to leave.",
        "ro_off": "🔓 Read-only mode off.",
        "undo_none": "Nothing to undo.",
        "undo_done": "↩️ Reverted {n} file(s):\n{files}",
        "jobs_none": "No background jobs.",
        "budget_warn": "💸 Daily budget passed: ${cost:.2f} / ${cap:.2f}",
        "approve_ask": "⚠️ Claude wants to run:",
        "approved": "✅ approved",
        "denied": "⛔ denied",
    },
    "id": {
        "welcome": "✅ Kamu sekarang owner bot ini — terkunci ke chat kamu.\n"
                   "Halo! Aku {name} — tanya apa aja, kirim foto juga bisa 🤖\n"
                   "Awali pesan dengan ! untuk model berat; reply jawaban lama untuk melanjutkan tugas itu.\n"
                   "Command: /new (session baru) · /stop · /model · /brain · /forget <nama> · /id · /health · /usage · /cost · /undo · /diff · /jobs · /ro",
        "has_owner": "Bot sudah punya owner 👋",
        "pairing_hint": "🔒 Pertama kali: kirim kode pairing dari installer\n"
                        "(atau `cat ~/.config/superbot/pairing`)\n"
                        "format: /start <kode>",
        "private": "🔒 Bot privat — hanya owner.",
        "new_session": "🧹 Session baru — otak fresh (memori jangka panjang tetap).",
        "brain_empty": "(otak masih kosong 🧠)",
        "forget_usage": "Pakai: /forget <nama-memori>",
        "forgot": "🗑 {name} dihapus dari memori.",
        "not_found": "Gak ketemu di memori: {name}",
        "photo_fail": "(gagal download foto 😕)",
        "doc_fail": "(gagal download file 😕)",
        "doc_too_big": "(file kegedean — bot Telegram cuma bisa download 20 MB 😕)",
        "health": "🩺 Cek kesehatan:\n{rows}",
        "health_disk": "disk",
        "health_units": "unit systemd",
        "health_docker": "docker",
        "usage_head": "📊 Pemakaian — {name}",
        "usage_row": "{label}: {cost} · {turns} turn · {dur} · {tok} tok",
        "usage_today": "hari ini",
        "usage_week": "7 hari",
        "usage_total": "total",
        "usage_empty": "(belum ada data pemakaian 📊)",
        "timeout": "(claude timeout {t}s — coba lagi / persempit pertanyaan)",
        "oom": "💥 Ronde dibunuh (sinyal {sig}) — kemungkinan kena batas RAM (MemoryMax). Puncak: ~{peak} MB. Coba tugas lebih kecil.",
        "queued": "⏳ Antre (#{n}) — jalan setelah ronde ini. /stop buang antrean.",
        "find_usage": "Pakai: /find <kata>",
        "find_none": "Gak ketemu: {q}",
        "stopped": "⏹ Dihentikan.",
        "nothing_running": "Gak ada yang lagi jalan.",
        "rotating": "🧠 Menyimpan pelajaran dari session lama…",
        "model_show": "🧠 Model: *{active}*\nGanti: /model <opus|sonnet|haiku|nama> · /model default",
        "model_set": "🧠 Model → *{active}*",
        "ro_on": "🔒 Mode baca-saja AKTIF — Claude cuma boleh melihat. /ro off untuk keluar.",
        "ro_off": "🔓 Mode baca-saja mati.",
        "undo_none": "Tidak ada yang bisa di-undo.",
        "undo_done": "↩️ {n} file dikembalikan:\n{files}",
        "jobs_none": "Tidak ada job latar belakang.",
        "budget_warn": "💸 Batas harian terlewat: ${cost:.2f} / ${cap:.2f}",
        "approve_ask": "⚠️ Claude mau menjalankan:",
        "approved": "✅ diizinkan",
        "denied": "⛔ ditolak",
    },
}


def _s(key: str, **kw) -> str:
    txt = STRINGS.get(CONFIG["lang"], STRINGS["en"])[key]
    return txt.format(name=CONFIG["bot_name"], t=TIMEOUT, **kw)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


CONFIG = load_config()
MEMORY_DIR = os.path.expanduser(CONFIG["memory_dir"]) or f"{CFG_DIR}/memory"
MEMORY_INDEX = f"{MEMORY_DIR}/MEMORY.md"
ENV = dict(os.environ, PATH=f"{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin")
# vars Claude Code reads: auth/endpoint/model (ANTHROPIC_*), CLAUDE_CODE_*, API_TIMEOUT_MS, DISABLE_*
_ENV_LINE = re.compile(r"""\s*(?:export\s+)?((?:ANTHROPIC|CLAUDE_CODE|DISABLE)_\w+|API_TIMEOUT_MS)"""
                       r"""=(?:"([^"]*)"|'([^']*)'|([^\s#]*))""")


def env_from_file(env: dict):
    """Parse claude-related `[export] KEY=value` lines from a shell rc / .env file into env."""
    if not CONFIG["env_file"]:
        return
    try:
        with open(os.path.expanduser(CONFIG["env_file"])) as f:
            for line in f:
                m = _ENV_LINE.match(line)
                if m:
                    env[m.group(1)] = next(g for g in m.groups()[1:] if g is not None)
    except OSError:
        pass


env_from_file(ENV)
ENV.update({k: str(v) for k, v in CONFIG["claude_env"].items()})   # explicit config wins


def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


_STATE_LOCK = threading.Lock()


def update_state(**kw):
    with _STATE_LOCK:                       # poll thread (/model) and worker both write
        st = load_state()
        st.update(kw)
        with open(STATE_FILE + ".tmp", "w") as f:
            json.dump(st, f)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)   # atomic: a crash never leaves half a file


def third_party(env: dict) -> bool:
    return "anthropic.com" not in env.get("ANTHROPIC_BASE_URL", "anthropic.com")

TOKEN = ""
try:
    with open(TOKEN_FILE) as f:
        TOKEN = f.read().strip()
except OSError:
    print(f"no token yet: {TOKEN_FILE} — create a bot via @BotFather, then write the token there", flush=True)


def tg(method: str, **params) -> dict:
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(API.format(token=TOKEN) + method, data=data, timeout=60) as r:
        return json.load(r)


def tg_json(method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        API.format(token=TOKEN) + method,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


# ---------------------------------------------------------------- rich text
def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_INLINE = [
    (re.compile(r"`([^`\n]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*\n]+)\*\*"), r"<b>\1</b>"),
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"<i>\1</i>"),
    (re.compile(r"~~([^~\n]+)~~"), r"<s>\1</s>"),
    (re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)"), r'<a href="\2">\1</a>'),
    (re.compile(r"^#{1,6}\s+(.+)$"), r"<b>\1</b>"),
]


def _inline(s: str) -> str:
    for rx, repl in _INLINE:
        s = rx.sub(repl, s)
    return s


def md_to_html(md: str) -> str:
    """Markdown (Claude output) → Telegram HTML parse mode."""
    lines = md.split("\n")
    out, i = [], 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("```"):
            lang = l[3:].strip()
            body = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            cls = f' class="language-{_esc(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{_esc(chr(10).join(body))}</code></pre>")
        elif l.strip().startswith("|"):
            tbl = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i]):
                    tbl.append(lines[i])
                i += 1
            out.append(f"<pre>{_esc(chr(10).join(tbl))}</pre>")
            continue
        elif re.match(r"^\s*([-*_])\1{2,}\s*$", l):
            out.append("—" * 12)
            i += 1
        elif l.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i].lstrip("> "))
                i += 1
            out.append(f"<blockquote>{_inline(_esc(chr(10).join(quote)))}</blockquote>")
        else:
            m = re.match(r"^(\s*)[-*]\s+(.+)$", l)
            l = f"{m.group(1)}• {m.group(2)}" if m else l
            out.append(_inline(_esc(l)))
            i += 1
    return "\n".join(out)


def chunks_html(html: str):
    """Split on line boundaries; close & reopen dangling <pre>/<blockquote>."""
    out = []
    while len(html) > MSG_LIMIT:
        cut = html.rfind("\n", 0, MSG_LIMIT)
        if cut < MSG_LIMIT // 2:
            cut = MSG_LIMIT
        seg, html = html[:cut], html[cut:].lstrip("\n")
        closes = reopens = ""
        for tag in ("pre", "blockquote"):
            if seg.count(f"<{tag}>") > seg.count(f"</{tag}>"):
                closes += f"</{tag}>"
                reopens = f"<{tag}>" + reopens
        out.append(seg + closes)
        html = reopens + html
    out.append(html)
    return out


_SECRET_KEY = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.I)


def redact(text: str) -> str:
    """Mask secret values (bot token, *KEY/*TOKEN/*SECRET env vars) before they leave."""
    vals = {TOKEN, *(str(v) for k, v in ENV.items() if _SECRET_KEY.search(k))}
    for v in sorted((v for v in vals if len(v) >= 8), key=len, reverse=True):
        text = text.replace(v, "‹redacted›")
    return text


def _mid(resp) -> list[int]:
    m = (resp or {}).get("result") if isinstance(resp, dict) else None
    return [m["message_id"]] if isinstance(m, dict) and m.get("message_id") else []


def send(chat_id: str, text: str, reply_to: int | None = None) -> list[int]:
    """Send as a native rich message; fall back to chunked HTML, then plain. Returns sent message ids."""
    text = redact(text)
    if len(text) <= 30000:
        try:
            payload = {"chat_id": chat_id, "rich_message": {"markdown": text}}
            if reply_to:
                payload["reply_to_message_id"] = reply_to
            return _mid(tg_json("sendRichMessage", payload))
        except Exception as e:
            print(f"rich fallback: {e}", flush=True)
    mids = []
    for chunk in chunks_html(md_to_html(text)):
        try:
            mids += _mid(tg("sendMessage", chat_id=chat_id, text=chunk,
                            reply_to_message_id=reply_to or "",
                            parse_mode="HTML"))
        except Exception:
            try:
                mids += _mid(tg("sendMessage", chat_id=chat_id, text=chunk, reply_to_message_id=reply_to or ""))
            except Exception as e:
                print(f"send error: {e}", flush=True)
    return mids


def send_document(chat_id: str, filename: str, content: str):
    """Upload a file via multipart (stdlib)."""
    b = "----superbotBndry"
    part = (f"--{b}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{filename}\"\r\n"
            f"Content-Type: text/markdown\r\n\r\n").encode()
    body = part + redact(content).encode() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(
        API.format(token=TOKEN) + "sendDocument", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


# ---------------------------------------------------------------- claude
def load_session() -> str:
    try:
        with open(SESSION_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _read(path: str, cap: int = FILE_CAP) -> str:
    try:
        with open(path) as f:
            return f.read().strip()[:cap]
    except OSError:
        return ""


def memory_index() -> str:
    return _read(MEMORY_INDEX, 1500)


def seed_workspace():
    """Copy missing SOUL/USER/AGENTS/HEARTBEAT.md templates — never overwrites."""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    try:
        names = os.listdir(TEMPLATES_DIR)
    except OSError:
        return
    for n in names:
        if not os.path.exists(f"{CFG_DIR}/{n}"):
            shutil.copy(f"{TEMPLATES_DIR}/{n}", f"{CFG_DIR}/{n}")


def system_prompt() -> str:
    """Identity + workspace files + memory index + handoff, appended to the system prompt.

    Re-sent each call (prompt-cached by the API) instead of living in the first user
    message, so it is always current and is never lost to auto-compaction.
    """
    parts = [f"You are {CONFIG['bot_name']} — Claude, a self-hosted personal AI assistant "
             f"reached via Telegram, running on the user's own machine with full tool access. "
             f"Reply concisely, mirror the user's language and tone, use Markdown. "
             f"Today is {time.strftime('%Y-%m-%d')}. Your own code and docs are in {os.path.dirname(SELF)} "
             f"(README.md, CHANGELOG.md, bot.py): for any question about yourself or what changed, read "
             f"them first — they beat memory and the handoff note.",
             CONFIG["extra_prompt"]]
    for title, path in (("SOUL", SOUL_MD), ("USER", USER_MD), ("AGENTS", AGENTS_MD)):
        body = _read(path)
        if body:
            parts.append(f"--- {title}.md ({path}) ---\n{body}")
    parts.append(f"--- LONG-TERM MEMORY index ---\n{memory_index() or '(empty — no memories yet)'}")
    handoff = _read(HANDOFF_MD, 1500)
    if handoff:
        parts.append(f"--- PREVIOUS SESSION HANDOFF (may be outdated: verify before repeating or acting "
                     f"on it) ---\n{handoff}")
    parts.append(
        f"Memory lives in {MEMORY_DIR}: one .md file = one fact, frontmatter name/description/"
        f"metadata.type (user|feedback|project|reference), one pointer line in {MEMORY_INDEX}. "
        f"Only the index is shown above — Read a memory file when it is relevant. Save ONLY durable "
        f"facts (preferences, infra/project facts, lessons) — not casual chat. Existing → update, "
        f"don't duplicate. Keep the index under 200 lines (Claude Code reads no further); merge duplicates, never delete a memory just to save space.\n"
        f"Push a message to the user any time (reminders via `at`/cron/systemd-run, long jobs): "
        f"`{SELF_CMD} --send 'text'`. Scheduled AI task whose answer is sent to the user: "
        f"`{SELF_CMD} --ask 'prompt'`.")
    return "\n\n".join(p for p in parts if p)


def forget(name: str) -> bool:
    """Remove one memory (file + index line)."""
    removed = False
    if not name or "/" in name or ".." in name:   # never leave MEMORY_DIR
        return False
    for p in (f"{MEMORY_DIR}/{name}", f"{MEMORY_DIR}/{name}.md"):
        if os.path.isfile(p):
            os.remove(p)
            removed = True
            break
    try:
        lines = [l for l in open(MEMORY_INDEX).readlines()
                 if f"({name}.md)" not in l and f"({name})" not in l]
        with open(MEMORY_INDEX, "w") as fh:
            fh.writelines(lines)
    except OSError:
        pass
    return removed


JOBS: "queue.Queue[tuple]" = queue.Queue()
BUSY = threading.Event()          # a session round is running
STOP = threading.Event()          # /stop requested
CURRENT: dict = {}                # {"proc": Popen} of the running session round


def kill_tree(proc):
    """Kill claude and every tool process it started (detached jobs via systemd-run/setsid survive)."""
    try:
        if proc.pid <= 1:                    # never signal init / every process
            raise OSError
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            proc.kill()
        except OSError:
            pass


def _tool_label(c: dict) -> str:
    inp = c.get("input") or {}
    arg = next((str(inp[k]) for k in ("command", "file_path", "pattern", "url", "query",
                                      "description", "prompt") if inp.get(k)), "")
    return f"🔧 {c.get('name', '?')}: {arg.splitlines()[0][:60] if arg else ''}"


class Progress:
    """One status message, edited (throttled) as Claude calls tools, deleted at the end."""

    def __init__(self, chat_id: str):
        self.chat_id, self.mid, self.last, self.lines = chat_id, None, 0.0, []

    def update(self, label: str):
        self.lines = (self.lines + [label])[-5:]
        if time.time() - self.last < 2.5:
            return
        self.last = time.time()
        txt = "⏳ " + "\n".join(self.lines)
        try:
            if self.mid:
                tg("editMessageText", chat_id=self.chat_id, message_id=self.mid, text=txt)
            else:
                self.mid = tg("sendMessage", chat_id=self.chat_id, text=txt,
                              disable_notification="true")["result"]["message_id"]
        except Exception:
            pass

    def done(self):
        if self.mid:
            try:
                tg("deleteMessage", chat_id=self.chat_id, message_id=self.mid)
            except Exception:
                pass


def heavy_prefix(text: str) -> tuple[str, bool]:
    """A message starting with "!" asks for the heavy model; returns (text without "!", heavy)."""
    t = text.lstrip()
    return (t[1:].lstrip(), True) if t.startswith("!") else (text, False)


def pick_model(heavy: bool = False) -> str:
    """"!" → auto_model heavy; otherwise /model override, auto_model light, config model ("" = claude default)."""
    am = CONFIG["auto_model"]
    if heavy and am.get("heavy"):
        return am["heavy"]
    return load_state().get("model") or am.get("light") or CONFIG["model"]


def load_threads() -> dict:
    try:
        with open(THREADS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def remember_thread(mids: list[int], sid: str):
    """Map the bot messages of one answer to its session, so replying to them resumes it."""
    if not sid or not mids:
        return
    th = load_threads()
    th.update({str(m): sid for m in mids})
    th = dict(sorted(th.items(), key=lambda kv: int(kv[0]))[-THREADS_KEEP:])
    with open(THREADS_FILE + ".tmp", "w") as f:
        json.dump(th, f)
    os.replace(THREADS_FILE + ".tmp", THREADS_FILE)


class claude_slot:
    """flock on CLAUDE_LOCK: one claude at a time across the bot, --ask and other scripts.
    shortcut: skipped inside a bot round (a nested `--ask` would wait on its own parent);
    an `at` job inherits that env and also skips — use systemd-run for scheduled --ask."""

    def __enter__(self):
        self.f = None
        if not os.environ.get("SUPERBOT_ROUND"):
            self.f = open(CLAUDE_LOCK, "a")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.f:
            self.f.close()          # closing releases the lock


def run_quick(chat_id: str, cmd: str):
    """Owner shortcut: run the configured shell command directly, no Claude round."""
    p = subprocess.Popen(CONFIG["quick_commands"][cmd], shell=True, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, cwd=HOME, start_new_session=True)
    try:
        out = p.communicate(timeout=60)[0].strip() or f"(exit {p.returncode})"
    except subprocess.TimeoutExpired:
        kill_tree(p)                       # kills grandchildren holding the pipe too
        out = "(timeout 60s)"
    send(chat_id, f"```\n{out[-3500:]}\n```")


def menu_keyboard() -> dict:
    keys = [*CONFIG["quick_commands"], "/new", "/usage", "/cost", "/undo", "/diff", "/jobs", "/find", "/ro", "/model", "/health"]
    return {"keyboard": [[{"text": k} for k in keys[i:i + 3]] for i in range(0, len(keys), 3)],
            "resize_keyboard": True, "is_persistent": True}


def transcribe(path: str) -> str:
    """Local speech-to-text; "" if faster-whisper is missing or fails (Claude then handles the file)."""
    try:
        from faster_whisper import WhisperModel
        m = WhisperModel(CONFIG["whisper_model"], device="cpu", compute_type="int8")
        # decode with ffmpeg ourselves: faster-whisper's PyAV path breaks on av>=15
        pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "s16le", "-ac", "1",
                              "-ar", "16000", "-"], capture_output=True, timeout=120, check=True).stdout
        import numpy as np
        # pin language: auto-detect on short clips misfires (Indonesian heard as Tagalog)
        segs, _ = m.transcribe(np.frombuffer(pcm, np.int16).astype(np.float32) / 32768, vad_filter=True,
                               language=CONFIG["lang"])
        return " ".join(s.text.strip() for s in segs).strip()
    except Exception as e:   # loaded per call on purpose: frees RAM on a small box
        print(f"transcribe failed: {e}", flush=True)
        return ""


def pg_rss_mb(pgid: int) -> int:
    """Total RSS (MB) of every process in process group `pgid` (claude + its tool children)."""
    pages = 0
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            st = open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()
            if int(st[2]) == pgid:                    # st[2] = pgrp (field 5 overall)
                pages += int(st[21])                  # st[21] = rss in pages (field 24)
        except (OSError, ValueError, IndexError):
            continue
    return pages * os.sysconf("SC_PAGE_SIZE") // (1 << 20)


def run_claude(text: str, chat_id: str = "", model: str = "", ephemeral: bool = False,
               thread: str = "", _retry: bool = True) -> tuple[str, str, bool]:
    """One headless claude round. Returns (reply, session_id, ok).

    ephemeral=True: no session (heartbeat, --ask); otherwise resumes the persistent one, or
    `thread` (an older session the owner replied into) without touching the current one.
    model "" → pick_model(). The prompt goes via stdin, so text starting with "-" is never
    parsed as a flag.
    """
    model = model or pick_model()
    sid = thread or ("" if ephemeral else load_session())
    cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
           "--permission-mode", CONFIG["permission_mode"],
           "--append-system-prompt", system_prompt()]
    if model:
        cmd += ["--model", model]
    cmd += [str(x) for x in CONFIG["claude_args"]]
    # PreToolUse hook → Telegram ✅/⛔ + Edit/Write snapshots for /undo (hooks run even under bypassPermissions)
    hook = {"type": "command", "command": f"{shlex.quote(sys.executable)} {shlex.quote(SELF)} --approve",
            "timeout": CONFIG["confirm_timeout"] + 30}
    cmd += ["--settings", json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit", "hooks": [hook]}]}})]
    if not ephemeral and load_state().get("ro"):
        cmd[cmd.index("--permission-mode") + 1] = "plan"      # /ro: plan mode = read-only
    env = dict(ENV, SUPERBOT_ROUND=str(int(time.time() * 1000)))
    if ephemeral:
        cmd.append("--no-session-persistence")
    elif sid:
        cmd += ["--resume", sid]
    else:
        cmd += ["--session-id", str(uuid.uuid4())]
    if not ephemeral:
        STOP.clear()
    stop_typing, timed_out = threading.Event(), threading.Event()

    def _typing():
        while not stop_typing.wait(4):
            try:
                tg("sendChatAction", chat_id=chat_id, action="typing")
            except Exception:
                pass

    if chat_id:
        threading.Thread(target=_typing, daemon=True).start()
    prog = Progress(chat_id) if chat_id and CONFIG["progress"] else None
    result, raw, ctx = None, [], 0
    with claude_slot():
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, cwd=HOME, env=env,
                                start_new_session=True)   # own process group: /stop kills tool children too
        if not ephemeral:
            CURRENT["proc"] = proc
        peak = [0]
        done = threading.Event()

        def _sample():
            while not done.wait(2):
                try:
                    peak[0] = max(peak[0], pg_rss_mb(proc.pid))
                except Exception:
                    pass
        if isinstance(getattr(proc, "pid", None), int):
            threading.Thread(target=_sample, daemon=True).start()
        killer = threading.Timer(TIMEOUT, lambda: (timed_out.set(), kill_tree(proc)))
        killer.start()
        try:
            proc.stdin.write(text)
            proc.stdin.close()
            for line in proc.stdout:
                try:
                    d = json.loads(line)
                except ValueError:
                    raw.append(line)
                    continue
                if d.get("type") == "assistant":
                    m = d.get("message") or {}
                    u = m.get("usage") or {}
                    ctx = ((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
                           + (u.get("cache_creation_input_tokens") or 0)) or ctx
                    for c in m.get("content") or []:
                        if prog and c.get("type") == "tool_use":
                            prog.update(_tool_label(c))
                elif d.get("type") == "result":
                    result = d
            proc.wait()
        except (OSError, ValueError) as e:
            raw.append(str(e))
            kill_tree(proc)
        finally:
            killer.cancel()
            done.set()
            stop_typing.set()
            CURRENT.pop("proc", None) if not ephemeral else None
            if prog:
                prog.done()
    if not ephemeral and STOP.is_set():
        STOP.clear()
        return _s("stopped"), sid, False
    if timed_out.is_set():
        return _s("timeout"), sid, False
    out = "".join(raw).strip()
    if sid and _retry and "No conversation found" in out:
        if thread:      # replied-to session was deleted → answer in the current session instead
            print(f"thread {sid} gone, using the current session", flush=True)
            return run_claude(text, chat_id, model, ephemeral, "", _retry=False)
        # session file points at a deleted/foreign session → start fresh instead of failing forever
        print(f"session {sid} gone, starting fresh", flush=True)
        os.remove(SESSION_FILE)
        return run_claude(text, chat_id, model, ephemeral, "", _retry=False)
    if result is None and isinstance(proc.returncode, int) and proc.returncode in (-9, 137, -15):
        return _s("oom", sig=abs(proc.returncode) % 128, peak=peak[0] or "?"), sid, False
    if result is None:
        return out[:8000] or f"(claude exit {proc.returncode})", sid, False
    reply = (result.get("result") or "").strip() or f"(claude exit {proc.returncode}, no output)"
    new_sid = result.get("session_id") or sid
    log_usage(result, new_sid, priced=not third_party(env))
    if not ephemeral and not thread:
        if new_sid and new_sid != sid:
            with open(SESSION_FILE, "w") as f:
                f.write(new_sid)
        update_state(ctx=ctx, last_ts=time.time())
    return reply, new_sid or "", not result.get("is_error")


REFLECT_PROMPT = (
    "[SESSION ENDING — internal maintenance, the user will not see this reply]\n"
    "1. Save durable new facts and lessons from this session to long-term memory following "
    "your memory rules (update existing files, no duplicates). Update USER.md only for stable "
    "facts about the user. If nothing is new, save nothing.\n"
    "2. Then reply with ONLY a handoff note, max 10 short lines: ongoing tasks, open questions, "
    "important recent context. Only facts you verified in this session; leave out proposals the user "
    "did not take up and anything you only assumed. No preamble.")


def rotate(reflect: bool = True):
    """End the current session: reflection pass (memory + handoff), then forget the session."""
    if not load_session():
        return
    if reflect and load_state().get("ctx", 0) > 3000:      # tiny sessions: nothing to learn
        note, _, ok = run_claude(REFLECT_PROMPT, model=CONFIG["reflect_model"])
        if ok and note:
            with open(HANDOFF_MD, "w") as f:
                f.write(f"({time.strftime('%Y-%m-%d %H:%M')})\n{note[:1500]}\n")
    try:
        os.remove(SESSION_FILE)
    except OSError:
        pass
    update_state(ctx=0)


def handle_job(job: tuple):
    kind, chat_id = job[0], job[1]
    if kind == "rotate":
        send(chat_id, _s("rotating"))
        rotate()
        send(chat_id, _s("new_session"))
    elif kind == "msg":
        _, _, text, mid, opts = job
        mids = opts.get("mids") or [mid]
        for m in mids:
            react(chat_id, m, WORKING)
        ok, thread = False, opts.get("thread", "")
        if thread and thread == load_session():
            thread = ""                     # replied into the current session: a normal round
        try:
            st = load_state()
            if (not thread and load_session()
                    and time.time() - st.get("last_ts", time.time()) > CONFIG["idle_hours"] * 3600):
                rotate()
            reply, sid, ok = run_claude(text, chat_id=chat_id, model=pick_model(opts.get("heavy", False)),
                                        thread=thread)
            remember_thread(deliver(chat_id, reply, mid), sid)
        finally:                            # 👍 done / 💔 failed, stopped or crashed — before any rotation
            for m in mids:
                react(chat_id, m, DONE if ok else FAILED)
        check_budget(chat_id)
        if not thread and load_state().get("ctx", 0) > CONFIG["rotate_tokens"]:
            print("context large, rotating session", flush=True)
            rotate()


def worker():
    """Serial job runner. Messages queued while Claude works are merged into one round."""
    pending: list = []
    while True:
        job = pending.pop(0) if pending else JOBS.get()
        if job[0] == "msg":
            while True:
                try:
                    nxt = JOBS.get_nowait()
                except queue.Empty:
                    break
                # same chat and same thread only: a reply into an old task stays its own round
                if nxt[0] == "msg" and nxt[1] == job[1] and nxt[4].get("thread", "") == job[4].get("thread", ""):
                    opts = dict(job[4], heavy=job[4].get("heavy", False) or nxt[4].get("heavy", False),
                                mids=job[4].get("mids", []) + nxt[4].get("mids", []))
                    job = ("msg", job[1], f"{job[2]}\n\n{nxt[2]}", nxt[3], opts)
                else:
                    pending.append(nxt)
                    break
        BUSY.set()
        try:
            handle_job(job)
        except Exception as e:
            print(f"job error: {e}", flush=True)
            send(job[1], f"⚠️ {type(e).__name__}: {e}")
        finally:
            BUSY.clear()


def stop_current() -> bool:
    """/stop: kill the running round and drop queued messages."""
    keep = []
    while True:
        try:
            j = JOBS.get_nowait()
            if j[0] != "msg":
                keep.append(j)          # /new must survive /stop
            else:
                for m in j[4].get("mids", []):
                    react(j[1], m, FAILED)   # dropped: never runs
        except queue.Empty:
            break
    for j in keep:
        JOBS.put(j)
    proc = CURRENT.get("proc")
    if not proc:
        return False
    STOP.set()
    kill_tree(proc)
    return True


HEARTBEAT_PROMPT = ("[HEARTBEAT] Work through the checklist in HEARTBEAT.md ({path}). If nothing "
                    "needs the user's attention, reply exactly HEARTBEAT_OK. Otherwise reply with a "
                    "short message for the user.")


def heartbeat_tasks() -> bool:
    """True when HEARTBEAT.md has at least one real (uncommented) checklist item."""
    body = re.sub(r"<!--.*?-->", "", _read(HEARTBEAT_MD), flags=re.S)
    return bool(re.search(r"^\s*[-*]\s+\S", body, re.M))


def heartbeat_agent():
    """Optional proactive loop: cheap model, no session, silent when all is well."""
    while True:
        time.sleep(CONFIG["heartbeat_minutes"] * 60)
        owner = owner_chat_id()
        if not owner or BUSY.is_set() or not heartbeat_tasks():
            continue
        reply, _, ok = run_claude(HEARTBEAT_PROMPT.format(path=HEARTBEAT_MD),
                                  model=CONFIG["heartbeat_model"], ephemeral=True)
        if ok and "HEARTBEAT_OK" not in reply:
            send(owner, reply)


def deliver(chat_id: str, reply: str, reply_to: int | None = None) -> list[int]:
    """Send an answer (long ones also as a file); returns the message ids sent."""
    if len(reply) > DOC_LIMIT:
        mids = send(chat_id, reply[:1000] + "\n\n…(truncated — full answer attached) 📄", reply_to=reply_to)
        try:
            mids += _mid(send_document(chat_id, "answer.md", reply))
        except Exception as e:
            print(f"doc error: {e}", flush=True)
        return mids
    return send(chat_id, reply, reply_to=reply_to)


# ---------------------------------------------------------------- usage & health
def log_usage(d: dict, session_id: str, priced: bool = True):
    """Append one claude round's cost/duration stats to usage.jsonl (reset at ~1 MB).

    priced=False: 3rd-party endpoint — claude prices every model at Anthropic rates,
    so that $ figure is meaningless and is left out of /usage.
    """
    entry = {"ts": time.time(), "session": session_id,
             "cost": d.get("total_cost_usd"), "duration_ms": d.get("duration_ms"),
             "num_turns": d.get("num_turns"), "priced": priced}
    if d.get("modelUsage"):
        entry["model"] = ",".join(sorted(d["modelUsage"]))
    usage = d.get("usage")
    if usage:
        entry["tokens_in"] = ((usage.get("input_tokens") or 0)
                              + (usage.get("cache_read_input_tokens") or 0)
                              + (usage.get("cache_creation_input_tokens") or 0))
        entry["tokens_out"] = usage.get("output_tokens") or 0
    line = json.dumps(entry) + "\n"
    try:
        if os.path.exists(USAGE_FILE) and os.path.getsize(USAGE_FILE) > USAGE_MAX_BYTES:
            with open(USAGE_FILE, "w") as f:      # simple rotation: reset
                f.write(line)
        else:
            with open(USAGE_FILE, "a") as f:
                f.write(line)
    except OSError:
        pass


def _hms(ms) -> str:
    s = int((ms or 0) / 1000)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}h {m:02d}m" if h else (f"{m}m {sec:02d}s" if m else f"{sec}s")


def _kfmt(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else (f"{n / 1e3:.1f}k" if n >= 1e3 else str(n))


def usage_report() -> str:
    """Aggregate usage.jsonl into today / 7 days / total."""
    entries = []
    try:
        with open(USAGE_FILE) as f:
            for l in f:
                try:
                    entries.append(json.loads(l))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    if not entries:
        return _s("usage_empty")
    now = time.time()
    lt = time.localtime(now)
    day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    week0 = now - 7 * 86400

    def agg(sub):
        priced = [e for e in sub if e.get("cost") is not None and e.get("priced", True)]
        cost = sum(e["cost"] for e in priced)
        turns = sum(e.get("num_turns") or 1 for e in sub)
        dur = sum(e.get("duration_ms") or 0 for e in sub)
        tok = sum((e.get("tokens_in") or 0) + (e.get("tokens_out") or 0) for e in sub)
        return (f"${cost:.4f}" if priced else "—"), turns, dur, _kfmt(tok)

    out = [_s("usage_head")]
    for key, sub in (("usage_today", [e for e in entries if e["ts"] >= day0]),
                     ("usage_week", [e for e in entries if e["ts"] >= week0]),
                     ("usage_total", entries)):
        cost, turns, dur, tok = agg(sub)
        out.append(_s("usage_row", label=_s(key), cost=cost, turns=turns, dur=_hms(dur), tok=tok))
    return "\n".join(out)


def _entries() -> list:
    out = []
    try:
        with open(USAGE_FILE) as f:
            for l in f:
                try:
                    out.append(json.loads(l))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return out


def day_cost(day_start: float, end: float = 1e18) -> float:
    return sum(e["cost"] for e in _entries() if day_start <= e["ts"] < end
               and e.get("cost") is not None and e.get("priced", True))


def day0(ts: float | None = None) -> float:
    lt = time.localtime(ts or time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def cost_report() -> str:
    """Spend per day (7 days) and per model."""
    es = [e for e in _entries() if e.get("cost") is not None and e.get("priced", True)]
    if not es:
        return _s("usage_empty")
    t0, rows, models = day0(), [], {}
    for n in range(7):
        a = t0 - n * 86400
        rows.append(f"{time.strftime('%a %d', time.localtime(a))}: ${day_cost(a, a + 86400):.2f}")
    for e in es:
        if e["ts"] >= t0 - 6 * 86400:
            models[e.get("model", "?")] = models.get(e.get("model", "?"), 0) + e["cost"]
    cap = CONFIG["daily_budget_usd"]
    return ("💸 " + "\n".join(rows) + "\n\n" + "\n".join(f"{m}: ${c:.2f}" for m, c in
            sorted(models.items(), key=lambda x: -x[1]))
            + (f"\n\nbudget/hari: ${cap:.2f}" if cap else ""))


def check_budget(chat_id: str):
    cap = CONFIG["daily_budget_usd"]
    if cap <= 0:
        return
    t0, cost = day0(), day_cost(day0())
    if cost > cap and load_state().get("budget_day") != t0:
        update_state(budget_day=t0)
        send(chat_id, _s("budget_warn", cost=cost, cap=cap))


def jobs_report(arg: str = "") -> str:
    """Background systemd-run jobs: list, `log <unit>`, `stop <unit>`."""
    act, _, unit = arg.partition(" ")
    unit = unit.strip()
    if act in ("log", "stop"):
        if not re.fullmatch(r"run-[\w@.:-]+\.(service|timer)", unit):
            return "unit?"
        if act == "stop":
            subprocess.run(["systemctl", "--user", "stop", unit], timeout=20)
            return f"⏹ {unit}"
        r = subprocess.run(["journalctl", "--user", "-u", unit, "-n", "15", "--no-pager"],
                           capture_output=True, text=True, timeout=20)
        return f"```\n{redact(r.stdout)[-3500:]}\n```"
    r = subprocess.run(["systemctl", "--user", "list-units", "run-*", "--no-legend", "--plain"],
                       capture_output=True, text=True, timeout=20)
    lines = [" ".join(l.split()[:4]) for l in r.stdout.splitlines() if l.strip()]
    return ("⚙️ " + "\n".join(lines) + "\n\n/jobs log|stop <unit>") if lines else _s("jobs_none")


def morning_digest(chat_id: str):
    send(chat_id, f"☀️ {health_report()}\n\n{cost_report()}\n\n{jobs_report()}")


def digest_loop():
    while True:
        time.sleep(30)
        hhmm = CONFIG["morning_digest"]
        owner = owner_chat_id()
        today = time.strftime("%Y-%m-%d")
        if (hhmm and owner and time.strftime("%H:%M") >= hhmm
                and load_state().get("digest_day") != today):
            update_state(digest_day=today)
            try:
                morning_digest(owner)
            except Exception as e:
                print(f"digest error: {e!r}", flush=True)


def health_report() -> str:
    """On-demand disk/systemd/docker checks via alert_check (dedup state untouched)."""
    import alert_check
    rows = []
    for key in ("disk", "units", "docker"):
        ok, detail = alert_check.CHECKS[key]()
        rows.append(f"{'✅' if ok else '🚨'} {_s(f'health_{key}')}: {detail}")
    return _s("health", rows="\n".join(rows))


def owner_chat_id() -> str:
    try:
        with open(OWNER_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def download_photo(msg: dict) -> str | None:
    """Download the largest photo size, return the local path."""
    photos = msg.get("photo") or []
    if not photos:
        return None
    os.makedirs(MEDIA_DIR, exist_ok=True)
    file_id = photos[-1]["file_id"]
    try:
        fp = tg("getFile", file_id=file_id)["result"]["file_path"]
        url = API.format(token=TOKEN).replace("/bot", "/file/bot") + fp
        path = f"{MEDIA_DIR}/{msg['message_id']}.jpg"
        with urllib.request.urlopen(url, timeout=120) as r, open(path, "wb") as f:
            f.write(r.read())
        return path
    except Exception as e:
        print(f"photo error: {e}", flush=True)
        return None


def attachment(msg: dict) -> dict | None:
    return next((msg[k] for k in ("document", "voice", "audio", "video", "video_note")
                 if msg.get(k)), None)


def clean_media(days: float):
    """Delete downloaded media older than `days`."""
    cutoff = time.time() - days * 86400
    try:
        for n in os.listdir(MEDIA_DIR):
            p = f"{MEDIA_DIR}/{n}"
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
    except OSError:
        pass


def download_document(msg: dict) -> str | None:
    """Download an incoming document, return the local path.

    Returns "" when the file exceeds the Telegram bot download limit
    (caller shows a friendly message), None on other failures.
    """
    doc = attachment(msg) or {}
    if doc.get("file_size", 0) > TG_DL_LIMIT:
        return ""
    try:
        info = tg("getFile", file_id=doc["file_id"])["result"]
        if info.get("file_size", 0) > TG_DL_LIMIT:
            return ""
        url = API.format(token=TOKEN).replace("/bot", "/file/bot") + info["file_path"]
        name = os.path.basename(doc.get("file_name") or info["file_path"])
        name = re.sub(r"[^\w.\-]", "_", name)[:80] or "file"
        path = f"{MEDIA_DIR}/{msg['message_id']}_{name}"
        os.makedirs(MEDIA_DIR, exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as r, open(path, "wb") as f:
            f.write(r.read())
        return path
    except Exception as e:
        print(f"document error: {e}", flush=True)
        return None


# ---------------------------------------------------------------- pairing
def pairing_code() -> str:
    """The 6-digit code a new owner must send as `/start <code>`.

    Generated once per process while unclaimed; only ever printed to the
    service journal (visible to whoever runs the machine), never to Telegram.
    """
    try:
        with open(PAIRING_FILE) as f:
            return f.read().strip()
    except OSError:
        code = f"{secrets.randbelow(1000000):06d}"
        with open(PAIRING_FILE, "w") as f:
            f.write(code)
        print(f"== PAIRING CODE: {code} — send '/start {code}' to this bot from Telegram ==",
              flush=True)
        return code


def claim_owner(chat_id: str, text: str) -> bool:
    """First-run ownership claim, secured by the pairing code."""
    if owner_chat_id():
        return False
    code = pairing_code()
    supplied = text.split()[1:] if len(text.split()) > 1 else []
    if supplied and secrets.compare_digest(supplied[0], code):
        with open(OWNER_FILE, "w") as f:
            f.write(chat_id)
        try:
            os.remove(PAIRING_FILE)
        except OSError:
            pass
        return True
    return False


def _heartbeat():
    """Touch the heartbeat file every 30s so alert_check can spot a hung bot; daily media cleanup."""
    cleaned = 0.0
    while True:
        if time.time() - cleaned > 86400:
            clean_media(CONFIG["media_days"])
            cleaned = time.time()
        try:
            if time.time() - LAST_POLL[0] < 180:   # only while the poll loop is alive
                with open(HEARTBEAT_FILE, "w") as f:
                    f.write(str(time.time()))
        except OSError:
            pass
        time.sleep(30)


# ---------------------------------------------------------------- main
EDIT_TOOLS = ("Edit", "Write", "MultiEdit")


def snapshot(path: str):
    """Save the file's pre-edit content under the current round so /undo and /diff can use it."""
    rnd = os.environ.get("SUPERBOT_ROUND", "")
    if not rnd.isdigit():
        return
    d = f"{UNDO_DIR}/{rnd}"
    os.makedirs(d, exist_ok=True)
    man = f"{d}/manifest.json"
    try:
        items = json.load(open(man))
    except (OSError, ValueError):
        items = []
    if any(i["path"] == path for i in items):
        return                                  # first snapshot wins = original state
    bak = None
    if os.path.isfile(path) and os.path.getsize(path) < 5_000_000:
        bak = f"b{len(items)}"
        shutil.copy2(path, f"{d}/{bak}")
    items.append({"path": path, "bak": bak})
    json.dump(items, open(man, "w"))
    rounds = sorted(os.listdir(UNDO_DIR))
    for old in rounds[:-CONFIG["undo_keep"]]:
        shutil.rmtree(f"{UNDO_DIR}/{old}", ignore_errors=True)


def last_round() -> tuple[str, list]:
    try:
        for rnd in sorted(os.listdir(UNDO_DIR), reverse=True):
            items = json.load(open(f"{UNDO_DIR}/{rnd}/manifest.json"))
            if items:
                return rnd, items
    except (OSError, ValueError):
        pass
    return "", []


def undo_last() -> str:
    rnd, items = last_round()
    if not items:
        return _s("undo_none")
    for it in items:
        if it["bak"]:
            shutil.copy2(f"{UNDO_DIR}/{rnd}/{it['bak']}", it["path"])
        elif os.path.exists(it["path"]):
            os.remove(it["path"])               # file did not exist before the round
    shutil.rmtree(f"{UNDO_DIR}/{rnd}", ignore_errors=True)
    return _s("undo_done", n=len(items), files="\n".join(f"`{i['path']}`" for i in items))


def diff_last() -> str:
    rnd, items = last_round()
    if not items:
        return _s("undo_none")
    out = []
    for it in items:
        old = open(f"{UNDO_DIR}/{rnd}/{it['bak']}", errors="replace").read().splitlines() if it["bak"] else []
        try:
            new = open(it["path"], errors="replace").read().splitlines()
        except OSError:
            new = []
        out += list(difflib.unified_diff(old, new, it["path"], it["path"], lineterm="", n=2))
    return "```diff\n" + redact("\n".join(out))[:3500] + "\n```" if out else _s("undo_none")


# the owner asked for "finish the job, then report": a blocked step must not end the whole round
SKIP_STEP = ("Do not retry it or work around it. Skip this step, finish everything else that does not "
             "depend on it, and list the skipped command in your final report so the owner can run it.")


def approve_hook(stdin_json: str) -> int:
    """PreToolUse hook: snapshot edits for /undo; ask the owner (inline buttons) before a risky
    Bash command or an Edit/Write on a sensitive path. Exit code 0/2."""
    try:
        d = json.loads(stdin_json)
        tool, inp = d.get("tool_name", ""), d.get("tool_input", {})
        command = inp.get("command", "") if tool == "Bash" else f"{tool}: {inp.get('file_path', '')}"
    except (json.JSONDecodeError, AttributeError):
        return 0
    try:   # decisions nobody waited for (pressed after the timeout) would pile up forever
        for f in os.listdir(APPROVALS_DIR):
            if time.time() - os.path.getmtime(f"{APPROVALS_DIR}/{f}") > 3600:
                os.remove(f"{APPROVALS_DIR}/{f}")
    except OSError:
        pass
    if tool in EDIT_TOOLS and inp.get("file_path"):
        try:
            snapshot(os.path.abspath(inp["file_path"]))
        except OSError:
            pass
    pats = CONFIG["confirm_patterns"] if tool == "Bash" else (
        CONFIG["confirm_paths"] if tool in EDIT_TOOLS else [])
    target = command if tool == "Bash" else command.partition(": ")[2]
    if not any(re.search(p, target) for p in pats):
        return 0
    owner, rid = owner_chat_id(), secrets.token_hex(6)
    os.makedirs(APPROVALS_DIR, exist_ok=True)
    try:
        tg_json("sendMessage", {
            "chat_id": owner, "text": f"{_s('approve_ask')}\n\n{redact(command)[:3000]}",
            "reply_markup": {"inline_keyboard": [[
                {"text": "✅ Run", "callback_data": f"ok:{rid}"},
                {"text": "⛔ Deny", "callback_data": f"no:{rid}"}]]}})
    except Exception as e:
        print(f"approval request failed ({e}). {SKIP_STEP}", file=sys.stderr)
        return 2
    path, deadline = f"{APPROVALS_DIR}/{rid}", time.time() + CONFIG["confirm_timeout"]
    while time.time() < deadline:
        if os.path.exists(path):
            ok = open(path).read().strip() == "ok"
            os.remove(path)
            if ok:
                return 0
            break
        time.sleep(1)
    print(f"The owner did not approve this command. {SKIP_STEP}", file=sys.stderr)
    return 2


def handle_callback(cq: dict):
    """Inline ✅/⛔ press → decision file for the waiting approve_hook."""
    msg = cq.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    verdict, _, rid = (cq.get("data") or "").partition(":")
    if chat_id != owner_chat_id() or verdict not in ("ok", "no") or not re.fullmatch(r"[0-9a-f]{12}", rid):
        return
    os.makedirs(APPROVALS_DIR, exist_ok=True)
    with open(f"{APPROVALS_DIR}/{rid}", "w") as f:
        f.write(verdict)
    label = _s("approved") if verdict == "ok" else _s("denied")
    try:
        tg("answerCallbackQuery", callback_query_id=cq["id"], text=label)
        tg("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
           text=f"{msg.get('text', '')}\n\n{label}")
    except Exception:
        pass


def cli(argv: list[str]) -> bool:
    """`--send text` / `--ask prompt` / `--approve` (hook) for cron, scripts and Claude. True if handled."""
    if argv[:1] == ["--approve"]:
        sys.exit(approve_hook(sys.stdin.read()))
    if argv and (len(argv) < 2 or argv[0] not in ("--send", "--ask")):
        sys.exit("usage: bot.py [--send text | --ask prompt | --approve]")   # never become a 2nd poller
    if not argv:
        return False
    owner, text = owner_chat_id(), " ".join(argv[1:])
    if not TOKEN or not owner:
        sys.exit("no token / owner yet")
    if argv[0] == "--ask":
        text, _, _ = run_claude(text, ephemeral=True)
        print(redact(text), flush=True)     # the calling script reads the answer from stdout
    deliver(owner, text)
    return True


# message status for the owner. Bots may only use Telegram's standard reaction set (no ✅),
# and a new reaction replaces the previous one.
SEEN, WORKING, DONE, FAILED = "👀", "👨‍💻", "👍", "💔"


def find_report(q: str) -> str:
    """/find: case-insensitive search in memories, then in past session transcripts (newest first)."""
    if not q:
        return _s("find_usage")
    ql, hits, t_end = q.lower(), [], time.time() + 25
    tdir = os.path.expanduser("~/.claude/projects/-" + HOME.strip("/").replace("/", "-"))
    files = sorted(glob.glob(f"{MEMORY_DIR}/*.md"))
    files += sorted(glob.glob(f"{tdir}/*.jsonl"), key=os.path.getmtime, reverse=True)[:400]
    for f in files:
        if len(hits) >= 8 or time.time() > t_end:
            break
        try:
            body = open(f, errors="replace").read()
        except OSError:
            continue
        i = body.lower().find(ql)
        if i >= 0:
            snip = re.sub(r"\s+", " ", body[max(0, i - 60): i + len(q) + 80])
            hits.append(f"• `{os.path.basename(f)[:40]}` …{snip}…")
    return "\n".join(hits) if hits else _s("find_none", q=q)


def react(chat_id: str, mid: int, emoji: str = SEEN):
    try:
        tg_json("setMessageReaction", {"chat_id": chat_id, "message_id": mid,
                                       "reaction": [{"type": "emoji", "emoji": emoji}]})
    except Exception:
        pass


def handle_command(chat_id: str, text: str) -> bool:
    """Instant owner commands (run on the poll thread). True if `text` was a command."""
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.split("@")[0], arg.strip()
    if cmd == "/new":
        JOBS.put(("rotate", chat_id))
    elif cmd in ("/stop", "/cancel"):
        send(chat_id, _s("stopped") if stop_current() else _s("nothing_running"))
    elif cmd == "/model":
        if not arg:
            send(chat_id, _s("model_show", active=pick_model() or "default"))
        else:
            update_state(model="" if arg == "default" else arg)
            send(chat_id, _s("model_set", active=arg))
    elif cmd == "/id":
        st = load_state()
        send(chat_id, f"chat_id: `{chat_id}`\nsession: `{load_session() or '(none)'}`\n"
                      f"model: `{pick_model() or 'default'}` · ro: {bool(st.get('ro'))} · context: "
                      f"{_kfmt(st.get('ctx', 0))} / {_kfmt(CONFIG['rotate_tokens'])} tok\n"
                      f"continue in terminal: `claude --resume $(cat {SESSION_FILE})`")
    elif cmd == "/brain":
        send(chat_id, memory_index() or _s("brain_empty"))
    elif cmd == "/forget":
        if not arg:
            send(chat_id, _s("forget_usage"))
        else:
            send(chat_id, _s("forgot", name=arg) if forget(arg) else _s("not_found", name=arg))
    elif cmd == "/menu":
        tg_json("sendMessage", {"chat_id": chat_id, "text": "📋", "reply_markup": menu_keyboard()})
    elif cmd in CONFIG["quick_commands"]:
        threading.Thread(target=run_quick, args=(chat_id, cmd), daemon=True).start()
    elif cmd == "/health":
        send(chat_id, health_report())
    elif cmd == "/usage":
        send(chat_id, usage_report())
    elif cmd == "/cost":
        send(chat_id, cost_report())
    elif cmd == "/undo":
        send(chat_id, undo_last())
    elif cmd == "/diff":
        send(chat_id, diff_last())
    elif cmd == "/jobs":
        threading.Thread(target=lambda: send(chat_id, jobs_report(arg)), daemon=True).start()
    elif cmd == "/find":
        threading.Thread(target=lambda: send(chat_id, find_report(arg)), daemon=True).start()
    elif cmd == "/ro":
        on = arg != "off" if arg else not load_state().get("ro")
        update_state(ro=on)
        send(chat_id, _s("ro_on" if on else "ro_off"))
    else:
        return False
    return True


VOICE_SEM = threading.Semaphore(2)


def handle_update(u: dict):
    if u.get("callback_query"):
        handle_callback(u["callback_query"])
        return
    msg = u.get("message")
    if not msg:
        return
    if msg["chat"].get("type") != "private":
        return                              # owner-only bot: groups and channels are ignored
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or msg.get("caption") or "").strip()
    has_photo = bool(msg.get("photo"))
    att = attachment(msg)
    if not text and not has_photo and not att:
        return
    if text.startswith("/start"):
        if claim_owner(chat_id, text):
            send(chat_id, _s("welcome"))
        elif owner_chat_id() and chat_id == owner_chat_id():
            send(chat_id, _s("has_owner"))
        else:
            send(chat_id, _s("pairing_hint") if not owner_chat_id() else _s("private"))
        return
    owner = owner_chat_id()
    if not owner or chat_id != owner:
        send(chat_id, _s("private"))
        return
    if text.startswith("/") and handle_command(chat_id, text):
        return
    text, heavy = heavy_prefix(text)
    if not text and not has_photo and not att:
        return                              # a lone "!"
    replied =(msg.get("reply_to_message") or {}).get("message_id")
    mid = msg.get("message_id")
    opts = {"thread": load_threads().get(str(replied), "") if replied else "", "heavy": heavy, "mids": [mid]}
    react(chat_id, mid)                     # 👀 seen
    print(f"[{time.strftime('%H:%M:%S')}] chat {chat_id}: {text[:80] or '(media)'}", flush=True)
    if has_photo:
        path = download_photo(msg)
        if not path:
            send(chat_id, _s("photo_fail"), reply_to=mid)
            return
        text = (f"The user sent a photo: {path}\nLook at it with the Read tool, then respond."
                + (f"\n\nMessage: {text}" if text else ""))
    elif att:
        path = download_document(msg)
        if path == "":
            send(chat_id, _s("doc_too_big"), reply_to=mid)
            return
        if path is None:
            send(chat_id, _s("doc_fail"), reply_to=mid)
            return
        voice = msg.get("voice") or msg.get("audio") or msg.get("video_note")
        if voice and CONFIG["whisper_model"]:
            def _voice(p=path, c=chat_id, m=mid, t=text, o=opts):
                with VOICE_SEM:                 # whisper is CPU-heavy: max 2 at once
                    said = transcribe(p)
                JOBS.put(("msg", c, f"(voice note) {said}" + (f"\n\n{t}" if t else "")
                          if said else f"The user sent a voice note: {p}\nTranscribe it and respond.", m, o))
            threading.Thread(target=_voice, daemon=True).start()
            return
        how = ("Transcribe it (e.g. whisper / faster-whisper if installed) and respond to "
               "what was said." if msg.get("voice") or msg.get("audio") or msg.get("video_note")
               else "Read it with the appropriate tool, then respond.")
        text = f"The user sent a file: {path}\n{how}" + (f"\n\nMessage: {text}" if text else "")
    if BUSY.is_set() or not JOBS.empty():
        send(chat_id, _s("queued", n=JOBS.qsize() + 1), reply_to=mid)
    JOBS.put(("msg", chat_id, text, mid, opts))



def main():
    if cli(sys.argv[1:]):
        return
    if not TOKEN:
        print("exiting: no token", flush=True)
        return
    me = tg("getMe")
    print(f"superbot: @{me['result']['username']} · api: "
          f"{ENV.get('ANTHROPIC_BASE_URL') or 'anthropic (default)'} · model: {pick_model() or 'default'}",
          flush=True)
    if not owner_chat_id():
        pairing_code()   # print it early so it's visible in the journal
    seed_workspace()
    threading.Thread(target=_heartbeat, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=digest_loop, daemon=True).start()
    if CONFIG["heartbeat_minutes"] > 0:
        threading.Thread(target=heartbeat_agent, daemon=True).start()
    offset = 0
    while True:
        try:
            with urllib.request.urlopen(
                API.format(token=TOKEN) + f"getUpdates?timeout=50&offset={offset}", timeout=70
            ) as r:
                updates = json.load(r).get("result", [])
            LAST_POLL[0] = time.time()
        except Exception as e:
            print(f"poll error: {e}", flush=True)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                handle_update(u)
            except Exception as e:   # one bad update must not crash-loop the bot
                print(f"update error: {e!r}", flush=True)

if __name__ == "__main__":
    main()
