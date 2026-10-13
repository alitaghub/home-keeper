# AGENTS.md

Home Observer is a local smart-home monitor. A collector (synthetic demo, or live
Home Assistant + Hue) feeds a FastAPI service that owns SQLite, and a React
dashboard selects which lights to watch. It reads devices and never controls
them. There is no AI agent in the app yet.

## Repo map

- `apps/observer`: Python 3.13 service, package `home_observer` (src layout).
  Tests in `tests/unit` (pure logic), `tests/integration` (FastAPI app, SQLite,
  fake Home Assistant), shared helpers in `tests/support`.
- `apps/web`: Vite + React + TypeScript dashboard. Vitest tests sit beside the
  code. `src/api/schema.d.ts` is generated; never edit it by hand.
- `e2e`: Compose smoke scripts, a fake Home Assistant, and the Playwright suite
  (`e2e/playwright`, config in `apps/web/playwright.config.ts`).
- `scripts`: guided Home Assistant setup. `docs`: operator and design docs.
- `typings`: local type stubs for basedpyright.
- `packages`: reserved for shared libraries once a second app needs one. Empty.

Python is a uv workspace (root `pyproject.toml` holds tool config). The web side
is a pnpm workspace. `mise.toml` pins every tool and defines every task.

## Commands

```sh
mise run setup      # first run: tools, dependencies, pre-commit hook
mise run check      # the gate: lint, format check, types, tests + coverage, API drift, workflow lint
mise run fmt        # Ruff format + Biome format
mise run gen:api    # regenerate apps/web/src/api/schema.d.ts from OpenAPI
mise run e2e        # Compose stack + smoke scripts + Playwright (needs Docker)
mise run dev        # demo stack + Vite dev server
```

Run one Python test with `uv run pytest <path>::<name>`, one web test with
`pnpm --filter @home-observer/web exec vitest run <file>`. Set `OBSERVER_PORT`
when 8000 is taken.

## Rules

- Types are strict on both sides: basedpyright `strict` for Python, TypeScript
  `strict` with `noUncheckedIndexedAccess`. TypeScript has no `any`. Python uses
  `Any` only for raw external data that has not been parsed yet. A suppression
  names its rule and says why:
  `# pyright: ignore[reportPrivateUsage] -- tests replace the transport`.
- Parse external data at the boundary (Home Assistant payloads, HTTP requests,
  environment, files) into typed models. Inside the app, trust the types.
- A public `/api/*` change is a contract change. Run `mise run gen:api` and
  commit `schema.d.ts`. If a response body changes on purpose, refresh the
  snapshots with
  `UPDATE_SNAPSHOTS=1 uv run pytest apps/observer/tests/integration/test_api_contract.py`
  and say why in the PR. Every public route needs a response model.
- Name tests by the behavior they check, and put them at the right level: unit,
  integration, or e2e. Keep every existing assertion when you move a test.
- Names from Home Assistant are untrusted. Render them as text, never HTML.
- Ruff ignores need a one-line reason next to them, like the ones in
  `pyproject.toml`.
- Add a tool or dependency only when the task needs it, and pin it (`mise.toml`,
  `uv.lock`, `pnpm-lock.yaml`).

## Definition of done

- `mise run check` exits 0.
- For changes to Docker, Compose, `scripts/`, or the UI, `mise run e2e` passes too.
- User-visible behavior changes are reflected in `README.md` or `docs/`.

PR conventions and CI jobs are in [CONTRIBUTING.md](CONTRIBUTING.md).
