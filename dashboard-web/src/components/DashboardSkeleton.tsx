import { Skeleton } from './ui/Skeleton';
import { KPI_GRID } from './Overview';

export function DashboardSkeleton() {
  return (
    <div role="status" aria-live="polite" className="space-y-6">
      <span className="sr-only">Loading snapshot…</span>
      <div className="rounded-lg border border-border bg-surface p-4 shadow-sm sm:p-5">
        <div className="mb-4 flex items-center justify-between gap-3">
          <Skeleton className="h-5 w-28" />
          <Skeleton className="h-9 w-48" />
        </div>
        <div className={KPI_GRID}>
          {Array.from({ length: 7 }, (_, i) => (
            <Skeleton key={i} className="h-[104px] rounded-lg" />
          ))}
        </div>
        <Skeleton className="mt-4 h-48 rounded-lg" />
      </div>
      <div className="space-y-3 rounded-lg border border-border bg-surface p-4 shadow-sm sm:p-5">
        <Skeleton className="h-5 w-32" />
        <Skeleton className="h-14" />
        <Skeleton className="h-14" />
      </div>
      <div className="space-y-2 rounded-lg border border-border bg-surface p-4 shadow-sm sm:p-5">
        <Skeleton className="mb-2 h-5 w-24" />
        {Array.from({ length: 6 }, (_, i) => (
          <Skeleton key={i} className="h-16" />
        ))}
      </div>
    </div>
  );
}
