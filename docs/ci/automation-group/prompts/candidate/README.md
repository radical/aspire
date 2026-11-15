# UNAPPLIED 3-role candidates

Rendered by `check_prompts.py --write` from `../source/` with RECOVERY CORE
v3 (1,820 characters) inline in every role and REPORT CONTRACT v1 (1,616
characters, from `.agents/skills/ci-health-report/SKILL.md`) inline in the
monitor only. Not live configuration; see [../README.md](../README.md) for
the pending deployment and reviewer retirement sequence.

Deployment checkpoint, November 15 at 19:45 UTC: the manager's fresh fixer
`automation_get` failed with "no active client is registered to execute this
client tool". No final v2 proposal or configuration write was attempted.
Reconnect the hosted client before fresh reads and superseding proposals.
The older pending fixer proposal `c81855b7-e2a2-502c-8787-2948f8a61a0c`
does not contain these candidates and must not be used for this rollout.
The report-edit freeze is candidate behavior, not a verified live freeze.

GPT-6 Astra's review found a non-atomic review reservation. The corrected
fixer reserves its shared non-expiring PR review record before either launch;
losing contenders skip, and unknown outcomes retain the reservation. Astra
verified the correction and the unchanged readiness gate.

Checker result: exit 0, 62 obligation rows / 266 anchors, 27 reviewer
transfers (33 anchors, 4 retired); `--historical` exit 0. All three exceed
the advisory 10,500-byte target (warning) and stay under the 12,000 hard
ceiling.

| Role | Candidate | Code points | UTF-16 | Bytes | SHA-256 |
| --- | --- | --- | --- | --- | --- |
| Monitor | [monitor.md](monitor.md) | 11,885 | 11,885 | 11,885 | `424c013c5f8005fa2fce27a1ce3bbe16e93e5c4afdb9f0129ccf7e40894a85ef` |
| Fixer | [worker.md](worker.md) | 11,955 | 11,956 | 11,958 | `e1a87c36ff144bdb9f7834aa061d674bfb5138c8b46adcb6cf50b0445be2714b` |
| Supervisor | [supervisor.md](supervisor.md) | 11,895 | 11,895 | 11,895 | `dd43e42f3de0242e0291423525fe50eece2c9f28860f75ca65af36b990f7cf52` |

Headroom is 42–115 bytes per prompt. The advisory target was not reached
without dropping essential authority, claim, review, recovery or evidence
rules; any addition needs an equal consolidation first.
