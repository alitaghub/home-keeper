// Draws the agent's show_chart calls in the panel. Loaded on the first chart, with Recharts:
// AgentPanel registers a lazy wrapper, so a chat without charts never fetches this code.
import { type ToolCallMessagePartComponent, useToolArgsStatus } from "@assistant-ui/react";
import { Ellipsis } from "lucide-react";
import { useContext, useEffect, useId, useRef, useState } from "react";
import { Bar, BarChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts";
import { Button } from "@/components/ui/button";
import {
  type ChartConfig,
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from "@/components/ui/chart";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { fetchUsageRange, type Usage } from "../useUsage";
import { ChartSkeleton } from "./ChartSkeleton";
import {
  categoricalDrawing,
  chartFileName,
  type Drawing,
  saveFile,
  type Table,
  tableCsv,
  usageDrawing,
} from "./chartData";
import { chartPng } from "./chartPng";
import { parseChartSpec, type UsageByDayChart } from "./chartSpec";
import { DeviceNames } from "./names";

const CANNOT_DRAW = "The agent sent a chart the panel cannot draw.";

function Notice({ children }: { children: React.ReactNode }) {
  return (
    <div role="alert" className="my-3 grid justify-items-start gap-2 rounded-lg border p-3 text-sm">
      {children}
    </div>
  );
}

type Row = Record<string, string | number>;

/** The marks over their categories, colored by the app; series are keyed by fixed ids. */
function Plot({ drawing }: { drawing: Drawing }) {
  const { marks, categories } = drawing;
  const config: ChartConfig = Object.fromEntries(
    marks.map((mark) => [mark.key, { label: mark.label }]),
  );
  const data = categories.map(
    (category, row): Row => ({
      category,
      ...Object.fromEntries(marks.map((mark) => [mark.key, mark.values[row] ?? 0])),
    }),
  );
  const parts = [
    <CartesianGrid key="grid" vertical={false} />,
    <XAxis
      key="x"
      dataKey="category"
      tickLine={false}
      axisLine={false}
      tickMargin={8}
      minTickGap={12}
    />,
    <YAxis key="y" tickLine={false} axisLine={false} width="auto" />,
    <ChartTooltip key="tooltip" content={<ChartTooltipContent />} />,
  ];
  const margin = { top: 8, right: 8, bottom: 0, left: 0 };
  return (
    // A screen reader hears the summary; the table holds every value.
    <div role="img" aria-label={drawing.summary}>
      <ChartContainer config={config} className="aspect-auto h-56 w-full">
        {drawing.kind === "bar" ? (
          <BarChart data={data} margin={margin} accessibilityLayer={false}>
            {parts}
            {marks.map((mark) => (
              <Bar
                key={mark.key}
                dataKey={mark.key}
                fill={mark.fill}
                isAnimationActive={false}
                {...(mark.stack ? { stackId: mark.stack } : { radius: [4, 4, 0, 0] })}
              />
            ))}
          </BarChart>
        ) : (
          <LineChart data={data} margin={margin} accessibilityLayer={false}>
            {parts}
            {marks.map((mark) => (
              // Straight segments: a smoothed curve would suggest values between the points.
              <Line
                key={mark.key}
                dataKey={mark.key}
                type="linear"
                stroke={mark.fill}
                strokeWidth={2}
                dot={{ r: 3, fill: mark.fill, strokeWidth: 0 }}
                activeDot={{ r: 4, fill: mark.fill, strokeWidth: 0 }}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        )}
      </ChartContainer>
    </div>
  );
}

function Legend({ drawing }: { drawing: Drawing }) {
  return (
    <ul data-chart-legend className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
      {drawing.legend.map((item, index) => (
        // Labels may repeat, so the position keys each one; the legend never reorders.
        // biome-ignore lint/suspicious/noArrayIndexKey: a fixed list built once per drawing
        <li key={index} data-legend-item className="inline-flex items-center gap-1.5">
          <span
            data-swatch
            aria-hidden="true"
            className={cn("inline-block size-2.5 rounded-sm", item.swatch)}
          />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

function DataTable({ table, caption }: { table: Table; caption: string }) {
  const [first, ...rest] = table.columns;
  return (
    <div className="max-h-72 overflow-auto">
      <table className="w-full text-xs tabular-nums">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b text-left text-muted-foreground">
            <th scope="col" className="py-1 pr-3 font-medium whitespace-nowrap">
              {first}
            </th>
            {rest.map((column, index) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: names may repeat; columns never reorder
              <th key={index} scope="col" className="py-1 pr-3 text-right font-medium">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map(([head, ...cells], row) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: categories may repeat; rows never reorder
            <tr key={row} className="border-b last:border-0">
              <th scope="row" className="py-1 pr-3 text-left font-normal whitespace-nowrap">
                {head?.text}
              </th>
              {cells.map((cell, index) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: cells follow the columns
                <td key={index} className="py-1 pr-3 text-right">
                  {cell.text}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

type View = "chart" | "table";

function ChartMenu({
  view,
  onView,
  onPng,
  onCsv,
}: {
  view: View;
  onView: (view: View) => void;
  onPng: () => void;
  onCsv: () => void;
}) {
  return (
    // Not modal: a modal menu locks scrolling with an injected <style>, which the CSP blocks.
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="Chart options" title="Chart options">
          <Ellipsis aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={() => onView(view === "chart" ? "table" : "chart")}>
          {view === "chart" ? "Show as table" : "Show as chart"}
        </DropdownMenuItem>
        {/* The PNG is painted from the drawn chart, so it needs the chart on screen. */}
        <DropdownMenuItem disabled={view !== "chart"} onSelect={onPng}>
          Download PNG
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={onCsv}>Download CSV</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** A drawn chart in a card, with a menu to switch it to a table and to download it. */
function ChartCard({ drawing }: { drawing: Drawing }) {
  const [view, setView] = useState<View>("chart");
  const card = useRef<HTMLElement>(null);
  const titleId = useId();

  const downloadPng = () => {
    if (!card.current) return;
    void chartPng(card.current).then((blob) => {
      if (blob) saveFile(blob, chartFileName(drawing.title, "png"));
    });
  };
  const downloadCsv = () =>
    saveFile(
      new Blob([tableCsv(drawing.table)], { type: "text/csv;charset=utf-8" }),
      chartFileName(drawing.title, "csv"),
    );

  return (
    <figure
      ref={card}
      aria-labelledby={titleId}
      className="my-3 grid gap-2 rounded-lg border bg-card p-3 text-card-foreground"
    >
      <div className="flex items-start gap-2">
        <figcaption id={titleId} className="grid min-w-0 flex-1 gap-0.5">
          <span data-chart-title className="text-sm font-medium break-words">
            {drawing.title}
          </span>
          {drawing.subtitle && (
            <span data-chart-subtitle className="text-xs text-muted-foreground">
              {drawing.subtitle}
            </span>
          )}
        </figcaption>
        <ChartMenu view={view} onView={setView} onPng={downloadPng} onCsv={downloadCsv} />
      </div>
      {view === "chart" ? (
        <>
          <Plot drawing={drawing} />
          <Legend drawing={drawing} />
        </>
      ) : (
        <DataTable table={drawing.table} caption={drawing.title} />
      )}
      {drawing.totals && (
        <ul className="grid gap-0.5 text-xs text-muted-foreground" aria-label="Totals">
          {drawing.totals.map((total, index) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: names may repeat; devices never reorder
            <li key={index}>{`${total.name}: on ${total.on}, unknown ${total.unknown}`}</li>
          ))}
        </ul>
      )}
    </figure>
  );
}

/** On and unknown time per day, which the panel loads itself; the agent passes no numbers. */
function UsageByDayView({ chart }: { chart: UsageByDayChart }) {
  const names = useContext(DeviceNames);
  const [attempt, setAttempt] = useState(0);
  const { start, end } = chart;
  // Parsed arguments are new arrays on every render; load again only when the IDs change.
  const ids = chart.entityIds.join("\n");
  const request = `${ids}\n${start}\n${end}\n${attempt}`;
  // Each answer remembers its request, so a retry never shows the old failure or numbers.
  const [loaded, setLoaded] = useState<
    { request: string; usage: Usage } | { request: string; failed: true }
  >();

  useEffect(() => {
    let current = true;
    const asked = `${ids}\n${start}\n${end}\n${attempt}`;
    fetchUsageRange(start, end, ids.split("\n")).then(
      (usage) => current && setLoaded({ request: asked, usage }),
      () => current && setLoaded({ request: asked, failed: true }),
    );
    return () => {
      current = false;
    };
  }, [ids, start, end, attempt]);

  const shown = loaded?.request === request ? loaded : undefined;
  if (!shown) return <ChartSkeleton label="Loading the chart data" />;
  if ("failed" in shown) {
    return (
      <Notice>
        <p className="text-danger">Could not load the chart data</p>
        <Button variant="outline" size="sm" onClick={() => setAttempt((n) => n + 1)}>
          Retry
        </Button>
      </Notice>
    );
  }
  const drawing = usageDrawing(chart, shown.usage, (id) => names.get(id) ?? id);
  if (!drawing.marks.length) {
    return (
      <p role="status" className="my-3 rounded-lg border p-3 text-sm text-muted-foreground">
        None of these devices is monitored, so there is no usage to chart.
      </p>
    );
  }
  return <ChartCard drawing={drawing} />;
}

/** Renders a show_chart call: a skeleton while its spec streams, then the chart or why not. */
export const ChartToolUI: ToolCallMessagePartComponent = ({ args, isError }) => {
  const { argsStatus } = useToolArgsStatus();
  if (argsStatus === "streaming") return <ChartSkeleton label="Drawing a chart" />;
  const chart = parseChartSpec(args);
  if (isError || !chart) {
    return (
      <Notice>
        <p className="text-danger">{CANNOT_DRAW}</p>
      </Notice>
    );
  }
  return chart.kind === "usage_by_day" ? (
    <UsageByDayView chart={chart} />
  ) : (
    <ChartCard drawing={categoricalDrawing(chart)} />
  );
};
