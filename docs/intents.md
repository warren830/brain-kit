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
brain intent list                 # 待开工：已登记、还没有 run 分支、没关
brain intent list --all           # 全部，带状态 open / started / closed
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
  - 文档收料打码的范围：按格式认出来的（身份证号、手机号、银行卡号、邮箱、私钥和令牌格式、金额、公司名、合同号）在整个材料包里出现在哪都打码；按标签认出来的人名和密码只打码认出来的那一处，不往别处扩（一处 `提出人：顾客` 不会把整个包里的"顾客"都换掉；名字在别处没标签地出现，就不打码，这是为了不误伤普通的词）。
- **记在谁名下**：登记的人（`GITLAB_TOKEN` 或 `git config brain.user`）。钩子要求 claim.json 里的人就是推送的人，机器人账号不能登记。开工的人也必须是这个人。
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
- KiroCrew cron 的 `--command` 沙箱是否允许 `git pull` 和推 claim 分支。
- `brain-reflector` 在运行机上能不能装上、resources 能不能读到；`brain trace` 由另一项工作提供。
- 桥在验收以后自动开反思者会话：还没做。
