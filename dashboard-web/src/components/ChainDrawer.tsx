import type { ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import { StatusBadge } from './ui/Badge';
import { Button } from './ui/Button';
import { Skeleton } from './ui/Skeleton';
import { Time } from './ui/Tooltip';
import { AgentDecision } from './AgentDecision';
import { ChainRowDetails } from './ChainRowDetails';
import { ErrorState } from './ErrorState';
import EvalScores from './EvalScores';
import { EventList } from './EventList';
import { LinksList } from './LinksList';
import { StagePills } from './StagePills';
import TraceWaterfall from './TraceWaterfall';
import { ViewToggles } from './ViewToggles';
import type { SnapshotClient } from '../data/client';
import { repoName } from '../data/format';
import { useEntity } from '../data/hooks';
import type { MetaFile } from '../data/types';
import { useView } from '../state/url';

function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <section className="space-y-3 border-t border-border px-4 py-5 sm:px-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold">{title}</h3>
        {aside && <span className="text-xs text-fg-subtle">{aside}</span>}
      </div>
      {children}
    </section>
  );
}

export default function ChainDrawer({ client, meta }: { client: SnapshotClient; meta: MetaFile | null }) {
  const { view, closeChain } = useView();
  const { state, retry } = useEntity(client, view.chain);
  const entity = state.status === 'ready' ? state.entity : null;
  const chain = entity?.chain ?? null;

  return (
    <Dialog.Root open={view.chain !== null} onOpenChange={(o) => !o && closeChain()}>
      <Dialog.Portal>
        <Dialog.Overlay className="drawer-overlay fixed inset-0 z-30 bg-[var(--overlay)]" />
        <Dialog.Content className="drawer-content fixed inset-y-0 right-0 z-40 flex w-full flex-col overflow-y-auto bg-surface shadow-lg outline-none sm:max-w-[640px] sm:rounded-l-lg">
          <div className="sticky top-0 z-10 space-y-3 border-b border-border bg-surface/95 px-4 pt-4 pb-3 backdrop-blur sm:px-6">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 space-y-1">
                <Dialog.Title className="text-base font-semibold break-words">{chain?.title ?? 'Chain'}</Dialog.Title>
                <Dialog.Description className="text-xs text-fg-subtle">
                  {chain ? (
                    <>
                      {repoName(chain.repo)} · started <Time iso={chain.started_at} />
                    </>
                  ) : state.status === 'error' ? (
                    'Chain details unavailable'
                  ) : (
                    'Loading chain…'
                  )}
                </Dialog.Description>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {chain && <StatusBadge status={chain.status} />}
                <Dialog.Close asChild>
                  <Button size="icon" variant="ghost" aria-label="Close" className="-mt-2 -mr-2">
                    <X className="size-5" aria-hidden="true" />
                  </Button>
                </Dialog.Close>
              </div>
            </div>
            <ViewToggles />
          </div>

          {(state.status === 'loading' || state.status === 'idle') && (
            <div className="space-y-4 px-4 py-5 sm:px-6" aria-busy="true">
              <span className="sr-only" role="status">
                Loading chain…
              </span>
              <Skeleton className="h-6 w-2/3" />
              <Skeleton className="h-24" />
              <Skeleton className="h-40" />
              <Skeleton className="h-32" />
            </div>
          )}

          {state.status === 'error' && (
            <div className="px-4 py-5 sm:px-6">
              <ErrorState
                compact
                error={state.error}
                meta={meta}
                onRetry={retry}
                message={state.error.kind === 'not_found' ? 'This chain is not in the current snapshot.' : undefined}
              />
            </div>
          )}

          {entity && (
            <div className="pb-8">
              <Section title="Summary">
                <StagePills stages={entity.chain.stages} />
                <ChainRowDetails chain={entity.chain} showOpen={false} />
              </Section>
              <Section title="Agent decision">
                <AgentDecision agent={entity.agent} />
              </Section>
              <Section title="Trace">
                <TraceWaterfall trace={entity.trace} langfuse={meta?.sources.langfuse ?? 'ok'} />
              </Section>
              <Section title="Eval">
                <EvalScores evals={entity.evals} />
              </Section>
              <Section
                title="Events"
                aside={
                  view.raw
                    ? `${entity.events.length} events · raw JSON shown`
                    : `${entity.events.length} events · turn on raw JSON to inspect payloads`
                }
              >
                <EventList events={entity.events} />
              </Section>
              <Section title="Links">
                <LinksList links={entity.links} />
              </Section>
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
