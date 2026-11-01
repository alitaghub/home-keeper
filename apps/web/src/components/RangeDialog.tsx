import { X } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { Button } from "./ui/button";

export const RANGES = [
  { days: 1, label: "24 hours" },
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
] as const;
export type Days = (typeof RANGES)[number]["days"];

type Request = { days: Days; attempt: number };

/**
 * A modal dialog of one device's report over a chosen range. It loads the report for each
 * range, shows loading and failure with a retry, and hands the loaded report to `children`.
 */
export function RangeDialog<Report>({
  headingId,
  name,
  entityId,
  load,
  failure,
  onClose,
  children,
}: {
  headingId: string;
  name: string;
  entityId: string;
  /** Must keep its identity across renders, such as a module function. */
  load: (days: Days, entityIds: string[]) => Promise<Report>;
  /** What failed, such as "Cannot load usage." */
  failure: string;
  onClose: () => void;
  children: (report: Report, days: Days) => ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  // A retry asks again for the same range.
  const [request, setRequest] = useState<Request>({ days: 1, attempt: 0 });
  const { days } = request;
  // Each answer remembers its request, so a range switch or a retry never shows old numbers.
  const [loaded, setLoaded] = useState<
    { request: Request; report: Report } | { request: Request; failed: true }
  >();

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  useEffect(() => {
    let current = true;
    load(request.days, [entityId]).then(
      (report) => current && setLoaded({ request, report }),
      () => current && setLoaded({ request, failed: true }),
    );
    return () => {
      current = false;
    };
  }, [request, entityId, load]);

  const shown = loaded?.request === request ? loaded : undefined;

  return (
    <dialog
      ref={dialog}
      className="m-auto w-[calc(100%-2rem)] max-w-2xl max-h-[calc(100dvh-2rem)] overflow-y-auto rounded-lg border bg-background p-6 text-foreground shadow-lg backdrop:bg-black/50"
      aria-labelledby={headingId}
      onClose={onClose}
    >
      <div className="grid gap-5">
        <div className="flex items-start justify-between gap-3">
          <h2 id={headingId} className="min-w-0 text-lg font-semibold leading-snug break-words">
            {name}
          </h2>
          <Button type="button" variant="ghost" size="sm" onClick={() => dialog.current?.close()}>
            <X aria-hidden="true" />
            Close
          </Button>
        </div>
        <fieldset className="flex flex-wrap items-center gap-2">
          <legend className="sr-only">Range</legend>
          {RANGES.map((range) => (
            <Button
              key={range.days}
              type="button"
              size="sm"
              variant={range.days === days ? "default" : "outline"}
              aria-pressed={range.days === days}
              onClick={() => setRequest({ days: range.days, attempt: 0 })}
            >
              {range.label}
            </Button>
          ))}
        </fieldset>
        {shown && "failed" in shown ? (
          <p className="text-sm text-danger" role="status">
            {failure}{" "}
            <Button
              type="button"
              variant="link"
              className="h-auto p-0"
              onClick={() => setRequest({ days, attempt: request.attempt + 1 })}
            >
              Retry
            </Button>
          </p>
        ) : !shown ? (
          <p className="text-sm text-muted-foreground" role="status">
            Loading…
          </p>
        ) : (
          children(shown.report, days)
        )}
      </div>
    </dialog>
  );
}
