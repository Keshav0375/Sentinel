import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SnapshotError } from '../data/client';
import { createFixtureClient } from '../data/fixtures-loader';
import { freezeNow, pendingClient, renderApp, stubClient } from './helpers';

const overview = () => screen.getByRole('region', { name: 'Overview' });
const kpi = (label: RegExp) => within(overview()).getByRole('button', { name: label });

describe('app', () => {
  it('test_loading_skeleton', () => {
    renderApp(pendingClient());
    expect(screen.getByRole('status')).toHaveTextContent('Loading snapshot…');
    expect(screen.getByRole('main')).toHaveAttribute('aria-busy', 'true');
  });

  it('test_populated_overview', async () => {
    freezeNow();
    const { user } = renderApp(createFixtureClient('populated'));
    await screen.findByRole('region', { name: 'Overview' });
    expect(screen.getByRole('main')).toHaveAttribute('aria-busy', 'false');
    expect(within(overview()).getAllByRole('button', { pressed: false })).toHaveLength(7);
    expect(kpi(/^Deploys/)).toHaveTextContent('6');
    expect(kpi(/^Deploys/)).toHaveAccessibleName('Deploys 6');
    expect(kpi(/^Eval score/)).toHaveAccessibleName('Eval score 88%');
    expect(kpi(/^Awaiting review/)).toHaveAccessibleDescription('oldest waiting 4 h 20 m (now)');

    await user.click(within(overview()).getByRole('radio', { name: '24 h' }));
    expect(kpi(/^Deploys/)).toHaveAccessibleName('Deploys 3');
    expect(kpi(/^Eval score/)).toHaveAccessibleName('Eval score —');
    expect(window.location.search).toBe('?range=24h');

    const lane = screen.getByRole('region', { name: /Needs action/ });
    const items = within(lane).getAllByRole('listitem');
    expect(items.map((li) => within(li).getByTitle(/./).textContent)).toEqual([
      'runtime/04: revert PR #57 awaiting review',
      'runtime/07: escalated to a human',
      'App deploy pr-61-4f1d2a9 failed at verify',
    ]);
    const first = items[0];
    if (!first) throw new Error('needs-action lane is empty');
    expect(first).toHaveTextContent('waiting 4 h 20 m');
    expect(within(first).getByRole('link', { name: /Open on GitHub/ })).toHaveAttribute(
      'href',
      'https://github.com/Keshav0375/Sentinel-deployment/pull/57',
    );
  });

  it('test_empty_state', async () => {
    freezeNow();
    renderApp(createFixtureClient('empty'));
    expect(await screen.findByText('No activity recorded yet')).toBeInTheDocument();
    expect(screen.getByText('Nothing needs action')).toBeInTheDocument();
    expect(kpi(/^Failure rate/)).toHaveAccessibleName('Failure rate —');
    expect(screen.getByText('No daily data yet.')).toBeInTheDocument();
    expect(screen.getByText('No eval runs scored yet.')).toBeInTheDocument();
  });

  it('test_error_with_snapshot_age', async () => {
    freezeNow();
    const client = createFixtureClient({ set: 'populated', fail: true, delayMs: 0 });
    const chains = vi.spyOn(client, 'chains');
    const { user } = renderApp(client);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent("Couldn't load the dashboard");
    expect(alert).toHaveTextContent('Server returned 503');
    expect(alert).toHaveTextContent('Last snapshot: generated 25 Oct 2026, 12:00 UTC, 30 m ago');
    expect(within(alert).getByRole('link', { name: /Check the reconciler runs/ })).toHaveAttribute(
      'href',
      'https://github.com/Keshav0375/Sentinel/actions/workflows/ops_reconcile.yml',
    );
    expect(chains).toHaveBeenCalledTimes(1);
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(chains).toHaveBeenCalledTimes(2));
  });

  it('test_error_age_unknown', async () => {
    renderApp(stubClient({ meta: () => Promise.reject(new SnapshotError('network', '/Sentinel/snapshot/meta.json')) }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Network request failed');
    expect(alert).toHaveTextContent('Snapshot age unknown');
  });

  it('test_reload_failure_keeps_data', async () => {
    freezeNow();
    const base = createFixtureClient('populated');
    let calls = 0;
    const fresh: (boolean | undefined)[] = [];
    const client = stubClient(
      {
        chains: (s, f) => {
          calls += 1;
          fresh.push(f);
          return calls === 1 ? base.chains(s) : Promise.reject(new SnapshotError('http', 'chains.json', 500));
        },
      },
      base,
    );
    const { user, container } = renderApp(client);
    await screen.findByRole('list', { name: 'Chains' });
    await user.click(screen.getByRole('button', { name: 'Reload snapshot' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent("Couldn't refresh the snapshot");
    expect(alert).toHaveTextContent('Server returned 500');
    expect(container.querySelectorAll('[data-row-index]')).toHaveLength(6);
    expect(screen.getByRole('region', { name: 'Overview' })).toBeInTheDocument();
    // The first load may use the preload; the reload bypasses the HTTP cache.
    expect(fresh).toEqual([false, true]);
  });

  it('test_staleness_banner', async () => {
    freezeNow(new Date('2026-10-25T13:30:00Z'));
    const { unmount } = renderApp(createFixtureClient('populated'));
    const banner = await screen.findByText(/Snapshot is 1 h 30 m old/);
    expect(banner.closest('[role="status"]')).not.toBeNull();
    expect(banner).toHaveTextContent('The reconciler normally refreshes it every 30 minutes');
    expect(within(banner).getByRole('link', { name: /Last reconciler run/ })).toBeInTheDocument();
    unmount();

    freezeNow(new Date('2026-10-25T12:30:00Z'));
    renderApp(createFixtureClient('populated'));
    await screen.findByRole('region', { name: 'Overview' });
    expect(screen.queryByText(/Snapshot is .* old/)).toBeNull();
  });

  it('test_source_notice', async () => {
    freezeNow();
    const base = createFixtureClient('populated');
    renderApp(
      stubClient({ meta: async () => ({ ...(await base.meta()), sources: { github: 'ok', langfuse: 'error' } }) }, base),
    );
    expect(await screen.findByText(/LangFuse could not be read/)).toBeInTheDocument();
    expect(screen.queryByText(/GitHub could not be read/)).toBeNull();
  });

  it('test_slash_focuses_search', async () => {
    freezeNow();
    const { user } = renderApp(createFixtureClient('populated'));
    const search = await screen.findByRole('searchbox', { name: 'Search chains' });
    await user.keyboard('/');
    expect(search).toHaveFocus();
    expect(search).toHaveValue('');
    await user.keyboard('x');
    expect(search).toHaveValue('x');
    await user.keyboard('{Escape}');
    expect(search).toHaveValue('');
    // While typing, "/" is just a character.
    await user.keyboard('a/b');
    expect(search).toHaveValue('a/b');
  });

  it('shows times in the local zone with the exact UTC instant on the element', async () => {
    freezeNow();
    renderApp(createFixtureClient('populated'));
    const note = await screen.findByText(/Window ends at the snapshot time/);
    const time = within(note).getByText('25 Oct 2026, 12:00 UTC');
    expect(time.tagName).toBe('TIME');
    expect(time).toHaveAttribute('datetime', '2026-10-25T12:00:00Z');
  });
});
