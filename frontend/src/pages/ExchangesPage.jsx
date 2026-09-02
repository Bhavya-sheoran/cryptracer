import { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { fetchRankedExchanges } from '../api/client.js';
import { RiskBadge, EmptyState, Skeleton } from '../components/ui/Bits.jsx';

/**
 * The repeat-destination view: which exchanges reported fraud keeps arriving at.
 * This is the question the problem statement actually asks, answered across
 * every case rather than one wallet at a time.
 */
export default function ExchangesPage({ signedIn }) {
  const [rows, setRows] = useState(null);
  const [scoring, setScoring] = useState(null);

  const load = useCallback(async () => {
    if (!signedIn) return;
    try {
      const d = await fetchRankedExchanges(50);
      setRows(d.entities || []);
      setScoring(d.scoring || null);
    } catch {
      setRows([]);
    }
  }, [signedIn]);

  useEffect(() => { load(); }, [load]);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="crumb">Workspace</div>
          <h1>Exchanges by fraud linkage</h1>
          <p className="lede">
            Ranked by how often victim-reported funds terminate there, time-decayed so recent
            complaints weigh more. A repeat destination is the signal; a single case is not.
          </p>
        </div>
        {signedIn ? (
          <button type="button" className="btn btn-secondary" onClick={load}>Refresh</button>
        ) : null}
      </div>

      <section className="panel">
        <div className="panel-body flush">
          {!signedIn ? (
            <EmptyState glyph="🔒" title="Sign in to view the ranking">
              Each row lists the case numbers behind an exchange&apos;s score, so this view is
              available to a signed-in officer.
            </EmptyState>
          ) : rows === null ? (
            <div className="panel-body stack">
              <Skeleton height="16px" /><Skeleton height="16px" /><Skeleton height="16px" />
            </div>
          ) : rows.length === 0 ? (
            <EmptyState glyph="⇄" title="No linked exchanges yet">
              File a complaint and run a trace. Once funds reach a tagged service, it appears here.
            </EmptyState>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Exchange / service</th>
                    <th>Chain</th>
                    <th className="num">Linked cases</th>
                    <th className="num">Score</th>
                    <th>Risk</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((e) => (
                    <tr key={`${e.entity_name}-${e.chain}`}>
                      <td>
                        <div className="row">
                          <span className="dot dot-ok" />
                          <strong>{e.entity_name}</strong>
                        </div>
                        <span className="tiny subtle">{e.entity_type}</span>
                      </td>
                      <td><span className="badge badge-neutral">{e.chain}</span></td>
                      <td className="num">{e.case_count}</td>
                      <td className="num mono">{Number(e.risk_score).toFixed(2)}</td>
                      <td><RiskBadge label={e.risk_label} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
        {scoring ? (
          <div className="panel-foot">
            Half-life {scoring.half_life_days} days · medium ≥ {scoring.medium_threshold} · high ≥{' '}
            {scoring.high_threshold} · model {scoring.model_version}. Scores are explainable
            aggregates over reported cases, not a black box.
          </div>
        ) : null}
      </section>
    </>
  );
}

ExchangesPage.propTypes = { signedIn: PropTypes.bool };
ExchangesPage.defaultProps = { signedIn: false };
