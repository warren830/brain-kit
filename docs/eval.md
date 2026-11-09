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

**人的身份**：只出现 GitLab 用户名和化名。git 和 Lean 记的邮箱、名字、主机名，换成 `G-` 加 6 位（邮箱的哈希）。Lean 的审计文件名里有主机名（`<主机>-<6 位>.jsonl`），trace 里写成 `aidlc-docs/audit/L-<6 位>.jsonl`（文件路径的哈希，每台机器上一样）。被拦下的命令、阶段说明、失败的检查命令里出现的邮箱换成 `G-` 化名，`/Users/<名字>`、`/home/<名字>` 换成 `~`。桥的答复人用凭证里的化名 `P-xxxx`。decisions.md 的 Who 栏不是规定的写法时，不写出来（可能是人名）。人的消息原文不抄，decisions.md 里本来就有的除外。

**读哪一份分支**：main 上有这个分支的合并提交时，以合并提交的第二个父提交为准（本地那份旧了、或合并后又有提交，都写进 `src.git.tip_note`）。没合并时，本地和 origin 两份里取新的那份（包含另一份的那份）；两份分叉了，用 origin 的，并在 trace 里写明。所以负责人自己机器上那份停在交接时的旧分支，不会把交接后的事丢掉。

**怎么认合并**（不问 GitLab）：

1. 分支的最后一个提交在 main 上：合并提交，或者 fast-forward（合并时间取 main 第一个包含它的提交）。
2. 分支的提交不在 main 上，但 `runs/<KEY>/run.json` 到了 main：从 main 上第一个带进 run.json 的提交 S 起，顺着 main 往后，属于这个 run 的提交（改了 `runs/<KEY>/` 或 `aidlc-docs/<intent>/`、机器人的提交、和分支上某个提交 patch-id 一样的提交）一直到下一个合并提交或下一个 run 的提交（改了别的 `runs/<KEY2>/`）为止；其中最后一个就是合并点，中间夹着的别人的提交也算这个 run 的。只有 S 一个：squash 合并（分支还在时，再比一下 S 是否正好是分支的全部改动）；有和分支 patch-id 一样的：rebase 合并，trace 读 main 上的这些副本；分支已经删了、又不止一个：fast-forward 或 rebase 合并，合并时间取最后一个 run 提交的时间（git 不记 fast-forward 是什么时候发生的）。
3. ledger 里 run 的状态是 merged、git 里却找不到合并：合并时间为 null，`merged_at.note` 写明。

"合并后"的提交：在 main 上、又不在合并点历史里的提交（不用 `git log --since`，一个时间早的提交会把后面的都挡住），再按提交时间看是否在 14 天内。这个 run 自己的提交（分支上的、main 上的副本）不算热修。

**同一件事只算一次**：桥送回 Lean 的审批口令，在 Lean 日志里也有一条。两条按凭证号合成一条，来源都列上。decisions.md 里记下桥上回答的那一行，也按凭证号连到桥上那条决策。

## 3. 指标

