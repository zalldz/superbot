"""Superbot self-checks — run: python3 test_bot.py"""
import importlib
import json
import os
import shutil
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# never touch the live bot: own HOME (config, owner, media, usage) and no real network
_HOME = "/tmp/test_superbot_home"
shutil.rmtree(_HOME, ignore_errors=True)
os.makedirs(_HOME)
os.environ["HOME"] = _HOME
os.makedirs(f"{_HOME}/.config/superbot")


def _no_network(*a, **k):
    raise OSError("network disabled in tests")


urllib.request.urlopen = _no_network
import bot
import alert_check

REAL_MEMORY_DIR = bot.MEMORY_DIR


def _tmp_cfg(tag):
    d = f"/tmp/test_superbot_{tag}"
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    return d


def test_md_to_html():
    h = bot.md_to_html("# J\n**b** *i* ~~s~~ `c` [l](https://x.com/a?b=1&c=2)\n```py\nx<1\n```\n> q **b**")
    assert "<b>J</b>" in h and "<i>i</i>" in h and "<s>s</s>" in h and "<code>c</code>" in h
    assert 'href="https://x.com/a?b=1&amp;c=2"' in h
    assert '<pre><code class="language-py">x&lt;1</code></pre>' in h
    assert "<blockquote>q <b>b</b></blockquote>" in h


def test_chunks_no_loss():
    md = "par **b**\n" * 300 + "```\n" + "baris\n" * 500 + "```"
    html = bot.md_to_html(md)
    cs = bot.chunks_html(html)
    assert sum(len(c) for c in cs) >= len(html)
    assert all(len(c) <= 4096 for c in cs)
    assert all(c.count("<pre>") == c.count("</pre>") for c in cs)
    assert all(c.count("<blockquote>") == c.count("</blockquote>") for c in cs)
    assert sum(c.count("baris") for c in cs) == 500


def test_config_defaults_and_merge():
    assert bot.DEFAULT_CONFIG["lang"] in ("en", "id")
    d = _tmp_cfg("cfg")
    with open(f"{d}/config.json", "w") as f:
        json.dump({"bot_name": "Maid", "lang": "id"}, f)
    bot.CONFIG_FILE = f"{d}/config.json"
    cfg = bot.load_config()
    assert cfg["bot_name"] == "Maid" and cfg["lang"] == "id"
    assert cfg["permission_mode"] == bot.DEFAULT_CONFIG["permission_mode"]  # default utuh
    bot.CONFIG_FILE = os.path.expanduser("~/.config/superbot/config.json")


def test_strings_lang_fallback():
    bot.CONFIG.update({"lang": "xx", "bot_name": "T"})
    assert "owner" in bot._s("welcome").lower()          # fallback ke en
    bot.CONFIG.update({"lang": "id"})
    assert "owner" in bot._s("welcome")
    bot.CONFIG.update({"lang": "en"})


def _workspace(d):
    """Point every workspace path at a temp dir; returns a restore function."""
    names = ("MEMORY_DIR", "MEMORY_INDEX", "SOUL_MD", "USER_MD", "AGENTS_MD", "HEARTBEAT_MD",
             "HANDOFF_MD", "CFG_DIR", "SESSION_FILE", "STATE_FILE", "USAGE_FILE", "MEDIA_DIR")
    orig = {n: getattr(bot, n) for n in names}
    bot.MEMORY_DIR, bot.MEMORY_INDEX = d, f"{d}/MEMORY.md"
    bot.SOUL_MD, bot.USER_MD, bot.AGENTS_MD = f"{d}/SOUL.md", f"{d}/USER.md", f"{d}/AGENTS.md"
    bot.HEARTBEAT_MD, bot.HANDOFF_MD, bot.CFG_DIR = f"{d}/HEARTBEAT.md", f"{d}/HANDOFF.md", d
    bot.SESSION_FILE, bot.STATE_FILE = f"{d}/session", f"{d}/state.json"
    bot.USAGE_FILE, bot.MEDIA_DIR = f"{d}/usage.jsonl", f"{d}/media"
    return lambda: [setattr(bot, n, v) for n, v in orig.items()]


class FakePopen:
    """Stands in for subprocess.Popen: replays canned stream-json lines."""
    script = []          # list of (match_fn(cmd) -> bool, [lines])
    calls = []

    def __init__(self, cmd, **kw):
        FakePopen.calls.append(cmd)
        self.cmd, self.returncode, self.prompt = cmd, 0, []
        lines = next(ls for match, ls in FakePopen.script if match(cmd))
        self.stdout = iter(lines)
        outer = self

        class In:
            def write(self, t):
                outer.prompt.append(t)

            def close(self):
                pass
        self.stdin = In()

    def wait(self):
        return 0

    def kill(self):
        pass


