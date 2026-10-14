#!/usr/bin/env bash
# SessionStart(startup): a fresh clone or worktree has no .venv or node_modules, so the
# format hook, type checks and tests fail in confusing ways. Install the locked dependencies.
# Stdout goes into Claude's context.
set -u
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
[ -d .venv ] && [ -d node_modules ] && exit 0

if ! command -v uv >/dev/null 2>&1 || ! command -v pnpm >/dev/null 2>&1; then
  echo "Dependencies are missing and uv or pnpm is not on PATH. Run \`mise install && mise run setup\`."
  exit 0
fi
if uv sync --locked --quiet >/dev/null 2>&1 && pnpm install --frozen-lockfile --silent >/dev/null 2>&1; then
  echo "Installed locked Python and web dependencies. \`mise run e2e\` also needs Chromium: run \`mise run setup\` once."
else
  echo "Installing dependencies failed. Run \`mise run setup\` and read its output."
fi
exit 0
