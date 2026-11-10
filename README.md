# brain-kit

每个人在自己电脑上开发需求，开发到一半，交给团队运行机（一台 KiroCrew）接着跑完后半程：评审、修 CI、出证据、交付。两边读同一份 Brain。需要人拍板时，运行机在 MR 上提问，人回一行字就行。

- **中间的开发**直接用 AI-DLC Lean v0.3（另一个仓库，子模块 `vendor/aidlc-lean`）。brain-kit 只调用 Lean 的命令、读它的记录，不改 Lean 本身。
- **不接 Jira 也能用**（`tracker: none`）：需求号由 git 认领分支分配，问答走 GitLab MR 评论。要接 Jira 时，改成 `tracker: jira`。
- **需求可以直接交文档**：飞书 / Lark 导出的文件夹、md、docx、xlsx、pptx、html、txt 都行；PDF 需要装 MarkItDown。文档先过五道关，打码后打成材料包。原件不进任何仓库。
- 只用 Python 标准库（3.8 以上）、git 和 `ssh-keygen`。签名用 Ed25519（`ssh-keygen -Y`）。
- **不监听任何端口**：所有组件都只往外连。KiroCrew 客户端只接受本机回环地址。

怎么装、怎么用（含 AI-DLC Lean）：[docs/安装和使用.md](docs/安装和使用.md)

## 一个需求怎么走（不接 Jira）

1. **认领并开工**：
   ```bash
   brain start refund --doc "<需求文件夹>" --classification internal --consent --business mask
   ```
   这一条命令依次做四件事：
   - 认领下一个空闲的需求号，比如 `PAY-3`。实际是推一个 `claim/PAY-3` 分支，两个人同时抢同一个号时，只有一个人能推成功。
   - 让文档过五道关，生成材料包 `PKG-PAY-3-1`，只读放到 `.brain-materials/`（这个目录已加入 gitignore）。
   - 开分支 `ai/<你>/PAY-3-refund`，用 Lean 建 intent。
   - 写 `runs/PAY-3/run.json`，记下 Brain commit 和材料包的哈希。
2. **本地开发**：用 `brain-dev` agent，走 Lean 的阶段和关卡。agent 先读材料包的 `toc.md`，再按需读具体的段；每条验收标准都标出来源段号。
3. **交接**：运行 `brain handoff`。它会：
   - 检查交接单（Lean 的 `aidlc-state.md`）填好了没有；
   - 把材料包推到 brain-intake；
   - 推送分支，给 MR 打上 `ai-handoff` 标签。
4. **桥核对后接手**：
   - 核对 8 项：分支名格式、打标签的人就是分支主人、标签比最后一次推送晚（按 GitLab 自己记的推送时间，不看 commit 上写的日期；`brain handoff` 会等 GitLab 记下推送再打标签）、认领是他本人的、run.json 有效、交接单已填、Brain commit 存在、材料包的哈希对得上。另外，同一个需求不能有两个 run 同时在跑。
   - 核对通过后，桥签接手合同，runnerd 按合同里的 commit 建工作区，并把材料包只读拷进去；桥再把 run 送进 KiroCrew。
   - 预计改动的模块和别的在跑需求重叠时，桥在 MR 上提醒。
5. **找人**：
   - 运行机需要人时，写 `runs/PAY-3/park.json` 并停下。桥把问题贴到 MR 上，编号 `D-<n>`，同时 @ 能回答的人。
   - 人回一行：`answer D-5 A`。如果是 Lean 的审批，回 `answer D-5 approve`。
   - 桥做 5 项核对，通过后签答复凭证，把回答送回 run。如果是 Lean 的审批，桥送回的是 Lean 认的那一行审批口令。
   - 问题的第一行写着提交号和 park 指纹；回答只认：问题评论没被改过、问题是在那次推送之后贴的、一个 D-n 只对应一个问题。否则这个问题作废，重问。
6. **验收**：运行机执行 `lean release` 和 `lean request RELEASE`，再以"验收"类问题停下；产品在 MR 上回 approve。合并仍然要过 GitLab 审批和 CI。

