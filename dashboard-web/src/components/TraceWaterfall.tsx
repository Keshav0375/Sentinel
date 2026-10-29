import clsx from 'clsx';
import { CircleX, TriangleAlert } from 'lucide-react';
import { Button } from './ui/Button';
import { spanDepths } from '../data/derive';
import { formatDuration, formatTokens } from '../data/format';
import type { SourceStatus, SpanStatus, Trace } from '../data/types';
import { useView } from '../state/url';

const BAR: Record<SpanStatus, string> = { ok: 'bg-accent', warning: 'bg-warning', error: 'bg-danger' };

export function traceTotalMs(trace: Trace): number {
  return trace.total_duration_ms || Math.max(0, ...trace.spans.map((s) => s.start_ms + s.duration_ms)) || 1;
}

export default function TraceWaterfall({ trace, langfuse }: { trace: Trace | null; langfuse: SourceStatus }) {
  const { view, update } = useView();
  if (!trace) {
    return (
      <p className="text-sm text-fg-muted">
        No trace summary recorded for this chain.
        {langfuse !== 'ok' && ` LangFuse was ${langfuse === 'skipped' ? 'skipped' : 'unavailable'} in the last refresh.`}
      </p>
    );
  }
  const total = traceTotalMs(trace);
  const summary = `Trace · ${trace.spans.length} spans · ${formatDuration(total / 1000)}`;
  if (!view.traces) {
    return (
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-fg-muted tabular-nums">{summary}</p>
        <Button size="sm" variant="secondary" onClick={() => update({ traces: true })}>
          Show trace
        </Button>
      </div>
    );
  }
  const depths = spanDepths(trace.spans);
  return (
    <div className="space-y-3">
      <p className="text-sm text-fg-muted tabular-nums">{summary}</p>
      <ol aria-label="Trace spans" className="space-y-2">
        {trace.spans.map((s) => {
          const depth = Math.min(depths.get(s.span_id) ?? 0, 6);
          const left = (s.start_ms / total) * 100;
          const width = Math.max((s.duration_ms / total) * 100, 0.5);
          const tokens = s.input_tokens !== null || s.output_tokens !== null;
          return (
            <li key={s.span_id} className="grid grid-cols-1 gap-1 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] sm:items-center sm:gap-3">
              <div className="flex min-w-0 items-center gap-2" style={{ paddingInlineStart: `${depth * 12}px` }}>
                <span className="truncate font-mono text-xs" title={s.name}>
                  {s.name}
                </span>
                <span className="shrink-0 rounded-sm bg-surface-2 px-1 font-mono text-[10px] text-fg-muted">{s.kind}</span>
                {s.status === 'warning' && (
                  <span className="inline-flex shrink-0 items-center gap-0.5 text-xs text-warning">
                    <TriangleAlert className="size-3.5" aria-hidden="true" />
                    warning
                  </span>
                )}
                {s.status === 'error' && (
                  <span className="inline-flex shrink-0 items-center gap-0.5 text-xs text-danger">
                    <CircleX className="size-3.5" aria-hidden="true" />
                    error
                  </span>
                )}
              </div>
              <div className="flex min-w-0 items-center gap-2">
                <div className="relative h-2 flex-1 rounded bg-surface-2">
                  <div
                    className={clsx('absolute inset-y-0 rounded', BAR[s.status])}
                    style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%` }}
                  />
                </div>
                <span className="w-28 shrink-0 text-right text-xs text-fg-subtle tabular-nums">
                  {formatDuration(s.duration_ms / 1000)}
                  {tokens && (
                    <span className="block">
                      {formatTokens(s.input_tokens)} in · {formatTokens(s.output_tokens)} out
                    </span>
                  )}
                </span>
              </div>
            </li>
          );
        })}
      </ol>
      <p className="text-xs text-fg-subtle">Span names, timings and status only; no inputs or outputs.</p>
    </div>
  );
}
