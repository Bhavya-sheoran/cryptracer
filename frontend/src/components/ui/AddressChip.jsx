import { useState } from 'react';
import PropTypes from 'prop-types';
import { truncateMiddle } from './format.js';

/**
 * A wallet address, rendered the way explorers render them.
 *
 * Long hex strings are unusable raw: an investigator needs to recognise one at
 * a glance, copy it exactly, and see any entity label attached to it. So the
 * chip shows a head/tail truncation, keeps the full value in the title and on
 * the clipboard, and carries the entity tag inline.
 */
export default function AddressChip({ address, entityName, entityType, onSelect, full, head, tail }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(address);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      /* clipboard blocked (insecure origin) - the title attribute still has it */
    }
  }

  const label = full ? address : truncateMiddle(address, head, tail);

  return (
    <span className="addr">
      {onSelect ? (
        <button type="button" className="addr-link" title={address} onClick={() => onSelect(address)}>
          {label}
        </button>
      ) : (
        <span className="addr-text" title={address}>{label}</span>
      )}

      <button
        type="button"
        className={`copy-btn${copied ? ' copied' : ''}`}
        onClick={copy}
        aria-label={copied ? 'Address copied' : 'Copy address'}
        title={copied ? 'Copied' : 'Copy to clipboard'}
      >
        {copied ? '✓' : '⧉'}
      </button>

      {entityName ? (
        <span className={`entity-tag is-${entityType || 'unknown'}`} title={`${entityName} (${entityType || 'unknown'})`}>
          <span className={`dot dot-${entityType === 'mixer' ? 'mixer' : entityType === 'sanctioned' ? 'danger' : 'ok'}`} />
          {entityName}
        </span>
      ) : null}
    </span>
  );
}

AddressChip.propTypes = {
  address: PropTypes.string.isRequired,
  entityName: PropTypes.string,
  entityType: PropTypes.string,
  onSelect: PropTypes.func,
  full: PropTypes.bool,
  head: PropTypes.number,
  tail: PropTypes.number,
};

AddressChip.defaultProps = {
  entityName: null,
  entityType: null,
  onSelect: undefined,
  full: false,
  head: 10,
  tail: 6,
};
