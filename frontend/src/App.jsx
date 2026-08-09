import HealthStatus from './components/HealthStatus.jsx';

export default function App() {
  return (
    <div className="app">
      <header className="app-header">
        <h1>Ontario All-Quote Agent</h1>
        <p className="tagline">Evidence-first Ontario auto-insurance shopping assistant</p>
      </header>

      <main className="app-main">
        <HealthStatus />
      </main>

      <footer className="app-footer">
        <p>Foundation milestone only — markets, quotes, and evidence coming in later milestones.</p>
      </footer>
    </div>
  );
}