**大需求拆成小需求，大家一起做**：大需求照常走 Lean 的 Inception，在 `unit-of-work.md` 里拆成几个单元，人批 DESIGN 就是批了拆法。
- `brain split PAY-1` 把每个单元登记成一个待认领的小需求（带着单元的 Paths、依赖、契约、验收标准）。
- 大家 `brain claim PAY-2` 认领，`brain start --key PAY-2` 开工：从大需求的分支开自己的分支，放在单独的 worktree 里（一个 Lean clone，Lean 团队就是单元）。依赖没合并前不让开工。
- `brain sync` 把大需求的分支合进来，自动解决 Lean 进度页的冲突；`brain handoff` 把 MR 开到大需求的分支，由大需求的负责人审、合并（小需求不交给运行机）。
- 小需求都合并了，大需求再评审、发布。`brain intent list` 看进度（已合并 n/m）。详见 [docs/安装和使用.md](docs/安装和使用.md) 4.7；Lean 缺的几样和怎么绕见 [docs/lean-requests.md](docs/lean-requests.md)。

## Brain：团队共用的知识

Brain 是仓库里的 `brain/PRODUCT.md`、`TECH.md`、`IMPROVEMENT.md`、`PROJECT.md` 四个文件，加上仓库根目录的 `AGENTS.md`。本地 run 和运行机上的 run 开工前都先读它们。

| 文件 | 谁批 | 放什么 | 条目类型 |
|---|---|---|---|
| PRODUCT | 产品 | 意图、用户、非目标、术语 | principle、decision、model、correction |
| TECH | Builder | 架构、约定、运行时的坑 | principle、decision、model、guideline、pitfall、correction |
| IMPROVEMENT | Builder | 做成的、失败的、踩过的坑 | pitfall、process、guideline、decision |
| PROJECT | 两边各一人 | 当前重点、决策、待办、资产 | decision、process |

**条目**：每条前面一行条目头，正文到下一个条目头、下一个标题或文件末尾为止。
```
<!-- id:T-PAY-1-2 type:pitfall side:builder rel:new src:D01#S030,V-1a2b3c4d5e -->
退款单号以产品确认为准：PRD 写 RF + 9 位，样例文件是 7 位。
```
- 条目号是 `<文件字母>-<需求号>-<n>`（P、T、I、J）。不同需求各用各的号段，几个人同时写也不会撞号。
- `src` 写出处：材料段号、决策号、凭证号、run 号。

**一条知识怎么进 Brain**
1. run 里 AI 不能改 `brain/`（CI 的 check-run 会拦）。它把要记下的东西写成 `brain-proposal` 块，放在 intent 的 `decisions.md` 里（skill `brain-reflect` 讲了格式）。被人驳回、纠正过的地方必须写。
2. run 结束后（或合并以后加 `--ref origin/main`），需求负责人运行：
   ```bash
   brain reflect                 # 预览：每条提案怎么处理，哪条过不了
   brain reflect --apply         # 在 main 上开 brain/PAY-1-reflect-1 分支写入并提交，不动当前工作区
   brain reflect --apply --push  # 同上，再推送并开 MR（标签 brain-reflect）
   ```
   - 条目号由 reflect 分配，会跳过 main 和其他 `brain/*` 分支上已用的号。
   - 同一条提案只写一次：再跑一遍，已写入的会跳过。
   - 正文和已有条目一样时，只给旧条目补出处。只有补出处、没有新内容时，不开 MR。
   - 有驳回过的决策、却没有对应的 correction 提案时，会提醒。
3. MR 上由文件负责人审：来自文档的条目单条审；放宽测试、日志、鉴权、依赖要求的条目，要 Builder 加安全两个人批。

**条目只能这样变**（`brain lint` 按 diff 检查，CI 的 `brain-lint` 作业也一样）

