import { useId, type ReactNode } from 'react';
import { ChevronDown } from 'lucide-react';
import { FOCUS_RING } from './Button';

export function Select({
  label,
  value,
  onChange,
  children,
  id,
  className,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  children: ReactNode;
  id?: string;
  className?: string;
}) {
  const auto = useId();
  const selectId = id ?? auto;
  return (
    <div className={className}>
      <label htmlFor={selectId} className="mb-1 block text-xs font-medium text-fg-muted">
        {label}
      </label>
      <div className="relative">
        <select
          id={selectId}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className={`h-10 w-full cursor-pointer appearance-none truncate rounded-sm border border-border-input bg-surface px-3 pr-8 text-sm text-fg ${FOCUS_RING}`}
        >
          {children}
        </select>
        <ChevronDown
          className="pointer-events-none absolute top-1/2 right-2.5 size-4 -translate-y-1/2 text-fg-subtle"
          aria-hidden="true"
        />
      </div>
    </div>
  );
}
