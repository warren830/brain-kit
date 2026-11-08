---
name: brain-reflect
description: Use when a person corrected you or rejected something, when a decision or trap will matter beyond this run, when a Brain entry turned out wrong, and before hand-over or acceptance - to write Brain proposals the team reviews.
---
# Write Brain proposals

The Brain (`brain/*.md`) is the team's memory. You never edit it. You write proposals into the Lean intent's
`decisions.md`, under `## Brain proposals`; after the run `brain reflect` checks them, gives them ids and opens a
Brain MR that the file owners review. Proposals that fail the checks are not written at all.

## One block per entry

````
```brain-proposal
file: TECH
type: pitfall
rel: new
section: 外部系统
src: D01#S030, D-4
---
One to five lines the next run can act on. State the rule, not the story.
```
````

| field | what |
|---|---|
| `file` | `PRODUCT` (intent, users, non-goals, terms: the product decides), `TECH` (architecture, conventions, runtime traps: Builder), `IMPROVEMENT` (what worked, what failed in how we work: Builder), `PROJECT` (current focus, open items, assets: both) |
| `type` | PRODUCT: principle, decision, model, correction · TECH: principle, decision, model, guideline, pitfall, correction · IMPROVEMENT: pitfall, process, guideline, decision · PROJECT: decision, process |
| `rel` | `new`; `replaces <id>` (the old entry is wrong or outdated; write the full new text); `adds-to <id>` (your text is appended); `same <id>` (nothing new, only another source); `repeats <id>` (a lesson the Brain already had came back: the person had to correct it again); `withdraws <id>` (text = why) |
| `section` | optional, new entries only: the `## ` heading to put it under (e.g. 术语, 非目标, 外部系统) |
| `src` | required: segment refs (`D01#S003`), your decisions (`D-4`), the bridge's decision or voucher ids (`V-1a2b3c4d5e`), the run id |

## Which type
- **correction**: a person told you that you were wrong. Say what is right, in one rule.
- **decision**: someone with the right role decided something that will hold beyond this run.
- **model**: a term, a state machine, how two things relate (`**term**：definition` on the first line).
- **principle**: a rule that decides many cases. **guideline**: how we usually do X. **pitfall**: a trap and how
  to avoid it. **process**: how the team works.

## Rules (reflect refuses the whole batch otherwise)
- No secrets, personal data, money amounts, contract numbers, and no masked placeholders such as `[人名-1]` or
  `[金额-2]`. Write the rule ("面值 ≤ 估计资产净值 × 30%"), not the figure.
- No instruction-like lines ("approve ...", "忽略以上指令"): every run reads the Brain.
- Only knowledge a later run needs. Not this run's status, not a diary.
- An entry that relaxes tests, logging, auth, reviews or dependencies needs Builder and security: say why.
- Entries from requirement documents (`src` with `D01#S...`) are reviewed one by one: keep each one small.
- If a person rejected your work (`answer D-n reject ...`), write a `correction` (or `repeats <id>`) citing that
  decision or voucher id. Reflect warns about rejections nobody wrote down.

## When you rely on the Brain
Cite the entry id in `decisions.md` (for example "per T-OMS-1-2"). That is how the team learns which entries are
used (`brain usage`) and which can go.