| 关系 | 怎么做 | 规则 |
|---|---|---|
| new | `brain entry add TECH pitfall --key PAY-1 --src D01#S030 --text "..."` | 新条目号 |
| same | `brain entry same T-PAY-1-2 --src V-...` | 正文不变，只补出处 |
| adds-to | `brain entry adds-to T-PAY-1-2 --src ... --text "..."` | 原文不动，追加在后面 |
| replaces | `brain entry replace T-PAY-1-2 --key PAY-5 --src ... --text "..."` | 新条目占原位置，旧条目搬进 `TECH-archive.md` |
| repeats | `brain entry repeat T-PAY-1-2 --src V-...` | 计数加 1；到 2 次提示"改成 gate"（写成会失败的检查） |
| withdraws | `brain entry withdraw T-PAY-1-2 --reason "..." --src ...` | 搬进 archive，写明原因 |
| 清除 | `brain entry purge T-PAY-1-2 --reason pii --approved-by <安全负责人>` | 只用于个人信息、密钥、误收的机密。从主文件和 archive 删掉，号记进 `PURGED.md`。git 历史要另外改写 |

- 除此之外的改动都过不了：手改正文、删条目、改 archive、出处变少、计数变小、改类型。archive 和 `PURGED.md` 只增不减。
- 新写进去的文字会检查：密钥、个人信息、带币种的金额、合同号、打码占位符（`[人名-1]` 这类）、像指令的行。命中就拒。
- 只提醒、不拦：来自文档（单条审）、放宽类（两人批）、同一个术语定义了两次、老式顺序号。

**Brain 有没有被用到**：`brain usage`（合并后加 `--ref origin/main`）
- run 在 Lean 记录里写了条目号，才算用到；提案块里出现的号不算。
- 每个 run 装载了多少条（按 run.json 的 brain_commit）、引用了哪些。
- 知识有用率 = 被引用过的条目 ÷ 现有条目；重复纠正率 = correction 的重复次数 ÷ 全部纠正。
- 至少 3 个 run 装载过、却从没被引用的条目，列为撤回候选；还会列出引用了已归档条目（用了过时知识）和不存在条目号的 run。

## 组成

| 命令 | 谁跑、在哪 | 做什么 |
|---|---|---|
| `brain` | 每个人的电脑 | `install`、`doctor`、`claim`、`intake`、`start`、`handoff`、`status`；大需求拆成小需求：`split`、`claim KEY`（认领待认领的）、`sync`（见 [docs/安装和使用.md](docs/安装和使用.md) 4.7）；Brain：`lint`、`entry`、`reflect`（含 `--draft`）、`usage`；入口：`intent add/list/show/close`（见 [docs/intents.md](docs/intents.md)）；评估：`trace`、`eval`、`eval gaps`、`golden`（见 [docs/eval.md](docs/eval.md)） |
| `brain-bridge` | 团队运行机，`brainbridge` 用户 | `poll`（接手核对）、`dispatch`（送进 KiroCrew）、`watch`（贴问题）、`collect`（收回答并送回）、`reconcile`（MR 合并后收尾）、`report`（统计）、`loop`（循环跑以上全部） |
| `brain-runnerd` | 团队运行机，`brainbot` 用户 | 验合同签名，按合同里的 commit 建 worktree，拷材料包 |
| `brain-ci check-run` | GitLab MR 流水线（只在 MR 流水线里跑） | 检查五件事：run.json 和分支一致；材料包没被提交进仓库；run 分支没改 `brain/` 和 `AGENTS.md`；改了 risk-paths 必须有桥签名的设计批准；机器人提交的 commit 要有接手凭证。kit 配置、risk-paths 和 diff 基线都从**目标分支**读，MR 分支上改了也不算数 |
| `brain-ci brain-lint` | GitLab MR 流水线（每个 MR） | Brain 的关系规则，对照 MR 的 diff 基线；配置从目标分支读 |
| `brain-pre-receive` | GitLab 服务器钩子 | 人只能推 `ai/<自己>/*`；`claim/<KEY>` 只有 claim.json 里写的那个人能建、能改、能删（待认领的另有规则，见 [docs/intents.md](docs/intents.md) 2.2）；机器人只能推 `ai/*`，不能删分支、不能强推、不能推 tag，也不能改 gates、CI、`.brain/` |
| `remote/agents/brain-run.json` + `remote/prompts/brain-run.md` | 团队运行机上的 KiroCrew | 接手模式的 agent |

