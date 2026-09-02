import { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { fetchCases } from '../api/client.js';
import CaseWorkspace from '../components/CaseWorkspace.jsx';
import { Badge, EmptyState, Skeleton, TimeAgo } from '../components/ui/Bits.jsx';

const SOURCE_TONE = { ncrp_mock: 'info', synthetic: 'neutral', manual: 'neutral' };
const STATUS_TONE = { open: 'warn', tracing: 'info', analysed: 'ok', escalated: 'danger', closed: 'neutral' };

/**
 * Case list, and the case file for whichever case is selected.
 *
 * Master/detail rather than navigation: an investigator comparing cases wants
 * the list to stay put while they read one.
 */
export default function CasesPage({ currentUser }) {
  const [data, setData] = useState(null);
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    if (!currentUser) return;
    try {
      setData(await fetchCases(50));
      setError(null);
    } catch (err) {
      setError(err.message);
      setData({ total: 0, cases: [] });
    }
  }, [currentUser]);

  useEffect(() => { load(); }, [load]);

  if (!currentUser) {
    return (
      <section className="panel">
        <div className="panel-body">
          <EmptyState glyph="▤" title="Sign in to view cases">
            Case records, notes and evidence are only available to a signed-in officer.
          </EmptyState>
        </div>
      </section>
    );
  }

  return (
    <>
      <div className="page-head">
        <div>
          <div className="crumb">Workspace</div>
          <h1>Cases</h1>
          <p className="lede">
            Every complaint filed through intake or the mock NCRP feed. Selecting one opens its
            file: notes, evidence, hashed report, STR draft and the freeze workflow.
          </p>
        </div>
        <button type="button" className="btn btn-secondary" onClick={load}>Refresh</button>
      </div>

      {error ? <div className="callout callout-danger"><span className="glyph">⚠</span><div>{error}</div></div> : null}

      <div className="grid-main">
        <section className="panel">
          <div className="panel-head">
            <h2>All cases {data ? <span className="badge badge-neutral">{data.total}</span> : null}</h2>
          </div>
          <div className="panel-body flush">
            {data === null ? (
              <div className="panel-body stack">
                <Skeleton height="16px" /><Skeleton height="16px" /><Skeleton height="16px" />
              </div>
            ) : data.cases.length === 0 ? (
              <EmptyState glyph="▤" title="No cases filed">
                Report a suspect wallet from the Investigate tab to open the first case.
              </EmptyState>
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Case</th>
                      <th>Status</th>
                      <th>Source</th>
                      <th className="num">Amount (INR)</th>
                      <th>Reported</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.cases.map((c) => (
                      <tr
                        key={c.case_id}
                        className="is-clickable"
                        onClick={() => setSelected(c.case_id)}
                        style={selected === c.case_id ? { background: 'var(--accent-soft)' } : undefined}
                      >
                        <td className="mono sm">{c.case_number}</td>
                        <td><Badge tone={STATUS_TONE[c.status] || 'neutral'}>{c.status}</Badge></td>
                        <td>
                          <Badge tone={SOURCE_TONE[c.source] || 'neutral'}>
                            {c.source === 'ncrp_mock' ? 'NCRP (mock)' : c.source}
                          </Badge>
                        </td>
                        <td className="num">{c.amount_inr != null ? c.amount_inr.toLocaleString('en-IN') : '—'}</td>
                        <td className="sm muted"><TimeAgo iso={c.reported_at} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </section>

        <div className="stack">
          {selected ? (
            <CaseWorkspace caseId={selected} currentUser={currentUser} />
          ) : (
            <section className="panel">
              <div className="panel-body">
                <EmptyState glyph="↤" title="Select a case">
                  Pick a case from the list to open its file.
                </EmptyState>
              </div>
            </section>
          )}
        </div>
      </div>
    </>
  );
}

CasesPage.propTypes = {
  currentUser: PropTypes.shape({ role: PropTypes.string, can_approve: PropTypes.bool }),
};
CasesPage.defaultProps = { currentUser: null };
