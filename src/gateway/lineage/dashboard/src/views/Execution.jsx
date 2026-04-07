import { useState, useEffect } from 'react';
import { getExecution, getTrace, getSession } from '../api';
import { displayModel, formatSessionId, formatTime, truncId, verdictBadgeClass, policyBadgeClass, formatBytes, copyToClipboard, fileTypeInfo } from '../utils';
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
  const [showExecutionRecord, setShowExecutionRecord] = useState(true);
  const [showChainIntegrity, setShowChainIntegrity] = useState(false);
  const [questionRecords, setQuestionRecords] = useState([]);

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

  useEffect(() => {
    if (!record?.session_id) return;
    (async () => {
      try {
        const data = await getSession(record.session_id);
        const recs = (data.records || []).filter((rec) => {
          const rt = rec.metadata?.request_type || '';
          return !rt.startsWith('system_task');
        });
        setQuestionRecords(recs);
      } catch {
        setQuestionRecords([]);
      }
    })();
  }, [record?.session_id]);

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
  const questionIndex = questionRecords.findIndex((rec) => rec.execution_id === r.execution_id);
  const hasQuestionNav = questionIndex >= 0;
  const isFirstQuestion = questionIndex <= 0;
  const isLastQuestion = questionIndex === questionRecords.length - 1;
  const prevQuestion = !isFirstQuestion ? questionRecords[questionIndex - 1] : null;
  const nextQuestion = !isLastQuestion ? questionRecords[questionIndex + 1] : null;

  return (
    <div className="fade-child">
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap', marginBottom: 12 }}>
        <div className="breadcrumb" style={{ marginBottom: 0 }}>
          <a onClick={() => navigate('sessions')}>Sessions</a>
          <span className="sep">▸</span>
          <a onClick={() => navigate('timeline', { sessionId: sid })}>{formatSessionId(sid)}</a>
          <span className="sep">▸</span>
          <span className="current">{truncId(r.execution_id, 20)}</span>
        </div>

        {hasQuestionNav && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {!isFirstQuestion && prevQuestion && (
              <button
                type="button"
                className="btn"
                onClick={() => navigate('execution', { executionId: prevQuestion.execution_id, sessionId: sid })}
              >
                ← Previous Question
              </button>
            )}
            <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text-muted)' }}>
              Question {questionIndex + 1} of {questionRecords.length}
            </span>
            {!isLastQuestion && nextQuestion && (
              <button
                type="button"
                className="btn btn-gold"
                onClick={() => navigate('execution', { executionId: nextQuestion.execution_id, sessionId: sid })}
              >
                Next Question →
              </button>
            )}
          </div>
        )}
      </div>

      {/* Top stack layout */}
      <div className="exec-cols" style={{ display: 'grid', gridTemplateColumns: '1fr', gap: 16 }}>
        {/* Metadata */}
        <div className="card">
          <div
            onClick={() => setShowExecutionRecord(!showExecutionRecord)}
            style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', userSelect: 'none', fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: showExecutionRecord ? 12 : 0, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}
          >
            <span style={{ fontSize: 10, color: 'var(--text-muted)', transition: 'transform 0.2s', transform: showExecutionRecord ? 'rotate(90deg)' : 'none' }}>▸</span>
            <span>Execution Record</span>
          </div>
          {showExecutionRecord && (
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
          )}
        </div>

        {/* TruzenAI Proof */}
        <div className="card">
          <div
            onClick={() => setShowChainIntegrity(!showChainIntegrity)}
            style={{ display: 'flex', alignItems: 'center', gap: 6, cursor: 'pointer', userSelect: 'none', fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: showChainIntegrity ? 12 : 0, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}
          >
            <span style={{ fontSize: 10, color: 'var(--text-muted)', transition: 'transform 0.2s', transform: showChainIntegrity ? 'rotate(90deg)' : 'none' }}>▸</span>
            <span>TruzenAI Proof</span>
          </div>
          {showChainIntegrity && (
            <>
              {/* TruzenAI Proof */}
              {(r._walacor_eid || r._envelope || r.EId) && (() => {
                const env = r._envelope || {};
                const eid = r._walacor_eid || r.EId || '';
                return (
                  <>
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
            </>
          )}
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

      {/* Prompt + Full Prompt */}
      <div className="card">
        <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 12, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}>
          Prompt
          {r.metadata?.request_type && <span className="badge badge-muted" style={{ fontSize: 10, marginLeft: 8 }}>{r.metadata.request_type}</span>}
          {r.metadata?._intent && <span className="badge badge-gold" style={{ fontSize: 10, marginLeft: 4 }}>intent: {r.metadata._intent}</span>}
        </div>
        <div className="text-block" style={{ fontSize: 15, lineHeight: 1.6 }}>
          {r.metadata?.walacor_audit?.user_question || r.prompt_text?.substring(0, 200) || '(empty)'}
        </div>
        {r.prompt_text && r.prompt_text.length > 200 && (
          <details style={{ marginTop: 8 }}>
            <summary style={{ cursor: 'pointer', fontSize: 11, color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
              Full Prompt ({r.prompt_text.length} chars — includes conversation history)
            </summary>
            <div className="text-block" style={{ marginTop: 8, fontSize: 12, opacity: 0.8 }}>{r.prompt_text}</div>
          </details>
        )}
      </div>

      {/* Attachments — shown inline between question and response */}
      {(r.file_metadata && r.file_metadata.length > 0) && (
        <div className="card">
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 12, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}>
            📎 Attachments ({r.file_metadata.length})
          </div>
          {r.file_metadata.map((f, i) => {
            const ft = fileTypeInfo(f.mimetype, f.filename);
            return (
              <div key={`file-${i}`} style={{ display: 'flex', gap: 14, padding: '12px 14px', background: 'var(--bg-hover)', borderRadius: 8, marginBottom: i < r.file_metadata.length - 1 ? 10 : 0, border: '1px solid var(--border)' }}>
                <div style={{ fontSize: 32, lineHeight: 1, alignSelf: 'center' }}>{ft.icon}</div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontWeight: 600, fontSize: 14, color: 'var(--text-primary)', marginBottom: 6 }}>
                    {f.filename || 'unknown'}
                  </div>
                  <div className="detail-grid" style={{ gridTemplateColumns: 'auto 1fr', gap: '4px 12px', fontSize: 12 }}>
                    <span style={{ color: 'var(--text-muted)', fontWeight: 600 }}>Type</span>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span className={`badge ${ft.badgeClass}`} style={{ fontSize: 11 }}>{ft.icon} {ft.label}</span>
                      <span className="mono" style={{ fontSize: 11, color: 'var(--text-secondary)' }}>{f.mimetype || 'unknown'}</span>
                    </span>
                    <span style={{ color: 'var(--text-muted)', fontWeight: 600 }}>Size</span>
                    <span className="mono" style={{ color: 'var(--text-primary)' }}>{formatBytes(f.size_bytes)}</span>
                    <span style={{ color: 'var(--text-muted)', fontWeight: 600 }}>Source</span>
                    <span><span className="badge badge-muted" style={{ fontSize: 11 }}>{f.source || 'unknown'}</span></span>
                    <span style={{ color: 'var(--text-muted)', fontWeight: 600 }}>SHA3-512</span>
                    <span className="copy-wrap">
                      <span className="copy-text mono" style={{ fontSize: 11, color: 'var(--gold)', wordBreak: 'break-all' }}>{f.hash_sha3_512 || '—'}</span>
                      <CopyBtn text={f.hash_sha3_512} />
                    </span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

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

      {/* Content Analysis */}
      {(() => {
        const available = (decisions || []).filter(d => !(d.reason || '').includes('unavailable') && d.confidence !== 0.0);
        if (!available.length) return null;
        return (
          <div className="card">
            <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 12, paddingBottom: 8, borderBottom: '1px solid var(--border)' }}>Content Analysis</div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>Analyzer</th><th>Verdict</th><th>Confidence</th><th>Category</th><th>Reason</th></tr></thead>
                <tbody>
                  {available.map((d, i) => (
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
        );
      })()}

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
