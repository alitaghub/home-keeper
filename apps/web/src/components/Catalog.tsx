import { ChevronDown } from "lucide-react";
import { useState } from "react";
import type { Schemas } from "../api/client";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

type Entity = Schemas["Entity"];

export const lightName = (entity: Pick<Entity, "device_name" | "name">) =>
  entity.device_name || entity.name;

const UNGROUPED = "__ungrouped";

// A supported entity without a kind counts as a light, as the API does.
const KINDS = [
  {
    heading: "Lights",
    has: (entity: Entity) => entity.device_kind !== "switch" && entity.device_kind !== "state",
  },
  { heading: "Plugs and switches", has: (entity: Entity) => entity.device_kind === "switch" },
  { heading: "Other", has: (entity: Entity) => entity.device_kind === "state" },
];

// Case-insensitive substring match on the entity's or its device's name.
function matches(entity: Entity, query: string) {
  const needle = query.trim().toLocaleLowerCase();
  return (
    !needle ||
    entity.name.toLocaleLowerCase().includes(needle) ||
    (entity.device_name?.toLocaleLowerCase().includes(needle) ?? false)
  );
}

function groupByDevice(entities: Entity[]) {
  const groups = new Map<string, Entity[]>();
  for (const entity of entities) {
    const key = entity.device_id || UNGROUPED;
    groups.set(key, [...(groups.get(key) ?? []), entity]);
  }
  return groups;
}

export function Catalog({
  entities,
  homeAssistantUrl,
  selected,
  onToggle,
}: {
  entities: Entity[] | null;
  homeAssistantUrl: string | null;
  selected: string[];
  onToggle: (id: string, selected: boolean) => void;
}) {
  const [query, setQuery] = useState("");
  const supported = entities?.filter((entity) => entity.supported) ?? [];
  const found = entities?.filter((entity) => matches(entity, query)) ?? [];
  const others = found.filter((entity) => !entity.supported);
  const kinds = KINDS.map(({ heading, has }) => ({
    heading,
    entities: found.filter((entity) => entity.supported && has(entity)),
  })).filter((group) => group.entities.length);
  return (
    <>
      {!!entities?.length && (
        <Input
          type="search"
          aria-label="Search devices"
          placeholder="Search devices"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      )}
      <div id="catalog" className="grid gap-4">
        {entities && !supported.length && (
          <p className="text-sm text-muted-foreground">
            {homeAssistantUrl ? (
              <>
                No supported devices yet.{" "}
                <a
                  id="connect-devices"
                  href={`${homeAssistantUrl}/config/integrations/dashboard`}
                  target="_blank"
                  rel="noreferrer"
                  className="font-medium text-foreground underline underline-offset-4"
                >
                  Connect your devices in Home Assistant
                </a>
                . They appear here within seconds. Home Keeper monitors single devices, not groups
                or rooms.
              </>
            ) : entities.length ? (
              "No supported devices found. Check your integrations in Home Assistant."
            ) : (
              "Home Assistant lists no devices."
            )}
          </p>
        )}
        {!!query.trim() && !kinds.length && (
          <p className="text-sm text-muted-foreground">
            {found.length ? "No supported devices match" : "No devices match"} “{query.trim()}”.
          </p>
        )}
        {kinds.map((group) => (
          <section key={group.heading} className="grid gap-2">
            <h4 className="text-sm font-semibold">{group.heading}</h4>
            <div className="grid gap-2 sm:grid-cols-2">
              {group.entities.map((entity) => {
                const name = lightName(entity);
                const checked = selected.includes(entity.id);
                const action = checked ? "Stop monitoring" : "Monitor";
                return (
                  <div
                    key={entity.id}
                    className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2"
                  >
                    <strong className="min-w-0 text-sm font-medium break-words">{name}</strong>
                    <Button
                      type="button"
                      size="sm"
                      variant={checked ? "outline" : "default"}
                      aria-label={`${action} ${name}`}
                      onClick={() => onToggle(entity.id, !checked)}
                    >
                      {action}
                    </Button>
                  </div>
                );
              })}
            </div>
          </section>
        ))}
      </div>
      <details id="other-entities" className="group" hidden={!others.length}>
        <summary
          id="other-summary"
          className="inline-flex cursor-pointer list-none items-center gap-1 text-sm font-medium text-muted-foreground hover:text-foreground [&::-webkit-details-marker]:hidden"
        >
          {entities ? `Show other entities (${others.length})` : "Show other entities"}
          <ChevronDown
            className="size-4 transition-transform group-open:rotate-180"
            aria-hidden="true"
          />
        </summary>
        <p className="mt-2 text-sm text-muted-foreground">
          Home Assistant also exposes groups and rooms, people, settings and entities that belong to
          no device. Home Keeper does not monitor these.
        </p>
        <div id="other-catalog" className="mt-3 grid gap-4">
          {[...groupByDevice(others)].map(([key, group]) => (
            <div key={key} className="grid gap-2">
              <h3 className="text-sm font-semibold break-words">
                {key === UNGROUPED ? "Ungrouped entities" : group[0]?.device_name || key}
              </h3>
              {group.map((entity) => (
                <div
                  key={entity.id}
                  className="flex items-start justify-between gap-3 rounded-lg border px-3 py-2"
                >
                  <div className="grid min-w-0 gap-0.5">
                    <strong className="text-sm font-medium break-words">{entity.name}</strong>
                    <small className="text-xs text-muted-foreground break-all">{entity.id}</small>
                    <small className="text-xs text-warning-foreground">{entity.reason}</small>
                  </div>
                  <Badge variant="outline">Unsupported</Badge>
                </div>
              ))}
            </div>
          ))}
        </div>
      </details>
    </>
  );
}
