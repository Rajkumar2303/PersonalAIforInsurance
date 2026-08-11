import { useEffect, useRef, useState } from 'react';
import { getJob } from '../api';

const STATUS_LABELS = {
  searching: 'Searching…',
  quote_received: 'Quote received',
  estimate_received: 'Estimate received',
  blocked: 'Blocked',
  callback_required: 'Callback required',
  manual_handoff: 'Manual handoff',
  ineligible: 'Ineligible',
  affinity_restricted: 'Affinity restricted',
  specialty_only: 'Specialty only',
  not_currently_writing: 'Not currently writing',
  unreachable: 'Unreachable',
  unresolved: 'Unresolved',
  duplicate_rate_source: 'Duplicate rate source',
  consent_required: 'Consent required',
  not_ready: 'Not ready',
  refused: 'Refused',
  not_configured: 'Not configured',
  error: 'Error',
};

const STATUS_CLASS = {
  searching: 'searching',
  quote_received: 'quote',
  estimate_received: 'estimate',
  duplicate_rate_source: 'duplicate',
  blocked: 'blocked',
  callback_required: 'callback',
  manual_handoff: 'handoff',
  unresolved: 'unresolved',
  unreachable: 'unreachable',
};

/**
 * Polls the backend comparison job and renders per-provider progress/results.
 * Polling only - no SSE/WebSockets. A raw observed premium is shown ONLY as
 * "Quote received - pending coverage normalization". It is NEVER labelled
 * best/cheapest/comparable (Issues #11/#12 are not implemented).
 */
export default function CompareProgress({ jobId, onReset }) {
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);
  const timerRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const data = await getJob(jobId);
        if (cancelled) return;
        setJob(data);
        setError(null);
        if (data.status === 'done' || data.status === 'failed') {
          clearInterval(timerRef.current);
        }
      } catch (err) {
        if (!cancelled) setError(err.message);
      }
    }
    poll();
    timerRef.current = setInterval(poll, 700);
    return () => {
      cancelled = true;
      clearInterval(timerRef.current);
    };
  }, [jobId]);

  const done = job && (job.status === 'done' || job.status === 'failed');
  const routes = job?.routes || [];

  return (
    <section className="card">
      <h2>Comparing quotes…</h2>
      <p className="privacy-note">
        {job?.execution_mode === 'mock'
          ? 'Mock mode: the local synthetic mock site is being driven (no real insurer contacted).'
          : 'Live mode.'}
      </p>

      {error && <p className="error-text">{error}</p>}
      {job?.error && <p className="error-text">{job.error}</p>}

      <table className="progress-table">
        <thead>
          <tr>
            <th>Provider</th>
            <th>Status</th>
            <th>Details</th>
          </tr>
        </thead>
        <tbody>
          {routes.map((route) => {
            const label = STATUS_LABELS[route.status] || route.status;
            return (
              <tr key={route.registry_id}>
                <td>
                  <strong>{route.brand_or_program}</strong>
                  {route.is_alternative && <span className="tag tag-alt">duplicate</span>}
                </td>
                <td>
                  <span className={`status-pill ${STATUS_CLASS[route.status] || ''}`}>
                    {!done && route.status === 'searching' ? (
                      <span className="spinner" aria-hidden="true" />
                    ) : null}
                    {label}
                  </span>
                </td>
                <td>
                  {route.status === 'quote_received' ? (
                    <div className="quote-box">
                      <span className="quote-amount">
                        ${route.annual_amount_parsed != null ? route.annual_amount_parsed.toFixed(2) : '—'}/year
                      </span>
                      <span className="quote-note">Quote received — pending coverage normalization</span>
                    </div>
                  ) : (
                    <span className="muted">
                      {route.reason_codes?.join(', ') || route.message || '—'}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {!done && routes.length === 0 && <p className="privacy-note">Waiting for the backend to begin…</p>}

      {done && (
        <div className="actions">
          <button type="button" className="secondary-btn" onClick={onReset}>
            Start over
          </button>
        </div>
      )}
    </section>
  );
}
