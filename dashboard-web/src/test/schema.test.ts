import fs from 'node:fs';
import path from 'node:path';
import Ajv2020 from 'ajv/dist/2020';
import { describe, expect, it } from 'vitest';
import { SCENARIOS } from '../data/derive';
import {
  CHAIN_STATUSES,
  KINDS,
  LINK_KINDS,
  NEEDS_ACTION_REASONS,
  RANGE_KEYS,
  REPOS,
  SOURCE_STATUSES,
  SPAN_STATUSES,
  STAGE_NAMES,
  STAGE_STATUSES,
} from '../data/types';

// jsdom gives import.meta.url an http: scheme, so resolve from this file's directory instead.
const here = import.meta.dirname;
const schemaPath = path.resolve(here, '../../../scripts/ops/schema/snapshot.v1.json');
const fixturesDir = path.resolve(here, '../fixtures');

interface JsonSchema {
  $defs: Record<string, { enum?: string[]; pattern?: string; properties?: Record<string, JsonSchema['$defs'][string]> }>;
}

const schema = JSON.parse(fs.readFileSync(schemaPath, 'utf8')) as JsonSchema;
const defs = schema.$defs;

const fixtureFiles = (fs.readdirSync(fixturesDir, { recursive: true }) as string[])
  .filter((f) => f.endsWith('.json'))
  .map((f) => f.split(path.sep).join('/'))
  .sort();

const readFixture = (rel: string): unknown => JSON.parse(fs.readFileSync(path.join(fixturesDir, rel), 'utf8'));

describe('snapshot.v1 contract', () => {
  it('test_every_fixture_is_schema_valid', () => {
    const ajv = new Ajv2020({ strict: false, allErrors: true });
    const validate = ajv.compile(schema);
    expect(fixtureFiles).toHaveLength(12);
    for (const rel of fixtureFiles) {
      const ok = validate(readFixture(rel));
      expect(ok, `${rel}: ${ajv.errorsText(validate.errors)}`).toBe(true);
    }
  });

  it('rejects a file that breaks the contract', () => {
    const validate = new Ajv2020({ strict: false }).compile(schema);
    const meta = readFixture('populated/meta.json') as Record<string, unknown>;
    expect(validate({ ...meta, sources: { github: 'ok', langfuse: 'down' } })).toBe(false);
  });

  it('test_enums_mirror_schema', () => {
    expect([...REPOS]).toEqual(defs.repo?.enum);
    expect([...KINDS]).toEqual(defs.kind?.enum);
    expect([...CHAIN_STATUSES]).toEqual(defs.chain_status?.enum);
    expect([...STAGE_NAMES]).toEqual(defs.stage?.properties?.name?.enum);
    expect([...STAGE_STATUSES]).toEqual(defs.stage?.properties?.status?.enum);
    expect([...NEEDS_ACTION_REASONS]).toEqual(defs.needs_action?.properties?.reason?.enum);
    expect([...LINK_KINDS]).toEqual(defs.link?.properties?.kind?.enum);
    expect([...SOURCE_STATUSES]).toEqual(defs.source_status?.enum);
    expect([...SPAN_STATUSES]).toEqual(defs.span?.properties?.status?.enum);
    expect([...RANGE_KEYS]).toEqual(Object.keys(defs.file_metrics?.properties?.ranges?.properties ?? {}));
  });

  it('test_scenarios_match_schema_pattern', () => {
    const pattern = new RegExp(defs.scenario?.pattern ?? '^$');
    expect(SCENARIOS).toHaveLength(30);
    expect(SCENARIOS.every((s) => pattern.test(s))).toBe(true);
    expect(new Set(SCENARIOS).size).toBe(30);
    expect(SCENARIOS[0]).toBe('pass/01');
    expect(SCENARIOS[29]).toBe('runtime/10');
  });

  it('test_entities_cover_populated_chains', () => {
    const chains = readFixture('populated/chains.json') as { chains: { chain_id: string }[] };
    expect(chains.chains.length).toBeGreaterThan(0);
    for (const { chain_id } of chains.chains) {
      const rel = `populated/entities/${chain_id}.json`;
      expect(fixtureFiles).toContain(rel);
      expect((readFixture(rel) as { chain_id: string }).chain_id).toBe(chain_id);
    }
  });
});
