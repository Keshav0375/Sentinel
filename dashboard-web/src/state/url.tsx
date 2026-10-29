// View state lives in the query string only (no path routes), so every view is a shareable
// link and GitHub Pages needs no 404 fallback.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { KPI_FOCI, SCENARIOS, type KpiFocus } from '../data/derive';
import {
  CHAIN_ID_RE,
  CHAIN_STATUSES,
  KINDS,
  RANGE_KEYS,
  REPOS,
  type ChainStatus,
  type Kind,
  type RangeKey,
  type Repo,
  type Scenario,
} from '../data/types';

export const SORT_KEYS = ['started_desc', 'started_asc', 'duration_desc', 'duration_asc'] as const;
export type SortKey = (typeof SORT_KEYS)[number];

export interface ViewState {
  range: RangeKey;
  focus: KpiFocus | null;
  repo: Repo | null;
  kind: Kind | null;
  status: ChainStatus | null;
  scenario: Scenario | null;
  q: string;
  sort: SortKey;
  chain: string | null;
  raw: boolean;
  traces: boolean;
  eval: boolean;
}

export const DEFAULT_VIEW: ViewState = {
  range: 'all',
  focus: null,
  repo: null,
  kind: null,
  status: null,
  scenario: null,
  q: '',
  sort: 'started_desc',
  chain: null,
  raw: false,
  traces: false,
  eval: false,
};

const Q_MAX = 200;

function oneOf<T extends string>(allowed: readonly T[], value: string | null): T | null {
  return value !== null && (allowed as readonly string[]).includes(value) ? (value as T) : null;
}

export function parseView(search: string): ViewState {
  const p = new URLSearchParams(search);
  const chain = p.get('chain');
  return {
    range: oneOf(RANGE_KEYS, p.get('range')) ?? DEFAULT_VIEW.range,
    focus: oneOf(KPI_FOCI, p.get('focus')),
    repo: oneOf(REPOS, p.get('repo')),
    kind: oneOf(KINDS, p.get('kind')),
    status: oneOf(CHAIN_STATUSES, p.get('status')),
    scenario: oneOf(SCENARIOS, p.get('scenario')),
    q: (p.get('q') ?? '').trim().slice(0, Q_MAX),
    sort: oneOf(SORT_KEYS, p.get('sort')) ?? DEFAULT_VIEW.sort,
    chain: chain !== null && CHAIN_ID_RE.test(chain) ? chain : null,
    raw: p.get('raw') === '1',
    traces: p.get('traces') === '1',
    eval: p.get('eval') === '1',
  };
}

const KEY_ORDER = ['range', 'focus', 'repo', 'kind', 'status', 'scenario', 'q', 'sort', 'chain', 'raw', 'traces', 'eval'] as const;

/** Non-default values only, in a fixed key order: `''` or `?range=7d&…`. */
export function serializeView(v: ViewState): string {
  const p = new URLSearchParams();
  for (const key of KEY_ORDER) {
    const value = v[key];
    if (value === DEFAULT_VIEW[key]) continue;
    if (typeof value === 'boolean') p.set(key, '1');
    else if (value !== null) p.set(key, value);
  }
  const s = p.toString();
  return s === '' ? '' : `?${s}`;
}

interface ViewContextValue {
  view: ViewState;
  update: (patch: Partial<ViewState>, opts?: { push?: boolean }) => void;
  closeChain: () => void;
}

const ViewContext = createContext<ViewContextValue | null>(null);

function urlFor(v: ViewState): string {
  return `${window.location.pathname}${serializeView(v)}${window.location.hash}`;
}

export function ViewStateProvider({ children }: { children: ReactNode }) {
  const [view, setView] = useState<ViewState>(() => parseView(window.location.search));
  // The view to restore after closeChain's history.back() (see closeChain).
  const pending = useRef<ViewState | null>(null);

  useEffect(() => {
    const onPop = () => {
      const p = pending.current;
      if (p === null) {
        setView(parseView(window.location.search));
        return;
      }
      pending.current = null;
      window.history.replaceState(window.history.state, '', urlFor(p));
      setView(p);
    };
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  const update = useCallback((patch: Partial<ViewState>, opts?: { push?: boolean }) => {
    const next = { ...parseView(window.location.search), ...patch };
    if (opts?.push) window.history.pushState({ drawer: true }, '', urlFor(next));
    else window.history.replaceState(window.history.state, '', urlFor(next));
    setView(next);
  }, []);

  const closeChain = useCallback(() => {
    const next = { ...parseView(window.location.search), chain: null };
    const state: unknown = window.history.state;
    if (typeof state === 'object' && state !== null && (state as { drawer?: unknown }).drawer === true) {
      // Opened by a click: Back pops the drawer entry; the view at close time (filters, toggles)
      // is then written over the previous entry.
      pending.current = next;
      window.history.back();
      return;
    }
    // Opened from a shared link: drop the param in place.
    window.history.replaceState(window.history.state, '', urlFor(next));
    setView(next);
  }, []);

  const value = useMemo(() => ({ view, update, closeChain }), [view, update, closeChain]);
  return <ViewContext.Provider value={value}>{children}</ViewContext.Provider>;
}

export function useView(): ViewContextValue {
  const ctx = useContext(ViewContext);
  if (!ctx) throw new Error('useView must be used inside <ViewStateProvider>');
  return ctx;
}