## 文档的五道关（`brain intake` / `brain start --doc`）

| 关 | 怎么判 | 不通过怎么办 |
|---|---|---|
| 1 交件权 | 交件人必须加 `--consent`，表示自己是作者，或已取得作者同意 | 不收 |
| 2 密级 | 用交件人选的 `--classification`，但文件里如果有更高的密级标记（机密、Confidential、MSIP 标签等），以文件为准。选"不确定"按机密处理 | 机密：`intake.allow_confidential`（决策 02）放行才收，否则写脱敏摘要，或用 `--exclude` 去掉这份。绝密：不收 |
| 3 商业敏感 | 认三类：金额（必须带币种或"元"字，如"5 万元""人民币 300 万"；只写"10万""2亿"算计数，3M、12M 这类期限也不算）、公司名（按"有限公司、銀行"等后缀认）、合同号 | 列出来，交件人用 `--business keep` 原样保留，或 `--business mask` 换成占位符 |
| 4 个人信息 | 香港和内地身份证（都核对校验位）、手机号、银行卡（Luhn 校验）、邮箱、@人名、"修訂人 / 負責人 / Author"这类列里的人名 | 一律换成占位符；同一个值在所有文件和文件名里按整词都换掉（标签认出的人名、密码也一样；名字列里没有姓氏的值如 风控、Billing 只列出来请你确认；admin、password 这类普通词的密码只换认出的那一处）。单个文件超过 30 处时，整份不收 |
| 5 密钥 | 内置规则；装了 gitleaks 时一并用上 | 普通密钥打码；私钥和云访问密钥整份不收 |

- **看不见的内容**移到 `extras/hidden.md`，不进正文：隐藏文字、白字和极小的字、批注、隐藏的工作表 / 行 / 列、`;;;` 格式隐藏的单元格、`display:none`、HTML 注释、隐藏的幻灯片。
- **像指令的行**（approve、批准、"忽略以上指令"等）在前面加 `> `，变成引用。
- **不收的格式**：老 Office 格式、加密文件、带宏的文件、压缩包、单独的图片。
- **加 `--dry-run`** 只看检查结果，什么都不写；所有过不了的关一次列全。

## 安装

**项目（一次）**
```bash
brain-kit/bin/brain install <项目目录> --lean <AI-DLC Lean 的 checkout 目录> --prefix PAY
# 检查 .brain/kit.json 里的 tracker、req_prefix、gitlab、bot_users、pins、decision_roles，然后提交
```

**每个人的电脑**
- 设 `git config brain.user <GitLab 用户名>`，或者设 `GITLAB_TOKEN`。后者更好，身份由 GitLab 确认。
- 材料包仓库：`git config brain.intake <brain-intake 的本地 clone>`。
- 开工前跑 `brain doctor`。

**团队运行机**
- 系统用户：`brainbot`（跑 KiroCrew 和 runnerd）、`brainbridge`（跑桥）、共用组 `brainspool`。
- 配置样例：`examples/bridge.json`、`runnerd.json`、`identities.json`。`identities.json` 里的 `roles` 规定谁能回答哪类决策。
- 服务：`deploy/*.service`。
- 签名私钥只放在 `brainbridge` 那边，公钥写进 `deploy/allowed_signers`。签名可以换成单独的签名进程（`bridge.json` 里 `"signer": {"type": "spool", ...}`），桥自己不拿私钥；KiroCrew app 用的就是这种方式。
- `.brain/kit.json` 里写上 `bridge_user`（桥在 GitLab 上的账号，不能是 bot 账号），用来分清哪些评论是桥贴的。
- KiroCrew 里：建一个最小 app `brain-bridge`，把它的 `.app_secret` 给 `brainbridge`，权限 0600；再装上 `remote/agents/brain-run.json`。