def _res(text, sid="new-sid", **kw):
    return json.dumps(dict({"type": "result", "result": text, "session_id": sid}, **kw)) + "\n"


def _with_fake_claude(script, fn):
    orig_popen, orig_tg = bot.subprocess.Popen, bot.tg
    FakePopen.script, FakePopen.calls = script, []
    bot.subprocess.Popen, bot.tg = FakePopen, (lambda m, **p: {"result": {"message_id": 1}})
    try:
        return fn()
    finally:
        bot.subprocess.Popen, bot.tg = orig_popen, orig_tg


def test_system_prompt_and_seed():
    d = _tmp_cfg("boot")
    restore = _workspace(d)
    bot.seed_workspace()
    for n in ("SOUL.md", "USER.md", "AGENTS.md", "HEARTBEAT.md"):
        assert os.path.exists(f"{d}/{n}")
    with open(bot.SOUL_MD, "w") as f:
        f.write("custom soul")
    bot.seed_workspace()                                   # never overwrites
    assert open(bot.SOUL_MD).read() == "custom soul"
    with open(bot.MEMORY_INDEX, "w") as f:
        f.write("- [Tes](tes.md) — hook\n" + "x" * 2000)
    with open(bot.HANDOFF_MD, "w") as f:
        f.write("open task: fix nginx")
    p = bot.system_prompt()
    assert "custom soul" in p and "LONG-TERM MEMORY" in p and "hook" in p
    assert "fix nginx" in p and "--send" in p and "don't duplicate" in p
    assert len(p) < 12000                                  # capped: stays cheap
    assert not bot.heartbeat_tasks()                       # template = comments only → off
    with open(bot.HEARTBEAT_MD, "a") as f:
        f.write("\n- check backups\n")
    assert bot.heartbeat_tasks()
    restore()


def test_forget():
    d = _tmp_cfg("forget")
    bot.MEMORY_DIR, bot.MEMORY_INDEX = d, f"{d}/MEMORY.md"
    with open(bot.MEMORY_INDEX, "w") as f:
        f.write("# Index\n- [A](a.md) — ha\n- [B](b.md) — hb\n")
    with open(f"{d}/a.md", "w") as f:
        f.write("fact a")
    assert bot.forget("a") is True
    assert not os.path.exists(f"{d}/a.md")
    idx = open(bot.MEMORY_INDEX).read()
    assert "(a.md)" not in idx and "(b.md)" in idx
    assert bot.forget("nope") is False
    bot.MEMORY_DIR, bot.MEMORY_INDEX = REAL_MEMORY_DIR, f"{REAL_MEMORY_DIR}/MEMORY.md"


def test_pairing(tmp="/tmp/test_superbot_pair"):
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    bot.PAIRING_FILE, bot.OWNER_FILE = f"{tmp}/pairing", f"{tmp}/owner"
    # belum ada owner: /start tanpa kode → gagal; dengan kode → klaim
    assert bot.claim_owner("111", "/start") is False
    assert bot.owner_chat_id() == ""
    code = bot.pairing_code()
    assert bot.claim_owner("111", f"/start {code}") is True
    assert bot.owner_chat_id() == "111" and not os.path.exists(bot.PAIRING_FILE)
    # kode salah dari chat lain → tetap bukan owner
    bot.OWNER_FILE = f"{tmp}/owner2"
    assert bot.claim_owner("222", "/start 000000") is False
    bot.PAIRING_FILE = f"{tmp}/pairing2"
    bot.pairing_code()  # generate baru
    assert bot.claim_owner("222", "/start 999999") is False  # kode salah ditolak
    shutil.rmtree(tmp)
    bot.PAIRING_FILE = f"{os.path.expanduser('~')}/.config/superbot/pairing"
    bot.OWNER_FILE = f"{os.path.expanduser('~')}/.config/superbot/owner"


