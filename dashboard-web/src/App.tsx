import { lazy, Suspense, useEffect } from 'react';
import * as Tooltip from '@radix-ui/react-tooltip';
import { AppFooter } from './components/AppFooter';
import { AppHeader } from './components/AppHeader';
import { ChainTimeline } from './components/ChainTimeline';
import { DashboardSkeleton } from './components/DashboardSkeleton';
import { ErrorState } from './components/ErrorState';
import { EvalScorecard } from './components/EvalScorecard';
import { SEARCH_INPUT_ID } from './components/FilterBar';
import { NeedsActionLane } from './components/NeedsActionLane';
import { Overview } from './components/Overview';
import { SourceNotice } from './components/SourceNotice';
import { StalenessBanner } from './components/StalenessBanner';
import type { SnapshotClient } from './data/client';
import { useSnapshot } from './data/hooks';
import { NowProvider } from './state/now';
import { ThemeProvider } from './state/theme';
import { useView, ViewStateProvider } from './state/url';

const ChainDrawer = lazy(() => import('./components/ChainDrawer'));

function isTypingTarget(el: EventTarget | null): boolean {
  return (
    el instanceof HTMLElement &&
    (el.isContentEditable || el instanceof HTMLInputElement || el instanceof HTMLSelectElement || el instanceof HTMLTextAreaElement)
  );
}

/** `/` focuses the chain search (not while typing, not with modifiers). */
function useSlashToSearch() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey || isTypingTarget(e.target)) return;
      const input = document.getElementById(SEARCH_INPUT_ID);
      if (!input) return;
      e.preventDefault();
      input.focus();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);
}

function Dashboard({ client }: { client: SnapshotClient }) {
  const { state, reload, refreshing } = useSnapshot(client);
  const { view } = useView();
  useSlashToSearch();
  const meta = state.status === 'loading' ? null : state.meta;

  return (
    <>
      <a href="#timeline" className="skip-link">
        Skip to chains
      </a>
      <AppHeader meta={meta} refreshing={refreshing} onReload={reload} />
      <main id="main" className="mx-auto max-w-7xl space-y-6 px-4 py-6 sm:px-6 lg:px-8" aria-busy={state.status === 'loading'}>
        {state.status === 'loading' && <DashboardSkeleton />}
        {state.status === 'error' && <ErrorState error={state.error} meta={state.meta} onRetry={reload} />}
        {state.status === 'ready' && (
          <>
            <StalenessBanner meta={state.meta} />
            <SourceNotice meta={state.meta} />
            {state.reloadError && (
              <ErrorState
                compact
                title="Couldn't refresh the snapshot. Showing the last loaded data."
                error={state.reloadError}
                meta={state.meta}
                onRetry={reload}
              />
            )}
            <Overview metrics={state.metrics} generatedAt={state.metrics.generated_at} />
            <NeedsActionLane chains={state.chains.chains} />
            <ChainTimeline chains={state.chains.chains} generatedAt={state.chains.generated_at} />
            <EvalScorecard metrics={state.metrics} />
          </>
        )}
      </main>
      <AppFooter meta={meta} />
      {view.chain && (
        <Suspense fallback={null}>
          <ChainDrawer client={client} meta={meta} />
        </Suspense>
      )}
    </>
  );
}

export default function App({ client }: { client: SnapshotClient }) {
  return (
    <ThemeProvider>
      <NowProvider>
        <ViewStateProvider>
          <Tooltip.Provider delayDuration={200}>
            <Dashboard client={client} />
          </Tooltip.Provider>
        </ViewStateProvider>
      </NowProvider>
    </ThemeProvider>
  );
}
