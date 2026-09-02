import PropTypes from 'prop-types';
import useAlerts from '../components/useAlerts.js';
import { EmptyState, RiskBadge, TimeAgo } from '../components/ui/Bits.jsx';
import AddressChip from '../components/ui/AddressChip.jsx';

/**
 * Live alert queue. Medium and High resolutions arrive here as they are raised;
 * Low deliberately does not alert, because a feed that fires on everything is a
 * feed investigators learn to ignore.
 */
export default function AlertsPage({ onInspect, signedIn }) {
  const { alerts, status } = useAlerts(60, signedIn);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="crumb">Workspace</div>
          <h1>Alerts</h1>
          <p className="lede">
            Raised when a traced wallet resolves to a Medium or High risk exchange. Delivered over
            Redis Streams to a WebSocket, and replayable — an alert raised while nobody had the
            dashboard open is still here.
          </p>
        </div>
        <span className={`badge badge-${status === 'open' ? 'ok' : status === 'connecting' ? 'warn' : 'neutral'}`}>
          <span className={`dot ${status === 'open' ? 'dot-live' : ''}`} />
          {status === 'open' ? 'live' : status === 'connecting' ? 'connecting' : 'polling'}
        </span>
      </div>

      <section className="panel">
        <div className="panel-body flush">
          {!signedIn ? (
            <EmptyState glyph="🔒" title="Sign in to view alerts">
              Alert messages name case numbers and the exchange a case resolved to, so the feed is
              only available to a signed-in officer.
            </EmptyState>
          ) : alerts.length === 0 ? (
            <EmptyState glyph="◈" title="No alerts yet">
              Analyse a wallet that resolves to a Medium or High risk exchange to raise one.
            </EmptyState>
          ) : (
            alerts.map((a, i) => (
              <div className="alert-row" key={a.stream_msg_id || a.id || i}>
                <RiskBadge label={a.severity} />
                <div className="alert-msg">
                  <div>{a.message}</div>
                  <div className="row wrap tiny subtle" style={{ marginTop: 2 }}>
                    {a.case_number ? <span>{a.case_number}</span> : null}
                    {a.address ? (
                      <AddressChip address={a.address} onSelect={onInspect} head={8} tail={5} />
                    ) : null}
                  </div>
                </div>
                <span className="tiny subtle nowrap">
                  {a.replay ? 'replayed · ' : ''}<TimeAgo iso={a.created_at} />
                </span>
              </div>
            ))
          )}
        </div>
        <div className="panel-foot">
          Alerts describe synthetic demonstration data. Low-risk resolutions do not alert by design.
        </div>
      </section>
    </>
  );
}

AlertsPage.propTypes = { onInspect: PropTypes.func, signedIn: PropTypes.bool };
AlertsPage.defaultProps = { onInspect: undefined, signedIn: false };
