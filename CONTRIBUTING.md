# Contributing

## Setup

Install [mise](https://mise.jdx.dev/), then from the repository root:

```sh
mise install        # Python, uv, Node.js, pnpm, actionlint, zizmor, lefthook
mise run setup      # dependencies (pnpm, uv) and the lefthook pre-commit hook
mise run check      # should pass on a clean checkout
```

The pre-commit hook formats and lints staged files only (Ruff for Python, Biome
for the web app). Types and tests run in `mise run check` and CI. You need Docker
with Compose for `mise run dev` and `mise run e2e`.

## Workflow

1. Branch from `main`.
2. Make the change. Run `mise run check`. If you touched Docker, Compose,
   `scripts/` or the UI, also run `mise run e2e`.
3. If you changed a public `/api/*` route, run `mise run gen:api` and commit the
   regenerated `apps/web/src/api/schema.d.ts`. CI fails when it is stale.
4. Open a pull request against `main` and fill in the template: the problem, the
   change, and the proof (what you ran, and what you did not).

`main` takes changes through pull requests only.

### Pull request titles

PR titles follow [Conventional Commits](https://www.conventionalcommits.org/),
for example `fix(observer): keep gaps open after a failed heartbeat`. The
`PR title` workflow checks this. Allowed types:

`feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`,
`chore`, `revert`.

## CI

`.github/workflows/ci.yaml` runs on every pull request and on pushes to `main`.

| Job             | What it checks                                                     |
| --------------- | ------------------------------------------------------------------ |
| `changes`       | Which areas a pull request touched (pull requests only)            |
| `python`        | Ruff lint and format, basedpyright, pytest with the coverage gate  |
| `web`           | Biome, `tsc`, Vitest, production build                             |
| `contract`      | `mise run check:api`: generated API types match the OpenAPI schema |
| `e2e`           | Compose config, smoke scripts (demo and live), Playwright          |
| `workflow-lint` | actionlint and zizmor on `.github/`                                |
| `ci-ok`         | Fails if any job above failed or was cancelled                     |

On a pull request, a job runs only when its paths changed. A skipped job counts
as a pass, so `ci-ok` always reports and is the one check to require. Pushes to
`main` run every job. Changes to `mise.toml` or `ci.yaml` also run every job.

Two more workflows run beside CI: `CodeQL` (Python and TypeScript, also weekly)
and `PR title`.

All actions are pinned to a commit SHA with a version comment. Keep it that way
when you add one; zizmor flags unpinned actions.

## Branch protection

The maintainer applies this policy to `main` in the repository settings:

- Require the `ci-ok` and `Conventional commit title` status checks to pass.
- Require a pull request before merging. No direct pushes.
- Block force-pushes and branch deletion.

Until it is applied, follow it by hand.

## Dependency updates

`renovate.json` groups updates for Python, JavaScript, GitHub Actions digests,
Docker images and mise tools. It runs weekly on Monday mornings, with lockfile
maintenance on and a three-day minimum release age. Nothing merges
automatically.

The config does nothing until the maintainer installs the
[Renovate GitHub App](https://github.com/apps/renovate) on the repository.
That is a manual step.
