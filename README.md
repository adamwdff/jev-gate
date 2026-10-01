# Jev Pre-flight Gate

在 WorkBuddy 里，**模型读你的提示词之前**，先让 Jev（TypeSafe System One 判定模型）花 ~1 秒对这次请求做一次结构化判定，把结论注入给主模型当路由提示。

一个 `UserPromptSubmit` hook + 两个脚本，零依赖、纯标准库 Python。

---

## 它解决什么

主模型的钱主要花在三处：多余的工具调用（一次 grep 5k–5 万 token）、多余的往返轮次（每轮 = 整个上下文重发一次）、控制不住的输出长度。

Jev 在提示词进来的一瞬间先判一次，把"这件事不需要读文件""这件事一两句话答完""这件事别反问确认"这类**禁止性**提示塞给模型。拦掉一次 grep 就赚回几千到几万 token。

实测注入成本约 300–400 token/轮，对本来就不会读文件的简单请求是净亏约 1%——期望值为正，但不是每条都赚。

---

## 一句话给自己装

```bash
git clone https://github.com/adamwdff/jev-gate.git && cd jev-gate && python3 install.py
```

缺 key 时脚本会 `exit 2` 并告诉你去哪儿拿。**装完必须完全退出重启 WorkBuddy**（hooks 在会话启动时快照）。

---

## 把链接给别人：让对方的 Agent 自己装

让对方把这个仓库链接丢给他的 agent 即可，agent 会读 `AGENT_INSTALL.md`。推荐提示词：

> 请读取 https://raw.githubusercontent.com/adamwdff/jev-gate/main/AGENT_INSTALL.md ，按里面的步骤帮我在本机 WorkBuddy 装好 Jev pre-flight gate。我没有 TypeSafe API key 时请你停下来问我要，不要自己编。

`AGENT_INSTALL.md` 里写死了三条硬约束，防止 agent 糊弄：

1. **没有 key 必须先停下来问人**——不许编造、不许用占位符、不许跳过。
2. **key 只落 `~/.workbuddy/.typesafe_key`（0600）**——不进 git、不进 `settings.json`、不在对话里回显。
3. **必须提示用户重启 WorkBuddy**——否则"装了没反应"。

---

## 判定输出长什么样

模型每轮会看到这样一段（用户可在回复首行看到 `Jev:` 回显）：

```
[Jev pre-flight]
task=operate(0.92) | history=yes | files=no | correction=no
ambiguity=loose | risk=medium | scope=project | depth=deliverable | route=ops
cues: do NOT ask to confirm — act now; read the relevant files first, do not guess at contents
```

九个维度：`task_type` / `needs_history` / `needs_files` / `answers_on_disk` / `is_correction` / `ambiguity` / `risk` / `scope` / `depth` / `skill_route`。

---

## 开关

| 命令 | 作用 |
|---|---|
| `/jev status` | 看开关状态、链路是否正常 |
| `/jev off` | 关闭（脚本仍在，只是不调 API） |
| `/jev on` | 开启，并重置熔断计数 |

命令由 hook **自消费**，走 exit 2 直接回显给用户，**不进模型、零模型 token**，也不会被 Jev 自己判定。

---

## 配置

`~/.workbuddy/hooks/jev_config.json`：

| 字段 | 默认 | 含义 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `min_chars` | `6` | 短于此不调 API |
| `api_timeout` | `3.5` | 超时即静默放行 |
| `history_turns` / `history_chars` | `20` / `800` | 喂给 Jev 的历史量 |
| `memory_chars` | `1500` | 喂给 Jev 的项目记忆量 |
| `show_verdict` | `true` | 让模型把判定回显给用户 |

---

## 仓库结构

```
hooks/jev_gate.py     UserPromptSubmit hook —— 核心
hooks/jev_ask.py      任务中途单次调用 Jev 的小工具
hooks/jev_config.json 默认配置
install.py            一键安装 / --check 体检
AGENT_INSTALL.md      给 AI Agent 的安装说明（引导填 key 在这）
docs/DESIGN.md        设计取舍与踩过的坑
```

---

## 安全说明

- **fail-open 铁律**：无 key、超时、报错、返回空——一律 `exit 0` 静默放行，**绝不阻塞用户提问**。
- **熔断**：连续 3 次失败后冷却 10 分钟不再调 API，否则网络不通时每条提问都被超时拖慢。
- **key 与配置分离**：key 在 `~/.workbuddy/.typesafe_key`（0600），不在 `settings.json`、不在仓库里。
- `.gitignore` 已屏蔽 `.typesafe_key`、`.env`、`jev_log.jsonl`。日志含 prompt 前 120 字，排错用，**别外传**。
