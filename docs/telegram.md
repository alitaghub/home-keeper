# Telegram

The optional Telegram bot connects private Telegram chats to the
[agent](agent.md). Each allowed person gets a private, saved conversation. They
can ask questions and approve monitoring changes. People on a separate control
list can also switch devices.

## Set up

1. In Telegram, open [BotFather](https://t.me/BotFather) and create a bot with
   `/newbot`.
2. Add these lines to `.env`, next to the
   [model settings](agent.md#set-the-model):

   ```dotenv
   TELEGRAM_BOT_TOKEN=your-bot-token
   TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
   # Optional: who may switch devices from Telegram.
   TELEGRAM_CONTROL_USER_IDS=123456789
   ```

3. Start the `agent` and `telegram` profiles:

   ```sh
   docker compose --profile agent --profile telegram up --build --wait                       # bundled Home Assistant
   docker compose -f compose.demo.yaml --profile agent --profile telegram up --build --wait  # demo
   ```

4. Open the bot in Telegram and send `/help`. `up --wait` does not prove that
   polling works; a reply does.

### Find user IDs

Access uses numeric Telegram user IDs. Names and usernames grant nothing.

1. Set `TELEGRAM_ALLOWED_USER_IDS=9223372036854775807` temporarily.
2. Start the bot. Have each person send it `/start` privately. The bot refuses
   access and replies with their numeric ID.
3. Put the real IDs in `.env` and recreate the container:

   ```sh
   docker compose --profile agent --profile telegram up -d --force-recreate telegram
   ```

   `restart` does not read changed environment settings. Recreate the container
   after every allowlist change.

## Use the bot

| Input   | Result                                                                    |
| ------- | ------------------------------------------------------------------------- |
| `/start`, `/help` | Explains the bot. Calls no model.                               |
| `/new`  | Starts a new conversation. Buttons from the old one stop working.         |
| A text question | The agent answers in the current conversation.                    |

Approvals:

- A change, such as "Start monitoring the desk light", shows a proposal with
  **Approve** and **Reject**. Nothing changes until you decide.
- Only the person who asked can decide. A repeated or old button cannot apply
  another change.
- With several actions in one proposal, decide each before the agent continues.
- While a decision is pending, more questions do not bypass it. Choose a
  button, or use `/new`.
- Conversations and pending approvals survive a normal restart.

Every allowed person can use all agent tools and change which devices the
shared home monitors.

## Switch devices

`TELEGRAM_CONTROL_USER_IDS` lists who may switch lights, plugs and switches.
Device commands also need the [`control` profile](control.md).

| Setting                           | Effect                                                                   |
| --------------------------------- | ------------------------------------------------------------------------ |
| Unset or empty                    | Nobody can switch a device from Telegram.                                |
| An ID not in `TELEGRAM_ALLOWED_USER_IDS` | The bot stops at startup and names `TELEGRAM_CONTROL_USER_IDS`.   |

- A person on the control list who asks "Turn on the desk light at 30 percent"
  gets a proposal such as `Turn on light.desk at 30%`. Nothing changes until
  they approve.
- Home Keeper records each approved command with the person's numeric ID and
  their Telegram name at approval time. The name is a label, not
  authentication.
- A person not on the control list gets no switch proposal. If an old proposal
  still reaches them, **Approve** sends nothing and the bot replies "You are not
  allowed to control lights. Nothing was changed."
- A proposal saved by an older version, such as one for `turn_on_light`, is no
  longer accepted. Use `/new`.

## Operate

Use the same Compose files and profiles you started with. For the demo, add
`-f compose.demo.yaml`.

```sh
docker compose --profile agent --profile telegram logs telegram
docker compose --profile agent --profile telegram restart telegram
docker compose --profile agent --profile telegram stop telegram
```

- Run one bot process per token.
- The bot polls Telegram. It needs outbound internet access, but no public URL
  or open port.
- If the bot already has a webhook, remove it with Telegram's `deleteWebhook`
  method. The worker reports the conflict and does not remove it.
- The worker restarts until stopped. Configuration errors, rejected tokens, a
  webhook or a second worker produce a log message that never includes the
  token.
- Rate-limit waits follow Telegram's requested cooldown.
- Stopping `telegram` leaves the dashboard and agent panel running.
- `down` keeps the bot's state and conversations. `down --volumes` deletes them.

### Change to another bot

The saved polling progress belongs to one bot ID. Rotating the token of the
same bot keeps its state. For a different bot, drop the `telegram` database;
`db-setup` creates an empty one on the next start. The worker refuses another
bot's database rather than skip the new bot's messages. Home Keeper data and
agent conversations are not affected.

## Delivery guarantees

One worker handles messages in order. There is no queue, worker pool or
delivery guarantee.

- A slow answer delays the next person's message and button acknowledgement.
- The agent request times out after 300 seconds.
- When a request may already have reached the agent, the bot does not resend
  it. It checks the conversation and says when the result is uncertain. Wait for
  the run to finish, or use `/new`.
- The bot records a question as received before it sends it to the agent. A
  crash between the two can lose the question with no error reply. This order
  prevents a restart from repeating a request that may already have run.
- A network failure can lose or duplicate a reply.
- Telegram keeps undelivered updates for at most 24 hours. A longer outage
  loses questions.
- The bot does not rebuild missing approval buttons or recover a lost answer.
  Use `/new`.

## Scope

- Supported: private text chats and approval buttons.
- Not supported: groups, channels, message edits, voice, photos and
  notifications the bot starts.
- Long replies are split into plain-text messages. Answers have no charts.
- Device names render as text, without HTML or Markdown.
- Telegram conversations are separate from each other and from the agent
  panel.

## What leaves your machine

Questions and replies, including home details, pass through Telegram. Questions
and tool results also go to your model provider. The bot never receives Home
Assistant or model credentials. See [security.md](security.md).

> [!NOTE]
> The Telegram allowlist does not protect the agent itself. Anyone who can reach
> the local agent API can read every thread. See [agent limits](agent.md#limits).

## Manual check with a real bot

Automated tests use a fake Telegram endpoint and a scripted model against the
real Aegra server. To check a real bot:

1. Ask a question and a follow-up. Confirm a second allowed person gets a
   separate conversation, and an unlisted person gets no home data.
2. Ask to monitor a light. Reject it. Confirm the dashboard is unchanged.
3. Ask again. Restart `telegram` and `agent` while the buttons are pending.
   After the agent is healthy, approve. Confirm the light is monitored and a
   second click does nothing.
4. Ask to stop monitoring, then use `/new`. Confirm the old buttons do nothing.
5. Restart the bot and ask a follow-up. Confirm the conversation continues.
6. With the `control` profile, have a person on the control list switch a
   monitored light and approve it. Confirm it switches and
   [`GET /api/commands`](control.md#command-history) shows their ID and name.
   Confirm a person not on the list gets no proposal.

Record which steps you ran. Automated results do not prove a real bot or model
works.

References: [Telegram Bot API](https://core.telegram.org/bots/api),
[BotFather](https://core.telegram.org/bots/features#botfather).
