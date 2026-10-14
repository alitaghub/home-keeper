#!/usr/bin/env bash
# PostToolUse(Edit|Write): format the one file Claude just edited, with the repo's pinned tools.
# Formatting and import order only; lint fixes stay with lefthook and `mise run check`.
# Never blocks: a formatter that fails must not stop the edit.
set -u
file=$(jq -r '.tool_input.file_path // empty' 2>/dev/null) || exit 0
[ -f "$file" ] || exit 0
root=${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)} || exit 0

case "$file" in
  *.py | *.pyi)
    ruff="$root/.venv/bin/ruff"
    [ -x "$ruff" ] || exit 0
    "$ruff" check --select I --fix --quiet "$file" >/dev/null 2>&1
    "$ruff" format --quiet "$file" >/dev/null 2>&1
    ;;
  *.ts | *.tsx | *.js | *.jsx | *.mjs | *.cjs | *.json | *.jsonc | *.css)
    # The native binary: `pnpm exec biome` is slower and crashes under WSL.
    biome="$root/node_modules/.bin/biome"
    [ -x "$biome" ] || exit 0
    "$biome" check --write --linter-enabled=false --no-errors-on-unmatched \
      --files-ignore-unknown=true "$file" >/dev/null 2>&1
    ;;
esac
exit 0
