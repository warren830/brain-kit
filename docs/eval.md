# Run Trace 和 Eval v1

2026-11-08 · brain-kit 0.1.0

三样东西：

- **run trace**：一个需求从开工到合并后 14 天，发生了什么。一个 JSON。
- **eval**：把所有 trace 合起来算指标，列出要人复看的 run 和值得记的缺口。
- **golden 题和消融**：固定考题，去掉一块 Brain 再考，看哪块知识在"承重"。

都是只读的：只读已有的记录，不改仓库，不联网。同样的输入，结果一样。

## 1. 命令

```bash
brain trace PAY-1                                  # 看一个需求（中文摘要）
brain trace PAY-1 --json --ref origin/main         # 合并以后，从 main 读
brain trace PAY-1 --ledger /srv/bridge/ledger.sqlite --vouchers ~/brain-intake/vouchers --credits credits.json

brain eval                                         # 全部需求的指标
brain eval --since 2026-11-01 --ref origin/main    # 只看这一期
brain eval --json
brain eval gaps                                    # 值得记的缺口
brain eval gaps --file                             # 记成需求（来源 eval-gap）

brain golden plan --tasks tasks --repeats 3 --json > jobs.json
brain golden snapshot tasks/G-1 --out /tmp/g1 --variant no-TECH
brain golden score tasks/G-1 /tmp/g1
brain golden summarize results.json --tasks tasks
```

通用选项（trace 和 eval 都有）：

- `--ref`：从这个 ref 读，不读工作区。
- `--ledger`：桥的 ledger（sqlite），只读打开。
- `--vouchers`：brain-intake 的 `vouchers/` 目录。不写时，用材料包仓库下的 `vouchers/`（`$BRAIN_INTAKE_DIR`、`git config brain.intake`、`~/.brain/intake`），有才用。
- `--allowed-signers`：给了就核对凭证签名，结果写进 trace（ok / bad）。
- `--credits`：KiroCrew app 传来的 JSON（见第 5 节）。
- `--as-of`：按这个时间判断"14 天满没满"，默认是现在。

## 2. trace 里有什么，从哪来

每个值都写了来源（条目里的 `src`，和顶层的 `src`）。没有来源的值是 null，不猜。读不到的来源列在 `missing` 里，所以 null 不会被当成"没发生"。

