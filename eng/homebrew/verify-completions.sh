#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: verify-completions.sh HOMEBREW_PREFIX" >&2
  exit 1
fi

brew_prefix="$1"
completion_files=(
  "$brew_prefix/etc/bash_completion.d/aspire"
  "$brew_prefix/share/zsh/site-functions/_aspire"
  "$brew_prefix/share/fish/vendor_completions.d/aspire.fish"
  "$brew_prefix/share/pwsh/completions/_aspire.ps1"
)

# Homebrew can warn on completion generation failures without failing installation.
# Check its output so a successful brew install cannot hide missing completions.
for completion_file in "${completion_files[@]}"; do
  if [[ ! -s "$completion_file" ]]; then
    echo "Error: completion file missing or empty after install: $completion_file" >&2
    exit 1
  fi
done

if command -v pwsh >/dev/null 2>&1; then
  ASPIRE_HOMEBREW_COMPLETION_FILE="$brew_prefix/share/pwsh/completions/_aspire.ps1" \
    pwsh -NoLogo -NoProfile -NonInteractive -Command '
      $ErrorActionPreference = "Stop"
      . $env:ASPIRE_HOMEBREW_COMPLETION_FILE
      $line = "aspire comp"
      $matches = (TabExpansion2 $line $line.Length).CompletionMatches
      if ($matches.Count -ne 1 -or $matches[0].CompletionText -ne "completions") {
        throw "Homebrew PowerShell completion loader did not register the CLI completer."
      }
    '
else
  echo "PowerShell is unavailable; skipping the loader execution check."
fi
