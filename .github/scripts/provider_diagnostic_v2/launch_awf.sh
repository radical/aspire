#!/usr/bin/env bash
set -euo pipefail
# Only this trusted shell step receives the frozen proxy credential.
# The credential-free observer was started in a separate preceding step.
trap 'status=$?; printf "%s\n" "$status" > "${RUNNER_TEMP}/provider-diagnostic-v2/intent.exit"; exit "$status"' EXIT
GH_AW_COPILOT_SRC="$(command -v copilot)"
GH_AW_COPILOT_BIN="${RUNNER_TEMP}/gh-aw/bin/copilot"
mkdir -p "${RUNNER_TEMP}/gh-aw/bin"
cp "$GH_AW_COPILOT_SRC" "$GH_AW_COPILOT_BIN"
chmod 755 "$GH_AW_COPILOT_BIN"
export COPILOT_API_KEY="$COPILOT_DUMMY_BYOK"
GH_AW_NODE_BIN="$(command -v node)"
export GH_AW_NODE_BIN
GH_AW_MAX_AI_CREDITS="${GH_AW_MAX_AI_CREDITS:-1000}"
if [[ ! "$GH_AW_MAX_AI_CREDITS" =~ ^[0-9]+$ ]]; then
  GH_AW_MAX_AI_CREDITS="1000"
