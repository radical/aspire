---
description: "Twice-daily dependency security reconciliation for radical/aspire. Reads the\nopen Dependabot alerts (including malware alerts) and the code scanning alerts\nthat map to a package or action version, then reconciles every alert against\nthe open Dependabot pull requests:\n\n  1. When an open Dependabot PR fixes an alert, the agent requests approval via\n     the `approve-dependabot-pr` safe-output job. That job re-verifies every\n     gate deterministically (Dependabot author, manifest-only diff, no new\n     package source, green CI, non-breaking version change, 7-day cooldown)\n     before the Aspire bot App submits an APPROVE review.\n  2. When no Dependabot PR covers an alert, the agent upgrades the affected\n     manifests itself and opens a single `auto-sec` labeled PR, or updates the\n     already-open `auto-sec` PR instead of opening another.\n\nAlert details never leave the run: PR bodies and summaries describe the\npackage upgrades only, and the run summary contains\
  \ counts.\n"
max-daily-ai-credits: -1
if: github.repository == 'radical/aspire' && github.ref == 'refs/heads/main'
concurrency:
  group: auto-sec-pr20693-trial
  cancel-in-progress: false
timeout-minutes: 60
permissions:
  contents: read
  pull-requests: read
  security-events: read
  copilot-requests: write
network:
  allowed:
  - defaults
  - github
  - node
  - python
  - dotnet
  - pkgs.dev.azure.com
  - dnceng.pkgs.visualstudio.com
tools:
  edit: null
  bash:
  - cat
  - ls
  - grep
  - head
  - tail
  - wc
  - jq
  - sed
  - find
  - diff
  - git
  - node
  - npm
  - yarn
  - corepack
  - uv
  github:
    toolsets:
    - repos
    - pull_requests
    min-integrity: none
    allowed-repos:
    - radical/aspire
pre-agent-steps:
- name: Install uv
  run: pipx install uv==0.12.22
- name: Copy gate module for the agent output scrub
  run: 'mkdir -p "${RUNNER_TEMP}/auto-sec-gate"

    cp .github/workflows/auto-sec/auto-sec.js "${RUNNER_TEMP}/auto-sec-gate/auto-sec.js"

    '
- name: Precreate runner-owned safe output file
  env:
    GH_AW_SAFE_OUTPUTS: ${{ steps.set-runtime-paths.outputs.GH_AW_SAFE_OUTPUTS }}
  run: |
    set -euo pipefail
    node - <<'JS'
    const fs = require('node:fs');
    const path = process.env.GH_AW_SAFE_OUTPUTS;
    if (!path) throw new Error('Missing safe output path');
    const fd = fs.openSync(path, 'wx', 0o600);
    fs.closeSync(fd);
    const stat = fs.statSync(path);
    if (stat.uid !== process.getuid() || (stat.mode & 0o777) !== 0o600) {
      throw new Error('Unexpected safe output ownership or mode');
    }
    console.log('Safe output file precreated: runner-owned, mode=0600.');
    JS
- name: Load synthetic refusal fixtures
  run: 'set -euo pipefail

    mkdir -p .auto-sec

    cat > .auto-sec/alerts.json <<''JSON''

    [{"number":1,"ecosystem":"maven","package":"trial-fixture:unsupported","manifest_path":"trial-fixture/pom.xml","malware":false,"vulnerable_version_range":"< 2.0.0","vulnerable_ranges":["< 2.0.0"],"first_patched_version":"2.0.0"},{"number":2,"ecosystem":"actions","package":"trial-fixture/action","manifest_path":".github/workflows/trial-fixture.yml","malware":false,"vulnerable_version_range":"< 2.0.0","vulnerable_ranges":["< 2.0.0"],"first_patched_version":"2.0.0"}]

    JSON

    printf ''%s\n'' ''[]'' > .auto-sec/malware-alert-numbers.json

    printf ''%s\n'' ''{"version_alerts":[],"other_rule_counts":[]}'' > .auto-sec/code-scanning.json

    printf ''%s\n'' ''[]'' > .auto-sec/dependabot-prs.json

    printf ''%s\n'' ''[]'' > .auto-sec/auto-sec-prs.json

    '
