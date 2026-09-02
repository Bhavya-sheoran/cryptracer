import PropTypes from 'prop-types';
import { RISK_TONE } from './ui/format.js';

const TONE_VAR = { ok: 'var(--ok-500)', warn: 'var(--warn-500)', danger: 'var(--dang-500)' };

/**
 * Risk score display.
 *
 * The number never appears alone. A ring shows magnitude at a glance, a
 * three-band scale shows where it falls against the configured thresholds, and
 * the contributing factors sit beside it — because "81" on its own tells an
 * investigator nothing about whether to act.
 */
export default function RiskPanel({ label, score, factors, halfLifeDays }) {
  const pct = Math.max(0, Math.min(100, Number(score) || 0));
  const tone = RISK_TONE[label] || 'neutral';
  const colour = TONE_VAR[tone] || 'var(--text-subtle)';

  const r = 46;
  const circumference = 2 * Math.PI * r;
  const filled = (pct / 100) * circumference;

  return (
    <div className="risk-hero">
      <div className="risk-gauge">
        <svg width="108" height="108" viewBox="0 0 108 108" aria-hidden="true">
          <circle cx="54" cy="54" r={r} fill="none" stroke="var(--bg-sunken)" strokeWidth="9" />
          <circle
            cx="54" cy="54" r={r} fill="none"
            stroke={colour} strokeWidth="9" strokeLinecap="round"
            strokeDasharray={`${filled} ${circumference - filled}`}
          />
        </svg>
        <div className="risk-gauge-label">
          <span className="risk-gauge-score" style={{ color: colour }}>{pct.toFixed(1)}</span>
          <span className="risk-gauge-of">/ 100</span>
        </div>
      </div>

      <div className="risk-meta grow">
        <span className={`badge badge-${tone} badge-uppercase`}>{label} fraud linkage</span>

        <div className="risk-scale" aria-hidden="true">
          <span className={`risk-scale-seg${pct > 0 ? ' on-low' : ''}`} />
          <span className={`risk-scale-seg${pct >= 40 ? ' on-medium' : ''}`} />
          <span className={`risk-scale-seg${pct >= 70 ? ' on-high' : ''}`} />
        </div>
        <div className="risk-scale-caption">
          <span>low</span><span>medium 40</span><span>high 70</span>
        </div>

        <ul className="factor-list" style={{ marginTop: 'var(--sp-3)' }}>
          {(factors || []).map((f) => <li key={f}>{f}</li>)}
        </ul>

        <p className="tiny subtle" style={{ marginTop: 'var(--sp-2)' }}>
          Aggregated over reported cases
          {halfLifeDays ? ` with a ${halfLifeDays}-day half-life` : ' with time decay'}, on a
          saturating curve so one prolific reporter cannot dominate the ranking.
        </p>
      </div>
    </div>
  );
}

RiskPanel.propTypes = {
  label: PropTypes.string,
  score: PropTypes.number,
  factors: PropTypes.arrayOf(PropTypes.string),
  halfLifeDays: PropTypes.number,
};
RiskPanel.defaultProps = { label: 'unknown', score: 0, factors: [], halfLifeDays: null };
