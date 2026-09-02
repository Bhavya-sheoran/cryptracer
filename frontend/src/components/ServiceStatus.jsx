import PropTypes from 'prop-types';

/** Backend dependency check results. */
export default function ServiceStatus({ checks }) {
  const names = Object.keys(checks);
  if (names.length === 0) return <p className="muted sm">No dependency data returned.</p>;

  return (
    <div className="stat-row" style={{ marginTop: 'var(--sp-3)' }}>
      {names.map((name) => (
        <div key={name} className="stat">
          <span className="stat-label">{name}</span>
          <span className="row">
            <span className={`dot dot-${checks[name].ok ? 'ok' : 'danger'}`} />
            <span className="sm">{checks[name].ok ? 'connected' : 'unreachable'}</span>
          </span>
          {checks[name].error ? <span className="stat-sub">{checks[name].error}</span> : null}
        </div>
      ))}
    </div>
  );
}

ServiceStatus.propTypes = {
  checks: PropTypes.objectOf(PropTypes.shape({ ok: PropTypes.bool, error: PropTypes.string })),
};
ServiceStatus.defaultProps = { checks: {} };