| 指标 | 怎么算 | 现在量不到的 |
|---|---|---|
| 自主度 | 交接以后，除了必经的审批（REQUIREMENTS、DESIGN、INCEPTION、PLAN、RELEASE、验收），人没做过别的事、而且交付正确的 run ÷ 已完成、交接过、对错已知的 run | 本地半程是人自己带着 AI 做，不算介入（本地的提交分不清是人还是 AI 写的）。没交接过的 run 单列，不算 |
| 交付正确率 | 一次通过验收、合并后 14 天内没有回滚和热修的交付 ÷ 14 天已满、有验收记录的交付 | 没接 Jira 时，缺陷单只能看新的 `claim/<KEY>`：报告问题的（来源是 product、alert，或没有来源的老认领），标题点名这个需求、又带"修复 / bug / 缺陷"这类词的才算；gap、eval-gap、debt、schedule 这些"缺了什么"的需求不算缺陷。热修只认点名需求号的提交。14 天没满的列为"还不知道"；已经知道出了问题、14 天还没满的，也等满了再算（单列 `wrong_open`），这样一期里的好坏按同样的时间算 |
| 介入落在判断点的比例 | 落在判断点上的人工介入 ÷ 全部人工介入。判断点 = Lean 的审批口令（批准、驳回、没被接受的口令）和桥上的 park 问题。别的都算判断点之外：交接后人往 ai/* 的提交、"允许改测试"、KiroCrew 上手动停止或重启 | 方案里"复盘也认定确实需要人判断"这一半要人看。git 不记推送，用"交接后人的提交"代替。手动停止和重启要 KiroCrew app 传进来才有 |
| 重复纠正率 | correction 条目的 repeats 合计 ÷（correction 条目数 + repeats 合计），目标趋近 0 | 和 `brain usage` 一样 |
| 知识有用率 | 被引用过的条目 ÷ 现有条目；另给每个 run 引用数之和 ÷ 装载数之和 | 只认 Lean 记录里写出的条目号 |
| 过时与冲突 | 过时引用（开工时已归档的条目还被引用）+ lint 的"同一个术语定义了两次" + 本期被覆盖或撤回的条目 | 桥上"两个人回答不一样"只发了 MR 评论，没进 ledger，数不到 |
| credits | 完成的 run 花的 credits 合计 ÷ 正确交付数。只用来诊断，不设上限 | **token 数量不到**：经 Kiro 跑模型，KiroCrew 只给 credits，input、output、cache 的 token 数都是 0。credits 也读不到，要 KiroCrew app 传进来 |

**决策漏斗**：期内的每一条决策，按"谁定的"归一类，每条只算一次，也按周（ISO 周）列：

1. **人**：桥上回答的问题；decisions.md 里 Who 是"化名 凭证号"（`P-0305 V-a1e0771735`）。
2. **确定性检查**：Who 是 `check`。
3. **Brain**：Who 是 `brain <条目号>`，或者这一行引用了条目号。
4. **agent 自己定**：Who 是 `agent`（或 kit.json 里的机器人账号），或者是需求页里的假设 A-n。
5. **看不出**：Who 空着。
6. **没归类**：Who 写了别的（product、人名、"TBD"……）。不猜，单独算，并列出是哪个需求的哪一行，好让人改；Who 的原文不写出来。

Who 的写法是运行机提示词（`remote/prompts/brain-run.md` 第 5 节）定的：`agent`、`brain <条目号>`、`check`、`<化名> <凭证号>`，后面可以加一个括号写备注（`agent (A-2)`）。

还没回答的问题（按提问的时间算在哪一期）、考题的自动回答（eval_oracle，按回答的时间）都不进漏斗，单独报个数。

**要人复看的 run**（每条写明原因）：

- 验收被驳回过；
- 合并后 14 天内有回滚、热修或缺陷；
- verify 失败 3 次以上（CI 的失败读不到，只算 verify）；
- 同样的纠正又来了：提案里有 `rel: repeats`，或者一个 run 被驳回 2 次以上；
- 引用了已归档的条目；
- 判断点之外有人介入；
- credits 超过中位数的 2 倍（至少 3 个 run 有 credits 时才判断）。

**缺口**（`brain eval gaps`）：

- 被驳回过、但 decisions.md 里没有引用它的 correction 或 repeats 提案（知识缺口）。验收时成批审、驳回了其中几项（`rejected_items`）的，每一项都要有一条 src 写着这一项的提案（和 `brain reflect` 一样）；本地 Lean 里的驳回没有凭证可引，提案的 src 写记下这次驳回的那一行（同一阶段、当天或之后的 decisions.md 行）就算；
- 条目重复了 2 次以上：改成 gate（测试缺口）；
- 装载过 3 次（`--min-runs`）以上、从没被引用的条目：撤回候选；
- 引用了已归档的条目；
- 验收被驳回或合并后出了问题的需求：做成新的 golden 题。

`--file` 用 `brainkit.intents.add` 记成需求，来源是 `eval-gap`，`found_in` 写发现它的需求号。同一个缺口只记一次：缺口的身份是"种类 + 说的是什么"（编号是这两样的哈希，标题也只由这两样组成），次数这些会变的写在正文里；已经有同一标题的 eval-gap 需求时，不再记。没有 intents 模块时，只打出来。

## 4. golden 题和消融

**题目**：`tasks/<id>/task.json`

```json
{"id": "G-1", "repo": "说明用", "base_sha": "<修复前的 commit>",
 "intent": "原始意图", "acceptance": ["AC-1 ..."],
 "hidden_tests": ["hidden.patch", "hidden/tests/test_refund.py"],
 "test_command": ["python3", "-m", "unittest", "discover", "-s", "tests"],
 "must_use": ["T-PAY-1-2"],
 "oracle_answers": {"RELEASE": "approve", "re:退款.*上限": "B"}}
```

- 隐藏测试放在快照外面：`.patch` / `.diff` 用 `git apply` 打进去；其他文件放在题目的 `hidden/` 下，拷到同样的路径（去掉 `hidden/`）。
- 出题：取近 3–6 个月已合并、带测试的 bug 和小功能，base 用修复前的 commit，当时的测试作隐藏测试；再加几道"必须用到某条 Brain 知识"的题（写进 `must_use`）。
- **这个项目还没有题**。机制已在自造的仓库上测过。

**快照**（`snapshot`）：

- 用 `git archive` 导出 base_sha，解到一个新的空目录：没有 `.git`，没有远端。这个目录不能在任何 git 仓库里面（包括项目自己，比如在项目里 `--out g1`），否则 git 从快照里就能找到那个仓库的历史和远端：直接拒绝；
- 删掉这些文件（清单的 `removed` 里列出）：
  - git 元数据：`.git`、`.gitmodules`（子模块的远端地址，可能带密码）。子模块的内容本来就不在快照里（`git archive` 不带），清单的 `submodules` 列出这些空目录；
  - 凭证文件：`.git-credentials`、`.netrc`、`_netrc`、`.pypirc`、`.pgpass`、`.my.cnf`、`.htpasswd`、`.dockercfg`、`.vault-token`、`.s3cfg`、`.boto`、`.terraformrc`、`terraform.rc`，任何目录下的 `.aws/credentials`、`.docker/config.json`、`.kube/config`，带登录令牌的 `.npmrc` 和 `.yarnrc.yml`；
  - env 文件：`.env`、`.env.*`、`.env-*`、`.env_*`、`.envrc`（`.env.example` 也删）；
  - 密钥库（二进制，扫不进去）：`*.p12`、`*.pfx`、`*.jks`、`*.keystore`；
- 符号链接按文件系统的方式解析（经过别的链接也算），指到快照外面的就整个拒绝；
- 再扫一遍私钥、云访问密钥、令牌，和配置、脚本里单独一行的密码赋值（大写的名字以 PASSWORD、SECRET、TOKEN、API_KEY 等结尾，值是写死的：`DB_PASSWORD=…`、`export X_TOKEN=…`、`SECRET_KEY: …`、`API_TOKEN = '…'`；`${VAR}`、`$VAR`、`None` 不算）。还有，就整个拒绝，快照不留。密码赋值这一条不扫代码文件（`.py`、`.js`、`.ts`、`.go`、`.java` 等）和文档（`.md`、`.rst`、`.txt`），也不扫小写的名字和 URL 里的密码（`postgres://u:p@db`）：测试里的假密码和 README 里的示例太常见，扫了大多数仓库都做不成快照；
- 消融变体：`full`、`no-PRODUCT`、`no-TECH`、`no-IMPROVEMENT`，从快照里删掉对应的 Brain 文件和它的 archive。PROJECT 没有消融变体；
- 消融时，这块知识在别处的副本也删（清单的 `scrubbed` 里列出）：Brain 目录以外的每个 Markdown 文件里（`decisions.md`、`reflect-proposals.md` 等），`file:` 是这个文件（或 `rel` 指向它的条目）的 `brain-proposal` 块、引用了它的条目号的块，以及其他引用它条目号（如 `T-PAY-1-2`）的行（决策表里的行）。**还会漏的**：代码和测试本身（注释、常量就是这条规则的实现）、需求文档和 `runs/<KEY>/intent.md`、`requirements.md` 里原样写着这条规则但没带条目号的文字、Lean 审计记录（`aidlc-docs/audit/*.jsonl`）、`AGENTS.md` 里没带号的抄写、模型本来就知道的常识。所以"没掉分"只说明这些都不在时 agent 也能做对，不一定说明这条知识没用；
- 快照的清单只返回给调用方，不写进快照，AI 看不到自己考的是哪个变体。

**打分**（`score`）：打进隐藏测试，跑题目的测试命令（去掉环境里的令牌、密钥、GitLab / AWS / 桥的变量），数通过和失败（认 unittest 和 pytest 的输出；认不出时只看退出码）。一个测试都没跑（`Ran 0 tests`、`no tests ran`、全部跳过）不算 0 分，算出错：多半是题目的测试命令找不到隐藏测试（`python3 -m unittest` 不进没有 `__init__.py` 的 `tests/`，所以上面的例子用 `discover -s tests`）。

**跑**：`plan` 生成 题数 × 变体 × 次数 个任务。`run(jobs, runner, tasks)` 里的 `runner(job)` 由 KiroCrew app 提供（真的派单）；测试里用假的。runner 出错的任务记为错误，不当 0 分。

**怎么判"掉分"**（`summarize`）：

- 两边都至少有 2 次有分的运行；
- 去掉这块知识的每一次，都比全量的每一次分低（完全不重叠）；
- 平均掉分不小于 `--min-drop`（默认 0.05）。分数按 6 位小数记，掉分也按 6 位小数比：0.30 对 0.25 正好掉 0.05，算掉分（直接用浮点数减是 0.04999…，会被误判成没掉）。
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

- 键可以是 run 号，也可以是需求号；`RUN-<KEY>-<n>` 本身就说明了是哪个需求，不需要 ledger 或凭证。只有一个 run 时，直接给一条记录也行；`brain eval` 有好几个需求时，一条没有键的记录不知道是谁的，不算给任何一个，报告里会说。
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
- **squash 合并**：分支删了、又是 squash 合并时，合并时间取 main 上带进 run.json 的那个提交；这个需求自己的提交就列不出来了。fast-forward 合并、分支又删了时，合并时间是最后一个 run 提交的时间，不是真正合并的时刻；run 里夹着、又排在最后的别人的提交，会被当成合并之后的。
- **读不了的 run**：某个 `runs/<KEY>/run.json` 坏了（不是 JSON、字段类型不对），`brain eval` 跳过它，在 `skipped` 和报告里写明原因，别的照算。
- **真实数据**：只在自造的仓库上测过。真 run、真 ledger、真凭证都还没跑过一遍。
