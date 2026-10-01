# AGENT_INSTALL — 给 AI Agent 的安装说明

> 你是 AI Agent。你的用户把这个仓库链接给了你，希望你在**他本机的 WorkBuddy** 里装上 Jev 前置判读层。
> 严格按下面的步骤顺序执行。**任何一步不满足条件就停下来向用户提问，不要自行编造、不要跳过、不要"先装了再说"。**

---

## 第 0 步：确认环境

在动手前先确认：

1. 用户机器上有 WorkBuddy，配置目录在 `~/.workbuddy/`（`settings.json` 存在）。
2. 有 Python 3（`python3 --version`）。
3. 有网络，能访问 `https://api.typesafe.ai`。

任一不满足 → 告诉用户缺什么，停止安装。

---

## 第 1 步：拿 API key（**最高优先级，先做这个**）

Jev 判读依赖 **TypeSafe API key**。没有 key 就装完也不会生效（hook 会 fail-open 静默放行，用户会发现"装了没反应"）。

**检查顺序**：

```bash
echo "$TYPESAFE_API_KEY"                 # 1) 环境变量
cat ~/.workbuddy/.typesafe_key 2>/dev/null | wc -c   # 2) key 文件（0 或不存在 = 没有）
```

**如果两个都没有 → 必须停下来向用户索要。** 用下面这段原话问（不要改写成"要不要装"，要直接问 key）：

> 装 Jev 前置判读需要你的 TypeSafe API key。key 只写进你本机的 `~/.workbuddy/.typesafe_key`（权限 0600），不会进 git 仓库，也不会被我打印出来。
> 申请入口：https://typesafe.ai （控制台创建 API key）
> 拿到后直接把 key 发我，或者你自己执行：
> `mkdir -p ~/.workbuddy && printf '%s' '<你的KEY>' > ~/.workbuddy/.typesafe_key && chmod 600 ~/.workbuddy/.typesafe_key`
> 然后让我继续。

**硬性禁止**（违反会让用户暴露密钥或白装一遍）：

- ❌ 不要自己编一个 key、不要用占位符（如 `YOUR_KEY_HERE`、`sk-xxx`）糊过去。
- ❌ 不要跳过 key 直接跑安装脚本然后报告"安装成功"。
- ❌ 不要把 key 写进仓库里的任何文件、不要 `git add` 它、不要在对话里回显完整 key。
- ❌ 不要把 key 写进 `settings.json`（hook 从 `.typesafe_key` 文件或环境变量读，两者分离）。

**拿到 key 后两种落地方式，选一**：

```bash
export TYPESAFE_API_KEY='<key>'      # 方式 A：临时环境变量（重启终端失效，需写进 shell profile 才持久）
printf '%s' '<key>' > ~/.workbuddy/.typesafe_key && chmod 600 ~/.workbuddy/.typesafe_key   # 方式 B：推荐，持久
```

---

## 第 2 步：跑安装脚本

```bash
git clone https://github.com/adamwdff/jev-gate.git
cd jev-gate
python3 install.py            # 缺 key 时会 exit 2 并打印索取提示 —— 那就回到第 1 步
# 或：python3 install.py --key '<key>'
```

脚本会做三件事：复制 `hooks/jev_gate.py`、`hooks/jev_ask.py`、`hooks/jev_config.json` 到 `~/.workbuddy/hooks/`；把 key 落到 `~/.workbuddy/.typesafe_key`（0600）；把 `UserPromptSubmit` hook 合并进 `~/.workbuddy/settings.json`（**先备份**，且不会重复注册）。

**如果 `settings.json` 解析失败**：脚本会备份为 `settings.json.bak-broken-<时间戳>` 并新建。**此时必须告诉用户**，让他确认原有配置没丢。

验证安装状态：

```bash
python3 install.py --check
```

---

## 第 3 步：必须重启 WorkBuddy

**hooks 在会话启动时快照，改完 `settings.json` 不重启就不生效。** 这是最常见的"装了没反应"原因。

告诉用户：完全退出 WorkBuddy（不是关窗口，是退出应用）再重新打开。

---

## 第 4 步：验证

让用户在新的对话里输入：

```
/jev status
```

- 返回 `Jev 前置判读：开启 | 链路：正常 | ...` → 装好了。这个命令由 hook 自消费，走 exit 2 回显，**不进模型、不花模型 token**。
- 返回 `链路：熔断中` → 连续 3 次调用失败，等 10 分钟或拿到正确 key 后 `/jev on` 重置。
- 什么都没返回 → hook 没注册上，回到第 2、3 步检查。

---

## 可选：向用户解释这是什么（一句话版本）

> 每次你提问时，在模型读你的提示词之前，先用 Jev（TypeSafe System One 判定模型）花 ~1 秒对这次请求做 9 个维度的结构化判定（任务类型 / 要不要读文件 / 是否模糊 / 风险 / 范围 / 产出深度 …），把结论注入给模型当路由提示。目的是**省主模型的 token**：该不读文件的就别 grep，该一句话答完的就别写长篇，该直接做的就别反问确认。全程 fail-open——Jev 挂了就静默放行，绝不拦你的提问。

---

## 排错速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 装完没反应 | 没重启 WorkBuddy | 完全退出重开 |
| `/jev status` 无输出 | hook 没注册上 | `python3 install.py --check`，看 `settings.json` 里 `hooks.UserPromptSubmit` |
| 一直"熔断中" | key 错 / 网络不通 | 换 key 后 `/jev on`（会重置熔断） |
| 每条提问都卡 ~3.5s | 网络不通且未熔断 | 等熔断生效（3 次失败后冷却 10 分钟）或 `/jev off` |
| 判定明显不对 | 历史/记忆没喂进去 | 见 `docs/DESIGN.md`「state 做厚」一节 |

日志在 `~/.workbuddy/hooks/jev_log.jsonl`（含 prompt 前 120 字），排错用，**不要外传**。
