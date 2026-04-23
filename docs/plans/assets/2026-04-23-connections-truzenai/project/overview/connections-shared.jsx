/* Walacor Gateway — Connections shared UI helpers.
   React/ReactDOM are globals (loaded via script tag).
   Every symbol is attached to `window` at the bottom so
   subsequent Babel scripts can use them without bundling. */

/* eslint-disable react/prop-types, no-unused-vars */
const { useState, useEffect, useMemo, useCallback } = React;

/* ── status helpers ──────────────────────────────────────── */
function cxStatusClass(s) {
  if (s === 'green') return 'is-green';
  if (s === 'amber') return 'is-amber';
  if (s === 'red')   return 'is-red';
  return 'is-unknown';
}
function cxPillClass(s) {
  if (s === 'green') return 'pass';
  if (s === 'amber') return 'warn';
  if (s === 'red')   return 'fail';
  return 'dim';
}
function cxStatusLabel(s) {
  if (s === 'green')   return 'HEALTHY';
  if (s === 'amber')   return 'DEGRADED';
  if (s === 'red')     return 'DOWN';
  if (s === 'unknown') return 'UNKNOWN';
  return (s || '—').toUpperCase();
}
function cxSeverityClass(sev) {
  if (sev === 'red')   return 'fail';
  if (sev === 'amber') return 'warn';
  return 'dim';
}
function cxAgo(ts) {
  if (!ts) return '—';
  const d = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
  if (d < 60)    return `${Math.round(d)}s ago`;
  if (d < 3600)  return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
}
function cxFmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function cxShortId(id) {
  if (!id) return '';
  if (id.length <= 14) return id;
  return `${id.slice(0, 10)}…${id.slice(-3)}`;
}

/* Summary by group — used by v2 / v4. */
function cxGroupSummary(tiles, group, GROUP_MAP) {
  const members = tiles.filter((t) => GROUP_MAP[t.id] === group);
  const reds    = members.filter((t) => t.status === 'red').length;
  const ambers  = members.filter((t) => t.status === 'amber').length;
  const worst   = reds > 0 ? 'red' : ambers > 0 ? 'amber' : 'green';
  return { members, reds, ambers, worst };
}

/* Tile-count summary — used by v3 ranked list. */
function cxCountsByStatus(tiles) {
  return {
    red:   tiles.filter((t) => t.status === 'red').length,
    amber: tiles.filter((t) => t.status === 'amber').length,
    green: tiles.filter((t) => t.status === 'green').length,
    unknown: tiles.filter((t) => t.status === 'unknown').length,
  };
}

/* ── JSON highlighter + viewer (reused from v1) ──────────── */
function cxEscapeHtml(s) {
  return s.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]);
}
function cxHighlightJson(src) {
  const safe = cxEscapeHtml(src);
  return safe.replace(
    /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(?:\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      if (/^"/.test(match)) {
        if (/:$/.test(match)) return `<span class="j-key">${match.replace(/:$/, '')}</span><span class="j-punct">:</span>`;
        return `<span class="j-str">${match}</span>`;
      }
      if (/true|false/.test(match)) return `<span class="j-bool">${match}</span>`;
      if (/null/.test(match))       return `<span class="j-null">${match}</span>`;
      return `<span class="j-num">${match}</span>`;
    },
  );
}