def test_optional_config():
    # memory_dir / env_file: default kosong → fitur mati
    d = _tmp_cfg("opt")
    rc = f"{d}/envrc"
    with open(rc, "w") as f:
        f.write('export ANTHROPIC_API_KEY="sk-tok"\nexport OTHER=x\n'
                "export ANTHROPIC_BASE_URL='https://api.z.ai/api/anthropic'\n"
                "ANTHROPIC_AUTH_TOKEN=zk.123  # comment\n"
                "export API_TIMEOUT_MS=3000000\n# export ANTHROPIC_MODEL=nope\n"
                'export ANTHROPIC_DEFAULT_SONNET_MODEL=""\n')
    bot.CONFIG_FILE = f"{d}/config.json"
    with open(bot.CONFIG_FILE, "w") as f:
        json.dump({"memory_dir": f"{d}/mem", "env_file": rc}, f)
    cfg = bot.load_config()
    assert cfg["memory_dir"] == f"{d}/mem"
    bot.CONFIG.update(cfg)
    env = {}
    bot.env_from_file(env)
    assert env.get("ANTHROPIC_API_KEY") == "sk-tok" and "OTHER" not in env
    assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"   # single quotes
    assert env["ANTHROPIC_AUTH_TOKEN"] == "zk.123"                          # no export, trailing comment
    assert env["API_TIMEOUT_MS"] == "3000000" and "ANTHROPIC_MODEL" not in env
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == ""
    assert bot.third_party(env) and not bot.third_party({})   # 3rd-party endpoint → $ not priced
    bot.CONFIG_FILE = os.path.expanduser("~/.config/superbot/config.json")
    bot.CONFIG.update(bot.load_config())


def test_alert_dedup():
    tmp = "/tmp/test_superbot_alert.json"
    if os.path.exists(tmp):
        os.remove(tmp)
    alert_check.STATE_FILE = tmp
    sent = []
    alert_check.tg = lambda m, **p: sent.append(p)
    alert_check._owner = lambda: "42"
    alert_check.CHECKS = {"disk": lambda: (True, "ok")}
    alert_check.run()
    assert sent == []
    alert_check.CHECKS = {"disk": lambda: (False, "disk 90%")}
    alert_check.run()
    assert len(sent) == 1 and "ALERT" in sent[0]["text"]
    alert_check.run()
    assert len(sent) == 1                       # dedup: no repeat
    alert_check.CHECKS = {"disk": lambda: (True, "ok")}
    alert_check.run()
    assert len(sent) == 2 and "PULIH" in sent[1]["text"]
    os.remove(tmp)


def test_alert_parsers():
    orig = alert_check._sh
    alert_check._sh = lambda c: (
        "Filesystem 512-blocks Used Avail Use% Mounted on\n/dev/sda1 100 90 10 90% /\n"
        if c[0] == "df" else "")
    ok, msg = alert_check.check_disk()
    assert not ok and "/ 90%" in msg
    alert_check._sh = orig


class _Resp:
    def __init__(self, data):
        self._d = data

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_document():
    d = _tmp_cfg("doc")
    bot.MEDIA_DIR = d
    orig_tg, orig_urlopen = bot.tg, urllib.request.urlopen
    bot.tg = lambda m, **p: {"result": {"file_id": "x", "file_size": 12,
                                        "file_path": "documents/abc"}}
    urllib.request.urlopen = lambda url, timeout=0: _Resp(b"DATA")
    msg = {"message_id": 77, "document": {"file_id": "x", "file_name": "../../my report?.log"}}
    path = bot.download_document(msg)
    assert path == f"{d}/77_my_report_.log", path      # nama disanitasi, tanpa traversal
    assert open(path, "rb").read() == b"DATA"
    big = 21 * 1024 * 1024
    msg = {"message_id": 78, "document": {"file_id": "x", "file_size": big, "file_name": "big.zip"}}
    assert bot.download_document(msg) == ""            # >20 MB ditolak tanpa download
    bot.tg = lambda m, **p: {"result": {"file_id": "x", "file_size": big, "file_path": "documents/b"}}
    msg = {"message_id": 79, "document": {"file_id": "x", "file_name": "b.zip"}}
    assert bot.download_document(msg) == ""            # oversize baru ketahuan via getFile
    bot.tg, urllib.request.urlopen = orig_tg, orig_urlopen
    bot.MEDIA_DIR = f"{os.path.expanduser('~')}/.config/superbot/media"


