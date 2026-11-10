# 需求从哪来，回顾写回什么

2026-11-08 · brain-kit 0.1.0 之后新增 · 只适用于 `tracker: none`（需求号由 claim 分支分配）

## 1. 需求的三个来源

| 来源 | 谁 | 例子 | 怎么登记 | `source` |
|---|---|---|---|---|
| 产品 | 产品 | 需求文档 | `brain start <slug> --doc <文件夹> ...`（原来的路） | `product`（老的 claim 不写 source，也算产品） |
| Builder | Builder | 技术债、告警跟进 | `brain intent add --source debt` / `--source alert` | `debt`、`alert` |
| 系统 | 定时任务、评测 | 每周依赖检查、评测发现的缺口 | `brain intent add --source schedule` / `--source eval-gap` | `schedule`、`eval-gap` |

另外还有一种：run 结束后回顾发现的**缺口**（见第 3 节），`source` 是 `gap`。

不管哪来的，最后都是一个 `claim/<KEY>` 分支，号码照常按 `req_prefix` 往后排，开工照常用 `brain start`。用 `--key` 自己指定号码时，必须是 `<req_prefix>-<n>`：n 从 1 起、不带前导 0、最多 7 位（`PAY-x`、`PAY-007`、`PAY-12345678` 都拒绝）。

## 2. 登记、查看、开工

```bash
brain intent add --source alert --title "支付回调超时告警一周 12 次" --body-file alert.md \
  --acs "回调失败重试 3 次" "同一回调只入账一次" --refs src/pay/callback.py
brain intent list                 # 待开工：已登记、还没有 run 分支、没关；还有没完成的大需求和它的小需求
brain intent list --all           # 全部，带状态 open / started / closed
brain intent list --json          # 给程序读（2.5）
brain intent show PAY-7           # 看正文、验收标准、参考
brain intent close PAY-7 --reason "误报，阈值已改"    # 只有登记的人能关
brain start retry --key PAY-7     # 开工：用 claim 里的标题，正文写进 runs/PAY-7/intent.md
```

