# Configuration

Home Keeper reads its settings from environment variables. Compose reads
`.env` in the repository root automatically; Git ignores it.
[.env.example](../.env.example) lists every variable you can set.

## Compose files

| Command                                                   | Stack                                        | Project            |
| --------------------------------------------------------- | -------------------------------------------- | ------------------ |
| `docker compose …`                                        | Home stack with a bundled Home Assistant     | `home-keeper`      |
| `docker compose -f compose.yaml -f compose.external.yaml …` | Home stack with your own Home Assistant    | `home-keeper`      |
| `docker compose -f compose.demo.yaml …`                   | Synthetic demo                               | `home-keeper-demo` |

The home stack and the demo use separate projects and volumes. The API refuses
a database written for the other source. Both publish port 8000, so stop one
before you start the other.

## Profiles

| Profile    | Adds                     | Requires                                  |
| ---------- | ------------------------ | ----------------------------------------- |
| `control`  | `controller`             | A control token (bundled setup creates it) |
| `agent`    | `agent`                  | Model settings                            |
| `telegram` | `telegram`               | The `agent` profile and bot settings      |

## Variables

### Ports

All ports bind to `127.0.0.1`.

| Variable           | Default | Service                   |
| ------------------ | ------- | ------------------------- |
| `HOME_KEEPER_PORT` | `8000`  | Dashboard (`web`)         |
| `HA_PORT`          | `8123`  | Bundled Home Assistant    |
| `AGENT_PORT`       | `2026`  | Agent (Aegra)             |

### History and commands

| Variable                     | Default | Description                                                      |
| ---------------------------- | ------- | ---------------------------------------------------------------- |
| `HOME_KEEPER_TIMEZONE`       | `UTC`   | IANA time zone for usage days and hours. An unknown name stops the API. |
| `HOME_KEEPER_RETENTION_DAYS` | `30`    | Days the command log is kept. Device history follows Home Assistant's recorder. |

### Your own Home Assistant

Used with `compose.external.yaml`. See
[home-assistant.md](home-assistant.md#use-your-own-home-assistant).

| Variable                            | Required             | Description                                         |
| ----------------------------------- | -------------------- | --------------------------------------------------- |
| `HOME_KEEPER_HA_URL`                | Yes                  | Home Assistant origin, reachable from the container |
| `HOME_KEEPER_HA_TOKEN_FILE`         | Yes                  | Absolute path to the reader token file              |
| `HOME_KEEPER_HA_PUBLIC_URL`         | No                   | The address your browser uses, for dashboard links  |
| `HOME_KEEPER_HA_CONTROL_TOKEN_FILE` | With `control`       | Absolute path to the control token file             |

### Agent

See [agent.md](agent.md#set-the-model).

| Variable                | Default              | Description                         |
| ----------------------- | -------------------- | ----------------------------------- |
| `AGENT_MODEL`           | `azure:gpt-6.1-sol`  | `provider:model`                    |
| `AZURE_OPENAI_ENDPOINT` | —                    | Azure OpenAI resource URL           |
| `AZURE_OPENAI_API_KEY`  | —                    | Azure OpenAI key                    |
| `OPENAI_API_KEY`        | —                    | Key for an `openai:` model          |

### Telegram

See [telegram.md](telegram.md).

| Variable                    | Required | Description                                                           |
| --------------------------- | -------- | --------------------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`        | Yes      | Token from BotFather                                                  |
| `TELEGRAM_ALLOWED_USER_IDS` | Yes      | Comma-separated numeric user IDs that may use the bot                 |
| `TELEGRAM_CONTROL_USER_IDS` | No       | IDs that may switch devices. Each must also be allowed. Empty means nobody. |

## Data

`docker compose down` keeps all data. `docker compose down --volumes` deletes it.

| Volume                 | Contents                                                    |
| ---------------------- | ----------------------------------------------------------- |
| `postgres-data`        | Monitored devices, command log, agent chats, Telegram state |
| `home-assistant-config`| The bundled Home Assistant, including its history           |
| `ha-token`, `ha-control-token`, `ha-owner` | Home Assistant credentials             |
| `service-secret`       | The internal service credential                             |
| `keeper-db-password`, `agent-db-password`, `telegram-db-password` | Database passwords |

### Upgrade from a SQLite release

Releases before Postgres kept their data in SQLite. Home Keeper does not import
it:

- Select your devices again. History still comes from Home Assistant.
- Chats and Telegram state start empty.
- The old `data`, `agent-postgres` and `telegram-state` volumes are unused.
  Delete them with `docker volume rm` when you no longer need them.

## Add or rename a variable

Each service reads its variables through one settings class, such as
`apps/keeper/src/home_keeper/settings.py`. When you add or rename a variable,
update the service's `environment` in Compose and, if users set it,
`.env.example`. `mise run check:env` fails when they drift.
