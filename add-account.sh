#!/usr/bin/env bash
# Usage: ./add-account.sh work
# Creates a separate Claude Code profile at ~/.claude-<name> and logs it in.
# Afterwards use that account in the CLI with:  CLAUDE_CONFIG_DIR=~/.claude-<name> claude
set -euo pipefail
name="${1:?usage: $0 <name>   e.g. work, side, personal}"
dir="$HOME/.claude-$name"
mkdir -p "$dir"
echo "Logging in profile '$name' ($dir) — pick the matching account in the browser."
CLAUDE_CONFIG_DIR="$dir" claude auth login
echo
echo "Done. Tip: add to ~/.bashrc ->  alias claude-$name='CLAUDE_CONFIG_DIR=$dir claude'"
