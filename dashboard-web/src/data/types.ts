// Hand-written mirror of scripts/ops/schema/snapshot.v1.json (ops.snapshot.v1).
// Drift is caught by src/test/schema.test.ts: every fixture is validated against the real
// schema and every enum array below must equal the schema's enum.

export const SNAPSHOT_SCHEMA = 'ops.snapshot.v1' as const;
export const REPOS = ['Keshav0375/Sentinel', 'Keshav0375/Sentinel-deployment'] as const;
export const KINDS = ['build.started', 'build.succeeded', 'build.failed', 'deploy.started', 'deploy.succeeded', 'deploy.failed',
  'ci.pr_gate.completed', 'scale.changed', 'alert.received', 'agent.started', 'agent.decided', 'agent.failed', 'revert_pr.opened',
  'revert_pr.review_submitted', 'revert_pr.merged', 'revert_pr.closed', 'escalation.raised', 'eval.scored', 'trace.summarized'] as const;
export const CHAIN_STATUSES = ['running', 'succeeded', 'failed', 'awaiting_review', 'escalated', 'reverted', 'closed'] as const;
export const STAGE_NAMES = ['deploy', 'alert', 'agent', 'pr', 'review', 'merge', 'escalation'] as const;
export const STAGE_STATUSES = ['succeeded', 'failed', 'running', 'waiting', 'skipped', 'none'] as const;
export const NEEDS_ACTION_REASONS = ['pr_awaiting_review', 'escalation', 'failed_no_incident'] as const;
export const LINK_KINDS = ['github_run', 'github_pr', 'langfuse', 'datadog'] as const;
export const SOURCE_STATUSES = ['ok', 'error', 'skipped'] as const;
export const SPAN_STATUSES = ['ok', 'warning', 'error'] as const;
export const RANGE_KEYS = ['24h', '7d', '30d', 'all'] as const;
export const SCENARIO_CASES = ['pass', 'deployfail', 'runtime'] as const;
export const EVAL_CASES = ['i', 'ii', 'iii'] as const;

export type Repo = (typeof REPOS)[number];
export type Kind = (typeof KINDS)[number];
export type ChainStatus = (typeof CHAIN_STATUSES)[number];
export type StageName = (typeof STAGE_NAMES)[number];
export type StageStatus = (typeof STAGE_STATUSES)[number];
export type NeedsActionReason = (typeof NEEDS_ACTION_REASONS)[number];
export type LinkKind = (typeof LINK_KINDS)[number];
export type SourceStatus = (typeof SOURCE_STATUSES)[number];
export type SpanStatus = (typeof SPAN_STATUSES)[number];
export type RangeKey = (typeof RANGE_KEYS)[number];
export type ScenarioCase = (typeof SCENARIO_CASES)[number];
export type EvalCase = (typeof EVAL_CASES)[number];

type Two = '01' | '02' | '03' | '04' | '05' | '06' | '07' | '08' | '09' | '10';
export type Scenario = `${ScenarioCase}/${Two}`;
export type Timestamp = string; // YYYY-MM-DDTHH:MM:SSZ (UTC)

export interface Stage {
  name: StageName;
  status: StageStatus;
  at: Timestamp | null;
  duration_s: number | null;
  label: string;
  url: string | null;
}

export interface NeedsAction {
  reason: NeedsActionReason;
  since: Timestamp;
  url: string | null;
}

export interface ChainSummary {
  chain_id: string;
  title: string;
  repo: Repo;
  status: ChainStatus;
  scenario: Scenario | null;
  sha: string | null;
  correlation_id: string | null;
  incident_id: string | null;
  pr: number | null;
  run_id: number | null;
  started_at: Timestamp;
  updated_at: Timestamp;
  ended_at: Timestamp | null;
  duration_s: number | null;
  stages: Stage[];
  needs_action: NeedsAction | null;
  summary: string | null;
  resolution_type: 'rollback' | 'escalated' | null;
  confidence: number | null;
  eval_total: number | null;
  kinds: Kind[];
  event_count: number;
  has_trace: boolean;
  has_eval: boolean;
}

