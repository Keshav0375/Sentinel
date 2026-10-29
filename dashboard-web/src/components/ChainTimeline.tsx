import { useDeferredValue, useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import clsx from 'clsx';
import { ChevronRight, SearchX } from 'lucide-react';
import {
  getCoreRowModel,
  getExpandedRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type ExpandedState,
  type SortingState,
} from '@tanstack/react-table';
import { useWindowVirtualizer } from '@tanstack/react-virtual';
import { StatusBadge } from './ui/Badge';
import { Button } from './ui/Button';
import { Card } from './ui/Card';
import { Time } from './ui/Tooltip';
import { ChainRowDetails } from './ChainRowDetails';
import { EmptyState, TIMELINE_EMPTY } from './EmptyState';
import { FilterBar } from './FilterBar';
import { StagePills } from './StagePills';
import { filterChains } from '../data/derive';
import { formatAgo, formatDuration, repoName, shortSha } from '../data/format';
import type { ChainSummary, Timestamp } from '../data/types';
import { useNow } from '../state/now';
import { useView, type SortKey, type ViewState } from '../state/url';

const COLUMNS: ColumnDef<ChainSummary>[] = [
  { id: 'started_at', accessorKey: 'started_at', sortingFn: 'basic' },
  { id: 'duration', accessorFn: (r) => r.duration_s ?? -1, sortingFn: 'basic' },
];

const SORTING: Record<SortKey, SortingState> = {
  started_desc: [{ id: 'started_at', desc: true }],
  started_asc: [{ id: 'started_at', desc: false }],
  duration_desc: [{ id: 'duration', desc: true }],
  duration_asc: [{ id: 'duration', desc: false }],
};

const ROW_GRID =
  'grid grid-cols-[2.5rem_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-2 ' +
  'md:grid-cols-[2.5rem_8.5rem_minmax(0,1.6fr)_minmax(0,2.4fr)_7rem_5.5rem_auto]';

const CLEARED: Partial<ViewState> = { focus: null, repo: null, kind: null, status: null, scenario: null, q: '' };

export function ChainTimeline({ chains, generatedAt }: { chains: ChainSummary[]; generatedAt: Timestamp }) {
  const { view, update } = useView();
  const now = useNow();
  const headingId = useId();
  const q = useDeferredValue(view.q);
  const filtered = useMemo(() => filterChains(chains, { ...view, q }, generatedAt), [chains, view, q, generatedAt]);
  const [expanded, setExpanded] = useState<ExpandedState>({});

  const table = useReactTable({
    data: filtered,
    columns: COLUMNS,
    state: { sorting: SORTING[view.sort], expanded },
    onExpandedChange: setExpanded,
    getRowId: (r) => r.chain_id,
    getRowCanExpand: () => true,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getExpandedRowModel: getExpandedRowModel(),
    // Expansion is page state: keep it when the data or sort changes.
    autoResetExpanded: false,
  });
  const rows = table.getRowModel().rows;

  const listRef = useRef<HTMLUListElement>(null);
  const [scrollMargin, setScrollMargin] = useState(0);
  useLayoutEffect(() => {
    const measure = () => {
      const el = listRef.current;
      if (!el) return;
      const top = Math.round(el.getBoundingClientRect().top + window.scrollY);
      setScrollMargin((prev) => (prev === top ? prev : top));
    };
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  });

  const virtualizer = useWindowVirtualizer({
    count: rows.length,
    estimateSize: () => 76,
    overscan: 8,
    scrollMargin,
    initialRect: { width: 1024, height: 800 },
    getItemKey: (i) => rows[i]?.id ?? i,
  });

  const focusRowToggle = (index: number, attempt = 0) => {
    const btn = listRef.current?.querySelector<HTMLButtonElement>(`[data-row-index="${index}"] [data-row-toggle]`);
    if (btn) btn.focus();
    else if (attempt < 5) requestAnimationFrame(() => focusRowToggle(index, attempt + 1));
  };

  const onToggleKeyDown = (e: KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    const target = index + (e.key === 'ArrowDown' ? 1 : -1);
    if (target < 0 || target >= rows.length) return;
    e.preventDefault();
    virtualizer.scrollToIndex(target, { align: 'auto', behavior: 'auto' });
    focusRowToggle(target);
  };

  return (
    <Card as="div" className="scroll-mt-20">
      <section id="timeline" aria-labelledby={headingId} tabIndex={-1} className="outline-none">
        <div className="space-y-4 px-4 pt-4 pb-3 sm:px-5">
          <h2 id={headingId} className="text-base font-semibold">
            Chains
          </h2>
          {chains.length > 0 && <FilterBar total={chains.length} shown={filtered.length} />}
        </div>
        {chains.length === 0 ? (
          <EmptyState title={TIMELINE_EMPTY.title} body={TIMELINE_EMPTY.body} />
        ) : filtered.length === 0 ? (
          <EmptyState
            Icon={SearchX}
            title="No chains match these filters"
            body="Try a wider time range or remove a filter."
            action={
              <Button variant="secondary" onClick={() => update(CLEARED)}>
                Clear filters
              </Button>
            }
          />
        ) : (
          <>
            <div
              aria-hidden="true"
              className={clsx(
                ROW_GRID,
                'hidden border-y border-border bg-surface-2 px-3 py-2 text-xs font-medium text-fg-subtle sm:px-4 md:grid',
              )}
            >
              <span />
              <span>Status</span>
              <span>Chain</span>
              <span>Stages</span>
              <span>Started</span>
              <span>Duration</span>
              <span />
            </div>
            <ul
              ref={listRef}
              aria-label="Chains"
              className="relative border-t border-border md:border-t-0"
              style={{ height: virtualizer.getTotalSize() }}
            >
              {virtualizer.getVirtualItems().map((item) => {
                const row = rows[item.index];
                if (!row) return null;
                const c = row.original;
                const open = row.getIsExpanded();
                const detailsId = `row-${c.chain_id}`;
                return (
                  <li
                    key={item.key}
                    ref={virtualizer.measureElement}
                    data-index={item.index}
                    data-row-index={item.index}
                    className="absolute top-0 left-0 w-full border-b border-border"
                    style={{ transform: `translateY(${item.start - virtualizer.options.scrollMargin}px)` }}
                  >
                    <div className={clsx(ROW_GRID, 'px-3 py-3 sm:px-4', open && 'bg-surface-2/60')}>
                      <Button
                        size="icon"
                        variant="ghost"
                        data-row-toggle
                        aria-expanded={open}
                        aria-controls={detailsId}
                        aria-label={`Show stages for ${c.title}`}
                        onClick={row.getToggleExpandedHandler()}
                        onKeyDown={(e) => onToggleKeyDown(e, item.index)}
                        className="row-start-1 self-start md:self-center"
                      >
                        <ChevronRight
                          className={clsx('size-4 transition-transform duration-[var(--dur-fast)]', open && 'rotate-90')}
                          aria-hidden="true"
                        />
                      </Button>
                      <div className="col-start-2 row-start-1 flex min-w-0 flex-col items-start gap-1.5 md:contents">
                        <div className="md:col-start-2 md:row-start-1">
                          <StatusBadge status={c.status} />
                        </div>
                        <div className="min-w-0 max-w-full md:col-start-3 md:row-start-1">
                          <p className="truncate text-sm font-medium" title={c.title}>
                            {c.title}
                          </p>
                          <p className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-fg-subtle">
                            <span>{repoName(c.repo)}</span>
                            {c.scenario && (
                              <span className="rounded-sm bg-surface-2 px-1.5 font-mono text-[11px] text-fg-muted">{c.scenario}</span>
                            )}
                            {c.sha && <span className="font-mono">{shortSha(c.sha)}</span>}
                            {c.pr !== null && <span className="tabular-nums">#{c.pr}</span>}
                          </p>
                        </div>
                      </div>
                      <div className="col-start-3 row-start-1 self-start md:col-start-7 md:self-center">
                        <Button
                          size="sm"
                          variant="secondary"
                          aria-label={`Open details for ${c.title}`}
                          onClick={() => update({ chain: c.chain_id }, { push: true })}
                        >
                          Details
                        </Button>
                      </div>
                      <div className="col-span-2 col-start-2 row-start-2 min-w-0 md:col-span-1 md:col-start-4 md:row-start-1">
                        <StagePills stages={c.stages} compact />
                      </div>
                      <div className="col-span-2 col-start-2 row-start-3 flex gap-2 text-xs text-fg-subtle tabular-nums md:contents">
                        <span className="md:col-start-5 md:row-start-1">
                          <span className="sr-only md:hidden">Started </span>
                          <Time iso={c.started_at} text={formatAgo(c.started_at, now)} />
                        </span>
                        <span aria-hidden="true" className="md:hidden">
                          ·
                        </span>
                        <span className="md:col-start-6 md:row-start-1">
                          <span className="sr-only">Duration </span>
                          {c.ended_at === null && c.status === 'running' ? 'running' : formatDuration(c.duration_s)}
                        </span>
                      </div>
                    </div>
                    {open && (
                      <div className="border-t border-border bg-surface-2/60 px-3 py-4 sm:px-4 md:pl-[4.25rem]">
                        <ChainRowDetails id={detailsId} chain={c} />
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </section>
    </Card>
  );
}
