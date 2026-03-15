import { useState, useEffect, useCallback } from 'react';
import { sha3_512 } from 'js-sha3';
import { getSession, verifySession } from '../api';
import { formatSessionId, displayModel, timeAgo, truncHash, getTokenCount, policyBadgeClass, copyToClipboard } from '../utils';

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
      const data = await getSession(sessionId);
      const recs = data.records || [];
      if (!recs.length) {
        setVerifyResult({ valid: true, message: 'No records to verify' });
        setVerifying(false);
        return;
      }
      const errors = [];
      const GENESIS = '0'.repeat(128);
      let prevHash = GENESIS;
      const results = [];

      for (let i = 0; i < recs.length; i++) {
        const r = recs[i];
        let ok = true;
        if (!r.record_hash) {
          errors.push(`Record #${i}: missing record_hash`);
          ok = false;
        } else {
          if (r.previous_record_hash != null && r.previous_record_hash !== prevHash) {
            errors.push(`Record #${i}: previous_record_hash mismatch`);
            ok = false;
          }
          const canonical = [
            r.execution_id,
            String(r.policy_version ?? ''),
            String(r.policy_result ?? ''),
            String(r.previous_record_hash ?? ''),
            String(r.sequence_number ?? ''),
            String(r.timestamp ?? ''),
          ].join('|');
          const computed = sha3_512(canonical);
          if (computed !== r.record_hash) {
            errors.push(`Record #${i}: hash mismatch (client recompute)`);
            ok = false;
          }
          prevHash = r.record_hash;
        }
        results.push(ok);
      }

      // Animate nodes sequentially
      for (let i = 0; i < results.length; i++) {
        await new Promise(r => setTimeout(r, 180));
        setNodeResults(prev => [...prev, results[i]]);
      }

      if (errors.length === 0) {
        setVerifyResult({ valid: true, message: `Chain Valid — ${recs.length} record${recs.length !== 1 ? 's' : ''} verified, all hashes match` });
      } else {
        setVerifyResult({ valid: false, errors, message: `Chain Invalid — ${errors.length} error${errors.length !== 1 ? 's' : ''}` });
      }
    } catch {
      // Fallback to server-side
      try {
        const result = await verifySession(sessionId);
        setVerifyResult(result.valid
          ? { valid: true, message: `Chain Valid (server-side) — ${result.record_count} record(s)` }
          : { valid: false, errors: result.errors, message: 'Chain Invalid (server-side)' }
        );
      } catch (e2) {
        setVerifyResult({ valid: false, message: `Verification failed: ${e2.message}`, errors: [] });
      }
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
      ) : (
        <div>
          {records.map((r, i) => {
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
                    {toolInfo.length > 0 && <span className="badge badge-gold">⚙ {toolInfo.map(t => t.tool_name || 'tool').join(', ')}</span>}
                    {(r.file_metadata && r.file_metadata.length > 0) && <span className="badge badge-file">📎 {r.file_metadata.length} file{r.file_metadata.length > 1 ? 's' : ''}</span>}
                    {tokens && <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-muted)' }}>{tokens} tokens</span>}
                    <span className="hash-gold">
                      <span className="copy-wrap">
                        <span className="copy-text">{truncHash(r.record_hash, 20)}</span>
                        <CopyBtn text={r.record_hash} />
                      </span>
                    </span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
