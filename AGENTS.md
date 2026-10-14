# AGENTS.md

Home Observer records what smart-home devices do. It reads devices through Home
Assistant and never controls them.

## Done

- `mise run check` exits 0.
- Changes to Docker, Compose, `scripts/` or the UI also pass `mise run e2e`.
  Set `OBSERVER_PORT` when port 8000 is taken.
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

Before opening a PR, read [CONTRIBUTING.md](CONTRIBUTING.md).