export interface AgentDecision {
  decided_at: Timestamp;
  resolution_type: 'rollback' | 'escalated';
  confidence: number;
  root_cause_summary: string | null;
  tool_calls: number | null;
  reflexion_loops: number | null;
  mttr_s: number | null;
  models_used: string[];
  langfuse_trace_url: string | null;
}

export interface Span {
  span_id: string;
  parent_id: string | null;
  name: string;
  kind: string;
  start_ms: number;
  duration_ms: number;
  status: SpanStatus;
  input_tokens: number | null;
  output_tokens: number | null;
}

export interface Trace {
  trace_id: string;
  langfuse_trace_url: string | null;
  total_duration_ms: number;
  spans: Span[];
}

export interface Checks {
  fired: number;
  classified: number;
  correlated: number;
  revert_target: number;
}

export interface Judge {
  triage_accuracy: number;
  root_cause_correctness: number;
  tool_efficiency: number;
  mttr: number;
  remediation_safety: number;
}

export interface EvalScore {
  scored_at: Timestamp;
  scenario: Scenario | null;
  case: EvalCase | null;
  checks: Checks | null;
  judge: Judge | null;
  total_score: number | null;
  judge_model: string | null;
  judge_conflict: boolean;
  judge_failed: boolean;
  langfuse_trace_url: string | null;
}

export interface Link {
  kind: LinkKind;
  label: string;
  url: string;
  restricted: boolean;
}

export interface Kpis {
  deploys: number;
  deploy_failures: number;
  failure_rate: number | null;
  builds: number;
  build_failures: number;
  incidents: number;
  mttr_p50_s: number | null;
  mttr_p90_s: number | null;
  revert_prs_opened: number;
  revert_prs_merged: number;
  escalations: number;
  eval_runs: number;
  eval_score_mean: number | null;
}

export interface DailyPoint {
  date: string;
  deploys: number;
  deploy_failures: number;
  incidents: number;
  escalations: number;
  revert_prs_merged: number;
  mttr_p50_s: number | null;
  eval_score_mean: number | null;
}

export interface EvalScenario {
  scenario: Scenario;
  case: EvalCase | null;
  runs: number;
  last_scored_at: Timestamp;
  last_total_score: number | null;
  mean_total_score: number | null;
  checks_pass_rate: Checks | null;
  judge_failures: number;
  judge_conflicts: number;
}

export interface OpsEvent {
  schema: string;
  event_id: string;
  kind: Kind;
  ts: Timestamp;
  recorded_at?: Timestamp;
  source?: string;
  chain?: Record<string, unknown>;
  data?: Record<string, unknown>;
}

interface FileBase<F extends string> {
  schema: typeof SNAPSHOT_SCHEMA;
  file: F;
  generated_at: Timestamp;
}

export interface MetaFile extends FileBase<'meta'> {
  ledger_head: string | null;
  event_count: number;
  chain_count: number;
  first_event_at: Timestamp | null;
  last_event_at: Timestamp | null;
  reconciler_run_url: string | null;
  sources: { github: SourceStatus; langfuse: SourceStatus };
}

export interface ChainsFile extends FileBase<'chains'> {
  chains: ChainSummary[];
}

export interface MetricsFile extends FileBase<'metrics'> {
  ranges: Record<RangeKey, Kpis>;
  current: {
    revert_prs_awaiting_review: number;
    oldest_awaiting_since: Timestamp | null;
    escalations_open: number;
    failed_no_incident: number;
  };
  daily: DailyPoint[];
  eval: { scenarios: EvalScenario[] };
}

export interface EntityFile extends FileBase<'entity'> {
  chain_id: string;
  chain: ChainSummary;
  events: OpsEvent[];
  agent: AgentDecision | null;
  trace: Trace | null;
  evals: EvalScore[];
  links: Link[];
}

export type SnapshotFile = MetaFile | ChainsFile | MetricsFile | EntityFile;

export const CHAIN_ID_RE = /^c_[0-9a-f]{16}$/;
