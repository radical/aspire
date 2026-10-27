#!/usr/bin/env bash

# Preserve a public metadata-only descriptor outside the existing private group.
exec 3>&1
provider_gateway_stage=prepare
provider_gateway_log=$1
provider_gateway_reporter=$2

provider_gateway_exit()
{
    local status=$?
    trap - EXIT
    if [ "${BASH_SUBSHELL:-0}" -ne 0 ]; then
        exit "$status"
    fi

    local stage=unknown
    case "${provider_gateway_stage:-unknown}" in
        prepare|socket_resolution|launcher_preparation|launcher_pipeline|complete)
            stage=$provider_gateway_stage
            ;;
    esac

    # Early shell termination, including nounset on some Bash versions, can
    # reach EXIT with status zero. Only the marker after the group permits zero.
    if [ "$status" -eq 0 ] && [ "$stage" != complete ]; then
        status=1
    fi
    local reported=0
    if python3 "$provider_gateway_reporter" --log "$provider_gateway_log" --stage "$stage" --status "$status" 2>/dev/null >&3; then
        :
    else
        reported=$?
        # Exit 3 means the reader already emitted an unknown-capture record.
        # Other failures get only this fixed fallback; never print interpreter errors.
        if [ "$reported" -ne 3 ]; then
            printf '{"boundary":"gateway_startup","stage":"%s","last_logged_node_stage":"unknown","reason":"unknown","status":%d,"capture":"unknown","inference_authorized":false}\n' "$stage" "$status" 2>/dev/null >&3 || :
        fi
        if [ "$status" -eq 0 ]; then
            status=1
        fi
    fi
    exit "$status"
}

trap provider_gateway_exit EXIT
