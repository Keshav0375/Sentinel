import { CircleCheck, CircleX, TriangleAlert } from 'lucide-react';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { ExternalLink } from './ui/ExternalLink';
import { Time } from './ui/Tooltip';
import { formatPct } from '../data/format';
import type { Checks, EvalScore, Judge } from '../data/types';
import { useView } from '../state/url';

const CHECKS: [keyof Checks, string][] = [
  ['fired', 'Fired'],
  ['classified', 'Classified'],
  ['correlated', 'Correlated'],
  ['revert_target', 'Revert target'],
];

const JUDGE: [keyof Judge, string][] = [
  ['triage_accuracy', 'Triage accuracy'],
  ['root_cause_correctness', 'Root-cause correctness'],
  ['tool_efficiency', 'Tool efficiency'],
  ['mttr', 'MTTR'],
  ['remediation_safety', 'Remediation safety'],
];

const EXCLUDED = 'The judge could not score this run, so it is excluded from averages.';

function CheckValue({ value }: { value: number }) {
  if (value === 1) {
    return (
      <span className="inline-flex items-center gap-1 text-success">
        <CircleCheck className="size-3.5" aria-hidden="true" />
        pass
      </span>
    );
  }
  if (value === 0) {
    return (
      <span className="inline-flex items-center gap-1 text-danger">
        <CircleX className="size-3.5" aria-hidden="true" />
        fail
      </span>
    );
  }
  return <span className="tabular-nums">{formatPct(value)}</span>;
}

function EvalCard({ e }: { e: EvalScore }) {
  return (
    <article className="space-y-4 rounded-md border border-border p-3 sm:p-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-0.5">
          <p className="flex flex-wrap items-center gap-2 text-sm font-medium">
            {e.scenario && <span className="font-mono">{e.scenario}</span>}
            {e.case && <span className="text-xs text-fg-subtle">case {e.case}</span>}
          </p>
          <p className="text-xs text-fg-subtle">
            Scored <Time iso={e.scored_at} />
          </p>
        </div>
        <p className="text-right">
          <span className="block text-xs text-fg-subtle">Total</span>
          <span className="text-2xl font-semibold tabular-nums">{formatPct(e.total_score)}</span>
        </p>
      </header>
      {(e.judge_failed || e.judge_conflict) && (
        <div className="space-y-1">
          <Badge tone="warning" Icon={TriangleAlert}>
            {e.judge_failed ? 'Judge failed' : 'Judge conflict'}
          </Badge>
          <p className="text-xs text-fg-muted">{EXCLUDED}</p>
        </div>
      )}
      {e.checks && (
        <div>
          <h4 className="mb-1.5 text-xs font-medium text-fg-muted">Deterministic checks</h4>
          <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {CHECKS.map(([key, label]) => (
              <div key={key} className="rounded-md bg-surface-2 px-2.5 py-1.5">
                <dt className="text-xs text-fg-subtle">{label}</dt>
                <dd className="text-sm font-medium">
                  <CheckValue value={e.checks?.[key] ?? 0} />
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}
      {e.judge && (
        <div>
          <h4 className="mb-1.5 text-xs font-medium text-fg-muted">LLM judge</h4>
          <ul className="space-y-1.5">
            {JUDGE.map(([key, label]) => {
              const pct = Math.round((e.judge?.[key] ?? 0) * 100);
              return (
                <li key={key} className="grid grid-cols-[minmax(0,10rem)_minmax(0,1fr)_3rem] items-center gap-2 text-xs">
                  <span className="truncate text-fg-muted">{label}</span>
                  <div
                    role="meter"
                    aria-label={label}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={pct}
                    aria-valuetext={`${pct}%`}
                    className="h-2 overflow-hidden rounded-full bg-surface-2"
                  >
                    <div className="h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
                  </div>
                  <span className="text-right font-medium tabular-nums">{pct}%</span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-fg-subtle">
        {e.judge_model && (
          <span>
            Judge <span className="font-mono text-fg-muted">{e.judge_model}</span>
          </span>
        )}
        {e.langfuse_trace_url && (
          <ExternalLink href={e.langfuse_trace_url} restricted>
            Eval trace in LangFuse
          </ExternalLink>
        )}
      </div>
    </article>
  );
}

export default function EvalScores({ evals }: { evals: EvalScore[] }) {
  const { view, update } = useView();
  if (evals.length === 0) return <p className="text-sm text-fg-muted">This chain has not been scored.</p>;
  if (!view.eval) {
    const latest = evals[evals.length - 1];
    return (
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-fg-muted tabular-nums">
          Eval · {evals.length} {evals.length === 1 ? 'run' : 'runs'} · score {formatPct(latest?.total_score ?? null)}
        </p>
        <Button size="sm" variant="secondary" onClick={() => update({ eval: true })}>
          Show eval
        </Button>
      </div>
    );
  }
  return (
    <div className="space-y-3">
      {evals.map((e) => (
        <EvalCard key={`${e.scored_at}-${e.scenario ?? ''}`} e={e} />
      ))}
    </div>
  );
}
