import { useEffect, useRef, useState } from 'react';
import {
  getPlan,
  startBrowserSession,
  runBrowserSession,
  resumeBrowserSession,
  approveBrowserCheckpoint,
  closeBrowserSession,
  getBrowserSessionQuote,
} from '../api';

/**
 * Sonnet LIVE operator (direct browser session).
 *
 * Drives ONE provider (Sonnet) through the existing direct browser-session
 * endpoints so Chromium stays open during pauses. Deliberately NOT the
 * comparison-run path and NOT generalized multi-provider orchestration.
 *
 * Safety:
 * - only SAFE execution identifiers are stored in React state: browser_session_id,
 *   attempt_id, provider, status, checkpoint type, missing canonical field.
 * - applicant values never enter URLs, logs, localStorage, or rendered metadata.
 * - approval control is shown ONLY for identity_lookup (resumable). It is never
 *   shown for application declaration / CAPTCHA / signature / payment / purchase /
 *   binding - those are terminal stops (the browser is closed).
 * - on an explicit quote result the browser is closed immediately after the
 *   quote is saved; the run never proceeds toward purchase.
 */

const RESUMABLE_PAUSES = new Set([
  'paused_needs_field',
  'paused_unknown_field',
  'paused_needs_consent',
  'paused_ambiguous',
  'paused_value_not_supported',
  'paused_validation_error',
]);

function labelFor(status, checkpointType) {
  switch (status) {
    case 'created':
      return 'Starting';
    case 'running':
      return 'Filling information';
    case 'paused_human_checkpoint':
      return checkpointType === 'identity_lookup'
        ? 'Waiting for identity approval'
        : 'Waiting for information';
    case 'succeeded':
      return 'Quote retrieved';
    case 'stopped_access_control':
      return 'Blocked';
    case 'stopped_prohibited':
    case 'stopped_human_checkpoint':
    case 'stopped_unexpected_host':
    case 'failed':
      return 'Stopped';
    default:
      return RESUMABLE_PAUSES.has(status) ? 'Waiting for information' : status;
  }
}

function formatMoney(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? `$${n.toLocaleString('en-CA', { minimumFractionDigits: 2 })}` : null;
}

function panelStyle() {
  return {
    border: '1px solid #c9a227',
    borderRadius: '8px',
    padding: '12px 16px',
    margin: '12px 0',
    background: '#fffdf2',
    fontFamily: 'inherit',
  };
}