fi
# AWF can render provider lists during startup failure. Keep all transport,
# SDK and engine output private; the host extractor emits only an allowlist.
{
printf '%s\n' "{\"\$schema\":\"https://github.com/github/gh-aw-firewall/releases/download/v0.28.20/awf-config.schema.json\",\"network\":{\"allowDomains\":[\"*.githubusercontent.com\",\"api.snapcraft.io\",\"archive.ubuntu.com\",\"azure.archive.ubuntu.com\",\"codeload.github.com\",\"crl.geotrust.com\",\"crl.globalsign.com\",\"crl.identrust.com\",\"crl.sectigo.com\",\"crl.thawte.com\",\"crl.usertrust.com\",\"crl.verisign.com\",\"crl3.digicert.com\",\"crl4.digicert.com\",\"crls.ssl.com\",\"docs.github.com\",\"github-cloud.githubusercontent.com\",\"github-cloud.s3.amazonaws.com\",\"github.blog\",\"github.com\",\"github.githubassets.com\",\"json-schema.org\",\"json.schemastore.org\",\"keyserver.ubuntu.com\",\"lfs.github.com\",\"objects.githubusercontent.com\",\"ocsp.digicert.com\",\"ocsp.geotrust.com\",\"ocsp.globalsign.com\",\"ocsp.identrust.com\",\"ocsp.sectigo.com\",\"ocsp.ssl.com\",\"ocsp.thawte.com\",\"ocsp.usertrust.com\",\"ocsp.verisign.com\",\"packagecloud.io\",\"packages.cloud.google.com\",\"packages.microsoft.com\",\"patch-diff.githubusercontent.com\",\"patchdiff.githubusercontent.com\",\"ppa.launchpad.net\",\"raw.githubusercontent.com\",\"s.symcb.com\",\"s.symcd.com\",\"security.ubuntu.com\",\"ts-crl.ws.symantec.com\",\"ts-ocsp.ws.symantec.com\",\"www.googleapis.com\"],\"isolation\":true,\"topologyAttach\":[\"awmg-mcpg\"]},\"apiProxy\":{\"enabled\":true,\"enableTokenSteering\":true,\"maxRuns\":500,\"maxAiCredits\":${GH_AW_MAX_AI_CREDITS},\"maxCacheMisses\":5,\"models\":{\"agent\":[\"sonnet-6x\",\"gpt-6\",\"gpt-5.4\",\"gpt-5.5\",\"gpt-5.6\",\"gpt-5.3\",\"gemini-pro\",\"any\"],\"antigravity\":[\"copilot/antigravity*\",\"google/antigravity*\",\"gemini/antigravity*\"],\"any\":[\"copilot/*\",\"anthropic/*\",\"openai/*\",\"google/*\",\"gemini/*\"],\"auto\":[\"copilot/auto\",\"large\"],\"claude\":[\"agent\"],\"codex\":[\"agent\"],\"coding\":[\"copilot/gpt-5*codex*\",\"openai/gpt-5*codex*\",\"gpt-5-codex\",\"kimi\"],\"computer-use\":[\"copilot/*computer-use*\",\"google/*computer-use*\",\"gemini/*computer-use*\",\"openai/*computer-use*\"],\"copilot\":[\"agent\"],\"deep-research\":[\"copilot/deep-research*\",\"copilot/o3-deep-research*\",\"copilot/o4-mini-deep-research*\",\"google/deep-research*\",\"gemini/deep-research*\",\"openai/o3-deep-research*\",\"openai/o4-mini-deep-research*\"],\"detection\":[\"small\"],\"evals\":[\"small\"],\"fable\":[\"copilot/*fable*\",\"anthropic/*fable*\"],\"gemini\":[\"agent\"],\"gemini-3-flash\":[\"copilot/gemini-3*flash*\",\"google/gemini-3*flash*\",\"gemini/gemini-3*flash*\"],\"gemini-3-pro\":[\"copilot/gemini-3*pro*\",\"google/gemini-3*pro*\",\"google/nano-banana*\",\"gemini/gemini-3*pro*\"],\"gemini-3.1-flash\":[\"copilot/gemini-3.1*flash*\",\"google/gemini-3.1*flash*\",\"gemini/gemini-3.1*flash*\"],\"gemini-3.1-pro\":[\"copilot/gemini-3.1*pro*\",\"google/gemini-3.1*pro*\",\"gemini/gemini-3.1*pro*\"],\"gemini-3.5-flash\":[\"copilot/gemini-3.5*flash*\",\"google/gemini-3.5*flash*\",\"gemini/gemini-3.5*flash*\"],\"gemini-3.6-flash\":[\"copilot/gemini-3.6*flash*\",\"google/gemini-3.6*flash*\",\"gemini/gemini-3.6*flash*\"],\"gemini-3.7-flash\":[\"copilot/gemini-3.7*flash*\",\"google/gemini-3.7*flash*\",\"gemini/gemini-3.7*flash*\"],\"gemini-3.8-flash\":[\"copilot/gemini-3.8*flash*\",\"google/gemini-3.8*flash*\",\"gemini/gemini-3.8*flash*\"],\"gemini-flash\":[\"copilot/gemini-*flash*\",\"google/gemini-*flash*\",\"gemini/gemini-*flash*\"],\"gemini-flash-lite\":[\"copilot/gemini-*flash*lite*\",\"google/gemini-*flash*lite*\",\"gemini/gemini-*flash*lite*\"],\"gemini-omni\":[\"copilot/gemini-omni*\",\"google/gemini-omni*\",\"gemini/gemini-omni*\"],\"gemini-pro\":[\"copilot/gemini-*pro*\",\"google/gemini-*pro*\",\"gemini/gemini-*pro*\"],\"gemma\":[\"copilot/gemma*\",\"google/gemma*\",\"gemini/gemma*\"],\"gpt-5\":[\"copilot/gpt-5*\",\"openai/gpt-5*\"],\"gpt-5-codex\":[\"copilot/gpt-5*codex*\",\"openai/gpt-5*codex*\"],\"gpt-5-mini\":[\"copilot/gpt-5*mini*\",\"openai/gpt-5*mini*\"],\"gpt-5-nano\":[\"copilot/gpt-5*nano*\",\"openai/gpt-5*nano*\"],\"gpt-5-pro\":[\"copilot/gpt-5*pro*\",\"openai/gpt-5*pro*\"],\"gpt-5.1\":[\"copilot/gpt-5.1*\",\"openai/gpt-5.1*\"],\"gpt-5.2\":[\"copilot/gpt-5.2*\",\"openai/gpt-5.2*\"],\"gpt-5.3\":[\"copilot/gpt-5.3*\",\"openai/gpt-5.3*\"],\"gpt-5.4\":[\"copilot/gpt-5.4*\",\"openai/gpt-5.4*\"],\"gpt-5.5\":[\"copilot/gpt-5.5*\",\"openai/gpt-5.5*\"],\"gpt-5.6\":[\"copilot/gpt-5.6*\",\"openai/gpt-5.6*\"],\"gpt-6\":[\"copilot/gpt-6*\",\"openai/gpt-6*\"],\"grok\":[\"copilot/*grok*\",\"openai/*grok*\"],\"haiku\":[\"copilot/*haiku*\",\"anthropic/*haiku*\"],\"image-generation\":[\"copilot/gpt-image*\",\"openai/gpt-image*\",\"openai/chatgpt-image*\",\"copilot/gemini-*image*\",\"google/gemini-*image*\",\"gemini/gemini-*image*\",\"google/imagen*\"],\"kimi\":[\"copilot/kimi*\",\"openai/kimi*\"],\"kiwi\":[\"copilot/kiwi*\",\"openai/kiwi*\"],\"large\":[\"sonnet\",\"gpt-6\",\"gpt-5-pro\",\"gpt-5\",\"gemini-pro\"],\"lyria\":[\"google/lyria*\",\"gemini/lyria*\",\"copilot/lyria*\"],\"mai-code\":[\"copilot/MAI-Code*\",\"copilot/mai-code*\",\"openai/MAI-Code*\"],\"mai-code-1-flash-picker\":[\"copilot/MAI-Code-1-Flash-picker*\",\"copilot/mai-code-1-flash-picker*\",\"openai/MAI-Code-1-Flash-picker*\"],\"mini\":[\"haiku\",\"gpt-5-mini\",\"gpt-5-nano\",\"gemini-flash-lite\"],\"nano-banana\":[\"copilot/nano-banana*\",\"google/nano-banana*\",\"gemini/nano-banana*\"],\"opus\":[\"copilot/*opus*\",\"anthropic/*opus*\"],\"opusplan\":[\"opus?effort=high\"],\"raptor-mini\":[\"copilot/raptor*\",\"openai/raptor*\"],\"reasoning\":[\"copilot/o1*\",\"copilot/o3*\",\"copilot/o4*\",\"openai/o1*\",\"openai/o3*\",\"openai/o4*\"],\"robotics\":[\"copilot/*robotics*\",\"google/*robotics*\",\"gemini/*robotics*\"],\"small\":[\"mini\"],\"small-agent\":[\"haiku\",\"gpt-5-mini\",\"gemini-flash\"],\"sonnet\":[\"copilot/*sonnet*\",\"anthropic/*sonnet*\"],\"sonnet-6x\":[\"copilot/*sonnet-4.5*\",\"copilot/*sonnet-4.6*\",\"copilot/*sonnet-5*\",\"copilot/*sonnet-4-5-*\",\"anthropic/*sonnet-4-5-*\",\"copilot/*sonnet-4-6*\",\"anthropic/*sonnet-4-6*\",\"anthropic/*sonnet-5*\"],\"summarization\":[\"haiku\",\"gpt-5-mini\",\"gemini-flash-lite\",\"mini\"],\"veo\":[\"google/veo*\",\"gemini/veo*\"],\"vision\":[\"copilot/gemini-*image*\",\"google/gemini-*image*\",\"gemini/gemini-*image*\",\"copilot/gemini-*flash*\",\"google/gemini-*flash*\",\"gemini/gemini-*flash*\"]}},\"container\":{\"imageTag\":\"0.28.20,squid=sha256:cba5f56857e4869c4a00c1cce29c3056516cf1a746e18ce380753ecfbe40f112,agent=sha256:1bacef0f405d77999d01d8c00cb731222531b3f43ef9dc8312c513d6d9038bb1,api-proxy=sha256:30ab6d3261dd95364281fa0b52ab78450e1c01aa074eccc3a8d4f04b12a6560b,cli-proxy=sha256:1f2d7e0791d0e522152f7479ed700476720effe3b7498b837259112049595d64\"},\"logging\":{\"proxyLogsDir\":\"/tmp/gh-aw/sandbox/firewall/logs\",\"auditDir\":\"/tmp/gh-aw/sandbox/firewall/audit\"}}" > "${RUNNER_TEMP}/gh-aw/awf-config.json"
cp "${RUNNER_TEMP}/gh-aw/awf-config.json" /tmp/gh-aw/awf-config.json
GH_AW_DOCKER_HOST=""
if [[ "${DOCKER_HOST:-}" =~ ^tcp:// ]]; then
  GH_AW_DOCKER_HOST="${DOCKER_HOST}"
fi
if [[ "${DOCKER_HOST:-}" =~ ^tcp:// ]]; then
  GH_AW_CHROOT_BINARIES_SOURCE_PATH="${RUNNER_TEMP}/gh-aw" GH_AW_CHROOT_IDENTITY_HOME="${RUNNER_TEMP}/gh-aw/home" node "${RUNNER_TEMP}/gh-aw/actions/patch_awf_chroot_config.cjs"
fi
GH_AW_TOOL_CACHE_MOUNT=""
GH_AW_TOOL_CACHE="${RUNNER_TOOL_CACHE:?RUNNER_TOOL_CACHE must be set}"
if [ -d "$GH_AW_TOOL_CACHE" ]; then
  if [[ "$GH_AW_TOOL_CACHE" != /opt/* ]]; then
    GH_AW_TOOL_CACHE_MOUNT="$GH_AW_TOOL_CACHE:$GH_AW_TOOL_CACHE:ro"
  fi
fi
# shellcheck disable=SC1003,SC2016,SC2086
mkdir -p "/tmp/gh-aw"
evidence_tmp="/tmp/gh-aw/agent_execution.json.tmp"
printf '{"version":1,"component":"agent","run_id":%s,"run_attempt":%s,"state":"started"}\n' "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" > "$evidence_tmp"
mv "$evidence_tmp" "/tmp/gh-aw/agent_execution.json"
export GH_AW_AWF_EXECUTION_COMPONENT="agent"
export GH_AW_AWF_EXECUTION_EVIDENCE_FILE="/tmp/gh-aw/agent_execution.json"
GH_AW_AWF_ENGINE_NAME=copilot \
GH_AW_AWF_HARNESS_MARKER='[copilot-harness]' \
GH_AW_AWF_LOG_FILE=/tmp/gh-aw/agent-stdio.log \
GH_AW_AWF_ATTEMPT_LOG_NAME=copilot \
bash "${RUNNER_TEMP}/gh-aw/actions/run_awf_with_startup_retries.sh" -- \
  awf --config "${RUNNER_TEMP}/gh-aw/awf-config.json" --container-workdir "${GITHUB_WORKSPACE}" --mount "${RUNNER_TEMP}/gh-aw:${RUNNER_TEMP}/gh-aw:ro" --mount "${RUNNER_TEMP}/gh-aw:/host${RUNNER_TEMP}/gh-aw:ro" --mount "${RUNNER_TEMP}/gh-aw/safeoutputs/upload-artifacts:${RUNNER_TEMP}/gh-aw/safeoutputs/upload-artifacts:rw" ${GH_AW_TOOL_CACHE_MOUNT:+--mount "$GH_AW_TOOL_CACHE_MOUNT"} ${GH_AW_DOCKER_HOST:+--docker-host "$GH_AW_DOCKER_HOST"} --env-all --exclude-env ACTIONS_ID_TOKEN_REQUEST_TOKEN --exclude-env ACTIONS_ID_TOKEN_REQUEST_URL --exclude-env COPILOT_GITHUB_TOKEN --exclude-env GITHUB_MCP_SERVER_TOKEN --exclude-env MCP_GATEWAY_AGENT_ID --mount /tmp/gh-aw:/tmp/gh-aw:rw --log-level info --skip-pull \
  -- /bin/bash -c 'set -euo pipefail; "${RUNNER_TEMP}/gh-aw/bin/copilot" --version > /tmp/gh-aw/provider-diagnostic-version.txt 2>/dev/null; sleep 20'
} > "${RUNNER_TEMP}/provider-diagnostic-v2/private-awf.log" 2>&1
