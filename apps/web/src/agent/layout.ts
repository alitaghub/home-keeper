// Where the agent panel sits. Wide screens: a resizable panel beside the dashboard (see App).
// Narrow screens: a full-screen sheet over the dashboard.
export const panelSlot =
  "fixed inset-0 z-50 flex flex-col bg-background lg:static lg:z-auto lg:h-full";

/** The panel's share of a wide window, in percent. */
export const AGENT_WIDTH = { initial: 40, max: 70 } as const;
/** Narrower than this, the composer and the approval cards no longer fit. */
export const AGENT_MIN_PX = 360;

/** Switches a wide panel between most of the window and the width you dragged it to. */
export type WidthControl = { expanded: boolean; toggle: () => void };