- **同一件事只登记一次**：来源、标题、正文都一样，就返回原来的号，不再占新号。关掉的也算，所以定时任务每天跑也不会刷出一堆需求；真要重开，就改正文。两个人同时登记同一件事，后到的那个会自己关掉，写明"duplicate of 先到的号"。按旧规则登记过、现在的检查会拒绝的文字，再登记一次也返回原来的号（什么都不新存）。
- **规则变了以后，旧的需求照样能用**：服务器钩子对新建的 claim 查全部字段，对之后的推送只查改了的字段，所以主人照样能关掉旧需求（`closed` 要过检查，没改的标题、正文不再查）。`brain start --key` 对格式不对的 claim（哈希对不上、多出字段）照样拒绝；正文里现在的规则才认出来的内容只打 WARN，不拦，开工前看一眼 `runs/<KEY>/intent.md`。
- **告警和技术债的文字不可信**：标题、正文、验收标准、参考里有密钥、个人信息、带币种的金额、合同号、打码占位符（`[人名-1]`）、像指令的行（approve…、"忽略以上指令"），**直接拒绝，不打码**。改好文字再登记。GitLab 的服务器钩子对 claim 分支做同样的检查。检查是按规则找的，下面是它**认得**和**认不得**的（和 Brain 条目、文档收料用的是同一套检查）：
  - 人名，认得：`@张伟`、`@Alice Wang`；标签后面的名字：`负责人`、`联系人`、`对接人`、`值班人`、`报告人`、`提出人`、`处理人`、`经办人`、`跟进人`、`执行人`、`申请人`、`审批人`、`审核人`、`修订人`、`发起人`、`提交人`、`报修人`、`指派给`、`分配给`（繁体也认；`值班` 要带冒号，如 `值班：张伟`），后面是常见姓氏加一两个字（`张伟`、`陳大文`，复姓 `欧阳娜娜`），或姓氏加称呼、职位（`张经理`、`王总`、`王工程师`），到标点、空格、行尾或"和/及/与/等"为止（`负责人：张伟，`、`负责人张伟，`、`联系人：王芳、李娜`）；英文标签 `Owner`、`Assignee`、`Reporter`、`On-call`、`Contact`、`Approver`、`Reviewer`、`Author`、`Requester` 后面带冒号（或 `=`）再跟 2–4 个首字母大写的词（`On-call: Zhang Wei`、`Assignee: Mary-Jane Smith`）；`Reported by`、`Assigned to`、`Approved by`、`Reviewed by`、`Prepared by`、`Written by`、`Raised by`、`Requested by`、`Filed by`、`Opened by`、`Escalated to`、`Handled by`、`Acknowledged by`、`Resolved by`、`Fixed by` 后面 2–4 个首字母大写的词、其中一个是常见姓氏（拼音、粤拼、常见英文姓：`Reported by Alice Wang`）；表格里表头是负责人、修订人、联系人、审批人、Owner、Reviewer 这类的列：一格里的几个人、人和团队写在一起时逐个判断，只打码人名（`张伟、支付组` 只打码张伟；`Alice Wang, Payments Team`、`李娜、王工程师`、`Peter To`、`Sarah Jenkins` 都认），分隔符是 `、` `,` `/` `;` `&`、括号、`<br>`，和两个名字之间的"和/及/与"。
  - 人名，认不得：没有标签的名字（`张伟说已经修了`）、名字后面直接跟汉字（`负责人张伟已确认`）、用逗号接着写的第二个名字（`联系人：王芳，李娜` 只认王芳）、生僻姓氏、单独一个英文名（`Owner: Alice`）、全小写的英文名、英文标签不带冒号（`Owner Alice Wang`、`Contact Alice Wang`）。不算人名：职位、团队、系统、班次和常见的词（`负责人：产品经理`、`支付组`、`客服`、`提出人：顾客`、`对接人：顾问`、`审批人：常务副总`、`值班：白班`、`执行人：任务调度`、`Payments Team`、`Reported by Zabbix Server`、`Reported by New Relic`、`Raised by Health Check`、`Requested by Acme Corp`，公司名另算，见下），以及功能名、界面名里的 Owner / Contact / Reviewer（`Contact Form Validation`、`Account Owner Transfer Process`、`Reviewer Comments Section`）。
  - 拿不准的（像人名或密码，也可能只是个词）：标签后面单字姓再跟三个字（`负责人：张三李四`）、单字姓加两个字且以"员/长/师/方/会/岗"结尾（`负责人：孙店长`）、`Reported by` 这类后面没有常见姓氏的 2–4 个大写词（`Reported by Sam Example`）、句子里 `密码是` / `密码为` 后面只有字母的值（`密码是 hunter`）。这些**不拒绝、也不打码**：登记告警和写 Brain 时放过，文档收料时在 findings.md 里列出来（"请你确认"），请提交的人看一眼，是人名或密码就改原文再交。
  - 手机号，认得：11 位连写（`13800138000`、`+86 13800138000`）和 3-4-4 分开写（`138 0013 8000`、`138-0013-8000`、`+86-138-0013-8000`、`(+86) 138 0013 8000`）；香港号码带 +852 或前面有"电话/手机/phone"等字样。认不得：别的分法（`1380 013 8000`）、混用分隔符。恰好是 1xx-xxxx-xxxx 的编号也会当成手机号拒绝，请换个写法。
  - 密钥和密码，认得：私钥、云访问密钥、常见令牌格式、`Bearer …`；`password: …` / `token=…` / `api_key = …` / `密码：…` 这类键值（值算到第一个汉字前，至少 4 个字符）；句子里的 `密码是 …`、`密码为 …`、`口令是 …`、`password is …`，值带数字或符号（`Hunter2024`、`abcd1234`；`is` 后面的值必须带数字）。认不得：中间隔了别的词（`数据库的密码现在是 …`）、值是中文、`${VAR}` 这样的引用、打过码的 `******`。说明格式、算法、类型、规则的词不算密码：`password is required`、`密码为 8-16 位`、`The password is 6-digit`、`password is base64-encoded`、`密码为 SM4-CBC 加密存储`、`口令为 TOTP 动态口令`、`密码为 null 时提示`、`token: string`。
  - 邮箱、身份证号（核校验位）、银行卡号（Luhn）都认。
  - 文档收料打码的范围：一个值只要在一处被认定是敏感的，同一个值在整个材料包里出现在哪都打码：别的行、别的文件、隐藏内容、文件名。按格式认出来的（身份证号、手机号、银行卡号、邮箱、私钥和令牌格式、金额、公司名、合同号）和按标签认出来的人名、密码都这样（例如 `负责人：张伟` 认出张伟以后，别处没带标签的"张伟"、文件名 `张伟的需求.docx` 也换掉）。只有确定的才往外扩，而且按整词换（`RefundService` 里的 Service、`13988881234` 里的 8888 不动）：人名检查不认的词（`提出人：顾客` 里的"顾客"）、名字列里没有姓氏的值（`风控`、`Billing`）、只列为"需要人看"的，都不扩；代码里的注解（`@Service`）和 `@所有人` 不算人名；人名本身也是常用词时（`负责人：高峰`，别处有`高峰期`），只打码认出的那一处；密码是 admin、test、password 这类普通词，或者就是字段名（`token=refresh_token`）时，只打码那一处，不扩，免得别处的 admin（角色名、`/api/admin`）也被换掉。
