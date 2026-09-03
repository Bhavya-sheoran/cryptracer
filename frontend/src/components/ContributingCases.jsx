import PropTypes from 'prop-types';
import { EmptyState } from './ui/Bits.jsx';

/**
 * The audit trail behind the risk score.
 *
 * Phase 2 stores each case's age, decay weight and points precisely so this
 * table can exist. A "High" rating that cannot name the complaints behind it is
 * not actionable, so this renders even when empty - saying so plainly rather
 * than hiding.
 */
export default function ContributingCases({ contributions, totalScore, total }) {
  if (!contributions || contributions.length === 0) {
    return (
      <EmptyState glyph="◔" title="Nothing contributing yet">
        No reported cases currently trace to this destination, so nothing is driving the score.
      </EmptyState>
    );
  }

  const sum = contributions.reduce((a, c) => a + (Number(c.points) || 0), 0);
  // A heavily reported wallet returns a capped list. The score is still
  // computed over every case, so the table can no longer claim to be the
  // complete arithmetic - and saying so is the whole point of this panel.
  const shown = contributions.length;
  const allOf = total || shown;
  const truncated = allOf > shown;

  return (
    <>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Case</th>
              <th>Reported</th>
              <th className="num">Age</th>
              <th className="num">Decay weight</th>
              <th className="num">Points</th>
            </tr>
          </thead>
          <tbody>
            {contributions.map((c) => (
              <tr key={c.case_id}>
                <td className="mono sm">{c.case_number}</td>
                <td className="sm muted">{(c.reported_at || '').slice(0, 10)}</td>
                <td className="num">{c.age_days}d</td>
                <td className="num mono sm">{Number(c.decay_weight).toFixed(4)}</td>
                <td className="num mono sm">{Number(c.points).toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td colSpan={4}>
                {truncated
                  ? `highest-scoring ${shown} of ${allOf} contributing case(s)`
                  : `${shown} contributing case(s)`}
              </td>
              <td className="num mono">{sum.toFixed(2)}</td>
            </tr>
          </tfoot>
        </table>
      </div>
      <p className="hint" style={{ marginTop: 'var(--sp-3)' }}>
        points = 10 × 0.5^(age ÷ half-life). Those points, plus any aggravating factors, map onto
        0–100 by a saturating curve giving {Number(totalScore).toFixed(2)}
        {truncated ? (
          <>
            {' '}— computed across all {allOf} cases. This table shows the {shown} highest
            contributors, so the column total above is a subtotal, not the whole score.
          </>
        ) : (
          <> — recomputable by hand from this table.</>
        )}
      </p>
    </>
  );
}

ContributingCases.propTypes = {
  contributions: PropTypes.arrayOf(PropTypes.object),
  totalScore: PropTypes.number,
  total: PropTypes.number,
};
ContributingCases.defaultProps = { contributions: [], totalScore: 0, total: 0 };
