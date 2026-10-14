# Verification history

What each delivery verified, kept as a record. Commands and test counts are as
they were at the time. Python tests then lived in a flat `tests/` folder and the
dashboard was plain JavaScript. Today `mise run check` and `mise run e2e` cover
these checks; see [CONTRIBUTING.md](../CONTRIBUTING.md).

CI uses hardware-free checks and a Compose demo smoke test. Live connector tests
use synthetic Home Assistant responses, not home credentials. They distinguish
connector behavior from Home Assistant's physical device behavior.

## Story 01

Story 01's local verification recorded 12 passing API tests, Ruff and JavaScript
syntax checks, browser selection/removal and malicious-name rendering, and
Compose startup/restart/smoke checks. One synthetic selection confirmed in 4.6
seconds with ten-second polling. That is not measured Hue latency.

## Story 02

Story 02 passed 41 local tests, including seven independent boundary probes.
Synthetic WebSocket/REST tests verified selected reads, identity changes, failure
recovery, credential sanitization, payload limits and invalid TLS rejection.
An isolated live Compose deployment against a fake Home Assistant passed
startup, selection, collection, outage and recovery checks in 44.31 seconds,
including image build time. This is test duration, not measured Hue latency.
Demo Compose smoke, Ruff, JavaScript syntax and browser DOM checks also passed.
Hosted CI has not run. No Python static typechecker or TypeScript checks are
claimed. Model calls and model cost are zero.

## Story 03

Story 03 passed 55 local tests, including three independent adversarial probes
and a regression check for upgrading existing databases. Demo Compose smoke
passed. Live Compose against a synthetic Home Assistant recorded a subscribed
brightness event and passed outage/recovery checks in 25.842 seconds, including
image build time. Ruff, JavaScript syntax and a headless browser check passed.
The browser displayed attributed events, no injected script nodes and no
horizontal overflow. These checks use synthetic devices. Real Hue validation,
hosted CI and real hardware were not verified in that milestone.

## Story 04

Story 04 completed the Phase 1 collection implementation. Final local verification
passed 73 tests, including three independent coverage probes. The reviewer found
one entity-attribution bug in gap timestamps; it was reproduced and fixed.
Synthetic live Compose passed subscription, outage/recovery and API restart-gap
checks in 28.485 seconds, including image build time. Stopping the demo collector
also produced a heartbeat gap without new device events, then recovered in a
25.402-second check. These durations are test execution times, not Hue latency.
Ruff, JavaScript syntax, demo Compose smoke and browser DOM checks passed.
Real Hue hardware, hosted CI and the platforms listed in
[networking.md](networking.md) remain untested.

## Review fixes

The full review fixes passed 82 local tests. Four major failures were reproduced
before their fixes. Demo Compose and synthetic live Compose passed; the latter
check took 22.789 seconds including startup/build. Ruff, JavaScript syntax, shell
syntax and Compose configuration checks passed. A secret scan found only public
synthetic fixture values. The optional Home Assistant 2026.10.1 container started
on this WSL2/Linux Docker Engine host and returned its onboarding page. Account
creation, real bridge pairing and hardware event capture are still pending.