export default function SonnetLiveOperator({ sessionId, liveGate, mode, onReset }) {
  const [ready, setReady] = useState(null); // null | true | false
  const [readyReason, setReadyReason] = useState(null);
  const [browserSessionId, setBrowserSessionId] = useState(null);
  const [attemptId, setAttemptId] = useState(null);
  const [status, setStatus] = useState(null);
  const [checkpointType, setCheckpointType] = useState(null);
  const [missingFields, setMissingFields] = useState([]);
  const [quote, setQuote] = useState(null);
  const [message, setMessage] = useState(null);
  const [busy, setBusy] = useState(false);
  const startedRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const plan = await getPlan(sessionId, mode);
        if (cancelled) return;
        const sonnet = (plan.routes || []).find((r) => r.registry_id === 'sonnet');
        const isReady = Boolean(sonnet && sonnet.is_ready);
        setReady(isReady);
        if (!isReady) {
          setReadyReason((sonnet?.blockers || []).map((b) => (b && b.kind) || b).join(', ') || 'not ready');
        }
      } catch (err) {
        if (!cancelled) {
          setReady(false);
          setReadyReason(err.message);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId, mode]);

  const liveAttested = mode === 'live' && !!liveGate && liveGate.personal_use_confirmed && liveGate.accurate_information_attested;
  const canStart = mode === 'live' && liveAttested && ready === true && !busy;

  function applyRunResult(res) {
    const sess = res.session || {};
    setBrowserSessionId(sess.browser_session_id);
    setAttemptId(sess.attempt_id);
    setStatus(sess.status);
    const step = res.step;
    const obs = step && step.observation ? step.observation : null;
    if (obs) {
      setCheckpointType(obs.checkpoint ? obs.checkpoint.checkpoint_type : null);
      setMissingFields(obs.missing_field_paths || obs.pending_field_paths || []);
      if (obs.quote && obs.quote.quote_present) {
        setQuote({ raw: obs.quote, saved: false });
      }
    }
  }

  async function startAndRun() {
    setBusy(true);
    setMessage(null);
    try {
      const started = await startBrowserSession({
        intake_session_id: sessionId,
        planned_route_id: 'sonnet',
        execution_mode: 'live',
        live_gate: liveGate,
      });
      if (!started.started) {
        const ref = started.refusal || {};
        setReady(false);
        setReadyReason(ref.detail || ref.reason || 'route refused');
        return;
      }
      setReady(true);
      applyRunResult({ session: started.session, step: null });
      startedRef.current = true;
      const res = await runBrowserSession(started.session.browser_session_id);
      applyRunResult(res);
      if (res.step && res.step.observation && res.step.observation.quote && res.step.observation.quote.quote_present) {
        await fetchQuote(started.session.browser_session_id);
      }
    } catch (err) {
      setMessage(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function fetchQuote(id) {
    try {
      const normalized = await getBrowserSessionQuote(id);
      setQuote((prev) => ({ ...prev, normalized, saved: true }));
      // Stop and close Chromium immediately after saving the quote; never
      // proceed toward purchase.
      await closeBrowserSession(id).catch(() => {});
      setStatus('succeeded');
    } catch (err) {
      setMessage(err.message);
    }
  }

  async function resume() {
    if (!browserSessionId) return;
    setBusy(true);
    setMessage(null);
    try {
      const res = await resumeBrowserSession(browserSessionId);
      applyRunResult(res);
      if (res.step && res.step.observation && res.step.observation.quote && res.step.observation.quote.quote_present) {
        await fetchQuote(browserSessionId);
      }
    } catch (err) {
      setMessage(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function approveAndContinue() {
    if (!browserSessionId || !checkpointType) return;
    setBusy(true);
    setMessage(null);
    try {
      await approveBrowserCheckpoint(browserSessionId, checkpointType);
      const res = await resumeBrowserSession(browserSessionId);
      applyRunResult(res);
      if (res.step && res.step.observation && res.step.observation.quote && res.step.observation.quote.quote_present) {
        await fetchQuote(browserSessionId);
      }
    } catch (err) {
      setMessage(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function stopAndClose() {
    setBusy(true);
    setMessage(null);
    if (browserSessionId) {
      try {
        await closeBrowserSession(browserSessionId);
      } catch {
        /* best-effort close */
      }
    }
    setStatus('closed');
    setCheckpointType(null);
    setQuote(null);
    setBrowserSessionId(null);
    setAttemptId(null);
    setBusy(false);
  }

  const terminalStop = ['stopped_access_control', 'stopped_prohibited', 'stopped_human_checkpoint', 'stopped_unexpected_host', 'failed'].includes(status);
  const waitingInfo = RESUMABLE_PAUSES.has(status);
  const awaitingIdentity = status === 'paused_human_checkpoint' && checkpointType === 'identity_lookup';
  const quoteDone = status === 'succeeded' && quote && quote.saved;

  return (
    <section style={panelStyle()}>
      <h3 style={{ margin: '0 0 8px' }}>Sonnet Live Operator</h3>
      <p style={{ margin: '0 0 8px', fontSize: '13px', color: '#555' }}>
        Directly drives a single live Sonnet browser session (Chromium stays open during pauses).
        Never proceeds toward purchase. {mode !== 'live' && <strong>LIVE mode only.</strong>}
      </p>

      {ready === false && (
        <p style={{ color: '#a33' }}>Sonnet route not ready: {readyReason || 'unknown'}</p>
      )}
      {mode !== 'live' && <p style={{ color: '#a33' }}>Run Sonnet Live requires LIVE mode (VITE_APP_MODE=live).</p>}
      {mode === 'live' && !liveAttested && ready === true && (
        <p style={{ color: '#a33' }}>Live attestations (personal use + accurate information) are required first.</p>
      )}

      {!browserSessionId && (
        <button type="button" onClick={startAndRun} disabled={!canStart}>
          {busy ? 'Starting…' : 'Run Sonnet Live'}
        </button>
      )}

      {browserSessionId && (
        <div>
          <p style={{ fontSize: '12px', color: '#666', margin: '4px 0' }}>
            provider=sonnet · browser_session_id={browserSessionId} · attempt_id={attemptId || '—'}
          </p>
          <p style={{ margin: '4px 0', fontWeight: 600 }}>Status: {labelFor(status, checkpointType)}</p>

          {waitingInfo && (
            <div>
              <p style={{ margin: '4px 0' }}>
                Complete the current question accurately in the Sonnet browser, then select Resume.
              </p>
              {missingFields.length > 0 && (
                <p style={{ margin: '4px 0', fontSize: '12px', color: '#555' }}>
                  Waiting on canonical fields: {missingFields.join(', ')}
                </p>
              )}
              <button type="button" onClick={resume} disabled={busy}>Resume</button>
            </div>
          )}

          {awaitingIdentity && (
            <div>
              <p style={{ margin: '4px 0' }}>
                Approval: the agent filled the fields on this screen and wants to submit them /
                trigger an identity or database lookup. No values are shown.
              </p>
              <button type="button" onClick={approveAndContinue} disabled={busy} style={{ marginRight: 8 }}>
                Approve and continue
              </button>
              <button type="button" onClick={stopAndClose} disabled={busy}>Stop</button>
            </div>
          )}

          {status === 'paused_human_checkpoint' && !awaitingIdentity && (
            <p style={{ margin: '4px 0', color: '#a33' }}>
              This checkpoint ({checkpointType}) cannot be approved automatically. Resolve it manually or stop.
            </p>
          )}

          {terminalStop && (
            <p style={{ margin: '4px 0', color: '#a33' }}>
              {status === 'stopped_access_control' ? 'Blocked (CAPTCHA / access restriction).' : `Stopped (${status}).`} Closing the browser.
            </p>
          )}

          {quoteDone && (
            <div style={{ margin: '8px 0' }}>
              <p style={{ fontWeight: 700, margin: '4px 0' }}>LIVE — Sonnet</p>
              {quote.normalized && quote.normalized.premium ? (
                <ul style={{ margin: '4px 0', paddingLeft: 18, fontSize: '13px' }}>
                  <li>Premium: {formatMoney(quote.normalized.premium.normalized_annual_amount) || '—'} per year ({quote.normalized.premium.provider_presented_frequency || 'annual'})</li>
                  {(quote.normalized.coverage_ledger?.items || []).map((item, i) => (
                    <li key={i}>
                      {item.item_key}: {item.state}
                      {item.value && Object.keys(item.value).length > 0 ? ` (${JSON.stringify(item.value)})` : ''}
                    </li>
                  ))}
                  <li>Quote reference: {quote.normalized.normalized_quote_id.slice(0, 8)}… (redacted)</li>
                  <li>Timestamp: {new Date(quote.normalized.normalized_at).toLocaleString()}</li>
                </ul>
              ) : quote.raw ? (
                <p style={{ margin: '4px 0', fontSize: '13px' }}>
                  Annual premium: {formatMoney(quote.raw.annual_amount_parsed)} CAD (explicitly returned)
                </p>
              ) : null}
              <p style={{ margin: '4px 0', fontSize: '12px', color: '#555' }}>
                Quote saved; Chromium closed. The run does not proceed toward purchase.
              </p>
            </div>
          )}

          {message && <p style={{ color: '#a33', margin: '4px 0' }}>{message}</p>}

          <div style={{ marginTop: 8 }}>
            <button type="button" onClick={stopAndClose} disabled={busy} style={{ marginRight: 8 }}>
              Stop Sonnet Run
            </button>
            <button type="button" onClick={onReset}>Reset</button>
          </div>
        </div>
      )}
    </section>
  );
}
