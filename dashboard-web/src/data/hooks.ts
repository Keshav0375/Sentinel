import { useEffect, useState } from 'react';
import { SnapshotError, type SnapshotClient } from './client';
import type { ChainsFile, EntityFile, MetaFile, MetricsFile } from './types';

export type SnapshotState =
  | { status: 'loading' }
  | { status: 'ready'; meta: MetaFile; chains: ChainsFile; metrics: MetricsFile; reloadError: SnapshotError | null }
  | { status: 'error'; error: SnapshotError; meta: MetaFile | null };

function asSnapshotError(err: unknown): SnapshotError {
  return err instanceof SnapshotError ? err : new SnapshotError('network', '');
}

export function useSnapshot(client: SnapshotClient): { state: SnapshotState; reload: () => void; refreshing: boolean } {
  const [state, setState] = useState<SnapshotState>({ status: 'loading' });
  const [nonce, setNonce] = useState(0);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    const ac = new AbortController();
    if (nonce > 0) setRefreshing(true);
    void Promise.allSettled([client.meta(ac.signal), client.chains(ac.signal), client.metrics(ac.signal)]).then(
      ([meta, chains, metrics]) => {
        if (ac.signal.aborted) return;
        setRefreshing(false);
        if (meta.status === 'fulfilled' && chains.status === 'fulfilled' && metrics.status === 'fulfilled') {
          setState({ status: 'ready', meta: meta.value, chains: chains.value, metrics: metrics.value, reloadError: null });
          return;
        }
        const failed = [meta, chains, metrics].find((r) => r.status === 'rejected');
        const error = asSnapshotError(failed?.status === 'rejected' ? failed.reason : undefined);
        setState((prev) =>
          prev.status === 'ready'
            ? { ...prev, reloadError: error }
            : { status: 'error', error, meta: meta.status === 'fulfilled' ? meta.value : null },
        );
      },
    );
    return () => ac.abort();
  }, [client, nonce]);

  return { state, reload: () => setNonce((n) => n + 1), refreshing };
}

export type EntityState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; entity: EntityFile }
  | { status: 'error'; error: SnapshotError };

export function useEntity(client: SnapshotClient, chainId: string | null): { state: EntityState; retry: () => void } {
  const [state, setState] = useState<EntityState>(chainId === null ? { status: 'idle' } : { status: 'loading' });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (chainId === null) {
      setState({ status: 'idle' });
      return;
    }
    const ac = new AbortController();
    setState({ status: 'loading' });
    client.entity(chainId, ac.signal).then(
      (entity) => {
        if (!ac.signal.aborted) setState({ status: 'ready', entity });
      },
      (err: unknown) => {
        if (!ac.signal.aborted) setState({ status: 'error', error: asSnapshotError(err) });
      },
    );
    return () => ac.abort();
  }, [client, chainId, nonce]);

  return { state, retry: () => setNonce((n) => n + 1) };
}
