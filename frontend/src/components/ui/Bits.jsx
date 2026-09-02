import PropTypes from 'prop-types';
import { RISK_TONE, relativeTime } from './format.js';

export function RiskBadge({ label }) {
  const tone = RISK_TONE[label] || 'neutral';
  return <span className={`badge badge-${tone} badge-uppercase`}>{label || 'unknown'}</span>;
}
RiskBadge.propTypes = { label: PropTypes.string };
RiskBadge.defaultProps = { label: 'unknown' };

export function Badge({ tone, children, uppercase }) {
  return (
    <span className={`badge badge-${tone}${uppercase ? ' badge-uppercase' : ''}`}>{children}</span>
  );
}
Badge.propTypes = {
  tone: PropTypes.string,
  children: PropTypes.node,
  uppercase: PropTypes.bool,
};
Badge.defaultProps = { tone: 'neutral', children: null, uppercase: false };

export function Stat({ label, value, sub, mono }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className={`stat-value${mono ? ' mono' : ''}`}>{value}</span>
      {sub ? <span className="stat-sub">{sub}</span> : null}
    </div>
  );
}
Stat.propTypes = {
  label: PropTypes.string.isRequired,
  value: PropTypes.node,
  sub: PropTypes.node,
  mono: PropTypes.bool,
};
Stat.defaultProps = { value: '—', sub: null, mono: false };

export function EmptyState({ glyph, title, children, action }) {
  return (
    <div className="empty">
      <span className="glyph" aria-hidden="true">{glyph}</span>
      {title ? <h3>{title}</h3> : null}
      {children ? <p>{children}</p> : null}
      {action}
    </div>
  );
}
EmptyState.propTypes = {
  glyph: PropTypes.string,
  title: PropTypes.string,
  children: PropTypes.node,
  action: PropTypes.node,
};
EmptyState.defaultProps = { glyph: '◎', title: null, children: null, action: null };

export function Callout({ tone, glyph, children }) {
  return (
    <div className={`callout callout-${tone}`}>
      {glyph ? <span className="glyph" aria-hidden="true">{glyph}</span> : null}
      <div className="grow">{children}</div>
    </div>
  );
}
Callout.propTypes = { tone: PropTypes.string, glyph: PropTypes.string, children: PropTypes.node };
Callout.defaultProps = { tone: 'info', glyph: null, children: null };

export function Skeleton({ width, height }) {
  return <div className="skeleton" style={{ width, height }} />;
}
Skeleton.propTypes = { width: PropTypes.string, height: PropTypes.string };
Skeleton.defaultProps = { width: '100%', height: '12px' };

/** Relative time, with the exact timestamp on hover - the convention every
 *  explorer uses, because "3 mins ago" is scannable but evidence needs exact. */
export function TimeAgo({ iso }) {
  if (!iso) return <span className="subtle">—</span>;
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return <span className="subtle">—</span>;
  return <span title={then.toISOString()}>{relativeTime(iso)}</span>;
}
TimeAgo.propTypes = { iso: PropTypes.string };
TimeAgo.defaultProps = { iso: null };