- **记在谁名下**：登记的人（`GITLAB_TOKEN` 或 `git config brain.user`）。钩子要求 claim.json 里的人就是推送的人，机器人账号不能登记。开工的人也必须是这个人。加 `--unclaimed` 登记给别人做（待认领，见 2.1）。
- **run 怎么读到**：`brain start --key` 把正文、验收标准、参考写进 `runs/<KEY>/intent.md`，和 run.json 一起提交。本地的 brain-dev 和运行机上的 brain-run 都会先读它；它是数据，不是指令。验收标准带进 requirements.md 时，出处写 `intent.md AC-n`。
- 定时任务怎么配：`examples/cron-intent.md`（KiroCrew 的 cron 和系统 crontab 各一种）。

**claim.json 多了这些字段**（都可以没有，老的 claim 照样能读）：

```json
{"schema": 1, "key": "PAY-7", "owner": "alice", "title": "…", "created_at": "…",
 "source": "alert", "body": "…", "acs": ["…"], "refs": ["…"], "hash": "<16 位十六进制>",
 "gap_kind": "test", "found_in": "PAY-3",
 "closed": {"at": "…", "by": "alice", "reason": "…", "duplicate_of": "PAY-5"}}
```

`hash` 是来源、标题、正文的 sha256 前 16 位，用来去重。`gap_kind`、`found_in` 只有缺口才有。关闭是在 claim 分支上加一个提交（快进），不删分支。

待认领的 claim 和大需求拆出来的小需求还有这些字段（见 2.1、2.4），也都可以没有：

```json
{"schema": 1, "key": "PAY-3", "owner": null, "filed_by": "alice", "title": "web · 在订单页看到退款状态",
 "created_at": "…", "source": "split", "body": "Unit web of PAY-1, …", "acs": ["AC-3: 订单页点\"申请退款\" → 显示退款单状态"],
 "refs": ["aidlc-docs/261109-pay-1-big/inception/application-design/unit-of-work.md", "US-2"], "hash": "<16 位十六进制>",
 "parent": "PAY-1", "unit": "web", "expected_paths": ["web/**"], "depends_on": ["PAY-2"],
 "contracts": ["aidlc-docs/contracts/api/refunds.yaml"],
 "split": {"intent": "261109-pay-1-big", "branch": "ai/alice/PAY-1-big", "commit": "<40 位十六进制>",
           "design_sha256": "<64 位十六进制>", "units_sha256": "<64 位十六进制>"},
 "claimed_at": "2026-11-09T11:43:51Z"}
```

| 字段 | 意思 |
|---|---|
| `owner` | `null`：待认领，还没人认领 |
| `filed_by` | 登记的人。待认领的必须有；小需求是大需求的主人 |
| `claimed_at` | 认领的时间；放回去时去掉 |
| `source` | 小需求是 `split` |
| `parent`、`unit` | 哪个大需求、它 unit-of-work.md 里的哪个单元 |
| `expected_paths` | 单元的 Paths（可以是 `web/**` 这样的通配） |
| `depends_on` | 所依赖单元的小需求号 |
| `contracts` | 单元用到或提供的契约 |
| `split` | 从哪拆出来的：大需求的 Lean intent、run 分支、拆的那个提交、设计和 unit-of-work.md 的 sha256 |

小需求按"大需求 + 单元"去重，不按 `hash`：同一个大需求的同一个单元只有一个没关的小需求。

### 2.1 待认领：登记给别人做

