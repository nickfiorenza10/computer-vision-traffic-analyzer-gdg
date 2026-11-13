import type { ReactNode } from 'react';

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="state loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function Empty({ title, detail }: { title: string; detail?: ReactNode }) {
  return (
    <div className="state">
      <strong>{title}</strong>
      {detail ? <div className="note">{detail}</div> : null}
    </div>
  );
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="state error" role="alert">
      <strong>Could not load this view.</strong>
      <div>{message}</div>
      {onRetry ? (
        <div style={{ marginTop: '0.6rem' }}>
          <button type="button" onClick={onRetry}>
            Try again
          </button>
        </div>
      ) : null}
    </div>
  );
}

export type StateProps = {
  loading: boolean;
  error: string | null;
  onRetry?: () => void;
  empty?: boolean;
  emptyTitle?: string;
  emptyDetail?: string;
  children: ReactNode;
};

/** Wraps every data view so loading, error and empty are never forgotten. */
export function DataState({
  loading,
  error,
  onRetry,
  empty,
  emptyTitle = 'No data in this window.',
  emptyDetail = 'The API returns empty arrays when a camera has no events in the selected range.',
  children,
}: StateProps) {
  if (loading) return <Loading />;
  if (error) return <ErrorState message={error} onRetry={onRetry} />;
  if (empty) return <Empty title={emptyTitle} detail={emptyDetail} />;
  return <>{children}</>;
}
