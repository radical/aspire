#!/usr/bin/env bash

set -euo pipefail

to_unix_path() {
  local value="$1"
  if command -v cygpath >/dev/null 2>&1 && [[ "$value" =~ ^[A-Za-z]:\\ ]]; then
    cygpath -u "$value"
  else
    printf '%s\n' "$value"
  fi
}

resolve_inputs() {
  local packages_dir
  packages_dir="$(to_unix_path "$1")"
  local expected_version="$2"
  local rid="$3"

  if [ -z "$packages_dir" ] || [ ! -d "$packages_dir" ]; then
    echo "##[error]npmPackagesDir parameter is required and must point to a directory containing the just-built microsoft-aspire-cli*.tgz files."
    exit 1
  fi

  if [ -z "$expected_version" ]; then
    echo "##[error]expectedVersion parameter is required."
    exit 1
  fi

  if [ -z "$rid" ]; then
    echo "##[error]rid parameter is required."
    exit 1
  fi

  echo "##vso[task.setvariable variable=NpmPackagesDir]$packages_dir"
  echo "##vso[task.setvariable variable=NpmExpectedVersion]$expected_version"
  echo "##vso[task.setvariable variable=NpmTestRid]$rid"
}

locate_packages() {
  local packages_dir="$1"
  local expected_version="$2"
  local rid="$3"

  # Bash 3.2 has no mapfile/readarray or associative arrays. Keep the regular
  # indexed array populated through a portable read loop.
  local all_packages=()
  while IFS= read -r package; do
    [ -n "$package" ] && all_packages+=("$package")
  done < <(find "$packages_dir" -type f -name "microsoft-aspire-cli*.tgz" | LC_ALL=C sort -u)

  if [ ${#all_packages[@]} -eq 0 ]; then
    echo "##[error]No microsoft-aspire-cli*.tgz files found under $packages_dir"
    exit 1
  fi

  local pointer=""
  local rid_tarball=""
  local rid_filename="microsoft-aspire-cli-${rid}-${expected_version}.tgz"
  local pointer_filename="microsoft-aspire-cli-${expected_version}.tgz"
  local package
  for package in "${all_packages[@]}"; do
    local base
    base="$(basename "$package")"
    if [ "$base" = "$pointer_filename" ]; then
      pointer="$package"
    elif [ "$base" = "$rid_filename" ]; then
      rid_tarball="$package"
    fi
  done

  if [ -z "$pointer" ]; then
    echo "##[error]Pointer package $pointer_filename was not found under $packages_dir"
    echo "Discovered files:"
    printf '  %s\n' "${all_packages[@]}"
    exit 1
  fi

  if [ -z "$rid_tarball" ]; then
    echo "##[error]RID package $rid_filename was not found under $packages_dir"
    echo "Discovered files:"
    printf '  %s\n' "${all_packages[@]}"
    exit 1
  fi

  echo "Pointer package: $pointer"
  echo "RID package:     $rid_tarball"
  echo "##vso[task.setvariable variable=NpmPointerTarball]$pointer"
  echo "##vso[task.setvariable variable=NpmRidTarball]$rid_tarball"
  echo "##vso[task.setvariable variable=NpmCheckInstall]failed"
  echo "##vso[task.setvariable variable=NpmCheckVersion]failed"
  echo "##vso[task.setvariable variable=NpmCheckLauncher]failed"
  echo "##vso[task.setvariable variable=NpmCheckUninstall]failed"
}

install_validate() {
  local pointer="$1"
  local rid_tarball="$2"
  local expected_version="$3"
  local rid="$4"
  local staging_directory
  staging_directory="$(to_unix_path "$5")"

  local prefix="$staging_directory/npm-validate-prefix"
  local cache="$staging_directory/npm-validate-cache"
  local aspire_cache="$staging_directory/npm-validate-aspire-cache"
  rm -rf "$prefix" "$cache" "$aspire_cache"
  mkdir -p "$prefix" "$cache" "$aspire_cache"

  export NPM_CONFIG_PREFIX="$prefix"
  export NPM_CONFIG_CACHE="$cache"
  export ASPIRE_NPM_CACHE_DIR="$aspire_cache"
  export PATH="$prefix/bin:$prefix:$PATH"

  if command -v aspire >/dev/null 2>&1; then
    echo "##[error]aspire is already on PATH before install ($(command -v aspire)) - test environment is not clean"
    exit 1
  fi

  # The pointer's optional dependencies are not yet public during source-build
  # validation. --offline prevents metadata resolution from hanging on isolated
  # pools, and the timeout keeps any accidental network path bounded.
  local npm_install_args=(
    --foreground-scripts=false
    --no-audit
    --no-fund
    --loglevel=warn
    --offline
    --fetch-timeout=15000
  )

  echo "Installing RID package..."
  npm install -g "${npm_install_args[@]}" "$rid_tarball"

  echo "Installing pointer package..."
  npm install -g "${npm_install_args[@]}" --omit=optional "$pointer"

  if ! command -v aspire >/dev/null 2>&1; then
    echo "##[error]aspire command not found in PATH after npm install"
    ls -la "$prefix/bin" || true
    exit 1
  fi

  local version_output
  version_output="$(aspire --version 2>&1 | tr -d '\r')"
  local version_line
  version_line="$(printf '%s\n' "$version_output" | grep -Eo '^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9._-]+)?(\+[A-Za-z0-9._-]+)?$' | head -n 1 || true)"
  local actual_version="${version_line%+*}"
  echo "  Raw output: $version_output"
  echo "  Matched semver line: $version_line"
  echo "  Comparable version (no +buildmeta): $actual_version"
  if [ -z "$actual_version" ] || [ "$actual_version" != "$expected_version" ]; then
    echo "##[error]aspire --version reported '$actual_version' but expected '$expected_version'"
    exit 1
  fi

  local binary_name="aspire"
  if [[ "$rid" == win-* ]]; then
    binary_name="aspire.exe"
  fi

  local cached_binary="$aspire_cache/$expected_version/$rid/bin/$binary_name"
  if [ ! -f "$cached_binary" ]; then
    echo "##[error]Expected cached binary at $cached_binary was not created by the launcher"
    find "$aspire_cache" -maxdepth 6 -type f || true
    exit 1
  fi
  if [ ! -x "$cached_binary" ]; then
    echo "##[error]Cached binary at $cached_binary is not executable"
    ls -la "$cached_binary"
    exit 1
  fi

  npm uninstall -g "${npm_install_args[@]}" '@microsoft/aspire-cli' "@microsoft/aspire-cli-${rid}"

  if command -v aspire >/dev/null 2>&1; then
    echo "##[error]aspire command still on PATH after uninstall ($(command -v aspire))"
    exit 1
  fi

  echo "##vso[task.setvariable variable=NpmCheckInstall]passed"
  echo "##vso[task.setvariable variable=NpmCheckVersion]passed"
  echo "##vso[task.setvariable variable=NpmCheckLauncher]passed"
  echo "##vso[task.setvariable variable=NpmCheckUninstall]passed"
}

write_summary() {
  local staging_directory
  staging_directory="$(to_unix_path "$1")"
  local rid="$2"
  local expected_version="$3"
  local skip_registry
  skip_registry="$(printf '%s' "$4" | tr '[:upper:]' '[:lower:]')"
  local check_install="$5"
  local check_version="$6"
  local check_launcher="$7"
  local check_uninstall="$8"

  local validated_by_prepare_pipeline=false
  if [ "$check_install" = "passed" ] &&
     [ "$check_version" = "passed" ] &&
     [ "$check_launcher" = "passed" ] &&
     [ "$check_uninstall" = "passed" ]; then
    validated_by_prepare_pipeline=true
  fi

  local output_dir="$staging_directory/npm-validation-summary"
  mkdir -p "$output_dir"
  local output_path="$output_dir/validation-summary.json"

  cat > "$output_path" <<EOF
{
  "schemaVersion": 1,
  "validatedByPreparePipeline": $validated_by_prepare_pipeline,
  "rid": "$rid",
  "expectedVersion": "$expected_version",
  "skipRegistryResolution": $skip_registry,
  "checks": {
    "install": {
      "status": "$check_install",
      "details": "npm install -g <rid>.tgz && npm install -g --omit=optional <pointer>.tgz"
    },
    "version": {
      "status": "$check_version",
      "details": "aspire --version output matched the build version"
    },
    "launcher": {
      "status": "$check_launcher",
      "details": "Launcher cached native binary at ASPIRE_NPM_CACHE_DIR/<version>/<rid>/bin/<binaryName>"
    },
    "uninstall": {
      "status": "$check_uninstall",
      "details": "npm uninstall -g @microsoft/aspire-cli @microsoft/aspire-cli-$rid"
    }
  }
}
EOF

  echo "Wrote npm validation summary to $output_path"
  cat "$output_path"
}

if [ "$#" -eq 0 ]; then
  echo "Usage: $0 <resolve-inputs|locate|install-validate|write-summary> ..." >&2
  exit 2
fi

command_name="$1"
shift

case "$command_name" in
  resolve-inputs)
    resolve_inputs "$@"
    ;;
  locate)
    locate_packages "$@"
    ;;
  install-validate)
    install_validate "$@"
    ;;
  write-summary)
    write_summary "$@"
    ;;
  *)
    echo "Unknown command: $command_name" >&2
    exit 2
    ;;
esac
