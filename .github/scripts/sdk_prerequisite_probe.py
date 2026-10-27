"""Verify the enrolled source SDK inside the previously probed Linux boundary."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

import isolation_probe as boundary


SOURCE_SHA = "934fbbbe0942da5025abdfefa93edca1ad9ec633"
SDK_VERSION = "11.0.100-rc.1.26425.128"
SDK_SIZE = 207623687
SDK_SHA512 = (
    "608518577aab9db33db92ebd74f0fc9bfbcc7b3d473619f4731aca2edba80af6"
    "c571a38d5be6994862fc4679f6f3d13db767712fcb6f70a7683974a18824a0cc"
)
SDK_URL = (
    f"https://builds.dotnet.microsoft.com/dotnet/Sdk/{SDK_VERSION}/"
    f"dotnet-sdk-{SDK_VERSION}-linux-x64.tar.gz"
)


def validate_result(result):
    if (result["exit_code"] != 0 or result["timed_out"] or result["output_limited"]
            or not result["container_stopped"]):
        raise ValueError("SDK execution failed or confirmed termination is missing")
    # The fixed probe emits SDK_VERSION=11.0.100-rc.1.26425.128 followed by
    # dotnet --list-runtimes rows such as Microsoft.NETCore.App 10.0.5 [/...].
    lines = result["stdout"].splitlines()
    versions = [line for line in lines if line.startswith("SDK_VERSION=")]
    if versions != [f"SDK_VERSION={SDK_VERSION}"]:
        raise ValueError("The exact enrolled-source SDK was not selected")
    for runtime in ("Microsoft.NETCore.App", "Microsoft.AspNetCore.App"):
        if not any(len(parts := line.split()) >= 2
                   and parts[0] == runtime and parts[1].startswith("10.") for line in lines):
            raise ValueError(f"Required .NET 10 runtime is missing: {runtime}")


def main(output):
    if os.environ.get("GITHUB_REPOSITORY") != "radical/aspire":
        raise ValueError("SDK prerequisite experiment is restricted to the test fork")
    output.mkdir(parents=True, exist_ok=False)
    global_url = (
        f"https://raw.githubusercontent.com/radical/aspire/{SOURCE_SHA}/global.json")
    subprocess.run([
        "curl", "--disable", "--fail", "--silent", "--show-error", "--location",
        "--max-time", "60", "--max-filesize", "32768",
        "--output", str(output / "source-global.json"), global_url,
    ], check=True, timeout=70)
    source_global = (output / "source-global.json").read_bytes()
    if json.loads(source_global)["sdk"]["version"] != SDK_VERSION:
        raise ValueError("The source SDK differs from the verified archive pin")
    provenance = {
        "enrolled_source_sha": SOURCE_SHA, "global_json_url": global_url,
        "sdk_version": SDK_VERSION, "sdk_archive_url": SDK_URL,
        "sdk_archive_size": SDK_SIZE, "sdk_archive_sha512": SDK_SHA512,
        "checksum_source": SDK_URL + ".sha512",
        "sdk10_base_image": boundary.IMAGE,
        "claim": "SDK selection and runtime availability only; no Aspire test executed",
        "accepted_test_counts": None,
    }
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    try:
        with tempfile.TemporaryDirectory(prefix="aspire-sdk-prerequisite-") as directory:
            root = Path(directory)
            context = root / "image"
            context.mkdir()
            archive = context / "sdk.tar.gz"
            subprocess.run([
                "curl", "--disable", "--fail", "--silent", "--show-error", "--location",
                "--max-time", "180", "--max-filesize", str(SDK_SIZE),
                "--output", str(archive), SDK_URL,
            ], check=True, timeout=190)
            if archive.stat().st_size != SDK_SIZE:
                raise ValueError("SDK archive size differs from the verified vendor artifact")
            with archive.open("rb") as stream:
                if hashlib.file_digest(stream, "sha512").hexdigest() != SDK_SHA512:
                    raise ValueError("SDK vendor checksum mismatch")
            (context / "Dockerfile").write_text(
                f"FROM {boundary.IMAGE}\n"
                "COPY sdk.tar.gz /tmp/sdk.tar.gz\n"
                "RUN tar -xzf /tmp/sdk.tar.gz -C /usr/share/dotnet "
                "&& rm /tmp/sdk.tar.gz\n")
            tag = f"aspire-sdk-prerequisite:{uuid.uuid4().hex}"
            built = boundary.docker(
                "build", "--network", "none", "--tag", tag, str(context), timeout=600)
            (output / "image-build.log").write_text(built.stdout + built.stderr)
            image = json.loads(boundary.docker("image", "inspect", tag).stdout)[0]["Id"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
                raise ValueError("Built image did not yield an immutable Docker identity")
            boundary.IMAGE = image
            workspace = boundary.candidate(root, (
                "set -eu\n"
                "export HOME=/tmp/sdk-home DOTNET_CLI_HOME=/tmp/sdk-home\n"
                "export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1\n"
                "selected=$(dotnet --version)\n"
                "printf 'SDK_VERSION=%s\\n' \"$selected\"\n"
                "dotnet --list-runtimes\n"
                "dotnet --list-sdks\n"
            ))
            (workspace / "global.json").write_bytes(source_global)
            container = boundary.Container(workspace, uuid.uuid4().hex)
            try:
                result = container.execute(30)
                result.update(provenance)
                (output / "host-receipt.json").write_text(
                    json.dumps(result, indent=2) + "\n")
                validate_result(result)
            finally:
                container.remove()
    except subprocess.CalledProcessError as error:
        (output / "command-failure.json").write_text(json.dumps({
            "command": error.cmd, "returncode": error.returncode,
            "stdout": error.stdout, "stderr": error.stderr,
        }, indent=2) + "\n")
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    main(parser.parse_args().output)