secret-masking:
  steps:
  - name: Scrub auto-sec agent transcript and outputs
    id: auto_sec_scrub
    if: always()
    uses: actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3
    env:
      GH_AW_SAFE_OUTPUTS: ${{ steps.set-runtime-paths.outputs.GH_AW_SAFE_OUTPUTS }}
    with:
      script: await require(`${process.env.RUNNER_TEMP}/auto-sec-gate/auto-sec.js`).runAgentOutputScrub({ core, github, context });
safe-outputs:
  missing-tool:
    create-issue: false
  missing-data:
    create-issue: false
  report-incomplete:
    create-issue: false
  report-failure-as-issue: false
  create-pull-request:
    max: 1
    title-prefix: '[auto-sec] '
    labels:
    - auto-sec
    draft: false
    preserve-branch-name: true
    recreate-ref: true
    allowed-branches:
    - auto-sec/security-updates
    if-no-changes: ignore
    patch-format: am
    allowed-files: &id001
    - '**/package.json'
    - '**/package-lock.json'
    - '**/npm-shrinkwrap.json'
    - '**/yarn.lock'
    - '**/pnpm-lock.yaml'
    - '**/uv.lock'
    - '**/pyproject.toml'
    - Directory.Packages.props
    - '**/Directory.Packages.props'
    - package.json
    - package-lock.json
    - yarn.lock
    - uv.lock
    - pyproject.toml
    protected-files: &id002
      policy: blocked
      exclude:
      - package.json
      - package-lock.json
      - npm-shrinkwrap.json
      - yarn.lock
      - pnpm-lock.yaml
      - uv.lock
      - pyproject.toml
      - Directory.Packages.props
  push-to-pull-request-branch:
    max: 1
    target: '*'
    required-labels:
    - auto-sec
    required-title-prefix: '[auto-sec]'
    check-branch-protection: false
    if-no-changes: ignore
    patch-format: am
    allowed-files: *id001
    protected-files: *id002
  steps:
  - name: Restrict pushes to the auto-sec branch
    if: (!cancelled()) && contains(needs.agent.outputs.output_types, 'push_to_pull_request_branch')
    uses: actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3
    env:
      GH_AW_AGENT_OUTPUT: ${{ steps.setup-agent-output-env.outputs.GH_AW_AGENT_OUTPUT }}
    with:
      script: 'const gate = require(`${process.env.GITHUB_WORKSPACE}/.github/workflows/auto-sec/auto-sec.js`);

        await gate.runPushTargetGate({ github, context, core });

        '
  - name: Validate auto-sec patch contents
    if: (!cancelled()) && (contains(needs.agent.outputs.output_types, 'create_pull_request') || contains(needs.agent.outputs.output_types, 'push_to_pull_request_branch'))
    uses: actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3
    with:
      script: 'const gate = require(`${process.env.GITHUB_WORKSPACE}/.github/workflows/auto-sec/auto-sec.js`);

        await gate.runPatchContentGate({ github, context, core });

        '
  - name: Validate auto-sec public text
    if: (!cancelled()) && (contains(needs.agent.outputs.output_types, 'create_pull_request') || contains(needs.agent.outputs.output_types, 'push_to_pull_request_branch'))
    uses: actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3
    env:
      GH_AW_AGENT_OUTPUT: ${{ steps.setup-agent-output-env.outputs.GH_AW_AGENT_OUTPUT }}
    with:
      script: 'const gate = require(`${process.env.GITHUB_WORKSPACE}/.github/workflows/auto-sec/auto-sec.js`);

        await gate.runPublicTextGate({ core, github, context });

        '
  jobs:
    approve-dependabot-pr:
      name: Approve Dependabot PR
      description: 'Request an approval for an open Dependabot pull request that fixes at

        least one open alert. Emit one item per pull request. The job re-checks

        every gate (Dependabot author, manifest-only diff, no new package source,

        green CI, non-breaking update, 7-day cooldown, fixes an open alert) and

        silently skips pull requests that fail any of them.

        '
      runs-on: ubuntu-latest
      needs:
      - safe_outputs
      permissions:
        contents: read
        pull-requests: read
        checks: read
        statuses: read
        security-events: read
      inputs:
        pr_number:
          description: Number of the open Dependabot pull request.
          required: true
          type: number
        head_sha:
          description: Full 40-character head commit SHA the request was evaluated against.
          required: true
          type: string
      steps:
      - name: Check out gate module
        uses: actions/checkout@v7.0.1
        with:
          persist-credentials: false
          path: _gate
          sparse-checkout: '.github/workflows/auto-sec/auto-sec.js

            '
          sparse-checkout-cone-mode: false
      - name: Verify gates and approve
        uses: actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3
        env:
          APPROVER_TOKEN: ${{ github.token }}
          AUTO_SEC_BOT_LOGIN: aspire-repo-bot[bot]
        with:
          script: 'const gate = require(`${process.env.GITHUB_WORKSPACE}/_gate/.github/workflows/auto-sec/auto-sec.js`);

            const approver = getOctokit(process.env.APPROVER_TOKEN);

            await gate.runApprovalJob({ github, approver, context, core });

            '
      env:
        GH_AW_SAFE_OUTPUTS_STAGED: 'true'
  staged: true
