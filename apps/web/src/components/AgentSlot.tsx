import { X } from "lucide-react";
import { Component, lazy, type ReactNode, Suspense } from "react";
import { panelSlot, type WidthControl } from "../agent/layout";
import { Button } from "./ui/button";

// The panel's code, and the chat libraries with it, load when it first opens.
const AgentPanel = lazy(() => import("../agent/AgentPanel"));

function Placeholder({ text, onClose }: { text: string; onClose: () => void }) {
  return (
    <div className={panelSlot}>
      <div className="flex items-center gap-1 border-b px-3 py-2">
        <p className="mr-auto text-sm font-semibold">Agent</p>
        <Button variant="ghost" size="icon-sm" aria-label="Close agent panel" onClick={onClose}>
          <X aria-hidden="true" />
        </Button>
      </div>
      <p className="p-4 text-sm text-muted-foreground" role="status">
        {text}
      </p>
    </div>
  );
}

// A chunk that fails to load (offline, or a newer build replaced it) must not take the
// dashboard down with it.
class LoadBoundary extends Component<{ fallback: ReactNode; children: ReactNode }> {
  override state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  override render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

export function AgentSlot({
  names,
  onClose,
  width,
}: {
  names: ReadonlyMap<string, string>;
  onClose: () => void;
  width?: WidthControl | undefined;
}) {
  return (
    <LoadBoundary
      fallback={
        <Placeholder text="The agent panel could not load. Reload the page." onClose={onClose} />
      }
    >
      <Suspense fallback={<Placeholder text="Loading the agent…" onClose={onClose} />}>
        <AgentPanel names={names} onClose={onClose} width={width} />
      </Suspense>
    </LoadBoundary>
  );
}
