import { differenceInSeconds, parseISO } from 'date-fns';
import type { Repo, Timestamp } from './types';

const DASH = '—';

/** Seconds → `45 s`, `2 m 50 s`, `3 h 50 m`, `1 d 1 h`. */
export function formatDuration(s: number | null): string {
  if (s === null) return DASH;
  const t = Math.round(s);
  if (t < 60) return `${t} s`;
  if (t < 3600) {
    const m = Math.floor(t / 60);
    const r = t % 60;
    return r > 0 ? `${m} m ${r} s` : `${m} m`;
  }
  if (t < 86400) {
    const h = Math.floor(t / 3600);
    const m = Math.floor((t % 3600) / 60);
    return m > 0 ? `${h} h ${m} m` : `${h} h`;
  }
  const d = Math.floor(t / 86400);
  const h = Math.floor((t % 86400) / 3600);
  return h > 0 ? `${d} d ${h} h` : `${d} d`;
}

/** Elapsed time at minute precision: `just now`, `4 h 20 m`. */
export function formatElapsed(s: number): string {
  if (s < 60) return 'just now';
  return formatDuration(Math.floor(s / 60) * 60);
}

export function secondsBetween(iso: Timestamp, now: Date): number {
  return Math.max(0, differenceInSeconds(now, parseISO(iso)));
}

export function formatAgo(iso: Timestamp, now: Date): string {
  const e = formatElapsed(secondsBetween(iso, now));
  return e === 'just now' ? e : `${e} ago`;
}

const localFormats = new Map<string, Intl.DateTimeFormat>();

/**
 * Absolute time in the viewer's own time zone, labelled with that zone:
 * `25 Oct 2026, 12:00 UTC`, `25 Oct 2026, 08:00 GMT-4`. `timeZone` overrides it (tests).
 */
export function formatTime(iso: Timestamp, timeZone?: string): string {
  const key = timeZone ?? '';
  let fmt = localFormats.get(key);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat('en-GB', {
      timeZone,
      day: 'numeric',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
      timeZoneName: 'short',
    });
    localFormats.set(key, fmt);
  }
  return fmt.format(parseISO(iso));
}

/** The exact instant in UTC, to the second: `2026-10-25 12:00:00 UTC`. */
export function formatUtc(iso: Timestamp): string {
  return `${parseISO(iso).toISOString().slice(0, 19).replace('T', ' ')} UTC`;
}

export function formatPct(u: number | null): string {
  return u === null ? DASH : `${Math.round(u * 100)}%`;
}

export function shortSha(sha: string): string {
  return sha.slice(0, 7);
}

export function repoName(repo: Repo): string {
  return repo.slice(repo.indexOf('/') + 1);
}

export function formatTokens(n: number | null): string {
  if (n === null) return DASH;
  return n < 1000 ? String(n) : `${(n / 1000).toFixed(1)}k`;
}

export function formatCount(n: number): string {
  return n.toLocaleString('en-US');
}
