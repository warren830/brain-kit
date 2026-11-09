---
name: brain-split
description: Use when a big requirement is to be built by several people in parallel (writing unit-of-work.md for `brain split`), when run.json has `parent` and `unit` (a child run of a split), and before the parent's review and release.
---
# Split a big requirement into small ones

A big requirement (the parent) runs Lean's Inception as usual. Its units in `unit-of-work.md` become small
requirements (its children) that different people claim and build in parallel, each in its own worktree. The
person's DESIGN approval is where they approve the split; `brain split` files one child per unit. Each child builds
its one unit of the parent's Lean intent; its MR goes into the parent's branch. The parent's owner reviews and
releases once every child has merged.

## 1. Parent run: write unit-of-work.md

`lean stage start units`, then one row per unit:

| Unit | Owner team | Paths | Depends on | Contracts | Stories / ACs | Status |
|---|---|---|---|---|---|---|
| api | api | services/api/**, aidlc-docs/contracts/api/** | - | aidlc-docs/contracts/api/refunds.yaml | US-1 | not started |
| web | web | web/** | api | aidlc-docs/contracts/api/refunds.yaml | US-2 | not started |
| jobs | jobs | jobs/** | - | - | US-3 | not started |

| column | what |
|---|---|
| `Unit` | a short lowercase name (letters, digits, `-`), not `plans` or `build-and-test`. It is the child's `--unit` value, its branch name part and its Lean team |
| `Owner team` | multi-team mode: the unit's name with `-` written as `_` (`billing-worker` -> `billing_worker`), the team its child runs Lean as; then Lean's Next sends each child to its own unit. Standard mode: write the same, for readers |
| `Paths` | every folder or glob of the unit's code and of the contract files it owns, comma separated. Every unit needs Paths. Units never share files: two children writing one file collide at merge (`brain split` warns about overlaps) |
| `Depends on` | units that must merge before this one can start, or `-`. No cycles. A unit that depends on another waits for it, so keep chains short |
| `Contracts` | the contracts it provides or reads. Units meet only at contracts; a unit's own contracts live in `aidlc-docs/contracts/<unit>/`, inside its Paths |
| `Stories / ACs` | the story ids (`US-n`) and AC ids (`AC-n`) it delivers. The child's title and ACs are copied from them; text that fails the Brain's content checks (names, amounts, secrets) is not copied, only the id |

At least two units. One unit: skip Units Generation, there is nothing to split.

## 2. The order: the DESIGN approval approves the split
1. requirements.md, stories.md, application-design.md, then unit-of-work.md.
2. `lean stage done units --note "..."` records unit-of-work.md. Do this before step 3.
3. `lean request DESIGN` (or INCEPTION). Tell the person the split is in unit-of-work.md and is approved with the
   design.
4. unit-of-work.md changed after the approval: `lean stage done units` again and `lean request DESIGN` again
   (approving the unchanged design again is fine). `brain split` refuses a split the approval did not cover.
5. Commit everything. The person runs `brain split <KEY> --dry-run`, then `brain split <KEY>`.
6. From then on, build no unit on the parent's branch: each unit is built by its child.

## 3. Child run
run.json has `parent`, `unit`, `target_branch` (the parent's branch) and `lean_team`. This worktree is its own Lean
clone: Lean team = the unit, active intent = the parent's.
- Read `runs/<KEY>/intent.md` first: the unit, its Paths, what it depends on, contracts, ACs.
- requirements.md, stories.md and the design are the parent's, approved: never write a new requirements.md or
  change them (that voids the approvals for every child). If the unit cannot be built as designed, stop and tell the
  person: the parent's owner changes the design and splits again.
- Build only this unit, its code inside its Paths: `lean stage start code-generation --unit <unit>`, the plan
  `construction/plans/<unit>-code-generation-plan.md`, `lean stage done code-generation --unit <unit> --note ...`;
  the per-unit design stages take `--unit <unit>` too. `lean verify` as usual.
- Session notes: keep your Status and numbered Next in your own `### Next (team <lean_team>)` subsection; it is
  this run's hand-over note (`brain handoff` and the bridge check it, not the shared Status and Next). Leave the
  other subsections as they are.
- Lean's Next may name another unit (standard mode does not know teams): that is another child's; skip it.
- `built_on` in run.json: you started from a dependency's branch before it merged; its commits ride in your MR
  until it does. Once it has, the person runs `brain sync`.

## 4. Contracts between children
Your own unit's contracts you edit directly. Another unit's contract only through Lean's CR flow, which needs
multi-team mode, set up by a person (you never edit these files): `"multi_team": true` in `aidlc-docs/lean.json`;
`aidlc-docs/contracts/OWNERS` maps each unit's contract folder to the unit (`aidlc-docs/contracts/api/** api`);
`Owner team` = the unit; the parent's own clone has a team that is no unit (`lean init --team lead`). Example: web
needs a change in api's contract.
1. web's worktree: `lean cr new <slug> --owner api --paths aidlc-docs/contracts/api/refunds.yaml`, fill it in,
   commit; the person pushes web's branch.
2. api's worktree: after `git fetch origin`, take only that CR file from web's branch (`git checkout
   origin/<web's branch> -- aidlc-docs/contracts/changes/CR-web-1-<slug>.md`), commit; `lean request CR-web-1`;
   the person types the approval line there (this clone's Lean team is api, so Lean takes it); commit; the person
   pushes api's branch.
3. web's worktree: the person runs `brain sync --from origin/<api's branch>`, which brings the approval in (web
   uses api's contract anyway). Now Lean lets web edit that contract, only as the CR says. Build against a stub
   while you wait. Once the change has shipped, either side runs `lean cr close CR-web-1 --reason "..."`.

Standard mode: Lean does not own contracts, so nothing stops an edit. Do not edit another unit's contract; ask the
person.

## 5. Sync and hand-over
1. Commit your work (`brain sync` commits Lean's ledger and aidlc-state.md itself; anything else uncommitted makes
   it refuse). Then the person runs `brain sync` (you never merge): it merges the parent's branch in and resolves
   Lean's own files (aidlc-state.md: Session notes merged by `###` subsection, the progress written again;
   build-and-test-summary.md). Other conflicts are left (exit 1): resolve the ones in your Paths with the person. A
   conflict in `aidlc-docs/audit/` (a Lean ledger): stop and tell the person, never edit it.
2. `lean verify` on the merged code, update your `### Next (team <lean_team>)` subsection, `brain mr-desc --write`
   (it lists this unit's ACs only), commit.
3. The person runs `brain handoff`: for a child it checks your `### Next (team <lean_team>)` note, writes and
   commits `runs/<KEY>/MR.md` together with a merge request in run.json (`mr`), pushes, and opens or updates the MR
   into the parent's branch, not main. No runner hand-over and no label: the parent's owner reviews and merges it
   (only they may push their branch). The child counts as merged once that commit, and every later one on its
   branch, is in the parent's branch.

## 6. Parent run: review and release
- `brain intent show <KEY>` until every child has merged ("都合并了，等大需求评审和发布"). `brain handoff` refuses the
  parent before that.
- A child's MR that conflicts in Lean's files: the person runs `brain sync --from origin/<child's branch>` on the
  parent's branch and pushes it.
- Then on the parent's branch: `lean status` (progress from every child's ledger), `lean verify`, a review by a
  context that did not write the code, `lean release`, `lean request RELEASE`; the person approves (or, once every
  child has merged, the parent's `brain handoff` is the normal one: MR into main, the team runner does this). Then
  the parent counts as closed; `brain intent close <KEY>` records it on the claim if the person wants.
