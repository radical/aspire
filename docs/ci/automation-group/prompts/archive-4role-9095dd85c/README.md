# ARCHIVED 4-role candidates: NOT FOR ROLLOUT

These are the exact four-role recovery candidates (monitor, fixer, reviewer,
supervisor) reviewed at `9095dd85c` (GPT-6 Astra re-review passed). They were
NEVER applied: deployment was blocked because hosted automation clients
aborted or had no active client.

On November 15, 2026, the owner replaced this set with three-role candidates
(`../candidate/`) that move per-head code review into a fixer child and retire
the separate reviewer cron. Do not deploy anything in this directory.

- `candidate/` holds the exact reviewed bytes. SHA-256: monitor
  `4ceca4f6f9bccf2edb9d1c414552b5065f68f206833aa7cec4dbf836afa2aa1d`, fixer
  `7f6e42b9a6717170e6697ae4b7d0a1776163453ffea6a2300f52c99b9baa5acd`,
  reviewer `3f132102328a9635e7ec5640382c57bc89b6472d544919093b9d3b71f1f6cc1f`,
  supervisor `278454213775edad5aa06490f1f872b74eb1f499b887eedfa8eb5f2648039488`.
- `source/` holds the templates that rendered them with RECOVERY CORE v2.
  `recovery-core-v2.txt` pins that exact core, so the skill can move to v3
  without changing this archive.
- `python3 ../check_prompts.py --historical` re-renders and compares this set
  without the current obligation map. The 4-role map is in git history at
  `1584d3607:docs/ci/automation-group/prompt-obligations.md`.

The live reviewer (`62ff16a7-a74c-4c96-a23c-d053c3f35743`, generation 19) is
unchanged; its exact prompt is `../current/reviewer.md`.
