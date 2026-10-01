# Jev Pre-flight Gate — Hermes 版

在 **Hermes Agent** 里做同一件事：模型读你的提示词之前，先让 Jev 花 ~1 秒判定，把结论注入当路由提示。

这里是 `hooks/jev_gate.py`（WorkBuddy 版）的移植。Hermes 没有 `UserPromptSubmit` 事件——官方文档明确写了 Claude Code 的 `UserPromptSubmit` 对应 **`pre_llm_call`**，所以入口就是一个 shell hook。

---

## 一句话装

```bash
git clone https://github.com/adamwdff/jev-gate.git && cd jev-gate
python3 hermes/install_hermes.py            # 缺 key 时 exit 2 并告诉你去哪儿拿
# 新开一轮会话，然后在交互界面输入 /jev
```

装完**必须新开会话**（hook 在会话启动时快照）；gateway 用户需要 `hermes gateway restart`。

---

## 事件映射

| WorkBuddy | Hermes |
|---|---|
| `UserPromptSubmit` | shell hook `pre_llm_call` |
| stdin `{prompt, session_id, transcript_path, cwd}` | stdin `{hook_event_name, session_id, cwd, profile, extra:{user_message, conversation_history, is_first_turn, model, platform, ...}}` |
| stdout `hookSpecificOutput.additionalContext` | stdout `{"context": "..."}`；`{}` = 本轮不判读 |
| 注入 additional context | 追加到**本轮用户消息**（不进系统提示 → prompt cache 不破） |
| `/jev on\|off\|status` 由 hook 自消费（exit 2） | `quick_commands` `type: exec`（直接跑脚本，零模型 token） |
| 解析 transcript jsonl 取历史 | `extra.conversation_history` 就是活的 OpenAI 格式消息列表，无需解析 |

---

## 安装脚本做了什么

`hermes/install_hermes.py`，四步，全幂等：

1. 解析 key：`TYPESAFE_API_KEY` → `$HERMES_HOME/.typesafe_key` → `~/.workbuddy/.typesafe_key`。**都没有就 exit 2 停下来问人**（不许编造、不许跳过）。
2. 复制 `jev_gate.py` + `jev_config.json` 到 `$HERMES_HOME/agent-hooks/`（已存在的 `jev_config.json` 不覆盖，那是你调过的）。
3. **追加**（不是重写）`$HERMES_HOME/config.yaml` 的 `hooks.pre_llm_call` 与三条 `quick_commands`；先备份，已存在同名顶层键就跳过，追加后校验 YAML，失败自动回滚。
4. 往 `$HERMES_HOME/shell-hooks-allowlist.json` 写 `{event, command}` —— 非 TTY 的 Hermes（gateway / cron / 桌面后端）永远没法回答首次授权弹窗，不写进 allowlist 的 hook 会**静默不生效**。

退出码：`0` 成功（或已装）、`1` 失败、`2` 缺 key。

```bash
python3 hermes/install_hermes.py --check          # 体检
python3 hermes/install_hermes.py --key <KEY>      # 顺手写 key（0600）
python3 hermes/install_hermes.py --hermes-home ~/.hermes --python $(which python3)
```

> 为什么脚本用「追加文本」而不是 `hermes config set`：后者会重新 dump 整个 YAML，**注释全丢**（实测）。

---

## 开关

| 命令 | 作用 |
|---|---|
| `/jev` | 看开关状态、key、链路、熔断、注入量 |
| `/jev-off` | 关闭（脚本还在，只是不调 API） |
| `/jev-on` | 开启并重置熔断 |

三条都是 exec 快捷命令，**不进模型、零 token**。注意只在**交互式**界面（CLI / TUI）生效——`hermes chat -q "/jev"` 会把字面量当消息发给模型。

---

## 配置

`$HERMES_HOME/agent-hooks/jev_config.json`（每轮重读，改完即时生效，不用重启）：

| 字段 | 默认 | 含义 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `min_chars` | `6` | 短于此不调 API（注意是**字符数**） |
| `api_timeout` | `3.5` | 超时即静默放行 |
| `history_turns` / `history_chars` | `20` / `800` | 喂给 Jev 的历史量 |
| `memory_chars` | `1500` | 喂给 Jev 的记忆量（Hermes 下是 `~/.hermes/memories/USER.md` + `MEMORY.md`，预算对半分） |
| `show_verdict` | `true` | 让模型把判定回显给用户 |
| `skip_platforms` | `["cron"]` | **Hermes 特有**：cron 日报任务不该付这 1 秒和几百 token |

---

## 与 WorkBuddy 版的差异

- **cron 隔离**：一个 Hermes 核心同时跑 cron / gateway / 桌面 / CLI，`skip_platforms` 挡掉定时任务。
- **矛盾 cue 收口**：`do NOT read files` 和 `ask one clarifying question` 同时下发会让模型在「猜」和「问」之间乱选。ambiguous 判定下禁止性 cue 全部收回。
- **记忆预算**：原版第一个记忆文件吃满预算就跑，Hermes 下 USER.md 会把 MEMORY.md 挤掉，改成按文件平分。
- **`skill_route` 补 `diagnosis`**：原版遗留的「验证一下 / 确认是否」被判成 `ops` 的问题，这里补上了。
- **历史不用解析文件**：Hermes 直接把活的 `conversation_history` 交给 hook，原版 §3 那个「历史一直 0 轮」的 bug 在 Hermes 版不可能复现。

---

## 成本与安全

- 注入约 300–400 token/轮，实测 API 判定 ~0.9–1.1s。拦掉一次 grep 省 5k–50k token，期望值为正；本来就不读文件的简单请求是净亏约 1%。
- **fail-open 铁律**：无 key / 超时 / HTTP 错 / 返回空 / 未捕获异常 → 一律 `{}` 静默放行。
- **熔断**：连续 3 次失败冷却 600 秒，否则网络不通时每条提问都被超时拖住。
- **key 与配置分离**：key 在 `$HERMES_HOME/.typesafe_key`（0600），不进 `config.yaml`、不进仓库。
- 日志 `$HERMES_HOME/agent-hooks/jev_log.jsonl` 含 prompt 前 120 字，排错用，**别外传**。

---

## 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| hook 已注册但没注入 | `enabled:false` / 短于 `min_chars` / 纯应答词 / `/` 开头 / 平台在 `skip_platforms` | `cat jev_config.json` 逐项对 |
| `hermes hooks list` 显示 `✗ not allowlisted` | 授权缺失，非 TTY 不会弹窗 | 重跑 `install_hermes.py` 写 allowlist |
| `链路：熔断中` | 连续 3 次失败 | 等 10 分钟或修好 key 后 `/jev-on` |
| 判定明显不对 | 历史/记忆没喂进去 | 见 `docs/DESIGN.md`「state 要厚」 |
| `/jev` 无反应 | 用了 `-q` 非交互模式 | 交互式界面里敲 |

```bash
hermes hooks list                        # 注册 + 授权状态
hermes hooks doctor                      # 可执行位、授权、JSON 冒烟、耗时
hermes hooks test pre_llm_call --payload-file p.json   # 真跑一次
```
