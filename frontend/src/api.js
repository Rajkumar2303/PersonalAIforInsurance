// Issue #8.5 minimal web E2E frontend - backend API client.
// Thin fetch wrapper only. All orchestration lives in the backend; this file
// never duplicates insurance business logic. Values travel only through the
// intake answers API (vault-backed) and stay in React in-memory state - never
// localStorage, never console.log'd.

const JSON_HEADERS = { 'Content-Type': 'application/json' };

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: JSON_HEADERS,
    ...options,
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const data = await response.json();
      if (data && data.detail) detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return response.json();
}

export function createSession(insuranceType) {
  return request('/api/v1/intake/sessions', {
    method: 'POST',
    body: JSON.stringify({ insurance_type: insuranceType }),
  });
}

export function getCatalog(product = 'auto') {
  return request(`/api/v1/intake/catalog?product=${product}`);
}

export function submitAnswer(sessionId, canonicalPath, value) {
  return request(`/api/v1/intake/sessions/${sessionId}/answers`, {
    method: 'POST',
    body: JSON.stringify({ canonical_path: canonicalPath, value }),
  });
}

export function profileSummary(sessionId) {
  return request(`/api/v1/intake/sessions/${sessionId}/profile-summary`);
}

export function recordCollectionConsent(sessionId) {
  return request(`/api/v1/intake/sessions/${sessionId}/consent`, {
    method: 'POST',
    body: JSON.stringify({ scope: 'collection' }),
  });
}

export function getPlan(sessionId, mode = 'mock') {
  return request(`/api/v1/planner/plan?session_id=${sessionId}&mode=${mode}`);
}

export function routeDisclosure(sessionId, registryId, mode = 'mock') {
  return request(`/api/v1/intake/sessions/${sessionId}/route-disclosure?mode=${mode}`, {
    method: 'POST',
    body: JSON.stringify({ registry_id: registryId }),
  });
}

export function grantRouteConsent(sessionId, registryId, mode = 'mock') {
  return request(`/api/v1/intake/sessions/${sessionId}/consent/route?mode=${mode}`, {
    method: 'POST',
    body: JSON.stringify({ registry_id: registryId, paths: [], granted: true }),
  });
}

export function getDemoPersona() {
  return request('/api/v1/demo/personas/standard-auto?mode=mock');
}

export function startCompare(sessionId, mode = 'mock') {
  return request('/api/v1/orchestrate/compare', {
    method: 'POST',
    body: JSON.stringify({ intake_session_id: sessionId, execution_mode: mode }),
  });
}

export function getJob(jobId) {
  return request(`/api/v1/orchestrate/jobs/${jobId}`);
}

// --- Issue #9: voice / phone handoff status surface ---------------------
// Minimal, read-mostly client for the voice layer. No recording, no LLM, no
// real calls - the backend drives a provider-agnostic phone/voice handoff.

export function prepareVoiceHandoff(payload) {
  return request('/api/v1/voice/handoffs', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function getVoiceSession(voiceSessionId) {
  return request(`/api/v1/voice/sessions/${voiceSessionId}`);
}

export function discloseVoiceSession(voiceSessionId, granted = true) {
  return request(`/api/v1/voice/sessions/${voiceSessionId}/disclosure`, {
    method: 'POST',
    body: JSON.stringify({ granted }),
  });
}

export function sendVoiceEvent(voiceSessionId, brokerQuestion) {
  return request(`/api/v1/voice/sessions/${voiceSessionId}/events`, {
    method: 'POST',
    body: JSON.stringify(brokerQuestion),
  });
}

export function resumeVoiceSession(voiceSessionId) {
  return request(`/api/v1/voice/sessions/${voiceSessionId}/resume`, {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

export function voiceObservation(voiceSessionId, payload) {
  return request(`/api/v1/voice/sessions/${voiceSessionId}/observations`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}
