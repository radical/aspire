#!/bin/bash
# setup-local-cli.sh - Set up Aspire CLI and NuGet packages from local artifacts
# Used by polyglot validation Dockerfiles to use pre-built artifacts from the workflow
#
# The artifact is a self-extracting binary that embeds the runtime, dashboard, dcp, etc.
# Bundle extraction happens lazily on first command that needs the layout.

set -e

ARTIFACTS_DIR="/workspace/artifacts"
BUNDLE_DIR="$ARTIFACTS_DIR/bundle"
NUGETS_DIR="$ARTIFACTS_DIR/nugets"
NUGETS_RID_DIR="$ARTIFACTS_DIR/nugets-rid"
ASPIRE_HOME="$HOME/.aspire"

# Install the self-extracting binary
echo "=== Installing Aspire CLI ==="
if [ ! -f "$BUNDLE_DIR/aspire" ]; then
    echo "ERROR: aspire binary not found at $BUNDLE_DIR/aspire"
    ls -la "$BUNDLE_DIR" 2>/dev/null || echo "Bundle directory does not exist"
    exit 1
fi

mkdir -p "$ASPIRE_HOME/bin"
cp "$BUNDLE_DIR/aspire" "$ASPIRE_HOME/bin/"
chmod +x "$ASPIRE_HOME/bin/aspire"
echo "  ✓ Installed to $ASPIRE_HOME/bin/aspire"

# Extract the embedded bundle so runtime/dotnet and other components are available
# Commands like 'aspire init' and 'aspire add' need the bundled dotnet for NuGet operations
echo "=== Extracting bundle ==="
"$ASPIRE_HOME/bin/aspire" setup || {
    echo "ERROR: aspire setup failed"
    exit 1
}

# Set up NuGet hive
echo "=== Setting up NuGet package hive ==="

SHIPPING_DIR="$NUGETS_DIR/Release/Shipping"
if [ ! -d "$SHIPPING_DIR" ]; then
    SHIPPING_DIR="$NUGETS_DIR"
fi

# Auto-detect PR identity from .nupkg filenames (e.g. "Aspire.Cli.13.4.0-pr.16820.g3703c5c4.nupkg")
# so PR-built packages land in the same hive the CLI's CliExecutionContext.Channel resolves to
# ("pr-<N>"). Falls back to "local" for true local-dev builds.
HIVE_LABEL="local"
SAMPLE_NUPKG=$(find "$SHIPPING_DIR" "$NUGETS_RID_DIR" -maxdepth 4 -name "Aspire.Cli.*.nupkg" 2>/dev/null | head -1)
if [ -n "$SAMPLE_NUPKG" ]; then
    SUFFIX=$(basename "$SAMPLE_NUPKG" | sed -nE 's/.*-(pr\.[0-9]+\.[0-9a-g]+).*\.nupkg$/\1/p')
    if [[ "$SUFFIX" =~ ^pr\.([0-9]+)\.[0-9a-g]+$ ]]; then
        HIVE_LABEL="pr-${BASH_REMATCH[1]}"
    fi
fi
HIVE_DIR="$ASPIRE_HOME/hives/$HIVE_LABEL/packages"
echo "  Using hive label: $HIVE_LABEL"
mkdir -p "$HIVE_DIR"

if [ -d "$SHIPPING_DIR" ]; then
    find "$SHIPPING_DIR" -name "*.nupkg" -exec cp {} "$HIVE_DIR/" \;
    echo "  ✓ Copied $(find "$HIVE_DIR" -name "*.nupkg" | wc -l) packages"
fi

if [ -d "$NUGETS_RID_DIR" ]; then
    find "$NUGETS_RID_DIR" -name "*.nupkg" -exec cp {} "$HIVE_DIR/" \;
    echo "  ✓ Copied RID-specific packages"
fi

echo "  Total packages in hive: $(find "$HIVE_DIR" -name "*.nupkg" | wc -l)"

echo ""
echo "=== Aspire CLI setup complete ==="
