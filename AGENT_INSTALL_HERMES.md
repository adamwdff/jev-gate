# AGENT_INSTALL_HERMES — 给 AI Agent 的安装说明（Hermes Agent 版）

> 你是 AI Agent。你的用户把这个仓库链接给了你，希望你在**他本机的 Hermes Agent** 里装上 Jev 前置判读层。
> 严格按下面的步骤顺序执行。**任何一步不满足条件就停下来向用户提问，不要自行编造、不要跳过、不要"先装了再说"。**
>
> 装 WorkBuddy 版看 `AGENT_INSTALL.md`；这一份只讲 Hermes。

---

## 第 0 步：确认环境

1. 用户机器上有 Hermes Agent：`command -v hermes && hermes --version`。
2. 有 Python 3：`python3 --version`（脚本只用标准库，任何 3.8+ 都行；**不要假设有 PyYAML**）。
3. 能访问 `https://api.typesafe.ai`。
4. Hermes 主目录：`echo "${HERMES_HOME:-$HOME/.hermes}"`，确认里面至少有 `config.yaml`。

任一不满足 → 告诉用户缺什么，停止安装。

---

## 第 1 步：拿 API key（**最高优先级，先做这个**）

没有 key 就装完也不会生效（hook 会 fail-open 静默放行，用户会觉得"装了没反应"）。

**检查顺序**（前两个命中就不用问）：

```bash
echo "$TYPESAFE_API_KEY"                                        # 1) 环境变量
cat "${HERMES_HOME:-$HOME/.hermes}/.typesafe_key" 2>/dev/null | wc -c   # 2) Hermes 的 key 文件
cat ~/.workbuddy/.typesafe_key 2>/dev/null | wc -c              # 3) 已有 WorkBuddy 版，可直接复用
```

**三个都没有 → 必须停下来向用户索要。** 用下面这段原话问（不要改写成"要不要装"，要直接问 key）：

> 装 Jev 前置判读需要你的 TypeSafe API key。key 只写进你本机的 `$HERMES_HOME/.typesafe_key`（权限 0600），不会进 `config.yaml`、不会进 git 仓库，也不会被我打印出来。
> 申请入口：https://typesafe.ai （控制台创建 API key）
> 拿到后直接把 key 发我，或者你自己执行：
> `printf '%s' '<你的KEY>' > ~/.hermes/.typesafe_key && chmod 600 ~/.hermes/.typesafe_key`
> 然后让我继续。

**硬性禁止**（违反会让用户暴露密钥或白装一遍）：

- ❌ 不要自己编一个 key、不要用占位符（如 `YOUR_KEY_HERE`、`sk-xxx`）糊过去。
- ❌ 不要跳过 key 直接跑安装脚本然后报告"安装成功"。
- ❌ 不要把 key 写进仓库里的任何文件、不要 `git add` 它、不要在对话里回显完整 key。
- ❌ **不要把 key 写进 `config.yaml`**（那是会被同步/备份/贴给别人看的文件）。hook 只从 `.typesafe_key` 或环境变量读。

---

## 第 2 步：跑安装脚本

```bash
git clone https://github.com/adamwdff/jev-gate.git
cd jev-gate
python3 hermes/install_hermes.py                 # 缺 key 时 exit 2 并打印索取提示 —— 回第 1 步
# 或：python3 hermes/install_hermes.py --key '<key>'
```

脚本做四件事：复制 `hermes/jev_gate.py` + `jev_config.json` 到 `$HERMES_HOME/agent-hooks/`；把 key 落到 `$HERMES_HOME/.typesafe_key`（0600，仅 `--key` 时）；**追加**（不重写）`config.yaml` 的 `hooks.pre_llm_call` + `/jev`、`/jev-on`、`/jev-off` 三条快捷命令（先备份、幂等）；把 `{event, command}` 写进 `$HERMES_HOME/shell-hooks-allowlist.json`。

**两种必须告诉用户的情况**：

- 脚本输出 `config.yaml ... 已包含 hooks / quick_commands，未改动` → 他原来就配过 hook，脚本不敢动。把 `hermes/README.md` 里的片段给他，让他手工合并（或他自己决定把 hook 放进已有的 `hooks:` 块）。
- 脚本输出 `追加后校验失败…已回滚` → 他的 `config.yaml` 本来就有 YAML 问题，让他先修配置；这种情况**不要**改用 `hermes config set` 绕过，那会重写整个文件、丢掉注释。

验证安装状态：

```bash
python3 hermes/install_hermes.py --check
hermes hooks list && hermes hooks doctor
```

`hermes hooks doctor` 要看到三行 ✓：脚本可执行、已授权、冒烟测试产出合法 JSON。

---

## 第 3 步：必须新开会话

**hooks 在会话启动时快照，装完不新开会话就不生效。** 这是最常见的"装了没反应"原因。

- 交互式 CLI / TUI：退出重进，或在对话里 `/new`（新会话）。
- 桌面 App：开一个新对话。
- gateway（Telegram / WeCom / 飞书 …）：`hermes gateway restart`，否则新 hook 不会被注册。

---

## 第 4 步：验证

让用户在**交互式**界面（不是 `hermes chat -q`）输入：

```
/jev
```

- 返回 `Jev 前置判读：开启 | key：已配置 | 链路：正常 | ...` → 装好了。三条 `/jev*` 是 exec 快捷命令，**不进模型、不花模型 token**。
- 返回 `key：缺失` → 回到第 1 步。
- 返回 `链路：熔断中` → 连续 3 次失败，等 10 分钟或修好 key 后 `/jev-on` 重置。
- 什么都没返回 → hook 没注册上或没授权，`hermes hooks list` 看是不是 `✗ not allowlisted`。

再验一次真链路（会花一次 API 调用）：

```bash
hermes hooks test pre_llm_call
```

---

## 可选：向用户解释这是什么（一句话版本）

> 每次你提问时，在模型读你的提示词之前，先用 Jev（TypeSafe System One 判定模型）花 ~1 秒做 9 个维度的结构化判定（任务类型 / 要不要读文件 / 是否模糊 / 风险 / 范围 / 产出深度 …），把结论注入当路由提示。目的是**省主模型 token**：该不读文件的就别 grep，该一句话答完的就别写长篇，该直接做的就别反问确认。全程 fail-open——Jev 挂了就静默放行，绝不拦你的提问。cron 定时任务默认不走这一层。

---

## 排错速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 装完没反应 | 没新开会话 / gateway 没重启 | 第 3 步 |
| `hermes hooks list` 显示 `✗ not allowlisted` | 缺授权（非 TTY 不弹窗，只能写 allowlist） | 重跑 `install_hermes.py` |
| `/jev` 无输出 | 用了 `-q`（非交互） | 交互式界面里敲 |
| 一直"熔断中" | key 错 / 网络不通 | 换 key 后 `/jev-on`（重置熔断） |
| 每条提问都卡 ~3.5s | 网络不通且未熔断 | 等熔断生效或 `/jev-off` |
| 日报/定时任务变慢 | 平台没排除 | `jev_config.json` 的 `skip_platforms` 加 `"cron"` |
| 判定明显不对 | 历史/记忆没喂进去 | 见 `docs/DESIGN.md`「state 做厚」一节 |

日志在 `$HERMES_HOME/agent-hooks/jev_log.jsonl`（含 prompt 前 120 字），排错用，**不要外传**。
