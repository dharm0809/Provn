import React from 'react';
import WalacorWordmark from './WalacorWordmark.jsx';

/**
 * SealButton — gold pill shown inside each record card.
 *
 * State rules:
 *   sealed       record has walacor_block_id + walacor_trans_id + walacor_dh
 *                → gold pill "SEALED IN" + Walacor wordmark, clickable
 *   pending      any of those fields is missing but delivery was expected
 *                → muted "◇ SEAL PENDING", disabled
 *   hidden       walacor storage not in use for this record → render null
 *
 * The caller owns the open/closed state and passes it in as `isOpen`.
 * Click is handled via onToggle — event propagation is stopped here so
 * the enclosing record card's own onClick (navigate) doesn't also fire.
 */
export default function SealButton({ state, isOpen, onToggle }) {
  if (state === 'hidden') return null;

  const handleClick = (e) => {
    e.stopPropagation();
    if (state === 'pending') return;
    onToggle && onToggle();
  };

  if (state === 'pending') {
    return (
      <button
        type="button"
        className="exec-seal-btn is-pending"
        disabled
        title="Delivery worker hasn't anchored this record yet"
        onClick={e => e.stopPropagation()}
      >
        <span className="exec-seal-diamond">◇</span> SEAL PENDING
      </button>
    );
  }

  return (
    <button
      type="button"
      className={'exec-seal-btn' + (isOpen ? ' is-active' : '')}
      onClick={handleClick}
      title={isOpen ? 'Close envelope detail' : 'View Walacor envelope'}
    >
      <span className="exec-seal-label">SEALED IN</span>
      <WalacorWordmark size="seal" />
    </button>
  );
}

/**
 * Compute the seal-button state for a record. Exposed as a named
 * helper so Timeline can decide whether to render the button AND
 * whether the drawer is allowed to open for this record.
 *
 * Anchor proof on Walacor:
 *   DH (DataHash) is the primary tamper-evidence anchor — populated
 *     immediately by OCM when the envelope is submitted. A non-null DH
 *     means the record is cryptographically anchored.
 *   BlockId / TransId are the secondary public-blockchain commit ids,
 *     written when OCM batches a chain transaction. They may lag DH by
 *     minutes-to-hours depending on backend batching policy; their
 *     absence does NOT mean the record is unsealed.
 *
 * Previously this function required BlockId/TransId/DH together, which
 * left every record stuck at "pending" because that backend batches
 * the chain commit asynchronously. Treat DH as the source of truth.
 */
export function sealState(r) {
  if (!r) return 'hidden';
  if (r.walacor_dh) return 'sealed';
  // No DH yet. Show pending iff there's any signal that anchoring was
  // expected: a Walacor EId hint, a legacy envelope blob, or an explicit flag.
  const expected = r._walacor_eid || r.EId || r._envelope || r.walacor_storage_enabled;
  if (expected) return 'pending';
  return 'hidden';
}