function CxCopyBtn({ value, label = 'copy' }) {
  const [copied, setCopied] = useState(false);
  const onClick = useCallback(async (e) => {
    e.stopPropagation();
    if (!value) return;
    try {
      await navigator.clipboard.writeText(String(value));
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch { /* noop */ }
  }, [value]);
  return (
    <button
      type="button"
      className={'exec-copy-btn' + (copied ? ' is-copied' : '')}
      onClick={onClick}
      title={copied ? 'Copied' : 'Copy to clipboard'}
    >
      {copied ? '✓ copied' : label}
    </button>
  );
}

function CxJsonView({ data, label = 'JSON', initialOpen = false }) {
  const [open, setOpen] = useState(initialOpen);
  const pretty = useMemo(() => {
    try { return JSON.stringify(data, null, 2); } catch { return String(data); }
  }, [data]);
  const html = useMemo(() => cxHighlightJson(pretty), [pretty]);
  return (
    <div className="exec-json-wrap">
      <div
        className="exec-json-head"
        role="button"
        tabIndex={0}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setOpen((v) => !v); }}
      >
        <span className="exec-json-head-label">
          {open ? '▾' : '▸'}&nbsp;&nbsp;{label}
        </span>
        <CxCopyBtn value={pretty} />
      </div>
      {open && (
        <pre
          className="exec-json-body"
          dangerouslySetInnerHTML={{ __html: html }}
        />
      )}
    </div>
  );
}

/* ── Shared shell (sidebar + topbar) ─────────────────────── */

const CX_NAV_ITEMS = [
  { key: 'overview', label: 'Overview' },
  { key: 'intel', label: 'Intelligence' },
  { key: 'sessions', label: 'Sessions' },
  { key: 'attempts', label: 'Attempts' },
  { key: 'control', label: 'Control' },
  { key: 'compliance', label: 'Compliance' },
  { key: 'connections', label: 'Connections' },
  { key: 'playground', label: 'Playground' },
];

const CX_NAV_ICONS = {
  overview:   <svg width="18" height="18" viewBox="0 0 18 18" fill="currentColor"><rect x="2" y="2" width="5.5" height="5.5"/><rect x="10.5" y="2" width="5.5" height="5.5"/><rect x="2" y="10.5" width="5.5" height="5.5"/><rect x="10.5" y="10.5" width="5.5" height="5.5"/></svg>,
  intel:      <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M9 1.5l6 3v4.5c0 3-2.5 6-6 7.5-3.5-1.5-6-4.5-6-7.5V4.5l6-3z"/><path d="M9 6v3l2 2"/></svg>,
  sessions:   <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M2 5h14M2 9h14M2 13h10"/></svg>,
  attempts:   <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M1 9h3l2-5 3 10 2-5h6"/></svg>,
  control:    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="M2 4h4m4 0h6M2 9h8m4 0h2M2 14h2m4 0h8"/><circle cx="9" cy="4" r="1.5" fill="currentColor"/><circle cx="13" cy="9" r="1.5" fill="currentColor"/><circle cx="5" cy="14" r="1.5" fill="currentColor"/></svg>,
  compliance: <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M9 1.5L2.5 4.5v5c0 4 3 6.5 6.5 7.5 3.5-1 6.5-3.5 6.5-7.5v-5L9 1.5z"/><path d="M6.5 9l2 2 3.5-3.5"/></svg>,
  connections:<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="4" cy="4" r="1.6"/><circle cx="14" cy="4" r="1.6"/><circle cx="4" cy="14" r="1.6"/><circle cx="14" cy="14" r="1.6"/><circle cx="9" cy="9" r="1.8"/><path d="M5.2 5.2l2.6 2.6M12.8 5.2l-2.6 2.6M5.2 12.8l2.6-2.6M12.8 12.8l-2.6-2.6"/></svg>,
  playground: <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M2 5l5 4-5 4M9 14h7"/></svg>,
};

function CxSidebar({ activeView, setActiveView }) {
  return (
    <aside className="sidebar">
      <div className="sb-brand"><span className="sb-diamond">◆</span></div>
      <nav className="sb-nav">
        {CX_NAV_ITEMS.map((it) => (
          <button
            key={it.key}
            className={`sb-item${it.key === activeView ? ' active' : ''}`}
            title={it.label.toUpperCase()}
            onClick={() => setActiveView(it.key)}
          >
            {CX_NAV_ICONS[it.key]}
          </button>
        ))}
      </nav>
    </aside>
  );
}

