#!/usr/bin/env bash
# PreToolUse(Bash) — block `git commit/push --no-verify` (and `commit -n`, its short form).
# Safeguards against false positives (the phrase inside a message, heredoc,
# comment, or sed string rather than a real invocation):
#   1. drop herestrings and heredoc bodies before matching
#   2. un-quote a quoted "--no-verify" first (it still reaches git as the real
#      flag), then strip remaining single/double-quoted spans
#   3. require the git call to START a command segment (^ or after ; | && ),
#      allowing `command`/`env` prefixes and git's -C/-c/--git-dir globals
set -u
payload=$(cat)
cmd=$(printf '%s' "$payload" | jq -r '.tool_input.command // empty' 2>/dev/null) || exit 0
[ -n "$cmd" ] || exit 0
cmd=$(printf '%s' "$cmd" | sed "s/[\"']--no-verify[\"']/--no-verify/g")
stripped=$(printf '%s' "$cmd" \
  | sed 's/<<<[[:space:]]*[^[:space:]]*//g' \
  | awk '
    indoc { line=$0; sub(/^\t*/, "", line); if (line == delim) indoc=0; next }
    {
      if (match($0, /(^|[^<])<<-?[[:space:]]*["'\'']?[A-Za-z0-9_]+/)) {
        delim = substr($0, RSTART, RLENGTH)
        sub(/^.*<<-?[[:space:]]*["'\'']?/, "", delim)
        indoc = 1
      }
      print
    }' \
  | sed "s/'[^']*'//g; s/\"[^\"]*\"//g")

env_pre='(command[[:space:]]+|env[[:space:]]+([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*)?'
git_glob='([[:space:]]+(-[Cc]|--git-dir(=[^[:space:]]+)?|--work-tree(=[^[:space:]]+)?|--no-pager)([[:space:]]+[^[:space:];|&]+)?)*'
git_seg="(^|[;|]|&&)[[:space:]]*${env_pre}git${git_glob}[[:space:]]+"
deny() { echo "Blocked: $1 bypasses git hooks. Fix the failing hook instead." >&2; exit 2; }
if printf '%s' "$stripped" | grep -Eq "${git_seg}(commit|push)([[:space:]][^;|&]*)?--no-verify"; then
  deny '--no-verify'
fi
# short form: `git commit -n`, including bundled clusters like -an / -sn
if printf '%s' "$stripped" | grep -Eq "${git_seg}commit([[:space:]][^;|&]*)?[[:space:]]-[A-Za-z]*n[A-Za-z]*([[:space:]]|$)"; then
  deny '-n (short for --no-verify)'
fi
exit 0
