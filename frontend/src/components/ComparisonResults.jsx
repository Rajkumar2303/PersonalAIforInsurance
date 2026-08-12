import { useEffect, useRef, useState } from 'react';
import { getComparisonRun } from '../api';

const POLL_MS = 1000;
// Client-side backstop so the UI never spins forever even if the backend is
// slow/hung (the backend also enforces its own run/route timeouts - Issue #14).
const MAX_WAIT_MS = 150000;
const TERMINAL = ['completed', 'completed_with_partial_results', 'failed'];

const STATUS_LABELS = {
  queued: 'Queued',
  running: 'Running…',
  quote_pending_normalization: 'Quote received – normalizing…',
  comparable: 'Comparable quote',
  non_comparable: 'Coverage incomplete',
  estimate_only: 'Estimate',
  duplicate_rate_source: 'Duplicate rate source',
  captcha_blocked: 'CAPTCHA blocked',
  unavailable: 'Temporarily unavailable',
  callback_required: 'Callback required',
  manual_handoff: 'Manual follow-up',
  needs_additional_information: 'Needs additional information',
  ineligible: 'Ineligible',
  not_currently_writing: 'Not currently writing',
  affinity_restricted: 'Affinity restricted',
  specialty_only: 'Specialty only',
  not_ready: 'Not ready',
  consent_required: 'Consent required',
  unresolved: 'Unresolved',
  failed: 'Failed',
};

const STATUS_CLASS = {
  comparable: 'quote comparable',
  non_comparable: 'blocked',
  estimate_only: 'estimate',
  duplicate_rate_source: 'duplicate',
  captcha_blocked: 'blocked',
  unavailable: 'blocked',
  needs_additional_information: 'callback',
  callback_required: 'callback',
  manual_handoff: 'handoff',
  failed: 'blocked',
};

function money(value) {
  if (value === null || value === undefined) return '—';
  return `$${Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}/year`;
}

function Coverage({ coverageSummary, missingKeys }) {
  const fields = [
    ['Liability', coverageSummary?.third_party_liability],
    ['Collision', coverageSummary?.collision],
    ['Comprehensive', coverageSummary?.comprehensive],
  ];
  return (
    <ul className="coverage-list">
      {fields.map(([label, value]) => (
        <li key={label}>
          <span className="coverage-label">{label}:</span>{' '}
          {missingKeys && missingKeys.includes(label.toLowerCase()) || !value
            ? <span className="unknown">Unknown</span>
            : <span>{value}</span>}
        </li>
      ))}
    </ul>
  );
}

function Summary({ run }) {
  const c = run.comparison?.summary;
  if (!c) return null;
  return (
    <div className="summary-grid">
      <div><strong>{run.total_routes}</strong><span>Routes attempted</span></div>
      <div><strong>{c.quote_results}</strong><span>Quote responses</span></div>
      <div><strong>{c.comparable_quotes}</strong><span>Comparable quotes</span></div>
      <div><strong>{c.estimates}</strong><span>Estimates</span></div>
      <div><strong>{c.distinct_rate_sources}</strong><span>Distinct rate sources</span></div>
      <div><strong>{c.duplicates}</strong><span>Duplicates</span></div>
    </div>
  );
}

/**
 * Issue #13/#14 - comparison run progress + results.
 *
 * Polls GET /comparison-runs/{id} every ~1s. Hardened for the demo (Issue #14):
 * - stops polling when the run is terminal, never leaves the UI spinning
 * - survives transient request failures (keeps polling, surfaces after N)
 * - has a client-side max-wait backstop + cleanup on unmount
 * - shows a safe fallback when no fully comparable quotes exist (never
 *   "no insurance available", never a fabricated quote)
 * - explains distinct rate sources + the evidence trail for judges
 */
