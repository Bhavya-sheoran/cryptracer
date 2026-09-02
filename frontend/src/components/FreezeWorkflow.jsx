import { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import AddressChip from './ui/AddressChip.jsx';
import { Badge, EmptyState } from './ui/Bits.jsx';
import { useToast } from './ui/toast-context.js';
import {
  approveFreezeRequest,
  createFreezeRequest,
  fetchFreezeRequests,
  rejectFreezeRequest,
  submitFreezeRequest,
} from '../api/client.js';

const FLOW = ['draft', 'pending_approval', 'approved', 'dispatched'];
const STATUS_COPY = {
  draft: 'Drafted. Not yet submitted for approval.',
  pending_approval: 'Awaiting authorisation by a supervisor.',
  approved: 'Authorised by a supervisor.',
  rejected: 'Rejected.',
  dispatched: 'Recorded as dispatched by an officer.',
};
const STATUS_TONE = {
  draft: 'neutral',
  pending_approval: 'warn',
  approved: 'ok',
  dispatched: 'ok',
  rejected: 'danger',
};

/** Progress through the approval chain, so the current stage is obvious. */
function Stepper({ status }) {
  if (status === 'rejected') {
    return <span className="badge badge-danger badge-uppercase">rejected</span>;
  }
  const idx = FLOW.indexOf(status);
  return (
    <div className="stepper">
      {FLOW.map((s, i) => (
        <span key={s} className="row" style={{ gap: 4 }}>
          <span className={`step ${i < idx ? 'done' : i === idx ? 'current' : ''}`}>
            <span className="bullet">{i < idx ? '✓' : i + 1}</span>
            {s.replace('_', ' ')}
          </span>
          {i < FLOW.length - 1 ? <span className="step-arrow">›</span> : null}
        </span>
      ))}
    </div>
  );
}
Stepper.propTypes = { status: PropTypes.string.isRequired };

/**
 * Exchange freeze-request workflow.
 *
 * The approval control is deliberately not a one-click button: it is disabled
 * unless the signed-in officer holds an approving role, and pressing it opens a
 * confirmation that states plainly what is being authorised and that it will be
 * recorded against their name. The backend independently refuses approval from
 * a non-supervisor and from the officer who raised the request - this UI is a
 * convenience, never the control itself.
 */
export default function FreezeWorkflow({ caseId, targetAddress, entityName, currentUser }) {
  const toast = useToast();
  const [requests, setRequests] = useState([]);
  const [justification, setJustification] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(null);

  const load = useCallback(async () => {
    if (!caseId) return;
    try {
      const data = await fetchFreezeRequests(caseId);
      setRequests(data.requests || []);
    } catch (err) {
      toast(err.message, 'error');
    }
  }, [caseId, toast]);

  useEffect(() => { load(); }, [load]);

  async function act(fn, okMessage, ...args) {
    setBusy(true);
    try {
      await fn(...args);
      await load();
      if (okMessage) toast(okMessage);
      return true;
    } catch (err) {
      toast(err.message, 'error');
      return false;
    } finally {
      setBusy(false);
    }
  }

  const canApprove = Boolean(currentUser?.can_approve);

  return (
    <div className="stack">
      <div className="callout callout-info">
        <span className="glyph" aria-hidden="true">ⓘ</span>
        <div className="sm">
          This system recommends; it does not act. A freeze request reaches an exchange only after
          an authorised officer approves it, and this prototype transmits nothing to any real
          exchange.
        </div>
      </div>

      {targetAddress ? (
        <div className="field">
          <label className="label" htmlFor="just">
            Justification for freezing <AddressChip address={targetAddress} />
          </label>
          <textarea
            id="just"
            className="textarea"
            rows={2}
            value={justification}
            placeholder="Why this address should be frozen (minimum 20 characters)."
            onChange={(e) => setJustification(e.target.value)}
          />
          <div className="row">
            <button
              type="button"
              className="btn"
              disabled={busy || justification.trim().length < 20}
              onClick={async () => {
                const ok = await act(
                  createFreezeRequest,
                  'Freeze request drafted',
                  {
                    case_id: caseId,
                    target_address: targetAddress,
                    entity_name: entityName || null,
                    justification,
                  },
                );
                if (ok) setJustification('');
              }}
            >
              Draft freeze request
            </button>
            <span className="hint">{justification.trim().length}/20 characters minimum</span>
          </div>
        </div>
      ) : (
        <p className="hint">Analyse a wallet first to pick a target address.</p>
      )}

      {requests.length === 0 ? (
        <EmptyState glyph="⊘" title="No freeze requests">
          Draft one above. It will require a supervisor to authorise.
        </EmptyState>
      ) : (
        requests.map((r) => (
          <div key={r.id} className={`workflow-item is-${r.status}`} style={{ borderRadius: 'var(--r-md)', border: '1px solid var(--border)' }}>
            <div className="row-between wrap" style={{ marginBottom: 'var(--sp-2)' }}>
              <Badge tone={STATUS_TONE[r.status] || 'neutral'} uppercase>
                {r.status.replace('_', ' ')}
              </Badge>
              <AddressChip address={r.target_address} />
            </div>

            <Stepper status={r.status} />

            <p className="sm" style={{ marginTop: 'var(--sp-2)' }}>{STATUS_COPY[r.status]}</p>
            <p className="tiny subtle">
              Raised by {r.requested_by || 'unknown'}
              {r.approved_by ? ` · actioned by ${r.approved_by}` : ''}
            </p>

            {r.status === 'draft' ? (
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={busy}
                onClick={() => act(submitFreezeRequest, 'Submitted for approval', r.id)}
              >
                Submit for approval
              </button>
            ) : null}

            {r.status === 'pending_approval' ? (
              <div className="approval">
                {!canApprove ? (
                  <p className="sm">
                    <strong>Awaiting a supervisor.</strong> Your role
                    {currentUser?.role ? ` (${currentUser.role})` : ''} cannot authorise a freeze.
                  </p>
                ) : confirming === r.id ? (
                  <>
                    <p className="sm">
                      Authorise a freeze request against <strong>{r.target_address}</strong>? This
                      records your approval against your name in the audit log.
                    </p>
                    <div className="row">
                      <button
                        type="button"
                        className="btn btn-danger btn-sm"
                        disabled={busy}
                        onClick={async () => {
                          await act(approveFreezeRequest, 'Freeze request authorised', r.id);
                          setConfirming(null);
                        }}
                      >
                        Yes, I authorise this freeze
                      </button>
                      <button
                        type="button"
                        className="btn btn-secondary btn-sm"
                        onClick={() => setConfirming(null)}
                      >
                        Cancel
                      </button>
                    </div>
                  </>
                ) : (
                  <div className="row">
                    <button type="button" className="btn btn-sm" disabled={busy} onClick={() => setConfirming(r.id)}>
                      Review &amp; approve…
                    </button>
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={busy}
                      onClick={() =>
                        act(
                          rejectFreezeRequest,
                          'Request rejected',
                          r.id,
                          'Insufficient grounds on current evidence.',
                        )
                      }
                    >
                      Reject
                    </button>
                  </div>
                )}
              </div>
            ) : null}
          </div>
        ))
      )}
    </div>
  );
}

FreezeWorkflow.propTypes = {
  caseId: PropTypes.string,
  targetAddress: PropTypes.string,
  entityName: PropTypes.string,
  currentUser: PropTypes.shape({ role: PropTypes.string, can_approve: PropTypes.bool }),
};
FreezeWorkflow.defaultProps = {
  caseId: null,
  targetAddress: null,
  entityName: null,
  currentUser: null,
};
