import { useCallback, useEffect, useRef, useState } from 'react';
import useTheme from './components/ui/useTheme.js';
import { Toaster } from './components/ui/Toaster.jsx';
import Investigate from './pages/Investigate.jsx';
import CasesPage from './pages/CasesPage.jsx';
import AlertsPage from './pages/AlertsPage.jsx';
import ExchangesPage from './pages/ExchangesPage.jsx';
import SignIn from './components/SignIn.jsx';
import ServiceStatus from './components/ServiceStatus.jsx';
import { fetchReadiness, fetchRecentAlerts } from './api/client.js';

const NAV = [
  { id: 'investigate', label: 'Investigate', glyph: '⌕' },
  { id: 'cases', label: 'Cases', glyph: '▤' },
  { id: 'alerts', label: 'Alerts', glyph: '◈' },
  { id: 'exchanges', label: 'Exchanges', glyph: '⇄' },
];

/**
 * Application shell.
 *
 * A left rail rather than one long scrolling page: an investigator moves
 * between a live trace, the case list, the alert queue and the exchange
 * ranking, and those are destinations, not sections of a document.
 *
 * The provenance banner is driven by the backend's own `data_source`, never a
 * constant here, so the UI claim about where the data came from cannot drift
 * from how the system is actually configured.
 */
export default function App() {
  const { theme, toggle } = useTheme();
  const [readiness, setReadiness] = useState(null);
  const [error, setError] = useState(null);
  const [view, setView] = useState('investigate');
  const [user, setUser] = useState(null);
  const [query, setQuery] = useState('');
  const [submitted, setSubmitted] = useState(null);
  const [alertCount, setAlertCount] = useState(0);
  const [showStatus, setShowStatus] = useState(false);
  const searchRef = useRef(null);

  const load = useCallback(async () => {
    try {
      setReadiness(await fetchReadiness());
      setError(null);
    } catch (err) {
      setError(err.message);
      setReadiness(null);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Alerts name case numbers, so the count is only fetched once signed in.
  useEffect(() => {
    if (!user) {
      setAlertCount(0);
      return undefined;
    }
    let cancelled = false;
    fetchRecentAlerts(50)
      .then((d) => !cancelled && setAlertCount((d.alerts || []).length))
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [user]);

  // "/" focuses search - the shortcut every explorer and console has.
  useEffect(() => {
    const onKey = (e) => {
      const tag = document.activeElement?.tagName;
      if (e.key === '/' && tag !== 'INPUT' && tag !== 'TEXTAREA') {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  // A deep link (?address=…) lands straight on a trace.
  useEffect(() => {
    const deep = new URLSearchParams(window.location.search).get('address');
    if (deep) {
      setQuery(deep);
      setSubmitted({ address: deep, at: Date.now() });
    }
  }, []);

  function runSearch(event) {
    event.preventDefault();
    const value = query.trim();
    if (!value) return;
    setView('investigate');
    setSubmitted({ address: value, at: Date.now() });
  }

  const isSynthetic = readiness?.data_source !== 'live_indexer_apis';

  return (
    <Toaster>
      <div className="app">
        <div className={`provenance-banner${isSynthetic ? '' : ' is-live'}`}>
          <strong>{isSynthetic ? 'SYNTHETIC DEMO DATA' : 'LIVE INDEXER DATA'}</strong>
          <span>
            {isSynthetic
              ? 'Complaints and transactions shown here are generated for demonstration. This system holds no real NCRP complaint data and no exchange KYC data.'
              : 'Traces use live public indexer APIs. Complaint records remain synthetic.'}
          </span>
        </div>

        <aside className="rail">
          <div className="brand">
            <div className="brand-mark" aria-hidden="true">CT</div>
            <div className="brand-text">
              <div className="brand-name">ChainTrace</div>
              <div className="brand-sub">SIH26183 · MHA</div>
            </div>
          </div>

          <nav className="rail-nav" aria-label="Sections">
            <span className="rail-label">Workspace</span>
            {NAV.map((item) => (
              <button
                key={item.id}
                type="button"
                className="rail-item"
                aria-current={view === item.id ? 'page' : undefined}
                onClick={() => setView(item.id)}
              >
                <span className="glyph" aria-hidden="true">{item.glyph}</span>
                {item.label}
                {item.id === 'alerts' && alertCount ? (
                  <span className="count">{alertCount}</span>
                ) : null}
              </button>
            ))}
          </nav>

          <div className="rail-foot">
            <button type="button" className="rail-item" onClick={() => setShowStatus((s) => !s)}>
              <span className="glyph" aria-hidden="true">◍</span>
              System status
            </button>
            <button type="button" className="rail-item" onClick={toggle}>
              <span className="glyph" aria-hidden="true">{theme === 'dark' ? '☀' : '☾'}</span>
              {theme === 'dark' ? 'Light theme' : 'Dark theme'}
            </button>
            <p className="tiny subtle" style={{ padding: '0 8px' }}>
              Recommendation-only. Freeze and disclosure require officer approval.
            </p>
          </div>
        </aside>

        <div className="main">
          <header className="topbar">
            <form className="search" onSubmit={runSearch} role="search">
              <span className="glyph" aria-hidden="true">⌕</span>
              <input
                ref={searchRef}
                className="input input-mono"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search a wallet address — BTC, Ethereum-style 0x, or Tron T…"
                aria-label="Search a wallet address"
                spellCheck="false"
                autoComplete="off"
              />
              <kbd>/</kbd>
            </form>

            {user ? (
              <span className="row sm muted nowrap">
                <span className="dot dot-ok" />
                {user.full_name}
                <span className={`badge badge-${user.can_approve ? 'ok' : 'neutral'}`}>
                  {user.role}
                </span>
              </span>
            ) : (
              <span className="badge badge-neutral">not signed in</span>
            )}
          </header>

          <main className="content">
            {error ? (
              <div className="callout callout-danger">
                <span className="glyph" aria-hidden="true">⚠</span>
                <div>
                  <strong>Backend unreachable.</strong> {error}
                  <div className="tiny">Start the stack with <code>make up</code>.</div>
                </div>
              </div>
            ) : null}

            {showStatus && readiness ? (
              <section className="panel">
                <div className="panel-head">
                  <h2>System status</h2>
                  <button type="button" className="btn btn-secondary btn-sm" onClick={load}>
                    Re-check
                  </button>
                </div>
                <div className="panel-body">
                  <p className="sm muted">
                    Backend <strong>{readiness.status}</strong> · phase {readiness.phase}
                  </p>
                  <ServiceStatus checks={readiness.checks} />
                </div>
              </section>
            ) : null}

            {!user ? <SignIn onSignedIn={setUser} /> : null}

            {view === 'investigate' ? (
              <Investigate currentUser={user} submitted={submitted} />
            ) : null}
            {view === 'cases' ? <CasesPage currentUser={user} /> : null}
            {view === 'alerts' ? (
              <AlertsPage
                signedIn={Boolean(user)}
                onInspect={(a) => {
                  setQuery(a);
                  setSubmitted({ address: a, at: Date.now() });
                  setView('investigate');
                }}
              />
            ) : null}
            {view === 'exchanges' ? <ExchangesPage signedIn={Boolean(user)} /> : null}
          </main>

          <footer className="app-footer">
            Recommendation-only system. Any freeze or disclosure request requires explicit approval
            by an authorised officer. Demonstration built on synthetic complaints and public
            datasets — no real NCRP or exchange KYC data. Not an official MHA or I4C product.
          </footer>
        </div>
      </div>
    </Toaster>
  );
}
