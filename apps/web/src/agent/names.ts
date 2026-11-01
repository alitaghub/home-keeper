import { createContext } from "react";

/** The dashboard's friendly name for each device ID; Home Assistant's text, so untrusted. */
export const DeviceNames = createContext<ReadonlyMap<string, string>>(new Map());