def test_usage_report_and_rotation():
    d = _tmp_cfg("usage")
    bot.USAGE_FILE = f"{d}/usage.jsonl"
    assert bot.usage_report() == bot._s("usage_empty")
    bot.log_usage({"total_cost_usd": 0.5, "duration_ms": 60000, "num_turns": 2}, "s1")
    with open(bot.USAGE_FILE, "a") as f:   # entri 8 hari lalu → di luar window 7 hari
        f.write(json.dumps({"ts": time.time() - 8 * 86400, "cost": 1.0,
                            "duration_ms": 3600000, "num_turns": 10, "session": "old"}) + "\n")
    rep = bot.usage_report()
    assert "$0.5000" in rep                                # hari ini & 7 hari
    assert "$1.5000" in rep                                # total
    assert "2 turns · 1m 00s" in rep and "12 turns · 1h 01m" in rep
    bot.USAGE_MAX_BYTES = 10                               # rotasi: reset jadi satu baris
    bot.log_usage({"total_cost_usd": 0.1}, "s2")
    left = open(bot.USAGE_FILE).read()
    assert left.count("\n") == 1 and '"s2"' in left
    bot.USAGE_MAX_BYTES = 1024 * 1024
    bot.log_usage({"total_cost_usd": 0.1, "usage": {"input_tokens": 1500, "output_tokens": 500}},
                  "s3", priced=False)                       # 3rd-party: $ ignored, tokens counted
    rep = bot.usage_report()
    assert "$0.1000" in rep and "$0.2000" not in rep and "2.0k tok" in rep
    bot.USAGE_FILE = f"{os.path.expanduser('~')}/.config/superbot/usage.jsonl"


