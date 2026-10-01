#!/usr/bin/env python3
"""Jev pre-flight gate — Hermes shell hook on ``pre_llm_call``.

Hermes port of ``hooks/jev_gate.py`` (the WorkBuddy ``UserPromptSubmit`` hook in
this repo). Hermes has no UserPromptSubmit event: the equivalent is a shell hook
registered under ``pre_llm_call``, whose stdout ``{"context": "..."}`` is
appended to the current turn's user message (never the system prompt, so prompt
caching survives).

Wire:
  stdin  {hook_event_name, tool_name, tool_input, session_id, cwd, profile,
          extra: {user_message, conversation_history, is_first_turn, model,
                  platform, turn_id, task_id}}
  stdout {"context": "<verdict block>"} or {} for a silent no-op.

Hard rule (inherited): never block a prompt we cannot judge.
Every failure path exits 0 with a silent no-op.

Manual use (wired to /jev, /jev-on, /jev-off by install_hermes.py):
  jev_gate.py status | on | off | help
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

DEFAULT_API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"

HERMES = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
HOOK_DIR = os.path.join(HERMES, "agent-hooks")
CONFIG_FILE = os.path.join(HOOK_DIR, "jev_config.json")
LOG_FILE = os.path.join(HOOK_DIR, "jev_log.jsonl")
CIRCUIT_FILE = os.path.join(HOOK_DIR, "jev_circuit.json")
# Key lookup order: env, Hermes-local, then the WorkBuddy install (same vendor API).
KEY_FILES = [
    os.path.join(HERMES, ".typesafe_key"),
    os.path.join(os.path.expanduser("~"), ".workbuddy", ".typesafe_key"),
]

FAIL_THRESHOLD = 3
COOLDOWN = 600

DEFAULTS = {
    "enabled": True,
    "min_chars": 6,
    "api_timeout": 3.5,
    "history_turns": 20,
    "history_chars": 800,
    "memory_chars": 1500,
    "show_verdict": True,
    # Hermes runs one agent core across cron, gateway, desktop and CLI. Cron
    # report jobs must not pay the latency or carry routing hints.
    "skip_platforms": ["cron"],
    # Only issue a verdict when the model is actually going to answer a human.
    "api_url": "",
    "model": "",
}

NOISE = {
    "好的", "好", "可以", "嗯", "嗯嗯", "ok", "okay", "yes", "y", "继续", "是的",
    "对", "行", "收到", "thanks", "谢谢", "thanks!", "确认", "没问题", "go ahead",
}

STATE_BUDGET = 8000


def log(entry):
    try:
        os.makedirs(HOOK_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    os.makedirs(HOOK_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def circuit_open():
    try:
        with open(CIRCUIT_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if time.time() < float(st.get("open_until", 0)):
            return True
    except Exception:
        pass
    return False


def note_fail():
    try:
        try:
            with open(CIRCUIT_FILE, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
        fails = int(st.get("fails", 0)) + 1
        out = {"fails": fails, "last_fail": time.time()}
        if fails >= FAIL_THRESHOLD:
            out["open_until"] = time.time() + COOLDOWN
        with open(CIRCUIT_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f)
    except Exception:
        pass


def note_ok():
    try:
        with open(CIRCUIT_FILE, "w", encoding="utf-8") as f:
            json.dump({"fails": 0}, f)
    except Exception:
        pass


def set_enabled(value):
    cfg = load_config()
    cfg["enabled"] = bool(value)
    save_config(cfg)


def load_key():
    key = (os.environ.get("TYPESAFE_API_KEY") or "").strip()
    if key:
        return key
    for path in KEY_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                k = f.read().strip()
            if k:
                return k
        except Exception:
            continue
    return ""


# ---------------------------------------------------------------- state build

NOISE_RE = re.compile(r"<system-reminder[^>]*>.*?</system-reminder>", re.S)
QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.S)
TEXT_PART_TYPES = ("input_text", "output_text", "text")


def as_text(value):
    """Flatten a Hermes/OpenAI message content field to plain text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for c in value:
            if isinstance(c, dict) and c.get("type") in TEXT_PART_TYPES:
                parts.append(str(c.get("text", "")))
            elif isinstance(c, str):
                parts.append(c)
        return " ".join(parts)
    if value is None:
        return ""
    return str(value)


def clean_text(raw):
    """Strip framework scaffolding so only what the human typed remains."""
    t = NOISE_RE.sub("", as_text(raw))
    m = QUERY_RE.search(t)
    if m:
        t = m.group(1)
    return " ".join(t.split())