'on':
  workflow_dispatch: {}
---

# Auto-sec dependency reconciliation

You reconcile the open dependency security alerts of `radical/aspire` against the
open Dependabot pull requests. Every alert must end this run in exactly one of
these states:

- **dependabot-pr**: an open Dependabot PR fixes it, and you requested approval.
- **auto-sec-pr**: you fixed it in the single `auto-sec` pull request.
- **blocked**: it cannot be fixed safely this run (reason codes below).

## Rules that are never negotiable

1. **Never** change a package source or feed: do not edit `NuGet.config`,
   `.npmrc`, `.yarnrc*`, `global.json`, `uv.toml`, pip index settings, or any
   `registry`/`resolved`/`source` URL in a lockfile to a new source. Only the
   dnceng public feeds (`https://pkgs.dev.azure.com/dnceng/public/_packaging/...` and
   `https://dnceng.pkgs.visualstudio.com/public/_packaging/...`) and sources the file
   already uses are acceptable.
2. **7-day cooldown.** Never propose a version published less than 7 days ago.
   Check every candidate version with:
   `node .github/workflows/auto-sec/auto-sec.js lookup <npm|pip|nuget> <name> <version>`
   and use it only when `cooldown_satisfied` is `true`. Pick the lowest version that
   is at or above the first patched version and satisfies the cooldown.
3. **Non-breaking only.** Stay within the current major version (within the current
   minor for `0.x`). If the only fix is a breaking upgrade, mark the alert
   `blocked: breaking-upgrade-required`.
4. **No advisory details in public output.** PR titles, bodies, commit messages,
   and safe-output text may name packages and versions only. Never mention GHSA or
   CVE ids, severities, vulnerability descriptions, or the words "vulnerability",
   "exploit", or "malware".
5. Never run dependency install scripts or build backends. Always pass
   `--ignore-scripts` to `npm` and `yarn`, `--lockfile-only` to `pnpm` (which then
   never runs scripts), and `--no-build` to `uv`.
   Never edit files outside dependency manifests and lockfiles.

## Inputs

The pre-agent step wrote these private files (read them with `cat`/`jq`).
The compiled logging boundary suppresses gateway/agent stdout and stderr before
execution; the later scrub separately prevents transcript and artifact publication.
Do not print alert contents into public safe outputs:

- `.auto-sec/alerts.json`: open Dependabot alerts. `malware: true` marks malware
  alerts; they have no `first_patched_version`, and the only fix is moving to a
  version outside every `vulnerable_ranges` entry. Never remove a dependency; if no
  such non-breaking version exists, mark the alert `blocked: no-safe-version`.
- `.auto-sec/code-scanning.json`: `version_alerts` are code scanning alerts tied to
  an action or package version; `other_rule_counts` are code findings that are out
  of scope for this workflow.
- `.auto-sec/dependabot-prs.json`: open Dependabot PRs with parsed `updates`
  (`name`, `from`, `to`), changed `files`, a `checks` rollup, and
  `covered_alerts`: the alert numbers the PR provably fixes, computed by the same
  module the approval job uses.
- `.auto-sec/auto-sec-prs.json`: open pull requests labeled `auto-sec`.
- `.github/dependabot.yml`: the repository Dependabot configuration (its
  `cooldown.default-days: 7` is the policy the cooldown rule enforces).

## Step 1: Match alerts to Dependabot PRs

