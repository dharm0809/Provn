/* Walacor Gateway — Sessions View
   Two-level flow: session list → session timeline drill-down.
   Mock-data only; designed to match the governance control-room aesthetic. */

// ── Shared tiny helpers ───────────────────────────────────────
function fmtShortId(id, head = 8, tail = 4) {
  if (!id) return '—';
  if (id.length <= head + tail + 1) return id;
  const tailPart = tail > 0 ? id.slice(-tail) : '';
  return id.slice(0, head) + '…' + tailPart;
}
function fmtDuration(sec) {
  if (sec == null) return '—';
  if (sec < 60) return sec + 's';
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return m + 'm ' + String(s).padStart(2, '0') + 's';
}
function fmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(2) + ' MB';
}
function CopyBtn({ text, title }) {
  const [copied, setCopied] = React.useState(false);
  if (!text) return null;
  return (
    <button
      type="button"
      className={`ses-copy-btn${copied ? ' copied' : ''}`}
      title={title || 'Copy'}
      onClick={(e) => {
        e.stopPropagation();
        try {
          navigator.clipboard.writeText(text).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1400);
          });
        } catch { /* ignore */ }
      }}>
      {copied ? '✓' : '⎘'}
    </button>
  );
}

// ── Metric bar ────────────────────────────────────────────────
function SessionsMetricBar({ sessions }) {
  const total = sessions.length;
  const lastHour = sessions.filter(s => (Date.now() - new Date(s.last_activity)) < 3600 * 1000).length;
  const avgTurns = total ? (sessions.reduce((s, x) => s + (x.user_message_count || 0), 0) / total) : 0;
  const withTools = sessions.filter(s => (s.tools || []).length > 0).length;
  const chainOk = sessions.filter(s => s.chain_status === 'verified').length;

  return (
    <div className="ses-metric-bar">
      <div className="ses-metric">
        <div className="ses-metric-label">Sessions</div>
        <div className="ses-metric-value">{total}</div>
        <div className="ses-metric-sub">threaded through gateway</div>
      </div>
      <div className="ses-metric">
        <div className="ses-metric-label">Active · last hour</div>
        <div className="ses-metric-value gold">{lastHour}</div>
        <div className="ses-metric-sub">
          <span className="ses-dot-green" />live traffic
        </div>
      </div>
      <div className="ses-metric">
        <div className="ses-metric-label">Avg turns</div>
        <div className="ses-metric-value">{avgTurns.toFixed(1)}</div>
        <div className="ses-metric-sub">user messages / session</div>
      </div>
      <div className="ses-metric">
        <div className="ses-metric-label">Sessions w/ tools</div>
        <div className="ses-metric-value">{withTools}</div>
        <div className="ses-metric-sub">gateway + mcp interactions</div>
      </div>
      <div className="ses-metric accent">
        <div className="ses-metric-label">Chain integrity</div>
        <div className="ses-metric-value green">
          {chainOk}/{total}
        </div>
        <div className="ses-metric-sub">
          <span className="ses-dot-green" />verified on chain
        </div>
      </div>
    </div>
  );
}

// ── Session row (list) ────────────────────────────────────────
function ToolBadge({ t }) {
  const cls = t.source === 'mcp' ? 'ses-tool-mcp' : 'ses-tool-gw';
  const mark = t.source === 'mcp' ? '⚡' : '⚙';
  return <span className={`ses-tool-chip ${cls}`}>{mark}<span>{t.name}</span></span>;
}

