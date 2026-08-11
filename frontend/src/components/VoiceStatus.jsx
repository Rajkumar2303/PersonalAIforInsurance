import { useEffect, useState } from 'react';
import { getVoiceSession } from '../api';

// Issue #9: minimal, read-mostly voice/phone handoff status surface.
// Maps the backend VoiceSession lifecycle / terminal status to plain-language
// text. It never renders applicant values (the backend only ever returns safe
// voice metadata), and it never claims a quote is comparable.

const LIFECYCLE_LABELS = {
  prepared: 'Preparing phone handoff…',
  awaiting_disclosure: 'Automation disclosure pending',
  active: 'Phone call in progress',
  paused_for_applicant: 'Paused — waiting for applicant',
  paused_for_consent: 'Paused — consent required',
  awaiting_human: 'Waiting for applicant or human handoff',
  completed: 'Phone handoff completed',
  terminated: 'Phone handoff terminated',
};

// Prompt 2: the backend exposes a single stable route_status contract.
const ROUTE_STATUS_LABELS = {
  prepared: 'Preparing phone handoff…',
  running: 'Phone call in progress',
  paused_missing_information: 'Paused — missing information',
  applicant_required: 'Waiting for applicant',
  manual_handoff: 'Manual handoff',
  callback_scheduled: 'Callback scheduled',
  quote_pending_normalization: 'Quote received — pending coverage normalization',
  estimate_only: 'Estimate received',
  completed: 'Phone handoff completed',
  failed: 'Phone route failed',
};

const TERMINAL_LABELS = {
  callback_required: 'Callback scheduled',
  estimate_only: 'Estimate received',
  manual_handoff: 'Manual handoff',
  ineligible: 'Ineligible',
  affinity_restricted: 'Affinity restricted',
  specialty_only: 'Specialty only',
  not_currently_writing: 'Not currently writing',
  unreachable: 'Unreachable',
};

const ROUTE_STATUS_CLASS = {
  prepared: 'searching',
  running: 'searching',
  paused_missing_information: 'unresolved',
  applicant_required: 'handoff',
  manual_handoff: 'handoff',
  callback_scheduled: 'callback',
  quote_pending_normalization: 'quote',
  estimate_only: 'estimate',
  completed: 'quote',
  failed: 'blocked',
};

const LIFECYCLE_CLASS = {
  prepared: 'searching',
  awaiting_disclosure: 'searching',
  active: 'searching',
  paused_for_applicant: 'unresolved',
  paused_for_consent: 'unresolved',
  awaiting_human: 'handoff',
  completed: 'quote',
  terminated: 'handoff',
};

/**
 * Polls the backend voice session and renders its lifecycle + terminal status.
 * Passed a voice_session_id (or null to render nothing).
 */
export default function VoiceStatus({ voiceSessionId }) {
  const [session, setSession] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!voiceSessionId) return undefined;
    let cancelled = false;
    let timer;
    async function poll() {
      try {
        const data = await getVoiceSession(voiceSessionId);
        if (cancelled) return;
        setSession(data);
        setError(null);
        if (data.lifecycle_status === 'completed' || data.lifecycle_status === 'terminated') {
          clearInterval(timer);
        }
      } catch (err) {
        if (!cancelled) setError(err.message);
      }
    }
    poll();
    timer = setInterval(poll, 700);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [voiceSessionId]);

  if (!voiceSessionId) return null;

  const lifecycle = session?.lifecycle_status || 'prepared';
  const routeStatus = session?.route_status;
  const terminal = session?.terminal_status;
  // Prefer the backend's stable route_status; fall back to lifecycle.
  const label = ROUTE_STATUS_LABELS[routeStatus] || LIFECYCLE_LABELS[lifecycle] || routeStatus || lifecycle;
  const statusClass = ROUTE_STATUS_CLASS[routeStatus] || LIFECYCLE_CLASS[lifecycle] || '';

  return (
    <section className="card voice-status-card">
      <h2>Phone handoff</h2>
      {error && <p className="error-text">{error}</p>}
      <div className="status-row">
        <span className={`status-dot ${statusClass}`} aria-hidden="true" />
        <span className="status-label">{label}</span>
      </div>
      {terminal && TERMINAL_LABELS[terminal] && (
        <p className="muted">
          <span className={`status-pill ${ROUTE_STATUS_CLASS[routeStatus] || ''}`}>{TERMINAL_LABELS[terminal]}</span>
        </p>
      )}
      <p className="privacy-note">
        Voice layer runs in the backend with safe metadata only. No recording, no LLM, no applicant values here.
      </p>
    </section>
  );
}