function CxTopbar({ theme, setTheme, viewLabel, variant, variantLabel }) {
  const [time, setTime] = useState(new Date());
  useEffect(() => {
    const t = setInterval(() => setTime(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <header className="topbar">
      <div className="topbar-left">
        {viewLabel}
        {variant && (
          <span
            style={{
              marginLeft: 12,
              padding: '2px 8px',
              fontFamily: 'var(--mono)',
              fontSize: 10,
              letterSpacing: '0.14em',
              color: 'var(--gold)',
              border: '1px solid var(--gold-dim)',
              background: 'var(--gold-glow)',
            }}
          >
            {variant} · {variantLabel}
          </span>
        )}
      </div>
      <div className="topbar-right">
        <span className="time">{time.toTimeString().slice(0, 8)} UTC</span>
        <span className="sep">│</span>
        <span>ENFORCED · 4d 12h</span>
        <span className="sep">│</span>
        <button
          className="theme-toggle"
          onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
          title={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`}
        >
          <span className="theme-toggle-track">
            <span className={`theme-toggle-thumb ${theme}`}>
              {theme === 'dark'
                ? <svg viewBox="0 0 16 16" width="10" height="10"><path fill="currentColor" d="M6 0a6 6 0 1 0 6 6A4 4 0 0 1 6 0z"/></svg>
                : <svg viewBox="0 0 16 16" width="10" height="10"><circle cx="8" cy="8" r="3.2" fill="currentColor"/><g stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"><line x1="8" y1="0.8" x2="8" y2="2.6"/><line x1="8" y1="13.4" x2="8" y2="15.2"/><line x1="0.8" y1="8" x2="2.6" y2="8"/><line x1="13.4" y1="8" x2="15.2" y2="8"/><line x1="2.9" y1="2.9" x2="4.2" y2="4.2"/><line x1="11.8" y1="11.8" x2="13.1" y2="13.1"/><line x1="2.9" y1="13.1" x2="4.2" y2="11.8"/><line x1="11.8" y1="4.2" x2="13.1" y2="2.9"/></g></svg>}
            </span>
          </span>
          <span className="theme-toggle-label">{theme === 'dark' ? 'DARK' : 'LIGHT'}</span>
        </button>
      </div>
    </header>
  );
}

function CxViewPlaceholder({ label }) {
  return (
    <div
      style={{
        padding: '60px 32px',
        textAlign: 'center',
        color: 'var(--text-muted)',
        fontFamily: 'var(--mono)',
        fontSize: 12,
        letterSpacing: '0.1em',
      }}
    >
      <div style={{ fontSize: 24, color: 'var(--gold)', marginBottom: 14 }}>◆</div>
      <div style={{ textTransform: 'uppercase', marginBottom: 6 }}>{label}</div>
      <div>Stub — this preview scopes only the Connections page.</div>
    </div>
  );
}

/* ── Scenario picker (shared across v2 / v3 / v4) ────────── */

function CxScenarioPicker({ scenario, setScenario }) {
  return (
    <div className="cx-scenario" role="group" aria-label="Demo scenario">
      {['happy', 'amber', 'red'].map((s) => (
        <button
          key={s}
          type="button"
          className={`cx-scenario-btn${scenario === s ? ' is-active' : ''}`}
          onClick={() => setScenario(s)}
        >
          {s === 'happy' ? 'all-green' : s === 'amber' ? 'mixed-amber' : 'one-red'}
        </button>
      ))}
    </div>
  );
}

/* ── Export to window ──────────────────────────────────── */

Object.assign(window, {
  cxStatusClass, cxPillClass, cxStatusLabel, cxSeverityClass,
  cxAgo, cxFmtTime, cxShortId,
  cxGroupSummary, cxCountsByStatus,
  CxCopyBtn, CxJsonView,
  CxSidebar, CxTopbar, CxViewPlaceholder,
  CxScenarioPicker,
});
