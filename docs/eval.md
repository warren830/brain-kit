# Run Trace 和 Eval v1

2026-11-09 · brain-kit 0.1.0

三样东西：

- **run trace**：一个需求从开工到合并后 14 天，发生了什么。一个 JSON。
- **eval**：把所有 trace 合起来算指标，列出要人复看的 run 和值得记的缺口。
- **golden 题和消融**：固定考题，去掉一块 Brain 再考，看哪块知识在"承重"。

都是只读的：只读已有的记录，不改仓库，不联网。同样的输入，结果一样。

## 1. 命令

```bash
brain trace PAY-1                                  # 看一个需求（中文摘要）
brain trace PAY-1 --json --ref origin/main         # 合并以后，从 main 读（还没合并的，读它的分支，和 brain eval 一样）
brain trace PAY-1 --ledger /srv/bridge/ledger.sqlite --vouchers ~/brain-intake/vouchers --credits credits.json

brain eval                                         # 全部需求的指标（开头一张表：这一批每个需求一行）
brain eval --since 2026-11-01 --ref origin/main    # 只看这一期
brain eval --ledger /srv/bridge/ledger.sqlite --stuck-hours 48   # 没合并的 run 48 小时没动静算停住（默认 24）
brain eval --json
brain eval gaps                                    # 值得记的缺口
brain eval gaps --file                             # 记成需求（来源 eval-gap）

brain golden plan --tasks tasks --repeats 3 --json > jobs.json
brain golden snapshot tasks/G-1 --out /tmp/g1 --variant no-TECH    # --mask-secrets: 见第 4 节，要人明确选
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
| stages | 每个阶段最后的状态（done / skipped / started）、时间、说明。Lean 0.3 整段跳过 Ideation 或 Operations 时只记一条 PHASE_SKIPPED（`lean new` 不带 `--ideation`、`lean stage skip ideation\|operations`），它列出的阶段都是 skipped，说明是跳过的原因，`phase_skipped` 写是哪一段（文字摘要里一段一行）。规则和 Lean 一样：阶段自己更晚的记录算数；Ideation 又用上以后（它的某个阶段在最后一次整段跳过之后开始或做完了），之前的整段跳过不算，之前单独跳过的必需阶段（intent-capture、scope-definition、approval-handoff）也不算 | Lean 审计日志 `aidlc-docs/audit/*.jsonl`；每个文件的哈希链都核对，断了会写出来 |
| actions | 提交（人还是机器人、在交接前还是后）、被 Lean 拦下的调用、会话和回合数、交接、run 停下来问的问题 | git；Lean 日志；run.json；ledger；凭证；`park.json` 的每个版本 |
| decisions | decisions.md 的每一行、需求页里的假设 A-n、桥上的问答 | `decisions.md`、`requirements.md`、ledger、凭证 |
| checks | 每次 verify：时间、过没过、哪条命令失败 | Lean 日志的 VERIFY；`build-and-test-summary.md` |
| humans | 人做的每件事：审批、驳回、没被接受的口令、回答、"允许改测试"、交接后往 ai/* 的提交、平台上手动停止或重启 | Lean 日志；ledger；凭证；git；KiroCrew app |
| brain | 开工时装载了几条、引用了哪些、过时的引用、提案 | `brainkit.usage`（不另算） |
| cost | credits，按回合拆分（有的话） | KiroCrew app 传入 |
| outcome | 交接了没有、验收（是否一次通过）、合并时间、14 天内的回滚 / 热修 / 缺陷 | run.json；Lean 日志；ledger；git |

**人的身份**：只出现 GitLab 用户名和化名。git 和 Lean 记的邮箱、名字、主机名，换成 `G-` 加 6 位（邮箱的哈希）。Lean 的审计文件名里有主机名（`<主机>-<6 位>.jsonl`），trace 里写成 `aidlc-docs/audit/L-<6 位>.jsonl`（文件路径的哈希，每台机器上一样）。被拦下的命令、阶段说明、失败的检查命令里出现的邮箱换成 `G-` 化名，`/Users/<名字>`、`/home/<名字>` 换成 `~`。桥的答复人用凭证里的化名 `P-xxxx`。Lean 记的审批口令里写着 `(via gitlab P-xxxx V-…)` 只是"自称"：这个凭证号在读到的凭证或 ledger 里有，就和桥上那条合成一条（答复人是桥上记的）；给了凭证或 ledger、里面却没有它，不认口令里的化名，`who` 是 Lean 这条记录自己的身份（`G-` 化名，运行机上就是机器人账号的）；凭证和 ledger 都没给时没法核对，照口令写。decisions.md 的 Who 栏不是规定的写法时，不写出来（可能是人名）。人的消息原文不抄，decisions.md 里本来就有的除外。

下面是 trace 读 git 的全部规则。每条都有测试（`tests/test_qa_r2_trace.py`、`tests/test_qa_r3_trace.py`、`tests/test_qa_r4_trace.py`，测试类名、测试名或说明里带规则号）。说法里：main = 默认分支（有 `origin/main` 用它，没有用本地的）；分支 = run.json 里的 `branch`；KEY = 需求号；运行机 = kit.json 的 `bot_emails`；**S** = main 的第一父链上第一个带进 `runs/<KEY>/run.json` 的提交。

**读哪一份分支**：分支可能有几份：本地的 `refs/heads/<分支>`、origin 的 `refs/remotes/origin/<分支>`，或者 `--ref <分支>` 给的。没合并时取新的那份（包含另一份的那份）；两份分叉了，用 origin 的。合并了以后读"合并进去的那份"（下面的 M1–M6）。用不上的那几份写进 `src.git.tip_note`：旧了、合并后又有提交、分叉了。所以负责人自己机器上那份停在交接时的旧分支，不会把交接后的事丢掉，结果和没有这个分支的机器一样。不给 `--ref` 时读工作区；工作区的 HEAD 比这个 run 要读的那份旧时（HEAD 是那份的祖先：负责人停在交接时的旧分支上，或者 fetch 了没 pull），run.json、Lean 记录、park.json、决策和小需求也都改从那份读（合并了的、main 上有它的 run.json 时从 main 读），和别的机器一样；`src.working_tree` 写明（`head`、`read`、`note`），工作区里没提交的改动这时不算。

**怎么认合并**（不问 GitLab）。按顺序看，第一条对上的就是：

- **M1 合并提交**：main 的第一父链上有标题正好是 `Merge branch '<分支>' into '<main>'` 的合并提交（GitLab 的写法）；没有的话，S 本身是合并提交、它的第二个父提交里有 run.json，也算。合并进去的那份 = 第二个父提交。只是提到分支名的合并不算：`git pull` 写的 `Merge branch '<分支>' of … into <分支>`、GitLab 解决冲突写的 `Merge branch 'main' into '<分支>'`，都是在分支上做的。
- **M1a squash + 合并提交**（GitLab 勾了 "Squash commits"，又用合并提交）：M1 的第二个父提交只有一个父提交、坐在合并基点上，而这里还有一份分支、和它互不包含：它是 squash 出来的。run 的提交读分支上的，squash 提交本身也算 run 的。分支删了时，就只有 squash 那一个提交。
- **M1b 别的分支上做的合并**：S 是合并提交、第二个父提交里有 run.json，但它的标题写着合并进了别的分支（`Merge branch 'main' into '<别的分支>'`：另一个 run 把 main 合进自己的分支，然后 fast-forward 进了 main）。它不是这个 run 的合并：run 是从它的第二个父提交（当时的 main）进来的，就在那个提交的第一父链上按 M1–M6 重新找（最多找 4 层）。`merged_at.via` 写明。不这样的话，先合并的 run 的合并时间会挪到后一个 run 合并 main 的时候，它真正的热修也落在那个合并的历史里，看不到了。
- **M2 fast-forward**：S 不是合并提交，而且某一份分支包含 S（main 的第一父链上就是分支自己的提交）。从 S 起顺着 main 的第一父链按 R1–R5 找 run 的提交，至少到分支最后一个已经在 main 上的提交；最后一个就是合并点。本地分支旧了、或者合并后分支上又提交了，合并点都一样。
- **M6**：分支最后一个提交已经在 main 上，但 M1、M2 都没对上（别的标题的合并提交）：main 第一父链上第一个包含它的提交就是合并点。
- **M3 rebase**（分支还在，它的提交不在 main 上）：从 S 起的 run 提交（R1–R5）里，有和分支上某个提交 patch-id 一样的。trace 读 main 上的这些副本，合并点是最后一个。
- **M4 squash**（分支还在，main 上没有它的副本）：合并点是从 S 起 run 的最后一个提交（一般就是 S）。分支上在合并之后才做的提交（作者时间晚于合并时间）不算这次合并的。还会比一下 squash 提交是不是正好是分支的全部改动（写进 `merged_at.note`）。
- **M5 分支不在了**：从 S 起按 R1–R5 找。只有 S 一个：squash 合并（run 自己的提交就列不出来了）；不止一个：fast-forward 或 rebase 合并，合并点是最后一个。
- ledger 里 run 的状态是 merged、git 里却找不到合并：合并时间为 null，`merged_at.note` 写明。

**run 自己的提交**（`actions.commits`，也是"交接后人的提交"从哪里找）：

- **C1**：合并进去的那份（没合并时是分支最新的那份），去掉 main 在 run 之前的历史：合并提交（M1、M6）去掉它第一个父提交的历史；M2、M3、M5 去掉 S 的父提交的历史；M4 和没合并的，去掉 main 上有的。
- **C2**：运行机不能 force-push，解决冲突时会把 main 合进分支。这种合并的另一个父提交里没有 `runs/<KEY>/run.json`：它带进来的是 main 的提交，不算 run 的（不会变成"交接后人的提交"）。
- **C3**：分支上合并之后又做的提交不算（M1、M2 看提交之间的关系；M4 看作者时间）。

**main 上从 S 起，哪些是 run 的**（M2、M3、M5 用）：

- **R1**：S、分支最后一个已经在 main 上的提交和它以前的、和分支上某个提交 patch-id 一样的（rebase 的副本），都是 run 的。
- **R2**：合并提交，在 run 分支上做的（标题是 `Merge … into <分支>`：把 main 或 origin 合进分支）或运行机做的，是 run 的；别的合并提交（别的 MR 合进 main）到此为止。
- **R3**：到此为止，而且不是 run 的：交接之后，人（不是运行机）做的回滚（消息里有 `This reverts commit`，或标题以 `Revert "` 开头）、人的提交里点名 KEY 又带修复词（hotfix、fix、bug、defect、regression、revert、修复、热修、回滚、缺陷……；regression test / suite / case / check 不算修复词："add regression tests for PAY-4" 什么也没修）的；改了别的 run 的 `runs/<KEY2>/` 的提交（谁做的、在哪都算）。"交接之后" = 在最后一个 `brain handoff` 提交（run.json 的 handoff 变了的提交）之后：从 S 到它，负责人本地半程的回滚、"PAY-1: fix failing test" 这类提交都是 run 自己的（按 R5 算），不会让 run 在开工那个提交就结束。运行机只在自己的 run 上干活，它的回滚、修复都是 run 的（R4）。
- **R4**：改了 `runs/<KEY>/` 或 `aidlc-docs/<intent>/` 的，和运行机的提交，是 run 的。
- **R5**：别的（人推的、什么都没点名的）：后面还有 run 的提交，才算 run 的。
- run 到最后一个 run 的提交为止。

**H0 这个分支的 run**（H1–H4 之前先分）：一个需求可以跑不止一次：第一个 MR 关了，在新分支上重新开工、重新交接（`RUN-<KEY>-2`）。每个分支有自己的交接、问答、验收和 KiroCrew 上的操作，旧 run 的从来不是新 run 的。trace 读的是 run.json 里 `branch` 那个分支；ledger 和凭证里这个需求的每个 run 属于哪个分支，按顺序认：它的交接凭证写的 `branch`；没有的话，桥接受的那个提交（ledger runs 的 sha，或它 handoffs 那一行的 sha）上 `runs/<KEY>/run.json` 写的 `branch`；还认不出，看同一个 MR 的别的 run（一个 MR 只有一个源分支）。没有 run 的 handoffs 行（拒掉的、正在查的）按它的提交上的 run.json，再按 MR 认。认不出分支的 run 算这个分支的。别的分支的 run：它的 ledger 行、凭证、桥上的问答、KiroCrew 传来的手动停止和重启（按 run 号给的）都不算进这个 trace（不算交接、验收、人的介入、提问，也不算"最后一次动静"）；列在顶层的 `other_runs`（`run_id`、`branch`、`state`、`src`）。`run_ids` 还是这个需求的全部 run 号，credits 也算全部 run 的（这个需求一共花了多少）；`outcome.bridge_runs` 只有这个分支的。

**交接是哪一次**（`outcome.handed_over`：`value`、`commit`、`rule`、`how`、`requests`、`requested`、`bridge_accepted`）。一次"交接请求" = run 的一个提交，它的 run.json 里 `handoff` 不为空、又和上一版不一样（每跑一次 `brain handoff` 写一次）。第一次没过（exit 1 没打上标签，或者桥拒了、拒的评论让负责人再跑一次）时，会有好几次请求。下面的"桥的记录"都只看这个分支的 run（H0）。按顺序看：

- **H1 桥接受了**：有交接凭证（签名不是 bad）、ledger 的 handoffs 里有 accepted、或 ledger 里有这个需求的 run（正在签的 `signing` 不算）：取最早的那个。交接 = 它的提交本身或之前的最后一次请求；那个提交不在这里（rebase 了）时，取 run.json 里 handoff 一样的那次请求；还对不上，取桥接受之前的最后一次请求。之后负责人再跑的 `brain handoff`（比如 run 失败了重交）算交接后人的提交。
- **H2 桥拒了**：没有接受的记录，ledger 里有桥拒掉这个 run 的请求（handoffs 的 rejected；按请求的提交找，拒掉的交接没有 run 那一行）：没有交接（"只在本地做"）。运行机没拿到过它，负责人接着做的都是本地半程。
- **H3 桥没接受过**：给了 ledger，没有接受的记录（也没有交接凭证），又没找到拒掉的那一行：没有交接。ledger 是桥的账，运行机只从桥那里拿 run，所以给了 ledger 就只认 ledger：没有任何一次请求的记录（`brain handoff` 停在打标签之前，或者桥还没来得及检查）、桥还在查（handoffs 是 checking），或者拒掉的那一行找不到（分支删了、请求的提交读不到，按提交找不到它），都是没有交接。请求之后运行机有提交、ledger 却没有记录时，也不按运行机的提交认交接，`how` 里写明（多半给的不是这个桥的 ledger）。
- **H4 没有桥的记录**（只在没给 ledger、也没有交接凭证时）：取运行机第一个提交之前的最后一次请求（之前那几次没过）；运行机一个提交都没有时，取最后一次。分支不在了、run 的提交列不出来时，按 run.json 的 `handoff`。
- `actions.handovers` 里每次请求一行，`taken` 标出用的是哪一次。

**用哪个时间**：

- 提交的 `at` 是作者时间：提交是什么时候做的，rebase、cherry-pick 都不改它；`committed_at` 是提交时间（进 main、或被 rebase 的时间）。以前用的是提交时间，rebase 合并后每个副本都成了合并那天。park 问题的时间也是带它的提交的作者时间。
- 合并时间：M1、M6 是合并提交的提交时间；M3、M4 是 main 上 run 最后一个提交的提交时间（squash 提交、rebase 后最后一个副本）；M2、M5 是最后一个 run 提交的提交时间（git 不记 fast-forward 是什么时候做的）。
- "交接后"的提交：不在交接提交（H1–H4 认出的那次请求）历史里的提交，看提交之间的关系，不看时间。交接的那个提交不在读到的提交里时（看不出关系），才按作者时间和 `handoff.requested_at` 比；连时间都没有的人的提交，`after_handover` 是 null，照样算一次"交接后人的提交"（自主度是"?"），不会被漏掉。Lean、桥、KiroCrew 上人做的事，用它们自己的时间和那次请求的 `handoff.requested_at` 比。没有交接（H2、H3）时，什么都不算交接后。
- 文件名用 `git log -z --no-renames` 读：带空格、引号、换行的文件名不会被转义成别的样子；改名时新旧两个名字都算"改过"。

**合并后 14 天**（`outcome.reverted_or_hotfixed_within_14d`）：

- **A1**："合并后"的提交 = 在 main 上、又不在合并点历史里的提交（不用 `git log --since`，一个时间早的提交会把后面的都挡住）。时间只用来框 14 天：按它的提交时间（进 main 的时间），从合并那一秒（含）到 14 天后（含）；和合并同一秒进来的热修也算。这个 run 自己的提交（C1–C3、R1–R5 认出的，和 M1a 的 squash 提交）不算热修。
- 回滚（`This reverts commit <run 的提交>`），或者点名 KEY、又带修复词的提交（修复词和 R3 一样：regression test 不算），算回滚 / 热修；新的报告问题的 `claim/<KEY2>`，标题点名 KEY 又带修复词的，算缺陷（第 3 节）。
- **A2**：回滚和热修，都要改了 `brain/`、`runs/`、`aidlc-docs/` 以外的东西才算。只改 Brain 和记录的不算：`brain reflect --apply` 的提交和它的 MR 合并，消息里都点名这个需求，缺口的标题又常带"回滚、故障、缺陷"这类词。这些列在 `ignored` 里（写明为什么），不算数。合并提交看它带进 main 的改动（和第一个父提交比）。

**同一件事只算一次**：桥送回 Lean 的审批口令，在 Lean 日志里也有一条。两条按凭证号合成一条，来源都列上。decisions.md 里记下桥上回答的那一行，也按凭证号连到桥上那条决策。

## 3. 指标

**E1 哪些需求**：`brain eval` 读四处，一个需求只算一次：

1. 读的那棵树（工作区或 `--ref`）里的 `runs/<KEY>/run.json`：在 main 上读，就是已经合并的；
2. 这里的 run 分支（本地的和 origin 的 `ai/<人>/<KEY>-<名字>`）里，上面没有的需求：没合并的 run，从它的分支读（几份里取新的那份，和第 2 节"读哪一份分支"一样；一个需求有几个分支时，取 tip 有 run.json、而且最后提交的那个；别的分支上的 run 按 H0 列在 `other_runs`）。报告开头写"没合并的 n 个从分支读"，JSON 的 `open_branches`。`brain trace KEY` 不给 `--ref`、或者 `--ref` 是默认分支（`--ref origin/main`），那里又没有它的 run.json 时，读的也是这个分支；
3. 给了 `--ledger` 时，ledger 里有 run、这里却读不到 run.json 的需求（分支没 fetch、MR 关了分支也删了）：列在 `skipped`（带它在 ledger 里的状态，`ledger_only`），也进下面那张表，报告第一行写"另有 n 个只在桥上"。桥签交接凭证时先占的 run 号（ledger 的状态是 `signing`）还不是 run，哪里都不算。
4. 读的那棵树的历史（`--ref`，或工作区的 HEAD）里有过、后来被它第一父链上的一个提交删掉的 `runs/<KEY>/run.json`，上面几处都没有的需求：从删掉它之前的那个提交读（最后一次删的那个提交的第一个父提交）。最常见的是 GitLab 上把整个 MR Revert 了，源分支合并时又删了：不这样，这个交付错了的 run 会从交付正确率的分母里消失。合并和 14 天里的回滚照常从 main 认。JSON 的 `removed`（`read_at`、`removed_in`、`revert`：删它的是不是回滚提交），报告开头写一行。`brain trace KEY` 不给 `--ref`、或者 `--ref` 是默认分支时，也从那里读。

`--credits` 只给了一条没有键的记录、需求却不止一个时（只在桥上的也算），不知道是谁的，不算给任何一个。

**期内**（`--since`）：最后一次有动静（阶段、人做的事、决策、提交、合并或验收的时间，和 `outcome.last_activity`：ledger 里 run 的行变了（MR 关了、运行机失败）、凭证）在期内的 run；没合并、没关的 run 不管 `--since` 都算（E3）。

**E2 状态**（JSON 的 `states`）：`merged` 已合并；`merged_not_here` ledger 说合并了，这里的 main 没有；`closed` MR 关了、没合并，或者需求单关了（`brain intent close`，普通需求和小需求都一样，和 `brain intent list` 一样算关了：`closed_by` 是 `claim`，不算在做，也不算停住；它的 run 分支还在这里的，多一个 `leftover`：`branch` 和 `where`（`local` / `origin`），要复看里提醒负责人删掉分支、关掉还开着的 MR）；`failed` 运行机没能开始（ledger 是 failed：brain-runnerd 失败）；`unknown` ledger 的状态以 `_unknown` 结尾（`send_unknown`：桥不知道派单成没成）；`answered` 有了回答、run 没接着做：run 分支上的 park.json 现在还是 waiting，可桥上已经有了这个问题的回答（决策是 delivered 已送到 run，或 answered 正在签名、发送，或 delivery_unknown 送达不明），谁都不用再答，是运行机（或桥）没往下走（`outcome.answered`：`decision`、`state`、`choice`、`at`、`kind`、`ask`、`lean_request`、`since`、`src`；这时 `outcome.waiting_on` 是 null）；`waiting` 在等回答（park.json 现在是 waiting、这个问题还没有人答，或者桥上的问题还开着；`outcome.waiting_on` 的 `ask` 是 park.json 问的谁）；`accepted` 验收过了、还没合并；`open` 进行中。"这个问题"的回答 = 桥在这个 park.json（它最近一次被问起的那个提交）或它之后的 MR head 上问的决策；签名服务拒签（`refused`）的回答没送到 run，不算回答。没合并、没关、没失败的 run，最后一次有动静（提交、Lean 记录、park 问题、ledger 里这个 run 的行、凭证；`outcome.last_activity`）离 `--as-of` 超过 `--stuck-hours`（默认 24 小时）的，算停住了（`stuck`）。

| 指标 | 怎么算 | 现在量不到的 |
|---|---|---|
| 自主度 | 交接以后，除了必经的审批（REQUIREMENTS、DESIGN、INCEPTION、PLAN、RELEASE、验收），人没做过别的事、而且交付正确的 run ÷ 已完成、交接过、对错已知的 run | 本地半程是人自己带着 AI 做，不算介入（本地的提交分不清是人还是 AI 写的）。没交接过的 run 单列，不算。验收被驳回过、还没合并的 run，和合并了的一样，等合并满 14 天再算（不先算成“不对”，合并后又退出分母）。验收过、后来 MR 关了或运行机失败的，不算（`status` 是 closed / failed）。交接后的提交读不到的（squash 合并、分支删了），看不出有没有人推过，不算（`status` 是 `unknown (the run's commits are not here)`） |
| 交付正确率 | 一次通过验收、合并后 14 天内没有回滚和热修的交付 ÷ 14 天已满、有验收记录的交付 | 没接 Jira 时，缺陷单只能看新的 `claim/<KEY>`：报告问题的（来源是 product、alert，或没有来源的老认领），标题点名这个需求、又带"修复 / bug / 缺陷"这类词的才算；gap、eval-gap、debt、schedule 这些"缺了什么"的需求不算缺陷。热修只认点名需求号、又改了 `brain/`、`runs/`、`aidlc-docs/` 以外东西的提交（A2）。14 天没满的列为"还不知道"；已经知道出了问题、14 天还没满的，也等满了再算（单列 `wrong_open`），这样一期里的好坏按同样的时间算 |
| 介入落在判断点的比例 | 落在判断点上的人工介入 ÷ 全部人工介入。判断点 = Lean 的审批口令（批准、驳回、没被接受的口令）和桥上的 park 问题。别的都算判断点之外：交接后人往 ai/* 的提交、"允许改测试"、KiroCrew 上手动停止或重启 | 方案里"复盘也认定确实需要人判断"这一半要人看。git 不记推送，用"交接后人的提交"代替。手动停止和重启要 KiroCrew app 传进来才有 |
| 重复纠正率 | correction 条目的 repeats 合计 ÷（correction 条目数 + repeats 合计），目标趋近 0 | 和 `brain usage` 一样 |
| 知识有用率 | 被引用过的条目 ÷ 现有条目；另给每个 run 引用数之和 ÷ 装载数之和 | 只认 Lean 记录里写出的条目号 |
| 过时与冲突 | 过时引用（开工时已归档的条目还被引用；给了 `--since` 时只算期内的 run 的）+ lint 的"同一个术语定义了两次" + 本期被覆盖或撤回的条目 | 桥上"两个人回答不一样"只发了 MR 评论，没进 ledger，数不到 |
| credits | 完成的 run 花的 credits 合计 ÷ 正确交付数。只用来诊断，不设上限 | **token 数量不到**：经 Kiro 跑模型，KiroCrew 只给 credits，input、output、cache 的 token 数都是 0。credits 也读不到，要 KiroCrew app 传进来 |

**决策漏斗**：期内的每一条决策，按"谁定的"归一类，每条只算一次，也按周（ISO 周）列：

1. **人**：桥上回答的问题；decisions.md 里 Who 是"化名 凭证号"（`P-0305 V-a1e0771735`），或者本地半程的 `owner`。
2. **确定性检查**：Who 是 `check`。
3. **Brain**：Who 是 `brain <条目号>`，或者这一行引用了条目号。
4. **agent 自己定**：Who 是 `agent`（或 kit.json 里的机器人账号），或者是需求页里的假设 A-n。
5. **看不出**：Who 空着。
6. **没归类**：Who 写了别的（product、人名、"TBD"……）。不猜，单独算，并列出是哪个需求的哪一行，好让人改；Who 的原文不写出来。

Who 的写法是运行机提示词（`remote/prompts/brain-run.md` 第 5 节）和本地的 steering（`kit/.kiro/steering`）定的：`agent`、`brain <条目号>`、`check`、`<化名> <凭证号>`，本地半程负责人在对话里答的写 `owner`；后面可以加一个括号写备注（`agent (A-2)`）。条目号、化名写成代码（`` brain `T-PAY-1-1` ``）也认。`brain eval` 报“没归类”时，提示的也是这几种写法。

还没回答的问题（按提问的时间算在哪一期）、考题的自动回答（eval_oracle，按回答的时间）都不进漏斗，单独报个数。**E4**：桥上不再等回答的问题（`stale` MR 更新了、问题重发；`void` 作废；`invalid` 无效；`closed` MR 关了）不算"还没回答"，按状态另报（`funnel.dropped_questions`）。签名服务拒签（`refused`）的回答没送到 run：不算人的决策、不算验收、也不是人的一次介入，问题算"还没回答"（复看队列里是 `question_problem`）。

**要人复看的 run**（每条写明原因）：

- 验收被驳回过；
- 合并后 14 天内有回滚、热修或缺陷；
- verify 失败 3 次以上（CI 的失败读不到，只算 verify）；
- 同样的纠正又来了：提案里有 `rel: repeats`，或者一个 run 被驳回 2 次以上；
- 引用了已归档的条目；
- 判断点之外有人介入；
- credits 超过中位数的 2 倍（至少 3 个 run 有 credits 时才判断）；
- **E3** 没合并的 run（不管 `--since`，没合并就还在这一批里）：停住了（`stuck`，写明最后一次动静，和在等谁（park.json 的 `ask`）回答哪个问题；`answered` 的写明哪个回答什么时候送到了、run 没接着做，该去 KiroCrew 看 slot，不写"在等回答"）；MR 关了没合并（`closed_unmerged`）；运行机失败（`runner_failed`）；派单结果不明（`state_unknown`）；桥上的问题作废、无效、送达不明、签名服务拒签（`question_problem`：void、invalid、delivery_unknown、refused）；ledger 说合并了、这里没有（`merge_not_here`）；ledger 里有、这里读不到 run.json（`not_readable`，MR 关了的除外）。

**E5 这一批**（报告开头那张表，JSON 的 `batch`）：每个需求一行：需求、状态（停住了的写"n 小时没动静"）、自主（交接后除了必经的审批没人插手：是 / 否（做了什么）/ 本地 = 没交接 / ? = 看不出：有人做了事、但没有时间，或者交接后的提交读不到（squash 合并、分支删了））、人工介入落在判断点内 / 外的次数、问过几个问题（park.json 里不同的问题，或桥上的问题，取多的）、credits、交付（对 / 不对 / 14 天未满 / 有问题（14 天未满）/ 没有验收记录 / 还没合并 / 没合并 / 桥说合并了（这里读不到合并，`merged_not_here`））。交接过、却没交付就结束了的 run 从来不算自主：运行机失败（`failed`）、MR 关了没合并（`closed`），自主是"否"，`beyond` 里写 `runner_failed` / `closed_unmerged`（`beyond` 是挡住自主的原因：交接后人做的事，或这两种结局）。`skipped` 里的每个需求也有一行：只在桥上的（`not_readable`），和 run.json 读不了的（`unreadable`：交接、自主都是"?"）。表上面一句话：交接给运行机的有几个、交接后没人插手的几个、其中已经确认交付对的几个。"能不能无人值守地跑一批"先看这张表，再看下面的复看队列。

**E6 大需求**（`brain split` 拆出来的，见 [安装和使用.md](安装和使用.md) 4.7）：

- 小需求和大需求共用一个 Lean intent。小需求的 trace 只读它自己 worktree 的 Lean 账本（run.json 的 `lean_clone`），提交按合进大需求的分支来认；`merged_at` 还是合进 main 的时间。另有 `family.merged_into_parent`：它 `brain handoff` 时在 run.json 记的 `mr` 和它分支上的提交都到了大需求的分支（`intents.child_merged`；只有 run.json 不算，从它分支开工的兄弟也带着它）。
- 大需求的 trace 去掉小需求的账本和提交。`family` 里列每个小需求的数（做完的阶段、verify 次数和失败次数、机器人和人的提交、被拦次数、判断点内外的人工介入、决策、问题、credits），`rollup.total` 是大需求自己加所有小需求；哪个数谁都不知道，就是 null。decisions.md、requirements.md 的行按 `git blame` 分给写它的那个 run。
- 状态多一个 `merged_into_parent`（已合进大需求，等大需求进 main）：不算停住。需求单关了的小需求是 `closed`（见 E2）。大需求的空闲时间按全家最后一次动静算。
- `batch` 每个需求照旧一行，小需求多一个 `parent`。`families` 每个大需求一行：小需求、已合并 n/m、状态、判断点内外的人工介入合计、问题合计、credits 合计。n/m 和 `brain intent list` 一样数：m 是没关的小需求，`brain split` 记的需求单（`claim/<KEY>` 写着这个大需求）有、这里还没有 run 的也算（`not_started`）；需求单关了的不算（`closed`），n 是其中已合进大需求分支的。`children` 还是这里有 run 的小需求。都合并了（n = m）才是 `ready_to_release`（等大需求评审和发布）。需求单读这里已有的（origin 的那份，没有就用本地的），不 fetch。报告里多一段"大需求"。没有拆分的仓库，`families` 是空列表，别的不变。
- `brain usage`：有小需求的 intent，每个 run 一行；单元自己的页面（`construction/<单元>/`、它的代码计划）算那个小需求的，别的页面（包括 decisions.md）算大需求的。

**缺口**（`brain eval gaps`）：

- 被驳回过、但 decisions.md 里没有引用它的 correction 或 repeats 提案（知识缺口）。验收时成批审、驳回了其中几项（`rejected_items`）的，每一项都要有一条 src 写着这一项的提案（和 `brain reflect` 一样）；本地 Lean 里的驳回没有凭证可引，提案的 src 写记下这次驳回的那一行（同一阶段、当天或之后的 decisions.md 行）就算。“同一阶段”按 Lean 的审批管哪些阶段看 Stage 栏：REQUIREMENTS 是 requirements；DESIGN 是 design；INCEPTION 管 requirements 和 application-design，写 inception、requirements、design 都认；PLAN 管 code-generation，写 plan、code 都认；RELEASE 是 release、acceptance、验收；
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
  - 目录只能放在某个 git 工作区下面时（CI 里 `TMPDIR=$CI_PROJECT_DIR/tmp`、家目录本身是个 dotfiles 仓库），设 `GIT_CEILING_DIRECTORIES`（git 自己的"别往上找"）到两者之间的一层，比如 `GIT_CEILING_DIRECTORIES=$CI_PROJECT_DIR`。检查认这个设置；清单的 `git_ceiling` 记下是哪个仓库被它挡住了，命令也会提醒：**agent 的 run 也必须带上同样的设置**，不然它在快照里照样能找到那个仓库。快照放在项目自己的工作区里，设了也拒绝；
  - brain-kit 自己的测试（`tests/test_golden.py`、`tests/test_qa_modules.py`、`tests/test_qa_r2_modules.py`）在开始时把 `GIT_CEILING_DIRECTORIES` 设成临时目录的根，所以 `TMPDIR` 放在哪都能跑；
- 删掉这些文件（清单的 `removed` 里列出）：
  - git 元数据：`.git`、`.gitmodules`（子模块的远端地址，可能带密码）。子模块的内容本来就不在快照里（`git archive` 不带），清单的 `submodules` 列出这些空目录；
  - 凭证文件：`.git-credentials`、`.netrc`、`_netrc`、`.pypirc`、`.pgpass`、`.my.cnf`、`.htpasswd`、`.dockercfg`、`.vault-token`、`.s3cfg`、`.boto`、`.terraformrc`、`terraform.rc`，任何目录下的 `.aws/credentials`、`.docker/config.json`、`.kube/config`，带登录令牌的 `.npmrc` 和 `.yarnrc.yml`；
  - env 文件：`.env`、`.env.*`、`.env-*`、`.env_*`、`.envrc`（`.env.example` 也删）；
  - 密钥库（二进制，扫不进去）：`*.p12`、`*.pfx`、`*.jks`、`*.keystore`；
- 符号链接按文件系统的方式解析（经过别的链接也算），指到快照外面的就整个拒绝；
- 再扫一遍私钥、云访问密钥、令牌，和配置、脚本里单独一行的密码赋值（大写的名字以 PASSWORD、SECRET、TOKEN、API_KEY 等结尾，值是写死的：`DB_PASSWORD=…`、`export X_TOKEN=…`、`SECRET_KEY: …`、`API_TOKEN = '…'`；`${VAR}`、`$VAR`、`None` 不算）。密码赋值这一条不扫代码文件（`.py`、`.js`、`.ts`、`.go`、`.java` 等）和文档（`.md`、`.rst`、`.txt`），也不扫小写的名字和 URL 里的密码（`postgres://u:p@db`）：测试里的假密码和 README 里的示例太常见，扫了大多数仓库都做不成快照。扫到的值分三种：
  - 不是密钥，不动：给上限的数字（名字里带 MAX、MIN、LIMIT、TTL、SIZE 等：`MAX_TOKEN: 4096`）、请求头的名字（`CSRF_TOKEN: X-CSRF-Token`）；
  - 开发用的默认值，打码并列出（清单的 `masked`：路径、行号、变量名，不写值）：docker-compose 和 CI 服务里常见的 `POSTGRES_PASSWORD: postgres`、`- POSTGRES_PASSWORD=postgres`、`runner-test`，`root`、`admin`、`changeme`、`123456` 这类，值里带 test / dev / local / ci / example / dummy / fake 等词的，或者值就是变量名里的一段（`MYSQL_PASSWORD: mysql`）。值换成 `masked-by-brain-<n>`，同一个值换成同一个，所以 compose 和应用配置里写的同一个默认密码还对得上；
  - 像真密钥的（其他的值，以及私钥、云访问密钥、令牌）：整个拒绝，快照不留，报错里只写路径和规则名。考题的 base_sha 是定死的，换不了 commit 时，确认这些是测试用的假值、或者本来就要轮换，可以加 `--mask-secrets`：密钥和密码都打码（同上），带私钥的文件整个去掉（列在 `removed`），清单写 `"override": "mask-secrets"`，命令在 stderr 打一行 WARN。这是要人明确选的，不会自动发生；
- 消融变体：`full`、`no-PRODUCT`、`no-TECH`、`no-IMPROVEMENT`，从快照里删掉对应的 Brain 文件和它的 archive。PROJECT 没有消融变体；
- 消融时，这块知识在别处的副本也删（清单的 `scrubbed` 里列出），只删去掉的那个文件（和它的 archive）里的条目号，按整个号比（`T-PAY-1-2` 不会碰到 `T-PAY-1-20`，也不会碰到别的文件或示例里的 `T-PAY-88-1`）：
  - Brain 里留下的文件（`IMPROVEMENT.md`、`PRODUCT.md` 等和它们的 archive）：引用了它条目号的条目整条删（条目头的 `src`、`rel` 里写着它也算），条目外引用了它完整条目号的行也删；
  - run 的文档里（`aidlc-docs/**`、`runs/**`，即 `decisions.md`、`reflect-proposals.md` 等）：`file:` 是这个文件（或 `rel` 指向它的条目）的 `brain-proposal` 块、引用了它条目号的块，以及其他引用它条目号的行（决策表里的行）；
  - 别的 Markdown 文件里：只删引用了它完整条目号（`T-PAY-1-2`）的行，不删块；
  - 老式的短号（`T-1`）也常是普通的词（"每天 T-1 日的交易"），只在 run 的文档和 Brain 的条目里、写成引用的样子时才算：`` `T-1` ``、`(T-1)`、`[T-1]`、`（T-1，T-2）`、`per T-1`；
  - 指令文件一个字都不改，每个变体的 agent 拿到的说明都一样：`.kiro/**`（`brain install` 装的 steering、skill，Lean）、`.claude/**`、`.cursor/**`、`.github/**`、`AGENTS.md`、`CLAUDE.md`、`GEMINI.md`。
  - **还会漏的**：代码和测试本身（注释、常量就是这条规则的实现）、需求文档和 `runs/<KEY>/intent.md`、`requirements.md` 里原样写着这条规则但没带条目号的文字、Lean 审计记录（`aidlc-docs/audit/*.jsonl`）、`AGENTS.md` 和指令文件里的抄写和引用、别的文档里写成短号的引用、模型本来就知道的常识。所以"没掉分"只说明这些都不在时 agent 也能做对，不一定说明这条知识没用；
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
- **squash 合并**：分支删了、又是 squash 合并时，合并时间取 main 上带进 run.json 的那个提交；这个需求自己的提交就列不出来了，交接后有没有人推过也就看不出来（自主度是"?"，不算）。分支还在、但停在交接时（旧了）、又是 squash 合并时，交接后运行机的提交也列不出来（它们只在已经删掉的分支上）。
- **fast-forward 合并**：合并时间是最后一个 run 提交的时间，不是真正合并的时刻。run 里夹着、又排在最后的别人的提交（R5），会被当成合并之后的；合并后紧接着有人往 main 推了改 `runs/<KEY>/` 或 `aidlc-docs/<intent>/`、又没点名修复的提交（R4），会被当成 run 的；交接后人往分支推的、点名 KEY 又带修复词的提交（R3），在 main 上会被当成合并后的热修。没交接过的 run（只在本地做），负责人自己的回滚、修复提交还是会让 run 在那里结束（没有交接点可依）。分支还在时，分支上有的都按分支算（R1），不会这样。
- **交接**：给了 ledger、桥还没来得及检查（ledger 里还没有这一行）时，按 H3 算没交接；桥检查过以后就对了。给的 ledger 不全（换过桥、给错了文件）时，它没记的 run 即使运行机提交过，也按 H3 算没交接（`how` 里写明）。没给 ledger、也没有凭证时（H4），桥拒掉、负责人自己做完的 run 看不出来，还是算交接了。
- **一个需求几个 run**（H0）：认不出属于哪个分支的 run（没有交接凭证，桥接受的那个提交这里也没有）算这个分支的；同一个分支名删了又重建、开了新 MR 的，两个 run 都算这个分支的。
- **没合并的 run**：只看得到这里有分支的（`git fetch` 过的）和 ledger 里有的。ledger 只记交接被接受之后的 run；没给 ledger、分支也没 fetch 的，`brain eval` 看不到。
- **读不了的 run**：某个 `runs/<KEY>/run.json` 坏了（不是 JSON、字段类型不对），`brain eval` 跳过它，在 `skipped` 和报告里写明原因，别的照算。
- **真实数据**：只在自造的仓库上测过。真 run、真 ledger、真凭证都还没跑过一遍。