**GitLab**
- 服务器钩子：`bin/brain-pre-receive`，样例配置在 `examples/pre-receive.json`。
- CI：引入 `ci/brain-kit.gitlab-ci.yml`，把 `brain-run-check` 和 `brain-lint` 设成必过检查。
- risk-paths 写在 main 的 `gates/risk-paths` 里。

## 谁能回答哪类问题

- 由 main 上的 `decision_roles` 和 brain-authz 里的 `roles` 决定，不看 run 在 park.json 里怎么写。
- Lean 的审批按审批类型认人：IDEATION、REQUIREMENTS 和 RELEASE 归产品，DESIGN 和 PLAN 归 Builder。
- 不接受合在一起的 INCEPTION 审批，需求和设计要分开批。

## 哪些算真门，哪些只是护栏

- **真门**（机器人改不了）：pre-receive 钩子、CI 的必过检查、桥的核对和签名、GitLab 的 MR 审批和 CODEOWNERS。
  - pre-receive 对机器人推送的规则：每个 commit 的作者和提交人都必须是机器人账号；merge 要和 main 那一侧的父提交比较，用 `-s ours` 悄悄还原 gates 的改动会被拦下；文件名按 NUL 分隔读取，带引号或中文的路径也认得出。
- **护栏**：Lean 的 hook，以及 agent 的说明文字。

## 测过什么

本机 994 个测试，在 python3.9（pytest）和 python3.12（`python3.12 -W error -m unittest discover -s tests -t .`）上都加 `-W error` 跑：除了 2 个跳过的（真实需求文档的测试，要 `BRAIN_TEST_REAL_DOCS` 指向一个文件夹），都要通过。加了或删了测试，就改这个数（`tests/test_r1_k2_cli.py` 的 R1-34 核对它）。做过三轮独立的对抗评审：流程部分找出 15 个问题，Brain 部分（条目工具、lint、reflect、usage）又找出 15 个，大需求拆分找出 16 个，都已修掉，每个问题都补了回归测试。

- **认领**：本地和远端都测了；两个 clone 抢同一个号，git 本身就会拒掉后到的那个；钩子按 claim.json 认人。
- **大需求拆成小需求**（`tests/test_split.py`、`tests/test_split_trace.py`）：用真的 Lean、一个 bare origin 和两个 clone 端到端跑（两人只靠登录名区分，没有 GitLab）：拆、认领、被拒的认领、被拒的开工、合并后开工、`brain sync` 解决 Lean 进度页的冲突、进度 n/m、大需求发布和关闭、重拆、`--on`、多团队模式下两个小需求之间走 Lean 的 CR；服务器钩子的每条认领规则；两个人抢同一个待认领；trace、eval、usage 的汇总。
- **文档接收**：
  - 自造的 docx 和 xlsx 里各种隐藏内容都识别出来了，HTML 的隐藏元素和脚本也去掉了；
  - 五道关和 dry-run 都测了；
  - 材料包被改动能查出来（旧版 brain-kit 做的材料包没有记 README.md、toc.md、findings.md、extras/ 的哈希，这几个被改查不出来，会提醒核对得没有新版严）；
  - 用一份真实的需求文件夹跑过：打码过的值，在材料包的任何文件里都找不到原值（设 `BRAIN_TEST_REAL_DOCS` 可以重跑）。
- **桥**：
  - 接手核对的每种拒绝情况：Jira 模式和不接 Jira 模式都有；
  - 材料包没发布就拒绝，对得上时 runnerd 只读拷进工作区；
  - 重叠提醒；
  - 决策循环：交接绑定打标签时的那次推送；回答绑定问题和推送（问题被改、问题早于推送、D-n 被重复使用都不认）；CI 认的设计批准必须对得上现在的设计文件；读 MR 评论超过页数上限时报错而不是悄悄停下；问题文字不会触发 GitLab 快捷命令；答错人、答错题号、选项不存在都不算；同一个问题只送一次；回答冲突时都不送；分支有新推送后问题作废；答复人只认 main 上配置的角色；送回的审批口令能被 Lean 自己的 `APPROVE_RE` 识别。
