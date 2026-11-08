---
name: brain-handoff
description: Use when the person wants this run continued on the team runner (review, CI fixes, evidence, delivery), or when work must pause for longer than a working day.
---
# Hand a local run over to the team runner

The team runner continues from your commit and your hand-over note. It cannot ask you what you meant, so the note
must stand on its own.

1. `lean status`: confirm the stage you are in.
2. Update `aidlc-docs/<intent>/aidlc-state.md` Session notes:
   - `Status:` one line (stage, unit, what is done).
   - `### Next`: numbered steps the runner should do next, each checkable (a command, a file, an AC id).
   - `### Open questions and blockers`: anything only the owner or product can decide.
   - `### Notes for the next session`: traps, decisions already taken (with `decisions.md` line refs).
3. Claims need evidence: "tests pass" only with the command and the date you ran it. The runner re-runs everything.
4. Commit the work (Lean's commit gate may ask for `lean verify` first).
5. Tell the person to run `brain handoff`. Do not push or label the MR yourself.

After the hand-over, stop editing this branch locally unless the runner hands it back (label removed and a comment
on the MR). Two writers on one branch cause lost work.
