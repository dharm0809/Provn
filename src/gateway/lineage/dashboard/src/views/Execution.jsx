import { useState, useEffect } from 'react';
import { getExecution, getTrace } from '../api';
import { displayModel, formatSessionId, formatTime, truncId, verdictBadgeClass, policyBadgeClass, formatBytes, copyToClipboard } from '../utils';
import TraceWaterfall from '../components/TraceWaterfall';

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

function DetailRow({ label, value, className = '', copyable = false }) {
  return (
    <>
      <div className="detail-label">{label}</div>
      <div className={`detail-value ${className}`}>
        {copyable && value && value !== '-' ? (
          <div className="copy-wrap">
            <span className="copy-text">{value}</span>
            <CopyBtn text={value} />
          </div>
        ) : (value ?? '-')}
      </div>
    </>
  );
}

export default function Execution({ navigate, executionId, sessionId }) {
  const [record, setRecord] = useState(null);
  const [toolEvents, setToolEvents] = useState([]);
  const [trace, setTrace] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showMeta, setShowMeta] = useState(false);

  useEffect(() => {
    (async () => {
      try {
        const [data, traceData] = await Promise.all([
          getExecution(executionId),
          getTrace(executionId).catch(() => null),
        ]);
        setRecord(data.record);
        setToolEvents(data.tool_events || []);
        setTrace(traceData);
      } catch (e) { setError(e.message); }
      finally { setLoading(false); }
    })();
  }, [executionId]);

  if (loading) return <div className="skeleton-block" style={{ height: 400 }} />;
  if (error) return <div className="error-card">Error: {error}</div>;
  if (!record) return <div className="error-card">Record not found</div>;

  const r = record;
  const sid = sessionId || r.session_id;
  const tools = toolEvents.length > 0 ? toolEvents : (r.metadata?.tool_interactions || []);
  const toolStrategy = r.metadata?.tool_strategy;
  const toolIterations = r.metadata?.tool_loop_iterations || 0;
  const decisions = r.metadata?.analyzer_decisions || [];
  const usage = r.metadata?.token_usage;

  return (
    <div className="fade-child">
      <div className="breadcrumb">
        <a onClick={() => navigate('sessions')}>Sessions</a>
        <span className="sep">▸</span>
        <a onClick={() => navigate('timeline', { sessionId: sid })}>{formatSessionId(sid)}</a>
        <span className="sep">▸</span>
        <span className="current">{truncId(r.execution_id, 20)}</span>
      </div>

      {/* Two-column layout */}
      <div className="exec-cols" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        {/* Metadata */}
        <div className="card">
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 12, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}>
            Execution Record
          </div>
          <div className="detail-grid">
            <DetailRow label="Execution ID" value={r.execution_id} className="mono" copyable />
            <DetailRow label="Model" value={displayModel(r.model_id || r.model_attestation_id)} />
            <DetailRow label="Provider Request" value={r.provider_request_id} className="mono" copyable />
            <DetailRow label="Policy" value={r.policy_result} className={policyBadgeClass(r.policy_result)} />
            <DetailRow label="Policy Version" value={r.policy_version} />
            <DetailRow label="Tenant" value={r.tenant_id} />
            <DetailRow label="User" value={r.user || r.metadata?.user || '-'} />
            <DetailRow label="Team" value={r.metadata?.team || '-'} />
            <DetailRow label="Roles" value={r.metadata?.caller_roles?.join(', ') || '-'} />
            <DetailRow label="Auth Source" value={r.metadata?.identity_source || '-'} />
            <DetailRow label="Timestamp" value={formatTime(r.timestamp)} />
          </div>
        </div>

        {/* Chain Integrity + Blockchain Proof */}
        <div className="card">
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 12, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}>
            ◆ Chain Integrity
          </div>
          <div className="detail-grid">
            <DetailRow label="Sequence" value={r.sequence_number} className="mono" />
            <DetailRow label="Record Hash" value={r.record_hash} className="gold mono" copyable />
            <DetailRow label="Previous Hash" value={r.previous_record_hash} className="gold mono" copyable />
          </div>

          {/* Walacor Blockchain Proof */}
          {(r._walacor_eid || r._envelope || r.EId) && (() => {
            const env = r._envelope || {};
            const eid = r._walacor_eid || r.EId || '';
            return (
              <>
                <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginTop: 16, marginBottom: 12, paddingTop: 12, paddingBottom: 8, borderTop: '1px solid var(--border)', borderBottom: '1px solid var(--border)' }}>
                  ◆ Walacor Blockchain Proof
                </div>
                <div className="detail-grid">
                  {eid && <DetailRow label="Entity ID (EId)" value={eid} className="mono" copyable />}
                  {env.block_id && <DetailRow label="Block ID" value={env.block_id} className="gold mono" copyable />}
                  {env.trans_id && <DetailRow label="Transaction ID" value={env.trans_id} className="gold mono" copyable />}
                  {env.data_hash && <DetailRow label="Data Hash (DH)" value={env.data_hash} className="gold mono" copyable />}
                  {env.block_level != null && <DetailRow label="Block Level" value={env.block_level} className="mono" />}
                  {env.block_index != null && <DetailRow label="Block Index" value={env.block_index} className="mono" />}
                  {env.created_at && <DetailRow label="Blockchain Timestamp" value={typeof env.created_at === 'number' ? new Date(env.created_at).toISOString() : env.created_at} className="mono" />}
                </div>
              </>
            );
          })()}
        </div>
      </div>

      {/* Governance Waterfall Trace */}
      {trace?.timings && Object.keys(trace.timings).length > 0 && (
        <div className="card">
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 12, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}>
            ◆ Pipeline Trace
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--gold)', marginLeft: 8 }}>
              {trace.timings.total_ms?.toFixed(0) || '—'}ms total
            </span>
          </div>
          <TraceWaterfall timings={trace.timings} toolEvents={trace.tool_events || []} />
        </div>
      )}

      {/* Prompt */}
      <div className="card">
        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 12, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}>Prompt</div>
        <div className="text-block">{r.prompt_text || '(empty)'}</div>
      </div>

      {/* Response */}
      <div className="card">
        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 12, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}>Response</div>
        <div className="text-block">{r.response_content || (r.thinking_content ? '(see reasoning below)' : '(empty)')}</div>
      </div>

      {/* Thinking */}
      {r.thinking_content && (
        <div className="card">
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 12, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}>◆ Reasoning / Thinking</div>
          <div className="text-block thinking">{r.thinking_content}</div>
        </div>
      )}

      {/* Tool Events */}
      {tools.length > 0 && (
        <div className="card">
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 12, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}>
            ⚙ Tool Calls ({tools.length})
            {toolStrategy && <span className="badge badge-muted" style={{ fontSize: 11, marginLeft: 8 }}>{toolStrategy} strategy</span>}
            {toolIterations > 0 && <span className="badge badge-muted" style={{ fontSize: 11, marginLeft: 8 }}>{toolIterations} iteration{toolIterations > 1 ? 's' : ''}</span>}
          </div>
          {tools.map((te, i) => {
            const isErr = te.is_error === true;
            const inputDisplay = typeof te.input_data === 'string' ? te.input_data : te.input_data ? JSON.stringify(te.input_data, null, 2) : null;
            const sources = te.sources || [];
            const toolAnalysis = te.content_analysis || [];
            return (
              <div key={i} className={`tool-event-card${isErr ? ' tool-error' : ''}`}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
                  <span style={{ fontFamily: 'var(--mono)', fontSize: 14, fontWeight: 600, color: 'var(--gold)', letterSpacing: '0.3px' }}>{te.tool_name || 'unknown'}</span>
                  <span className={`badge ${te.tool_type === 'web_search' ? 'badge-gold' : 'badge-muted'}`}>{te.tool_type || 'function'}</span>
                  <span className={`badge ${te.source === 'gateway' ? 'badge-pass' : 'badge-muted'}`}>{te.source || '-'}</span>
                  {isErr && <span className="badge badge-fail">error</span>}
                  {!isErr && (te.tool_type === 'web_search' || te.tool_name === 'web_search') && sources.length === 0 && <span className="badge badge-warn">no results</span>}
                  {te.duration_ms != null && <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-muted)', marginLeft: 'auto', padding: '2px 8px', background: 'var(--bg-inset)', borderRadius: 3 }}>{te.duration_ms.toFixed(0)}ms</span>}
                </div>
                {inputDisplay && (
                  <div style={{ marginBottom: 12 }}>
                    <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: 1, marginBottom: 6 }}>Input</div>
                    <div style={{ background: 'var(--bg-inset)', border: '1px solid var(--border)', borderRadius: 4, padding: '10px 14px 10px 20px', fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text-primary)', whiteSpace: 'pre-wrap', wordBreak: 'break-word', lineHeight: 1.5, position: 'relative' }}>
                      <span style={{ position: 'absolute', left: 5, top: 10, color: 'var(--gold-dim)', fontWeight: 700 }}>›</span>
                      {inputDisplay}
                    </div>
                  </div>
                )}
                {sources.length > 0 && (
                  <div style={{ marginBottom: 12 }}>
                    <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: 1, marginBottom: 6 }}>Sources ({sources.length})</div>
                    {sources.map((src, si) => (
                      <div key={si} style={{ padding: '6px 0', borderBottom: si < sources.length - 1 ? '1px solid var(--border)' : 'none' }}>
                        <a href={src.url || '#'} target="_blank" rel="noopener noreferrer" style={{ fontSize: 12, fontWeight: 500, color: 'var(--blue)', textDecoration: 'none' }}>
                          {src.title || src.url || 'Link'} ↗
                        </a>
                        {src.snippet && <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2, lineHeight: 1.4 }}>{src.snippet}</div>}
                      </div>
                    ))}
                  </div>
                )}
                <div className="detail-grid" style={{ marginTop: 8 }}>
                  {te.input_hash && <DetailRow label="Input Hash" value={te.input_hash} className="hash-gold" copyable />}
                  {te.output_hash && <DetailRow label="Output Hash" value={te.output_hash} className="hash-gold" copyable />}
                  <DetailRow label="Timestamp" value={formatTime(te.timestamp)} />
                  {te.iteration && <DetailRow label="Iteration" value={te.iteration} />}
                </div>
                {toolAnalysis.length > 0 && (
                  <div style={{ marginTop: 8, paddingTop: 8, borderTop: '1px solid var(--border)' }}>
                    <div style={{ fontSize: 10, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: 1, marginBottom: 6 }}>Output Analysis</div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                      {toolAnalysis.map((a, ai) => (
                        <span key={ai}>
                          <span className={`badge ${verdictBadgeClass(a.verdict)}`}>{a.verdict || '-'}</span>
                          <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--text-muted)', marginLeft: 4, marginRight: 6 }}>{a.analyzer_id || ''}</span>
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Attachments */}
      {(r.file_metadata && r.file_metadata.length > 0) && (
        <div className="card">
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 12, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}>
            Attachments
          </div>
          <div className="attachment-cards">
            {(r.file_metadata || []).map((f, i) => (
              <div key={`file-${i}`} className="attachment-card">
                <div className="attachment-name">{f.filename || 'unknown'}</div>
                <div className="attachment-meta">
                  <span className="badge badge-file">{f.mimetype || 'unknown'}</span>
                  <span>{formatBytes(f.size_bytes)}</span>
                  <span className="badge badge-source">{f.source || 'unknown'}</span>
                </div>
                <div className="attachment-hash">
                  <div className="copy-wrap">
                    <span className="copy-text" title={f.hash_sha3_512 || ''}>SHA3: {(f.hash_sha3_512 || '').substring(0, 24)}…</span>
                    <CopyBtn text={f.hash_sha3_512} />
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Content Analysis */}
      {decisions.length > 0 && (
        <div className="card">
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 12, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}>Content Analysis</div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Analyzer</th><th>Verdict</th><th>Confidence</th><th>Category</th><th>Reason</th></tr></thead>
              <tbody>
                {decisions.map((d, i) => (
                  <tr key={i}>
                    <td className="mono">{d.analyzer_id}</td>
                    <td><span className={`badge ${verdictBadgeClass(d.verdict)}`}>{d.verdict || '-'}</span></td>
                    <td>{d.confidence != null ? d.confidence.toFixed(2) : '-'}</td>
                    <td>{d.category || '-'}</td>
                    <td>{d.reason || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Token Usage */}
      {usage && (
        <div className="card">
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 12, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}>Token Usage</div>
          <div className="detail-grid">
            <DetailRow label="Prompt Tokens" value={usage.prompt_tokens} />
            <DetailRow label="Completion Tokens" value={usage.completion_tokens} />
            <DetailRow label="Total" value={usage.total_tokens} className="mono" />
          </div>
        </div>
      )}

      {/* Raw Metadata */}
      {r.metadata && (
        <div className="card">
          <div onClick={() => setShowMeta(!showMeta)} style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', userSelect: 'none' }}>
            <span style={{ fontSize: 10, color: 'var(--text-muted)', transition: 'transform 0.2s', transform: showMeta ? 'rotate(90deg)' : 'none' }}>▸</span>
            <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px' }}>Raw Metadata</span>
          </div>
          {showMeta && (
            <div className="text-block" style={{ marginTop: 12 }}>
              {JSON.stringify(r.metadata, null, 2)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
