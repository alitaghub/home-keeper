import type { Schemas } from "../api/client";

type Entity = Schemas["Entity"];

export const lightName = (entity: Pick<Entity, "device_name" | "name">) =>
  entity.device_name || entity.name;

const UNGROUPED = "__ungrouped";

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
  const lights = entities?.filter((entity) => entity.supported) ?? [];
  const others = entities?.filter((entity) => !entity.supported) ?? [];
  return (
    <>
      <div id="catalog" className="light-grid">
        {entities && !lights.length && (
          <p className="empty">
            {homeAssistantUrl ? (
              <>
                No supported devices yet.{" "}
                <a
                  id="connect-devices"
                  href={`${homeAssistantUrl}/config/integrations/dashboard`}
                  target="_blank"
                  rel="noreferrer"
                >
                  Connect your devices in Home Assistant
                </a>
                , then refresh discovery. Home Observer records individual Hue lights.
              </>
            ) : entities.length ? (
              "No supported lamps found. Check your integrations in Home Assistant."
            ) : (
              "Waiting for discovery."
            )}
          </p>
        )}
        {lights.map((entity) => {
          const name = lightName(entity);
          const checked = selected.includes(entity.id);
          const action = checked ? "Stop monitoring" : "Monitor";
          return (
            <div key={entity.id} className="entity-row lamp-card">
              <strong>{name}</strong>
              <button
                type="button"
                className={checked ? "secondary" : "primary"}
                aria-label={`${action} ${name}`}
                onClick={() => onToggle(entity.id, !checked)}
              >
                {action}
              </button>
            </div>
          );
        })}
      </div>
      <details id="other-entities" className="other-entities" hidden={!others.length}>
        <summary id="other-summary">
          {entities ? `Show other entities (${others.length})` : "Show other entities"}
        </summary>
        <p className="history-status">
          Home Assistant also exposes room groups, scenes, sensors and settings. These are not
          additional lamps and cannot be monitored here yet.
        </p>
        <div id="other-catalog">
          {[...groupByDevice(others)].map(([key, group]) => (
            <div key={key} className="device">
              <h3>{key === UNGROUPED ? "Ungrouped entities" : group[0]?.device_name || key}</h3>
              {group.map((entity) => (
                <div key={entity.id} className="entity-row">
                  <div>
                    <strong>{entity.name}</strong>
                    <small>{entity.id}</small>
                    <small className="unsupported">{entity.reason}</small>
                  </div>
                  <span className="tag">Unsupported</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      </details>
    </>
  );
}
