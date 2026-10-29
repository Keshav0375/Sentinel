import {
  CHAIN_ID_RE,
  SNAPSHOT_SCHEMA,
  type ChainsFile,
  type EntityFile,
  type MetaFile,
  type MetricsFile,
  type SnapshotFile,
} from './types';

export type SnapshotErrorKind = 'network' | 'http' | 'not_found' | 'parse' | 'schema';

function messageFor(kind: SnapshotErrorKind, status?: number): string {
  switch (kind) {
    case 'network':
      return 'Network request failed';
    case 'http':
      return `Server returned ${status ?? 'an error'}`;
    case 'not_found':
      return 'Not in the current snapshot';
    case 'parse':
      return 'Snapshot file is not valid JSON';
    case 'schema':
      return `Unexpected snapshot format (expected ${SNAPSHOT_SCHEMA})`;
  }
}

export class SnapshotError extends Error {
  constructor(
    readonly kind: SnapshotErrorKind,
    readonly url: string,
    readonly status?: number,
  ) {
    super(messageFor(kind, status));
    this.name = 'SnapshotError';
  }
}

export interface SnapshotClient {
  /** `fresh`: a reload or Retry, which must bypass the HTTP cache. */
  meta(s?: AbortSignal, fresh?: boolean): Promise<MetaFile>;
  chains(s?: AbortSignal, fresh?: boolean): Promise<ChainsFile>;
  metrics(s?: AbortSignal, fresh?: boolean): Promise<MetricsFile>;
  entity(chainId: string, s?: AbortSignal): Promise<EntityFile>;
}

/** `/Sentinel` or `/Sentinel/` → `/Sentinel/snapshot/`. */
export function snapshotBaseUrl(baseUrl: string): string {
  return `${baseUrl.replace(/\/+$/, '')}/snapshot/`;
}

/** The only check the browser makes: right contract, right file. Full validation is the writer's job. */
export function checkFile<T extends SnapshotFile>(json: unknown, file: T['file'], url: string): T {
  if (
    typeof json !== 'object' ||
    json === null ||
    (json as { schema?: unknown }).schema !== SNAPSHOT_SCHEMA ||
    (json as { file?: unknown }).file !== file
  ) {
    throw new SnapshotError('schema', url);
  }
  return json as T;
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

export function createHttpClient(base: string, fetchImpl: typeof fetch = (...a) => fetch(...a)): SnapshotClient {
  async function load<T extends SnapshotFile>(
    path: string,
    file: T['file'],
    signal: AbortSignal | undefined,
    cache: RequestCache,
  ): Promise<T> {
    const url = `${base}${path}`;
    let res: Response;
    try {
      res = await fetchImpl(url, { cache, signal });
    } catch (err) {
      if (isAbort(err)) throw err;
      throw new SnapshotError('network', url);
    }
    if (res.status === 404) throw new SnapshotError('not_found', url, 404);
    if (!res.ok) throw new SnapshotError('http', url, res.status);
    let json: unknown;
    try {
      json = await res.json();
    } catch (err) {
      if (isAbort(err)) throw err;
      throw new SnapshotError('parse', url);
    }
    return checkFile<T>(json, file, url);
  }

  // The first load uses the default mode so the index.html preload is consumed (a no-cache request
  // may not reuse it); Pages' max-age=600 is under the 30-min reconcile cadence. A reload or Retry
  // (fresh) revalidates, so it never shows the cached copy it was asked to replace.
  const mode = (fresh?: boolean): RequestCache => (fresh ? 'no-cache' : 'default');
  return {
    meta: (s, fresh) => load<MetaFile>('meta.json', 'meta', s, mode(fresh)),
    chains: (s, fresh) => load<ChainsFile>('chains.json', 'chains', s, mode(fresh)),
    metrics: (s, fresh) => load<MetricsFile>('metrics.json', 'metrics', s, mode(fresh)),
    entity: (chainId, s) => {
      const path = `entities/${chainId}.json`;
      if (!CHAIN_ID_RE.test(chainId)) return Promise.reject(new SnapshotError('not_found', `${base}${path}`));
      return load<EntityFile>(path, 'entity', s, 'no-cache');
    },
  };
}

export async function createDefaultClient(): Promise<SnapshotClient> {
  // Written inline so the bundler folds it to `false` in a production build and drops the
  // dynamic import: the deployed site never ships a fixture.
  if (
    import.meta.env.VITE_SNAPSHOT_SOURCE === 'fixtures' ||
    (import.meta.env.DEV && import.meta.env.VITE_SNAPSHOT_SOURCE !== 'http')
  ) {
    const m = await import('./fixtures-loader');
    return m.createFixtureClient(m.readFixtureOptions(window.location.search));
  }
  return createHttpClient(snapshotBaseUrl(import.meta.env.BASE_URL));
}