```bash
brain intent add --source debt --title "退款表加索引" --body-file debt.md --unclaimed   # 登记，不认领
brain claim PAY-8               # 认领
brain claim --release PAY-8     # 放回去，又是待认领
```

- 待认领：`"owner": null`，`"filed_by"` 是登记的人。`brain intent list` 里 owner 一栏是 `-`。
- `brain claim KEY`：主人从"没有"变成你。两个人同时认领，先推上去的算；后到的看到 `PAY-8 was just claimed by alice: their claim reached the server first`，再试一次看到 `PAY-8 is claimed by alice`。
- `brain start <slug> --key KEY` 碰到待认领的，顺便认领。
- `brain claim --release KEY`：只有主人能放。你已经有这个需求的 run 分支（本地或 origin）时不让放：先把分支删掉，本地和 origin 都要删（`git branch -D <分支>`、`git push origin --delete <分支>`）。交接了也不行，交接以后分支还在。
- 没人认领时，登记的人可以改、关、删它；小需求还有大需求的主人。有人认领以后，只有主人能。
- `brain split` 登记的小需求都是待认领的（[安装和使用.md](安装和使用.md) 4.7）。
- 老的 claim 没有这些字段，照旧读：老的 claim 都写着主人。

### 2.2 服务器钩子管 claim 分支的规则

| 推送 | 谁可以 |
|---|---|
| 新建，写了主人 | 只有主人本人；写了 `filed_by` 的话也必须是他 |
| 新建，待认领 | 只有 `filed_by` 写的那个人 |
| 新建小需求 | 只能是待认领的，只有大需求的主人能登记（`filed_by` 是他） |
| 认领 | 主人只能从"没有"变成推送的人；别的字段一个都不能变 |
| 放回 | 主人把自己改回"没有"；只改主人 |
| 已认领的 | 只有主人能改、放、删；谁都不能抢过去。小需求的 `split`（它从哪个拆分来）有人认领时不能改 |
| 待认领时改、关、删 | 登记的人，或大需求的主人 |
| 小需求 | 只能关（写原因），不能删 |
| 任何时候 | `key`、`parent`、`unit`、`source`、`filed_by` 永远不变；机器人账号不能动 claim 分支 |

文字字段照旧过内容检查：新建时查全部，之后只查这次改了的。

### 2.3 状态

`brain intent list --json` 里每个 claim 有两个状态字段。`status` 和以前一样：`open`（没开工）、`started`（有 run 分支，或 run.json 在 main 上）、`closed`。新加的 `state`：

| `state` | `brain intent list` 里 | 什么时候 |
|---|---|---|
| `unclaimed` | 待认领 | 没主人，没 run |
| `claimed` | `<人>` 认领了 | 有主人，还没 run 分支 |
| `in_progress` | `<人>` 在做 | 有 run 分支（本地或 origin） |
| `merged` | 已合并 | run.json 到了 main。小需求要两条都满足：它 `brain handoff` 时在 run.json 里记的那一笔（`mr`）到了大需求的 run 分支（或 main）；它的 run 分支还在时，分支的最新提交也在那里面。只有 run.json 不算：从它的分支开工（`--on`）或合过它的兄弟，也带着它的 run.json |
| `closed` | 已关闭 | 关了 |

大需求整体还有一个 `family_state`：`in_progress`（还有小需求没合并）、`ready_to_release`（都合并了，等评审和发布）、`done`（发布了，小需求也都合并了：大需求就算关了，不用再做什么）、`closed`（有人用 `brain intent close` 在大需求的 claim 上记了关闭）。

### 2.4 大需求和小需求

- `brain intent list`：每个还没完成的大需求下面列出它的小需求（状态、依赖和依赖的状态），后面写"已合并 n/m"。关掉的小需求不列。大需求 `done` 或 `closed` 以后，默认不再列；`--all` 都列。
- `brain intent show KEY`：一个大需求和它的小需求；或一个小需求在等谁合并。
- `brain intent close <大需求>`：还有小需求既没合并、也没关时拒绝。大需求 `done` 以后可以用它在 claim 上记一笔，不是必须的。
- 整个流程（拆、认领、开工、同步、合并、发布）见 [安装和使用.md](安装和使用.md) 4.7。

### 2.5 给 KiroCrew app 的 JSON

