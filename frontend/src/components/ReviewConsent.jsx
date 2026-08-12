import { useEffect, useState } from 'react';
import { getPlan, routeDisclosure, recordCollectionConsent, grantRouteConsent } from '../api';

const SECTION_LABELS = {
  identity: 'Applicant',
  address: 'Address',
  driver: 'Driver',
  vehicle: 'Vehicle',
  history: 'Insurance history',
  coverage: 'Coverage',
  household: 'Household',
};

/**
 * Review & Consent step.
 *
 * Shows the canonical values the applicant typed (in-memory React state only -
 * never localStorage), a safe provider/route disclosure preview with the
 * canonical paths being shared, and EXPLICIT route-disclosure consent. Clicking
 * Compare Quotes records collection + route consent through the Issue #5 APIs
 * (it never silently grants consent).
 */
export default function ReviewConsent({ sessionId, catalog, values, mode, onBack, onStartCompare }) {
  const [plan, setPlan] = useState(null);
  const [disclosures, setDisclosures] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [collectionConsent, setCollectionConsent] = useState(false);
  const [routeConsent, setRouteConsent] = useState(false);
  // LIVE-only attestations required before a LIVE comparison run is sent. These
  // are separate, explicit applicant attestations (never auto-granted) and are
  // forwarded to the backend as the LiveExecutionGate.
  const [livePersonalUse, setLivePersonalUse] = useState(false);
  const [liveAccurate, setLiveAccurate] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const planData = await getPlan(sessionId, mode);
        if (cancelled) return;
        setPlan(planData);
        const ready = planData.routes.filter(isEligible);
        const disc = {};
        for (const r of ready) {
          try {
            disc[r.registry_id] = await routeDisclosure(sessionId, r.registry_id, mode);
          } catch {
            disc[r.registry_id] = null;
          }
        }
        if (!cancelled) setDisclosures(disc);
      } catch (err) {
        if (!cancelled) setError(err.message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [sessionId, mode]);

  const sections = ['identity', 'address', 'driver', 'vehicle', 'history', 'coverage', 'household']
    .map((group) => ({
      group,
      label: SECTION_LABELS[group] || group,
      fields: catalog
        .filter((f) => f.collection_group === group)
        .sort((a, b) => a.priority - b.priority),
    }))
    .filter((s) => s.fields.some((f) => values[f.canonical_path] !== undefined && values[f.canonical_path] !== ''));

  // A route is compare-eligible when it has an online channel and is either
  // already ready OR blocked only by route-disclosure consent (which the user
  // grants on this very screen). This is deterministic - it never bypasses the
  // explicit consent requirement; it just lets the user grant it here.
  const kindOf = (item) => (item && typeof item === 'object' && item.kind ? item.kind : item);
  const isEligible = (r) => {
    const channels = (r.channels || []).map(kindOf);
    const blockers = (r.blockers || []).map(kindOf);
    if (!channels.includes('online')) return false;
    if (r.is_ready) return true;
    return blockers.length > 0 && blockers.every((b) => b === 'consent_required');
  };
  const eligibleRoutes = (plan?.routes || []).filter(isEligible);
  // Compare requires both consent boxes AND (in live mode) both live
  // attestations. Missing LIVE config never disables MOCK comparison.
  const liveAttested = mode !== 'live' || (livePersonalUse && liveAccurate);
  const canCompare = collectionConsent && routeConsent && liveAttested && !loading && eligibleRoutes.length > 0;

  async function compare() {
    setSubmitting(true);
    try {
      await recordCollectionConsent(sessionId);
      for (const r of eligibleRoutes) {
        await grantRouteConsent(sessionId, r.registry_id, mode);
      }
      let liveGate = null;
      if (mode === 'live') {
        liveGate = {
          personal_use_confirmed: livePersonalUse,
          accurate_information_attested: liveAccurate,
          attested_at: new Date().toISOString(),
        };
      }
      onStartCompare(liveGate);
    } catch (err) {
      setError(err.message);
      setSubmitting(false);
    }
  }

  function formatValue(field, value) {
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    if (typeof value === 'number') {
      if (field.input_type === 'currency') return `$${value.toLocaleString('en-CA')}`;
      return value.toLocaleString('en-CA');
    }
    return String(value);
  }

  // Friendly label lookup: canonical path -> catalog short label, so a blocked
  // provider can show EXACTLY which fields it is missing (never raw values).
  const labelByPath = new Map(
    (catalog || [])
      .filter((f) => f.canonical_path)
      .map((f) => [f.canonical_path, f.short_label || f.field_id]),
  );
  function missingFor(route) {
    const paths = (route.blockers || [])
      .filter((b) => kindOf(b) === 'missing_field' && b.canonical_path)
      .map((b) => b.canonical_path);
    return paths.map((p) => labelByPath.get(p) || p);
  }
  const anyMissing = (plan?.routes || []).some((r) => missingFor(r).length > 0);

  return (
    <section className="card">
      <h2>Review &amp; Consent</h2>
      {loading && <p className="privacy-note">Loading your plan and disclosure preview…</p>}
      {error && <p className="error-text">{error}</p>}

      {!loading && (
        <>
          <h3>Your information</h3>
          {sections.length === 0 && <p className="privacy-note">No fields collected yet.</p>}
          {sections.map((section) => (
            <div key={section.group} className="review-section">
              <h4>{section.label}</h4>
              <ul>
                {section.fields.map((field) => (
                  <li key={field.field_id}>
                    <span className="review-label">{field.short_label}</span>
                    <span className="review-value">{formatValue(field, values[field.canonical_path])}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}

          <h3>Providers &amp; data being shared</h3>
          <p className="privacy-note">
            Below are the route providers ready to receive your data and the canonical fields that
            would be shared with each (paths only - values never leave your control without consent).
          </p>
          {eligibleRoutes.length === 0 && (
            <p className="error-text">
              No providers are ready yet. Check that all required fields above are filled.
            </p>
          )}
          <table className="disclosure-table">
            <thead>
              <tr>
                <th>Provider</th>
                <th>Data being shared</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {(plan?.routes || []).map((r) => {
                const disc = disclosures[r.registry_id];
                return (
                  <tr key={r.registry_id}>
                    <td>
                      <strong>{r.brand_or_program}</strong>
                      {r.is_alternative && <span className="tag tag-alt">duplicate rate source</span>}
                    </td>
                    <td>
                      {disc && disc.items.length > 0 ? (
                        <ul className="path-list">
                          {disc.items.map((item) => (
                            <li key={item.canonical_path}>
                              <code>{item.canonical_path}</code>
                              {item.sensitivity === 'sensitive' && <span className="tag tag-sensitive">sensitive</span>}
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <span className="muted">No data currently present for this route</span>
                      )}
                    </td>
                    <td>
                      {r.is_alternative ? (
                        <span className="status-text">Duplicate rate source - not executed</span>
                      ) : r.is_ready ? (
                        <span className="status-text ok">Ready</span>
                      ) : missingFor(r).length > 0 ? (
                        <span className="status-text">
                          Missing: {missingFor(r).join(', ')}
                        </span>
                      ) : (
                        <span className="status-text">
                          {(r.blockers || []).map(kindOf).join(', ') || 'Not ready'}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <fieldset className="consent-box">
            <legend>Consent</legend>
            <label className="check-row">
              <input
                type="checkbox"
                checked={collectionConsent}
                onChange={(e) => setCollectionConsent(e.target.checked)}
              />
              I consent to collection and storage of my information for this quote journey.
            </label>
            <label className="check-row">
              <input
                type="checkbox"
                checked={routeConsent}
                onChange={(e) => setRouteConsent(e.target.checked)}
              />
              I explicitly consent to sharing the listed data above with the providers shown
              (route-disclosure consent).
            </label>
            {mode === 'live' && (
              <>
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={livePersonalUse}
                    onChange={(e) => setLivePersonalUse(e.target.checked)}
                  />
                  I confirm this comparison is for my own personal insurance shopping
                  (personal-use attestation, required for LIVE routes).
                </label>
                <label className="check-row">
                  <input
                    type="checkbox"
                    checked={liveAccurate}
                    onChange={(e) => setLiveAccurate(e.target.checked)}
                  />
                  I attest that the information I provided is accurate
                  (accuracy attestation, required for LIVE routes).
                </label>
              </>
            )}
          </fieldset>

          <div className="actions">
            <button type="button" className="secondary-btn" onClick={onBack} disabled={submitting}>
              Back
            </button>
            {anyMissing && (
              <button
                type="button"
                className="primary-btn"
                onClick={onBack}
                disabled={submitting}
              >
                Complete required information
              </button>
            )}
            <button
              type="button"
              className="primary-btn"
              onClick={compare}
              disabled={!canCompare || submitting}
            >
              {submitting ? 'Recording consent…' : 'Compare Quotes'}
            </button>
          </div>
        </>
      )}
    </section>
  );
}
