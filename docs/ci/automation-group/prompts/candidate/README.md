# UNAPPLIED 3-role candidates

Rendered by `check_prompts.py --write` from `../source/` with RECOVERY CORE
v3 (1,820 characters) inline in every role and REPORT CONTRACT v1 (1,616
characters, from `.agents/skills/ci-health-report/SKILL.md`) inline in the
monitor only. Not live configuration; see [../README.md](../README.md) for
the pending deployment and reviewer retirement sequence.

Checker result: exit 0, 62 obligation rows / 264 anchors, 27 reviewer
transfers (33 anchors, 4 retired); `--historical` exit 0. All three exceed
the advisory 10,500-byte target (warning) and stay under the 12,000 hard
ceiling.

| Role | Candidate | Code points | UTF-16 | Bytes | SHA-256 |
| --- | --- | --- | --- | --- | --- |
| Monitor | [monitor.md](monitor.md) | 11,885 | 11,885 | 11,885 | `424c013c5f8005fa2fce27a1ce3bbe16e93e5c4afdb9f0129ccf7e40894a85ef` |
| Fixer | [worker.md](worker.md) | 11,927 | 11,928 | 11,930 | `951a62980fb592c938e2dae00ea1d670641debee8d133953343a6e66386a2217` |
| Supervisor | [supervisor.md](supervisor.md) | 11,895 | 11,895 | 11,895 | `dd43e42f3de0242e0291423525fe50eece2c9f28860f75ca65af36b990f7cf52` |

Headroom is 70–115 bytes per prompt. The advisory target was not reached
without dropping essential authority, claim, review, recovery or evidence
rules; any addition needs an equal consolidation first.
