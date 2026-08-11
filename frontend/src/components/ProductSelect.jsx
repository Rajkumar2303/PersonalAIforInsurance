import { useState } from 'react';

const PRODUCTS = [
  {
    key: 'auto',
    label: 'Auto Insurance',
    description: 'Ontario private-passenger automobile insurance',
    supported: true,
  },
  { key: 'home', label: 'Home Insurance', supported: false },
  { key: 'tenant', label: 'Tenant Insurance', supported: false },
  { key: 'life', label: 'Life Insurance', supported: false },
  { key: 'travel', label: 'Travel Insurance', supported: false },
];

/**
 * Product selection gate. Only AUTO is implemented; everything else stays
 * clearly "coming soon" (matches the backend product gate).
 */
export default function ProductSelect({ onSelect }) {
  const [error, setError] = useState(null);

  async function choose(product) {
    setError(null);
    try {
      await onSelect(product.key);
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <section className="card">
      <h2>What do you want to shop for?</h2>
      <ul className="product-list">
        {PRODUCTS.map((product) => (
          <li key={product.key}>
            <button
              type="button"
              className="product-btn"
              disabled={!product.supported}
              onClick={() => choose(product)}
            >
              <span className="product-label">{product.label}</span>
              {product.supported ? (
                <span className="product-hint">{product.description}</span>
              ) : (
                <span className="product-coming">Coming soon</span>
              )}
            </button>
          </li>
        ))}
      </ul>
      {error && <p className="error-text">Failed to start: {error}</p>}
      <p className="privacy-note">Only local synthetic data in mock mode - no real insurer is contacted.</p>
    </section>
  );
}
