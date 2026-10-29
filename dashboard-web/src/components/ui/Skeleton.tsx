import clsx from 'clsx';

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={clsx(
        'rounded-md bg-surface-2 bg-[length:200%_100%]',
        'bg-[linear-gradient(90deg,var(--surface-2)_0%,var(--border)_50%,var(--surface-2)_100%)]',
        'motion-safe:animate-[shimmer_1.6s_linear_infinite]',
        className,
      )}
    />
  );
}
