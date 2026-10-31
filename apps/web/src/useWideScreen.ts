import { useSyncExternalStore } from "react";

// Tailwind's `lg` breakpoint: from here the agent panel sits beside the dashboard.
const WIDE = "(min-width: 64rem)";

const query = () => window.matchMedia?.(WIDE);

function subscribe(onChange: () => void) {
  const wide = query();
  wide?.addEventListener("change", onChange);
  return () => wide?.removeEventListener("change", onChange);
}

/** Whether the window is wide enough for the agent panel to sit beside the dashboard. */
export function useWideScreen(): boolean {
  return useSyncExternalStore(subscribe, () => query()?.matches ?? false);
}
