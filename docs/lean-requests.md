# 给 AI-DLC Lean 的请求

2026-11-09 · 来自 brain-kit 的「大需求拆成小需求」（`brain split`）· 对应 Lean 0.3（`vendor/aidlc-lean`，705c0dc）

brain-kit 不改 Lean，只用它的命令、文件和配置。下面是这次做拆分时碰到的、Lean 现在没有的东西。每条都写了 brain-kit 现在怎么绕（都是拿不准就拒绝），和希望 Lean 怎么做。

## R1. DESIGN 的审批不管拆分（unit-of-work.md）

- **现在**：`approve DESIGN <哈希>` 只绑 `application-design.md` 的 sha256；INCEPTION 绑需求和设计两页。`unit-of-work.md`（Units Generation）没有任何审批绑着，批完以后改了，Lean 不会发现。可这一页正是"拆成哪几块、谁依赖谁"，是人要拍板的地方。
- **brain-kit 的绕法**（`split.approved_split`）：只有同时满足这几条才拆，否则拒绝、说明原因：
  - 需求和设计都批过，而且批完没改过；
  - 最后一条 `lean stage done units` 记下的 `unit-of-work.md` 哈希就是现在的文件；
  - 这份内容是在**当前那次 DESIGN 批准之前**记下的（人批设计时看到的就是这份拆分）。之后才记的，要人再批一次 DESIGN（设计没改、再批一次也行）。
- **请求**：DESIGN 和 INCEPTION 的哈希把 `unit-of-work.md` 一起算进去（文件存在、不是模板时），就像 INCEPTION 一个哈希管两页；或者加一种 UNITS 审批。

## R2. Lean 不读 unit-of-work.md 的依赖、契约、故事三列

- **现在**：`Project.units()` 只读第一列（单元名）、Paths 和 Owner team。Depends on、Contracts、Stories / ACs 是"给人看的"，Lean 不用。
- **brain-kit 的绕法**：按 Lean 读表的同一套规则（第一张表、HTML 注释不算、`|` 分格、Paths 按 `split_paths` 拆）自己读这三列；依赖指向不存在的单元、自己依赖自己、循环依赖，都拒绝拆分。
- **请求**：
  - 一个机器能读的输出，比如 `lean units --json`：每个单元的 name、owner、paths、depends_on、contracts、stories，外加 Lean 忽略掉的名字；
  - `lean status` 的"下一步"按 Depends on 提示（"unit web 等 api 先做完"）。

## R3. 没有机器能读的状态

- **现在**：要知道"需求和设计批了没、批完改没改、发布了没"，只能读 `aidlc-docs/audit/*.jsonl` 和页面，自己按 Lean 的规则算（brain-kit 的 mrdesc、trace、split 都这么做）。
- **请求**：`lean status --json`：每一页的审批状态（draft / requested / approved / stale / rejected）和哈希、阶段记录、是否已发布、单元列表。外部工具就不用复制 Lean 的规则了。

## R4. 一个 clone 同时干两条线，账本会分叉

- **现在**：每个 clone 一个账本文件（`aidlc-docs/audit/<clone>.jsonl`，带哈希链）。同一个 clone 在两个分支上各记了几条，两个分支合并时这个文件冲突，哈希链也接不上。Lean 的设计默认"一个 clone 一条线"，但文档没写明。
- **brain-kit 的绕法**：每个小需求开一个自己的 git worktree（`~/.brain/worktrees/<仓库>/<KEY>`）。`.aidlc-lean/` 在 worktree 里，所以每个小需求有自己的 clone id 和账本文件，并行的小需求合并时账本从不冲突。`brain sync` 碰到账本冲突就停下，不自动合。
- **请求**：README 写明"一个 clone（或 worktree）只干一条线"；或者账本按分支分文件。

## R5. 并行的单元合并时，aidlc-state.md 一定冲突