function SessionListRow({ s, onOpen }) {
  const qPreview = s.user_question || '';
  return (
    <div className="ses-row" role="button" tabIndex={0}
      onClick={() => onOpen(s)}
      onKeyDown={(e) => { if (e.key === 'Enter') onOpen(s); }}>
      <div className="ses-row-line1">
        <span className="ses-row-user">
          <span className="ses-avatar">{(s.user || '?').charAt(0).toUpperCase()}</span>
          <span className="ses-user-name">{s.user}</span>
        </span>
        <span className="ses-row-id">
          <span className="mono ses-row-id-text">{fmtShortId(s.session_id, 13, 0)}</span>
          <CopyBtn text={s.session_id} />
        </span>
        {s.chain_status === 'verified'
          ? <span className="ses-chain-chip ok">◆ chain verified</span>
          : <span className="ses-chain-chip warn">⚠ chain warn</span>}
        {s.blocked_count > 0 && (
          <span className="ses-policy-chip block">{s.blocked_count} blocked</span>
        )}
        <span className="ses-row-time">{timeAgo(s.last_activity)}</span>
      </div>

      <div className="ses-row-q">&ldquo;{qPreview.length > 124 ? qPreview.slice(0, 124) + '…' : qPreview}&rdquo;</div>

      <div className="ses-row-line3">
        <span className="ses-stat">
          <span className="ses-stat-lbl">MODEL</span>
          <span className="ses-stat-val mono">{s.model}</span>
        </span>
        <span className="ses-stat">
          <span className="ses-stat-lbl">TURNS</span>
          <span className="ses-stat-val mono">{s.user_message_count}</span>
        </span>
        <span className="ses-stat">
          <span className="ses-stat-lbl">RECORDS</span>
          <span className="ses-stat-val mono">{s.record_count}</span>
        </span>
        <span className="ses-stat">
          <span className="ses-stat-lbl">DURATION</span>
          <span className="ses-stat-val mono">{fmtDuration(s.duration_sec)}</span>
        </span>

        <span className="ses-indicators">
          {(s.tools || []).map((t, i) => <ToolBadge key={i} t={t} />)}
          {s.has_rag_context && <span className="ses-ind-chip ses-ind-rag">◫ RAG</span>}
          {s.has_images && <span className="ses-ind-chip ses-ind-img">▣ image</span>}
          {s.has_files && !s.has_images && <span className="ses-ind-chip ses-ind-file">▤ files</span>}
          {!(s.tools || []).length && !s.has_rag_context && !s.has_files && !s.has_images && (
            <span className="ses-ind-none">no attachments</span>
          )}
        </span>
      </div>
    </div>
  );
}

