import { useState } from "react";

// Whether the agent panel is open, per browser. It starts closed.
const KEY = "home-keeper.agent-panel";

function read(): boolean {
  try {
    return localStorage.getItem(KEY) === "open";
  } catch {
    return false;
  }
}

export function useAgentPanel() {
  const [open, setOpen] = useState(read);
  const change = (next: boolean) => {
    setOpen(next);
    try {
      localStorage.setItem(KEY, next ? "open" : "closed");
    } catch {
      // Storage is blocked: the panel still opens and closes, it is just not remembered.
    }
  };
  return [open, change] as const;
}
