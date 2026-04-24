import React, { useEffect, useState, useMemo, useCallback } from 'react';
import CopyBtn from './CopyBtn.jsx';
import JsonView from './JsonView.jsx';
import WalacorWordmark from './WalacorWordmark.jsx';
import { getSealEnvelope, getExecution } from '../api';
import { truncHash, getTokenCount } from '../utils';

const asArray = (v) => (Array.isArray(v) ? v : []);

/**
 * SealDrawer — Walacor envelope detail for one execution.
 *
 * Rendered as a sibling block directly below the record card it
 * belongs to. Independent of any other drawer on the page — multiple
 * records may have their seal drawer open at the same time.
 *
 * Reuses .chain-verified-pass / .chain-verified-fail glow classes
 * from index.css for the verification badge — NEVER redefines them.
 *
 * Props:
 *   r             record from getSession().records[i]
 *   sessionId     string (for the "copy link" button)
 *   totalInChain  number (for "#N of M" in the sidebar)
 */
export default function SealDrawer({ r, sessionId, totalInChain }) {
  const [seal, setSeal] = useState(null);
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showPrompt, setShowPrompt] = useState(false);
  const [showResp,   setShowResp]   = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    Promise.all([
      getSealEnvelope(r.execution_id).catch(e => ({ error: String(e?.message || e) })),
      getExecution(r.execution_id).catch(() => null),
    ]).then(([s, d]) => {
      if (!alive) return;
      setSeal(s || {});
      setDetail(d);
      setLoading(false);
    });
    return () => { alive = false; };
  }, [r.execution_id]);

  const rec = (detail && detail.record) || r;
  const toolList = (detail && detail.tool_events) || asArray(rec.metadata?.tool_interactions);

  // Block B state — envelope === null first, then match?.all_ok
  const blockB = useMemo(() => {
    if (!seal) return { state: 'loading' };
    if (seal.error) return { state: 'unreachable', msg: seal.error };
    if (seal.envelope === null || seal.envelope === undefined) return { state: 'missing' };
    if (!seal.match) return { state: 'no-match' };
    return { state: seal.match.all_ok ? 'match' : 'drift' };
  }, [seal]);

  const sigStatus = rec.record_signature || 'absent';
  const anchored  = !!(rec.walacor_block_id && rec.walacor_trans_id && rec.walacor_dh);
  const chainOk   = sigStatus === 'valid' && anchored && blockB.state === 'match';

  const onCopyLink = useCallback(() => {
    const url = `${location.origin}/lineage/sessions/${sessionId}?focus=${r.execution_id}&seal=1`;
    navigator.clipboard.writeText(url).catch(() => {});
  }, [sessionId, r.execution_id]);

  const onDownloadJson = useCallback(() => {
    const payload = JSON.stringify(seal?.envelope ?? { local: seal?.local ?? null }, null, 2);
    const blob = new Blob([payload], { type: 'application/json' });
    const url  = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${r.execution_id}.envelope.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }, [r.execution_id, seal]);

  return (
    <div className="exec-seal-drawer-wrap">
      <div className="exec-seal-drawer" onClick={e => e.stopPropagation()}>
        <p className="exec-drawer-eyebrow exec-drawer-eyebrow-brand">
          <span>SEALED IN</span>
          <WalacorWordmark size="eyebrow" />
          <span className="exec-drawer-eyebrow-sep">· {r.execution_id}</span>
        </p>

          {loading && <div className="exec-loading"><span className="exec-spinner" /> fetching envelope…</div>}

          {!loading && (
            <>
              <div className="exec-seal-grid">
                {/* ---------- LEFT ---------- */}
                <div className="exec-drawer-main">

                  {/* Block A — LOCAL WAL ANCHOR */}
                  <section className="exec-seal-block">
                    <header className="exec-seal-block-head">
                      <span className="exec-seal-block-label">A · Local WAL anchor</span>
                      <span className="exec-seal-block-hint">from local ledger</span>
                    </header>
                    <div className="exec-seal-block-body">
                      <div className="exec-kv">
                        <KV k="execution_id"         v={r.execution_id} />
                        <KV k="record_id"            v={rec.record_id} />
                        <KV k="previous_record_id"   v={rec.previous_record_id} />
                        <KV k="sequence_number"      v={'#' + (rec.sequence_number ?? '?')} />
                        <KV k="record_hash"          v={rec.record_hash} />
                        <KVPlain k="record_signature" v={sigBadge(sigStatus)} />
                        <KV k="walacor_block_id"     v={rec.walacor_block_id} />
                        <KV k="walacor_trans_id"     v={rec.walacor_trans_id} />
                        <KV k="walacor_dh"           v={rec.walacor_dh} />
                      </div>
                    </div>
                  </section>

                  {/* Block B — LIVE WALACOR ENVELOPE */}
                  {blockB.state === 'unreachable' ? (
                    <div className="exec-banner amber">
                      <span className="exec-banner-dot" />
                      Walacor unreachable — showing last known values from local WAL
                      {seal?.warning && <span style={{ marginLeft: 8, opacity: .7 }}>· {seal.warning}</span>}
                    </div>
                  ) : (
                    <section className="exec-seal-block">
                      <header className="exec-seal-block-head">
                        <span className="exec-seal-block-label">B · Live Walacor envelope</span>
                        <span className="exec-seal-block-hint">walacor.getcomplex()</span>
                      </header>
                      <div className="exec-seal-block-body">
                        {blockB.state === 'missing' ? (
                          <div className="exec-banner amber">
                            <span className="exec-banner-dot" /> envelope not yet delivered
                          </div>
                        ) : (
                          <>
                            <div className="exec-kv">
                              <KV k="UID"       v={seal.envelope?.UID} />
                              <KV k="ORGId"     v={seal.envelope?.ORGId} />
                              <KV k="SV"        v={seal.envelope?.SV} />
                              <KV k="EId"       v={seal.envelope?.EId || rec._walacor_eid || rec.EId} />
                              <KV k="sealed_at" v={seal.envelope?.sealed_at} />
                            </div>
                            <div style={{ marginTop: 14, paddingTop: 14, borderTop: '1px solid var(--border)' }}>
                              <p className="exec-rr-section-label">ROUND-TRIP CHECK</p>
                              {blockB.state === 'match' && (
                                <div className="exec-roundtrip pass">✓ local DH matches remote</div>
                              )}
                              {blockB.state === 'drift' && (
                                <div className="exec-roundtrip fail">
                                  ✗ DRIFT: local={truncHash(rec.walacor_dh, 10)} remote={truncHash(seal.match?.dh, 10)}
                                </div>
                              )}
                              {blockB.state === 'no-match' && (
                                <div className="exec-roundtrip" style={{ color: 'var(--text-muted)' }}>
                                  — no match record returned
                                </div>
                              )}
                            </div>
                          </>
                        )}
                      </div>
                    </section>
                  )}

                  {/* Block C — EXECUTION SNAPSHOT */}
                  <section className="exec-seal-block">
                    <header className="exec-seal-block-head">
                      <span className="exec-seal-block-label">C · Execution snapshot</span>
                    </header>
                    <div className="exec-seal-block-body">
                      <div className="exec-snapshot-meta">
                        <span><strong>{rec.model_id || rec.model_attestation_id || '—'}</strong> · {rec.provider || '—'}</span>
                        <span>{getTokenCount(rec) || '—'} tokens</span>
                        <span>{rec.latency_ms != null ? rec.latency_ms + ' ms' : '—'}</span>
                      </div>

                      <div className="exec-decision-row">
                        {buildPillsCompact(rec).map(p => (
                          <span key={p.id} className={'exec-pill ' + p.status} style={{ cursor: 'default' }}>
                            <span className="exec-pill-dot" />{p.label}
                          </span>
                        ))}
                      </div>

                      <Collapsible label="PROMPT"   open={showPrompt} onToggle={() => setShowPrompt(v => !v)} body={rec.prompt_text     || '(empty)'} />
                      <Collapsible label="RESPONSE" open={showResp}   onToggle={() => setShowResp(v => !v)}   body={rec.response_content || '—'} />

                      <div className="exec-snapshot-meta" style={{ marginTop: 12, marginBottom: 0 }}>
                        <span><strong>{toolList.length}</strong> tool events</span>
                        {toolList.slice(0, 4).map((t, i) => (
                          <span key={i} style={{ color: 'var(--gold)' }}>◆ {t.tool_name || t.name}</span>
                        ))}
                      </div>
                    </div>
                  </section>
                </div>

                {/* ---------- RIGHT ---------- */}
                <aside className="exec-drawer-side">
                  <p className="exec-side-eyebrow">Cryptographic evidence</p>

                  <div className={
                    'exec-side-badge ' + (chainOk ? 'pass' : 'fail') + ' '
                    + (chainOk ? 'chain-verified-pass' : 'chain-verified-fail')
                  }>
                    {chainOk ? '✓ CHAIN VERIFIED' : '✗ CHAIN FAILED'}
                  </div>

                  <div className="exec-side-rows">
                    <span className="exec-side-rows-k">Chain Position</span>
                    <span className="exec-side-rows-v">#{rec.sequence_number ?? '?'} of {totalInChain}</span>

                    <span className="exec-side-rows-k">Signature</span>
                    <span className="exec-side-rows-v">{sigBadge(sigStatus)}</span>

                    <span className="exec-side-rows-k">Anchor</span>
                    <span className="exec-side-rows-v">
                      <span className={'exec-badge-mini ' + (anchored ? 'pass' : 'warn')}>
                        {anchored ? 'sealed' : 'pending'}
                      </span>
                    </span>

                    <span className="exec-side-rows-k">Round-trip</span>
                    <span className="exec-side-rows-v">
                      <span className={'exec-badge-mini ' + rtClass(blockB.state)}>
                        {rtLabel(blockB.state)}
                      </span>
                    </span>
                  </div>

                  <div className="exec-side-footer">
                    <button type="button" onClick={onCopyLink}>COPY LINK</button>
                    <button type="button" onClick={onDownloadJson}>DOWNLOAD JSON</button>
                  </div>
                </aside>
              </div>

              <JsonView
                data={seal?.envelope ?? { local: seal?.local ?? null, note: 'no envelope returned' }}
                label="RAW ENVELOPE JSON"
                initialOpen={false}
              />
            </>
          )}
      </div>
    </div>
  );
}

