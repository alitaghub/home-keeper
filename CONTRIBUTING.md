# Contributing

## Setup

Install [mise](https://mise.jdx.dev/), then from the repository root:

```sh
mise install        # Python, uv, Node.js, pnpm, actionlint, zizmor, lefthook, jq
mise run setup      # dependencies (pnpm, uv) and the lefthook pre-commit hook
mise run check      # should pass on a clean checkout
```

The pre-commit hook formats and lints staged files only (Ruff for Python, Biome
for the web app). Types and tests run in `mise run check` and CI. You need Docker
with Compose for `mise run dev` and `mise run e2e`.

### VS Code

Accept the recommended extensions and disable Pylance for this workspace;
basedpyright reports the same type errors as CI.

## Coding agents

[AGENTS.md](AGENTS.md) holds the rules for any coding agent; `CLAUDE.md` links to
it. For Claude Code, `.claude/settings.json` adds hooks from `.claude/hooks/`:

- On startup, install the locked dependencies when `.venv` or `node_modules` is
  missing, as in a fresh clone or worktree.
- Block `git commit` and `git push` with `--no-verify`, so the lefthook checks run.
- Format each edited file with Ruff or Biome.
- When Claude stops, type-check uncommitted files with basedpyright and tsc and
  report errors back to it. This never blocks; `mise run check` is the gate.

Put personal Claude settings in `.claude/settings.local.json`, which git ignores.

`.worktreeinclude` makes Claude Code copy your `.env` into each new worktree it
creates. The copy does not follow later edits: after you change the main `.env`,
copy it again into worktrees that already exist.

## Environment variables

Each service reads its variables through one settings class, such as
`apps/keeper/src/home_keeper/settings.py`. When you add or rename one, update the
service's `environment` in Compose and, if users set it, `.env.example`.
`mise run check:env` fails when Compose sets a variable the service's settings
never read, or when `.env.example` and the variables Compose reads from your
shell differ.

## Workflow

1. Branch from `main`.
2. Make the change. Run `mise run check`. If you touched Docker, Compose
   or the UI, also run `mise run e2e`.
3. If you changed a public `/api/*` route, run `mise run gen:api` and commit the
   regenerated `apps/web/src/api/schema.d.ts`. CI fails when it is stale.
4. Open a pull request against `main` and fill in the template: the problem, what
   behaves differently, where to look hardest, and the proof (what you ran, and
   what you did not). For a UI change, add before and after screenshots with
   `mise run pr:images -- --pr <number> <image>...`. It commits them to
   `refs/screenshots/pr-<number>` and adds them under the description's
   Screenshots heading.

`main` takes changes through pull requests only.

### Pull request titles

PR titles follow [Conventional Commits](https://www.conventionalcommits.org/),
for example `fix(keeper): answer 503 when the reader times out`. The
`PR title` workflow checks this. Allowed types:

`feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`,
`chore`, `revert`.

## CI

`.github/workflows/ci.yaml` runs on every pull request and on pushes to `main`.
Each job runs one mise task, so a green local run means a green CI run.

| Job          | Runs             | What it checks                                                        |
| ------------ | ---------------- | --------------------------------------------------------------------- |
| `Check`      | `mise run check` | Lint, format, types, tests with coverage, build, API types, environment variables, workflows |
| `End-to-end` | `mise run e2e`   | Demo, own-HA, bundled-HA, agent and control Compose smoke scripts, Playwright |

On failure, `End-to-end` uploads the Compose logs and the Playwright report.

Two more workflows run beside CI: `CodeQL` (Python and TypeScript, also weekly)
and `PR title`.

All actions are pinned to a commit SHA with a version comment. Keep it that way
when you add one; zizmor flags unpinned actions.

## Branch protection

`main` has this protection in the repository settings:

- The `Check`, `End-to-end` and `Conventional commit title` status checks must pass.
- The branch must be up to date with `main`. Rebase before you merge.
- A pull request is required. No direct pushes.
- History stays linear. Merge with **Squash and merge** or **Rebase and merge**;
  GitHub rejects merge commits.
- Force-pushes and branch deletion are blocked.

## Dependency updates

`renovate.json` opens one weekly pull request with every non-major update, on
Monday mornings, and separate pull requests for major updates. Lockfile
maintenance is on, and a release must be three days old. Nothing merges
automatically.

The config does nothing until the maintainer installs the
[Renovate GitHub App](https://github.com/apps/renovate) on the repository.
That is a manual step.
