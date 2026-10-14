# Home Observer

[![CI](https://github.com/alitaghub/home-observer/actions/workflows/ci.yaml/badge.svg)](https://github.com/alitaghub/home-observer/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Home Observer records what your smart-home devices actually do. It reads them
through [Home Assistant](https://www.home-assistant.io/), keeps the evidence on
your machine, and never sends a command to a device.

The long-term goal is an agent that understands your home and runs it for you.
An agent can only act well on a home it can see, so this project starts with
seeing: a trustworthy, typed record of state changes, with honest gaps where
collection stopped.

![The Home Observer dashboard with two monitored lamps](docs/images/dashboard.png)

## How it works

```mermaid
flowchart LR
    subgraph home["Your home"]
        hue["Hue lights"] --> ha["Home Assistant"]
    end

    subgraph observer["Home Observer"]
        collector["Collector"] --> api["API"] --> db[("SQLite")]
    end

    ha -- "read only" --> collector
    dashboard["Dashboard"] <--> api
```

1. The **collector** discovers devices in Home Assistant and lists them in the
   dashboard.
2. You pick the lights to monitor. Nothing is recorded before you do.
3. The collector takes a baseline snapshot of each selected light, then streams
   its state changes: on/off, brightness, color, availability.
4. The **API** validates every record and stores it in SQLite. If collection
   stops, it records a gap instead of guessing what happened.
5. The **dashboard** shows current state, a timeline of changes, and every gap.

[docs/architecture.md](docs/architecture.md) has the full picture, the selection
flow and the terms the code uses.

## Design principles

- **Read-only.** The connector makes fixed reads and one event subscription. It
  has no code path that changes a device.
- **Opt-in.** Discovery is not permission. Each light is recorded only after you
  select it, and a removal takes effect even if a slow read is still in flight.
- **Evidence, not guesses.** Every event keeps three timestamps: when Home
  Assistant fired it, when the collector received it, and when the API stored it.
  Interruptions show up as gaps. The app never invents a change it did not see.
- **Local.** Everything binds to `127.0.0.1`. The Home Assistant token stays in
  the collector container. Nothing goes to a cloud service.

## Try the demo

The demo runs a synthetic home: no hardware and no Home Assistant needed. You
need Docker with Compose.

```sh
docker compose up --build --wait
```

Open <http://127.0.0.1:8000> and click **Monitor** on a lamp. Changes appear in
**Recorded changes** within a few seconds. If port 8000 is taken, run
`OBSERVER_PORT=18000 docker compose up --build --wait` instead.

Stop it with `docker compose down`. Your selections and history stay in Docker
volumes. [docs/demo.md](docs/demo.md) explains what the demo shows.

## Connect your home

Home Observer talks to Home Assistant, not to the Hue Bridge. If you don't run
Home Assistant yet, a guided script sets one up in a container:

```sh
./scripts/start-home.sh
```

If you already have one, follow [docs/home-assistant.md](docs/home-assistant.md)
to create a token and start live mode.

## Documentation

| Guide                                            | Read it to                                                         |
| ------------------------------------------------ | ------------------------------------------------------------------ |
| [Architecture](docs/architecture.md)             | Understand the parts, the data flow, and the domain terms          |
| [Demo](docs/demo.md)                             | See what the synthetic home does and how to reset it               |
| [Home Assistant and Hue](docs/home-assistant.md) | Connect a real home and check it against a real light              |
| [Collection and gaps](docs/collection.md)        | Learn what each record proves, how gaps work, and retention        |
| [Networking](docs/networking.md)                 | Pick an address the collector can reach on WSL2, macOS or Linux    |
| [Security](docs/security.md)                     | Understand the trust boundaries around the token, API and browser  |

## Repository layout

| Path            | What lives there                                                   |
| --------------- | ------------------------------------------------------------------ |
| `apps/observer` | Python service: FastAPI API, collectors, SQLite store, tests        |
| `apps/web`      | Dashboard: React and TypeScript, API types generated from OpenAPI  |
| `e2e`           | Compose smoke tests, a fake Home Assistant, Playwright specs       |
| `scripts`       | Guided Home Assistant setup                                        |
| `docs`          | User and design docs                                               |

## Develop

[mise](https://mise.jdx.dev/) installs every tool at a pinned version and runs
the tasks.

```sh
mise run setup      # tools, dependencies, and the pre-commit hook
mise run check      # lint, types, tests, build: what CI runs
mise run dev        # demo stack plus the Vite dev server
mise run e2e        # Docker stack, smoke tests, Playwright
```

Read [CONTRIBUTING.md](CONTRIBUTING.md) before you open a pull request. Coding
agents start at [AGENTS.md](AGENTS.md).

## License

[MIT](LICENSE). This independent project uses original code, public documentation
and synthetic examples. It contains no employer code or home credentials.