// ── Sessions list view ────────────────────────────────────────
function SessionsListView({ onOpen }) {
  const all = window.SessionsData.list;
  const [q, setQ] = React.useState('');
  const [chainFilter, setChainFilter] = React.useState('all');
  const [modelFilter, setModelFilter] = React.useState('all');
  const [sort, setSort] = React.useState('recent');

  const models = React.useMemo(() => {
    const s = new Set(all.map(x => x.model));
    return ['all', ...Array.from(s)];
  }, [all]);

  const filtered = React.useMemo(() => {
    const qq = q.trim().toLowerCase();
    let list = all.filter(s => {
      if (chainFilter !== 'all' && s.chain_status !== chainFilter) return false;
      if (modelFilter !== 'all' && s.model !== modelFilter) return false;
      if (!qq) return true;
      return (
        (s.session_id || '').toLowerCase().includes(qq) ||
        (s.user || '').toLowerCase().includes(qq) ||
        (s.model || '').toLowerCase().includes(qq) ||
        (s.user_question || '').toLowerCase().includes(qq)
      );
    });
    if (sort === 'turns') list = list.slice().sort((a, b) => b.user_message_count - a.user_message_count);
    else if (sort === 'duration') list = list.slice().sort((a, b) => b.duration_sec - a.duration_sec);
    return list;
  }, [all, q, chainFilter, modelFilter, sort]);

  return (
    <div className="ses-view">
      <SessionsMetricBar sessions={all} />

      <div className="card ses-list-card">
        <div className="ses-list-head">
          <div className="ses-list-title">
            <span className="ses-list-title-main">Sessions</span>
            <span className="ses-list-title-count">
              <span className="mono">{filtered.length}</span>
              {filtered.length !== all.length && <span className="ses-muted">of {all.length}</span>}
            </span>
          </div>

          <div className="ses-list-controls">
            <div className="ses-search">
              <svg className="ses-search-icon" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.5">
                <circle cx="7" cy="7" r="5"/>
                <line x1="11" y1="11" x2="14.5" y2="14.5" strokeLinecap="round"/>
              </svg>
              <input
                className="ses-search-input"
                placeholder="filter by session, user, model, or question…"
                value={q}
                onChange={(e) => setQ(e.target.value)} />
              {q && (
                <button type="button" className="ses-search-clear" onClick={() => setQ('')} aria-label="Clear">×</button>
              )}
            </div>

            <div className="ses-filter-group">
              <span className="ses-filter-label">chain</span>
              {['all', 'verified', 'warn'].map(f => (
                <button key={f}
                  className={`ses-filter-chip${chainFilter === f ? ' active' : ''}`}
                  onClick={() => setChainFilter(f)}>
                  {f}
                </button>
              ))}
            </div>

            <div className="ses-filter-group">
              <span className="ses-filter-label">model</span>
              <select className="ses-select" value={modelFilter} onChange={(e) => setModelFilter(e.target.value)}>
                {models.map(m => <option key={m} value={m}>{m === 'all' ? 'all models' : m}</option>)}
              </select>
            </div>

            <div className="ses-filter-group">
              <span className="ses-filter-label">sort</span>
              <select className="ses-select" value={sort} onChange={(e) => setSort(e.target.value)}>
                <option value="recent">most recent</option>
                <option value="turns">most turns</option>
                <option value="duration">longest duration</option>
              </select>
            </div>

            <button className="btn-wal btn-ghost btn-sm">⇣ Export</button>
          </div>
        </div>

        <div className="ses-rows">
          {filtered.length === 0 ? (
            <div className="ses-empty">
              <div className="ses-empty-title">No sessions match</div>
              <div className="ses-empty-sub">Adjust the filters above or clear the search.</div>
            </div>
          ) : filtered.map(s => (
            <SessionListRow key={s.session_id} s={s} onOpen={onOpen} />
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Timeline drill-down ────────────────────────────────────────
function PolicyChip({ result }) {
  const map = {
    allow:                { cls: 'ses-pol-allow', label: 'ALLOW' },
    allow_with_redaction: { cls: 'ses-pol-redact', label: 'ALLOW + REDACT' },
    block:                { cls: 'ses-pol-block', label: 'BLOCK' },
    warn:                 { cls: 'ses-pol-warn', label: 'WARN' },
  };
  const meta = map[result] || map.allow;
  return <span className={`ses-pol-chip ${meta.cls}`}>{meta.label}</span>;
}

function VerifyBanner({ result }) {
  if (!result) return null;
  const ok = result.valid;
  return (
    <div className={`ses-verify-banner ${ok ? 'pass' : 'fail'}`}>
      <div className="ses-verify-icon">{ok ? '◆' : '✗'}</div>
      <div className="ses-verify-body">
        <div className="ses-verify-msg">{result.message}</div>
        {!ok && result.errors && (
          <ul className="ses-verify-errs">
            {result.errors.slice(0, 3).map((e, i) => <li key={i}>{e}</li>)}
          </ul>
        )}
      </div>
      <div className="ses-verify-meta mono">
        {ok ? 'SHA3-512 · ED25519' : 'verification failed'}
      </div>
    </div>
  );
}

function ChainRecord({ r, isLast, verified, onClick }) {
  const tools = (r.metadata && r.metadata.tool_interactions) || [];
  const prompt = r.prompt_text || '';
  const response = r.response_content || '';
  const seqCls = verified === 'pass' ? 'verified-pass' : verified === 'fail' ? 'verified-fail' : '';
  const onChain = !!(r._envelope && r._envelope.block_id);

  return (
    <div className="ses-chain-node">
      <div className="ses-chain-marker">
        <div className={`ses-chain-seq ${seqCls}`}>
          {verified === 'pass' ? '✓' : verified === 'fail' ? '✗' : r.sequence_number}
        </div>
        {!isLast && <div className={`ses-chain-connector ${seqCls}`} />}
      </div>

      <div className="ses-chain-card" onClick={onClick}>
        <div className="ses-chain-card-head">
          <span className="ses-chain-seq-lbl mono">#{r.sequence_number}</span>
          <PolicyChip result={r.policy_result} />
          {r.user && <span className="ses-identity-chip">👤 {r.user}</span>}
          <span className="ses-chain-time mono">{new Date(r.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
          <span className="ses-chain-toks mono">{r.tokens} tok</span>
        </div>

        <div className="ses-chain-prompt">
          <span className="ses-chain-arrow">▸</span>
          <span className="ses-chain-prompt-text">{prompt}</span>
        </div>
        {response && (
          <div className="ses-chain-response">
            <span className="ses-chain-arrow">↳</span>
            <span className="ses-chain-response-text">{response}</span>
          </div>
        )}

        {(tools.length > 0 || r.file_metadata) && (
          <div className="ses-chain-attach">
            {tools.map((t, i) => (
              <span key={i} className={`ses-tool-chip ${t.tool_source === 'mcp' ? 'ses-tool-mcp' : 'ses-tool-gw'}${t.is_error ? ' err' : ''}`}>
                {t.tool_source === 'mcp' ? '⚡' : '⚙'}
                <span>{t.tool_name}</span>
                {t.is_error ? <span className="ses-tool-err"> failed</span> : null}
                {t.tool_name === 'web_search' && !t.is_error ? <span className="ses-tool-cnt">·{(t.sources || []).length}</span> : null}
              </span>
            ))}
            {r.file_metadata && r.file_metadata.map((f, i) => (
              <span key={`f-${i}`} className="ses-file-chip">
                <span className="ses-file-icon">◱</span>
                <span className="ses-file-name">{f.filename}</span>
                <span className="ses-file-size mono">{fmtBytes(f.size_bytes)}</span>
              </span>
            ))}
          </div>
        )}

        <div className="ses-chain-proof">
          <div className="ses-proof-row">
            <span className="ses-proof-lbl mono">RECORD</span>
            <span className="ses-proof-hash mono">{fmtShortId(r.record_hash, 14, 6)}</span>
            <CopyBtn text={r.record_hash} title="Copy record hash" />
            <span className="ses-proof-sig" title={`Ed25519: ${r.record_signature || ''}`}>signed</span>
          </div>
          {onChain && (
            <div className="ses-proof-row">
              <span className="ses-proof-lbl mono gold">◆ BLOCK</span>
              <span className="ses-proof-hash mono gold">{fmtShortId(r._envelope.block_id, 12, 6)}</span>
              <CopyBtn text={r._envelope.block_id} title="Copy block ID" />
              <span className="ses-proof-lbl mono muted">DH</span>
              <span className="ses-proof-hash mono muted">{fmtShortId(r._envelope.data_hash, 10, 4)}</span>
              <span className="ses-proof-lbl mono muted">EID</span>
              <span className="ses-proof-hash mono muted">{fmtShortId(r._walacor_eid, 8, 4)}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function SessionTimelineView({ session, onBack }) {
  const records = React.useMemo(() => window.SessionsData.getRecords(session.session_id), [session.session_id]);

  const [verifying, setVerifying] = React.useState(false);
  const [verifyResult, setVerifyResult] = React.useState(null);
  const [nodeResults, setNodeResults] = React.useState([]);

  const handleVerify = React.useCallback(async () => {
    setVerifying(true);
    setVerifyResult(null);
    setNodeResults([]);
    let prev = '0'.repeat(64);
    const results = [];
    for (let i = 0; i < records.length; i++) {
      const r = records[i];
      const ok = r.previous_record_hash === prev && !!r.record_hash;
      results.push(ok);
      prev = r.record_hash;
    }
    // Sequential animation
    for (let i = 0; i < results.length; i++) {
      await new Promise(r => setTimeout(r, 240));
      setNodeResults(prev => [...prev, results[i]]);
    }
    const allOk = results.every(Boolean);
    setVerifyResult(allOk
      ? { valid: true, message: `Chain verified — ${records.length} records, all hashes match, all signatures valid.` }
      : { valid: false, message: `Chain invalid — ${results.filter(x => !x).length} mismatch(es)`, errors: [] }
    );
    setVerifying(false);
  }, [records]);

  const duration = records.length > 1
    ? Math.round((new Date(records[records.length - 1].timestamp) - new Date(records[0].timestamp)) / 1000)
    : 0;
  const totalTokens = records.reduce((s, r) => s + (r.tokens || 0), 0);

  return (
    <div className="ses-view">
      <button className="ses-back-btn" onClick={onBack}>
        <span className="ses-back-arrow">◂</span>
        <span>Back to sessions</span>
      </button>

      <div className="card ses-timeline-head">
        <div className="ses-timeline-head-left">
          <div className="ses-timeline-eyebrow">◆ SESSION CHAIN · {records.length} RECORDS</div>
          <div className="ses-timeline-id">
            <span className="mono">{session.session_id}</span>
            <CopyBtn text={session.session_id} title="Copy session id" />
          </div>
          <div className="ses-timeline-meta">
            <span className="ses-meta-item"><span className="ses-meta-lbl">USER</span><span className="ses-meta-val">{session.user}</span></span>
            <span className="ses-meta-item"><span className="ses-meta-lbl">MODEL</span><span className="ses-meta-val mono">{session.model}</span></span>
            <span className="ses-meta-item"><span className="ses-meta-lbl">DURATION</span><span className="ses-meta-val mono">{fmtDuration(duration || session.duration_sec)}</span></span>
            <span className="ses-meta-item"><span className="ses-meta-lbl">TOKENS</span><span className="ses-meta-val mono">{totalTokens.toLocaleString()}</span></span>
            <span className="ses-meta-item"><span className="ses-meta-lbl">STARTED</span><span className="ses-meta-val mono">{new Date(records[0]?.timestamp || session.started_at).toLocaleString()}</span></span>
          </div>
        </div>
        <div className="ses-timeline-head-right">
          <div className="ses-verify-card">
            <div className="ses-verify-eyebrow mono">CRYPTOGRAPHIC VERIFICATION</div>
            <div className="ses-verify-algo mono">SHA3-512 + ED25519 · Walacor chain</div>
            <button
              className="btn-wal btn-primary"
              onClick={handleVerify}
              disabled={verifying}>
              {verifying ? '◆ verifying…' : '◆ verify chain'}
            </button>
          </div>
        </div>
      </div>

      <VerifyBanner result={verifyResult} />

      <div className="ses-chain">
        {records.map((r, i) => (
          <ChainRecord
            key={r.execution_id}
            r={r}
            isLast={i === records.length - 1}
            verified={i < nodeResults.length ? (nodeResults[i] ? 'pass' : 'fail') : null}
            onClick={() => {}} />
        ))}
      </div>
    </div>
  );
}

// ── Shell ─────────────────────────────────────────────────────
function SessionsView() {
  const [selected, setSelected] = React.useState(null);
  React.useEffect(() => {
    // Reset scroll when drilling in/out
    const el = document.querySelector('.main');
    if (el) el.scrollTop = 0;
  }, [selected]);

  if (selected) {
    return <SessionTimelineView session={selected} onBack={() => setSelected(null)} />;
  }
  return <SessionsListView onOpen={setSelected} />;
}

window.SessionsView = SessionsView;
