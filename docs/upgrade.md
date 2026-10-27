# Upgrade from Home Observer

Home Observer is now Home Keeper. An existing install keeps its Home Assistant
setup, token, history, agent conversations and Telegram state, but only if you
copy its Docker volumes to the new names **before the first `up` under the new
name**. If you start first, Compose creates empty volumes, and `ha-setup`
onboards a fresh Home Assistant beside your old one.

What changed:

- The Compose projects are now pinned: the home stack is `home-keeper` and the
  demo is `home-keeper-demo`. Before, the home stack took its name from the
  checkout folder, usually `home-observer`, and the demo was
  `home-observer-demo`. Compose prefixes every volume with the project name, so
  `home-observer_data` becomes `home-keeper_data`.
- Every `OBSERVER_*` variable is now `HOME_KEEPER_*`. Every service refuses to
  start while an old name is still set, and its log names the replacement.

## Steps

1. Stop the old stack, without `--volumes`. Use the old project name, so this
   works before and after you pull:

   ```sh
   docker compose -p home-observer --profile agent --profile telegram down
   ```

   For the demo, use `-p home-observer-demo -f compose.demo.yaml` instead.
   If your checkout folder had another name, that name is the old project.
   `docker volume ls` shows it as the prefix of the volume names.

2. Pull the new version and rename the variables in your `.env` and shell, as
   the [table](#renamed-variables) below lists.

3. Copy the volumes. The script uses only Python's standard library and the
   Docker CLI, so it runs before `mise run setup`:

   ```sh
   python3 scripts/migrate_volumes.py --from home-observer
   ```

   For the demo:

   ```sh
   python3 scripts/migrate_volumes.py --from home-observer-demo --to home-keeper-demo
   ```

   `--from` defaults to the current folder's name and `--to` to `home-keeper`.
   The script copies every volume of the old project, including the agent
   profile's `agent-postgres` and `telegram-state`, and keeps file owners and
   modes. It prints one line per copied volume. It never changes or removes an
   old volume. It copies nothing and exits 1 when a new volume already holds
   data, or when the old project has no volumes.
   An empty new volume without Compose's labels, such as one made with
   `docker volume create`, is removed and recreated with them. Compose treats a
   volume as its own only when it carries them.

4. Start Home Keeper as usual, for example `docker compose up --build --wait`.
   Check that the dashboard shows your monitored lights and their history.

5. When everything works, delete the old volumes. List them first:

   ```sh
   docker volume ls --filter label=com.docker.compose.project=home-observer
   docker volume rm home-observer_data home-observer_service-secret \
     home-observer_home-assistant-config home-observer_ha-token home-observer_ha-owner \
     home-observer_agent-postgres home-observer_telegram-state
   ```

   Leave out the agent volumes if you never used the agent profile. For the
   demo, the volumes are `home-observer-demo_data`,
   `home-observer-demo_service-secret`, and the two agent volumes if you used them.

If you started Home Keeper before you copied the volumes, stop it and delete the
new, still-empty volumes with `docker compose --profile agent --profile telegram
down --volumes`. Then run step 3. Your old volumes are untouched.

## Renamed variables

Each variable kept its suffix. "Compose" means `docker compose` reads it from
`.env` or your shell; "service" means a container or the dev server reads it.

| Old name                          | New name                             | Read by          |
| --------------------------------- | ------------------------------------ | ---------------- |
| `OBSERVER_PORT`                   | `HOME_KEEPER_PORT`                   | Compose, service |
| `OBSERVER_RETENTION_DAYS`         | `HOME_KEEPER_RETENTION_DAYS`         | Compose, service |
| `OBSERVER_RECONCILIATION_SECONDS` | `HOME_KEEPER_RECONCILIATION_SECONDS` | Compose, service |
| `OBSERVER_TIMEZONE`               | `HOME_KEEPER_TIMEZONE`               | Compose, service |
| `OBSERVER_HA_PUBLIC_URL`          | `HOME_KEEPER_HA_PUBLIC_URL`          | Compose, service |
| `OBSERVER_HA_URL`                 | `HOME_KEEPER_HA_URL`                 | Compose, service |
| `OBSERVER_HA_TOKEN_FILE`          | `HOME_KEEPER_HA_TOKEN_FILE`          | Compose, service |
| `OBSERVER_HA_LOGIN_FILE`          | `HOME_KEEPER_HA_LOGIN_FILE`          | service          |
| `OBSERVER_BIND`                   | `HOME_KEEPER_BIND`                   | service          |
| `OBSERVER_DB`                     | `HOME_KEEPER_DB`                     | service          |
| `OBSERVER_SOURCE_ID`              | `HOME_KEEPER_SOURCE_ID`              | service          |
| `OBSERVER_TOKEN_FILE`             | `HOME_KEEPER_TOKEN_FILE`             | service          |
| `OBSERVER_URL`                    | `HOME_KEEPER_URL`                    | service (agent)  |
| `OBSERVER_API_ORIGIN`             | `HOME_KEEPER_API_ORIGIN`             | `mise run dev`   |

The `X-Home-Observer` request header is now `X-Home-Keeper`. Scripts that change
the selection through the API must send the new header.
