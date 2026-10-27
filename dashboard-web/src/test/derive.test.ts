import { describe, expect, it } from 'vitest';
import { filterChains, focusPredicate, isStale, matchesQuery, needsActionGroups, spanDepths, activeFilterCount } from '../data/derive';
import type { ChainSummary, ChainsFile, EntityFile, MetaFile } from '../data/types';
import { DEFAULT_VIEW, type ViewState } from '../state/url';
import chainsJson from '../fixtures/populated/chains.json';
import metaJson from '../fixtures/populated/meta.json';
import revertedJson from '../fixtures/populated/entities/c_2c8f0e7a5b1d3f94.json';

const chains = (chainsJson as ChainsFile).chains;
const GEN = '2026-10-25T12:00:00Z';
const ids = (cs: ChainSummary[]) => cs.map((c) => c.chain_id.slice(2, 6));
const view = (patch: Partial<ViewState>): ViewState => ({ ...DEFAULT_VIEW, ...patch });
const filter = (patch: Partial<ViewState>) => ids(filterChains(chains, view(patch), GEN));

describe('derive', () => {
  it('test_range_filter', () => {
    expect(filter({ range: 'all' })).toHaveLength(6);
    expect(filter({ range: '7d' })).toHaveLength(6);
    expect(filter({ range: '30d' })).toHaveLength(6);
    expect(filter({ range: '24h' })).toEqual(['0a1b', '4f1d', '7a3c', '9b2e']);
  });

  it('test_range_keys_on_event_ts', () => {
    // Started 3 days ago, deployed an hour ago: the 24h deploys KPI counts it, so the row is listed.
    const base = chains[0];
    if (!base) throw new Error('fixture chains missing');
    const late: ChainSummary = { ...base, chain_id: 'c_ffff000000000001', started_at: '2026-10-22T12:00:00Z', updated_at: '2026-10-25T11:00:00Z' };
    const old: ChainSummary = { ...base, chain_id: 'c_ffff000000000002', started_at: '2026-10-22T12:00:00Z', updated_at: '2026-10-22T13:00:00Z' };
    expect(ids(filterChains([late, old], view({ range: '24h' }), GEN))).toEqual(['ffff']);
    expect(filterChains([late, old], view({ range: '24h' }), GEN).map((c) => c.chain_id)).toEqual(['c_ffff000000000001']);
  });

  it('test_focus_predicates', () => {
    const by = (f: Parameters<typeof focusPredicate>[0]) => ids(chains.filter(focusPredicate(f)));
    expect(by('incidents')).toEqual(['7a3c', '9b2e', '2c8f']);
    expect(by('awaiting')).toEqual(['7a3c']);
    expect(by('escalations')).toEqual(['9b2e']);
    expect(by('eval')).toEqual(['9b2e', '2c8f']);
    expect(by('failure_rate')).toEqual(['4f1d']);
    expect(by('deploys')).toEqual(['0a1b', '4f1d', '7a3c', '9b2e', '5e6a']);
    expect(by('mttr')).toHaveLength(3);
    expect(filter({ focus: 'escalations' })).toEqual(['9b2e']);
  });

  it('test_search', () => {
    expect(filter({ q: '#57' })).toEqual(['7a3c']);
    expect(filter({ q: '2c8f0e7' })).toEqual(['2c8f']);
    expect(filter({ q: 'pypi' })).toEqual(['2c8f']);
    expect(filter({ q: 'RUNTIME/07' })).toEqual(['9b2e']);
    expect(filter({ q: 'zzz' })).toEqual([]);
    expect(filter({ q: '' })).toHaveLength(6);
    expect(filter({ q: 'c_5e6a7b8c9d0e1f23' })).toEqual(['5e6a']);
    expect(filter({ q: '#5' })).toEqual([]);
    const first = chains[0];
    expect(first && matchesQuery(first, '   ')).toBe(true);
  });

  it('test_combined_filters', () => {
    expect(filter({ repo: 'Keshav0375/Sentinel' })).toEqual(['0a1b']);
    expect(filter({ status: 'failed', repo: 'Keshav0375/Sentinel' })).toEqual([]);
    expect(filter({ scenario: 'deployfail/02' })).toEqual(['2c8f']);
    expect(filter({ kind: 'revert_pr.opened' })).toEqual(['7a3c', '2c8f']);
    expect(filter({ range: '24h', kind: 'revert_pr.opened' })).toEqual(['7a3c']);
    expect(activeFilterCount(view({ repo: 'Keshav0375/Sentinel', q: 'x', range: '24h' }))).toBe(2);
  });

  it('test_needs_action_groups', () => {
    const groups = needsActionGroups(chains);
    expect(groups.map((g) => g.reason)).toEqual(['pr_awaiting_review', 'escalation', 'failed_no_incident']);
    expect(groups.map((g) => ids(g.items))).toEqual([['7a3c'], ['9b2e'], ['4f1d']]);

    const pr = chains.find((c) => c.chain_id === 'c_7a3c9e1b2d4f6a80');
    if (!pr?.needs_action) throw new Error('fixture changed');
    const older: ChainSummary = {
      ...structuredClone(pr),
      chain_id: 'c_1111111111111111',
      needs_action: { ...pr.needs_action, since: '2026-10-24T06:00:00Z' },
    };
    const two = needsActionGroups([...chains, older]);
    expect(two[0]?.items.map((c) => c.chain_id)).toEqual(['c_1111111111111111', 'c_7a3c9e1b2d4f6a80']);
    expect(needsActionGroups([])).toEqual([]);
  });

  it('test_is_stale', () => {
    const meta = metaJson as MetaFile;
    expect(isStale(meta, new Date('2026-10-25T12:59:59Z'))).toBe(false);
    expect(isStale(meta, new Date('2026-10-25T13:00:00Z'))).toBe(false);
    expect(isStale(meta, new Date('2026-10-25T13:00:01Z'))).toBe(true);
  });

  it('test_span_depths', () => {
    const trace = (revertedJson as EntityFile).trace;
    if (!trace) throw new Error('fixture changed');
    const depths = spanDepths(trace.spans);
    const byName = (n: string) => depths.get(trace.spans.find((s) => s.name === n)?.span_id ?? '');
    expect(byName('incident_pipeline')).toBe(0);
    expect(byName('triage_agent')).toBe(1);
    expect(byName('get_service_metadata')).toBe(2);
    const child = trace.spans.find((s) => s.span_id === 's2');
    if (!child) throw new Error('fixture changed');
    expect(spanDepths([{ ...child, parent_id: 'missing' }]).get('s2')).toBe(0);
  });
});
