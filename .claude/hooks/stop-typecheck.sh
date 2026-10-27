#!/usr/bin/env bash
# Stop: type-check uncommitted changes with the same strict tools CI runs, so Claude hears about
# type errors before it calls the work done. Advisory: findings go back as additionalContext and
# never block. Skipped on a continuation turn, so errors Claude chose to leave cannot loop.
# settings.json caps the run time.
set -u
input=$(cat)
[ "$(printf '%s' "$input" | jq -r '.stop_hook_active // false' 2>/dev/null)" = "true" ] && exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

# -uall lists new files one by one; keep the target side of a rename.
changed=$(git status --porcelain -uall 2>/dev/null | sed 's/^...//; s/^.* -> //')
py=$(printf '%s\n' "$changed" | grep -E '^(apps/keeper/(src|tests)|e2e|tests)/.*\.py$' || true)
web=$(printf '%s\n' "$changed" | grep -E '^apps/web/.*\.(ts|tsx)$' || true)
out=""

if [ -n "$py" ] && [ -x .venv/bin/basedpyright ]; then
  # A read loop, not mapfile: macOS ships Bash 3.2.
  files=()
  while IFS= read -r f; do [ -f "$f" ] && files+=("$f"); done <<< "$py"
  if [ "${#files[@]}" -gt 0 ] && ! err=$(.venv/bin/basedpyright "${files[@]}" 2>&1); then
    out+="basedpyright:"$'\n'"$(printf '%s\n' "$err" | grep -E ' - error' | head -8)"$'\n'
  fi
fi
if [ -n "$web" ] && [ -x apps/web/node_modules/.bin/tsc ]; then
  if ! err=$(cd apps/web && pnpm --silent typecheck 2>&1); then
    out+="tsc (apps/web):"$'\n'"$(printf '%s\n' "$err" | grep -E 'error TS' | head -8)"$'\n'
  fi
fi

[ -n "$out" ] || exit 0
printf 'Type errors in uncommitted files (advisory; `mise run check` is the real gate):\n%s' "$out" |
  jq -Rs '{hookSpecificOutput: {hookEventName: "Stop", additionalContext: .}}'
exit 0
