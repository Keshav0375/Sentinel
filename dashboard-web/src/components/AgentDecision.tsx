import { Siren, Undo2 } from 'lucide-react';
import { Badge } from './ui/Badge';
import { ExternalLink } from './ui/ExternalLink';
import { Time } from './ui/Tooltip';
import { ConfidenceMeter } from './ConfidenceMeter';
import { formatDuration } from '../data/format';
import type { AgentDecision as Decision } from '../data/types';

export function AgentDecision({ agent }: { agent: Decision | null }) {
  if (!agent) return <p className="text-sm text-fg-muted">No agent run in this chain.</p>;
  const rollback = agent.resolution_type === 'rollback';
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        {rollback ? (
          <Badge tone="success" Icon={Undo2}>
            Rollback
          </Badge>
        ) : (
          <Badge tone="danger" Icon={Siren}>
            Escalated
          </Badge>
        )}
        <ConfidenceMeter value={agent.confidence} />
      </div>
      <p className="text-sm">{agent.root_cause_summary ?? <span className="text-fg-muted">No summary recorded.</span>}</p>
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          ['Tool calls', agent.tool_calls === null ? '—' : String(agent.tool_calls)],
          ['Reflexion loops', agent.reflexion_loops === null ? '—' : String(agent.reflexion_loops)],
          ['MTTR', formatDuration(agent.mttr_s)],
        ].map(([label, value]) => (
          <div key={label} className="rounded-md border border-border px-3 py-2">
            <dt className="text-xs text-fg-subtle">{label}</dt>
            <dd className="text-sm font-medium tabular-nums">{value}</dd>
          </div>
        ))}
        <div className="rounded-md border border-border px-3 py-2">
          <dt className="text-xs text-fg-subtle">Decided</dt>
          <dd className="text-sm font-medium tabular-nums">
            <Time iso={agent.decided_at} />
          </dd>
        </div>
      </dl>
      {agent.models_used.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-fg-subtle">Models</span>
          {agent.models_used.map((m) => (
            <span key={m} className="rounded-sm border border-border bg-surface-2 px-1.5 py-0.5 font-mono text-xs">
              {m}
            </span>
          ))}
        </div>
      )}
      {agent.langfuse_trace_url && (
        <p className="text-sm">
          <ExternalLink href={agent.langfuse_trace_url} restricted>
            Agent trace in LangFuse
          </ExternalLink>
        </p>
      )}
      <p className="text-xs text-fg-subtle">
        LLM-written summary, capped at 500 characters. Raw logs and tool output are never published.
      </p>
    </div>
  );
}
