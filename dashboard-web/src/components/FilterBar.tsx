import { Search, X } from 'lucide-react';
import { Badge } from './ui/Badge';
import { Button, FOCUS_RING } from './ui/Button';
import { Select } from './ui/Select';
import { activeFilterCount, kindFamily, SCENARIOS, type KindFamily } from '../data/derive';
import { CHAIN_STATUS_META, FOCUS_META, KIND_FAMILY_META, KIND_LABEL } from '../data/meta';
import { CHAIN_STATUSES, KINDS, REPOS, SCENARIO_CASES, type Kind } from '../data/types';
import { repoName } from '../data/format';
import { useView, type SortKey, type ViewState } from '../state/url';

export const SEARCH_INPUT_ID = 'chain-search';

const SORT_LABELS: Record<SortKey, string> = {
  started_desc: 'Newest first',
  started_asc: 'Oldest first',
  duration_desc: 'Longest first',
  duration_asc: 'Shortest first',
};

const KIND_GROUPS = KINDS.reduce<[KindFamily, Kind[]][]>((groups, k) => {
  const family = kindFamily(k);
  const last = groups[groups.length - 1];
  if (last && last[0] === family) last[1].push(k);
  else groups.push([family, [k]]);
  return groups;
}, []);

const CASE_LABELS = { pass: 'Pass', deployfail: 'Deploy failure', runtime: 'Runtime' } as const;

const CLEARED: Partial<ViewState> = { focus: null, repo: null, kind: null, status: null, scenario: null, q: '' };

export function FilterBar({ total, shown }: { total: number; shown: number }) {
  const { view, update } = useView();

  const chips: { key: string; label: string; clear: Partial<ViewState> }[] = [];
  if (view.focus) chips.push({ key: 'focus', label: `KPI: ${FOCUS_META[view.focus].label}`, clear: { focus: null } });
  if (view.repo) chips.push({ key: 'repo', label: `Repo: ${repoName(view.repo)}`, clear: { repo: null } });
  if (view.kind) chips.push({ key: 'kind', label: `Kind: ${KIND_LABEL(view.kind)}`, clear: { kind: null } });
  if (view.status) chips.push({ key: 'status', label: `Status: ${CHAIN_STATUS_META[view.status].label}`, clear: { status: null } });
  if (view.scenario) chips.push({ key: 'scenario', label: `Scenario: ${view.scenario}`, clear: { scenario: null } });
  if (view.q.trim() !== '') chips.push({ key: 'q', label: `Search: “${view.q.trim()}”`, clear: { q: '' } });

  return (
    <div className="space-y-3">
      <div role="search" className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-6">
        <div className="sm:col-span-2">
          <label htmlFor={SEARCH_INPUT_ID} className="mb-1 block text-xs font-medium text-fg-muted">
            Search chains
          </label>
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-fg-subtle" aria-hidden="true" />
            <input
              id={SEARCH_INPUT_ID}
              type="search"
              aria-label="Search chains"
              placeholder="Search title, summary, SHA or #PR"
              autoComplete="off"
              spellCheck={false}
              value={view.q}
              onChange={(e) => update({ q: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === 'Escape' && view.q !== '') {
                  e.preventDefault();
                  update({ q: '' });
                }
              }}
              className={`h-10 w-full rounded-sm border border-border-input bg-surface pr-10 pl-9 text-sm text-fg placeholder:text-fg-subtle ${FOCUS_RING}`}
            />
            <kbd
              aria-hidden="true"
              className="pointer-events-none absolute top-1/2 right-2.5 hidden -translate-y-1/2 rounded border border-border bg-surface-2 px-1.5 font-mono text-[11px] text-fg-subtle md:block"
            >
              /
            </kbd>
          </div>
        </div>
        <Select label="Repo" value={view.repo ?? ''} onChange={(v) => update({ repo: v === '' ? null : (v as ViewState['repo']) })}>
          <option value="">All repos</option>
          {REPOS.map((r) => (
            <option key={r} value={r}>
              {repoName(r)}
            </option>
          ))}
        </Select>
        <Select label="Kind" value={view.kind ?? ''} onChange={(v) => update({ kind: v === '' ? null : (v as Kind) })}>
          <option value="">All kinds</option>
          {KIND_GROUPS.map(([family, kinds]) => (
            <optgroup key={family} label={KIND_FAMILY_META[family].label}>
              {kinds.map((k) => (
                <option key={k} value={k}>
                  {KIND_LABEL(k)}
                </option>
              ))}
            </optgroup>
          ))}
        </Select>
        <Select
          label="Status"
          value={view.status ?? ''}
          onChange={(v) => update({ status: v === '' ? null : (v as ViewState['status']) })}
        >
          <option value="">All statuses</option>
          {CHAIN_STATUSES.map((s) => (
            <option key={s} value={s}>
              {CHAIN_STATUS_META[s].label}
            </option>
          ))}
        </Select>
        <Select
          label="Scenario"
          value={view.scenario ?? ''}
          onChange={(v) => update({ scenario: v === '' ? null : (v as ViewState['scenario']) })}
        >
          <option value="">All scenarios</option>
          {SCENARIO_CASES.map((c) => (
            <optgroup key={c} label={CASE_LABELS[c]}>
              {SCENARIOS.filter((s) => s.startsWith(`${c}/`)).map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </optgroup>
          ))}
        </Select>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          {chips.map((chip) => (
            <Badge key={chip.key} tone="accent" className="py-0 pr-0.5">
              {chip.label}
              <button
                type="button"
                aria-label={`Remove filter ${chip.label}`}
                onClick={() => update(chip.clear)}
                className={`-my-px inline-flex size-6 items-center justify-center rounded-full hover:bg-accent/15 ${FOCUS_RING}`}
              >
                <X className="size-3.5" aria-hidden="true" />
              </button>
            </Badge>
          ))}
          {activeFilterCount(view) > 0 && (
            <Button variant="ghost" size="sm" onClick={() => update(CLEARED)}>
              Clear all
            </Button>
          )}
          <p aria-live="polite" className="text-xs text-fg-subtle tabular-nums">
            Showing {shown} of {total} chains
          </p>
        </div>
        <Select
          label="Sort"
          className="flex items-center gap-2 [&>label]:mb-0"
          value={view.sort}
          onChange={(v) => update({ sort: v as SortKey })}
        >
          {Object.entries(SORT_LABELS).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </Select>
      </div>
    </div>
  );
}
