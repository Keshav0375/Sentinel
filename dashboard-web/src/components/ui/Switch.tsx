import { useId } from 'react';
import * as RadixSwitch from '@radix-ui/react-switch';
import { FOCUS_RING } from './Button';

export function Switch({
  checked,
  onCheckedChange,
  label,
  description,
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  label: string;
  description?: string;
}) {
  const id = useId();
  const descId = `${id}-desc`;
  return (
    <div className="flex min-h-10 items-center gap-2">
      <RadixSwitch.Root
        id={id}
        checked={checked}
        onCheckedChange={onCheckedChange}
        aria-describedby={description ? descId : undefined}
        className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-border-input bg-surface-2 transition-colors duration-[var(--dur-fast)] data-[state=checked]:border-accent data-[state=checked]:bg-accent ${FOCUS_RING}`}
      >
        <RadixSwitch.Thumb className="block size-3.5 translate-x-0.5 rounded-full bg-fg-muted shadow-sm transition-transform duration-[var(--dur-fast)] data-[state=checked]:translate-x-[18px] data-[state=checked]:bg-accent-fg" />
      </RadixSwitch.Root>
      <label htmlFor={id} className="cursor-pointer text-xs font-medium text-fg-muted select-none">
        {label}
      </label>
      {description && (
        <span id={descId} className="sr-only">
          {description}
        </span>
      )}
    </div>
  );
}
