// Dev and test only: serves src/fixtures/ (10.1's frozen snapshot samples) through the
// SnapshotClient interface. Imported dynamically, so production builds never include it.
import { checkFile, SnapshotError, type SnapshotClient } from './client';
import { CHAIN_ID_RE, type SnapshotFile } from './types';

const loaders = import.meta.glob('../fixtures/**/*.json', { import: 'default' });

export type FixtureSet = 'populated' | 'empty';

export interface FixtureOptions {
  set: FixtureSet;
  fail: boolean;
  delayMs: number;
}

/** `?fixture=populated|empty|error&delay=<ms>` (delay clamped to 0–5000). */
export function readFixtureOptions(search: string): FixtureOptions {
  const params = new URLSearchParams(search);
  const fixture = params.get('fixture');
  const delay = Number.parseInt(params.get('delay') ?? '', 10);
  return {
    set: fixture === 'empty' ? 'empty' : 'populated',
    fail: fixture === 'error',
    delayMs: Number.isFinite(delay) ? Math.min(5000, Math.max(0, delay)) : 0,
  };
}

const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export function createFixtureClient(opts: FixtureSet | FixtureOptions): SnapshotClient {
  const { set, fail, delayMs } = typeof opts === 'string' ? { set: opts, fail: false, delayMs: 0 } : opts;

  async function load<T extends SnapshotFile>(name: string, file: T['file'], failable: boolean): Promise<T> {
    const key = `../fixtures/${set}/${name}.json`;
    if (delayMs > 0) await wait(delayMs);
    if (fail && failable) throw new SnapshotError('http', key, 503);
    const loader = loaders[key];
    if (!loader) throw new SnapshotError('not_found', key, 404);
    const json = await loader();
    return checkFile<T>(structuredClone(json), file, key);
  }

  return {
    meta: () => load('meta', 'meta', false),
    chains: () => load('chains', 'chains', true),
    metrics: () => load('metrics', 'metrics', true),
    entity: (chainId) => {
      if (!CHAIN_ID_RE.test(chainId)) {
        return Promise.reject(new SnapshotError('not_found', `../fixtures/${set}/entities/${chainId}.json`, 404));
      }
      return load(`entities/${chainId}`, 'entity', false);
    },
  };
}
