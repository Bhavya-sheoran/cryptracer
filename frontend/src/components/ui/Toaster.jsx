import { useCallback, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import { ToastContext } from './toast-context.js';

/**
 * Transient feedback. Actions like "report generated" or "hash verified" need
 * an acknowledgement that does not push the layout around, and errors need to
 * be visible without hunting for a red line somewhere on the page.
 */
export function Toaster({ children }) {
  const [toasts, setToasts] = useState([]);

  const push = useCallback((message, tone = 'ok', ttl = 4200) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    setToasts((prev) => [...prev, { id, message, tone }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), ttl);
  }, []);

  const value = useMemo(() => push, [push]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.tone}`}>
            <span aria-hidden="true">{t.tone === 'error' ? '⚠' : t.tone === 'warn' ? '!' : '✓'}</span>
            <span className="grow">{t.message}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

Toaster.propTypes = { children: PropTypes.node };
Toaster.defaultProps = { children: null };
