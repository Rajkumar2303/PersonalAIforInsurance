import { Fragment, useEffect, useRef, useState } from 'react';
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
  not_ready: 'not-ready',
  consent_required: 'blocked',
  ineligible: 'blocked',
  not_currently_writing: 'blocked',
  affinity_restricted: 'blocked',
  specialty_only: 'blocked',
  unresolved: 'callback',
  failed: 'blocked',
};

// Pill variant used for the status cell (reuses the existing .status-pill CSS).
const STATUS_PILL_CLASS = {
  comparable: 'quote',
  non_comparable: 'blocked',
  estimate_only: 'estimate',
  duplicate_rate_source: 'duplicate',
  captcha_blocked: 'blocked',
  unavailable: 'blocked',
  needs_additional_information: 'callback',
  callback_required: 'callback',
  manual_handoff: 'handoff',
  not_ready: 'searching',
  consent_required: 'blocked',
  ineligible: 'blocked',
  not_currently_writing: 'blocked',
  affinity_restricted: 'blocked',
  specialty_only: 'blocked',
  unresolved: 'callback',
  failed: 'blocked',
};

// Statuses that actually produced a quote/estimate. Anything else (callback,
// blocked, not-ready, …) returned NO quote, so the result-type cell must not
// claim one - it shows an em dash instead.
const QUOTE_RESULT_STATUSES = new Set(['comparable', 'non_comparable', 'estimate_only', 'duplicate_rate_source']);

function resultTypeLabel(route) {
  if (!QUOTE_RESULT_STATUSES.has(route.status)) return '—';
  return route.status === 'estimate_only' || route.firm_vs_estimate === 'estimate'
    ? 'Estimate'
    : 'Quote';
}

function money(value) {
  if (value === null || value === undefined) return '—';
  return `$${Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}/year`;
}

// Status -> safe "what happened" labels for the redacted evidence panel.
// Derived from the route's status, never from provider-specific values, so the
// panel stays generic and shows no applicant or raw evidence data.
const EVIDENCE_EVENT_LABELS = {
  callback_required: 'Callback barrier detected',
  captcha_blocked: 'Access-control / CAPTCHA barrier detected',
  blocked: 'Access barrier detected',
  unavailable: 'Route unavailable during attempt',
  manual_handoff: 'Human handoff required',
  ineligible: 'Ineligible - no quote returned',
  not_currently_writing: 'Not currently writing',
  affinity_restricted: 'Affinity restricted',
  specialty_only: 'Specialty only',
  unresolved: 'Outcome unresolved',
  failed: 'Attempt failed',
};

const EVIDENCE_ACTION_LABELS = {
  callback_required: 'Prepare human/voice handoff',
  captcha_blocked: 'Stop - barrier is never bypassed',
  blocked: 'Stop - barrier is never bypassed',
  unavailable: 'Participant may retry later',
  manual_handoff: 'Prepare manual follow-up with a licensed representative',
  unresolved: 'Provider-specific research or permitted manual completion',
};

function EvidencePanel({ route }) {
  const quoteReturned = (route.quote_count || 0) > 0 ? 'Yes' : 'No';
  return (
    <div className="evidence-panel">
      <h4>Redacted evidence</h4>
      <dl className="evidence-grid">
        <div><dt>Provider</dt><dd>{route.display_name}</dd></div>
        <div><dt>Status</dt><dd>{STATUS_LABELS[route.status] || route.status}</dd></div>
        <div><dt>Observed at</dt><dd>{route.evidence_observed_at || '—'}</dd></div>
        <div><dt>Source</dt><dd>{route.safe_source_url || '—'}</dd></div>
        <div><dt>Event</dt><dd>{EVIDENCE_EVENT_LABELS[route.status] || 'Evidence recorded'}</dd></div>
        <div><dt>Action</dt><dd>{EVIDENCE_ACTION_LABELS[route.status] || 'See evidence record'}</dd></div>
        <div><dt>Quote returned</dt><dd>{quoteReturned}</dd></div>
        <div><dt>Evidence ID</dt><dd className="mono">{route.evidence_id || '—'}</dd></div>
        <div><dt>Content hash</dt><dd className="mono">{route.evidence_content_hash || '—'}</dd></div>
      </dl>
      <p className="privacy-note">
        Redacted safe metadata only - no applicant information, request payloads, cookies,
        tokens, or browser storage is shown.
      </p>
    </div>
  );
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
  const [openEvidence, setOpenEvidence] = useState(null); // registry_id of the open panel
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
          {(run.route_summaries || []).map((route) => {
            const hasEvidence = route.evidence_status === 'recorded' && !!route.evidence_id;
            const expanded = openEvidence === route.registry_id;
            return (
              <Fragment key={route.registry_id}>
                <tr className={STATUS_CLASS[route.status] || ''}>
                  <td className="provider-cell">{route.display_name}</td>
                  <td>{money(route.annual_premium)}</td>
                  <td><Coverage coverageSummary={route.coverage_summary} missingKeys={route.missing_coverage_keys} /></td>
                  <td>{resultTypeLabel(route)}</td>
                  <td>
                    <span className={`status-pill ${STATUS_PILL_CLASS[route.status] || 'searching'}`}>
                      {STATUS_LABELS[route.status] || route.status}
                    </span>
                    {hasEvidence && (
                      <button
                        type="button"
                        className="evidence-toggle"
                        onClick={() => setOpenEvidence(expanded ? null : route.registry_id)}
                      >
                        {expanded ? 'Hide evidence' : 'View evidence'}
                      </button>
                    )}
                  </td>
                </tr>
                {expanded && hasEvidence && (
                  <tr className="evidence-row">
                    <td colSpan={5}>
                      <EvidencePanel route={route} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
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
