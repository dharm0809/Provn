/* Walacor Gateway — Attempts View (from design zip, wired to real API)
   Dense request-stream with filters, disposition chips, and per-row drawers. */

import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { getAttempts } from '../api';
import { timeAgo } from '../utils';
import '../styles/attempts-v2.css';

// Memoized table row. Extracted so a 3s poll that returns unchanged rows
// skips re-rendering 40+ <tr>s entirely. Custom comparator compares only
// the primitive fields the row reads, plus the isExpanded flag and stable
// handlers. AttemptDrawer stays inside so its expanded content refetches
// on toggle; the row itself remains a stable block in the virtual DOM.
const AttemptRow = React.memo(function AttemptRow({ a, isExpanded, onToggle, onOpenTrace }) {
  const disp = dispositionMetaA(a.disposition);
  const status = statusMeta(a.status_code);
  const analyzersTotal = a.analyzers_total ?? 0;
  const analyzersPassed = a.analyzers_passed ?? analyzersTotal;
  const analyzerFail = analyzersPassed < analyzersTotal;
  return (
    <React.Fragment>
      <tr
        className={`att-row${isExpanded ? ' expanded' : ''} att-row-${disp.cat}`}
        onClick={() => onToggle(a.request_id)}>
        <td className="att-td-disp">
          <span className={`att-disp-chip ${disp.cls}`}>{disp.label}</span>
        </td>
        <td><span className="mono att-mono-sm">{fmtShortIdA(a.request_id, 10, 4)}</span></td>
        <td className="att-td-user">{a.user || '—'}</td>
        <td><span className="mono att-mono-sm">{a.model_id || '—'}</span></td>
        <td>
          <span className="att-method mono">{a.method || 'POST'}</span>
          <span className="mono att-mono-sm att-path">{a.path}</span>
        </td>
        <td className="att-td-num"><span className={`att-status ${status.cls}`}>{status.text}</span></td>
        <td className="att-td-num mono att-mono-sm">{fmtLatency(a.latency_ms)}</td>
        <td className="att-td-num mono att-mono-sm">{fmtTokens(a.total_tokens)}</td>
        <td className="att-td-num">
          <span className={`att-analyzer-chip${analyzerFail ? ' fail' : ''}`}>
            ◆ {analyzersPassed}/{analyzersTotal}
          </span>
        </td>
        <td className="att-td-time">{timeAgo(a.timestamp)}</td>
      </tr>
      {isExpanded && (
        <tr className="att-drawer-row">
          <td colSpan={10}><AttemptDrawer a={a} onOpenTrace={onOpenTrace} /></td>
        </tr>
      )}
    </React.Fragment>
  );
}, (prev, next) => {
  if (prev.isExpanded !== next.isExpanded) return false;
  if (prev.onToggle !== next.onToggle) return false;
  if (prev.onOpenTrace !== next.onOpenTrace) return false;
  const a = prev.a, b = next.a;
  return a.request_id === b.request_id
    && a.disposition === b.disposition
    && a.status_code === b.status_code
    && a.latency_ms === b.latency_ms
    && a.total_tokens === b.total_tokens
    && a.user === b.user
    && a.model_id === b.model_id
    && a.path === b.path
    && a.method === b.method
    && a.timestamp === b.timestamp
    && a.analyzers_total === b.analyzers_total
    && a.analyzers_passed === b.analyzers_passed;
});

function dispositionMetaA(d) {
  if (!d) return { cls: 'att-disp-muted', cat: 'other', label: '—' };
  if (d === 'allowed' || d === 'forwarded') return { cls: 'att-disp-allow', cat: 'allow', label: d === 'forwarded' ? 'FORWARDED' : 'ALLOWED' };
  if (d === 'audit_only_allowed')            return { cls: 'att-disp-allow', cat: 'allow', label: 'AUDIT ONLY' };
  if (d.startsWith('denied_'))                return { cls: 'att-disp-block', cat: 'block', label: d.replace(/_/g, ' ').toUpperCase() };
  if (d.startsWith('error_'))                 return { cls: 'att-disp-error', cat: 'error', label: d.replace(/_/g, ' ').toUpperCase() };
  return { cls: 'att-disp-muted', cat: 'other', label: d.toUpperCase() };
}

function statusMeta(code) {
  if (code == null) return { cls: 'att-status-muted', text: '—' };
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
  return help[d] || `Disposition "${(d || '').replace(/_/g, ' ')}" — see gateway documentation or logs for this category.`;
}

function fmtLatency(ms) {
  if (ms == null) return '—';
  if (ms < 1000) return ms + 'ms';
  return (ms / 1000).toFixed(2) + 's';
}

function fmtTokens(t) {
  if (t == null) return '—';
  return Number(t).toLocaleString();
}

function fmtShortIdA(id, head = 10, tail = 4) {
  if (!id) return '—';
  if (id.length <= head + tail + 1) return id;
  const tailPart = tail > 0 ? id.slice(-tail) : '';
  return id.slice(0, head) + '…' + tailPart;
}

