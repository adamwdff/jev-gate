#!/usr/bin/env python3
"""Install the Jev pre-flight gate into WorkBuddy.

What it does, in order:
  1. Resolve the API key (env -> key file -> --key -> interactive prompt).
     No key means exit 2: the caller must go ask the human. Never invented,
     never skipped.
  2. Copy hooks/*.py into ~/.workbuddy/hooks/ and make them executable.
  3. Merge the UserPromptSubmit hook into ~/.workbuddy/settings.json,
     backing the original up first and never registering twice.

Exit codes:
  0  installed (or already installed)
  1  install failed
  2  no API key available -- ask the user, then re-run with --key

Usage:
  python3 install.py                 # will prompt for the key if missing
  python3 install.py --key <KEY>
  python3 install.py --wb ~/.workbuddy --python $(which python3)
  python3 install.py --check         # verify an existing install
"""

import argparse
import json
import os
import shutil
import stat
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_ENV = "TYPESAFE_API_KEY"

# Edit this if your key comes from somewhere else. It is only ever shown to
# a human when the key is missing -- it is never sent anywhere.
KEY_HOWTO = (
    "申请入口：https://typesafe.ai （控制台创建 API key）"
)


def wb_dir(arg):
    return os.path.abspath(os.path.expanduser(arg or "~/.workbuddy"))


def find_key(wb):
    """Order matters: env wins, then the on-disk key file the hook reads."""
    k = os.environ.get(KEY_ENV, "").strip()
    if k:
        return k, "env:" + KEY_ENV
    p = os.path.join(wb, ".typesafe_key")
    try:
        with open(p, encoding="utf-8") as f:
            k = f.read().strip()
    except Exception:
        return "", ""
    return (k, p) if k else ("", "")


def write_key(wb, key):
    """Key file is 0600: nobody else on the box should read it."""
    os.makedirs(wb, exist_ok=True)
    p = os.path.join(wb, ".typesafe_key")
    with open(p, "w", encoding="utf-8") as f:
        f.write(key.strip() + "\n")
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    return p


def copy_hooks(wb):
    dst_dir = os.path.join(wb, "hooks")
    os.makedirs(dst_dir, exist_ok=True)
    copied = []
    for name in ("jev_gate.py", "jev_ask.py", "jev_config.json"):
        src = os.path.join(HERE, "hooks", name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(dst_dir, name)
        # Never clobber a local config the user has tuned.
        if name == "jev_config.json" and os.path.exists(dst):
            continue
        shutil.copy2(src, dst)
        if name.endswith(".py"):
            os.chmod(dst, 0o755)
        copied.append(dst)
    return copied


def merge_settings(wb, python_bin):
    """Register the hook without destroying whatever else is in settings.json."""
    path = os.path.join(wb, "settings.json")
    cfg = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        try:
            cfg = json.loads(raw)
        except Exception:
            backup = path + ".bak-broken-" + time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(path, backup)
            print("settings.json 不是合法 JSON，已备份到 %s，本次将新建。" % backup)
            cfg = {}
        else:
            backup = path + ".bak-before-jev-" + time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(path, backup)
            print("已备份原配置：%s" % backup)
    elif not os.path.exists(wb):
        os.makedirs(wb, exist_ok=True)

    hooks = cfg.setdefault("hooks", {})
    entries = hooks.setdefault("UserPromptSubmit", [])

    gate = os.path.join(wb, "hooks", "jev_gate.py")
    for entry in entries:
        for h in entry.get("hooks", []):
            if "jev_gate.py" in str(h.get("command", "")):
                print("settings.json 中已注册 Jev hook，跳过重复注册。")
                return path, False

    entries.append({
        "hooks": [{
            "type": "command",
            "command": '%s "%s"' % (python_bin, gate),
            "timeout": 15,
            "statusMessage": "Jev 判读中",
        }]
    })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return path, True


def check(wb):
    ok = True
    key, src = find_key(wb)
    print("API key      : %s" % ("已配置（来源 %s）" % src if key else "缺失"))
    if not key:
        ok = False
    gate = os.path.join(wb, "hooks", "jev_gate.py")
    print("hook 脚本    : %s" % ("已安装" if os.path.exists(gate) else "缺失"))
    if not os.path.exists(gate):
        ok = False
    path = os.path.join(wb, "settings.json")
    reg = False
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        for entry in cfg.get("hooks", {}).get("UserPromptSubmit", []):
            for h in entry.get("hooks", []):
                if "jev_gate.py" in str(h.get("command", "")):
                    reg = True
    except Exception:
        pass
    print("settings 注册: %s" % ("已注册" if reg else "未注册"))
    if not reg:
        ok = False
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wb", default="~/.workbuddy", help="WorkBuddy 配置目录")
    ap.add_argument("--key", help="TypeSafe API key（也可以设 %s）" % KEY_ENV)
    ap.add_argument("--python", default=sys.executable, help="运行 hook 的 python")
    ap.add_argument("--check", action="store_true", help="只检查安装状态")
    args = ap.parse_args()

    wb = wb_dir(args.wb)
    if args.check:
        return check(wb)

    key = (args.key or "").strip()
    if not key:
        key, _src = find_key(wb)

    if not key:
        # Exit 2 is the contract with the installing agent: stop and ask the
        # human. Do not guess, do not continue without a key.
        print(
            "\n[需要你提供 API key]\n"
            "Jev 前置判读依赖 TypeSafe API key，当前未找到。\n"
            "%s\n\n"
            "任选一种方式提供后重跑：\n"
            "  1) python3 install.py --key <你的KEY>\n"
            "  2) export %s=<你的KEY> && python3 install.py\n"
            "  3) 把 KEY 写入 %s（单行，不要有多余空格换行）\n\n"
            "注意：key 只落在本机 %s，权限 0600，不会被写进 git 仓库。\n"
            % (KEY_HOWTO, KEY_ENV, os.path.join(wb, ".typesafe_key"), wb)
        )
        return 2

    if args.key:
        print("API key 已写入：%s" % write_key(wb, key))

    for p in copy_hooks(wb):
        print("已安装 %s" % p)

    path, changed = merge_settings(wb, args.python)
    print("settings.json: %s" % ("已注入 hook" if changed else "无需改动"))

    print(
        "\n完成。下一步：\n"
        "  1) 完全退出并重启 WorkBuddy —— hooks 在会话启动时快照，不重启不生效。\n"
        "  2) 新开一轮对话输入 /jev status 验证链路。\n"
        "  3) 关闭用 /jev off，重开 /jev on（命令不进模型，零 token）。\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