- **现在**：两个单元从同一处分出去，各自跑阶段命令，`aidlc-state.md` 里生成的那段（Stage log 在末尾追加）合并时必冲突。Lean 的文档说"跑一次 `lean verify` 重写，Session notes 手工合并"。GitLab 上点合并解决不了这种冲突，每个并行的小需求都要人手工处理一次。
- **brain-kit 的绕法**：`brain sync` 把大需求的分支合进来；只在 Lean 自己的文件上冲突时自动解决：`aidlc-state.md` 头和生成段取自己这边，Session notes 按 `###` 小节三方合并（每个小需求只改自己那节 `### Next (team <单元>)`），再跑 `lean status` 按合并后的账本重写生成段；`build-and-test-summary.md` 取自己这边（下次 `lean verify` 重写）。别的冲突留给人。
- **请求**：一个 git 合并驱动（比如 `lean merge-state %O %A %B`，`.gitattributes` 里配）；或者把 Stage log 放进单独的生成文件，`aidlc-state.md` 只留进度列表和 Session notes。

## R6. 团队是每个 clone 一个，名字里的 `-` 会变成 `_`

- **现在**：
  - `lean init --team X` 写进这个 clone 的 `.aidlc-lean/local.json`，环境变量 `AIDLC_LEAN_TEAM` 会盖掉它；
  - 存下的团队名把字母、数字、`_` 以外的字符都换成 `_`（`billing-worker` 变成 `billing_worker`）；
  - 可是 `next_step` 拿 unit-of-work.md 里 Owner team 那一格的**原文**去比（`owner not in ("", "-", mine)`，不经 `team_key`）。单元名或团队名带 `-` 时，多团队模式下 Owner team 那格要写成 `billing_worker`，Lean 才认得是自己的单元。
- **brain-kit 的绕法**：小需求的 Lean 团队就是单元名（按 Lean 的规则换成 `_`），在小需求自己的 worktree 里 `lean init --team`；设了 `AIDLC_LEAN_TEAM` 而且和单元对不上时，`brain start` 拒绝。多团队模式下，`brain split` 对 Owner team 和单元的团队名对不上的单元打 WARN。
- **请求**：`next_step` 比较 Owner team 时也用 `team_key`。
- **Lean 0.3.1 已经做了**（e8d47c7）：团队名照原样存，比较都经 `team_key`，`web-ui` 和 `web_ui` 是同一个团队。`brain split` 的这两条 WARN 跟着装的 Lean 走：0.3.1 起按 `team_key` 比，Owner team 写单元名（`web-ui`）不再报；装的还是 0.3.0 时照旧按原文比。

## R7. 大需求可能先发布，把还在做的小需求关在外面

- **现在**：`lean release` 和 RELEASE 审批不看"还有单元的代码生成没做完"（只在阶段进度里显示 `[-] verify PASS on partial code`）。标准模式下，大需求自己的 `lean status` 还会让它去做所有单元。RELEASE 批了以后 intent 就关了，没合并的小需求再也记不了阶段。
- **brain-kit 的绕法**：大需求还有小需求没合并时，`brain handoff` 拒绝交接它；steering 和 brain-dev 的说明告诉 AI：大需求的分支上不做单元，等 `brain intent show <大需求>` 显示都合并了再评审、发布。这是护栏，不是硬门。
- **请求**：一个可选的关卡：所有单元的 code-generation 都 STAGE_COMPLETED 才能 `lean release`（比如 `lean.json` 里 `"release_needs_all_units": true`）。

## R8. 用户故事页没有审批绑着

- **现在**：REQUIREMENTS 和 INCEPTION 只管 `requirements.md`（和设计页），`stories.md` 改了 Lean 不管。
- **brain-kit 的做法**：小需求的验收标准原文从 `requirements.md`（批过的）抄；标题和故事描述从 `stories.md` 抄，这部分没有审批绑着。抄过来的每一条都过 Brain 的内容检查，过不了的只留编号（`AC-3`）。
- **请求**：REQUIREMENTS / INCEPTION 的哈希也算上 `stories.md`（存在时）。
