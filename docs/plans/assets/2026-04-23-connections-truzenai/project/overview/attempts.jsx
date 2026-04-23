/* Walacor Gateway — Attempts View
   Dense request-stream with filters, disposition chips, and per-row drawers. */

// ── Disposition helpers (match source app semantics) ──────────
function dispositionMetaA(d) {
  if (!d) return { cls: 'att-disp-muted', cat: 'other', label: '—' };
  if (d === 'allowed' || d === 'forwarded') return { cls: 'att-disp-allow', cat: 'allow', label: d === 'forwarded' ? 'FORWARDED' : 'ALLOWED' };
  if (d === 'audit_only_allowed')            return { cls: 'att-disp-allow', cat: 'allow', label: 'AUDIT ONLY' };
  if (d.startsWith('denied_'))                return { cls: 'att-disp-block', cat: 'block', label: d.replace(/_/g, ' ').toUpperCase() };
  if (d.startsWith('error_'))                 return { cls: 'att-disp-error', cat: 'error', label: d.replace(/_/g, ' ').toUpperCase() };
  return { cls: 'att-disp-muted', cat: 'other', label: d.toUpperCase() };
}

function statusMeta(code) {
  if (code < 300) return { cls: 'att-status-ok', text: String(code) };
  if (code < 500) return { cls: 'att-status-warn', text: String(code) };
  return { cls: 'att-status-err', text: String(code) };
}

function dispositionHelp(d) {
  const help = {
    forwarded: 'Forwarded to the upstream model provider according to routing rules. The full prompt, tools, and timings are available in the execution trace.',
    allowed: 'Passed gateway auth, policy, and storage checks. Completion was executed and persisted in the audit chain.',
    audit_only_allowed: 'Allowed under an audit-only mode — logged without full forward, or with restricted forward, per gateway configuration.',
    denied_policy: 'A configured policy blocked this request (OPA/Rego rule, model allowlist, or content constraint). Adjust the policy or the request payload to proceed.',
    denied_auth: 'Rejected before policy or forwarding. Typical causes: missing or invalid API key, wrong key for this route, or an auth middleware failure.',
    denied_budget: 'Token or spend budget for this tenant or key was exceeded, or reserved capacity was unavailable.',
    denied_rate_limit: 'Exceeded configured per-key or global rate limits for the gateway.',
    denied_attestation: 'Attestation or integrity checks failed — model or deployment attestation did not satisfy required proofs.',
    denied_wal_full: 'Could not persist audit state (WAL or storage back-pressure). The gateway rejected the request to preserve the completeness invariant.',
    error_provider: 'Upstream model provider returned an error or was unreachable after the gateway accepted the request.',
    error_parse: 'Could not parse the request body as valid JSON or the expected chat/completions shape.',
    error_overloaded: 'Gateway reported overload and declined to take this request. Retry later or scale capacity.',
  };
  return help[d] || `Disposition "${d.replace(/_/g, ' ')}" — see gateway documentation or logs for this category.`;
}

function fmtLatency(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return ms + 'ms';
  return (ms / 1000).toFixed(2) + 's';
}

function fmtTokens(t) {
  if (t == null) return '—';
  return t.toLocaleString();
}

function fmtShortIdA(id, head = 10, tail = 4) {
  if (!id) return '—';
  if (id.length <= head + tail + 1) return id;
  const tailPart = tail > 0 ? id.slice(-tail) : '';
  return id.slice(0, head) + '…' + tailPart;
}

