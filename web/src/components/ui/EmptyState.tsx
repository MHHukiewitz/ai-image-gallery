import clsx from "clsx";
import type { ReactNode } from "react";

interface EmptyStateProps {
  title: string;
  description?: string;
  icon?: ReactNode;
  /** Usually a Button, for example "Clear search". */
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ title, description, icon, action, className }: EmptyStateProps) {
  return (
    <div
      className={clsx(
        "flex flex-col items-center gap-3 px-6 py-16 text-center text-zinc-500 dark:text-zinc-400",
        className,
      )}
    >
      {icon && <div className="text-zinc-400 dark:text-zinc-500">{icon}</div>}
      <h2 className="text-base font-medium text-zinc-900 dark:text-zinc-100">{title}</h2>
      {description && <p className="max-w-sm text-sm">{description}</p>}
      {action}
    </div>
  );
}
