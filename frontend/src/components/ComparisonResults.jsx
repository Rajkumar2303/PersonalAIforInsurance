import { useEffect, useRef, useState } from 'react';
import { getComparisonRun } from '../api';

const STATUS_LABELS = {
  queued: 'Queued',
  running: 'Running…',
  quote_pending_normalization: 'Quote received – normalizing…',
  comparable: 'Comparable',
  non_comparable: 'Not comparable',
  estimate_only: 'Estimate',
  duplicate_rate_source: 'Duplicate rate source',
  captcha_blocked: 'CAPTCHA blocked',
  unavailable: 'Temporarily unavailable',
  callback_required: 'Callback required',
  manual_handoff: 'Manual handoff',
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
 * Issue #13 - comparison run progress + results.
 * Polls GET /comparison-runs/{id} every ~1s. Shows honest per-provider status
 * (CAPTCHA blocked, estimate, duplicate, etc.) and comparable quotes sorted by
 * annual premium - never "best plan".
 */
export default function ComparisonResults({ runId, sessionId, onReset }) {
  const [run, setRun] = useState(null);
  const [error, setError] = useState(null);
  const timerRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const data = await getComparisonRun(runId, sessionId);
        if (cancelled) return;
        setRun(data);
        setError(null);
        const done = ['completed', 'completed_with_partial_results', 'failed'].includes(data.status);
        if (done) clearInterval(timerRef.current);
      } catch (err) {
        if (!cancelled) setError(err.message);
      }
    }
    poll();
    timerRef.current = setInterval(poll, 1000);
    return () => { cancelled = true; clearInterval(timerRef.current); };
  }, [runId, sessionId]);

  if (error) return <section className="card"><p className="error-text">{error}</p></section>;
  if (!run) return <section className="card"><h2>Starting comparison…</h2></section>;

  const running = run.status === 'running' || run.status === 'prepared';
  const done = ['completed', 'completed_with_partial_results', 'failed'].includes(run.status);
  const comparable = run.comparison?.comparable_quotes || [];
  const lowest = comparable[0]?.annual_premium;

  return (
    <section className="card">
      <h2>{done ? 'Comparison results' : 'Comparing quotes…'}</h2>
      {running && (
        <p className="progress-line">
          {run.completed_routes} / {run.total_routes} routes completed
          {run.running_routes > 0 ? ` · ${run.running_routes} still running` : ''}
        </p>
      )}

      {done && <Summary run={run} />}

      {comparable.length > 0 && (
        <div className="lowest-note">
          Lowest annual premium among comparable quotes:{' '}
          <strong>{money(lowest)}</strong>
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

      <button type="button" className="btn" onClick={onReset}>Start over</button>
    </section>
  );
}
