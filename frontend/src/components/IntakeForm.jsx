import { useState } from 'react';
import { getDemoPersona, submitAnswer } from '../api';

const SECTION_ORDER = ['identity', 'address', 'driver', 'vehicle', 'history', 'coverage', 'household'];
const SECTION_LABELS = {
  identity: 'Applicant',
  address: 'Address',
  driver: 'Driver',
  vehicle: 'Vehicle',
  history: 'Insurance history',
  coverage: 'Coverage',
  household: 'Household',
};

function isBlank(value) {
  if (value === undefined || value === null) return true;
  if (typeof value === 'string') return value.trim() === '';
  if (typeof value === 'number') return Number.isNaN(value);
  return false;
}

/**
 * Catalog-driven intake form. Renders whatever the backend catalog returns -
 * no hardcoded insurance schema in React. Submits answers in the engine-correct
 * order (seeds -> unit fields -> remaining), skipping blank values.
 *
 * ``initialValues`` rehydrates the form when the user navigates back from
 * Review & Consent (App keeps the entered values in React in-memory state -
 * never localStorage). On re-mount the local state starts from those values so
 * nothing entered is lost; editing then resubmitting refreshes the review.
 */
export default function IntakeForm({ sessionId, catalog, initialValues = {}, onComplete }) {
  const [values, setValues] = useState(initialValues || {});
  const [errors, setErrors] = useState({});
  const [filling, setFilling] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const grouped = SECTION_ORDER.map((group) => ({
    group,
    label: SECTION_LABELS[group] || group,
    fields: catalog
      .filter((f) => f.collection_group === group)
      .sort((a, b) => a.priority - b.priority || a.field_id.localeCompare(b.field_id)),
  })).filter((section) => section.fields.length > 0);

  async function fillDemoProfile() {
    setFilling(true);
    setErrors({});
    try {
      const persona = await getDemoPersona();
      setValues((prev) => ({ ...prev, ...persona }));
    } catch (err) {
      setErrors({ _global: err.message });
    } finally {
      setFilling(false);
    }
  }

  function setValue(path, value) {
    setValues((prev) => ({ ...prev, [path]: value }));
    setErrors((prev) => {
      const next = { ...prev };
      delete next[path];
      return next;
    });
  }

  function renderControl(field) {
    const path = field.canonical_path;
    const value = values[path];
    switch (field.input_type) {
      case 'integer':
      case 'years':
        return (
          <input
            type="number"
            step="1"
            value={value === undefined ? '' : value}
            onChange={(e) => {
              const raw = e.target.value;
              setValue(path, raw === '' ? undefined : Number.parseInt(raw, 10));
            }}
          />
        );
      case 'float':
      case 'currency':
        if (field.choices && field.choices.length > 0) {
          return (
            <select
              value={value === undefined ? '' : String(value)}
              onChange={(e) => setValue(path, e.target.value === '' ? undefined : Number(e.target.value))}
            >
              <option value="">Select…</option>
              {field.choices.map((choice) => (
                <option key={choice} value={choice}>
                  {Number(choice).toLocaleString('en-CA')}
                </option>
              ))}
            </select>
          );
        }
        return (
          <input
            type="number"
            step="any"
            value={value === undefined ? '' : value}
            onChange={(e) => {
              const raw = e.target.value;
              setValue(path, raw === '' ? undefined : Number(raw));
            }}
          />
        );
      case 'date':
        return (
          <input
            type="date"
            value={value === undefined ? '' : value}
            onChange={(e) => setValue(path, e.target.value || undefined)}
          />
        );
      case 'boolean':
        return (
          <select value={value === undefined ? '' : String(value)} onChange={(e) => setValue(path, e.target.value === '' ? undefined : e.target.value === 'true')}>
            <option value="">Select…</option>
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        );
      case 'single_select':
        return (
          <select value={value === undefined ? '' : String(value)} onChange={(e) => setValue(path, e.target.value || undefined)}>
            <option value="">Select…</option>
            {field.choices.map((choice) => (
              <option key={choice} value={choice}>
                {choice}
              </option>
            ))}
          </select>
        );
      case 'multi_select': {
        const selected = Array.isArray(value) ? value : [];
        return (
          <div className="multi-select">
            {field.choices.map((choice) => (
              <label key={choice} className="check-row">
                <input
                  type="checkbox"
                  checked={selected.includes(choice)}
                  onChange={(e) => {
                    const next = e.target.checked
                      ? [...selected, choice]
                      : selected.filter((c) => c !== choice);
                    setValue(path, next.length ? next : undefined);
                  }}
                />
                {choice}
              </label>
            ))}
          </div>
        );
      }
      default:
        return (
          <input
            type="text"
            value={value === undefined ? '' : value}
            placeholder={field.input_type === 'licence' ? 'A1234-56789-01234' : undefined}
            onChange={(e) => setValue(path, e.target.value)}
          />
        );
    }
  }

  async function submitAll() {
    setSubmitting(true);
    setErrors({});
    const nextErrors = {};
    // Engine-correct order: seeds -> unit fields -> remaining fields. Each
    // group is sorted by priority, but the GROUPS are never re-sorted together
    // (seeds must complete before the profile can be materialized).
    const byGroup = [
      catalog.filter((f) => f.seed_required),
      catalog.filter((f) => f.item_unit && f.item_unit_required),
      catalog.filter((f) => !f.seed_required && !(f.item_unit && f.item_unit_required) && !f.household_attestation_required),
    ];
    const ordered = byGroup.flat().map((f) => f);
    const seedIds = new Set(catalog.filter((f) => f.seed_required).map((f) => f.field_id));
    const unitIds = new Set(
      catalog.filter((f) => f.item_unit && f.item_unit_required).map((f) => f.field_id),
    );
    ordered.sort((a, b) => {
      const ga = seedIds.has(a.field_id) ? 0 : unitIds.has(a.field_id) ? 1 : 2;
      const gb = seedIds.has(b.field_id) ? 0 : unitIds.has(b.field_id) ? 1 : 2;
      if (ga !== gb) return ga - gb;
      return a.priority - b.priority || a.field_id.localeCompare(b.field_id);
    });

    for (const field of ordered) {
      const value = values[field.canonical_path];
      if (isBlank(value)) continue; // leave blank fields for the route planner to flag
      try {
        const result = await submitAnswer(sessionId, field.canonical_path, value);
        if (!result.validation_success) {
          nextErrors[field.canonical_path] = result.error_message || 'Invalid value';
        }
      } catch (err) {
        nextErrors[field.canonical_path] = err.message;
      }
    }

    if (Object.keys(nextErrors).length > 0) {
      setErrors(nextErrors);
      setSubmitting(false);
      return;
    }
    setSubmitting(false);
    onComplete(values);
  }

  return (
    <section className="card">
      <div className="card-head">
        <h2>Tell us about your auto insurance</h2>
        <button type="button" className="secondary-btn" onClick={fillDemoProfile} disabled={filling}>
          {filling ? 'Loading…' : 'Fill demo profile'}
        </button>
      </div>
      <p className="privacy-note">
        Your answers stay in your browser memory for this session and are stored by the backend
        profile vault. Nothing is written to localStorage or browser storage.
      </p>

      {grouped.map((section) => (
        <fieldset key={section.group} className="section">
          <legend>{section.label}</legend>
          <div className="field-grid">
            {section.fields.map((field) => (
              <label key={field.field_id} className={`field ${field.sensitivity === 'sensitive' ? 'sensitive' : ''}`}>
                <span className="field-label">
                  {field.short_label}
                  {field.sensitivity === 'sensitive' && <em className="sensitive-tag">sensitive</em>}
                </span>
                {renderControl(field)}
                {field.help_text && <span className="help-text">{field.help_text}</span>}
                {field.household_attestation_required && (
                  <span className="help-text">Requires a household-driver consent attestation.</span>
                )}
                {errors[field.canonical_path] && (
                  <span className="error-text">{errors[field.canonical_path]}</span>
                )}
              </label>
            ))}
          </div>
        </fieldset>
      ))}

      {errors._global && <p className="error-text">{errors._global}</p>}

      <div className="actions">
        <button type="button" className="primary-btn" onClick={submitAll} disabled={submitting}>
          {submitting ? 'Submitting…' : 'Continue'}
        </button>
      </div>
    </section>
  );
}
