import { lazy, Suspense } from 'react';
import { Card } from './ui/Card';
import { Skeleton } from './ui/Skeleton';
import type { DailyPoint } from '../data/types';

const TrendChart = lazy(() => import('./TrendChart'));

export function DailyActivity({ daily }: { daily: DailyPoint[] }) {
  return (
    <Card
      title="Daily activity"
      actions={
        daily.length > 0 ? (
          <span className="text-xs text-fg-subtle">
            last {daily.length} {daily.length === 1 ? 'day' : 'days'} with events (UTC days)
          </span>
        ) : undefined
      }
    >
      <div className="px-2 pt-2 pb-3 sm:px-3">
        {daily.length === 0 ? (
          <p className="px-2 py-8 text-center text-sm text-fg-muted">No daily data yet.</p>
        ) : (
          <>
            <Suspense fallback={<Skeleton className="h-48" />}>
              <TrendChart daily={daily} />
            </Suspense>
            {/* sr-only on the wrapper: a <table> ignores the 1px width and would widen the page. */}
            <div className="sr-only">
              <table>
                <caption>Daily activity</caption>
                <thead>
                  <tr>
                    <th scope="col">Date</th>
                    <th scope="col">Deploys</th>
                    <th scope="col">Failed</th>
                    <th scope="col">Incidents</th>
                    <th scope="col">Escalations</th>
                    <th scope="col">Merged revert PRs</th>
                  </tr>
                </thead>
                <tbody>
                  {daily.map((d) => (
                    <tr key={d.date}>
                      <th scope="row">{d.date}</th>
                      <td>{d.deploys}</td>
                      <td>{d.deploy_failures}</td>
                      <td>{d.incidents}</td>
                      <td>{d.escalations}</td>
                      <td>{d.revert_prs_merged}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </Card>
  );
}
