#!/usr/bin/env python3
"""Install the Jev pre-flight gate into a local Hermes install.

What it does, in order:
  1. Resolve the API key (env -> key file -> --key). No key means exit 2: the
     caller must go ask the human. Never invented, never skipped.
  2. Copy jev_gate.py + jev_config.json into $HERMES_HOME/agent-hooks/.
  3. Append the `pre_llm_call` shell hook and the /jev quick commands to
     $HERMES_HOME/config.yaml -- backing it up first, never registering twice,
     and never rewriting the file (comments and layout stay intact).
  4. Add the {event, command} pair to $HERMES_HOME/shell-hooks-allowlist.json,
     because a non-TTY Hermes (gateway, cron, desktop backend) can never answer
     the first-use consent prompt and would silently skip an unapproved hook.

Exit codes:
  0  installed (or already installed)
  1  install failed
  2  no API key available -- ask the user, then re-run with --key

Usage:
  python3 hermes/install_hermes.py                 # prompts when the key is missing
  python3 hermes/install_hermes.py --key <KEY>
  python3 hermes/install_hermes.py --hermes-home ~/.hermes --python $(which python3)
  python3 hermes/install_hermes.py --check         # verify an existing install
"""

import argparse
import json
import os
import re
import shutil
import stat
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_ENV = "TYPESAFE_API_KEY"
HOOK_NAME = "jev_gate.py"
HOOK_REL = os.path.join("agent-hooks", HOOK_NAME)
CONFIG_NAME = "jev_config.json"

# Edit this if the key comes from somewhere else. It is only ever shown to a
# human when the key is missing -- it is never sent anywhere.
KEY_HOWTO = "申请入口：https://typesafe.ai （控制台创建 API key）"

# Order matters: Hermes' own key file first, then the WorkBuddy install's, so a
# machine that already runs the WorkBuddy gate needs no second key.
KEY_FILES = [".typesafe_key", os.path.join("..", ".workbuddy", ".typesafe_key")]


def hermes_home(arg):
    home = arg or os.environ.get("HERMES_HOME") or "~/.hermes"
    return os.path.abspath(os.path.expanduser(home))


def find_key(home):
    """Returns (key, source). Env wins, then the key files the hook reads."""
    k = os.environ.get(KEY_ENV, "").strip()
    if k:
        return k, "env:" + KEY_ENV
    for rel in KEY_FILES:
        p = os.path.abspath(os.path.join(home, rel))
        try:
            with open(p, encoding="utf-8") as f:
                k = f.read().strip()
        except Exception:
            continue
        if k:
            return k, p
    return "", ""


