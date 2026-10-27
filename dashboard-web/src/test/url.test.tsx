import { act, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { DEFAULT_VIEW, parseView, serializeView, useView, ViewStateProvider, type ViewState } from '../state/url';

describe('url state', () => {
  it('test_parse_defaults', () => {
    expect(parseView('')).toEqual(DEFAULT_VIEW);
    expect(DEFAULT_VIEW.range).toBe('all');
    expect(DEFAULT_VIEW.sort).toBe('started_desc');
    expect(serializeView(DEFAULT_VIEW)).toBe('');
  });

  it('test_roundtrip', () => {
    const full: ViewState = {
      range: '7d',
      focus: 'escalations',
      repo: 'Keshav0375/Sentinel-deployment',
      kind: 'deploy.failed',
      status: 'escalated',
      scenario: 'runtime/07',
      q: 'pool size',
      sort: 'duration_desc',
      chain: 'c_9b2e4d6f8a0c1e35',
      raw: true,
      traces: true,
      eval: true,
    };
    const s = serializeView(full);
    expect(parseView(s)).toEqual(full);
    expect([...new URLSearchParams(s).keys()]).toEqual([
      'range', 'focus', 'repo', 'kind', 'status', 'scenario', 'q', 'sort', 'chain', 'raw', 'traces', 'eval',
    ]);
    expect(serializeView({ ...DEFAULT_VIEW, traces: true })).toBe('?traces=1');
  });

  it('test_invalid_values_dropped', () => {
    expect(parseView('?range=1y&status=boom&scenario=runtime/11&chain=c_xyz&raw=true&kind=deploy')).toEqual(DEFAULT_VIEW);
    expect(parseView(`?q=${'a'.repeat(300)}`).q).toHaveLength(200);
    expect(parseView('?focus=nope&sort=random&repo=other/repo').focus).toBeNull();
  });

  it('test_update_push_and_close', async () => {
    let api: ReturnType<typeof useView> | null = null;
    function Probe() {
      api = useView();
      return <output>{api.view.chain ?? 'none'}</output>;
    }
    window.history.replaceState(null, '', '/Sentinel/?range=7d');
    render(
      <ViewStateProvider>
        <Probe />
      </ViewStateProvider>,
    );
    act(() => api?.update({ chain: 'c_2c8f0e7a5b1d3f94' }, { push: true }));
    expect(window.location.search).toBe('?range=7d&chain=c_2c8f0e7a5b1d3f94');
    expect((window.history.state as { drawer?: boolean }).drawer).toBe(true);
    expect(screen.getByRole('status')).toHaveTextContent('c_2c8f0e7a5b1d3f94');

    act(() => api?.closeChain());
    await waitFor(() => expect(window.location.search).toBe('?range=7d'));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('none'));

    // Deep link (no drawer history entry): close drops the param in place.
    act(() => api?.update({ chain: 'c_2c8f0e7a5b1d3f94' }));
    expect(window.location.search).toBe('?range=7d&chain=c_2c8f0e7a5b1d3f94');
    act(() => api?.closeChain());
    expect(window.location.search).toBe('?range=7d');
    expect(screen.getByRole('status')).toHaveTextContent('none');
  });

  it('test_close_restores_toggles_and_filters', async () => {
    let api: ReturnType<typeof useView> | null = null;
    function Probe() {
      api = useView();
      return <output>{api.view.chain ?? 'none'}</output>;
    }
    window.history.replaceState(null, '', '/Sentinel/?range=7d&status=failed');
    render(
      <ViewStateProvider>
        <Probe />
      </ViewStateProvider>,
    );
    act(() => api?.update({ chain: 'c_2c8f0e7a5b1d3f94' }, { push: true }));
    act(() => api?.update({ raw: true, traces: true }));
    expect(window.location.search).toBe('?range=7d&status=failed&chain=c_2c8f0e7a5b1d3f94&raw=1&traces=1');

    act(() => api?.closeChain());
    await waitFor(() => expect(window.location.search).toBe('?range=7d&status=failed&raw=1&traces=1'));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('none'));
    const current = (): ViewState | undefined => api?.view;
    expect(current()?.chain).toBeNull();
    expect(current()?.raw).toBe(true);
    expect((window.history.state as { drawer?: boolean } | null)?.drawer).not.toBe(true);
  });
});
