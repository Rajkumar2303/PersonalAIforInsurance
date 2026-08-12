import { useEffect, useRef, useState } from 'react';
import ProductSelect from './components/ProductSelect.jsx';
import IntakeForm from './components/IntakeForm.jsx';
import ReviewConsent from './components/ReviewConsent.jsx';
import ComparisonResults from './components/ComparisonResults.jsx';
import VoiceStatus from './components/VoiceStatus.jsx';
import { createSession, getCatalog, getMarkets, startComparisonRun } from './api';

/**
 * Issue #13 - comparison run wizard (product -> intake -> review/consent ->
 * Compare Quotes -> polled comparison run). Mock mode (default) uses the
 * isolated demo overlay + local mock site. Live is explicit and gated.
 */
export default function App() {
  const [step, setStep] = useState('product'); // product|form|review|comparing
  const [sessionId, setSessionId] = useState(null);
  const [catalog, setCatalog] = useState([]);
  const [values, setValues] = useState({});
  const [runId, setRunId] = useState(null);
  // Execution mode is controlled per-environment via VITE_APP_MODE (mock|live).
  // Defaults to 'mock' when unset so existing behavior is unchanged. LIVE is
  // still fully gated on the backend (personal-use + attestation + route
  // consent + verified route) - this only selects which mode the API receives.
  const [mode, setMode] = useState(import.meta.env.VITE_APP_MODE === 'live' ? 'live' : 'mock');
  // Live banner is DATA-DRIVEN from the market registry (verified routes), so
  // it never shows a stale "not configured" claim once a route is verified.
  // loading | configured | unconfigured | unknown
  const [liveEnv, setLiveEnv] = useState('loading');
  const [liveRouteNames, setLiveRouteNames] = useState([]);
  const [voiceSessionId, setVoiceSessionId] = useState(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const markets = await getMarkets('auto');
        if (cancelled) return;
        const verified = (markets || []).filter(
          (m) => m.status === 'verified' && !!m.quote_url
        );
        setLiveRouteNames(verified.map((m) => m.display_name || m.registry_id));
        setLiveEnv(verified.length > 0 ? 'configured' : 'unconfigured');
      } catch {
        if (cancelled) return;
        setLiveEnv('unknown');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Issue #14: guard so a double-click / repeated start never fires two
  // comparison-run requests (the backend is also idempotent per intake).
  const startingRef = useRef(false);

  async function onSelectProduct(productKey) {
    if (productKey !== 'auto') return; // gate handled in the component
    const { session } = await createSession('auto');
    const fields = await getCatalog('auto');
    setSessionId(session.session_id);
    setCatalog(fields);
    setValues({});
    setStep('form');
  }

  function onFormComplete(completedValues) {
    setValues(completedValues);
    setStep('review');
  }

  async function onStartCompare(liveGate = null) {
    if (startingRef.current) return;
    startingRef.current = true;
    try {
      const run = await startComparisonRun(sessionId, mode, liveGate);
      setRunId(run.comparison_run_id);
      setStep('comparing');
    } finally {
      startingRef.current = false;
    }
  }

  function onReset() {
    setStep('product');
    setSessionId(null);
    setCatalog([]);
    setValues({});
    setRunId(null);
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>Ontario All-Quote Agent</h1>
        <p className="tagline">Evidence-first Ontario auto-insurance shopping assistant</p>
        <div className="mode-bar">
          <span className={`mode-badge ${mode === 'mock' ? 'mock' : 'live'}`}>
            {mode === 'mock' ? 'Mock mode (local synthetic)' : 'Live mode'}
          </span>
          {mode === 'live' && (
            <span className="mode-live-note">
              {liveEnv === 'loading' && 'Checking live configuration…'}
              {liveEnv === 'configured' && `Verified live route: ${liveRouteNames.join(', ')}`}
              {liveEnv === 'unconfigured' && 'Not configured - no verified live route'}
              {liveEnv === 'unknown' && 'Live configuration unavailable'}
            </span>
          )}
        </div>
      </header>

      <main className="app-main">
        {step === 'product' && (
          <ProductSelect onSelect={onSelectProduct} />
        )}

        {step === 'form' && (
          <IntakeForm
            sessionId={sessionId}
            catalog={catalog}
            initialValues={values}
            onComplete={onFormComplete}
          />
        )}

        {step === 'review' && (
          <ReviewConsent
            sessionId={sessionId}
            catalog={catalog}
            values={values}
            mode={mode}
            onBack={() => setStep('form')}
            onStartCompare={onStartCompare}
          />
        )}

        {step === 'comparing' && (
          <>
            <VoiceStatus voiceSessionId={voiceSessionId} />
            <ComparisonResults runId={runId} sessionId={sessionId} onReset={onReset} />
          </>
        )}
      </main>

      <footer className="app-footer">
        <p>
          Evidence-first comparison — mock is the default; LIVE stays explicit and gated.
          Comparable firm quotes sort by annual premium (never “best plan”).
        </p>
      </footer>
    </div>
  );
}