def test_stale_session_retry():
    d = _tmp_cfg("stale")
    restore = _workspace(d)
    open(bot.SESSION_FILE, "w").write("dead-sid")
    script = [(lambda c: "--resume" in c, ["No conversation found with session ID: dead-sid\n"]),
              (lambda c: True, [json.dumps({"type": "assistant", "message": {
                  "usage": {"input_tokens": 10, "cache_read_input_tokens": 4990},
                  "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}) + "\n",
                  _res("hi")])]
    reply, sid, ok = _with_fake_claude(script, lambda: bot.run_claude("-Q", chat_id="1"))
    assert reply == "hi" and sid == "new-sid" and ok and len(FakePopen.calls) == 2
    assert "--session-id" in FakePopen.calls[1] and "-Q" not in FakePopen.calls[1]   # prompt via stdin
    assert "--append-system-prompt" in FakePopen.calls[1]
    assert open(bot.SESSION_FILE).read() == "new-sid"
    assert bot.load_state()["ctx"] == 5000                 # context size tracked
    restore()


def test_rotate_writes_handoff():
    d = _tmp_cfg("rot")
    restore = _workspace(d)
    open(bot.SESSION_FILE, "w").write("old")
    bot.update_state(ctx=50000)
    script = [(lambda c: True, [_res("- task A open", sid="old")])]
    _with_fake_claude(script, bot.rotate)
    assert "--model" in FakePopen.calls[0] and "haiku" in FakePopen.calls[0]    # cheap reflection
    assert "task A open" in open(bot.HANDOFF_MD).read()
    assert not os.path.exists(bot.SESSION_FILE) and bot.load_state()["ctx"] == 0
    restore()


def test_worker_merges_queued_messages():
    seen = []
    orig = bot.handle_job
    bot.handle_job = lambda job: seen.append(job)
    o = lambda mid, thread="", heavy=False: {"thread": thread, "heavy": heavy, "mids": [mid]}
    for j in (("msg", "1", "a", 1, o(1)), ("msg", "1", "!b", 2, o(2, heavy=True)), ("rotate", "1"),
              ("msg", "1", "c", 3, o(3)), ("msg", "1", "d", 4, o(4, "old-sid"))):
        bot.JOBS.put(j)
    t = threading.Thread(target=bot.worker, daemon=True)
    t.start()
    for _ in range(50):
        if len(seen) == 4:
            break
        time.sleep(0.02)
    assert seen == [("msg", "1", "a\n\n!b", 2, {"thread": "", "heavy": True, "mids": [1, 2]}),  # heavy if any is
                    ("rotate", "1"), ("msg", "1", "c", 3, o(3)),
                    ("msg", "1", "d", 4, o(4, "old-sid"))]                       # other thread: own round
    bot.handle_job = orig


def test_clean_media_and_cli():
    d = _tmp_cfg("media")
    restore = _workspace(d)
    os.makedirs(bot.MEDIA_DIR)
    old, new = f"{bot.MEDIA_DIR}/old.jpg", f"{bot.MEDIA_DIR}/new.jpg"
    for p in (old, new):
        open(p, "w").write("x")
    os.utime(old, (time.time() - 10 * 86400,) * 2)
    bot.clean_media(7)
    assert not os.path.exists(old) and os.path.exists(new)
    sent = []
    orig_send, orig_owner, orig_tok = bot.send, bot.owner_chat_id, bot.TOKEN
    bot.send, bot.owner_chat_id, bot.TOKEN = (lambda c, t, reply_to=None: sent.append((c, t))), (lambda: "42"), "x"
    assert bot.cli(["--send", "backup", "done"]) and sent == [("42", "backup done")]
    assert not bot.cli([])
    bot.send, bot.owner_chat_id, bot.TOKEN = orig_send, orig_owner, orig_tok
    restore()


def test_private_only_and_ask():
    orig = bot.owner_chat_id, bot.send, bot.TOKEN, bot.run_claude
    sent, prompts = [], []
    bot.owner_chat_id, bot.TOKEN = (lambda: "42"), "x"
    bot.send = lambda c, t, reply_to=None: sent.append((c, t)) or []
    while not bot.JOBS.empty():
        bot.JOBS.get()
    bot.handle_update({"message": {"chat": {"id": -100, "type": "supergroup"}, "from": {"id": 42},
                                   "message_id": 1, "text": "cek disk"}})
    assert bot.JOBS.empty() and not sent                     # groups ignored, even from the owner
    bot.run_claude = lambda t, **k: (prompts.append((t, k)) or "done ✅", "", True)
    assert bot.cli(["--ask", "audit", "repo"])
    assert sent == [("42", "done ✅")] and prompts == [("audit repo", {"ephemeral": True})]
    bot.owner_chat_id, bot.send, bot.TOKEN, bot.run_claude = orig


def test_reply_resumes_thread():
    d = _tmp_cfg("thread")
    restore = _workspace(d)
    orig = bot.THREADS_FILE, bot.owner_chat_id, bot.send, bot.react
    bot.THREADS_FILE, bot.owner_chat_id = f"{d}/threads.json", (lambda: "42")
    mids = iter(range(100, 200))
    bot.send = lambda c, t, reply_to=None: [next(mids)]
    reacts = []
    bot.react = lambda c, m, e=bot.SEEN: reacts.append((m, e))
    open(bot.SESSION_FILE, "w").write("cur-sid")
    bot.update_state(last_ts=time.time())
    msg = lambda mid, text, reply=None: {"message": {"chat": {"id": 42, "type": "private"}, "message_id": mid,
                                                     "text": text, **({"reply_to_message": {"message_id": reply}}
                                                                      if reply else {})}}
    while not bot.JOBS.empty():
        bot.JOBS.get()
    # 1) normal round on the current session → its answer (msg 100) is mapped to cur-sid
    bot.handle_update(msg(1, "halo"))
    script = [(lambda c: True, [_res("hai", sid="cur-sid")])]
    _with_fake_claude(script, lambda: bot.handle_job(bot.JOBS.get_nowait()))
    assert bot.load_threads() == {"100": "cur-sid"}
    assert reacts == [(1, "👀"), (1, "👨‍💻"), (1, "👍")]        # seen → working → done
    # 2) session rotated; replying to answer 100 resumes cur-sid, current session untouched
    open(bot.SESSION_FILE, "w").write("new-cur")
    bot.update_state(ctx=100)
    bot.handle_update(msg(2, "!lanjut tugas itu", reply=100))
    job = bot.JOBS.get_nowait()
    assert job[2] == "lanjut tugas itu" and job[4] == {"thread": "cur-sid", "heavy": True, "mids": [2]}
    bot.CONFIG["auto_model"] = {"light": "sonnet", "heavy": "opus"}
    script = [(lambda c: True, [_res("lanjut ok", sid="cur-sid")])]
    _with_fake_claude(script, lambda: bot.handle_job(job))
    cmd = FakePopen.calls[0]
    assert cmd[cmd.index("--resume") + 1] == "cur-sid" and cmd[cmd.index("--model") + 1] == "opus"
    assert open(bot.SESSION_FILE).read() == "new-cur" and bot.load_state()["ctx"] == 100
    assert bot.load_threads()["101"] == "cur-sid"           # replying to the new answer keeps the thread
    # 3) reply into a deleted session → falls back to the current one
    bot.handle_update(msg(3, "x", reply=100))
    job = bot.JOBS.get_nowait()
    script = [(lambda c: "cur-sid" in c, ["No conversation found with session ID: cur-sid\n"]),
              (lambda c: True, [_res("ok", sid="new-cur")])]
    _with_fake_claude(script, lambda: bot.handle_job(job))
    assert FakePopen.calls[1][FakePopen.calls[1].index("--resume") + 1] == "new-cur"
    assert open(bot.SESSION_FILE).read() == "new-cur"
    # 4) unknown reply target / lone "!" → plain message / ignored
    bot.handle_update(msg(4, "y", reply=5))
    assert bot.JOBS.get_nowait()[4] == {"thread": "", "heavy": False, "mids": [4]}
    # a failed round → 💔
    reacts.clear()
    script = [(lambda c: True, [_res("API Error: 500", is_error=True)])]
    _with_fake_claude(script, lambda: bot.handle_job(("msg", "42", "z", 6, {"thread": "", "heavy": False, "mids": [6]})))
    assert reacts == [(6, "👨‍💻"), (6, "💔")]
    bot.handle_update(msg(5, " ! "))
    assert bot.JOBS.empty()
    bot.CONFIG["auto_model"] = {}
    bot.THREADS_FILE, bot.owner_chat_id, bot.send, bot.react = orig
    restore()


def test_claude_slot_serializes():
    order = []

    def hold(tag):
        with bot.claude_slot():
            order.append(f"{tag}+")
            time.sleep(0.2)
            order.append(f"{tag}-")
    a = threading.Thread(target=hold, args=("a",))
    a.start()
    time.sleep(0.05)
    hold("b")
    a.join()
    assert order == ["a+", "a-", "b+", "b-"]                 # never two claude at once
    os.environ["SUPERBOT_ROUND"] = "1"                         # inside a bot round: no self-deadlock
    with bot.claude_slot() as s1:
        assert s1.f is None
    os.environ.pop("SUPERBOT_ROUND")


def test_templates_fit_prompt_cap():
    """Workspace files are cut at FILE_CAP chars in the prompt: shipped templates must fit whole."""
    for n in os.listdir(bot.TEMPLATES_DIR):
        assert len(open(os.path.join(bot.TEMPLATES_DIR, n)).read().strip()) <= bot.FILE_CAP, n


def test_example_confirm_patterns():
    """Example config: dangerous commands ask for ✅, everyday work never does."""
    pats = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.json")))["confirm_patterns"]
    hit = lambda c: any(bot.re.search(p, c) for p in pats)
    for c in ("rm -rf /", "sudo rm -rf ~", "rm -fr /etc", "rm -rf /home/alice", "rm -rf *",
              "mkfs.ext4 /dev/sdb1", "dd if=/dev/zero of=/dev/sda", "sudo reboot", "systemctl poweroff",
              "docker system prune -af", "docker compose down -v", "git push --force origin main", "git reset --hard HEAD~3",
              "curl -s https://x | sh", "psql -c 'drop table users'", "crontab -r", "sudo chmod -R 777 /",
              "sudo apt purge nginx", "ufw disable", "iptables -F",
              # killing processes it didn't start / deleting history (a "cleanup timer" did both once)
              "pkill -f mcp_server", "killall node", "kill 1234", 'kill "$pid"', "kill -9 $PID",
              "cat > ~/bin/clean.sh <<'EOF'\nkill \"$pid\"\nEOF",
              "find ~/.claude/projects -name '*.jsonl' -mtime +14 -delete", "rm -rf ~/.claude/projects/x"):
        assert hit(c), c
    for c in ("rm -f /tmp/out.txt", "rm -rf node_modules", "rm -rf /tmp/build", "systemctl --user restart superbot",
              "pm2 restart api", "docker restart serp", "git push origin main", "ufw status", "sudo iptables -L",
              "grep -rn 'rm -rf' .", "git commit -m 'fix: docker stop on shutdown'", "cat /var/log/x | grep rm",
              "chmod -R 755 public", "sudo apt upgrade", "docker compose down", "echo reboot needed",
              "pgrep -af claude", "ps aux | grep kill", "find . -name '*.pyc'", "skill check"):
        assert not hit(c), c


def test_approval_flow():
    d = _tmp_cfg("appr")
    orig_dir, orig_owner, orig_json, orig_tg = bot.APPROVALS_DIR, bot.owner_chat_id, bot.tg_json, bot.tg
    orig_cfg = dict(bot.CONFIG)
    bot.APPROVALS_DIR, bot.owner_chat_id = d, (lambda: "42")
    bot.CONFIG.update(confirm_patterns=[r"\brm\s+-\w*r"], confirm_timeout=3)
    asked = []

    def fake_json(method, payload):
        rid = payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"][3:]
        asked.append(rid)
        # owner presses a button (as the poll loop would) — after the hook started waiting
        threading.Timer(0.3, bot.handle_callback, [{"id": "c", "data": f"{verdict}:{rid}",
                        "message": {"chat": {"id": 42}, "message_id": 5, "text": "x"}}]).start()
        return {}
    bot.tg_json, bot.tg = fake_json, (lambda m, **p: {})
    hook_in = lambda c: json.dumps({"tool_name": "Bash", "tool_input": {"command": c}})
    verdict = "ok"
    assert bot.approve_hook(hook_in("ls -la")) == 0 and not asked          # no match → no prompt
    assert bot.approve_hook(hook_in("rm -rf /tmp/x")) == 0 and len(asked) == 1
    verdict = "no"
    assert bot.approve_hook(hook_in("sudo rm -r /srv")) == 2
    bot.handle_callback({"id": "c", "data": "ok:../../etc", "message": {"chat": {"id": 42}}})
    bot.handle_callback({"id": "c", "data": "ok:aaaaaaaaaaaa", "message": {"chat": {"id": 7}}})
    assert os.listdir(d) == []                             # bad id / stranger → ignored
    stale = f"{d}/aaaaaaaaaaaa"
    open(stale, "w").write("ok")
    os.utime(stale, (time.time() - 7200,) * 2)
    bot.approve_hook(hook_in("ls"))
    assert not os.path.exists(stale)                       # unread decision older than 1 h → swept
    bot.CONFIG.update(orig_cfg)
    bot.APPROVALS_DIR, bot.owner_chat_id, bot.tg_json, bot.tg = orig_dir, orig_owner, orig_json, orig_tg


def test_redact_and_model():
    orig_tok = bot.TOKEN
    bot.TOKEN = "123456:BOTTOKENxyz"
    bot.ENV["MY_API_KEY"] = "sk-supersecret-1"
    bot.ENV["ANTHROPIC_AUTH_TOKEN"] = "zai-secret-999"
    out = bot.redact("t=123456:BOTTOKENxyz k=sk-supersecret-1 z=zai-secret-999 path=/home ok")
    assert "BOTTOKEN" not in out and "supersecret" not in out and "zai-secret" not in out
    assert "path=/home ok" in out
    del bot.ENV["MY_API_KEY"], bot.ENV["ANTHROPIC_AUTH_TOKEN"]
    bot.TOKEN = orig_tok
    d = _tmp_cfg("model")
    restore = _workspace(d)
    sent = []
    orig_send = bot.send
    bot.send = lambda c, t, reply_to=None: sent.append(t)
    assert bot.handle_command("1", "/model opus") and bot.load_state()["model"] == "opus"
    script = [(lambda c: True, [_res("ok")])]
    _with_fake_claude(script, lambda: bot.run_claude("Q"))
    assert FakePopen.calls[0][FakePopen.calls[0].index("--model") + 1] == "opus"
    bot.handle_command("1", "/model default")
    assert bot.load_state()["model"] == ""
    assert not bot.handle_command("1", "/unknowncmd")      # falls through to Claude
    bot.send = orig_send
    restore()


def test_kill_tree():
    import subprocess as sp
    p = sp.Popen(["sh", "-c", "sleep 300 & sleep 300"], start_new_session=True)
    time.sleep(0.3)
    bot.kill_tree(p)
    p.wait(timeout=5)
    time.sleep(0.2)
    try:
        os.killpg(p.pid, 0)                                # group still has members?
        assert False, "child sleep survived"
    except ProcessLookupError:
        pass                                               # child sleep killed too


def test_health_report():
    orig_lang = bot.CONFIG["lang"]
    orig_checks = alert_check.CHECKS
    bot.CONFIG["lang"] = "en"
    alert_check.CHECKS = {"disk": lambda: (True, "ok"),
                          "units": lambda: (False, "unit gagal: bad.service"),
                          "docker": lambda: (True, "ok")}
    rep = bot.health_report()
    assert "✅ disk: ok" in rep
    assert "🚨 systemd units: unit gagal: bad.service" in rep
    bot.CONFIG["lang"] = orig_lang
    alert_check.CHECKS = orig_checks


def test_check_bot():
    d = _tmp_cfg("hb")
    orig_hb, orig_owner = alert_check.HEARTBEAT_FILE, alert_check.OWNER_FILE
    alert_check.HEARTBEAT_FILE, alert_check.OWNER_FILE = f"{d}/heartbeat", f"{d}/owner"
    ok, _ = alert_check.check_bot()
    assert ok                                            # fresh install: belum ada owner → no alarm
    with open(alert_check.OWNER_FILE, "w") as f:
        f.write("42")
    ok, msg = alert_check.check_bot()
    assert not ok and "heartbeat" in msg                 # owner ada, bot belum pernah jalan
    now = time.time()
    with open(alert_check.HEARTBEAT_FILE, "w") as f:
        f.write(str(now))
    assert alert_check.check_bot()[0]                    # heartbeat segar → ok
    os.utime(alert_check.HEARTBEAT_FILE, (now - 900, now - 900))
    ok, msg = alert_check.check_bot()
    assert not ok and "15" in msg                        # stale → alarm dengan umur menit
    alert_check.HEARTBEAT_FILE, alert_check.OWNER_FILE = orig_hb, orig_owner


def test_auto_model_quick_menu():
    orig = dict(bot.CONFIG)
    bot.CONFIG.update(auto_model={"light": "sonnet", "heavy": "opus"},
                      quick_commands={"/disk": "echo hai"})
    d = _tmp_cfg("pick")
    restore = _workspace(d)
    assert bot.heavy_prefix("!refactor bot.py") == ("refactor bot.py", True)
    assert bot.heavy_prefix("cek ! status") == ("cek ! status", False)
    assert bot.pick_model() == "sonnet" and bot.pick_model(True) == "opus"
    bot.update_state(model="haiku")                             # /model overrides light, not "!"
    assert bot.pick_model() == "haiku" and bot.pick_model(True) == "opus"
    bot.update_state(model="")
    bot.CONFIG["auto_model"] = {}
    assert bot.pick_model() == "" and bot.pick_model(True) == ""   # off → claude default
    restore()
    kb = bot.menu_keyboard()["keyboard"]
    assert kb[0][0]["text"] == "/disk" and all(len(r) <= 3 for r in kb)
    sent, real = [], bot.send
    bot.send = lambda c, t, reply_to=None: sent.append(t)
    bot.run_quick("1", "/disk")
    bot.send = real
    assert "hai" in sent[0]
    bot.CONFIG.clear(); bot.CONFIG.update(orig)

def test_forget_no_traversal_and_cli_unknown_exits():
    import tempfile
    d = tempfile.mkdtemp(); victim = f"{d}/victim"; open(victim, "w").write("x")
    orig = bot.MEMORY_DIR; bot.MEMORY_DIR = f"{d}/mem"; os.makedirs(bot.MEMORY_DIR)
    try:
        assert bot.forget("../victim") is False and os.path.exists(victim)
    finally:
        bot.MEMORY_DIR = orig
    for bad in (["--help"], ["--ask"], ["oops"]):
        try:
            bot.cli(bad); assert False, bad
        except SystemExit:
            pass
    assert bot.cli([]) is False


def test_undo_diff_stop_cost_ro():
    d = _tmp_cfg("undo")
    orig = (bot.UNDO_DIR, bot.owner_chat_id, bot.USAGE_FILE)
    bot.UNDO_DIR, bot.owner_chat_id, bot.USAGE_FILE = d + "/u", (lambda: "42"), d + "/usage.jsonl"
    os.environ["SUPERBOT_ROUND"] = "1000"
    f, new = d + "/a.txt", d + "/new.txt"
    open(f, "w").write("old\n")
    hook = lambda p, t="Edit": bot.approve_hook(json.dumps({"tool_name": t, "tool_input": {"file_path": p}}))
    assert hook(f) == 0 and hook(new, "Write") == 0 and hook(f) == 0
    open(f, "w").write("changed\n"); open(new, "w").write("x")
    assert "-old" in bot.diff_last() and "+changed" in bot.diff_last()
    bot.undo_last()
    assert open(f).read() == "old\n" and not os.path.exists(new)
    assert bot.undo_last() == bot._s("undo_none")
    # confirm_paths gate
    bot.CONFIG["confirm_paths"] = [r"/\.ssh/"]
    bot.CONFIG["confirm_timeout"] = 0
    assert hook("/home/x/.ssh/id") == 2 and hook(f) == 0
    bot.CONFIG["confirm_paths"] = []
    # /stop keeps /new, drops msgs
    while not bot.JOBS.empty(): bot.JOBS.get()
    dropped, real_react = [], bot.react
    bot.react = lambda c, m, e=bot.SEEN: dropped.append((m, e))
    bot.JOBS.put(("msg", "1", "a", 1, {"mids": [1]})); bot.JOBS.put(("rotate", "1"))
    bot.stop_current()
    bot.react = real_react
    assert dropped == [(1, "💔")]                             # dropped message is marked
    assert bot.JOBS.get_nowait()[0] == "rotate" and bot.JOBS.empty()
    # cost per day / model
    now = time.time()
    open(bot.USAGE_FILE, "w").write(json.dumps({"ts": now, "cost": 1.5, "model": "opus", "priced": True}) + "\n")
    r = bot.cost_report()
    assert "$1.50" in r and "opus" in r
    assert bot.jobs_report("log ../x") == "unit?"
    bot.UNDO_DIR, bot.owner_chat_id, bot.USAGE_FILE = orig
    os.environ.pop("SUPERBOT_ROUND")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
    print("ALL OK")
