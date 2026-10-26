import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { createFixtureClient } from '../data/fixtures-loader';
import { freezeNow, renderApp } from './helpers';

const REVERTED = 'deployfail/02: reverted by PR #55';

async function openDeepLink(search: string) {
  freezeNow();
  const r = renderApp(createFixtureClient('populated'), search);
  const dialog = await screen.findByRole('dialog');
  await within(dialog).findByRole('heading', { name: 'Summary' });
  return { ...r, dialog };
}

describe('chain drawer', () => {
  it('test_open_from_row_and_close', async () => {
    freezeNow();
    const { user } = renderApp(createFixtureClient('populated'), '?range=7d');
    await user.click(await screen.findByRole('button', { name: `Open details for ${REVERTED}` }));
    const dialog = await screen.findByRole('dialog', { name: REVERTED });
    expect(window.location.search).toBe('?range=7d&chain=c_2c8f0e7a5b1d3f94');
    expect(dialog).toHaveAccessibleDescription(/Sentinel-deployment · started 23 Oct 2026, 10:03 UTC/);
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(window.location.search).toBe('?range=7d');
  });

  it('opens from the needs-action lane and closes with the close button', async () => {
    freezeNow();
    const { user } = renderApp(createFixtureClient('populated'));
    await user.click(await screen.findByRole('button', { name: 'Details for runtime/07: escalated to a human' }));
    const dialog = await screen.findByRole('dialog', { name: 'runtime/07: escalated to a human' });
    await user.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(window.location.search).toBe('');
  });

  it('test_agent_decision', async () => {
    const { dialog } = await openDeepLink('?chain=c_2c8f0e7a5b1d3f94');
    const meter = within(dialog).getByRole('meter', { name: 'Agent confidence' });
    expect(meter).toHaveAttribute('aria-valuenow', '93');
    expect(meter).toHaveAttribute('aria-valuetext', '93%, High');
    expect(dialog).toHaveTextContent('93% · High');
    const toolCalls = within(dialog).getByText('Tool calls').closest('div');
    expect(toolCalls).toHaveTextContent('7');
    expect(within(dialog).getByText('Rollback').querySelector('svg')).not.toBeNull();
    expect(dialog).toHaveTextContent('claude-sonnet-4-5');
    expect(dialog).toHaveTextContent('Raw logs and tool output are never published.');
  });

  it('test_trace_toggle', async () => {
    const { user, dialog } = await openDeepLink('?chain=c_2c8f0e7a5b1d3f94');
    expect(dialog).toHaveTextContent('Trace · 7 spans · 2 m 40 s');
    expect(within(dialog).queryByRole('list', { name: 'Trace spans' })).toBeNull();
    await user.click(within(dialog).getByRole('button', { name: 'Show trace' }));
    expect(window.location.search).toContain('traces=1');
    const spans = within(dialog).getByRole('list', { name: 'Trace spans' });
    expect(within(spans).getAllByRole('listitem')).toHaveLength(7);
    expect(spans).toHaveTextContent('3.2k in · 410 out');
    expect(spans).toHaveTextContent('warning');
    const toggle = within(dialog).getByRole('switch', { name: 'Show traces' });
    expect(toggle).toBeChecked();
    await user.click(toggle);
    expect(within(dialog).queryByRole('list', { name: 'Trace spans' })).toBeNull();
    expect(within(dialog).getByRole('button', { name: 'Show trace' })).toBeInTheDocument();
    expect(window.location.search).not.toContain('traces=1');
  });

  it('test_raw_json_toggle', async () => {
    const { user, dialog } = await openDeepLink('?chain=c_2c8f0e7a5b1d3f94');
    expect(dialog.querySelectorAll('pre')).toHaveLength(0);
    expect(dialog).toHaveTextContent('9 events · turn on raw JSON to inspect payloads');
    await user.click(within(dialog).getByRole('switch', { name: 'Show raw event JSON' }));
    const pres = dialog.querySelectorAll('pre');
    expect(pres).toHaveLength(9);
    expect(pres[0]?.textContent).toContain('"event_id": "85f0976c');
    // Raw JSON carries the exact UTC timestamps.
    expect(pres[0]?.textContent).toContain('"ts": "2026-10-23T10:03:00Z"');
    expect(pres[0]).toHaveAttribute('aria-label', 'Raw JSON for build.failed');
    expect(window.location.search).toContain('raw=1');
  });

  it('test_eval_and_links', async () => {
    const { dialog } = await openDeepLink('?chain=c_2c8f0e7a5b1d3f94&eval=1');
    expect(within(dialog).getByRole('switch', { name: 'Show eval' })).toBeChecked();
    expect(within(dialog).getByText('88%')).toBeInTheDocument();
    const fired = within(dialog).getByText('Fired').closest('div');
    expect(fired).toHaveTextContent('pass');
    expect(within(dialog).getByRole('meter', { name: 'Root-cause correctness' })).toHaveAttribute('aria-valuenow', '95');

    const links = within(dialog).getByRole('list', { name: 'Links' });
    const langfuse = within(links).getByRole('link', { name: /LangFuse trace/ });
    expect(langfuse.closest('li')).toHaveTextContent('may require access');
    expect(langfuse).toHaveAttribute('target', '_blank');
    expect(langfuse).toHaveAttribute('rel', 'noopener noreferrer');
    const run = within(links).getByRole('link', { name: /Deployment run/ });
    expect(run).toHaveAccessibleName('Deployment run (opens in a new tab)');
    expect(run.closest('li')).not.toHaveTextContent('may require access');
    expect(dialog).toHaveTextContent('those links work only for viewers with access');
  });

  it('test_escalated_judge_failed', async () => {
    const { dialog } = await openDeepLink('?chain=c_9b2e4d6f8a0c1e35&eval=1');
    expect(dialog).toHaveTextContent('Judge failed');
    expect(dialog).toHaveTextContent('excluded from averages');
    const meter = within(dialog).getByRole('meter', { name: 'Agent confidence' });
    expect(meter).toHaveAttribute('aria-valuenow', '41');
    expect(dialog).toHaveTextContent('41% · Low');
    expect(within(dialog).getByText('Revert target').closest('div')).toHaveTextContent('fail');
  });

  it('test_no_trace_message', async () => {
    const { dialog } = await openDeepLink('?chain=c_7a3c9e1b2d4f6a80&traces=1');
    expect(dialog).toHaveTextContent('No trace summary recorded for this chain.');
    expect(dialog).toHaveTextContent('This chain has not been scored.');
    const datadog = within(dialog).getByRole('link', { name: /Datadog monitor/ });
    expect(datadog.closest('li')).toHaveTextContent('may require access');
  });

  it('test_unknown_chain', async () => {
    freezeNow();
    renderApp(createFixtureClient('populated'), '?chain=c_ffffffffffffffff');
    const dialog = await screen.findByRole('dialog');
    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent("Couldn't load this chain");
    expect(alert).toHaveTextContent('This chain is not in the current snapshot.');
    expect(within(alert).getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });
});
