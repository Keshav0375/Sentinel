import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi } from 'vitest';
import App from '../App';
import type { SnapshotClient } from '../data/client';
import { createFixtureClient } from '../data/fixtures-loader';

/** 30 minutes after the fixtures' generated_at (2026-10-25T12:00:00Z). */
export const NOW = new Date('2026-10-25T12:30:00Z');

/** Freeze `Date` only; real timers keep Testing Library's polling working. */
export function freezeNow(d: Date = NOW): void {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(d);
}

export function stubClient(
  overrides: Partial<SnapshotClient>,
  base: SnapshotClient = createFixtureClient('populated'),
): SnapshotClient {
  return { ...base, ...overrides };
}

export function pendingClient(): SnapshotClient {
  const never = () => new Promise<never>(() => {});
  return { meta: never, chains: never, metrics: never, entity: never };
}

export function renderApp(client: SnapshotClient, search = '') {
  window.history.replaceState(null, '', `/Sentinel/${search}`);
  const user = userEvent.setup();
  return { user, ...render(<App client={client} />) };
}

// ── matchMedia mock ────────────────────────────────────────────────────────────
type Listener = (e: MediaQueryListEvent) => void;

const media = { dark: false, reducedMotion: false };
const listeners = new Map<string, Set<Listener>>();

function matches(query: string): boolean {
  if (query.includes('prefers-color-scheme: dark')) return media.dark;
  if (query.includes('prefers-color-scheme: light')) return !media.dark;
  if (query.includes('prefers-reduced-motion: reduce')) return media.reducedMotion;
  return false;
}

function fire(fragment: string): void {
  for (const [query, set] of listeners) {
    if (!query.includes(fragment)) continue;
    for (const l of set) l({ matches: matches(query), media: query } as MediaQueryListEvent);
  }
}

export function installMatchMedia(): void {
  window.matchMedia = (query: string) => {
    const set = listeners.get(query) ?? new Set<Listener>();
    listeners.set(query, set);
    return {
      media: query,
      get matches() {
        return matches(query);
      },
      onchange: null,
      addEventListener: (_: string, l: Listener) => set.add(l),
      removeEventListener: (_: string, l: Listener) => set.delete(l),
      addListener: (l: Listener) => set.add(l),
      removeListener: (l: Listener) => set.delete(l),
      dispatchEvent: () => true,
    } as unknown as MediaQueryList;
  };
}

export function resetMedia(): void {
  media.dark = false;
  media.reducedMotion = false;
  listeners.clear();
}

export function setPrefersDark(b: boolean): void {
  media.dark = b;
  fire('prefers-color-scheme');
}

export function setReducedMotion(b: boolean): void {
  media.reducedMotion = b;
  fire('prefers-reduced-motion');
}
