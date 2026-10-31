import { type ToolCallMessagePartComponent, useAuiState } from "@assistant-ui/react";
import { useLangGraphInterruptState, useLangGraphSendCommand } from "@assistant-ui/react-langgraph";
import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { ToolFallback } from "@/components/assistant-ui/elements/tool-fallback.aui";
import { Button } from "@/components/ui/button";
import {
  callsFor,
  type Decision,
  describeAction,
  type ProposedAction,
  parseApproval,
  resumeFor,
  type ShownToolCall,
} from "./approval";
import type { RunEvents } from "./runs";

type Pending = { index: number; action: ProposedAction; toolCallId: string | undefined };

type Approvals = {
  pending: readonly Pending[];
  names: ReadonlyMap<string, string>;
  decide: (index: number, decision: Decision) => void;
  /** Why the last resume failed; shown on the first card until the next decision. */
  error: string | null;
};

const ApprovalsContext = createContext<Approvals | null>(null);

/** The panel's run events; a failed resume brings its approval cards back. */
export const RunsContext = createContext<RunEvents | null>(null);

// Decisions belong to one interrupt: a new interrupt starts with none.
type Answers = {
  interrupt: unknown;
  decisions: readonly (Decision | undefined)[];
  error: string | null;
};

const RESUME_FAILED = "Your decisions did not reach the agent. Decide again to retry.";

const isDecided = (decision: Decision | undefined) => decision !== undefined;

/** Collects one decision per action of the open chat's approval and resumes once all are
 * decided. Key it by thread, so an approval never follows a chat switch. */
export function ApprovalScope({
  names,
  children,
}: {
  names: ReadonlyMap<string, string>;
  children: ReactNode;
}) {
  const interrupt = useLangGraphInterruptState();
  const sendCommand = useLangGraphSendCommand();
  const lastParts = useAuiState((s) => s.thread.messages.at(-1)?.content);
  const approval = useMemo(() => parseApproval(interrupt?.value), [interrupt]);
  const actions = approval?.kind === "actions" ? approval.actions : null;
  const [answers, setAnswers] = useState<Answers>({
    interrupt: undefined,
    decisions: [],
    error: null,
  });
  // Two clicks can land before a render; the second must see the first decision.
  const latest = useRef(answers);

  const calls = useMemo(
    () =>
      (lastParts ?? []).flatMap((part): ShownToolCall[] =>
        part.type === "tool-call" ? [part] : [],
      ),
    [lastParts],
  );
  const callIds = useMemo(() => (actions ? callsFor(actions, calls) : []), [actions, calls]);

  const current = answers.interrupt === interrupt ? answers : null;
  const decisions = current?.decisions ?? [];
  const pending = (actions ?? []).flatMap((action, index) =>
    decisions[index] ? [] : [{ index, action, toolCallId: callIds[index] }],
  );

  const decide = (index: number, decision: Decision) => {
    if (!actions) return;
    const previous = latest.current.interrupt === interrupt ? latest.current.decisions : [];
    if (previous[index]) return;
    const next = actions.map((_, i) => (i === index ? decision : previous[i]));
    latest.current = { interrupt, decisions: next, error: null };
    setAnswers(latest.current);
    if (!next.every(isDecided)) return;
    send(next);
  };

  // assistant-ui types a resume as a string; the stream callback decodes it.
  const send = (all: readonly Decision[]) =>
    sendCommand({ resume: JSON.stringify(resumeFor(all)) }).catch(reopen);

  const rejectAll = () => {
    if (approval?.kind !== "unreadable") return;
    if (latest.current.interrupt === interrupt && latest.current.decisions.length) return;
    const all = Array.from({ length: approval.count }, (): Decision => "reject");
    latest.current = { interrupt, decisions: all, error: null };
    setAnswers(latest.current);
    send(all);
  };

  // The interrupt is still open after a failed resume: its cards come back, undecided.
  const reopen = () => {
    if (latest.current.interrupt !== interrupt || !latest.current.decisions.length) return;
    latest.current = { interrupt, decisions: [], error: RESUME_FAILED };
    setAnswers(latest.current);
  };
  const reopenRef = useRef(reopen);
  reopenRef.current = reopen;
  const runs = useContext(RunsContext);
  const threadId = useAuiState((s) => s.threadListItem.externalId);
  useEffect(
    () =>
      runs?.subscribe((event) => {
        if (event.type === "end" && event.resume && event.failed && event.threadId === threadId) {
          reopenRef.current();
        }
      }),
    [runs, threadId],
  );

  const value = { pending, names, decide, error: current?.error ?? null };
  // Actions whose call the conversation does not show still need a decision.
  const unplaced = pending.filter((entry) => entry.toolCallId === undefined);
  return (
    <ApprovalsContext.Provider value={value}>
      {children}
      {approval?.kind === "unreadable" && !decisions.length && (
        <UnreadableApproval error={current?.error ?? null} onReject={rejectAll} />
      )}
      {unplaced.length > 0 && (
        <div className="grid gap-2 border-t p-3">
          {unplaced.map((entry) => (
            <ApprovalCard key={entry.index} entry={entry} />
          ))}
        </div>
      )}
    </ApprovalsContext.Provider>
  );
}

function ApprovalCard({ entry }: { entry: Pending }) {
  const approvals = useContext(ApprovalsContext);
  if (!approvals) return null;
  const description = describeAction(entry.action, approvals.names.get(entry.action.entityId));
  return (
    <fieldset aria-label={description} className="grid min-w-0 gap-3 rounded-lg border bg-card p-3">
      <p className="text-sm">
        <span className="text-muted-foreground">The agent asks to: </span>
        <strong className="font-medium">{description}</strong>
      </p>
      {approvals.error && entry === approvals.pending[0] && (
        <p role="alert" className="text-sm text-destructive">
          {approvals.error}
        </p>
      )}
      <div className="flex gap-2">
        <Button
          size="sm"
          aria-label={`Approve: ${description}`}
          onClick={() => approvals.decide(entry.index, "approve")}
        >
          Approve
        </Button>
        <Button
          size="sm"
          variant="outline"
          aria-label={`Reject: ${description}`}
          onClick={() => approvals.decide(entry.index, "reject")}
        >
          Reject
        </Button>
      </div>
    </fieldset>
  );
}

/** A change the agent proposed in a form the panel cannot read; it can only be rejected. */
function UnreadableApproval({ error, onReject }: { error: string | null; onReject: () => void }) {
  return (
    <div className="border-t p-3">
      <fieldset
        aria-label="Proposed changes"
        className="grid min-w-0 gap-3 rounded-lg border bg-card p-3"
      >
        <p className="text-sm">
          The agent proposed a change the dashboard cannot show. Reject it to continue.
        </p>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            aria-label="Reject the proposed changes"
            onClick={onReject}
          >
            Reject
          </Button>
        </div>
      </fieldset>
    </div>
  );
}

/** A tool call, collapsed, with a card for each action it waits on approval for. */
export const ToolCall: ToolCallMessagePartComponent = (props) => {
  const approvals = useContext(ApprovalsContext);
  const cards = approvals?.pending.filter((entry) => entry.toolCallId === props.toolCallId) ?? [];
  return (
    <div className="grid gap-2">
      <ToolFallback {...props} />
      {cards.map((entry) => (
        <ApprovalCard key={entry.index} entry={entry} />
      ))}
    </div>
  );
};