/* ---------- helpers ---------- */

function KV({ k, v }) {
  const val = v == null || v === '' ? '—' : v;
  return (
    <>
      <span className="exec-kv-k">{k}</span>
      <span className={'exec-kv-v' + (val === '—' ? ' dim' : '')}>
        {val}
        {val !== '—' && <CopyBtn value={String(val)} compact />}
      </span>
    </>
  );
}
function KVPlain({ k, v }) {
  return (
    <>
      <span className="exec-kv-k">{k}</span>
      <span className="exec-kv-v">{v}</span>
    </>
  );
}
function Collapsible({ label, open, onToggle, body }) {
  return (
    <div className="exec-snapshot-collapse">
      <button type="button" className="exec-snapshot-toggle" onClick={onToggle}>
        <span>{open ? '▾' : '▸'} {label}</span>
        <span>{open ? 'hide' : 'show full'}</span>
      </button>
      {open && <div className="exec-snapshot-body">{body}</div>}
    </div>
  );
}
function sigBadge(s) {
  const cls = s === 'valid' ? 'pass' : s === 'invalid' ? 'fail' : s === 'unverifiable' ? 'warn' : 'dim';
  return <span className={'exec-badge-mini ' + cls}>{s}</span>;
}
function rtClass(state) {
  return state === 'match' ? 'pass' : state === 'drift' ? 'fail' : state === 'unreachable' ? 'warn' : 'dim';
}
function rtLabel(state) {
  return state === 'match' ? 'ok' : state === 'drift' ? 'drift'
       : state === 'unreachable' ? 'offline' : state === 'missing' ? 'pending' : 'unknown';
}
function buildPillsCompact(r) {
  const p = policyStatus(r.policy_result);
  return [
    { id: 'att', label: 'attestation',   status: r.attestation_ok === false ? 'fail' : 'pass' },
    { id: 'pol', label: 'policies',      status: p },
    { id: 'ana', label: 'analyzers',     status: (r.analyzers_passed ?? 0) < (r.analyzers_total ?? 0) ? 'fail' : 'pass' },
    { id: 'bud', label: 'budget',        status: r.budget_exceeded ? 'fail' : 'pass' },
    { id: 'rsp', label: 'response pol',  status: r.response_policy_result === 'block' ? 'fail'
                                               : r.response_policy_result === 'warn'  ? 'warn' : 'pass' },
  ];
}
function policyStatus(p) {
  if (!p) return 'pass';
  const s = String(p).toLowerCase();
  if (s.includes('block') || s.includes('deny')) return 'fail';
  if (s.includes('warn')) return 'warn';
  return 'pass';
}
