import * as ToggleGroup from '@radix-ui/react-toggle-group';
import { Monitor, Moon, Sun, type LucideIcon } from 'lucide-react';
import { FOCUS_RING } from './ui/Button';
import { useTheme, type ThemePref } from '../state/theme';

const OPTIONS: { value: ThemePref; label: string; Icon: LucideIcon }[] = [
  { value: 'system', label: 'System theme', Icon: Monitor },
  { value: 'light', label: 'Light theme', Icon: Sun },
  { value: 'dark', label: 'Dark theme', Icon: Moon },
];

export function ThemeToggle() {
  const { pref, setPref } = useTheme();
  return (
    <ToggleGroup.Root
      type="single"
      aria-label="Theme"
      value={pref}
      onValueChange={(v) => {
        if (v !== '') setPref(v as ThemePref);
      }}
      className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface-2 p-0.5"
    >
      {OPTIONS.map(({ value, label, Icon }) => (
        <ToggleGroup.Item
          key={value}
          value={value}
          aria-label={label}
          className={`inline-flex size-8 items-center justify-center rounded-sm text-fg-subtle transition-colors duration-[var(--dur-fast)] hover:text-fg data-[state=on]:bg-surface data-[state=on]:text-fg data-[state=on]:shadow-sm ${FOCUS_RING}`}
        >
          <Icon className="size-4" aria-hidden="true" />
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  );
}