def recent_turns(history, count, limit):
    """Last `count` user/assistant turns from the hook's conversation_history.

    Hermes hands the hook the live OpenAI-format message list, so there is no
    transcript file to parse (the WorkBuddy bug where history silently came
    back empty cannot recur here).
    """
    if not isinstance(history, list) or count <= 0:
        return []
    out = []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") not in ("user", "assistant"):
            continue
        text = clean_text(msg.get("content"))[:limit]
        if text:
            out.append({"role": msg["role"], "text": text})
    return out[-count:]


def load_memory(cwd, limit):
    """Hermes memory + user profile, truncated.

    This is what resolves references like "这个 skill" or "是否在走" — a short
    follow-up with no state looks under-specified to the judge model.
    """
    files = []
    cands = [
        os.path.join(HERMES, "memories", "USER.md"),
        os.path.join(HERMES, "memories", "MEMORY.md"),
    ]
    if cwd:
        cands.append(os.path.join(cwd, "AGENTS.md"))
    for p in cands:
        try:
            with open(p, encoding="utf-8") as f:
                txt = f.read().strip()
        except Exception:
            continue
        if txt:
            files.append((os.path.basename(p), txt))
    if not files:
        return ""
    # Split the budget across the files instead of letting the first one eat it
    # all: a truncated USER.md with no MEMORY.md loses exactly the environment
    # facts a short follow-up needs to resolve.
    share = max(300, limit // len(files))
    parts = []
    for name, txt in files:
        parts.append("### %s\n%s" % (name, txt[:share]))
    return "\n\n".join(parts)[:limit]


def build_state(payload, cfg):
    extra = payload.get("extra") or {}
    prompt = as_text(extra.get("user_message"))[:3000]
    cwd = payload.get("cwd") or extra.get("cwd") or ""
    mem = load_memory(cwd, int(cfg.get("memory_chars", 1500)))
    turns = recent_turns(
        extra.get("conversation_history"),
        int(cfg.get("history_turns", 20)),
        int(cfg.get("history_chars", 800)),
    )
    budget = STATE_BUDGET - len(mem) - len(prompt)
    kept, used = [], 0
    for t in reversed(turns):
        if used + len(t["text"]) > budget:
            break
        kept.append(t)
        used += len(t["text"])
    kept.reverse()
    return {
        "new_message": prompt,
        "cwd": cwd,
        "project_memory": mem,
        "recent_turns": kept,
    }


def questions():
    return {
        "task_type": {
            "type": "choice",
            "instructions": "What kind of work is `new_message` asking for?",
            "criteria": {
                "lookup": "Find or explain a fact, no artifact produced.",
                "create": "Produce new content, a document, code, or a deliverable.",
                "operate": "Act on the system: files, commands, sending, publishing.",
                "analyze": "Inspect, diagnose, or analyze existing data or state.",
                "configure": "Change settings, install, or wire up tooling.",
                "chat": "Casual, acknowledgement, or a one-line reply suffices.",
            },
        },
        "needs_history": {
            "type": "noul",
            "instructions": (
                "Correctly handling `new_message` requires context from "
                "`recent_turns` or prior project state."
            ),
        },
        "needs_files": {
            "type": "noul",
            "instructions": (
                "Executing `new_message` requires reading files under `cwd` "
                "before anything else can happen."
            ),
        },
        "answers_on_disk": {
            "type": "noul",
            "instructions": (
                "Answering `new_message` requires looking at the filesystem: "
                "listing directories or opening files, as opposed to relying "
                "on `recent_turns` and `project_memory` alone."
            ),
        },
        "is_correction": {
            "type": "noul",
            "instructions": (
                "`new_message` is correcting, rejecting, or redirecting a "
                "previous answer in `recent_turns`."
            ),
        },
        "ambiguity": {
            "type": "score",
            "instructions": (
                "How underspecified is `new_message`? Judge the request as it "
                "stands with `recent_turns` and `project_memory` available. A "
                "short message is NOT underspecified when those resolve its "
                "references. Only score high when missing information cannot "
                "be recovered from the state at all."
            ),
            "criteria": [
                "Fully specified, can act immediately.",
                "Minor gaps, reasonable defaults exist.",
                "Materially ambiguous, must ask before acting.",
            ],
        },
        "risk": {
            "type": "score",
            "instructions": (
                "How much irreversible damage could acting on `new_message` cause?"
            ),
            "criteria": [
                "Read-only or fully reversible.",
                "Has side effects but recoverable.",
                "Irreversible: delete, send, publish, pay, overwrite.",
            ],
        },
        "scope": {
            "type": "choice",
            "instructions": "What is the blast radius of `new_message`?",
            "criteria": {
                "point": "A single self-contained question.",
                "file": "One or a few files.",
                "project": "Spans a whole project or folder.",
                "cross": "Spans multiple projects or external systems.",
            },
        },
        "depth": {
            "type": "choice",
            "instructions": "How much output does `new_message` expect?",
            "criteria": {
                "one_liner": "A sentence or two.",
                "short": "A short paragraph or quick edit.",
                "deliverable": "A complete artifact: report, file, app, plan.",
            },
        },
        "skill_route": {
            "type": "choice",
            "instructions": (
                "Which capability should own `new_message`? Pick "
                "`none` if no specialized workflow applies."
            ),
            "criteria": {
                "none": "General work, no specialized workflow needed.",
                "report": "A research or intelligence report.",
                "code": "Writing, debugging, or refactoring code.",
                "data": "Working with spreadsheets or datasets.",
                "writing": "Long-form writing or document production.",
                "ops": "System, deploy, or tooling operations.",
                "diagnosis": "Verify, check, or confirm that something works.",
            },
        },
    }


def call_jev(key, state, timeout, cfg):
    api_url = (os.environ.get("TYPESAFE_API_URL")
               or cfg.get("api_url") or DEFAULT_API_URL)
    model = cfg.get("model") or DEFAULT_MODEL
    body = json.dumps(
        {"state": state, "model": model, "questions": questions()}
    ).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=body,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fmt(answers):
    """Render verdicts into a compact one-screen brief for the model."""
    def pick(k, field, dash="?"):
        a = answers.get(k) or {}
        v = a.get(field)
        return dash if v is None else v

    def conf(k):
        c = (answers.get(k) or {}).get("confidence")
        return "" if c is None else "(%.2f)" % float(c)

    def noul(k):
        v = (answers.get(k) or {}).get("noul")
        if v is None:
            return "?"
        return "yes" if float(v) >= 0.5 else "no"

    def confv(k):
        try:
            return float((answers.get(k) or {}).get("confidence"))
        except Exception:
            return 0.0

    def noulv(k):
        try:
            return float((answers.get(k) or {}).get("noul"))
        except Exception:
            return 0.5

    def risk_label(v):
        try:
            v = int(round(float(v)))
        except Exception:
            return "?"
        return {0: "low", 1: "medium", 2: "high"}.get(v, "?")

    def amb_label(v):
        try:
            v = int(round(float(v)))
        except Exception:
            return "?"
        return {0: "clear", 1: "loose", 2: "ambiguous"}.get(v, "?")

    task = pick("task_type", "choice")
    head = "task=%s%s | history=%s | files=%s | correction=%s" % (
        task, conf("task_type"), noul("needs_history"), noul("needs_files"),
        noul("is_correction"),
    )
    tail = "ambiguity=%s | risk=%s | scope=%s | depth=%s | route=%s" % (
        amb_label(pick("ambiguity", "score")),
        risk_label(pick("risk", "score")),
        pick("scope", "choice"),
        pick("depth", "choice"),
        pick("skill_route", "choice"),
    )

    # Prohibitions come first: they are the ones that save tokens. A skipped
    # grep is worth tens of thousands of tokens; a skipped clarification round
    # is a full context replay. Only issue one when the verdict is decisive —
    # a wrong prohibition makes the model guess instead of looking.
    notes = []
    task_v = pick("task_type", "choice")
    decisive = confv("task_type") >= 0.5
    scope_v = pick("scope", "choice")
    amb = amb_label(pick("ambiguity", "score"))

    # A "don't look" prohibition next to "ask one question" hands the model
    # contradictory orders and it resolves the conflict by guessing. When the
    # verdict is ambiguous, the clarification cue wins and the anti-exploration
    # prohibitions are withheld.
    if (decisive and not amb == "ambiguous"
            and noulv("needs_files") < 0.2 and noulv("answers_on_disk") < 0.2
            and scope_v == "point"
            and task_v not in ("operate", "create", "configure")):
        notes.append("do NOT read files or grep — nothing on disk is needed")
    if (decisive and amb != "ambiguous" and scope_v == "point"
            and task_v != "operate" and confv("scope") >= 0.6):
        notes.append("do NOT scan the project structure")
    if decisive and pick("depth", "choice") == "one_liner" and confv("depth") >= 0.6:
        notes.append("answer in 1-2 sentences, produce no artifact")
    if decisive and amb == "clear":
        notes.append("do NOT ask to confirm — act now")

    if noul("is_correction") == "yes":
        notes.append("user is redirecting a prior answer — re-read it before continuing")
    if noul("needs_files") == "yes":
        notes.append("read the relevant files first, do not guess at contents")
    if risk_label(pick("risk", "score")) == "high":
        notes.append("irreversible action — confirm scope before executing")
    if amb == "ambiguous":
        notes.append("under-specified — ask one clarifying question before acting")

    return head, tail, notes


def render(answers, cfg):
    head, tail, notes = fmt(answers)
    block = "[Jev pre-flight]\n" + head + "\n" + tail
    if notes:
        block += "\ncues: " + "; ".join(notes)
    if cfg.get("show_verdict"):
        block += ("\nEcho this verdict as the first line of your reply, prefixed "
                  "with `Jev:`, so the user can see it.")
    block += ("\nTreat these as routing hints, not as a substitute for reading "
              "the actual request.")
    return block


def emit_context(text):
    print(json.dumps({"context": text}, ensure_ascii=False))


def emit_nothing():
    """Silent no-op: `{}` is a valid, non-blocking shell-hook response."""
    print("{}")


# ------------------------------------------------------------ manual commands

def handle_cli(argv):
    cmd = (argv[1] if len(argv) > 1 else "status").strip().lower()
    cfg = load_config()
    if cmd in ("on", "enable"):
        set_enabled(True)
        note_ok()
        print("Jev 前置判读：已开启（下一条消息起生效，无需重启）")
    elif cmd in ("off", "disable"):
        set_enabled(False)
        print("Jev 前置判读：已关闭（下一条消息起生效，无需重启）")
    elif cmd == "status":
        key_ok = bool(load_key())
        print("Jev 前置判读：%s | key：%s | 链路：%s | 平台跳过：%s | 超时 %ss | 历史 %s轮×%s字 | 回显 %s"
              % ("开启" if cfg.get("enabled") else "关闭",
                 "已配置" if key_ok else "缺失",
                 "熔断中" if circuit_open() else "正常",
                 ",".join(cfg.get("skip_platforms") or []) or "-",
                 cfg.get("api_timeout"), cfg.get("history_turns"),
                 cfg.get("history_chars"),
                 "开" if cfg.get("show_verdict") else "关"))
        print("用法：/jev | /jev-on | /jev-off")
    else:
        print("用法：/jev（状态） | /jev-on | /jev-off")
    return 0


def main():
    cfg = load_config()
    # Hermes forbids a hook from aborting the turn, so there is no "clear the
    # prompt" exit path here: the on/off switch is a quick_command instead.
    try:
        payload = json.load(sys.stdin)
    except Exception:
        emit_nothing()
        return

    extra = payload.get("extra") or {}
    prompt = clean_text(extra.get("user_message"))
    platform = (extra.get("platform") or "").strip()

    if not cfg.get("enabled"):
        emit_nothing()
        return
    if platform and platform in (cfg.get("skip_platforms") or []):
        emit_nothing()
        return
    if len(prompt) < int(cfg.get("min_chars", 6)):
        emit_nothing()
        return
    if prompt.lower().strip("。.!！ ") in NOISE or prompt.startswith("/"):
        emit_nothing()
        return

    key = load_key()
    if not key:
        log({"ts": time.time(), "event": "no_key", "prompt": prompt[:120]})
        emit_nothing()
        return
    if circuit_open():
        log({"ts": time.time(), "event": "circuit_open"})
        emit_nothing()
        return

    state = build_state(payload, cfg)
    t0 = time.time()
    try:
        resp = call_jev(key, state, float(cfg.get("api_timeout", 3.5)), cfg)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            detail = ""
        note_fail()
        log({"ts": t0, "event": "http_error", "code": e.code, "detail": detail})
        emit_nothing()
        return
    except Exception as e:
        note_fail()
        log({"ts": t0, "event": "api_error", "err": repr(e)[:200]})
        emit_nothing()
        return

    ms = int((time.time() - t0) * 1000)
    answers = (resp.get("answers") or {})
    if not answers:
        note_fail()
        log({"ts": t0, "event": "empty_answers"})
        emit_nothing()
        return
    note_ok()

    log({"ts": t0, "ms": ms, "prompt": prompt[:120], "answers": answers})
    emit_context(render(answers, cfg))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(handle_cli(sys.argv))
    try:
        main()
    except Exception as e:
        log({"ts": time.time(), "event": "unhandled", "err": repr(e)[:200]})
        emit_nothing()
