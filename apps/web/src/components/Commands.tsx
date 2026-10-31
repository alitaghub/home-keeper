import type { CommandRecord } from "../api/client";
import { date, time } from "../format";
import { cn } from "../lib/utils";
import { Button } from "./ui/button";

// Observed is the expected result; anything else may need a look.
const outcomes: Record<CommandRecord["outcome"], { label: string; tone: string }> = {
  observed: { label: "Observed", tone: "text-foreground" },
  not_observed: { label: "Not observed", tone: "text-warning-foreground" },
  refused: { label: "Refused by Home Assistant", tone: "text-danger" },
  unreachable: { label: "Controller unreachable", tone: "text-danger" },
  pending: { label: "Pending", tone: "text-warning-foreground" },
  interrupted: { label: "Interrupted", tone: "text-warning-foreground" },
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
    <div id="commands" className="grid">
      {commands && !commands.length && (
        <p className="text-sm text-muted-foreground">No device commands yet.</p>
      )}
      {commands?.map((command) => (
        <article
          key={command.id}
          className="flex items-baseline justify-between gap-3 border-b py-2.5 text-sm last-of-type:border-0"
        >
          <span className="min-w-0 break-words">
            <strong className="font-medium">
              {names.get(command.entity_id) ?? command.entity_id}
            </strong>{" "}
            <span>{actionText(command.command)}</span>
            {" · "}
            <span className={cn("font-medium", outcomes[command.outcome].tone)}>
              {outcomes[command.outcome].label}
            </span>
            {" · "}
            <small className="text-xs text-muted-foreground">{senderText(command.sender)}</small>
          </span>
          <time
            dateTime={command.sent_at}
            title={date(command.sent_at)}
            className="shrink-0 text-xs text-muted-foreground tabular-nums"
          >
            {time(command.sent_at)}
          </time>
        </article>
      ))}
      {hasMore && (
        <Button
          id="commands-more"
          type="button"
          variant="outline"
          size="sm"
          className="mt-3 justify-self-start"
          disabled={loading}
          onClick={onMore}
        >
          Load older commands
        </Button>
      )}
    </div>
  );
}
