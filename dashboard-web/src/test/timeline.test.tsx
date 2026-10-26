import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { createFixtureClient } from '../data/fixtures-loader';
import { freezeNow, renderApp } from './helpers';

const PR57 = 'runtime/04: revert PR #57 awaiting review';

async function setup(search = '') {
  freezeNow();
  const r = renderApp(createFixtureClient('populated'), search);
  await screen.findByRole('list', { name: 'Chains' });
  // Re-query each time: the list unmounts while a filter matches nothing.
  const rows = () =>
    Array.from(screen.queryByRole('list', { name: 'Chains' })?.querySelectorAll<HTMLElement>(':scope > [data-row-index]') ?? []);
  const rowTitles = () => rows().map((li) => li.querySelector('[title]')?.getAttribute('title'));
  return { ...r, rows, rowTitles };
}

describe('timeline', () => {
  it('test_rows_and_pills', async () => {
    const { rows, rowTitles } = await setup();
    expect(rows()).toHaveLength(6);
    // Newest first by default.
    expect(rowTitles()[0]).toBe('Backend deploy sha-0a1b2c3');
    const row = rows().find((li) => li.textContent?.includes(PR57));
    if (!row) throw new Error('row missing');
    expect(within(row).getByRole('listitem', { name: 'Review: Waiting — Awaiting review' })).toBeInTheDocument();
    expect(within(row).getByRole('list', { name: 'Stages' }).children).toHaveLength(6);
    // Status is icon + label, never color alone.
    const badge = within(row).getByText('Awaiting review', { selector: 'span' });
    expect(badge.querySelector('svg')).not.toBeNull();
    expect(row).toHaveTextContent('Sentinel-deployment');
    expect(row).toHaveTextContent('7a3c9e1');
    expect(row).toHaveTextContent('#57');
    expect(within(row).getByText('4 h 30 m ago').tagName).toBe('TIME');
  });

  it('test_expand_row', async () => {
    const { user } = await setup();
    const toggle = screen.getByRole('button', { name: `Show stages for ${PR57}` });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const region = screen.getByRole('region', { name: `Stages of ${PR57}` });
    expect(toggle).toHaveAttribute('aria-controls', region.id);
    expect(region).toHaveTextContent('Revert PR #57 opened');
    expect(region).toHaveTextContent('5b0c3f1e-8a2d-4c6b-9e7f-1a2b3c4d5e6f');
    expect(region).toHaveTextContent('5 events');
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('region', { name: `Stages of ${PR57}` })).toBeNull();
  });

  it('test_filters_and_chips', async () => {
    const { user, rows, rowTitles } = await setup();
    await user.selectOptions(screen.getByRole('combobox', { name: 'Status' }), 'failed');
    expect(rows()).toHaveLength(1);
    expect(rowTitles()).toEqual(['App deploy pr-61-4f1d2a9 failed at verify']);
    expect(screen.getByText('Status: Failed')).toBeInTheDocument();
    expect(screen.getByText('Showing 1 of 6 chains')).toHaveAttribute('aria-live', 'polite');
    expect(window.location.search).toBe('?status=failed');

    await user.click(screen.getByRole('button', { name: 'Remove filter Status: Failed' }));
    expect(rows()).toHaveLength(6);
    expect(screen.getByText('Showing 6 of 6 chains')).toBeInTheDocument();

    await user.type(screen.getByRole('searchbox', { name: 'Search chains' }), '#55');
    await waitFor(() => expect(rows()).toHaveLength(1));
    expect(rowTitles()).toEqual(['deployfail/02: reverted by PR #55']);
    await user.selectOptions(screen.getByRole('combobox', { name: 'Repo' }), 'Keshav0375/Sentinel-deployment');
    expect(screen.getByText('Repo: Sentinel-deployment')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Clear all' }));
    await waitFor(() => expect(rows()).toHaveLength(6));
    expect(window.location.search).toBe('');
    expect(screen.queryByRole('button', { name: 'Clear all' })).toBeNull();
  });

  it('test_kpi_card_filters_timeline', async () => {
    const { user, rows, rowTitles } = await setup();
    const card = screen.getByRole('button', { name: /^Escalations/ });
    await user.click(card);
    expect(card).toHaveAttribute('aria-pressed', 'true');
    expect(rows()).toHaveLength(1);
    expect(rowTitles()).toEqual(['runtime/07: escalated to a human']);
    expect(window.location.search).toBe('?focus=escalations');
    expect(screen.getByText('KPI: Escalations')).toBeInTheDocument();
    await user.click(card);
    expect(card).toHaveAttribute('aria-pressed', 'false');
    expect(rows()).toHaveLength(6);
  });

  it('test_sort', async () => {
    const { user, rowTitles } = await setup();
    const sort = screen.getByRole('combobox', { name: 'Sort' });
    await user.selectOptions(sort, 'Oldest first');
    expect(rowTitles()[0]).toBe('pass/01: deployed pr-52-5e6a7b8');
    await user.selectOptions(sort, 'Longest first');
    expect(rowTitles()[0]).toBe('runtime/07: escalated to a human');
    expect(window.location.search).toBe('?sort=duration_desc');
  });

  it('test_arrow_keys_move_between_rows', async () => {
    const { user, rows } = await setup();
    const [first, second] = rows().map((li) => li.querySelector<HTMLButtonElement>('[data-row-toggle]'));
    first?.focus();
    await user.keyboard('{ArrowDown}');
    expect(second).toHaveFocus();
    await user.keyboard('{ArrowUp}');
    expect(first).toHaveFocus();
    await user.keyboard('{ArrowUp}');
    expect(first).toHaveFocus();
  });

  it('test_filtered_empty', async () => {
    const { user, rows } = await setup();
    await user.type(screen.getByRole('searchbox', { name: 'Search chains' }), 'zzz');
    expect(await screen.findByText('No chains match these filters')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    await waitFor(() => expect(rows()).toHaveLength(6));
  });

  it('applies filters from a shared link', async () => {
    const { rowTitles } = await setup('?range=24h&repo=Keshav0375/Sentinel-deployment&sort=started_asc');
    expect(rowTitles()).toEqual([
      'runtime/07: escalated to a human',
      'runtime/04: revert PR #57 awaiting review',
      'App deploy pr-61-4f1d2a9 failed at verify',
    ]);
    expect(screen.getByRole('radio', { name: '24 h' })).toHaveAttribute('aria-checked', 'true');
  });
});
