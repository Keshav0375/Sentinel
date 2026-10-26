import { Card } from './ui/Card';
import { Time } from './ui/Tooltip';
import { formatPct } from '../data/format';
import type { Checks, MetricsFile } from '../data/types';

const CHECK_LABELS: [keyof Checks, string][] = [
  ['fired', 'Fired'],
  ['classified', 'Classified'],
  ['correlated', 'Correlated'],
  ['revert_target', 'Revert target'],
];

function ChecksPass({ checks }: { checks: Checks | null }) {
  if (!checks) return <span className="text-fg-subtle">—</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {CHECK_LABELS.map(([key, label]) => (
        <span key={key} className="rounded-sm bg-surface-2 px-1.5 text-xs tabular-nums" title={label}>
          <span className="text-fg-subtle">{label}</span> {formatPct(checks[key])}
        </span>
      ))}
    </span>
  );
}

export function EvalScorecard({ metrics }: { metrics: MetricsFile }) {
  const rows = metrics.eval.scenarios;
  return (
    <Card as="section" title="Eval scorecard">
      <div className="px-4 pt-1 pb-4 sm:px-5 sm:pb-5">
        <p className="mb-3 text-xs text-fg-subtle">
          Deterministic checks plus an LLM judge, per catalog scenario. Failed or conflicting judge runs are excluded from means.
        </p>
        {rows.length === 0 ? (
          <p className="py-6 text-center text-sm text-fg-muted">No eval runs scored yet.</p>
        ) : (
          <>
            <ul className="space-y-2 md:hidden">
              {rows.map((r) => (
                <li key={r.scenario} className="space-y-2 rounded-md border border-border p-3">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="font-mono text-sm">{r.scenario}</span>
                    <span className="text-lg font-semibold tabular-nums">{formatPct(r.last_total_score)}</span>
                  </div>
                  <p className="text-xs text-fg-subtle tabular-nums">
                    {r.case ? `case ${r.case} · ` : ''}
                    {r.runs} {r.runs === 1 ? 'run' : 'runs'} · mean {formatPct(r.mean_total_score)} · judge failures {r.judge_failures}
                    {' · '}conflicts {r.judge_conflicts}
                  </p>
                  <ChecksPass checks={r.checks_pass_rate} />
                </li>
              ))}
            </ul>
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-border text-xs text-fg-subtle">
                  <tr>
                    <th scope="col" className="py-2 pr-3 font-medium">Scenario</th>
                    <th scope="col" className="py-2 pr-3 font-medium">Case</th>
                    <th scope="col" className="py-2 pr-3 text-right font-medium">Runs</th>
                    <th scope="col" className="py-2 pr-3 text-right font-medium">Last score</th>
                    <th scope="col" className="py-2 pr-3 text-right font-medium">Mean</th>
                    <th scope="col" className="py-2 pr-3 font-medium">Checks pass rate</th>
                    <th scope="col" className="py-2 font-medium">Judge failures / conflicts</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {rows.map((r) => (
                    <tr key={r.scenario}>
                      <th scope="row" className="py-2.5 pr-3 font-mono text-xs font-normal">
                        {r.scenario}
                        <span className="block font-sans text-[11px] text-fg-subtle">
                          last <Time iso={r.last_scored_at} />
                        </span>
                      </th>
                      <td className="py-2.5 pr-3 text-fg-muted">{r.case ?? '—'}</td>
                      <td className="py-2.5 pr-3 text-right tabular-nums">{r.runs}</td>
                      <td className="py-2.5 pr-3 text-right font-medium tabular-nums">{formatPct(r.last_total_score)}</td>
                      <td className="py-2.5 pr-3 text-right tabular-nums">{formatPct(r.mean_total_score)}</td>
                      <td className="py-2.5 pr-3">
                        <ChecksPass checks={r.checks_pass_rate} />
                      </td>
                      <td className="py-2.5 tabular-nums">
                        {r.judge_failures} / {r.judge_conflicts}
                      </td>
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
