import { useState } from 'react';
import ProductSelect from './components/ProductSelect.jsx';
import IntakeForm from './components/IntakeForm.jsx';
import ReviewConsent from './components/ReviewConsent.jsx';
import CompareProgress from './components/CompareProgress.jsx';
import { createSession, getCatalog, startCompare } from './api';

/**
 * Issue #8.5 - minimal web E2E wizard (integration checkpoint, NOT the #13
 * dashboard).
 *
 * product -> catalog-driven intake form -> review & explicit consent ->
 * backend orchestration (#6 planner -> #7 browser -> #8 recovery) -> polling.
 *
 * Mock mode (default) uses the isolated demo overlay + local mock site. Live
 * is explicit and requires verified routes (none configured yet, so the UI
 * keeps it clearly not-configured). No Issue #9-#13 functionality.
 */
export default function App() {
  const [step, setStep] = useState('product'); // product|form|review|comparing
  const [sessionId, setSessionId] = useState(null);
  const [catalog, setCatalog] = useState([]);
  const [values, setValues] = useState({});
  const [jobId, setJobId] = useState(null);
  const [mode, setMode] = useState('mock');

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
    const job = await startCompare(sessionId, mode);
    setJobId(job.job_id);
    setStep('comparing');
  }

  function onReset() {
    setStep('product');
    setSessionId(null);
    setCatalog([]);
    setValues({});
    setJobId(null);
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

        {step === 'comparing' && <CompareProgress jobId={jobId} onReset={onReset} />}
      </main>

      <footer className="app-footer">
        <p>
          Integration checkpoint — Issues #1–#8 only. Quotes are raw observations pending coverage
          normalization (#11/#12 not implemented). Mock is the default; LIVE stays explicit and gated.
        </p>
      </footer>
    </div>
  );
}