```bash
brain intent list --json          # 待开工的（和以前一样）+ 每个还没完成的大需求和它的小需求（关掉的小需求不带）
brain intent list --json --all    # 全部 claim
brain intent show KEY --json      # 一个，字段一样
```

输出是一个列表。每一项是 claim.json 原样（`owner` 为 `null` 就是待认领），再加：

| 字段 | 谁有 | 意思 |
|---|---|---|
| `status` | 都有 | `open`、`started`、`closed`，和以前一样 |
| `state` | 都有 | `unclaimed`、`claimed`、`in_progress`、`merged`、`closed`（2.3） |
| `source` | 都有 | 老的 claim 没写的，填 `product` |
| `blocked_by` | 小需求 | 依赖里还没合并的小需求号 |
| `children` | 大需求 | 小需求号，按登记的先后（先登记被依赖的单元） |
| `progress` | 大需求 | `merged`、`total`（没关的小需求数）、`unclaimed`、`claimed`、`in_progress`、`closed`、`text`（`"n/m merged"`） |
| `family_state` | 大需求 | `in_progress`、`ready_to_release`、`done`、`closed` |
| `released` | 大需求 | RELEASE 批过没有（看大需求的 run 分支和 main） |

例子（PAY-2 在做，PAY-3 待认领、等 PAY-2，PAY-4 认领了；省掉了一些字段）：

```json
[
  {"key": "PAY-1", "owner": "alice", "title": "退款大需求", "created_at": "2026-11-09T11:43:44Z", "schema": 1,
   "source": "product", "status": "started", "state": "in_progress",
   "children": ["PAY-2", "PAY-3", "PAY-4"], "family_state": "in_progress", "released": false,
   "progress": {"merged": 0, "total": 3, "unclaimed": 1, "claimed": 1, "in_progress": 1, "closed": 0,
                "text": "0/3 merged"}},
  {"key": "PAY-2", "owner": "alice", "filed_by": "alice", "claimed_at": "2026-11-09T11:43:51Z",
   "title": "api · 通过接口提交退款", "source": "split", "status": "started", "state": "in_progress",
   "parent": "PAY-1", "unit": "api", "expected_paths": ["services/api/**", "aidlc-docs/contracts/api/**"],
   "depends_on": [], "blocked_by": [],
   "contracts": ["aidlc-docs/contracts/api/refunds.yaml"], "split": {"intent": "261109-pay-1-big", "…": "…"}},
  {"key": "PAY-3", "owner": null, "filed_by": "alice", "title": "web · 在订单页看到退款状态",
   "source": "split", "status": "open", "state": "unclaimed",
   "parent": "PAY-1", "unit": "web", "expected_paths": ["web/**"], "depends_on": ["PAY-2"], "blocked_by": ["PAY-2"]},
  {"key": "PAY-4", "owner": "bob", "filed_by": "alice", "title": "jobs · 每晚自动对账",
   "source": "split", "status": "open", "state": "claimed",
   "parent": "PAY-1", "unit": "jobs", "expected_paths": ["jobs/**"], "depends_on": [], "blocked_by": []}
]
```

## 3. 缺口变成下一个需求

run 里发现"缺了什么"，但不该在这个 run 里做，就在 `decisions.md` 里写一个 `brain-gap` 块，和 `brain-proposal` 放在一起：

````
```brain-gap
title: 退款对账没有自动测试
kind: test
refs: AC-2, D-4, src/pay/reconcile.py
---
对账只在验收时手工核对过（D-4）。要一个每天跑的测试：两边金额和笔数一致。
```
````

- `kind`：`knowledge`（仓库和 Brain 答不了、只能问人的）、`test`（验收标准没有测试、晚发现的缺陷、人手工做的检查）、`other`。
- `brain reflect` 预览时列出每个缺口；`brain reflect --apply` 把新的缺口登记成需求（`source: gap`，记在跑 reflect 的人名下），已经登记过的跳过，并在 Brain MR 的描述里列出号码。
- 缺口和提案一样过内容检查；有一个过不了，整批都不写。
- 只有缺口、没有新提案时，只登记缺口，不开 Brain MR。

## 4. 反思者：验收以后换一个新会话回顾

run 自己写的提案往往不全。验收以后，开一个**新的会话**（反思者，agent `brain-reflector`），让它从头读一遍这个 run：

