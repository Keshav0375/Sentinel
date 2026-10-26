// Display vocabulary: every status is icon + label + tone, never color alone.
import {
  Activity,
  Bell,
  Bot,
  CircleCheck,
  CircleDashed,
  CircleMinus,
  CircleSlash,
  CircleX,
  Clock,
  Eye,
  FlaskConical,
  GitMerge,
  GitPullRequest,
  Hammer,
  ListTree,
  LoaderCircle,
  Rocket,
  ShieldCheck,
  Siren,
  Undo2,
  type LucideIcon,
} from 'lucide-react';
import type { KindFamily, KpiFocus } from './derive';
import type { ChainStatus, Kind, NeedsActionReason, StageName, StageStatus } from './types';

export type Tone = 'success' | 'danger' | 'warning' | 'info' | 'neutral' | 'accent';

export interface StatusMeta {
  label: string;
  tone: Tone;
  Icon: LucideIcon;
}

export const TONE_CLASSES: Record<Tone, string> = {
  success: 'text-success bg-success-bg border-success/30',
  danger: 'text-danger bg-danger-bg border-danger/30',
  warning: 'text-warning bg-warning-bg border-warning/30',
  info: 'text-info bg-info-bg border-info/30',
  neutral: 'text-neutral bg-neutral-bg border-neutral/30',
  accent: 'text-accent bg-accent-soft border-accent/30',
};

export const CHAIN_STATUS_META: Record<ChainStatus, StatusMeta> = {
  running: { label: 'Running', tone: 'info', Icon: LoaderCircle },
  succeeded: { label: 'Succeeded', tone: 'success', Icon: CircleCheck },
  failed: { label: 'Failed', tone: 'danger', Icon: CircleX },
  awaiting_review: { label: 'Awaiting review', tone: 'warning', Icon: Clock },
  escalated: { label: 'Escalated', tone: 'danger', Icon: Siren },
  reverted: { label: 'Reverted', tone: 'success', Icon: Undo2 },
  closed: { label: 'Closed', tone: 'neutral', Icon: CircleSlash },
};

export const STAGE_STATUS_META: Record<StageStatus, StatusMeta> = {
  succeeded: { label: 'Succeeded', tone: 'success', Icon: CircleCheck },
  failed: { label: 'Failed', tone: 'danger', Icon: CircleX },
  running: { label: 'Running', tone: 'info', Icon: LoaderCircle },
  waiting: { label: 'Waiting', tone: 'warning', Icon: Clock },
  skipped: { label: 'Skipped', tone: 'neutral', Icon: CircleMinus },
  none: { label: 'Not reached', tone: 'neutral', Icon: CircleDashed },
};

export const STAGE_NAME_META: Record<StageName, { label: string; Icon: LucideIcon }> = {
  deploy: { label: 'Deploy', Icon: Rocket },
  alert: { label: 'Alert', Icon: Bell },
  agent: { label: 'Agent', Icon: Bot },
  pr: { label: 'PR', Icon: GitPullRequest },
  review: { label: 'Review', Icon: Eye },
  merge: { label: 'Merge', Icon: GitMerge },
  escalation: { label: 'Escalation', Icon: Siren },
};

export const NEEDS_ACTION_META: Record<NeedsActionReason, StatusMeta> = {
  pr_awaiting_review: { label: 'Revert PR awaiting review', tone: 'warning', Icon: GitPullRequest },
  escalation: { label: 'Escalation', tone: 'danger', Icon: Siren },
  failed_no_incident: { label: 'Failed, no incident', tone: 'danger', Icon: CircleX },
};

export const KIND_FAMILY_META: Record<KindFamily, { label: string; Icon: LucideIcon }> = {
  build: { label: 'Build', Icon: Hammer },
  deploy: { label: 'Deploy', Icon: Rocket },
  ci: { label: 'CI', Icon: ShieldCheck },
  scale: { label: 'Scale', Icon: Activity },
  alert: { label: 'Alert', Icon: Bell },
  agent: { label: 'Agent', Icon: Bot },
  revert_pr: { label: 'Revert PR', Icon: GitPullRequest },
  escalation: { label: 'Escalation', Icon: Siren },
  eval: { label: 'Eval', Icon: FlaskConical },
  trace: { label: 'Trace', Icon: ListTree },
};

const KIND_LABELS: Record<Kind, string> = {
  'build.started': 'Build started',
  'build.succeeded': 'Build succeeded',
  'build.failed': 'Build failed',
  'deploy.started': 'Deploy started',
  'deploy.succeeded': 'Deploy succeeded',
  'deploy.failed': 'Deploy failed',
  'ci.pr_gate.completed': 'PR gate completed',
  'scale.changed': 'Scale changed',
  'alert.received': 'Alert received',
  'agent.started': 'Agent started',
  'agent.decided': 'Agent decided',
  'agent.failed': 'Agent failed',
  'revert_pr.opened': 'Revert PR opened',
  'revert_pr.review_submitted': 'Revert PR reviewed',
  'revert_pr.merged': 'Revert PR merged',
  'revert_pr.closed': 'Revert PR closed',
  'escalation.raised': 'Escalation raised',
  'eval.scored': 'Eval scored',
  'trace.summarized': 'Trace summarized',
};

export function KIND_LABEL(k: Kind): string {
  return KIND_LABELS[k];
}

export const FOCUS_META: Record<KpiFocus, { label: string }> = {
  deploys: { label: 'Deploys' },
  failure_rate: { label: 'Failure rate' },
  incidents: { label: 'Incidents' },
  mttr: { label: 'MTTR' },
  awaiting: { label: 'Awaiting review' },
  escalations: { label: 'Escalations' },
  eval: { label: 'Eval score' },
};
