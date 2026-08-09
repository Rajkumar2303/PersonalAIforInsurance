import { useEffect, useState } from 'react';

const STATUS_META = {
  ok: { label: 'Backend online', className: 'ok' },
  error: { label: 'Backend unreachable', className: 'error' },
  loading: { label: 'Checking backend…', className: 'loading' },
};

/**
 * Polls GET /health (proxied to the FastAPI backend in dev) and shows a
 * simple status indicator. Issue 1: shell only, no dashboard yet.
 */
export default function HealthStatus() {
  const [state, setState] = useState('loading');

  useEffect(() => {
    let cancelled = false;

    async function check() {
      try {
        const response = await fetch('/health');
        const data = await response.json();
        if (!cancelled) setState(data.status === 'ok' ? 'ok' : 'error');
      } catch {
        if (!cancelled) setState('error');
      }
    }

    check();
    const interval = setInterval(check, 15000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  const meta = STATUS_META[state] ?? STATUS_META.loading;

  return (
    <section className="health-card">
      <h2>Backend status</h2>
      <div className="status-row">
        <span className={`status-dot ${meta.className}`} aria-hidden="true" />
        <span className="status-label">{meta.label}</span>
      </div>
    </section>
  );
}
