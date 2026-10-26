import * as ToggleGroup from '@radix-ui/react-toggle-group';
import { FOCUS_RING } from './ui/Button';
import type { RangeKey } from '../data/types';
import { useView } from '../state/url';

const OPTIONS: { value: RangeKey; label: string }[] = [
  { value: '24h', label: '24 h' },
  { value: '7d', label: '7 d' },
  { value: '30d', label: '30 d' },
  { value: 'all', label: 'All' },
];

export function TimeRangePicker() {
  const { view, update } = useView();
  return (
    <ToggleGroup.Root
      type="single"
      aria-label="Time range"
      value={view.range}
      onValueChange={(v) => {
        if (v !== '') update({ range: v as RangeKey });
      }}
      className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface-2 p-0.5"
    >
      {OPTIONS.map(({ value, label }) => (
        <ToggleGroup.Item
          key={value}
          value={value}
          className={`inline-flex h-8 min-w-12 items-center justify-center rounded-sm px-3 text-xs font-medium text-fg-muted tabular-nums transition-colors duration-[var(--dur-fast)] hover:text-fg data-[state=on]:bg-surface data-[state=on]:text-fg data-[state=on]:shadow-sm ${FOCUS_RING}`}
        >
          {label}
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  );
}
