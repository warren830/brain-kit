# AGENTS.md

<!-- Keep under 1500 words. Owners: product + Builder (both approve changes). -->

## What this repo does
(one paragraph)

## Boundaries
- Never touch: production, `gates/`, `.brain/`, `project-agent.yaml`, CI files.
- Always a human: dangerous permissions, external actions, irreversible actions, customer data.

## Where to read
- `brain/PRODUCT.md` — intent, users, non-goals, terms
- `brain/TECH.md` — architecture, conventions, runtime pitfalls
- `brain/IMPROVEMENT.md` — what worked, what failed
- `brain/PROJECT.md` — current focus, decisions, open items
- Run records: `runs/<JIRA>/run.json`; stage records: `aidlc-docs/<intent>/` (AI-DLC Lean)
