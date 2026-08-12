import { useRef, useState } from 'react';
import ProductSelect from './components/ProductSelect.jsx';
import IntakeForm from './components/IntakeForm.jsx';
import ReviewConsent from './components/ReviewConsent.jsx';
import ComparisonResults from './components/ComparisonResults.jsx';
import VoiceStatus from './components/VoiceStatus.jsx';
import { createSession, getCatalog, startComparisonRun } from './api';

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
  const [mode, setMode] = useState('mock');
  const [voiceSessionId, setVoiceSessionId] = useState(null);
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

  async function onStartCompare() {
    if (startingRef.current) return;
    startingRef.current = true;
    try {
      const run = await startComparisonRun(sessionId, mode);
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
          {mode === 'live' && <span className="mode-live-note">Not configured - no verified live route</span>}
        </div>
      </header>

      <main className="app-main">
        {step === 'product' && (
          <ProductSelect onSelect={onSelectProduct} />
        )}

        {step === 'form' && (
          <IntakeForm sessionId={sessionId} catalog={catalog} onComplete={onFormComplete} />
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

