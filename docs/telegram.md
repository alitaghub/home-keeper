# Talk to the agent through Telegram

The optional Telegram bot gives your family another way to talk to the existing
Home Observer agent. Each allowed person has a private, saved conversation.
The bot can answer questions and propose monitoring changes. It cannot control
devices.

## Set up the bot

1. Open [BotFather](https://t.me/BotFather) in Telegram and use `/newbot` to create
   a bot. Copy its token into the repository's `.env`, alongside your existing
   [agent model settings](agent.md#set-the-model):

   ```dotenv
   TELEGRAM_BOT_TOKEN=your-bot-token
   TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
   ```

   Use numeric Telegram user IDs, separated by commas. Names and usernames do
   not grant access. Every allowed family member can use the same agent tools
   and change which lights the shared home records.

   If you do not know the IDs yet, temporarily use
   `TELEGRAM_ALLOWED_USER_IDS=9223372036854775807`. Start the bot, then have each
   person send it `/start` privately. It refuses access and returns that person's
   numeric ID. Replace the temporary value with those IDs and recreate the bot container
   as shown below.

2. Enable both profiles with your usual stack:

   ```sh
   # Bundled Home Assistant
   docker compose --profile agent --profile telegram up --build --wait

   # Or the synthetic demo
   docker compose -f compose.demo.yaml --profile agent --profile telegram up --build --wait
   ```

3. Open the bot in Telegram, send `/start`, then ask a text question such as
   "Which lights are monitored?" or "How long was the desk light on this week?"
   A follow-up uses the same conversation, including after a normal restart.

Run only one bot process per token. The bot fetches messages from Telegram, so
it needs outbound internet access but no public URL or incoming port. If this
bot already has a webhook, remove it through Telegram's `deleteWebhook` method
before using this polling worker. The worker reports the conflict and does not
remove it for you.

## Conversations and approvals

- `/start` and `/help` explain how to use the bot without calling a model.
- `/new` starts a fresh conversation. It keeps old history and makes all buttons
  from the previous conversation unusable.
- "Start monitoring the desk light" produces a proposal with **Approve** and
  **Reject** buttons. Nothing changes until you decide. Rejection leaves that
  light's monitoring unchanged.
- Only the requesting person can decide. A repeated or old button cannot apply
  another monitoring change.
- If a proposal has several actions, decide each one before the agent continues.
- While a decision is pending, choose a button or use `/new`. More questions do
  not bypass the approval. Pending approvals survive normal bot/agent restarts.

Telegram history is separate from browser chat and from other family members'
conversations. Browser chat still runs at <http://127.0.0.1:3000>. Anyone who can
reach the existing local Aegra API can still inspect threads, as described in
[the agent limits](agent.md#limits).

## Restart, stop, and diagnose

Use the same Compose file and profiles you started with:

```sh
docker compose --profile agent --profile telegram logs telegram
docker compose --profile agent --profile telegram restart telegram
docker compose --profile agent --profile telegram stop telegram
```

For the demo, add `-f compose.demo.yaml` after `docker compose`. Restart after
changing the allowlist with `up -d --force-recreate telegram`, rather than
`restart`: Docker must recreate the container to read changed environment
settings. The full command is:

```sh
docker compose --profile agent --profile telegram up -d --force-recreate telegram
```

Compose waits for the agent to become healthy, then starts the bot process.
The bot has no Compose healthcheck, so `up --wait` does not prove that Telegram
polling works. Send `/help` privately to verify it after setup.

Stopping only `telegram` leaves browser chat and collection running. Normal
`down` keeps the bot's small SQLite state volume and Aegra's conversation
volume. `down --volumes` deletes them along with the stack's other saved data.

Missing settings, invalid IDs, rejected tokens, a preexisting webhook, or a
second polling worker produce a diagnostic without printing the token. Keep
the token in `.env`; do not put it in a command URL or share it in logs.

One worker handles messages sequentially. A slow answer delays the next
person's message and approval-button acknowledgement. The agent HTTP client
uses a 300-second timeout. There is no custom queue, concurrent
worker pool, or delivery guarantee. When a request might already have reached the agent, the bot does
not automatically run it again. It checks the existing conversation and tells
you if the result is uncertain. Wait for that run to finish or use `/new`.
A network failure can lose or duplicate a reply. Telegram keeps undelivered
incoming updates for at most 24 hours, so a longer outage can lose questions.

A crash can also lose a question just after the bot records it as received and
before it reaches the agent. There may be no error reply. Send the question
again if no answer arrives. The bot records receipt first so restarting cannot
silently repeat a request that might already have run.

If an uncertain request leaves the agent waiting for approval, or some approval
buttons could not be delivered, use `/new`. The bot does not rebuild missing
buttons. A normal restart preserves buttons that were already delivered and
saved. If a completed answer was lost during an uncertain request, the bot
does not recover that old answer when it accepts the next question.

The saved polling progress belongs to one bot's numeric ID. Rotating the token
for that same bot keeps its state. To switch to a different bot, use a fresh
Telegram state volume. The worker refuses to reuse another bot's volume rather
than silently skip the new bot's messages. Keep the old volume as a backup;
there is no need to delete Observer history or Aegra's conversations. Volumes
created before this identity check bind to the configured bot on their first
upgraded startup; keep the original bot configured for that startup.

Only private text chats and approval buttons are supported. Groups, channels,
message edits, voice, photos, and proactive notifications are outside scope.
Long replies are split into plain-text messages. Device names are displayed as
text, without interpreting their HTML or Markdown.

## What leaves your machine

Your questions and replies, including home details in the answers, pass through
Telegram. Questions and tool results also reach your configured model provider,
as they do in browser chat. Home Assistant credentials and model credentials
are not passed to the bot. See [security.md](security.md).

## Manual real-bot check

The automated Docker proof uses a fake Telegram endpoint and a deterministic
model through the real pinned Aegra server. It needs no Telegram or model keys.
For a real-bot check after configuring credentials:

1. Ask a question and a follow-up. Confirm another allowed person's conversation
   starts independently; an unapproved person gets no home data.
2. Ask to monitor a discovered light. Reject it and check the dashboard still
   shows the previous monitoring choice.
3. Ask again. Restart `telegram` and `agent` while the buttons are pending,
   wait for the agent to become healthy, then approve. Confirm the dashboard
   now shows the light as monitored. Clicking again must do nothing.
4. Ask to stop monitoring, then use `/new`. The old buttons must no longer work.
5. Restart the bot and ask a follow-up. Confirm the current conversation remains.

Record whether these steps were actually run. Automated fixture results are not
a claim that a real Telegram bot or your configured model was tested.

Protocol details: [Telegram Bot API](https://core.telegram.org/bots/api),
[bot creation](https://core.telegram.org/bots/features#botfather).
