import { useId, type ReactNode } from 'react';
import clsx from 'clsx';

export function Card({
  as: As = 'div',
  title,
  actions,
  children,
  className,
}: {
  as?: 'section' | 'div';
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const id = useId();
  const labelled = As === 'section' && title !== undefined ? id : undefined;
  return (
    <As aria-labelledby={labelled} className={clsx('rounded-lg border border-border bg-surface shadow-sm', className)}>
      {(title !== undefined || actions !== undefined) && (
        <div className="flex flex-wrap items-center justify-between gap-3 px-4 pt-4 sm:px-5">
          {title !== undefined && (
            <h2 id={id} className="flex items-center gap-2 text-base font-semibold">
              {title}
            </h2>
          )}
          {actions}
        </div>
      )}
      {children}
    </As>
  );
}