export default function ComparisonResults({ runId, sessionId, onReset }) {
  const [run, setRun] = useState(null);
  const [error, setError] = useState(null);
  const [timedOut, setTimedOut] = useState(false);
  const timerRef = useRef(null);
  const startedRef = useRef(0);
  const consecutiveErrorsRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    startedRef.current = Date.now();
    consecutiveErrorsRef.current = 0;

    async function poll() {
      if (cancelled) return;
      try {
        const data = await getComparisonRun(runId, sessionId);
        if (cancelled) return;
        consecutiveErrorsRef.current = 0;
        setRun(data);
        setError(null);
        if (TERMINAL.includes(data.status)) clearInterval(timerRef.current);
      } catch (err) {
        if (cancelled) return;
        consecutiveErrorsRef.current += 1;
        // Transient backend hiccups should not kill the poll; surface only
        // after repeated failures (and keep the user informed).
        if (consecutiveErrorsRef.current >= 3) {
          setError(`Could not reach the backend (${err.message}). Retrying…`);
        }
      }
    }

    // Client-side backstop: never let the UI spin forever.
    const watchdog = setInterval(() => {
      if (Date.now() - startedRef.current > MAX_WAIT_MS) {
        clearInterval(timerRef.current);
        setTimedOut(true);
      }
    }, 1000);

    poll();
    timerRef.current = setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timerRef.current);
      clearInterval(watchdog);
    };
  }, [runId, sessionId]);

  if (error && !run) {
    return (
      <section className="card">
        <h2>Comparison unavailable</h2>
        <p className="error-text">{error}</p>
        <p className="privacy-note">Make sure the backend is running, then try again.</p>
        <button type="button" className="btn" onClick={onReset}>Back to start</button>
      </section>
    );
  }

  if (timedOut && (!run || !TERMINAL.includes(run.status))) {
    return (
      <section className="card">
        <h2>Comparison timed out</h2>
        <p className="error-text">
          The comparison did not finish within the expected time. This can happen if a provider
          site is very slow or unreachable. No quote was invented.
        </p>
        <button type="button" className="btn" onClick={onReset}>Try again</button>
      </section>
    );
  }

  if (!run) {
    return <section className="card"><h2>Starting comparison…</h2></section>;
  }

  const running = run.status === 'running' || run.status === 'prepared';
  const done = TERMINAL.includes(run.status);
  const comparable = run.comparison?.comparable_quotes || [];
  const lowest = comparable[0]?.annual_premium;
  const failedRun = run.status === 'failed';

  return (
    <section className="card">
      <h2>{done ? 'Comparison results' : 'Comparing quotes…'}</h2>

      {running && (
        <p className="progress-line">
          {run.completed_routes} / {run.total_routes} routes completed
          {run.running_routes > 0 ? ` · ${run.running_routes} still running` : ''}
        </p>
      )}

      {done && !failedRun && <Summary run={run} />}

      {done && (
        <p className="help-note" title="Some brands and aggregators may return the same underlying insurance rate. We count confirmed duplicates only once.">
          ℹ Some brands and aggregators may return the same underlying insurance rate — confirmed
          duplicates are counted only once.
        </p>
      )}

      {done && comparable.length > 0 && (
        <div className="lowest-note">
          Lowest annual premium among comparable quotes:{' '}
          <strong>{money(lowest)}</strong>
        </div>
      )}

      {done && failedRun && (
        <p className="error-text">
          The comparison run failed. This is reported honestly — no quote was fabricated.
        </p>
      )}

      {done && comparable.length === 0 && !failedRun && (
        <div className="no-comparable">
          <strong>No fully comparable quotes were available from this run.</strong>
          <p>
            Estimates, blocked routes, and unavailable routes are listed below — nothing was invented.
          </p>
        </div>
      )}

      <table className="results-table">
        <thead>
          <tr>
            <th>Provider</th>
            <th>Annual premium</th>
            <th>Coverage</th>
            <th>Result type</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {(run.route_summaries || []).map((route) => (
            <tr key={route.registry_id} className={STATUS_CLASS[route.status] || ''}>
              <td>{route.display_name}</td>
              <td>{money(route.annual_premium)}</td>
              <td><Coverage coverageSummary={route.coverage_summary} missingKeys={route.missing_coverage_keys} /></td>
              <td>{route.firm_vs_estimate === 'estimate' ? 'Estimate' : 'Quote'}</td>
              <td>{STATUS_LABELS[route.status] || route.status}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {run.comparison && run.comparison.summary && run.comparison.summary.coverage_mismatch > 0 && (
        <p className="privacy-note">Some quotes were not ranked because their quoted coverage differed from what you requested.</p>
      )}

      {done && (
        <p className="help-note">
          Every route keeps an evidence trail showing what was attempted and why a result
          succeeded or failed.
        </p>
      )}

      <button type="button" className="btn" onClick={onReset}>Start over</button>
    </section>
  );
}
