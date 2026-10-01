# 设计取舍与踩过的坑

按「结论 → 为什么」记，避免后来人重踩。

## 1. skill 做不到前置，只有 hook 能

想让 Jev 抢在 WorkBuddy 原生上下文分析之前介入，起初考虑做成 skill。**做不到**：skill 是模型侧的被选项，模型必须先读完整条消息才决定加载谁，那时上下文已经组装完了。

真正的入口是 `UserPromptSubmit` hook——它在提示词提交后、模型处理前运行。payload 只有 `prompt` / `session_id` / `transcript_path` / `cwd`。

代价：hook 跑在框架拼装上下文**之前**，那一刻系统提示、记忆、文件树、工具列表都还没生成，hook 拿不到。所以 state 得自己凑。

## 2. fail-open 是铁律

任何失败路径（无 key / 超时 / HTTP 错 / 返回空 / 未捕获异常）一律 `exit 0` 无输出。宁可这一轮没有判读，也不能让用户问不出去。

配套的**熔断**：连续 3 次失败后冷却 600 秒。实测：失败 3 次后第 4 次耗时从 ~1s 降到 0.07s。没熔断的话，网络不通时每条提问都被 `api_timeout` 拖 3.5 秒。

## 3. 历史曾经一直是 0 轮（真根因）

`tail_lines()` 最初按嵌套格式解析 transcript：

```json
{"message": {"role": "user", "content": [...]}}
```

**实际是扁平的**：

```json
{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "..."}]}
```

`role` 在顶层，没有 `message` 包装层。所以历史一直静默返回 0 条——这个 bug 藏了很久，表现是"Jev 判定老是莫名其妙"。

## 4. user 轮次里绝大部分是噪声

单轮实测 4798 字，其中 4585 字是 `<system-reminder>`（user_info / identity / hook 输出）。必须先正则剥掉 `<system-reminder ...>...</system-reminder>`，再从 `<user_query>` 取正文，否则 state 全被垃圾占满。

另外：**不能用"最后 N 行"窗口取历史**。历史轮次与 `function_call` / `tool_result` 行交错且长度差异极大（单轮最长 4798 字），行窗口会漏掉大量 user 轮。正确做法是扫描尾部全部行再按 type/role 过滤。

## 5. state 要"做厚"，不是只喂一句

起初以为误报来自"短句无宾语"，改措辞无效。对照实验（同一句 `这个skill没有用，请删除`）：

| state | ambiguity | 判定 |
|---|---|---|
| 只有这一句 | 1.98 | ambiguous ❌ |
| 带 8 轮历史 | 1.26 | loose，不再触发追问 ✓ |

**根因是 state 里没有指代对象**——"这个 skill""是否在走"脱离历史看就是模糊的。修措辞不如喂上下文。

最终方案：历史 6轮×220字 → 20轮×800字；补读项目当日日志 + 项目 MEMORY.md + 用户级 MEMORY.md（截断 1500）；`STATE_BUDGET=8000`，优先级 新消息 > 项目记忆 > 历史，历史从最近往回填直到预算耗尽。

修完的对照（越低越明确）：

| 输入 | 无历史无记忆 | 20轮历史+记忆 | 正确 |
|---|---|---|---|
| 这个skill没有用，请删除 | 1.99 ambiguous ❌ | **0.06 clear** ✓ | clear |
| 已经重启了，请验证是否在走 | 1.96 ambiguous ❌ | **0.25 clear** ✓ | clear |
| 帮我看看这个报错 | 1.99 ambiguous ✓ | 1.68 ambiguous ✓ | ambiguous（确实没说哪个） |

从"无脑 1.99"变成有辨别力。**延迟持平**：state 从 0 涨到 6270 字符，耗时仍 ~1s——TypeSafe 并行判定对 state 大小不敏感。

边界：Jev 是判定模型不是推理模型，state 不是越大越好。**不喂文件树**（对语义判定是噪声）。

## 6. 禁止性 cue 才省钱

最初的 cue 全是"要做什么"（读文件、确认范围…），一分钱没省——模型本来就会做。真正的省法是**禁止**：

- `do NOT read files or grep — nothing on disk is needed`
- `do NOT scan the project structure`
- `answer in 1-2 sentences, produce no artifact`
- `do NOT ask to confirm — act now`

但禁令下错比不下更糟（模型只能猜）。两道保险：

1. `task_type` 置信度 < 0.5 时**一条禁令都不下**——连任务类型都判不准，下游全不可信。
2. `needs_files` 之外加**反向维度 `answers_on_disk`**，两个都判 no 才禁。因为"这个项目里有哪些 skill"没说要读文件，但只能靠列目录回答——正向问"要不要读文件"会漏掉这类。

## 7. 开关必须在 enabled 判断之前

`/jev on|off|status` 由 hook 自消费，走 `exit 2`（清除提示词、只回显 stdout），所以命令**不进模型、零 token**，也不被 Jev 判定。

顺序是硬约束：`handle_command()` 必须排在 `enabled` 判断**之前**，否则关闭后就再也开不回来了。

## 8. 成本账

注入从 ~250 涨到 498 字符（约 300–400 token）。拦掉一次 grep 省 5000–50000 token，期望值为正；但对本来就不会读文件的简单请求是净亏约 300 token（占整轮上下文约 1%）。

框架自动注入的系统提示 / 记忆 / 文件树 / 历史**省不掉**——hook 无权裁剪。这是这一层能力的天花板。

## 9. 已知遗留

- `skill_route` 选项集缺 `diagnosis`，把"验证一下""确认是否"类判成 `ops`（已 2 次）。加选项即可。
- `confidence` 只反映分布尖锐度，**不反映对错**。曾出现 0.88 判 ambiguous、0.96 判 ops 的高置信误报。阈值别卡太死。
- 改动 `jev_gate.py` 或 `settings.json` 后**必须重启 WorkBuddy**（hooks 在会话启动时快照）。
