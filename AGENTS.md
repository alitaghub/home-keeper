# AGENTS.md

Home Keeper is an agent that looks after your home. It reads devices, and their
history from Home Assistant's recorder, through a stateless reader service; it keeps
only which devices you monitor and its command log. Only the opt-in
controller (`--profile control`) switches lights, with a fixed command set, and only
lights that are monitored.

## Done

- `mise run check` exits 0.
- Changes to Docker, Compose or the UI also pass `mise run e2e`.
  Set `HOME_KEEPER_PORT` when port 8000 is taken. The e2e proofs use fixed Compose
  project names and ports, so run one `mise run e2e` per machine at a time.
- User-visible changes update `README.md` or `docs/`.

## Rules

- Public `/api/*` changes are contract changes: give every route a response model,
  run `mise run gen:api`, and commit the generated `schema.d.ts`. An intended
  response change refreshes the snapshots with `UPDATE_SNAPSHOTS=1` on
  `test_api_contract.py`, and the PR says why.
- Parse external data (Home Assistant payloads, requests, environment, files) into
  typed models at the boundary; Python `Any` lives only there. Every type or lint
  suppression names its rule and gives a one-line reason.
- Device and entity names come from Home Assistant and are untrusted; render them
  as text.
- Name tests by the behavior they prove, at the lowest level that proves it:
  `unit`, `integration`, or `e2e`.
- Shared code moves to `packages/` once a second app needs it.

## Pull requests

- One feature per PR. If the Change section covers two unrelated things, split it.
- Fill the template for a reviewer who reads it in two minutes. Describe what
  behaves differently, not which files changed.
- A UI change shows a before and an after screenshot of the same view and
  state. Take the before screenshot before you change the code, and upload both
  with `mise run pr:images -- --pr <number> <image>...`.

Before opening a PR, read [CONTRIBUTING.md](CONTRIBUTING.md).