function AttCopyBtn({ text, title }) {
  const [copied, setCopied] = useState(false);
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

function AttemptsMetricBar({ items, filtered }) {
  const total = filtered.length;
  const allow = filtered.filter(a => dispositionMetaA(a.disposition).cat === 'allow').length;
  const block = filtered.filter(a => dispositionMetaA(a.disposition).cat === 'block').length;
  const err = filtered.filter(a => dispositionMetaA(a.disposition).cat === 'error').length;
  const pctAllow = total ? (allow / total * 100) : 0;

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

function AttemptDrawer({ a, onOpenTrace }) {
  const meta = dispositionMetaA(a.disposition);
  const analyzersTotal = a.analyzers_total ?? 0;
  const analyzersPassed = a.analyzers_passed ?? analyzersTotal;
  const failedAnalyzers = a.failed_analyzers || [];

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
              <span className="att-mrow-val mono">{a.request_id || '—'}</span>
              {a.request_id && <AttCopyBtn text={a.request_id} />}
            </div>
            <div className="att-mrow"><span className="att-mrow-lbl">EXECUTION</span>
              {a.execution_id
                ? (
                  <>
                    <span className="att-mrow-val mono gold">{a.execution_id}</span>
                    <AttCopyBtn text={a.execution_id} />
                  </>
                )
                : <span className="att-mrow-val att-mrow-empty" title="Pre-forward exits (auth/policy/budget denials, parse errors, readiness drift) never reach the provider, so no execution record is written by design.">— pre-forward exit · no execution record</span>}
            </div>
            <div className="att-mrow"><span className="att-mrow-lbl">USER</span>
              <span className="att-mrow-val">{a.user || '—'}</span>
            </div>
            <div className="att-mrow"><span className="att-mrow-lbl">PROVIDER</span>
              <span className="att-mrow-val mono">{a.provider || '—'}</span>
            </div>
            <div className="att-mrow"><span className="att-mrow-lbl">PATH</span>
              <span className="att-mrow-val mono">
                <span className="att-method mono">{a.method || 'POST'}</span> {a.path}
              </span>
            </div>
            <div className="att-mrow"><span className="att-mrow-lbl">MODEL</span>
              <span className="att-mrow-val mono">{a.model_id || '—'}</span>
            </div>
          </div>
        </div>

        <aside className="att-drawer-side">
          <div className="att-side-eyebrow">ANALYZER VERDICTS</div>
          <div className="att-analyzer-bar">
            <span className="att-analyzer-score mono">{analyzersPassed}<span className="att-analyzer-of">/{analyzersTotal}</span></span>
            <span className="att-analyzer-label">passed</span>
          </div>
          {failedAnalyzers.length > 0 ? (
            <ul className="att-analyzer-list">
              {failedAnalyzers.map((n, i) => (
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
            title={a.execution_id ? 'Open the full execution trace' : 'No execution trace stored for this attempt'}
            onClick={() => a.execution_id && onOpenTrace && onOpenTrace(a.execution_id)}>
            <span>{a.execution_id ? '◆ open execution trace' : 'no trace available'}</span>
            {a.execution_id && <span className="att-open-arrow">→</span>}
          </button>
        </aside>
      </div>
    </div>
  );
}

export default function Attempts({ navigate, params = {} }) {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);

  const [q, setQ] = useState(params.q || '');
  const [dispCat, setDispCat] = useState('all');
  const [provider, setProvider] = useState('all');
  const [statusBand, setStatusBand] = useState('all');
  const [expanded, setExpanded] = useState(null);
  const [page, setPage] = useState(0);
  const PAGE = 40;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await getAttempts(500, 0, { q, sort: params.sort, order: params.order });
        if (!cancelled) setItems(res.attempts || res.items || []);
      } catch { if (!cancelled) setItems([]); }
      finally { if (!cancelled) setLoading(false); }
    })();
    return () => { cancelled = true; };
  }, [q, params.sort, params.order]);

  const providers = useMemo(() => {
    const set = new Set(items.map(x => x.provider).filter(Boolean));
    return ['all', ...Array.from(set)];
  }, [items]);

  const tabSource = useMemo(() => {
    return items.filter(a => {
      if (provider !== 'all' && a.provider !== provider) return false;
      if (statusBand !== 'all') {
        const code = a.status_code || 0;
        if (statusBand === '2xx' && !(code >= 200 && code < 300)) return false;
        if (statusBand === '4xx' && !(code >= 400 && code < 500)) return false;
        if (statusBand === '5xx' && !(code >= 500)) return false;
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

  const counts = useMemo(() => {
    const c = { total: tabSource.length, allow: 0, block: 0, error: 0 };
    for (const a of tabSource) {
      const m = dispositionMetaA(a.disposition);
      if (m.cat === 'allow') c.allow++;
      else if (m.cat === 'block') c.block++;
      else if (m.cat === 'error') c.error++;
    }
    return c;
  }, [tabSource]);

  const filtered = useMemo(() => {
    if (dispCat === 'all') return tabSource;
    return tabSource.filter(a => dispositionMetaA(a.disposition).cat === dispCat);
  }, [tabSource, dispCat]);

  useEffect(() => { setPage(0); setExpanded(null); }, [q, provider, statusBand, dispCat]);

  const pageItems = filtered.slice(page * PAGE, (page + 1) * PAGE);
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE));

  // Stable refs so AttemptRow's memo comparator correctly identifies
  // unchanged handlers and can skip re-renders of untouched rows.
  const handleOpenTrace = useCallback((executionId) => {
    if (navigate) navigate('execution', { executionId });
  }, [navigate]);

  const handleToggle = useCallback((rid) => {
    setExpanded(prev => prev === rid ? null : rid);
  }, []);

  if (loading) {
    return <div className="card"><div className="att-empty"><div className="att-empty-title">Loading attempts…</div></div></div>;
  }

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
              {pageItems.map(a => (
                <AttemptRow
                  key={a.request_id || `${a.timestamp}-${a.path}`}
                  a={a}
                  isExpanded={expanded === a.request_id}
                  onToggle={handleToggle}
                  onOpenTrace={handleOpenTrace}
                />
              ))}
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
