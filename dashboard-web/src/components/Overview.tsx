import { useId } from 'react';
import { Bell, FlaskConical, GitPullRequest, Rocket, Siren, Timer, TriangleAlert } from 'lucide-react';
import { DailyActivity } from './DailyActivity';
import { KpiCard } from './KpiCard';
import { TimeRangePicker } from './TimeRangePicker';
import { Time } from './ui/Tooltip';
import { formatCount, formatDuration, formatElapsed, formatPct, secondsBetween } from '../data/format';
import type { MetricsFile, Timestamp } from '../data/types';
import { useNow } from '../state/now';
import { useView } from '../state/url';

export const KPI_GRID = 'grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7';

export function Overview({ metrics, generatedAt }: { metrics: MetricsFile; generatedAt: Timestamp }) {
  const { view } = useView();
  const now = useNow();
  const headingId = useId();
  const k = metrics.ranges[view.range];
  const cur = metrics.current;
  return (
    <section aria-labelledby={headingId} className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 id={headingId} className="text-base font-semibold">
            Overview
          </h2>
          <p className="text-xs text-fg-subtle">
            Window ends at the snapshot time (<Time iso={generatedAt} />).
          </p>
        </div>
        <TimeRangePicker />
      </div>
      <div className={KPI_GRID}>
        <KpiCard
          focus="deploys"
          label="Deploys"
          value={formatCount(k.deploys)}
          sub={`${k.deploy_failures} failed · ${k.builds} builds (${k.build_failures} failed)`}
          Icon={Rocket}
        />
        <KpiCard
          focus="failure_rate"
          label="Failure rate"
          value={formatPct(k.failure_rate)}
          sub={`${k.deploy_failures} of ${k.deploys} deploys`}
          Icon={TriangleAlert}
        />
        <KpiCard focus="incidents" label="Incidents" value={formatCount(k.incidents)} sub="alerts that started a chain" Icon={Bell} />
        <KpiCard
          focus="mttr"
          label="MTTR p50"
          value={formatDuration(k.mttr_p50_s)}
          sub={`p90 ${formatDuration(k.mttr_p90_s)}`}
          Icon={Timer}
        />
        <KpiCard
          focus="awaiting"
          label="Awaiting review"
          value={formatCount(cur.revert_prs_awaiting_review)}
          sub={
            cur.oldest_awaiting_since
              ? `oldest waiting ${formatElapsed(secondsBetween(cur.oldest_awaiting_since, now))}`
              : 'none waiting'
          }
          badge="now"
          Icon={GitPullRequest}
        />
        <KpiCard
          focus="escalations"
          label="Escalations"
          value={formatCount(k.escalations)}
          sub={`${cur.escalations_open} open now`}
          Icon={Siren}
        />
        <KpiCard
          focus="eval"
          label="Eval score"
          value={formatPct(k.eval_score_mean)}
          sub={`${k.eval_runs} scored runs`}
          Icon={FlaskConical}
        />
      </div>
      <DailyActivity daily={metrics.daily} />
    </section>
  );
}
