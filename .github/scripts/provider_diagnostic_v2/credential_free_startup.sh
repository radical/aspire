#!/usr/bin/env bash
set -euo pipefail
umask 077
existing=$(docker ps -a --filter 'name=^/awmg-mcpg$' --format '{{.ID}}')
test -z "$existing"
printf 'owned-empty-name-preflight\n' > "${CF_STATE}/preflight-ok"
bash "${RUNNER_TEMP}/gh-aw/actions/install_copilot_cli.sh" 1.0.85
bash "${RUNNER_TEMP}/gh-aw/actions/install_awf_binary.sh" v0.28.20 --rootless
bash "${RUNNER_TEMP}/gh-aw/actions/download_docker_images.sh" ghcr.io/github/gh-aw-firewall/agent:0.28.20@sha256:1bacef0f405d77999d01d8c00cb731222531b3f43ef9dc8312c513d6d9038bb1 ghcr.io/github/gh-aw-firewall/api-proxy:0.28.20@sha256:30ab6d3261dd95364281fa0b52ab78450e1c01aa074eccc3a8d4f04b12a6560b ghcr.io/github/gh-aw-firewall/squid:0.28.20@sha256:cba5f56857e4869c4a00c1cce29c3056516cf1a746e18ce380753ecfbe40f112 ghcr.io/github/gh-aw-mcpg:v0.4.25@sha256:9be0a86220e807a0ecc89e53d7453468f7a53fbc6b3d1efd2299025ffe01d086 ghcr.io/github/gh-aw-node@sha256:0daa8971fa4732b647150cb6524a6b0804b68d5d24f6f58b5dd1af23bd63fb23 ghcr.io/github/github-mcp-server:v1.12.1@sha256:0ba840c46a237879c8300e7fddb0b6347f20e029ccb9cbe2ce4a943daa1ff560
docker image inspect --format '{{.Id}}' ghcr.io/github/gh-aw-mcpg:v0.4.25 > "${CF_STATE}/image-id"
test -s "${CF_STATE}/image-id"
mkdir -p "${RUNNER_TEMP}/gh-aw/mcp-config"
# Export gateway environment variables for MCP config and gateway script
export MCP_GATEWAY_PORT="8080"
export MCP_GATEWAY_DOMAIN="awmg-mcpg"
export MCP_GATEWAY_HOST_DOMAIN="localhost"
MCP_GATEWAY_AGENT_ID="synthetic-credential-free-gateway-startup-v1"
echo "::add-mask::${MCP_GATEWAY_AGENT_ID}"
export MCP_GATEWAY_AGENT_ID
export MCP_GATEWAY_PAYLOAD_DIR="/tmp/gh-aw/mcp-payloads"
mkdir -p "${MCP_GATEWAY_PAYLOAD_DIR}"
export MCP_GATEWAY_PAYLOAD_SIZE_THRESHOLD="524288"
export MCP_GATEWAY_ALLOWED_MOUNT_ROOTS="${GITHUB_WORKSPACE}:rw,${RUNNER_TEMP}/gh-aw:ro,${RUNNER_TEMP}/gh-aw/safeoutputs:rw,/opt:ro,/tmp:rw,/usr/bin/gh:ro"
export GH_AW_PR_HEAD_BASE_BRANCH="${GH_AW_PR_HEAD_BASE_BRANCH:-}"
export GH_AW_PR_HEAD_BASE_SHA="${GH_AW_PR_HEAD_BASE_SHA:-}"
export GH_AW_PR_HEAD_BASE_REPO="${GH_AW_PR_HEAD_BASE_REPO:-}"
export GH_AW_PR_HEAD_BASE_PR_NUMBER="${GH_AW_PR_HEAD_BASE_PR_NUMBER:-}"
export GH_AW_PR_HEAD_BASE_REF="${GH_AW_PR_HEAD_BASE_REF:-}"
export GH_AW_PR_HEAD_REPO="${GH_AW_PR_HEAD_REPO:-}"
export DEBUG="*"

