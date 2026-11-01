// The chart card's states that need no chart code: the panel shows them before, or instead
// of, loading it.
import { Component, type ReactNode } from "react";
import { Skeleton } from "@/components/ui/skeleton";

export const CANNOT_DRAW = "The agent sent a chart the panel cannot draw.";

/** A chart-sized placeholder, while a chart's spec streams or its code or data loads. */
export function ChartSkeleton({ label }: { label: string }) {
  return (
    <div role="status" className="my-3 grid gap-2 rounded-lg border p-3">
      <span className="sr-only">{label}</span>
      <Skeleton className="h-4 w-1/3" />
      <Skeleton className="h-56 w-full" />
    </div>
  );
}

/** A card that says why there is no chart, with any action that could bring it back. */
export function ChartNotice({ children }: { children: ReactNode }) {
  return (
    <div role="alert" className="my-3 grid justify-items-start gap-2 rounded-lg border p-3 text-sm">
      {children}
    </div>
  );
}

/** Keeps a chart that fails to load or draw to its own card, so the chat stays usable. */
export class ChartBoundary extends Component<{ children: ReactNode }> {
  override state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  override render() {
    return this.state.failed ? (
      <ChartNotice>
        <p className="text-danger">{CANNOT_DRAW}</p>
      </ChartNotice>
    ) : (
      this.props.children
    );
  }
}
