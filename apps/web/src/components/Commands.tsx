import type { Schemas } from "../api/client";
import { date } from "../format";

type CommandRecord = Schemas["CommandRecord"];

const outcomeText: Record<CommandRecord["outcome"], string> = {
  observed: "Observed",
  not_observed: "Not observed",
  refused: "Refused by Home Assistant",
  unreachable: "Controller unreachable",
  pending: "Pending",
  interrupted: "Interrupted",
};

function actionText(command: CommandRecord["command"]) {
  if (command.action === "off") return "Turn off";
  return command.brightness_pct == null ? "Turn on" : `Turn on at ${command.brightness_pct}%`;
}

function senderText(sender: CommandRecord["sender"]): string {
  switch (sender.kind) {
    case "dashboard":
      return "Dashboard";
    case "browser_chat":
      return "Browser chat";
    case "telegram":
      return `${sender.name} (Telegram ${sender.id})`;
    default: {
      // A sender kind added to schema.d.ts fails typecheck here.
      const unhandled: never = sender;
      return unhandled;
    }
  }
}

export function Commands({
  commands,
  names,
  hasMore,
  loading,
  onMore,
}: {
  commands: CommandRecord[] | null;
  /** Light names by entity ID; an unknown light shows its raw entity ID. */
  names: ReadonlyMap<string, string>;
  hasMore: boolean;
  loading: boolean;
  onMore: () => void;
}) {
  return (
    <div id="commands">
      <h3>Light commands</h3>
      {commands && !commands.length && <p className="empty">No light commands yet.</p>}
      {commands?.map((command) => (
        <article key={command.id} className="command">
          <strong>{names.get(command.entity_id) ?? command.entity_id}</strong>
          <span className="command-action">{actionText(command.command)}</span>
          <span className={`command-outcome ${command.outcome}`}>
            {outcomeText[command.outcome]}
          </span>
          <small>{senderText(command.sender)}</small>
          <small>{date(command.sent_at)}</small>
          {command.event_id !== null && (
            <small className="command-evidence">{`Evidence ID: ${command.event_id}`}</small>
          )}
        </article>
      ))}
      {hasMore && (
        <button id="commands-more" type="button" disabled={loading} onClick={onMore}>
          Load older commands
        </button>
      )}
    </div>
  );
}