def write_key(home, key):
    """Key file is 0600: nobody else on the box should read it."""
    p = os.path.join(home, ".typesafe_key")
    os.makedirs(home, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(key.strip() + "\n")
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    return p


def copy_hook(home):
    dst_dir = os.path.join(home, "agent-hooks")
    os.makedirs(dst_dir, exist_ok=True)
    copied = []
    for name in (HOOK_NAME, CONFIG_NAME):
        src = os.path.join(HERE, name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(dst_dir, name)
        # Never clobber a config the user has tuned.
        if name == CONFIG_NAME and os.path.exists(dst):
            continue
        shutil.copy2(src, dst)
        if name.endswith(".py"):
            os.chmod(dst, 0o755)
        copied.append(dst)
    return copied


def _top_level_keys(path):
    """Top-level mapping keys, read without PyYAML.

    The installer must work under any python3 the user happens to run it with,
    and a bare framework interpreter usually has no PyYAML -- the first version
    of this script silently skipped the config edit because of that.
    """
    keys = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if not line or line[0] in " \t#-\n":
                    continue
                m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:", line)
                if m:
                    keys.append(m.group(1))
    except FileNotFoundError:
        return None
    except Exception as exc:
        print("config.yaml 读取失败：%s" % exc)
        return None
    return keys


def _validate_yaml(path):
    """Strict check when PyYAML is around; a key-count check otherwise.

    Returns "" when the file looks sound, else a human-readable reason.
    """
    keys = _top_level_keys(path)
    if keys is None:
        return "config.yaml 不可读"
    for k in ("hooks", "quick_commands"):
        if keys.count(k) > 1:
            return "config.yaml 出现重复的顶层 %s 键" % k
    try:
        import yaml
    except Exception:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            yaml.safe_load(f)
    except Exception as exc:
        return "YAML 解析失败：%s" % exc
    return ""


def merge_config(home, hook_cmd):
    """Append the hooks + quick_commands blocks without rewriting the file.

    Hermes' own `config set` re-dumps the whole YAML, which drops comments; the
    gate has no business reformatting a user's config, so we append text and
    verify the result.
    """
    path = os.path.join(home, "config.yaml")
    keys = _top_level_keys(path)
    if keys is None:
        if not os.path.exists(path):
            keys = []
        else:
            return path, False, "config.yaml 读取失败，未改动"

    need_hooks = "hooks" not in keys
    need_quick = "quick_commands" not in keys
    if not need_hooks and not need_quick:
        return path, False, "config.yaml 已包含 hooks / quick_commands，未改动"

    backup = path + ".bak-before-jev-" + time.strftime("%Y%m%d-%H%M%S")
    if os.path.exists(path):
        shutil.copy2(path, backup)
        print("已备份原配置：%s" % backup)

    blocks = []
    if need_hooks:
        blocks.append(
            "# ── Jev pre-flight gate (Hermes) ────────────────────────────────────\n"
            "# Shell hook on pre_llm_call; exit 0 + {} means \"no verdict this turn\".\n"
            "hooks:\n"
            "  pre_llm_call:\n"
            "    - command: \"%s\"\n"
            "      timeout: 10\n" % hook_cmd
        )
    if need_quick:
        blocks.append(
            "# /jev, /jev-on, /jev-off: exec quick commands -- they run the hook\n"
            "# script directly, so they cost no model tokens. Interactive CLI/TUI only.\n"
            "quick_commands:\n"
            "  jev:\n"
            "    type: exec\n"
            "    description: \"Jev 前置判读：状态\"\n"
            "    command: \"%s %s status\"\n"
            "  jev-on:\n"
            "    type: exec\n"
            "    description: \"Jev 前置判读：开启\"\n"
            "    command: \"%s %s on\"\n"
            "  jev-off:\n"
            "    type: exec\n"
            "    description: \"Jev 前置判读：关闭\"\n"
            "    command: \"%s %s off\"\n"
            % (sys.executable, os.path.join(home, HOOK_REL),
               sys.executable, os.path.join(home, HOOK_REL),
               sys.executable, os.path.join(home, HOOK_REL))
        )

    payload = ("\n" if os.path.exists(path) else "") + "\n".join(blocks)
    with open(path, "a", encoding="utf-8") as f:
        f.write(payload)

    problem = _validate_yaml(path)
    if problem:
        if os.path.exists(backup):
            shutil.copy2(backup, path)
        return path, False, "追加后校验失败（%s），已回滚；请按 README 手工添加片段" % problem
    return path, True, "已追加 hooks / quick_commands"


def merge_allowlist(home, hook_cmd):
    """Consent is per (event, exact command string); non-TTY installs never prompt."""
    path = os.path.join(home, "shell-hooks-allowlist.json")
    data = {"approvals": []}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            backup = path + ".bak-broken-" + time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(path, backup)
            print("shell-hooks-allowlist.json 不是合法 JSON，已备份到 %s。" % backup)
            data = {"approvals": []}
    approvals = data.setdefault("approvals", [])
    entry = {"event": "pre_llm_call", "command": hook_cmd}
    if entry in approvals:
        return path, False
    approvals.append(entry)
    os.makedirs(home, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path, True


def check(home):
    ok = True
    key, src = find_key(home)
    print("API key        : %s" % ("已配置（来源 %s）" % src if key else "缺失"))
    ok = ok and bool(key)

    script = os.path.join(home, HOOK_REL)
    installed = os.path.exists(script)
    print("hook 脚本      : %s" % (script if installed else "缺失"))
    ok = ok and installed

    registered = False
    config_path = os.path.join(home, "config.yaml")
    try:
        with open(config_path, encoding="utf-8") as f:
            text = f.read()
        registered = HOOK_NAME in text and "pre_llm_call" in text
    except Exception:
        registered = False
    print("config 注册    : %s" % ("已注册" if registered else "未注册"))
    ok = ok and registered

    allowed = False
    try:
        with open(os.path.join(home, "shell-hooks-allowlist.json"), encoding="utf-8") as f:
            for a in json.load(f).get("approvals", []):
                if a.get("event") == "pre_llm_call" and HOOK_NAME in str(a.get("command", "")):
                    allowed = True
    except Exception:
        pass
    print("hook 授权      : %s" % ("已授权" if allowed else "未授权（hook 不会触发）"))
    ok = ok and allowed

    print("\n复验命令：hermes hooks list && hermes hooks doctor")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hermes-home", default=None, help="Hermes home（默认 $HERMES_HOME 或 ~/.hermes）")
    ap.add_argument("--key", help="TypeSafe API key（也可以设 %s）" % KEY_ENV)
    ap.add_argument("--python", default=sys.executable,
                    help="写进 hook 命令的 python 解释器（默认当前解释器）")
    ap.add_argument("--check", action="store_true", help="只检查安装状态")
    args = ap.parse_args()

    home = hermes_home(args.hermes_home)
    if args.check:
        return check(home)

    key = (args.key or "").strip()
    if not key:
        key, _src = find_key(home)

    if not key:
        # Exit 2 is the contract with the installing agent: stop and ask the
        # human. Do not guess, do not continue without a key.
        print(
            "\n[需要你提供 API key]\n"
            "Jev 前置判读依赖 TypeSafe API key，当前未找到。\n"
            "%s\n\n"
            "任选一种方式提供后重跑：\n"
            "  1) python3 hermes/install_hermes.py --key <你的KEY>\n"
            "  2) export %s=<你的KEY> && python3 hermes/install_hermes.py\n"
            "  3) 把 KEY 写入 %s（单行，不要有多余空格换行）\n\n"
            "注意：key 只落在本机 %s，权限 0600，不会被写进 git 仓库。\n"
            % (KEY_HOWTO, KEY_ENV, os.path.join(home, ".typesafe_key"), home)
        )
        return 2

    if args.key:
        print("API key 已写入：%s" % write_key(home, key))

    for p in copy_hook(home):
        print("已安装 %s" % p)

    hook_cmd = "%s %s" % (args.python, os.path.join(home, HOOK_REL))
    path, changed, note = merge_config(home, hook_cmd)
    print("config.yaml    : %s（%s）" % (path, note))
    apath, achanged = merge_allowlist(home, hook_cmd)
    print("allowlist      : %s（%s）" % (apath, "已写入" if achanged else "已存在"))

    print(
        "\n完成。下一步：\n"
        "  1) 新开一轮会话（或重启 gateway）—— hook 在会话启动时快照，"
        "当前会话不会加载它。\n"
        "  2) 交互式界面里输入 /jev 验证链路（exec 快捷命令，不进模型、零 token）。\n"
        "  3) 关闭用 /jev-off，重开 /jev-on；也可以改 %s\n"
        "  4) 复核：hermes hooks list && hermes hooks doctor\n"
        % os.path.join(home, "agent-hooks", CONFIG_NAME)
    )
    return 0 if changed or achanged else 0


if __name__ == "__main__":
    sys.exit(main())