function AttCopyBtn({ text, title }) {
  const [copied, setCopied] = React.useState(false);
  if (!text) return null;
  return (
    <button
      type="button"
      className={`att-copy-btn${copied ? ' copied' : ''}`}
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

// ── Metric bar ─────────────────────────────────────────────
function AttemptsMetricBar({ items, filtered }) {
  const total = filtered.length;
  const allow = filtered.filter(a => {
    const m = dispositionMetaA(a.disposition);
    return m.cat === 'allow';
  }).length;
  const block = filtered.filter(a => dispositionMetaA(a.disposition).cat === 'block').length;
  const err = filtered.filter(a => dispositionMetaA(a.disposition).cat === 'error').length;
  const pctAllow = total ? (allow / total * 100) : 0;

  // Avg latency on allowed requests only
  const allowed = filtered.filter(a => a.latency_ms != null && dispositionMetaA(a.disposition).cat === 'allow');
  const avgLat = allowed.length ? allowed.reduce((s, a) => s + a.latency_ms, 0) / allowed.length : 0;

  return (
    <div className="att-metric-bar">
      <div className="att-metric">
        <div className="att-metric-label">Attempts · in view</div>
        <div className="att-metric-value">{total.toLocaleString()}</div>
        <div className="att-metric-sub">of {items.length.toLocaleString()} total · 24h window</div>
      </div>
      <div className="att-metric accent-green">
        <div className="att-metric-label">Allow rate</div>
        <div className="att-metric-value green">
          {pctAllow.toFixed(1)}<span className="att-metric-unit">%</span>
        </div>
        <div className="att-metric-sub">
          <span className="att-dot-green" />
          {allow.toLocaleString()} allowed / forwarded
        </div>
      </div>
      <div className="att-metric">
        <div className="att-metric-label">Blocked</div>
        <div className="att-metric-value red">{block.toLocaleString()}</div>
        <div className="att-metric-sub">policy · auth · budget · limit</div>
      </div>
      <div className="att-metric">
        <div className="att-metric-label">Errored</div>
        <div className="att-metric-value amber">{err.toLocaleString()}</div>
        <div className="att-metric-sub">provider · parse · overload</div>
      </div>
      <div className="att-metric">
        <div className="att-metric-label">Avg latency · allowed</div>
        <div className="att-metric-value">{Math.round(avgLat)}<span className="att-metric-unit">ms</span></div>
        <div className="att-metric-sub">server-side observed</div>
      </div>
    </div>
  );
}

// ── Filter tabs ────────────────────────────────────────────
function DispositionTabs({ counts, value, onChange }) {
  const tabs = [
    { key: 'all',     label: 'All',     count: counts.total,  cls: 'att-tab-all' },
    { key: 'allow',   label: 'Allow',   count: counts.allow,  cls: 'att-tab-allow' },
    { key: 'block',   label: 'Block',   count: counts.block,  cls: 'att-tab-block' },
    { key: 'error',   label: 'Error',   count: counts.error,  cls: 'att-tab-error' },
  ];
  return (
    <div className="att-tabs">
      {tabs.map(t => (
        <button key={t.key}
          className={`att-tab ${t.cls}${value === t.key ? ' active' : ''}`}
          onClick={() => onChange(t.key)}>
          <span className="att-tab-label">{t.label}</span>
          <span className="att-tab-count">{t.count.toLocaleString()}</span>
        </button>
      ))}
    </div>
  );
}

// ── Expanded row drawer ────────────────────────────────────
function AttemptDrawer({ a }) {
  const meta = dispositionMetaA(a.disposition);
  return (
    <div className={`att-drawer att-drawer-${meta.cat}`}>
      <div className="att-drawer-grid">
        <div className="att-drawer-main">
          <div className="att-drawer-eyebrow">
            <span className={`att-disp-chip ${meta.cls}`}>{meta.label}</span>
            {a.reason && (
              <span className="att-reason">
                <span className="att-reason-lbl">REASON</span>
                <span className="att-reason-val mono">{a.reason}</span>
              </span>
            )}
          </div>
          <p className="att-drawer-help">{dispositionHelp(a.disposition)}</p>

          <div className="att-drawer-meta">
            <div className="att-mrow"><span className="att-mrow-lbl">REQUEST</span>
              <span className="att-mrow-val mono">{a.request_id}</span>
              <AttCopyBtn text={a.request_id} />
            </div>
            <div className="att-mrow"><span className="att-mrow-lbl">EXECUTION</span>
              {a.execution_id
                ? (
                  <>
                    <span className="att-mrow-val mono gold">{a.execution_id}</span>
                    <AttCopyBtn text={a.execution_id} />
                  </>
                )
                : <span className="att-mrow-val att-mrow-empty">— no trace stored for this attempt</span>}
            </div>
            <div className="att-mrow"><span className="att-mrow-lbl">USER</span>
              <span className="att-mrow-val">{a.user}</span>
            </div>
            <div className="att-mrow"><span className="att-mrow-lbl">PROVIDER</span>
              <span className="att-mrow-val mono">{a.provider}</span>
            </div>
            <div className="att-mrow"><span className="att-mrow-lbl">PATH</span>
              <span className="att-mrow-val mono">
                <span className="att-method mono">{a.method}</span> {a.path}
              </span>
            </div>
            <div className="att-mrow"><span className="att-mrow-lbl">MODEL</span>
              <span className="att-mrow-val mono">{a.model_id}</span>
            </div>
          </div>
        </div>

        <aside className="att-drawer-side">
          <div className="att-side-eyebrow">ANALYZER VERDICTS</div>
          <div className="att-analyzer-bar">
            <span className="att-analyzer-score mono">{a.analyzers_passed}<span className="att-analyzer-of">/{a.analyzers_total}</span></span>
            <span className="att-analyzer-label">passed</span>
          </div>
          {a.failed_analyzers.length > 0 ? (
            <ul className="att-analyzer-list">
              {a.failed_analyzers.map((n, i) => (
                <li key={i} className="att-analyzer-fail">
                  <span className="att-analyzer-dot att-analyzer-dot-fail" />
                  <span className="mono">{n}</span>
                  <span className="att-analyzer-tag">FAIL</span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="att-analyzer-all-pass">
              <span className="att-analyzer-dot att-analyzer-dot-pass" />
              All checks passed
            </div>
          )}

          <div className="att-side-divider" />
          <div className="att-side-eyebrow">TIMINGS</div>
          <div className="att-timing-rows">
            <div className="att-timing"><span className="att-timing-lbl">LATENCY</span><span className="att-timing-val mono">{fmtLatency(a.latency_ms)}</span></div>
            <div className="att-timing"><span className="att-timing-lbl">TOKENS · IN</span><span className="att-timing-val mono">{fmtTokens(a.prompt_tokens)}</span></div>
            <div className="att-timing"><span className="att-timing-lbl">TOKENS · OUT</span><span className="att-timing-val mono">{fmtTokens(a.completion_tokens)}</span></div>
            <div className="att-timing"><span className="att-timing-lbl">TOTAL</span><span className="att-timing-val mono">{fmtTokens(a.total_tokens)}</span></div>
          </div>

          <button
            className={`btn-wal ${a.execution_id ? 'btn-primary' : 'btn-ghost'} att-open-exec`}
            disabled={!a.execution_id}
            title={a.execution_id ? 'Open the full execution trace' : 'No execution trace stored for this attempt'}>
            <span>{a.execution_id ? '◆ open execution trace' : 'no trace available'}</span>
            {a.execution_id && <span className="att-open-arrow">→</span>}
          </button>
        </aside>
      </div>
    </div>
  );
}

// ── Main view ──────────────────────────────────────────────
function AttemptsView() {
  const items = window.AttemptsData.items;

  const [q, setQ] = React.useState('');
  const [dispCat, setDispCat] = React.useState('all');   // all / allow / block / error
  const [provider, setProvider] = React.useState('all');
  const [statusBand, setStatusBand] = React.useState('all'); // all / 2xx / 4xx / 5xx
  const [expanded, setExpanded] = React.useState(null);
  const [page, setPage] = React.useState(0);
  const PAGE = 40;

  const providers = React.useMemo(() => {
    const set = new Set(items.map(x => x.provider));
    return ['all', ...Array.from(set)];
  }, [items]);

  // Counts for the disposition tabs — computed over the non-cat filters so they remain meaningful
  const tabSource = React.useMemo(() => {
    return items.filter(a => {
      if (provider !== 'all' && a.provider !== provider) return false;
      if (statusBand !== 'all') {
        if (statusBand === '2xx' && !(a.status_code >= 200 && a.status_code < 300)) return false;
        if (statusBand === '4xx' && !(a.status_code >= 400 && a.status_code < 500)) return false;
        if (statusBand === '5xx' && !(a.status_code >= 500)) return false;
      }
      if (q.trim()) {
        const qq = q.trim().toLowerCase();
        if (!(
          (a.request_id || '').toLowerCase().includes(qq) ||
          (a.user || '').toLowerCase().includes(qq) ||
          (a.model_id || '').toLowerCase().includes(qq) ||
          (a.path || '').toLowerCase().includes(qq) ||
          (a.disposition || '').toLowerCase().includes(qq)
        )) return false;
      }
      return true;
    });
  }, [items, q, provider, statusBand]);

  const counts = React.useMemo(() => {
    const c = { total: tabSource.length, allow: 0, block: 0, error: 0 };
    for (const a of tabSource) {
      const m = dispositionMetaA(a.disposition);
      if (m.cat === 'allow') c.allow++;
      else if (m.cat === 'block') c.block++;
      else if (m.cat === 'error') c.error++;
    }
    return c;
  }, [tabSource]);

  const filtered = React.useMemo(() => {
    if (dispCat === 'all') return tabSource;
    return tabSource.filter(a => dispositionMetaA(a.disposition).cat === dispCat);
  }, [tabSource, dispCat]);

  // Reset paging when filters change
  React.useEffect(() => { setPage(0); setExpanded(null); }, [q, provider, statusBand, dispCat]);

  const pageItems = filtered.slice(page * PAGE, (page + 1) * PAGE);
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE));

  return (
    <div className="att-view">
      <AttemptsMetricBar items={items} filtered={filtered} />

      <div className="card att-list-card">
        <div className="att-head">
          <div className="att-head-title-row">
            <div className="att-head-title">
              <span className="att-title-main">Request Stream</span>
              <span className="att-title-sub mono">
                <span className="att-live-dot" />LIVE · {filtered.length.toLocaleString()} rows
              </span>
            </div>
            <div className="att-head-actions">
              <button className="btn-wal btn-ghost btn-sm">⇣ export csv</button>
              <button className="btn-wal btn-ghost btn-sm">⇣ export jsonl</button>
            </div>
          </div>

          <div className="att-head-filters">
            <DispositionTabs counts={counts} value={dispCat} onChange={setDispCat} />

            <div className="att-search">
              <svg className="att-search-icon" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="1.5">
                <circle cx="7" cy="7" r="5" /><line x1="11" y1="11" x2="14.5" y2="14.5" strokeLinecap="round" />
              </svg>
              <input
                className="att-search-input"
                placeholder="request id · user · model · path · disposition"
                value={q}
                onChange={(e) => setQ(e.target.value)} />
              {q && <button type="button" className="att-search-clear" onClick={() => setQ('')} aria-label="clear">×</button>}
            </div>

            <div className="att-mini-group">
              <span className="att-mini-lbl">provider</span>
              <select className="att-select" value={provider} onChange={(e) => setProvider(e.target.value)}>
                {providers.map(p => <option key={p} value={p}>{p === 'all' ? 'all' : p}</option>)}
              </select>
            </div>

            <div className="att-mini-group">
              <span className="att-mini-lbl">status</span>
              {['all', '2xx', '4xx', '5xx'].map(s => (
                <button key={s}
                  className={`att-filter-chip${statusBand === s ? ' active' : ''}`}
                  onClick={() => setStatusBand(s)}>{s}</button>
              ))}
            </div>
          </div>
        </div>

        <div className="att-table-wrap">
          <table className="att-table">
            <thead>
              <tr>
                <th className="att-th-disp">Disposition</th>
                <th>Request</th>
                <th>User</th>
                <th>Model</th>
                <th>Method · Path</th>
                <th className="att-th-num">Status</th>
                <th className="att-th-num">Latency</th>
                <th className="att-th-num">Tokens</th>
                <th className="att-th-num">Analyzers</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody>
              {pageItems.map(a => {
                const disp = dispositionMetaA(a.disposition);
                const status = statusMeta(a.status_code);
                const isExpanded = expanded === a.request_id;
                const analyzerFail = a.analyzers_passed < a.analyzers_total;

                return (
                  <React.Fragment key={a.request_id}>
                    <tr
                      className={`att-row${isExpanded ? ' expanded' : ''} att-row-${disp.cat}`}
                      onClick={() => setExpanded(isExpanded ? null : a.request_id)}>
                      <td className="att-td-disp">
                        <span className={`att-disp-chip ${disp.cls}`}>{disp.label}</span>
                      </td>
                      <td><span className="mono att-mono-sm">{fmtShortIdA(a.request_id, 10, 4)}</span></td>
                      <td className="att-td-user">{a.user}</td>
                      <td><span className="mono att-mono-sm">{a.model_id}</span></td>
                      <td>
                        <span className="att-method mono">{a.method}</span>
                        <span className="mono att-mono-sm att-path">{a.path}</span>
                      </td>
                      <td className="att-td-num"><span className={`att-status ${status.cls}`}>{status.text}</span></td>
                      <td className="att-td-num mono att-mono-sm">{fmtLatency(a.latency_ms)}</td>
                      <td className="att-td-num mono att-mono-sm">{fmtTokens(a.total_tokens)}</td>
                      <td className="att-td-num">
                        <span className={`att-analyzer-chip${analyzerFail ? ' fail' : ''}`}>
                          ◆ {a.analyzers_passed}/{a.analyzers_total}
                        </span>
                      </td>
                      <td className="att-td-time">{timeAgo(a.timestamp)}</td>
                    </tr>
                    {isExpanded && (
                      <tr className="att-drawer-row">
                        <td colSpan={10}><AttemptDrawer a={a} /></td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })}
              {pageItems.length === 0 && (
                <tr><td colSpan={10} className="att-empty">
                  <div className="att-empty-title">No attempts match</div>
                  <div className="att-empty-sub">Adjust filters above or clear the search.</div>
                </td></tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="att-foot">
          <div className="att-foot-range mono">
            {filtered.length === 0
              ? '0 rows'
              : `${page * PAGE + 1}–${Math.min((page + 1) * PAGE, filtered.length)} of ${filtered.length.toLocaleString()}`}
          </div>
          <div className="att-foot-pager">
            <button className="btn-wal btn-ghost btn-sm" disabled={page === 0} onClick={() => setPage(p => Math.max(0, p - 1))}>◂ prev</button>
            <span className="mono att-foot-page">page {page + 1} / {totalPages}</span>
            <button className="btn-wal btn-ghost btn-sm" disabled={page >= totalPages - 1} onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}>next ▸</button>
          </div>
        </div>
      </div>
    </div>
  );
}

window.AttemptsView = AttemptsView;
