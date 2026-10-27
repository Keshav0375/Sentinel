import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { createHttpClient, snapshotBaseUrl, SnapshotError } from '../data/client';
import { createFixtureClient, readFixtureOptions } from '../data/fixtures-loader';
import meta from '../fixtures/populated/meta.json';

function response(body: unknown, init: { status?: number; badJson?: boolean } = {}): Response {
  const status = init.status ?? 200;
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      if (init.badJson) throw new SyntaxError('Unexpected token');
      return structuredClone(body);
    },
  } as Response;
}

async function rejection(p: Promise<unknown>): Promise<SnapshotError> {
  const err = await p.then(
    () => null,
    (e: unknown) => e,
  );
  expect(err).toBeInstanceOf(SnapshotError);
  return err as SnapshotError;
}

describe('http client', () => {
  it('test_http_client_fetches_meta_under_base', async () => {
    const f = vi.fn(async () => response(meta));
    const result = await createHttpClient('/Sentinel/snapshot/', f).meta();
    expect(f).toHaveBeenCalledWith('/Sentinel/snapshot/meta.json', expect.objectContaining({ cache: 'default' }));
    expect(result).toEqual(meta);
  });

  it('test_entity_fetch_is_no_cache', async () => {
    const f = vi.fn<typeof fetch>(async () => response({ schema: 'ops.snapshot.v1', file: 'entity' }));
    const client = createHttpClient('/Sentinel/snapshot/', f);
    await client.entity('c_2c8f0e7a5b1d3f94');
    expect(f).toHaveBeenCalledWith(
      '/Sentinel/snapshot/entities/c_2c8f0e7a5b1d3f94.json',
      expect.objectContaining({ cache: 'no-cache' }),
    );
    // The body is an entity file, so these reject; only the request mode matters here.
    await client.chains().catch(() => null);
    await client.metrics().catch(() => null);
    const modes = f.mock.calls.map(([url, init]) => [String(url), init?.cache]);
    expect(modes).toEqual([
      ['/Sentinel/snapshot/entities/c_2c8f0e7a5b1d3f94.json', 'no-cache'],
      ['/Sentinel/snapshot/chains.json', 'default'],
      ['/Sentinel/snapshot/metrics.json', 'default'],
    ]);
  });

  it('test_index_preloads', () => {
    // The three first-load files are preloaded (and consumed: the client fetches them in default mode).
    const index = fs.readFileSync(path.resolve(import.meta.dirname, '../../index.html'), 'utf8');
    const links = [...index.matchAll(/<link rel="preload" as="fetch" crossorigin href="([^"]+)">/g)].map((m) => m[1]);
    expect(links).toEqual([
      '%BASE_URL%snapshot/meta.json',
      '%BASE_URL%snapshot/chains.json',
      '%BASE_URL%snapshot/metrics.json',
    ]);
    expect(index.match(/rel="preload"/g)).toHaveLength(3);
  });

  it('test_snapshot_base_url', () => {
    expect(snapshotBaseUrl('/Sentinel/')).toBe('/Sentinel/snapshot/');
    expect(snapshotBaseUrl('/Sentinel')).toBe('/Sentinel/snapshot/');
  });

  it('test_http_errors_are_typed', async () => {
    const client = (r: () => Promise<Response>) => createHttpClient('/Sentinel/snapshot/', vi.fn(r));

    expect((await rejection(client(async () => response(null, { status: 404 })).chains())).kind).toBe('not_found');

    const http = await rejection(client(async () => response(null, { status: 503 })).metrics());
    expect(http.kind).toBe('http');
    expect(http.status).toBe(503);
    expect(http.message).toBe('Server returned 503');

    expect((await rejection(client(async () => response(meta, { badJson: true })).meta())).kind).toBe('parse');

    const network = await rejection(
      client(async () => {
        throw new TypeError('Failed to fetch');
      }).meta(),
    );
    expect(network.kind).toBe('network');

    expect((await rejection(client(async () => response({ ...meta, file: 'chains' })).meta())).kind).toBe('schema');
    const v2 = await rejection(client(async () => response({ ...meta, schema: 'ops.snapshot.v2' })).meta());
    expect(v2.kind).toBe('schema');
    expect(v2.message).toBe('Unexpected snapshot format (expected ops.snapshot.v1)');
  });

  it('test_entity_rejects_bad_id_without_fetch', async () => {
    const f = vi.fn(async () => response(meta));
    const err = await rejection(createHttpClient('/Sentinel/snapshot/', f).entity('../meta'));
    expect(err.kind).toBe('not_found');
    expect(f).not.toHaveBeenCalled();
  });

  it('rethrows aborts untouched', async () => {
    const abort = new DOMException('Aborted', 'AbortError');
    const client = createHttpClient(
      '/Sentinel/snapshot/',
      vi.fn(async () => {
        throw abort;
      }),
    );
    await expect(client.meta()).rejects.toBe(abort);
  });
});

describe('fixture client', () => {
  it('test_fixture_client_sets', async () => {
    const populated = createFixtureClient('populated');
    expect((await populated.meta()).chain_count).toBe(6);
    expect((await populated.chains()).chains).toHaveLength(6);
    expect((await populated.entity('c_2c8f0e7a5b1d3f94')).events).toHaveLength(9);
    expect((await rejection(populated.entity('c_ffffffffffffffff'))).kind).toBe('not_found');

    const empty = createFixtureClient('empty');
    expect((await empty.chains()).chains).toEqual([]);
  });

  it('returns a copy, never the shared fixture object', async () => {
    const client = createFixtureClient('populated');
    const a = await client.meta();
    a.chain_count = 99;
    expect((await client.meta()).chain_count).toBe(6);
  });

  it('test_fixture_options', async () => {
    expect(readFixtureOptions('?fixture=empty&delay=9999')).toEqual({ set: 'empty', fail: false, delayMs: 5000 });
    expect(readFixtureOptions('?fixture=error')).toEqual({ set: 'populated', fail: true, delayMs: 0 });
    expect(readFixtureOptions('')).toEqual({ set: 'populated', fail: false, delayMs: 0 });
    expect(readFixtureOptions('?delay=-5').delayMs).toBe(0);

    const failing = createFixtureClient(readFixtureOptions('?fixture=error'));
    const err = await rejection(failing.chains());
    expect(err.kind).toBe('http');
    expect(err.status).toBe(503);
    expect((await rejection(failing.metrics())).status).toBe(503);
    await expect(failing.meta()).resolves.toMatchObject({ file: 'meta' });
  });
});
