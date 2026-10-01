#!/usr/bin/env python3
"""Jev pre-flight gate — WorkBuddy UserPromptSubmit hook.

Runs before WorkBuddy assembles context. Sends the incoming prompt (plus a
little recent history) to Jev (TypeSafe System One) for a fast structured
judgment, and injects the verdict as additionalContext.

Hard rule: this hook must never block a prompt it cannot judge.
Every failure path exits 0 with no stdout (fail-open).
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
HOME = os.path.expanduser("~")
WB = os.path.join(HOME, ".workbuddy")
KEY_FILE = os.path.join(WB, ".typesafe_key")
CONFIG_FILE = os.path.join(WB, "hooks", "jev_config.json")
LOG_FILE = os.path.join(WB, "hooks", "jev_log.jsonl")
CIRCUIT_FILE = os.path.join(WB, "hooks", "jev_circuit.json")

# After this many consecutive failures, stop calling the API for COOLDOWN
# seconds. Without this, an unreachable endpoint taxes every prompt by
# api_timeout seconds.
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
}

# Prompts that are pure acknowledgements — not worth an API call.
NOISE = {
    "好的", "好", "可以", "嗯", "嗯嗯", "ok", "okay", "yes", "y", "继续", "是的",
    "对", "行", "收到", "thanks", "谢谢", "thanks!", "确认", "没问题", "go ahead",
}


def log(entry):
    try:
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


def circuit_open():
    """True when recent failures mean we should skip the API entirely."""
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


def save_enabled(enabled):
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = dict(DEFAULTS)
    cfg["enabled"] = bool(enabled)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def handle_command(prompt):
    """Self-service switch, consumed here so it never reaches the model.

    Exit code 2 clears the prompt and shows stdout to the user only: the
    command costs no model tokens and is never judged by Jev itself.
    """
    cmd = " ".join(prompt.strip().lower().split())
    if not cmd.startswith("/jev"):
        return

    if cmd in ("/jev", "/jev help"):
        print("用法：/jev on | /jev off | /jev status")
        sys.exit(2)

    if cmd == "/jev status":
        cfg = load_config()
        state = "开启" if cfg.get("enabled") else "关闭"
        circ = "熔断中" if circuit_open() else "正常"
        print("Jev 前置判读：%s | 链路：%s | 超时 %ss | 历史 %s轮×%s字 | 回显 %s"
              % (state, circ, cfg.get("api_timeout"), cfg.get("history_turns"),
                 cfg.get("history_chars"),
                 "开" if cfg.get("show_verdict") else "关"))
        sys.exit(2)

    if cmd == "/jev on":
        save_enabled(True)
        note_ok()
        print("Jev 前置判读已开启")
        sys.exit(2)

    if cmd == "/jev off":
        save_enabled(False)
        print("Jev 前置判读已关闭")
        sys.exit(2)

    print("无法识别的 Jev 命令，用法：/jev on | /jev off | /jev status")
    sys.exit(2)


def load_key():
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


NOISE_RE = re.compile(r"<system-reminder[^>]*>.*?</system-reminder>", re.S)
QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.S)


def clean_text(raw):
    """Strip framework scaffolding so only what the human actually said remains.

    A raw user turn can be ~4800 chars of which ~4600 is <system-reminder>
    (user_info, identity, hook output). Feeding that back is pure noise.
    """
    t = NOISE_RE.sub("", raw)
    m = QUERY_RE.search(t)
    if m:
        t = m.group(1)
    return " ".join(t.split())


def tail_lines(path, count, limit):
    """Read the last meaningful turns from a jsonl transcript without slurping it.

    Layout is flat: {"type":"message","role":...,"content":[{"type":"input_text"
    |"output_text","text":...}]}. Not nested under a "message" key.
    """
    if not path or not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
        chunk = min(size, 524288)
        with open(path, "rb") as f:
            f.seek(size - chunk)
            raw = f.read().decode("utf-8", "ignore")
    except Exception:
        return []
    lines = [ln for ln in raw.split("\n") if ln.strip()]
    out = []
    # Scan every line in the tail, not a fixed line window: turns are
    # interleaved with function_call / tool_result rows of wildly varying
    # size, so a line-count window silently drops user turns.
    for ln in lines[1:]:
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if obj.get("type") != "message":
            continue
        role = obj.get("role")
        if role not in ("user", "assistant"):
            continue
        content = obj.get("content")
        parts = []
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") in (
                    "input_text", "output_text", "text"
                ):
                    parts.append(c.get("text", ""))
        elif isinstance(content, str):
            parts.append(content)
        text = clean_text(" ".join(parts))[:limit]
        if text:
            out.append({"role": role, "text": text})
    if count <= 0:
        return []
    return out[-count:]


def load_memory(cwd, limit):
    """Project + user long-term memory, truncated to `limit` chars.

    This is what resolves references like "this skill" or "is it running".
    Without it a short follow-up looks under-specified to Jev — feeding more
    history alone does not fix references that point at prior state.
    """
    parts = []
    total = 0
    cands = []
    if cwd:
        base = os.path.join(cwd, ".workbuddy", "memory")
        cands.append(os.path.join(base, time.strftime("%Y-%m-%d") + ".md"))
        cands.append(os.path.join(base, "MEMORY.md"))
    cands.append(os.path.join(WB, "MEMORY.md"))
    for p in cands:
        try:
            with open(p, encoding="utf-8") as f:
                txt = f.read().strip()
        except Exception:
            continue
        if not txt:
            continue
        parts.append("### %s\n%s" % (os.path.basename(p), txt))
        total += len(txt)
        if total >= limit:
            break
    return "\n\n".join(parts)[:limit]


# Total character budget for the state we hand to Jev. Jev is a judgment
# model, not a reasoning one — more context helps resolve references, but
# past a point it is just cost and noise.
STATE_BUDGET = 8000


def build_state(prompt, cwd, transcript, cfg):
    """Assemble the state within a hard character budget.

    Priority: the new message, then project memory, then as much recent
    history as the budget allows, newest first.
    """
    mem = load_memory(cwd, int(cfg.get("memory_chars", 1500)))
    msg = prompt[:3000]
    turns = tail_lines(
        transcript,
        int(cfg.get("history_turns", 20)),
        int(cfg.get("history_chars", 800)),
    )
    budget = STATE_BUDGET - len(mem) - len(msg)
    kept, used = [], 0
    for t in reversed(turns):
        if used + len(t["text"]) > budget:
            break
        kept.append(t)
        used += len(t["text"])
    kept.reverse()
    return {
        "new_message": msg,
        "cwd": cwd,
        "project_memory": mem,
        "recent_turns": kept,
    }


def questions():
    return {
        "task_type": {
            "type": "choice",
            "instructions": (
                "What kind of work is `new_message` asking for?"
            ),
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
        # Asked the other way round. "needs_files" alone misses cases like
        # "what skills are in this project", which is not phrased as a file
        # read but can only be answered by listing a directory. Both must
        # say no before we forbid the model from touching disk.
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
    lines = [
        "task=%s%s | history=%s | files=%s | correction=%s",
        "ambiguity=%s | risk=%s | scope=%s | depth=%s | route=%s",
    ]
    head = lines[0] % (
        task, conf("task_type"), noul("needs_history"), noul("needs_files"),
        noul("is_correction"),
    )
    tail = lines[1] % (
        amb_label(pick("ambiguity", "score")),
        risk_label(pick("risk", "score")),
        pick("scope", "choice"),
        pick("depth", "choice"),
        pick("skill_route", "choice"),
    )

    # Prohibitions come first: they are the ones that save the model money.
    # A skipped grep is worth tens of thousands of tokens; a skipped
    # clarification round is worth a full context replay. Only issue a
    # prohibition when the verdict is decisive — a wrong one makes the
    # model guess instead of looking.
    notes = []
    task_v = pick("task_type", "choice")
    # If we cannot even tell what kind of task this is, every downstream
    # verdict is suspect — issue no prohibitions at all.
    decisive = confv("task_type") >= 0.5
    scope_v = pick("scope", "choice")

    if (decisive and noulv("needs_files") < 0.2 and noulv("answers_on_disk") < 0.2
            and scope_v == "point"
            and task_v not in ("operate", "create", "configure")):
        notes.append("do NOT read files or grep — nothing on disk is needed")
    if decisive and scope_v == "point" and task_v != "operate" and confv("scope") >= 0.6:
        notes.append("do NOT scan the project structure")
    if decisive and pick("depth", "choice") == "one_liner" and confv("depth") >= 0.6:
        notes.append("answer in 1-2 sentences, produce no artifact")
    if decisive and amb_label(pick("ambiguity", "score")) == "clear":
        notes.append("do NOT ask to confirm — act now")

    if noul("is_correction") == "yes":
        notes.append("user is redirecting a prior answer — re-read it before continuing")
    if noul("needs_files") == "yes":
        notes.append("read the relevant files first, do not guess at contents")
    if risk_label(pick("risk", "score")) == "high":
        notes.append("irreversible action — confirm scope before executing")
    if amb_label(pick("ambiguity", "score")) == "ambiguous":
        notes.append("under-specified — ask one clarifying question before acting")

    return head, tail, notes


def emit(text):
    print(json.dumps({
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        },
    }, ensure_ascii=False))


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    prompt = (payload.get("prompt") or "").strip()
    handle_command(prompt)

    cfg = load_config()

    # Fast path: never spend an API call on trivial input.
    if not cfg.get("enabled"):
        sys.exit(0)
    if len(prompt) < int(cfg.get("min_chars", 6)):
        sys.exit(0)
    if prompt.lower().strip("。.!！ ") in NOISE or prompt.startswith("/"):
        sys.exit(0)

    key = load_key()
    if not key:
        log({"ts": time.time(), "event": "no_key", "prompt": prompt[:120]})
        sys.exit(0)
    if circuit_open():
        log({"ts": time.time(), "event": "circuit_open"})
        sys.exit(0)

    state = build_state(prompt, payload.get("cwd", ""),
                        payload.get("transcript_path"), cfg)

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
        sys.exit(0)
    except Exception as e:
        note_fail()
        log({"ts": t0, "event": "api_error", "err": repr(e)[:200]})
        sys.exit(0)

    ms = int((time.time() - t0) * 1000)
    answers = (resp.get("answers") or {})
    if not answers:
        note_fail()
        log({"ts": t0, "event": "empty_answers"})
        sys.exit(0)
    note_ok()

    head, tail, notes = fmt(answers)
    block = "[Jev pre-flight]\n" + head + "\n" + tail
    if notes:
        block += "\ncues: " + "; ".join(notes)
    if cfg.get("show_verdict"):
        block += "\nEcho this verdict as the first line of your reply, prefixed with `Jev:`, so the user can see it."
    block += "\nTreat these as routing hints, not as a substitute for reading the actual request."

    log({"ts": t0, "ms": ms, "prompt": prompt[:120], "answers": answers})
    emit(block)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log({"ts": time.time(), "event": "unhandled", "err": repr(e)[:200]})
        sys.exit(0)
