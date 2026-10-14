# Architecture

Home Observer has three running parts: a collector that reads the home, an API
that decides what to keep, and a dashboard that shows it. They run as two
containers from one image. The default stack adds a bundled Home Assistant and
a one-shot `ha-setup` container that onboards it and stores the collector's token.

```mermaid
flowchart LR
    subgraph home["Your home"]
        hue["Hue Bridge and lights"]
        ha["Home Assistant<br/><i>bundled or your own</i>"]
        hue --> ha
    end

    setup["ha-setup<br/><i>runs once</i>"] -- "onboarding,<br/>long-lived token" --> ha
    setup -- "token volume<br/>(collector reads it)" --> collector

    subgraph observer["Home Observer (Docker Compose)"]
        direction LR
        collector["Collector<br/><i>live or demo</i>"]
        api["API<br/><i>FastAPI</i>"]
        db[("SQLite")]
        collector -- "catalog, snapshots,<br/>events, heartbeats" --> api
        api -- "selection revision" --> collector
        api --> db
    end

    ha -- "registry reads, state reads,<br/>state_changed events" --> collector
    browser["Dashboard<br/><i>React, in your browser</i>"] -- "select, refresh, read history" --> api
```

| Part      | Code                                   | Job                                                                                         |
| --------- | -------------------------------------- | ------------------------------------------------------------------------------------------- |
| Collector | `live.py`, `home_assistant.py`         | Reads Home Assistant. Keeps only selected lights and approved fields. Sends typed records.   |
| Setup     | `ha_setup.py`                          | Onboards the bundled Home Assistant once and stores the collector's token. Never runs again after that. |
| Demo      | `demo.py`                              | Replaces the live collector with a synthetic home. Same API calls, no hardware.             |
| API       | `api.py`, `models.py`                  | Validates every record, owns the selection, and records collection gaps.                    |
| Store     | `store.py`                             | SQLite schema, transactions, retention.                                                     |
| Dashboard | `apps/web`                             | Discovery, selection, current state, the change timeline, and collection gaps.              |

Python code lives in `apps/observer/src/home_observer`.

## Rules the layout enforces

- **The API is the only writer.** The collector has no database mount. It posts
  to internal endpoints that need a service credential the API creates at
  startup.
- **Nothing is monitored until you select it.** Discovery lists devices. It does
  not approve them. Each selection change bumps a server-owned revision. The API
  rejects data from a collector that works from an older revision, so a slow
  read cannot bring back a light you just removed.
- **Read-only toward the home.** The connector makes fixed registry reads,
  selected-state reads and one filtered event subscription. It has no service
  calls and no device writes.
- **Evidence, not guesses.** When collection stops, the API records a gap. After
  recovery it stores a fresh snapshot and marks the transition time as unknown.
  It never invents the change that happened while it was blind.

## Selecting a light

```mermaid
sequenceDiagram
    actor You
    participant D as Dashboard
    participant A as API
    participant C as Collector
    participant H as Home Assistant

    You->>D: Click Monitor
    D->>A: PUT /api/selection/{entity_id}
    A-->>D: New selection revision
    C->>A: Next pass reads the selection
    C->>H: Read current state
    C->>A: Baseline snapshot
    C->>H: Subscribe to state_changed
    H-->>C: Light turned on
    C->>A: Event (filtered to approved fields)
    D->>A: GET /api/events
    A-->>D: "off → on" with timestamps
```

A collection pass runs every 10 seconds by default. Between passes, live mode
streams events as Home Assistant sends them.

## Words used in the code

| Term        | Meaning                                                                                       |
| ----------- | --------------------------------------------------------------------------------------------- |
| Source      | Where data comes from: `demo` or `home_assistant`. One database holds one source.             |
| Catalog     | The devices and entities the collector discovered, with whether each one can be monitored.   |
| Selection   | The lights you chose to monitor, plus a revision number the API owns.                        |
| Baseline    | A snapshot of a light's state. Taken at selection, during polling, and after recovery.       |
| Event       | One observed change, with old and new values and three timestamps (fired, received, saved).  |
| Gap         | A period when collection was interrupted. Open until a recovery snapshot closes it.          |
| Freshness   | How recently Home Assistant answered. It says nothing about whether the bulb itself answered. |

## The API contract

The dashboard's types in `apps/web/src/api/schema.d.ts` are generated from the
API's OpenAPI schema with `mise run gen:api`. A field rename that the UI does not
follow fails the TypeScript build. Snapshot tests in `test_api_contract.py` pin
the response shapes.

[collection.md](collection.md) covers collection, gaps and retention in detail.
[security.md](security.md) covers the trust boundaries.