- **Brain**：
  - 条目工具的每种改法，以及改完之后 lint 是否放行；
  - 手改正文、删条目、改 archive、出处变少、计数变小、改类型、已归档的号复活，都会被拦；
  - 条目外的文字、标题、撤回原因、条目头里的属性、AGENTS.md 新加的行，都查密钥、个人信息、金额、占位符和指令行；
  - 多出来的文件、大小写改名、符号链接、非 UTF-8，都会被拦；清除必须由 `purge_approvers` 里的人批；
  - reflect：预览和真写的结果一致；同一提案不写两次（包括还没合并的 brain/* 分支，和已清除的条目）；并行分支不撞号；失败时不留分支和工作区；
  - usage：提案块里的号不算引用；后来才归档的条目，当时引用了照样算用到；带中文的路径也认得。
- **pre-receive、CI、签名、runnerd**：见各自的测试文件。

## 还没验证（要在真环境里测）

- 运行机这一半（V1）：2026-11-08 在一个内部 GitLab 上实测了 GitLab 和 runnerd 这一侧：交接核对、签合同、建工作区、MR 上的问答、Lean 审批经桥送回后被 Lean 记下（审计里能追到凭证）。KiroCrew 换成了只记录的替身：真开会话、会话读接手消息、答完后接着跑，等 KiroCrew app 做好再测。实测找出的 3 个问题（身份表空条目、读超时让桥退出、hook 适配脚本没进 git）已修，见 `tests/test_live_fixes.py`。
- KiroCrew 的 app token 只能调 manifest 里声明的接口；声明 `/api/chat` 后能看到本机所有聊天会话的列表（实测）。
- Lean 的 hook 在 KiroCrew 里：本地 kas 引擎已实测生效（见下一节）；子代理有没有带上 hook 还没测。
- Brain 闭环在真 run 里走一遍：run 写提案 → reflect → Brain MR → 下一个 run 引用。
- run 中途合并了新的 Brain 时，run.json 的 `brain_commit` 不会跟着更新，`brain usage` 仍按旧的那版算"装载"。
- 真实的 GitLab：服务器钩子环境、CI 镜像（标签事件、MR 评论、文件 API 已实测）。
- 用真人跑完整条接力流程（V14）。

## 在 KiroCrew 里本地开发

- **KiroCrew 不读项目里的 `.kiro/hooks/`**，所以 Lean 自带的 hook 在 KiroCrew 里不会跑：v2（kiro）和 v3（kas）两个引擎都一样，2026-11-08 实测确认。
- KiroCrew 只跑写在 **agent 定义 `hooks` 字段**里的 hook；用 v3 时，只读 `~/.kiro/agents/` 下的定义。
- 所以 brain-kit 做了两件事：
  - 提供 `.kiro/brain/lean-hook.py`：把 KiroCrew 传来的会话号补成 Lean 认的字段名，再交给 Lean 原有的 hook 判断，Lean 本身不改；
  - 提供 `brain crew-agent` 命令：安装 `~/.kiro/agents/brain-dev.json`，里面写好了 Lean 的三个 hook（会话开始、发消息、工具调用前）。
- 引擎请用 v3：`kirocrew config set agent.acp_backend kas`。
- 在 KiroCrew 里新建会话，工作目录选项目，agent 选 brain-dev。
- 实测结果（一次真实需求的试跑）：会话开始、真人回合都记下来了（`turn.json` 也生成了），需求没批之前往 `apps/` 写文件被拦下。
- `brain doctor` 会检查这几项：brain-dev 有没有带上 hook、适配脚本在不在、引擎是不是 kas。
- 这个 brain-dev 只能在装了 brain-kit 的项目里用。在别的项目里，hook 找不到适配脚本，工具调用会全部被拦下（故意这样设计，宁可拦错不可放过）。