- **读什么**：先试 `brain trace <KEY> --json`；没有这个命令就读原始文件：`decisions.md`、`requirements.md`（验收标准、假设）、`aidlc-state.md`、审计记录（`audit.md` 和 `aidlc-docs/audit/*.jsonl`）、`park.json` 的每一版、答复凭证、Brain。
- **写什么**：只写一个文件 `runs/<KEY>/reflect-proposals.md`（在它自己的工作区里），里面是 `brain-proposal` 和 `brain-gap` 块。被驳回、被纠正的每一处都要有 correction。
- **不做什么**：不改 `brain/`、不改代码、不提交、不推送。
- **然后由人**：`brain reflect --key <KEY> --from <那个文件>` 预览，再加 `--apply`。Brain 照旧只能通过文件负责人审的 MR 改。

在自己电脑上跑：

```bash
brain reflect --draft PAY-3
```

它只打印、不改任何东西：怎么建一个单独的 worktree、在 KiroCrew 里怎么开会话、发给会话的完整消息（带着全部说明），以及最后要跑的 `brain reflect --from` 命令。它不 fetch：只看本机已有的分支（先 `origin/main`、`main`，再 run 分支在 origin 的副本、本地分支）；本机都没有时用 `git ls-remote origin` 查一下（只读），把要先跑的 `git fetch` 写进第 1 步。要最新的，先自己 `git fetch origin`。

运行机上：把 `remote/agents/brain-reflector.json` 装到 `~/.kiro/agents/`（和 brain-run 一样只认用户级）。现在还要人去开这个会话；桥不会在验收后自动开。

## 5. agent 自己定的事，验收时一次审完

- **在权限内、有合理默认做法的事，agent 不停下来问**：直接做，在 `decisions.md` 记一行（`Who` 写 `agent`，`Why` 写理由），或者记成 requirements.md 里的假设 `A-n`。需求变更、危险/对外/不可逆/碰客户数据的事、Lean 审批，照旧要问人。
- **验收时一起审**：运行机发验收问题时，`park.json` 带上 `review_items`，列出所有 `Who = agent` 的决策和所有假设，每项一个号、一句话：

  ```json
  {"schema": 1, "status": "waiting", "kind": "acceptance", "options": [], "lean_request": "RELEASE <hash>",
   "question": "请验收 AC-1..AC-3",
   "review_items": [{"id": "D-7", "text": "退款按自然日计（需求没写）"}, {"id": "A-3", "text": "旧订单不显示退款按钮"}]}
  ```

  只能用在验收问题上（approve/reject，不带选项）；号只能是 `D-n` 或 `A-n`，不重复；每句一行，最多 200 字；最多 50 项。
- **MR 上**：桥把它们贴成清单（文字放在行内代码里，@ 和快捷命令都不生效）。产品回一行：
  - `answer D-5 approve`：全部通过（包括这些待审项）。
  - `answer D-5 reject A-3,D-7 说明`：只驳回列出的几项，其余通过。号之间用逗号、顿号或空格都行，大小写都认；列了不存在的号，这条回复不算。
  - `answer D-5 reject 说明`：不列号，就是整个驳回，和以前一样。
  - 两个人驳回的项不一样，算回答冲突，都不送，请商量后由一个人重答。
- **送回 run**：驳回的项作为纠正送回去：run 要改掉、在 decisions.md 记一行、每一项写一个 `type: correction` 的提案（`src` 写项号和凭证号），再重新请求验收。给 Lean 的那行口令仍然是 `reject RELEASE <hash>`。
- **留痕**：答复凭证里多了 `review_items`（这次审了哪些）和 `rejected_items`（驳回了哪些）。`brain reflect` 对每一个驳回的项检查有没有 correction 引用它的号，没有就提醒。

## 6. 还没验证

- 真实的 GitLab 服务器钩子环境里，带中文正文的 claim.json（按 UTF-8 读，和服务器的 locale 无关，但没在真服务器上跑过）。
- 待认领和认领的规则（2.2）只在本机按钩子的代码测过，没在真 GitLab 上跑过。
- KiroCrew cron 的 `--command` 沙箱是否允许 `git pull` 和推 claim 分支。
- `brain-reflector` 在运行机上能不能装上、resources 能不能读到；`brain trace` 由另一项工作提供。
- 桥在验收以后自动开反思者会话：还没做。