Use each PR's `covered_alerts` as the only source of truth for which alerts it
fixes. Do not match alerts to PRs yourself: a PR covers an alert only when the
alert number is listed in that PR's `covered_alerts`.

For every PR with a non-empty `covered_alerts` that is **not** a GitHub Actions
update, emit one `approve_dependabot_pr` item with `pr_number` and the full
`head_sha`. The approval job verifies green CI,
the cooldown, the manifest-only diff, the package source, and the version change
itself, and skips PRs that fail any check, so request approval even when CI is
still running. Still treat alerts as covered by the PR; do not duplicate the fix in
the auto-sec PR. The approval job never approves a PR that updates a package with a
`malware: true` alert; such PRs stay with a human reviewer, so do not request
approval for them, but still treat the alert as covered by the PR.

## Step 2: Fix the remaining alerts in the auto-sec PR

Collect every alert not covered in Step 1.

**Choose the working branch.**

- If `.auto-sec/auto-sec-prs.json` has an open PR on `auto-sec/security-updates`,
  update that PR: `git fetch origin auto-sec/security-updates` then
  `git checkout -B auto-sec/security-updates FETCH_HEAD`. Merge in the latest
  default branch only if the PR can no longer be cleanly updated
  (`git fetch origin main && git merge --no-edit FETCH_HEAD`); if that conflicts,
  mark the remaining alerts `blocked: auto-sec-pr-conflict` and stop.
- Otherwise start from the current checkout:
  `git checkout -B auto-sec/security-updates`.

Skip alerts the existing auto-sec PR already fixes.

**Fix by ecosystem.** Change only the alert's own manifest directory.

Each patch must contain a real reconstructed dependency version change; never
submit a metadata-only patch. Regenerate integrity/checksum/hash and other package
metadata only for the specific entry whose version changes. An unrelated upgrade
or another copy of the same package does not authorize editing an unchanged entry.
New or re-keyed entries at an already installed version must preserve its metadata.
Preserve npm project/package identities. npm workspace-root selectors and pnpm
importer/snapshot references must select installed targets with matching version
changes, without changing the consumer's artifact metadata. If other metadata
regeneration on an unchanged owner is required, report `blocked: update-failed`.

- **npm** (`package-lock.json`): in the manifest directory run
  `npm install <name>@<version> --package-lock-only --ignore-scripts` for direct
  dependencies. For transitive dependencies, prefer
  `npm update <name> --package-lock-only --ignore-scripts`; if the parent pins an
  old range, update an existing `overrides` version in `package.json`. Edit only
  single-bound selectors (such as exact, caret, or tilde versions); leave compound
  selectors such as `>=1 <2` unchanged, or report `blocked: update-failed` when the
  fix requires changing one. The policy cannot verify every bound of a compound
  selector. Do not add
  new entries: the manifest gate permits only version-token replacements. If a
  fix requires a new override, mark the alert `blocked: update-failed`.
  Keep the registry the lockfile already uses.
  In JSON lockfiles, add dependency data only inside recognized npm dependency maps
  or package entries. Do not add arbitrary metadata keys, even when their values
  look like versions, and do not duplicate JSON properties.
- **yarn** (`yarn.lock`): use `yarn up <name>@<version> --mode=update-lockfile`
  (Berry) or `yarn upgrade <name>@<version> --ignore-scripts` (classic), whichever
  matches the directory, or update an existing `resolutions` version for transitive
  dependencies. If a new entry is required, mark it `blocked: update-failed`.
- **pnpm** (`pnpm-lock.yaml`): in the manifest directory run
  `corepack pnpm update <name>@<version> --lockfile-only` for direct dependencies.
  For transitive dependencies, update an existing `pnpm.overrides` version in
  `package.json`, then run `corepack pnpm install --lockfile-only`. If a new entry
  is required, mark it `blocked: update-failed`. Never run
  `npm` or `yarn` in a pnpm directory; they would write a second lockfile.
- **pip** (`uv.lock`): in the project directory run
  `uv lock --no-build --upgrade-package <name>==<version>`. `--no-build` stops uv
  from running a source distribution's build backend to read its metadata. If uv
  cannot resolve without building from source, mark the alert
  `blocked: source-build-required`.
