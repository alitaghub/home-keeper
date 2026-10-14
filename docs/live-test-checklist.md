# Phase 1 live test checklist

Once you are home, use the [live setup](home-assistant.md) and test one
individual Hue light:

1. Confirm the bulb appears and responds in Home Assistant first.
2. Open Home Observer, check the **HOME ASSISTANT** label, and select that bulb.
   Wait for fresh evidence and an initial baseline.
3. Change on/off and brightness through the Hue app, leaving each setting for
   about 20 seconds. Confirm attributed entries in **Recorded changes**.
4. Stop the live collector with both Compose files. Wait more than twice the
   configured interval. Confirm stale evidence and an open collection gap.
   Change the bulb through the Hue app while collection is stopped.
5. Start the live collector. Confirm a recovery snapshot and closed gap. The app
   must not invent the transition that happened while it was stopped.
6. Restart the API. Confirm selections and history remain, with a persisted
   restart gap that closes after recovery.

The commands for steps 4 to 6 are in
[collection.md](collection.md#test-a-gap-without-hardware); add
`-f compose.yaml -f compose.live.yaml` to each.

Do not share the token or raw registry payloads when reporting a failure.
Share the safe dashboard error and Home Assistant version. Phase 1 implements
monitoring and evidence collection. Real home hardware validation remains pending.
