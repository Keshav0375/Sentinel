import { describe, expect, it } from 'vitest';
import {
  formatAgo,
  formatCount,
  formatDuration,
  formatElapsed,
  formatPct,
  formatTime,
  formatTokens,
  formatUtc,
  repoName,
  shortSha,
} from '../data/format';
import { NOW } from './helpers';

describe('format', () => {
  it('test_format_duration', () => {
    expect(formatDuration(null)).toBe('—');
    expect(formatDuration(0)).toBe('0 s');
    expect(formatDuration(45)).toBe('45 s');
    expect(formatDuration(240)).toBe('4 m');
    expect(formatDuration(170)).toBe('2 m 50 s');
    expect(formatDuration(3600)).toBe('1 h');
    expect(formatDuration(13800)).toBe('3 h 50 m');
    expect(formatDuration(90000)).toBe('1 d 1 h');
    expect(formatDuration(59.6)).toBe('1 m');
  });

  it('test_format_elapsed_and_ago', () => {
    expect(formatElapsed(30)).toBe('just now');
    expect(formatElapsed(170)).toBe('2 m');
    expect(formatElapsed(15_659)).toBe('4 h 20 m');
    expect(formatAgo('2026-10-25T12:00:00Z', NOW)).toBe('30 m ago');
    expect(formatAgo('2026-10-25T13:00:00Z', NOW)).toBe('just now');
  });

  it('test_format_time_is_local_with_exact_utc', () => {
    // The page shows the viewer's own zone, labelled; tooltips carry the exact UTC instant.
    expect(formatTime('2026-10-25T12:00:00Z', 'UTC')).toBe('25 Oct 2026, 12:00 UTC');
    expect(formatTime('2026-10-25T12:00:00Z', 'America/New_York')).toBe('25 Oct 2026, 08:00 GMT-4');
    expect(formatTime('2026-10-25T12:00:00Z', 'Asia/Kolkata')).toBe('25 Oct 2026, 17:30 GMT+5:30');
    // The test run pins TZ=UTC (vite.config.ts), so the default zone renders as UTC.
    expect(formatTime('2026-10-25T12:00:00Z')).toBe('25 Oct 2026, 12:00 UTC');
    expect(formatUtc('2026-10-25T12:00:07Z')).toBe('2026-10-25 12:00:07 UTC');
  });

  it('test_format_utc_pct_tokens', () => {
    expect(formatPct(0.3333)).toBe('33%');
    expect(formatPct(null)).toBe('—');
    expect(formatTokens(3200)).toBe('3.2k');
    expect(formatTokens(410)).toBe('410');
    expect(formatTokens(null)).toBe('—');
    expect(shortSha('2c8f0e7a5b1d3f9463a7b8c9d0e1f2a3b4c5d6e7')).toBe('2c8f0e7');
    expect(repoName('Keshav0375/Sentinel-deployment')).toBe('Sentinel-deployment');
    expect(formatCount(12345)).toBe('12,345');
  });
});
