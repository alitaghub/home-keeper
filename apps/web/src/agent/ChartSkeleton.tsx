import { Skeleton } from "@/components/ui/skeleton";

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