export GH_AW_ENGINE="copilot"
MCP_GATEWAY_UID=$(id -u 2>/dev/null || echo '0')
MCP_GATEWAY_GID=$(id -g 2>/dev/null || echo '0')
source "${RUNNER_TEMP}/gh-aw/actions/resolve_docker_socket_gid.sh"
export MCP_GATEWAY_DOCKER_COMMAND='docker run -i --rm --network bridge -p 127.0.0.1:'"${MCP_GATEWAY_PORT}"':'"${MCP_GATEWAY_PORT}"' --name awmg-mcpg --add-host host.docker.internal:host-gateway --user '"${MCP_GATEWAY_UID}"':'"${MCP_GATEWAY_GID}"' --group-add '"${DOCKER_SOCK_GID}"' -v '"${DOCKER_SOCK_PATH}"':/var/run/docker.sock -e MCP_GATEWAY_PORT -e MCP_GATEWAY_DOMAIN -e MCP_GATEWAY_AGENT_ID -e MCP_GATEWAY_PAYLOAD_DIR -e MCP_GATEWAY_PAYLOAD_SIZE_THRESHOLD -e DOCKER_HOST=unix:///var/run/docker.sock -e DEBUG -e MCP_GATEWAY_LOG_DIR -e GH_AW_MCP_LOG_DIR -e GH_AW_SAFE_OUTPUTS -e GH_AW_SAFE_OUTPUTS_CONFIG_PATH -e GH_AW_SAFE_OUTPUTS_TOOLS_PATH -e GH_AW_PR_HEAD_BASE_BRANCH -e GH_AW_PR_HEAD_BASE_SHA -e GH_AW_PR_HEAD_BASE_REPO -e GH_AW_PR_HEAD_BASE_PR_NUMBER -e GH_AW_PR_HEAD_BASE_REF -e GH_AW_PR_HEAD_REPO -e GH_AW_POLICY_ALLOW_CREATE_PULL_REQUEST -e GH_AW_ASSETS_BRANCH -e GH_AW_ASSETS_MAX_SIZE_KB -e GH_AW_ASSETS_ALLOWED_EXTS -e DEFAULT_BRANCH -e GITHUB_MCP_SERVER_TOKEN -e GITHUB_MCP_GUARD_MIN_INTEGRITY -e GITHUB_MCP_GUARD_REPOS -e GH_AW_SINK_VISIBILITY -e GITHUB_REPOSITORY -e GITHUB_SERVER_URL -e GITHUB_SHA -e GITHUB_WORKSPACE -e GITHUB_TOKEN -e GITHUB_RUN_ID -e GITHUB_RUN_NUMBER -e GITHUB_RUN_ATTEMPT -e GITHUB_JOB -e GITHUB_ACTION -e GITHUB_EVENT_NAME -e GITHUB_EVENT_PATH -e GITHUB_ACTOR -e GITHUB_ACTOR_ID -e GITHUB_TRIGGERING_ACTOR -e GITHUB_WORKFLOW -e GITHUB_WORKFLOW_REF -e GITHUB_WORKFLOW_SHA -e GITHUB_REF -e GITHUB_REF_NAME -e GITHUB_REF_TYPE -e GITHUB_HEAD_REF -e GITHUB_BASE_REF -e RUNNER_TEMP -e RUNNER_TOOL_CACHE -e MCP_GATEWAY_ALLOWED_MOUNT_ROOTS -e GITHUB_AW_OTEL_TRACE_ID -e GITHUB_AW_OTEL_PARENT_SPAN_ID -e OTEL_EXPORTER_OTLP_HEADERS -v /tmp/gh-aw/mcp-payloads:/tmp/gh-aw/mcp-payloads:rw -v /opt:/opt:ro -v /tmp:/tmp:rw -v '"${GITHUB_WORKSPACE}"':'"${GITHUB_WORKSPACE}"':rw -v '"${RUNNER_TEMP}"'/gh-aw/safeoutputs:'"${RUNNER_TEMP}"'/gh-aw/safeoutputs:rw ghcr.io/github/gh-aw-mcpg:v0.4.25'

mkdir -p "$HOME/.copilot"
"$CF_PYTHON" -I "$CF_HELPER" --produce | "$CF_NODE" "${RUNNER_TEMP}/gh-aw/actions/start_mcp_gateway.cjs"

