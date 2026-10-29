import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DailyActivity } from '../components/DailyActivity';
import { dayLabel, monotonePath, yTicks } from '../components/TrendChart';
import type { DailyPoint } from '../data/types';

const day = (date: string, p: Partial<DailyPoint>): DailyPoint => ({
  date,
  deploys: 0,
  deploy_failures: 0,
  incidents: 0,
  escalations: 0,
  revert_prs_merged: 0,
  mttr_p50_s: null,
  eval_score_mean: null,
  ...p,
});

const DAILY = [
  day('2026-10-23', { deploys: 3, deploy_failures: 1, incidents: 1 }),
  day('2026-10-24', { deploys: 2, incidents: 3, escalations: 1, revert_prs_merged: 1 }),
  day('2026-10-25', { deploys: 4, deploy_failures: 2, incidents: 0 }),
];

async function renderChart() {
  const r = render(<DailyActivity daily={DAILY} />);
  // The chart is a lazy chunk behind a skeleton.
  const chart = await waitFor(() => {
    const el = r.container.querySelector<HTMLElement>('[data-trend-chart]');
    if (!el) throw new Error('chart not rendered yet');
    return el;
  });
  const svg = chart.querySelector('svg');
  if (!svg) throw new Error('chart has no svg');
  return { ...r, chart, svg };
}

const series = (svg: SVGSVGElement, key: string) => svg.querySelector<SVGGElement>(`[data-series="${key}"]`);

describe('trend chart', () => {
  it('test_trend_draws_every_series_from_tokens', async () => {
    const { svg } = await renderChart();
    // One bar per non-zero day, per bar series.
    expect(series(svg, 'deploys')?.querySelectorAll('path')).toHaveLength(3);
    expect(series(svg, 'deploy_failures')?.querySelectorAll('path')).toHaveLength(2);
    expect(series(svg, 'revert_prs_merged')?.querySelectorAll('path')).toHaveLength(1);
    // Incidents: one curve plus a dot per day.
    const line = series(svg, 'incidents');
    expect(line?.querySelectorAll('circle')).toHaveLength(3);
    expect(line?.querySelector('path')?.getAttribute('d')).toMatch(/^M[\d.]+,[\d.]+C/);
    // Colours are the theme tokens, so both themes (and a live switch) resolve through CSS.
    expect(series(svg, 'deploys')?.style.fill).toBe('var(--chart-1)');
    expect(series(svg, 'deploy_failures')?.style.fill).toBe('var(--chart-2)');
    expect(series(svg, 'revert_prs_merged')?.style.fill).toBe('var(--chart-4)');
    expect(line?.querySelector('path')?.style.stroke).toBe('var(--chart-3)');
    // Axes: UTC-day labels and integer y ticks.
    const labels = Array.from(svg.querySelectorAll('text')).map((t) => t.textContent);
    expect(labels).toEqual(expect.arrayContaining(['23 Oct', '24 Oct', '25 Oct', '0', '1', '2', '3', '4']));
  });

  it('test_trend_is_decorative_and_the_table_carries_the_data', async () => {
    const { chart } = await renderChart();
    expect(chart).toHaveAttribute('aria-hidden', 'true');
    expect(chart.querySelectorAll('a, button, input, [tabindex]')).toHaveLength(0);
    expect(within(chart).getByText('Merged revert PRs')).toBeInTheDocument(); // legend
    const table = screen.getByRole('table', { name: 'Daily activity' });
    const rows = within(table).getAllByRole('row');
    expect(rows).toHaveLength(1 + DAILY.length);
    expect(within(rows[2] as HTMLElement).getAllByRole('cell').map((c) => c.textContent)).toEqual(['2', '0', '3', '1', '1']);
  });

  it('test_trend_tooltip_follows_the_pointer', async () => {
    const { chart, svg } = await renderChart();
    // jsdom has no layout: 640 px fallback width, 32 px axis, so each of 3 days is 200 px wide.
    fireEvent.mouseMove(svg, { clientX: 32 + 250 });
    const tip = within(chart).getByText('24 Oct', { selector: 'p' }).parentElement as HTMLElement;
    expect(tip).toHaveTextContent('Deploys: 2');
    expect(tip).toHaveTextContent('Incidents: 3');
    expect(tip).toHaveTextContent('Merged revert PRs: 1');
    expect(svg.querySelector('rect')).not.toBeNull(); // hover band
    fireEvent.mouseLeave(svg);
    expect(within(chart).queryByText('24 Oct', { selector: 'p' })).toBeNull();
    expect(svg.querySelector('rect')).toBeNull();
  });

  it('test_trend_scale_helpers', () => {
    expect(dayLabel('2026-10-05')).toBe('5 Oct');
    expect(dayLabel('2026-01-31')).toBe('31 Jan');
    expect(yTicks(0)).toEqual([0, 1]);
    expect(yTicks(1)).toEqual([0, 1]);
    expect(yTicks(3)).toEqual([0, 1, 2, 3]);
    expect(yTicks(7)).toEqual([0, 2, 4, 6, 8]);
    expect(yTicks(37)).toEqual([0, 10, 20, 30, 40]);
  });

  it('test_trend_curve_never_overshoots', () => {
    expect(monotonePath([], [])).toBe('');
    expect(monotonePath([0, 10], [5, 7])).toBe('M0,5L10,7');
    const ys = [100, 40, 40, 90, 10];
    const d = monotonePath([0, 10, 20, 30, 40], ys);
    const nums = (d.match(/-?[\d.]+/g) ?? []).map(Number);
    const cys = nums.filter((_, i) => i % 2 === 1);
    // Every control point stays inside the data's range, and the flat run stays flat.
    for (const y of cys) {
      expect(y).toBeGreaterThanOrEqual(10);
      expect(y).toBeLessThanOrEqual(100);
    }
    expect(d).toContain('C13.333333333333334,40 16.666666666666668,40 20,40');
  });
});
