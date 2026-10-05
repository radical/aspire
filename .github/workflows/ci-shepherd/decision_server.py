"""Local stdio transport for the same closed safe-output tool used on Actions."""

import argparse
import json
from pathlib import Path
import sys

import round as contracts


TOOL = {
    "name": "submit_decision",
    "description": "Submit the one closed packet-bound decision; host mode controls effects.",
    "inputSchema": {
        "type": "object",
        "properties": {"decision": {
            "type": "string",
            "description": "The complete JSON decision matching the host packet and supplied policy.",
        }},
        "required": ["decision"],
        "additionalProperties": False,
    },
}


def handle(request, output):
    method = request["method"]
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "ci-shepherd-safeoutputs", "version": "1"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [TOOL]}
    if method != "tools/call":
        raise ValueError("unsupported MCP method")
    params = request["params"]
    if not isinstance(params, dict) or "_meta" in params and not isinstance(params["_meta"], dict):
        raise ValueError("invalid MCP tool request metadata")
    # Valid MCP requests can carry {"_meta":{"progressToken":0}} alongside
    # name/arguments. Transport metadata never becomes decision authorization.
    # https://modelcontextprotocol.io/specification/2025-06-18/basic/utilities/progress
    params = contracts.exact({key: value for key, value in params.items() if key != "_meta"},
                             {"name", "arguments"}, "tool call")
    if params["name"] != TOOL["name"]:
        raise ValueError("only submit_decision is supported")
    arguments = contracts.exact(params["arguments"], {"decision"}, "decision arguments")
    decision = contracts.loads(arguments["decision"])
    # The transport records a proposal only, never calls GitHub or runs code.
    # Exclusive creation also rejects a second submission in this fresh session.
    with Path(output).open("x", encoding="utf-8") as stream:
        json.dump(decision, stream, allow_nan=False)
    Path(output).chmod(0o600)
    return {"content": [{"type": "text", "text": "Decision recorded."}], "isError": False}


def serve(source, sink, output):
    # MCP stdio is newline-delimited JSON-RPC, not Content-Length framing:
    # {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{...}}
    # https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#stdio
    while line := source.readline(contracts.MAX_JSON_BYTES + 1):
        request = contracts.loads(line)
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            raise ValueError("invalid JSON-RPC request")
        if "id" not in request:
            continue
        response = {"jsonrpc": "2.0", "id": request["id"]}
        try:
            response["result"] = handle(request, output)
        except (ValueError, KeyError, TypeError, OSError) as error:
            response["error"] = {"code": -32602, "message": str(error)}
        sink.write(json.dumps(response, allow_nan=False) + "\n")
        sink.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    serve(sys.stdin, sys.stdout, parser.parse_args().output)
