import { useState, useEffect, useCallback } from 'react';
import { getSession, verifySession } from '../api';
import { formatSessionId, displayModel, timeAgo, truncHash, getTokenCount, policyBadgeClass, copyToClipboard, fileTypeInfo, formatBytes } from '../utils';

function CopyBtn({ text }) {
  const [copied, setCopied] = useState(false);
  if (!text) return null;
  return (
    <button className={`copy-btn${copied ? ' copied' : ''}`} onClick={e => {
      e.stopPropagation();
      copyToClipboard(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); });
    }}>{copied ? '✓' : '⎘'}</button>
  );
}

export default function Timeline({ navigate, sessionId }) {
  const [records, setRecords] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [verifyResult, setVerifyResult] = useState(null);
  const [verifying, setVerifying] = useState(false);
  const [nodeResults, setNodeResults] = useState([]);

  useEffect(() => {
    (async () => {
      try {
        const data = await getSession(sessionId);
        setRecords(data.records || []);
      } catch (e) { setError(e.message); }
      finally { setLoading(false); }
    })();
  }, [sessionId]);

  const handleVerify = useCallback(async () => {
    setVerifying(true);
    setVerifyResult(null);
    setNodeResults([]);
    try {
      const result = await verifySession(sessionId);
      const n = result.records_checked ?? 0;
      // Animate nodes: valid if no errors for that record (all-or-nothing per session)
      const nodeOk = result.valid;
      for (let i = 0; i < n; i++) {
        await new Promise(r => setTimeout(r, 180));
        setNodeResults(prev => [...prev, nodeOk]);
      }
      if (result.valid) {
        setVerifyResult({ valid: true, message: `Chain Valid — ${n} record${n !== 1 ? 's' : ''} verified, ID pointers link · Walacor sealed`, attestation: result.walacor_attestation });
      } else {
        setVerifyResult({ valid: false, errors: result.errors, message: `Chain Invalid — ${result.errors.length} error${result.errors.length !== 1 ? 's' : ''}`, attestation: result.walacor_attestation });
      }
    } catch (e) {
      setVerifyResult({ valid: false, message: `Verification failed: ${e.message}`, errors: [] });
    }
    setVerifying(false);
  }, [sessionId]);

  if (loading) return <div className="skeleton-block" style={{ height: 400 }} />;
  if (error) return <div className="error-card">Error: {error}</div>;

  const model = records.length > 0 ? displayModel(records[0].model_id || records[0].model_attestation_id) : '-';
  const lastTime = records.length > 0 ? timeAgo(records[records.length - 1].timestamp) : '-';

  return (
    <div className="fade-child">
      <div className="breadcrumb">
        <a onClick={() => navigate('sessions')}>Sessions</a>
        <span className="sep">▸</span>
        <span className="current">{formatSessionId(sessionId)}</span>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20 }}>
        <div>
          <div style={{ fontSize: 14, fontWeight: 600 }}>
            <span className="copy-wrap">
              <span className="copy-text">{formatSessionId(sessionId)}</span>
              <CopyBtn text={sessionId} />
            </span>
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
            {records.length} record{records.length !== 1 ? 's' : ''} · {model} · {lastTime}
          </div>
        </div>
        <button className="btn btn-gold" onClick={handleVerify} disabled={verifying}>
          {verifying ? 'Verifying…' : '◆ Verify Chain'}
        </button>
      </div>

      {verifyResult && (
        <div className={`verify-banner ${verifyResult.valid ? 'pass' : 'fail'}`}>
          <span style={{ fontSize: 18, lineHeight: 1 }}>{verifyResult.valid ? '✓' : '✗'}</span>
          {verifyResult.message}
        </div>
      )}

      {records.length === 0 ? (
        <div className="empty-state"><h3>No records in this session</h3></div>
      ) : (() => {
        const userRecords = records.filter(r => {
          const rt = r.metadata?.request_type || '';
          return !rt.startsWith('system_task');
        });
        const systemRecords = records.filter(r => {
          const rt = r.metadata?.request_type || '';
          return rt.startsWith('system_task');
        });
        return (
        <div>
          {userRecords.map((r, i) => {
            const seq = r.sequence_number ?? '?';
            const prompt = (r.prompt_text || '').substring(0, 100);
            const response = (r.response_content || r.thinking_content || '').substring(0, 80);
            const tokens = getTokenCount(r);
            const isLast = i === records.length - 1;
            const verified = i < nodeResults.length ? (nodeResults[i] ? 'pass' : 'fail') : null;
            const toolInfo = r.metadata?.tool_interactions || [];

            return (
              <div key={r.execution_id || i} className="chain-node">
                <div className="chain-marker">
                  <div className={`chain-seq${verified ? ` verified-${verified}` : ''}`}>{seq}</div>
                  {!isLast && <div className={`chain-connector${verified ? ` verified-${verified}` : ''}`} />}
                </div>
                <div className="chain-card" onClick={() => navigate('execution', { executionId: r.execution_id, sessionId })}>
                  <div style={{ fontSize: 13, color: 'var(--text-primary)', marginBottom: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {prompt || '(empty prompt)'}
                  </div>
                  <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 10, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {response && `→ ${response}`}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
                    {r.policy_result && <span className={`badge ${policyBadgeClass(r.policy_result)}`}>{r.policy_result}</span>}
                    {r.user && <span className="badge badge-identity">{r.user}</span>}
                    {toolInfo.map((t, ti) => {
                      const hasErr = t.is_error === true;
                      const srcCount = (t.sources || []).length;
                      const isSearch = t.tool_name === 'web_search' || t.tool_type === 'web_search';
                      const cls = hasErr ? 'badge-fail' : (isSearch && srcCount === 0) ? 'badge-warn' : 'badge-gold';
                      const suffix = isSearch
                        ? (hasErr ? ' failed' : srcCount > 0 ? ` ·${srcCount}` : ' ·0')
                        : '';
                      return <span key={ti} className={`badge ${cls}`}>⚙ {t.tool_name || 'tool'}{suffix}</span>;
                    })}
                    {(r.file_metadata && r.file_metadata.length > 0) && r.file_metadata.map((f, fi) => {
                      const ft = fileTypeInfo(f.mimetype, f.filename);
                      return (
                        <span key={fi} className={`badge ${ft.badgeClass}`}
                          title={`${f.filename}\n${ft.label} · ${f.mimetype} · ${f.size_bytes ? formatBytes(f.size_bytes) : '—'}\nSHA3: ${f.hash_sha3_512 || '—'}`}
                          style={{ cursor: 'default' }}>
                          {ft.icon} {f.filename || 'file'}
                        </span>
                      );
                    })}
                    {tokens && <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-muted)' }}>{tokens} tokens</span>}
                    {(r.record_id || r.record_hash) && (
                      <span className="hash-gold">
                        <span className="copy-wrap">
                          <span className="copy-text">{truncHash(r.record_id || r.record_hash, 20)}</span>
                          <CopyBtn text={r.record_id || r.record_hash} />
                        </span>
                      </span>
                    )}
                    {r.record_signature && (
                      <span className="badge badge-gold" title={`Ed25519: ${r.record_signature}`} style={{ cursor: 'default', fontSize: 10 }}>
                        signed
                      </span>
                    )}
                    {(r.walacor_block_id || r.walacor_dh || r._walacor_eid || r.EId) && (
                      <span className="badge badge-gold" title={`Block: ${r.walacor_block_id || '—'}\nDH: ${r.walacor_dh || '—'}\nTrans: ${r.walacor_trans_id || '—'}`} style={{ cursor: 'default' }}>
                        ◆ on-chain
                      </span>
                    )}
                  </div>
                  {/* File/Image detail */}
                  {r.file_metadata && r.file_metadata.length > 0 && (
                    <div style={{ marginTop: 8, padding: '8px 10px', background: 'var(--bg-hover)', borderRadius: 6, fontSize: 11 }}>
                      {r.file_metadata.map((f, fi) => {
                        const ft = fileTypeInfo(f.mimetype, f.filename);
                        return (
                          <div key={fi} style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: fi < r.file_metadata.length - 1 ? 6 : 0 }}>
                            <span style={{ fontSize: 16 }}>{ft.icon}</span>
                            <div style={{ flex: 1, minWidth: 0 }}>
                              <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{f.filename || 'unknown'}</div>
                              <div style={{ color: 'var(--text-muted)', display: 'flex', gap: 12, flexWrap: 'wrap', marginTop: 2 }}>
                                <span className={`badge ${ft.badgeClass}`} style={{ fontSize: 10 }}>{ft.label}</span>
                                <span>{f.mimetype || '—'}</span>
                                {f.size_bytes > 0 && <span>{formatBytes(f.size_bytes)}</span>}
                                <span style={{ textTransform: 'uppercase', fontSize: 10 }}>{f.source || 'upload'}</span>
                              </div>
                              {f.hash_sha3_512 && (
                                <div style={{ marginTop: 2 }}>
                                  <span className="copy-wrap">
                                    <span className="copy-text" style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--gold)' }}>
                                      SHA3: {truncHash(f.hash_sha3_512, 20)}
                                    </span>
                                    <CopyBtn text={f.hash_sha3_512} />
                                  </span>
                                </div>
                              )}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                  {/* Blockchain proof summary row */}
                  {r._envelope && r._envelope.block_id && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 6, flexWrap: 'wrap' }}>
                      <span style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>Blockchain:</span>
                      <span className="hash-gold" style={{ fontSize: 10 }}>
                        <span className="copy-wrap">
                          <span className="copy-text" title="Block ID">{truncHash(r._envelope.block_id, 16)}</span>
                          <CopyBtn text={r._envelope.block_id} />
                        </span>
                      </span>
                      {r._envelope.data_hash && (
                        <span className="hash-gold" style={{ fontSize: 10 }}>
                          <span className="copy-wrap">
                            <span className="copy-text" title="Data Hash">DH: {truncHash(r._envelope.data_hash, 12)}</span>
                            <CopyBtn text={r._envelope.data_hash} />
                          </span>
                        </span>
                      )}
                      {r._walacor_eid && (
                        <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-muted)' }} title="Walacor Entity ID">
                          EId: {truncHash(r._walacor_eid, 12)}
                        </span>
                      )}
                    </div>
                  )}
                </div>
              </div>
            );
          })}

          {/* System Tasks (follow-ups, tags, etc.) — collapsible */}
          {systemRecords.length > 0 && (
            <details style={{ marginTop: 20 }}>
              <summary style={{
                cursor: 'pointer', fontSize: 12, fontWeight: 600,
                color: 'var(--text-muted)', textTransform: 'uppercase',
                letterSpacing: '0.8px', padding: '8px 0',
                borderTop: '1px solid var(--border)',
              }}>
                System Tasks ({systemRecords.length}) — follow-ups, tags, suggestions
              </summary>
              <div style={{ marginTop: 8 }}>
                {systemRecords.map((r, i) => {
                  const prompt = (r.prompt_text || '').substring(0, 120);
                  const response = (r.response_content || r.thinking_content || '').substring(0, 100);
                  const rt = r.metadata?.request_type || 'system_task';
                  return (
                    <div key={r.execution_id || `sys-${i}`}
                      className="chain-card" style={{ marginBottom: 8, opacity: 0.7 }}
                      onClick={() => navigate('execution', { executionId: r.execution_id, sessionId })}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                        <span className="badge badge-muted" style={{ fontSize: 10 }}>{rt}</span>
                        {r.metadata?.tool_interactions?.length > 0 && (
                          <span className="badge badge-gold" style={{ fontSize: 10 }}>
                            tools: {r.metadata.tool_interactions.length}
                          </span>
                        )}
                        <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-muted)', marginLeft: 'auto' }}>
                          {getTokenCount(r)} tokens
                        </span>
                      </div>
                      <div style={{ fontSize: 12, color: 'var(--text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {prompt}
                      </div>
                      {response && (
                        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', opacity: 0.7 }}>
                          → {response}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </details>
          )}
        </div>
        );
      })()}
    </div>
  );
}
