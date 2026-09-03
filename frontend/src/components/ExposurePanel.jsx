import { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import AddressChip from './ui/AddressChip.jsx';
import { Badge, EmptyState, Skeleton, TimeAgo } from './ui/Bits.jsx';
import { fetchExposure } from '../api/client.js';

const SERVICE_TONE = {
  exchange: 'ok',
  mixer: 'warn',
  sanctioned: 'danger',
  darknet: 'danger',
  gambling: 'warn',
  payment_processor: 'neutral',
};

/** Rupees, in the grouping an Indian investigator reads without counting digits. */
function inr(value) {
  if (value == null) return null;
  return `₹${Number(value).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
}

function FeatureBars({ explanation, score }) {
  if (!explanation?.length) return null;
  const max = Math.max(...explanation.map((e) => e.contribution), 0.0001);

  return (
    <div className="factors">
      <div className="row-between">
        <h4>Why this ranks here</h4>
        <span className="tiny subtle">
          contributions sum to {Number(score).toFixed(4)}
        </span>
      </div>
      {explanation.map((e) => (
        <div className="factor" key={e.feature}>
          <span className="factor-name">{e.feature}</span>
          <span className="factor-track">
            <span
              className="factor-fill"
              style={{ width: `${Math.max((e.contribution / max) * 100, 1.5)}%` }}
            />
          </span>
          <span className="factor-value mono">{e.contribution.toFixed(4)}</span>
          <span className="factor-raw tiny subtle">
            {e.feature === 'volume' && typeof e.raw === 'number'
              ? inr(e.raw)
              : e.feature === 'recency' && typeof e.raw === 'number'
                ? `${Math.round(e.raw / 3600)}h ago`
                : String(e.raw)}
          </span>
        </div>
      ))}
    </div>
  );
}
FeatureBars.propTypes = { explanation: PropTypes.array, score: PropTypes.number };
FeatureBars.defaultProps = { explanation: [], score: 0 };

/**
 * Direct exposure. One hop, one transaction, and the strongest evidence the
 * system can produce - so it gets its own treatment rather than being ranked
 * against nothing.
 */
function DirectExposure({ top, onSelectAddress }) {
  const ev = top.evidence || {};
  return (
    <div className="exposure-hero">
      <div className="row wrap">
        <Badge tone="danger" uppercase>Direct exposure</Badge>
        <Badge tone={SERVICE_TONE[top.service_type] || 'neutral'}>{top.service_type}</Badge>
        <span className="tiny subtle">hop {top.hop} — the wallet paid this service itself</span>
      </div>

      <h2 className="exposure-name">{top.service}</h2>

      <dl className="kv">
        <dt>Amount</dt>
        <dd className="mono">
          {ev.amount} {ev.asset}
          {ev.transfer_type === 'token' ? <span className="tiny subtle"> · token transfer</span> : null}
        </dd>
        {ev.token_contract ? (
          <>
            <dt>Token contract</dt>
            <dd><AddressChip address={ev.token_contract} /></dd>
          </>
        ) : null}
        <dt>Transaction</dt>
        <dd><AddressChip address={ev.txid} head={14} tail={8} /></dd>
        <dt>When</dt>
        <dd><TimeAgo iso={ev.timestamp} /></dd>
        <dt>Paid to</dt>
        <dd><AddressChip address={ev.to_address} onSelect={onSelectAddress} /></dd>
        <dt>Label source</dt>
        <dd>
          <span className="mono sm">{top.label?.source}</span>
          <span className="tiny subtle"> · confidence {Number(top.label?.confidence).toFixed(2)}</span>
        </dd>
      </dl>

      <p className="hint">
        This transaction is independently checkable on a block explorer. It is the
        line that goes in the case file.
      </p>
    </div>
  );
}
DirectExposure.propTypes = { top: PropTypes.object.isRequired, onSelectAddress: PropTypes.func };
DirectExposure.defaultProps = { onSelectAddress: undefined };

function Candidate({ candidate, expanded, onToggle, onSelectAddress }) {
  const f = candidate.features || {};
  const tone = SERVICE_TONE[f.service_type] || 'neutral';

  return (
    <div className={`candidate${candidate.rank === 1 ? ' is-top' : ''}`}>
      <button type="button" className="candidate-head" onClick={onToggle}>
        <span className="candidate-rank">{candidate.rank}</span>
        <span className="grow">
          <span className="row wrap">
            <strong>{f.service}</strong>
            <Badge tone={tone}>{f.service_type}</Badge>
            <span className="tiny subtle">hop {f.hop}</span>
          </span>
          <span className="row wrap tiny subtle" style={{ marginTop: 2 }}>
            {f.total_volume_inr != null ? (
              <span><strong className="mono">{inr(f.total_volume_inr)}</strong> reached this service</span>
            ) : (
              <span>{Number(f.total_volume).toFixed(4)} {f.asset} (not priceable)</span>
            )}
            <span>· {f.transfer_count} transfer{f.transfer_count === 1 ? '' : 's'}</span>
            <span>· {f.unique_counterparties} counterpart{f.unique_counterparties === 1 ? 'y' : 'ies'}</span>
            {f.last_seen ? <span>· last <TimeAgo iso={f.last_seen} /></span> : null}
            {f.continuity_ok === false ? (
              <span className="warn-text">· path runs backwards in time</span>
            ) : null}
          </span>
        </span>
        <span className="candidate-score mono">{Number(candidate.score).toFixed(3)}</span>
        <span className="candidate-caret" aria-hidden="true">{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded ? (
        <div className="candidate-body">
          <FeatureBars explanation={candidate.explanation} score={candidate.score} />

          {f.shortest_path?.length ? (
            <>
              <h4>Shortest route</h4>
              <div className="route">
                {f.shortest_path.map((addr, i) => (
                  <span className="route-step" key={`${addr}-${i}`}>
                    <AddressChip address={addr} onSelect={onSelectAddress} head={8} tail={5} />
                    {i < f.shortest_path.length - 1 ? (
                      <span className="route-arrow" aria-hidden="true">→</span>
                    ) : null}
                  </span>
                ))}
              </div>
            </>
          ) : null}

          <div className="row wrap tiny subtle" style={{ marginTop: 'var(--sp-3)' }}>
            <span>label: {(f.label_sources || []).join(', ') || 'unknown'}</span>
            <span>· confidence {Number(f.label_confidence).toFixed(2)}</span>
            {f.price_sources?.length ? <span>· priced via {f.price_sources.join(', ')}</span> : null}
            <span>· basis {f.volume_basis}</span>
          </div>
        </div>
      ) : null}
    </div>
  );
}
Candidate.propTypes = {
  candidate: PropTypes.object.isRequired,
  expanded: PropTypes.bool,
  onToggle: PropTypes.func,
  onSelectAddress: PropTypes.func,
};
Candidate.defaultProps = { expanded: false, onToggle: undefined, onSelectAddress: undefined };

/**
 * Service exposure: the answer to "where did the money end up".
 *
 * Direct exposure is presented differently from a ranked list on purpose. When
 * the wallet paid a service itself there is nothing to rank and the evidence is
 * a single transaction; when it did not, the honest answer is several
 * candidates with the arithmetic that ordered them.
 */
export default function ExposurePanel({ address, maxHops, onSelectAddress }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [openRank, setOpenRank] = useState(1);

  const load = useCallback(async () => {
    if (!address) return;
    setBusy(true);
    setError(null);
    try {
      setData(await fetchExposure(address, maxHops));
    } catch (err) {
      setError(err.message);
      setData(null);
    } finally {
      setBusy(false);
    }
  }, [address, maxHops]);

  useEffect(() => { load(); }, [load]);

  if (busy && !data) {
    return (
      <div className="panel-body stack">
        <Skeleton height="20px" width="45%" />
        <Skeleton height="90px" />
        <Skeleton height="60px" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="panel-body">
        <div className="callout callout-danger">
          <span className="glyph" aria-hidden="true">⚠</span>
          <div>{error}</div>
        </div>
      </div>
    );
  }

  if (!data) return null;

  if (data.kind === 'none') {
    return (
      <div className="panel-body">
        <EmptyState glyph="⊘" title="No service exposure found">
          Nothing reached a labelled exchange, mixer or other service within {data.searched_to_hop}{' '}
          hops. That is a finding, not a failure — the funds may still be sitting in
          unattributed wallets.
        </EmptyState>
      </div>
    );
  }

  return (
    <div className="panel-body stack" style={{ gap: 'var(--sp-5)' }}>
      {data.kind === 'direct' ? (
        <DirectExposure top={data.top} onSelectAddress={onSelectAddress} />
      ) : (
        <>
          <div className="callout callout-info">
            <span className="glyph" aria-hidden="true">ⓘ</span>
            <div className="sm">
              No direct payment to a known service, so the funds were traced onward.
              {' '}{data.explanation}
            </div>
          </div>

          <div className="candidates">
            {data.candidates.map((c) => (
              <Candidate
                key={`${c.features?.service}-${c.rank}`}
                candidate={c}
                expanded={openRank === c.rank}
                onToggle={() => setOpenRank(openRank === c.rank ? null : c.rank)}
                onSelectAddress={onSelectAddress}
              />
            ))}
          </div>
        </>
      )}

      <p className="hint">
        Searched to {data.searched_to_hop} hops · scoring {data.scoring_version} ·{' '}
        {data.data_provenance === 'synthetic' ? 'synthetic demonstration data' : 'live indexer data'}.
        {' '}Rupee values are estimates from public price data, not exchange records.
      </p>
    </div>
  );
}

ExposurePanel.propTypes = {
  address: PropTypes.string,
  maxHops: PropTypes.number,
  onSelectAddress: PropTypes.func,
};
ExposurePanel.defaultProps = { address: null, maxHops: 8, onSelectAddress: undefined };