- **nuget** (`Directory.Packages.props`): update a `PackageVersion` entry only
  when its `Version` is a literal and the lookup reports
  `available_on_approved_feed: true`. If the `Version` is an MSBuild property
  (for example `$(MicrosoftExtensionsAIVersion)`), the version is shared or flows
  from Arcade dependency flow through `eng/Versions.props`; do not replace it with
  a literal. Mark the alert `blocked: nuget-version-managed`. Otherwise, if the
  version is not mirrored, mark it `blocked: nuget-not-mirrored`. Never add a feed.
- **actions** and version-mapped code scanning alerts: do not edit workflow
  files. Mark them `blocked: actions-pin-requires-maintainer`; action pins must be
  updated together with the repository Actions allow-list.
- **Any other ecosystem or manifest** (for example Maven `pom.xml`, Gradle, Go
  modules, Cargo, or pip `requirements.txt`): this workflow cannot approve or fix
  it. Mark the alert `blocked: unsupported-ecosystem`; Dependabot PRs for these
  ecosystems stay with a human reviewer.

If no compliant version exists yet, use `blocked: no-version-past-cooldown`. If a
command fails or the result does not resolve, revert that directory with
`git checkout -- <dir>` and use `blocked: update-failed`.

After the edits, review `git diff --stat` and `git diff` for each lockfile. All
lockfile additions are restricted to recognized dependency/version/artifact
data; new metadata such as descriptions, notes, funding, or license text is not
supported. If regeneration requires unsupported metadata, revert that directory
and mark it `blocked: update-failed`.

Summary rows must match the exact manifest, package, old version, and new version
in a fully reconstructed patch, with exactly one row per manifest/package/target
and no omitted updates. Missing trusted bases or unsupported scalar encodings
block publication; do not substitute package names mentioned elsewhere in the diff.

Confirm
only dependency manifests changed, no new registry host appears, and every version
change is within the same major. Commit once with exactly this message: the subject
`Update dependencies`, a blank line, then one line per distinct package/from/target
tuple (`<name> <from> -> <to>`, for example `lodash 4.17.20 -> 4.17.21`).
Cover every reconstructed prior version and target; identical tuples repeated
across manifests appear only once. Empty, partial, and duplicate summaries are
rejected, including case and version-prefix aliases. Each commit must describe
its own changes, and a push message must cover the complete emitted patch batch.
Intermediate-only targets absent from the final reconstructed files remain blocked.
Do not invent a prior version for a newly introduced package. Use the default
git identity; never pass `--author` or change `user.name`/`user.email`. The
safe-output job rejects any other commit message, author, title, or body.

**Emit the safe output.**

Emit at most one code-writing output: create or push, never both. Duplicate
code-writing requests are rejected before publication.

- Existing auto-sec PR: emit `push_to_pull_request_branch` with that PR's number and
  the same commit message as `message`.
- No existing PR, and you committed changes: emit `create_pull_request` with branch
  `auto-sec/security-updates`, title `Automated dependency updates`, and this body:

  ```markdown
  This is an automated pull request created by the auto-sec workflow.

  It updates the following dependencies to newer, non-breaking versions that have
  been published for at least 7 days and resolve from the existing package sources:

  | Package | Manifest | From | To |
  | --- | --- | --- | --- |
  | ... | ... | ... | ... |

  This table records the initial update batch. Subsequent automated updates are
  listed in the commit history; review the full branch diff for the current changes.

  No package sources or feeds were changed. Please review the lockfile diffs and
  CI results before merging.
  ```

  Use exactly this text with one table row per updated package
  (`| <name> | <manifest path> | <from> | <to> |`); each row's package and target
  version must appear in your commit.

- Nothing to change: emit no PR output.

## Step 3: Report

Finish with a single `noop` message containing counts only, in exactly this form:
`alerts=12 dependabot-pr=3 auto-sec-pr=6 blocked=3 (nuget-not-mirrored=1, breaking-upgrade-required=2) code-findings-out-of-scope=344`.
Omit the parenthesized breakdown only when `blocked=0`. Otherwise include unique
reason codes with positive counts summing exactly to `blocked`, using only the codes
defined above. Do not list package names, alert numbers, or advisory ids in this
message; any other text fails the run. Do not emit `missing_tool`, `missing_data`,
or `report_incomplete`; they are dropped.
