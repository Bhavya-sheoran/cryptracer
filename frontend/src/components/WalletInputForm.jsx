import { useCallback, useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { validateAddress } from '../api/client.js';

const DEBOUNCE_MS = 350;

/**
 * Complaint intake.
 *
 * Validates against the backend as the investigator types, so a mistyped
 * address is caught before it becomes a case that can never be traced. The
 * check is real - Base58Check, bech32 polymod, EIP-55 - not a regex, so the
 * feedback is worth trusting.
 */
export default function WalletInputForm({ onAnalyse, onSubmitted, busy }) {
  const [address, setAddress] = useState('');
  const [victimRef, setVictimRef] = useState('');
  const [amount, setAmount] = useState('');
  const [narrative, setNarrative] = useState('');
  const [check, setCheck] = useState(null);
  const [checking, setChecking] = useState(false);
  const timer = useRef(null);
  const latest = useRef(0);

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    const value = address.trim();
    if (!value) {
      setCheck(null);
      setChecking(false);
      return undefined;
    }
    setChecking(true);
    timer.current = setTimeout(async () => {
      const seq = ++latest.current;
      try {
        const result = await validateAddress(value);
        // Ignore a stale response that lost the race against a newer keystroke.
        if (seq === latest.current) setCheck(result);
      } catch {
        if (seq === latest.current) setCheck(null);
      } finally {
        if (seq === latest.current) setChecking(false);
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer.current);
  }, [address]);

  const valid = check?.valid === true;

  const file = useCallback(
    async (event) => {
      event.preventDefault();
      const result = await onSubmitted({
        address: address.trim(),
        source: 'manual',
        victim_ref: victimRef.trim() || null,
        amount_inr: amount ? Number(amount) : null,
        narrative: narrative.trim() || null,
      });
      if (result) {
        setVictimRef('');
        setAmount('');
        setNarrative('');
      }
    },
    [address, victimRef, amount, narrative, onSubmitted],
  );

  return (
    <form className="stack" onSubmit={file}>
      <div className="field">
        <label className="label" htmlFor="addr">Suspect wallet address</label>
        <input
          id="addr"
          className={`input input-mono${check ? (valid ? ' is-valid' : ' is-invalid') : ''}`}
          value={address}
          placeholder="Bitcoin, Ethereum-style 0x, or Tron T… address"
          onChange={(e) => setAddress(e.target.value)}
          autoComplete="off"
          spellCheck="false"
        />
        <span className="hint" aria-live="polite" style={{ minHeight: 16 }}>
          {checking ? 'Checking…' : null}
          {!checking && check && valid ? (
            <span style={{ color: 'var(--ok-500)' }}>
              ✓ Valid {check.chain} address ({check.address_kind})
              {check.warnings?.length ? ` — ${check.warnings[0]}` : ''}
            </span>
          ) : null}
          {!checking && check && !valid ? (
            <span style={{ color: 'var(--dang-500)' }}>✕ {check.reason}</span>
          ) : null}
        </span>
      </div>

      <div className="grid-2" style={{ gap: 'var(--sp-4)' }}>
        <div className="field">
          <label className="label" htmlFor="vref">Victim reference (pseudonymous)</label>
          <input
            id="vref"
            className="input"
            value={victimRef}
            placeholder="e.g. NCRP-REF-014"
            onChange={(e) => setVictimRef(e.target.value)}
          />
        </div>
        <div className="field">
          <label className="label" htmlFor="amt">Amount (INR)</label>
          <input
            id="amt"
            className="input"
            type="number"
            min="0"
            value={amount}
            placeholder="250000"
            onChange={(e) => setAmount(e.target.value)}
          />
        </div>
      </div>

      <div className="field">
        <label className="label" htmlFor="narr">Complaint narrative</label>
        <textarea
          id="narr"
          className="textarea"
          rows={2}
          value={narrative}
          placeholder="How the victim was defrauded."
          onChange={(e) => setNarrative(e.target.value)}
        />
      </div>

      <p className="hint">
        Do not enter names, phone numbers or other personal data. Use a pseudonymous case
        reference — the system has nowhere to store PII by design.
      </p>

      <div className="row">
        <button className="btn" type="submit" disabled={!valid || busy}>
          {busy ? 'Working…' : 'File complaint & trace'}
        </button>
        <button
          type="button"
          className="btn btn-secondary"
          disabled={!valid || busy}
          onClick={() => onAnalyse(address.trim())}
        >
          Analyse only
        </button>
      </div>
    </form>
  );
}

WalletInputForm.propTypes = {
  onAnalyse: PropTypes.func.isRequired,
  onSubmitted: PropTypes.func.isRequired,
  busy: PropTypes.bool,
};
WalletInputForm.defaultProps = { busy: false };