| 部分 | 内容 | 来源 |
|---|---|---|
| 基本 | key、intent、owner、branch、brain_commit、run_ids | `runs/<KEY>/run.json`；run 号来自 ledger 和凭证 |
| stages | 每个阶段最后的状态（done / skipped / started）、时间、说明 | Lean 审计日志 `aidlc-docs/audit/*.jsonl`；每个文件的哈希链都核对，断了会写出来 |
| actions | 提交（人还是机器人、在交接前还是后）、被 Lean 拦下的调用、会话和回合数、交接、run 停下来问的问题 | git；Lean 日志；run.json；ledger；凭证；`park.json` 的每个版本 |
| decisions | decisions.md 的每一行、需求页里的假设 A-n、桥上的问答 | `decisions.md`、`requirements.md`、ledger、凭证 |
| checks | 每次 verify：时间、过没过、哪条命令失败 | Lean 日志的 VERIFY；`build-and-test-summary.md` |
| humans | 人做的每件事：审批、驳回、没被接受的口令、回答、"允许改测试"、交接后往 ai/* 的提交、平台上手动停止或重启 | Lean 日志；ledger；凭证；git；KiroCrew app |
| brain | 开工时装载了几条、引用了哪些、过时的引用、提案 | `brainkit.usage`（不另算） |
| cost | credits，按回合拆分（有的话） | KiroCrew app 传入 |
| outcome | 交接了没有、验收（是否一次通过）、合并时间、14 天内的回滚 / 热修 / 缺陷 | run.json；Lean 日志；ledger；git |

**人的身份**：只出现 GitLab 用户名和化名。git 和 Lean 记的邮箱、名字、主机名，换成 `G-` 加 6 位（邮箱的哈希）。桥的答复人用凭证里的化名 `P-xxxx`。decisions.md 的 Who 栏写的是人名时，不写出来。人的消息原文不抄，decisions.md 里本来就有的除外。

**同一件事只算一次**：桥送回 Lean 的审批口令，在 Lean 日志里也有一条。两条按凭证号合成一条，来源都列上。decisions.md 里记下桥上回答的那一行，也按凭证号连到桥上那条决策。

## 3. 指标

| 指标 | 怎么算 | 现在量不到的 |
|---|---|---|
| 自主度 | 交接以后，除了必经的审批（REQUIREMENTS、DESIGN、INCEPTION、PLAN、RELEASE、验收），人没做过别的事、而且交付正确的 run ÷ 已完成、交接过、对错已知的 run | 本地半程是人自己带着 AI 做，不算介入（本地的提交分不清是人还是 AI 写的）。没交接过的 run 单列，不算 |
| 交付正确率 | 一次通过验收、合并后 14 天内没有回滚和热修的交付 ÷ 14 天已满、有验收记录的交付 | 没接 Jira 时，缺陷单只能看新的 `claim/<KEY>`：标题点名这个需求、又带"修复 / bug / 缺陷"这类词的才算。热修只认点名需求号的提交。14 天没满的列为"还不知道" |
| 介入落在判断点的比例 | 落在判断点上的人工介入 ÷ 全部人工介入。判断点 = Lean 的审批口令（批准、驳回、没被接受的口令）和桥上的 park 问题。别的都算判断点之外：交接后人往 ai/* 的提交、"允许改测试"、KiroCrew 上手动停止或重启 | 方案里"复盘也认定确实需要人判断"这一半要人看。git 不记推送，用"交接后人的提交"代替。手动停止和重启要 KiroCrew app 传进来才有 |
| 重复纠正率 | correction 条目的 repeats 合计 ÷（correction 条目数 + repeats 合计），目标趋近 0 | 和 `brain usage` 一样 |
| 知识有用率 | 被引用过的条目 ÷ 现有条目；另给每个 run 引用数之和 ÷ 装载数之和 | 只认 Lean 记录里写出的条目号 |
| 过时与冲突 | 过时引用（开工时已归档的条目还被引用）+ lint 的"同一个术语定义了两次" + 本期被覆盖或撤回的条目 | 桥上"两个人回答不一样"只发了 MR 评论，没进 ledger，数不到 |
| credits | 完成的 run 花的 credits 合计 ÷ 正确交付数。只用来诊断，不设上限 | **token 数量不到**：经 Kiro 跑模型，KiroCrew 只给 credits，input、output、cache 的 token 数都是 0。credits 也读不到，要 KiroCrew app 传进来 |

**决策漏斗**：期内的每一条决策，按"谁定的"归一类，每条只算一次，也按周（ISO 周）列：

1. **人**：桥上回答的问题；decisions.md 里 Who 是人或角色（product、owner、用户名、`P-xxxx`）。
2. **确定性检查**：Who 是 verify、CI、lint、gate、test 这类。
3. **Brain**：这一行引用了条目号。
4. **agent 自己定**：Who 是 agent，或者是需求页里的假设 A-n。
5. **看不出**：Who 空着。

还没回答的问题、考题的自动回答（eval_oracle）都不进漏斗，单独报个数。

**要人复看的 run**（每条写明原因）：

- 验收被驳回过；
- 合并后 14 天内有回滚、热修或缺陷；
- verify 失败 3 次以上（CI 的失败读不到，只算 verify）；
- 同样的纠正又来了：提案里有 `rel: repeats`，或者一个 run 被驳回 2 次以上；
- 引用了已归档的条目；
- 判断点之外有人介入；
- credits 超过中位数的 2 倍（至少 3 个 run 有 credits 时才判断）。

**缺口**（`brain eval gaps`）：

- 被驳回过、但 decisions.md 里没有引用它的 correction 或 repeats 提案（知识缺口）；
- 条目重复了 2 次以上：改成 gate（测试缺口）；
- 装载过 3 次（`--min-runs`）以上、从没被引用的条目：撤回候选；
- 引用了已归档的条目；
- 验收被驳回或合并后出了问题的需求：做成新的 golden 题。

`--file` 用 `brainkit.intents.add` 记成需求，来源是 `eval-gap`，`found_in` 写发现它的需求号。同一个缺口只记一次。没有 intents 模块时，只打出来。

## 4. golden 题和消融

**题目**：`tasks/<id>/task.json`

```json
{"id": "G-1", "repo": "说明用", "base_sha": "<修复前的 commit>",
 "intent": "原始意图", "acceptance": ["AC-1 ..."],
 "hidden_tests": ["hidden.patch", "hidden/tests/test_refund.py"],
 "test_command": ["python3", "-m", "unittest"],
 "must_use": ["T-PAY-1-2"],
 "oracle_answers": {"RELEASE": "approve", "re:退款.*上限": "B"}}
```

- 隐藏测试放在快照外面：`.patch` / `.diff` 用 `git apply` 打进去；其他文件放在题目的 `hidden/` 下，拷到同样的路径（去掉 `hidden/`）。
- 出题：取近 3–6 个月已合并、带测试的 bug 和小功能，base 用修复前的 commit，当时的测试作隐藏测试；再加几道"必须用到某条 Brain 知识"的题（写进 `must_use`）。
- **这个项目还没有题**。机制已在自造的仓库上测过。

**快照**（`snapshot`）：

- 用 `git archive` 导出 base_sha，解到一个新的空目录：没有 `.git`，没有远端；
- 删掉 `.git-credentials`、`.netrc`、`.pypirc`、`.env*`，以及带登录令牌的 `.npmrc`；
- 再扫一遍私钥、云访问密钥、令牌。还有，就整个拒绝，快照不留；
- 消融变体：`full`、`no-PRODUCT`、`no-TECH`、`no-IMPROVEMENT`，从快照里删掉对应的 Brain 文件和它的 archive。PROJECT 没有消融变体；
- 快照的清单只返回给调用方，不写进快照，AI 看不到自己考的是哪个变体。

**打分**（`score`）：打进隐藏测试，跑题目的测试命令（去掉环境里的令牌、密钥、GitLab / AWS / 桥的变量），数通过和失败（认 unittest 和 pytest 的输出；认不出时只看退出码）。

**跑**：`plan` 生成 题数 × 变体 × 次数 个任务。`run(jobs, runner, tasks)` 里的 `runner(job)` 由 KiroCrew app 提供（真的派单）；测试里用假的。runner 出错的任务记为错误，不当 0 分。

**怎么判"掉分"**（`summarize`）：

- 两边都至少有 2 次有分的运行；
- 去掉这块知识的每一次，都比全量的每一次分低（完全不重叠）；
- 平均掉分不小于 `--min-drop`（默认 0.05）。
- 为什么这么判：3 次对 3 次时，没有任何影响也完全分开的概率是 1/C(6,3) = 5%（精确的单侧 Mann-Whitney，U = 0）。每次比较都写出这个概率。
- 不到 2 次的，写"次数不够"，不下结论。
- `must_use` 里的条目：去掉它所在的文件后，列出它的每道题都判过、都没掉分，就列为撤回候选。

**自动回答**：考题 run 停下来问问题时，`oracle_answer(task, park)` 按 `oracle_answers` 回答。先按审批类型（RELEASE、DESIGN）或问题类型（acceptance），再按 `re:` 正则，再按问题里的文字匹配。凡是记下来的地方都标 `eval_oracle: true`，真实 MR 不认。trace 和 eval 把这类回答单独放，不算人的介入，也不进漏斗。

**规模**（方案 §F）：10 题 × 4 组 × 3 次 = 120 个 run，每月一次，单独记预算。

## 5. KiroCrew 传进来的 JSON（`--credits`）

```json
{"runs": {"RUN-PAY-1-1": {"credits": 12.5,
                           "turns": [{"turn": 1, "credits": 3.0, "agent": "brain-run", "at": "2026-11-08T04:00:00Z"}],
                           "manual": [{"at": "2026-11-08T05:00:00Z", "kind": "restart"}],
                           "context_used": 81000, "context_window": 200000}}}
```

- 键可以是 run 号，也可以是需求号；只有一个 run 时，直接给一条记录也行。
- `credits` 不写时，用各回合加起来。
- `manual`：平台上的手动停止、重启，算判断点之外的介入。
- 上下文大小只是诊断，不是计费的 token。

## 6. 还量不到的、还没验证的

- **token**：见上。只有 credits，而且要 KiroCrew 传进来。
- **CI 流水线**：GitLab 的流水线结果这里读不到；"修 CI 的轮数"只能用 verify 的失败次数代替。
- **推送**：git 不记推送；用交接后人的提交代替（一次推送可能有几个提交）。
- **本地半程**：本地的提交用的是人的 git 身份，分不清是人写的还是 AI 写的，所以不算介入。
- **复盘认定**：介入是不是"确实需要人判断"，要人看。
- **缺陷单**：不接 Jira 时只能看 claim 标题；接 Jira 以后要另外读。
- **squash 合并**：分支删了、又是 squash 合并时，合并时间取 main 上第一次带进 run.json 的那个提交；这个需求自己的提交就列不出来了。
- **真实数据**：只在自造的仓库上测过。真 run、真 ledger、真凭证都还没跑过一遍。
