#!/usr/bin/env python3
"""Ad-hoc Jev call for mid-task judgments.

Usage:
  python3 jev_ask.py '{"state": "...", "questions": {...}}'
  python3 jev_ask.py questions.json

Reads a JSON object with `state` and `questions` from a file or argv,
calls Jev, prints the raw answer JSON. Exits non-zero on failure.
"""

import json
import os
import sys
import urllib.request

API_URL = "https://api.typesafe.ai/v1/systemone"
KEY_FILE = os.path.join(os.path.expanduser("~"), ".workbuddy", ".typesafe_key")


def load_key():
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    with open(KEY_FILE, encoding="utf-8") as f:
        return f.read().strip()


def main():
    if len(sys.argv) < 2:
        print("usage: jev_ask.py '<json>' | jev_ask.py file.json", file=sys.stderr)
        return 2
    src = sys.argv[1]
    if os.path.exists(src):
        with open(src, encoding="utf-8") as f:
            req = json.load(f)
    else:
        req = json.loads(src)

    body = json.dumps({
        "state": req["state"],
        "model": req.get("model", "jev-latest"),
        "questions": req["questions"],
    }).encode("utf-8")

    r = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": "Bearer " + load_key(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(r, timeout=20) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    print(json.dumps(out.get("answers", {}), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
