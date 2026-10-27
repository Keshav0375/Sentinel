import { parseISO } from 'date-fns';
import { secondsBetween } from './format';
import {
  NEEDS_ACTION_REASONS,
  SCENARIO_CASES,
  type ChainSummary,
  type Kind,
  type MetaFile,
  type NeedsActionReason,
  type RangeKey,
  type Scenario,
  type Span,
  type Timestamp,
} from './types';
import type { ViewState } from '../state/url';

export const RANGE_SECONDS: Record<RangeKey, number | null> = {
  '24h': 86400,
  '7d': 604800,
  '30d': 2592000,
  all: null,
};

/** The 30 catalog scenarios, case-major: `pass/01` … `runtime/10`. */
export const SCENARIOS: Scenario[] = SCENARIO_CASES.flatMap((c) =>
  Array.from({ length: 10 }, (_, i) => `${c}/${String(i + 1).padStart(2, '0')}` as Scenario),
);

export const KPI_FOCI = ['deploys', 'failure_rate', 'incidents', 'mttr', 'awaiting', 'escalations', 'eval'] as const;
export type KpiFocus = (typeof KPI_FOCI)[number];

const hasKind = (c: ChainSummary, k: Kind) => c.kinds.includes(k);

export function focusPredicate(f: KpiFocus): (c: ChainSummary) => boolean {
  switch (f) {
    case 'deploys':
      return (c) => c.kinds.some((k) => k.startsWith('deploy.'));
    case 'failure_rate':
      return (c) => hasKind(c, 'deploy.failed');
    case 'incidents':
      return (c) => hasKind(c, 'alert.received');
    case 'mttr':
      return (c) => hasKind(c, 'agent.decided');
    case 'awaiting':
      return (c) => c.status === 'awaiting_review';
    case 'escalations':
      return (c) => c.status === 'escalated' || hasKind(c, 'escalation.raised');
    case 'eval':
      return (c) => c.has_eval;
  }
}

const SHA_QUERY = /^[0-9a-f]{4,40}$/;
const PR_QUERY = /^#(\d+)$/;

/** Search: title/summary substring, SHA prefix, `#<pr>` exact, or chain id exact. */
export function matchesQuery(c: ChainSummary, q: string): boolean {
  const needle = q.trim().toLowerCase();
  if (needle === '') return true;
  if (c.chain_id === needle) return true;
  const pr = PR_QUERY.exec(needle);
  if (pr) return c.pr === Number(pr[1]);
  if (SHA_QUERY.test(needle) && c.sha?.startsWith(needle)) return true;
  return c.title.toLowerCase().includes(needle) || (c.summary ?? '').toLowerCase().includes(needle);
}

/**
 * The range keys on event ts, like the KPI windows (metrics.json counts events whose ts is in the
 * window): a chain is in range when its latest event (`updated_at`) is, so every chain holding an
 * event a KPI counted is listed. Keying on `started_at` would drop a chain that started before the
 * window but deployed or alerted inside it.
 */
export function filterChains(chains: ChainSummary[], v: ViewState, generatedAt: Timestamp): ChainSummary[] {
  const window = RANGE_SECONDS[v.range];
  const end = parseISO(generatedAt).getTime();
  const focus = v.focus ? focusPredicate(v.focus) : null;
  return chains.filter(
    (c) =>
      (window === null || parseISO(c.updated_at).getTime() >= end - window * 1000) &&
      (focus === null || focus(c)) &&
      (v.repo === null || c.repo === v.repo) &&
      (v.kind === null || c.kinds.includes(v.kind)) &&
      (v.status === null || c.status === v.status) &&
      (v.scenario === null || c.scenario === v.scenario) &&
      matchesQuery(c, v.q),
  );
}

export interface NeedsActionGroup {
  reason: NeedsActionReason;
  items: ChainSummary[];
}

/** Spec order, oldest `since` first, empty groups omitted. Ignores range and filters: it is "now". */
export function needsActionGroups(chains: ChainSummary[]): NeedsActionGroup[] {
  return NEEDS_ACTION_REASONS.map((reason) => ({
    reason,
    items: chains
      .filter((c) => c.needs_action?.reason === reason)
      .sort((a, b) => (a.needs_action?.since ?? '').localeCompare(b.needs_action?.since ?? '')),
  })).filter((g) => g.items.length > 0);
}

export const STALE_AFTER_S = 3600;

export function isStale(meta: MetaFile, now: Date): boolean {
  return secondsBetween(meta.generated_at, now) > STALE_AFTER_S;
}

/** Nesting depth per span; parents precede children, an unknown parent counts as a root. */
export function spanDepths(spans: Span[]): Map<string, number> {
  const depths = new Map<string, number>();
  for (const s of spans) {
    const parent = s.parent_id === null ? undefined : depths.get(s.parent_id);
    depths.set(s.span_id, parent === undefined ? 0 : parent + 1);
  }
  return depths;
}

export type KindFamily = 'build' | 'deploy' | 'ci' | 'scale' | 'alert' | 'agent' | 'revert_pr' | 'escalation' | 'eval' | 'trace';

export function kindFamily(k: Kind): KindFamily {
  return k.slice(0, k.indexOf('.')) as KindFamily;
}

export function activeFilterCount(v: ViewState): number {
  return [v.focus, v.repo, v.kind, v.status, v.scenario].filter((x) => x !== null).length + (v.q !== '' ? 1 : 0);
}
