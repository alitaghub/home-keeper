import type { CommandRecord } from "../api/client";
import { date, time } from "../format";

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
  // A plug's or switch's command has no brightness.
  const percent = "brightness_pct" in command ? command.brightness_pct : null;
  return percent == null ? "Turn on" : `Turn on at ${percent}%`;
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
    <div id="commands" className="rows">
      {commands && !commands.length && <p className="empty">No device commands yet.</p>}
      {commands?.map((command) => (
        <article key={command.id} className="row command">
          <div className="line">
            <span className="row-main">
              <strong>{names.get(command.entity_id) ?? command.entity_id}</strong>{" "}
              <span className="command-action">{actionText(command.command)}</span>
              {" · "}
              <span className={`command-outcome ${command.outcome}`}>
                {outcomeText[command.outcome]}
              </span>
              {" · "}
              <small>{senderText(command.sender)}</small>
            </span>
            <time dateTime={command.sent_at} title={date(command.sent_at)}>
              {time(command.sent_at)}
            </time>
          </div>
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
