# Home Observer

[![CI](https://github.com/alitaghub/home-observer/actions/workflows/ci.yaml/badge.svg)](https://github.com/alitaghub/home-observer/actions/workflows/ci.yaml)

A local smart-home monitor with explicit device discovery and selection. Run a
hardware-free synthetic demo, or connect an existing Home Assistant instance and
record selected individual Hue lights. Nothing is monitored until you select it.
There is no AI agent or device control yet.

## Start the demo

Install Docker Engine with Compose on Linux, or Docker Desktop with Compose on
Windows/WSL2 or macOS. From this repository:

```sh
docker compose up --build --wait
```

Open <http://127.0.0.1:8000> and click **Monitor** on a lamp. If port 8000 is
taken, publish on another loopback port:

```sh
OBSERVER_PORT=18000 docker compose up --build --wait
```

Stop the stack with `docker compose down`. Selected devices and history stay in
named volumes. [docs/demo.md](docs/demo.md) explains what the demo shows and how
to restart or reset it.

## Use your own home

Home Observer reads from Home Assistant, not from the Hue Bridge directly. It
needs a Home Assistant token stored in a file outside this repository.

- [docs/home-assistant.md](docs/home-assistant.md): guided local setup, connecting
  an existing Home Assistant with Hue, and restarting or stopping live mode.
- [docs/networking.md](docs/networking.md): which address the collector container
  can reach on WSL2, macOS and Linux, and what has been tested.
- [docs/live-test-checklist.md](docs/live-test-checklist.md): the Phase 1 check
  against one real Hue light.

## How it works

- [docs/architecture.md](docs/architecture.md): the collectors, the API and who
  writes to SQLite.
- [docs/collection.md](docs/collection.md): what each collection pass records,
  the evidence limits, retention, and how gaps and recovery work.
- [docs/security.md](docs/security.md): the security boundary around the token,
  the API and the dashboard.
- [docs/verification-history.md](docs/verification-history.md): what each story
  verified, and what is still untested.

## Repository map

| Path            | What lives there                                                          |
| --------------- | ------------------------------------------------------------------------- |
| `apps/observer` | Python service: FastAPI API, demo and live collectors, unit and integration tests |
| `apps/web`      | Dashboard: Vite + React + TypeScript, API types generated from OpenAPI    |
| `e2e`           | Compose smoke scripts, a fake Home Assistant, and the Playwright suite    |
| `scripts`       | Guided Home Assistant setup (`start-home.sh`, `connect-home.sh`)          |
| `docs`          | Operator and design docs                                                  |
| `packages`      | Reserved for shared libraries once a second app needs one. Empty today.   |

Python uses a uv workspace (root `pyproject.toml`, `uv.lock`). The web side uses
a pnpm workspace (root `package.json`, `pnpm-workspace.yaml`). The Docker image
builds the dashboard in a Node stage, and FastAPI serves it.

## Develop

[mise](https://mise.jdx.dev/) pins every tool (Python 3.13, uv, Node.js 24,
pnpm, actionlint, zizmor, lefthook) and defines the tasks.

```sh
mise run setup      # install tools and dependencies, then the pre-commit hook
mise run check      # everything CI runs except the Compose e2e
mise run dev        # demo stack, then the Vite dev server with /api proxied to it
mise run e2e        # Compose smoke scripts and Playwright against a fresh stack
mise run gen:api    # regenerate apps/web/src/api/schema.d.ts after an API change
```

`mise tasks` lists the rest (`fmt`, `lint`, `typecheck`, `test`). Set
`OBSERVER_PORT` for `dev` and `e2e` too when 8000 is taken, for example
`OBSERVER_PORT=18000 mise run e2e`.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before you open a pull request. Coding
agents start at [AGENTS.md](AGENTS.md).

## License

[MIT](LICENSE). This independent project uses original code, public documentation
and synthetic examples. It contains no employer code or home credentials.
