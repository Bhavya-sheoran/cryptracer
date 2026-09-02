import PropTypes from 'prop-types';
import AddressChip from './ui/AddressChip.jsx';

const METHOD = {
  tagged_db: {
    badge: 'Curated tag',
    tone: 'ok',
    blurb: 'Matched against the tagged-address database seeded from public sources.',
  },
  classifier: {
    badge: 'Behavioural inference',
    tone: 'warn',
    blurb:
      'No curated tag matched. The category below is inferred from behaviour and is a suggestion, not an identification.',
  },
  none: {
    badge: 'Unattributed',
    tone: 'neutral',
    blurb: 'No curated tag, and not enough behavioural data to suggest a category.',
  },
};

/**
 * VASP attribution.
 *
 * The method badge leads, not the entity name. Whether an attribution is a
 * sourced fact or a model's guess carries very different evidentiary weight,
 * and the classifier deliberately never returns a company name.
 */
export default function AttributionCard({ attribution, terminals, onSelectAddress }) {
  const method = attribution?.method || 'none';
  const copy = METHOD[method] || METHOD.none;

  return (
    <div className="stack">
      <div className="row wrap">
        <span className={`badge badge-${copy.tone} badge-uppercase`}>{copy.badge}</span>
        {attribution?.confidence != null ? (
          <span className="badge badge-neutral">
            confidence {Number(attribution.confidence).toFixed(2)}
          </span>
        ) : null}
      </div>

      <h2 style={{ fontSize: 'var(--fs-xl)' }}>
        {attribution?.entity_name || (method === 'classifier' ? 'Unnamed service' : 'Not attributed')}
      </h2>

      <dl className="kv">
        {attribution?.entity_type ? (<><dt>Category</dt><dd>{attribution.entity_type}</dd></>) : null}
        {attribution?.source ? (<><dt>Tag source</dt><dd className="mono sm">{attribution.source}</dd></>) : null}
        {attribution?.cluster_size ? (
          <><dt>Cluster</dt><dd>{attribution.cluster_size} address{attribution.cluster_size === 1 ? '' : 'es'}</dd></>
        ) : null}
        {attribution?.matched_address ? (
          <>
            <dt>Matched on</dt>
            <dd><AddressChip address={attribution.matched_address} onSelect={onSelectAddress} /></dd>
          </>
        ) : null}
      </dl>

      <p className="sm muted">{copy.blurb}</p>
      {attribution?.note ? <p className="tiny subtle">{attribution.note}</p> : null}

      {terminals && terminals.length > 0 ? (
        <>
          <h4>Services reached by this trace</h4>
          <div className="stack" style={{ gap: 'var(--sp-2)' }}>
            {terminals.map((t) => (
              <div className="row" key={`${t.address}-${t.hop}`}>
                <span className={`dot dot-${t.entity_type === 'mixer' ? 'mixer' : t.entity_type === 'sanctioned' ? 'danger' : 'ok'}`} />
                <strong className="sm">{t.entity_name}</strong>
                <span className="tiny subtle">{t.entity_type} · hop {t.hop}</span>
              </div>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}

AttributionCard.propTypes = {
  attribution: PropTypes.shape({
    method: PropTypes.string,
    entity_name: PropTypes.string,
    entity_type: PropTypes.string,
    confidence: PropTypes.number,
    source: PropTypes.string,
    cluster_size: PropTypes.number,
    matched_address: PropTypes.string,
    note: PropTypes.string,
  }),
  terminals: PropTypes.arrayOf(PropTypes.object),
  onSelectAddress: PropTypes.func,
};
AttributionCard.defaultProps = { attribution: null, terminals: [], onSelectAddress: undefined };
