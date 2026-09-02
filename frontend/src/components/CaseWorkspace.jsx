import { useCallback, useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import FreezeWorkflow from './FreezeWorkflow.jsx';
import AddressChip from './ui/AddressChip.jsx';
import { Badge, EmptyState, Skeleton, TimeAgo } from './ui/Bits.jsx';
import { useToast } from './ui/toast-context.js';
import { truncateMiddle } from './ui/format.js';
import {
  addCaseNote,
  approveStrDraft,
  createStrDraft,
  fetchCase,
  fetchStrDrafts,
  generateReport,
  reportDownloadUrl,
  uploadEvidence,
  verifyReport,
} from '../api/client.js';

/**
 * The case file: notes, evidence with chain-of-custody digests, the hashed
 * forensic report, the STR draft, and the freeze workflow.
 *
 * Every exhibit and report shows its SHA-256, because that digest is the whole
 * chain-of-custody claim - it is what someone recomputes to prove the artifact
 * has not been altered since export.
 */
export default function CaseWorkspace({ caseId, currentUser, targetAddress, entityName, embedded }) {
  const toast = useToast();
  const [detail, setDetail] = useState(null);
  const [drafts, setDrafts] = useState([]);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [verified, setVerified] = useState({});
  const fileRef = useRef(null);

  const load = useCallback(async () => {
    if (!caseId) return;
    try {
      const [c, d] = await Promise.all([fetchCase(caseId), fetchStrDrafts(caseId)]);
      setDetail(c);
      setDrafts(d.drafts || []);
    } catch (err) {
      toast(err.message, 'error');
    }
  }, [caseId, toast]);

  useEffect(() => { load(); }, [load]);

  async function run(fn, okMessage) {
    setBusy(true);
    try {
      await fn();
      await load();
      if (okMessage) toast(okMessage);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setBusy(false);
    }
  }

  if (!caseId) {
    return (
      <Wrapper embedded={embedded}>
        <EmptyState glyph="▤" title="No case linked">
          File this wallet as a complaint to open a case file for it.
        </EmptyState>
      </Wrapper>
    );
  }

  if (!detail) {
    return (
      <Wrapper embedded={embedded}>
        <div className="stack"><Skeleton height="16px" /><Skeleton height="16px" /><Skeleton height="16px" /></div>
      </Wrapper>
    );
  }

  return (
    <Wrapper embedded={embedded} title={detail.case_number} onRefresh={load}>
      <div className="kv-inline" style={{ marginBottom: 'var(--sp-5)' }}>
        <div><dt>Status</dt><dd><Badge tone="info">{detail.status}</Badge></dd></div>
        <div><dt>Source</dt><dd>{detail.source === 'ncrp_mock' ? 'NCRP (mock)' : detail.source}</dd></div>
        <div><dt>NCRP ref</dt><dd className="mono sm">{detail.ncrp_ref || '—'}</dd></div>
        <div><dt>Amount (INR)</dt><dd>{detail.amount_inr != null ? detail.amount_inr.toLocaleString('en-IN') : '—'}</dd></div>
      </div>

      {/* --- notes --- */}
      <h4>Case notes</h4>
      {detail.notes.length === 0 ? (
        <p className="hint">No notes yet.</p>
      ) : (
        <div style={{ marginBottom: 'var(--sp-3)' }}>
          {detail.notes.map((n) => (
            <div className="note-item" key={n.id}>
              <div className="note-meta"><TimeAgo iso={n.created_at} /></div>
              <div className="sm">{n.body}</div>
            </div>
          ))}
        </div>
      )}
      <div className="field">
        <textarea
          className="textarea"
          rows={2}
          value={note}
          placeholder="Add an investigation note."
          onChange={(e) => setNote(e.target.value)}
        />
        <div className="row">
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={busy || !note.trim()}
            onClick={() => run(async () => { await addCaseNote(caseId, note.trim()); setNote(''); }, 'Note added')}
          >
            Add note
          </button>
        </div>
      </div>

      {/* --- evidence --- */}
      <h4 style={{ marginTop: 'var(--sp-6)' }}>Evidence exhibits</h4>
      {detail.evidence.length === 0 ? (
        <p className="hint">No exhibits attached.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr><th>File</th><th className="num">Size</th><th>SHA-256 (chain of custody)</th></tr>
            </thead>
            <tbody>
              {detail.evidence.map((e) => (
                <tr key={e.id}>
                  <td className="sm">{e.filename}</td>
                  <td className="num sm">{e.size_bytes}</td>
                  <td><span className="hash" title={e.sha256}>{truncateMiddle(e.sha256, 12, 8)}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="row" style={{ marginTop: 'var(--sp-2)' }}>
        <input type="file" ref={fileRef} className="input" style={{ padding: 6 }} />
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          disabled={busy}
          onClick={() =>
            run(async () => {
              const f = fileRef.current?.files?.[0];
              if (!f) throw new Error('Choose a file first.');
              await uploadEvidence(caseId, f);
              if (fileRef.current) fileRef.current.value = '';
            }, 'Exhibit attached and hashed')
          }
        >
          Upload exhibit
        </button>
      </div>

      {/* --- reports --- */}
      <h4 style={{ marginTop: 'var(--sp-6)' }}>Forensic report</h4>
      {detail.reports.length === 0 ? (
        <p className="hint">No report generated yet.</p>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr><th>Generated</th><th>SHA-256</th><th>Actions</th></tr>
            </thead>
            <tbody>
              {detail.reports.map((r) => (
                <tr key={r.id}>
                  <td className="sm muted"><TimeAgo iso={r.generated_at} /></td>
                  <td><span className="hash" title={r.sha256}>{truncateMiddle(r.sha256, 12, 8)}</span></td>
                  <td>
                    <div className="row">
                      <a className="btn btn-secondary btn-sm" href={reportDownloadUrl(caseId, r.id)} target="_blank" rel="noreferrer">
                        Download
                      </a>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() =>
                          run(async () => {
                            const v = await verifyReport(caseId, r.id);
                            setVerified((p) => ({ ...p, [r.id]: v.verified }));
                          })
                        }
                      >
                        Verify hash
                      </button>
                      {verified[r.id] === true ? <Badge tone="ok">intact</Badge> : null}
                      {verified[r.id] === false ? <Badge tone="danger">altered</Badge> : null}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="row" style={{ marginTop: 'var(--sp-2)' }}>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          disabled={busy}
          onClick={() => run(() => generateReport(caseId), 'Hashed PDF report generated')}
        >
          Generate hashed PDF report
        </button>
      </div>

      {/* --- STR --- */}
      <h4 style={{ marginTop: 'var(--sp-6)' }}>FIU-IND STR draft</h4>
      <p className="hint">
        Draft only. This system files nothing with FIU-IND; an officer must review, complete and
        file any actual STR.
      </p>
      {drafts.map((d) => (
        <details key={d.id} className="panel" style={{ marginTop: 'var(--sp-2)', padding: 'var(--sp-3)' }}>
          <summary className="row" style={{ cursor: 'pointer' }}>
            <Badge tone={d.status === 'approved' ? 'ok' : 'warn'} uppercase>{d.status}</Badge>
            <span className="tiny muted"><TimeAgo iso={d.created_at} /></span>
            {d.status === 'draft' && currentUser?.can_approve ? (
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={(e) => { e.preventDefault(); run(() => approveStrDraft(d.id), 'STR draft approved for filing'); }}
              >
                Approve for filing
              </button>
            ) : null}
          </summary>
          <pre className="doc-pre" style={{ marginTop: 'var(--sp-3)' }}>{d.body}</pre>
        </details>
      ))}
      <div className="row" style={{ marginTop: 'var(--sp-2)' }}>
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          disabled={busy}
          onClick={() => run(() => createStrDraft(caseId), 'STR draft generated')}
        >
          Generate STR draft
        </button>
      </div>

      {/* --- freeze --- */}
      <h4 style={{ marginTop: 'var(--sp-6)' }}>Exchange freeze request</h4>
      <FreezeWorkflow
        caseId={caseId}
        targetAddress={targetAddress || detail.wallets?.[0]?.address}
        entityName={entityName}
        currentUser={currentUser}
      />

      {detail.wallets?.length ? (
        <>
          <h4 style={{ marginTop: 'var(--sp-6)' }}>Reported addresses</h4>
          <div className="stack" style={{ gap: 'var(--sp-2)' }}>
            {detail.wallets.map((w) => (
              <div className="row sm" key={`${w.chain}-${w.address}`}>
                <Badge tone="neutral">{w.chain}</Badge>
                <AddressChip address={w.address} />
                <span className="tiny subtle">{w.role.replace('_', ' ')}</span>
              </div>
            ))}
          </div>
        </>
      ) : null}
    </Wrapper>
  );
}

/** Renders as a bare block inside a tab, or as its own panel on the Cases page. */
function Wrapper({ embedded, title, onRefresh, children }) {
  if (embedded) return <div className="panel-body">{children}</div>;
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Case file {title ? <span className="mono sm muted">{title}</span> : null}</h2>
        {onRefresh ? (
          <button type="button" className="btn btn-ghost btn-sm" onClick={onRefresh}>Refresh</button>
        ) : null}
      </div>
      <div className="panel-body">{children}</div>
    </section>
  );
}
Wrapper.propTypes = {
  embedded: PropTypes.bool,
  title: PropTypes.string,
  onRefresh: PropTypes.func,
  children: PropTypes.node,
};
Wrapper.defaultProps = { embedded: false, title: null, onRefresh: undefined, children: null };

CaseWorkspace.propTypes = {
  caseId: PropTypes.string,
  currentUser: PropTypes.shape({ role: PropTypes.string, can_approve: PropTypes.bool }),
  targetAddress: PropTypes.string,
  entityName: PropTypes.string,
  embedded: PropTypes.bool,
};
CaseWorkspace.defaultProps = {
  caseId: null,
  currentUser: null,
  targetAddress: null,
  entityName: null,
  embedded: false,
};
