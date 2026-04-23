/* Walacor Gateway — Connections page
   Preview-mode copy of src/gateway/lineage/dashboard/src/views/Connections.jsx.
   Mirrors the real component 1:1 but with React/ReactDOM as globals so it
   runs inside the standalone HTML shell (no ES modules, no Vite).         */

/* eslint-disable react/prop-types */
const { useState: cxUseState, useEffect: cxUseEffect, useMemo: cxUseMemo, useCallback: cxUseCallback } = React;

const CX_POLL_MS = 3000;
const CX_EMPTY_WINDOW_MINUTES = 10;

const CX_TILE_ORDER = [
  'providers',
  'walacor_delivery',
  'analyzers',
  'tool_loop',
  'model_capabilities',
  'control_plane',
  'auth',
  'readiness',
  'streaming',
  'intelligence_worker',
];

const CX_TILE_META = {
  providers:           { label: 'Providers',           blurb: 'Upstream LLM backends' },
  walacor_delivery:    { label: 'Walacor Delivery',    blurb: 'Sealed write pipeline'  },
  analyzers:           { label: 'Analyzers',           blurb: 'Content safety probes'  },
  tool_loop:           { label: 'Tool Loop',           blurb: 'Tool-executor pipeline' },
  model_capabilities:  { label: 'Model Capabilities',  blurb: 'Per-model feature flags'},
  control_plane:       { label: 'Control Plane',       blurb: 'Policy cache & sync'    },
  auth:                { label: 'Auth',                blurb: 'API-key / JWT / JWKS'   },
  readiness:           { label: 'Readiness',           blurb: 'Phase-26 rollup'        },
  streaming:           { label: 'Streaming',           blurb: 'SSE interruption tally' },
  intelligence_worker: { label: 'Intelligence Worker', blurb: 'Async training queue'   },
};

/* ── Mock scenarios ─────────────────────────────────────────────── */

function cxBuildMocks() {
  const now = Date.now();
  const iso = (sec) => new Date(now - sec * 1000).toISOString();

  const tile = (id, status, headline, subline, detail, last_change = 300) => ({
    id, status, headline, subline,
    last_change_ts: last_change == null ? null : iso(last_change),
    detail,
  });

  const happy = {
    generated_at: iso(0),
    ttl_seconds: 3,
    overall_status: 'green',
    tiles: [
      tile('providers', 'green', '3 providers · 0.4% err', 'openai · anthropic · ollama', {
        providers: {
          openai:    { error_rate_60s: 0.004, cooldown_until: null, last_error: null },
          anthropic: { error_rate_60s: 0.002, cooldown_until: null, last_error: null },
          ollama:    { error_rate_60s: 0.000, cooldown_until: null, last_error: null },
        },
      }, 1800),
      tile('walacor_delivery', 'green', '99.8% success · 0 pending', 'last success 1.2s ago', {
        success_rate_60s: 0.998, pending_writes: 0,
        last_failure: null,
        last_success_ts: iso(1.2),
        time_since_last_success_s: 1.2,
      }, 3600),
      tile('analyzers', 'green', '4 enabled · 0 fail-opens', 'llama_guard · presidio_pii · safety · prompt_guard', {
        analyzers: {
          llama_guard:       { enabled: true, fail_opens_60s: 0, last_fail_open: null },
          presidio_pii:      { enabled: true, fail_opens_60s: 0, last_fail_open: null },
          safety_classifier: { enabled: true, fail_opens_60s: 0, last_fail_open: null },
          prompt_guard:      { enabled: true, fail_opens_60s: 0, last_fail_open: null },
        },
      }, 7200),
      tile('tool_loop', 'green', '42 loops · 0 exceptions', 'failure rate 0.00%', {
        exceptions_60s: 0, last_exception: null, loops_60s: 42, failure_rate_60s: 0.00,
      }, 600),
      tile('model_capabilities', 'green', '8 models · 0 auto-disabled', 'all capabilities intact', {
        auto_disabled_count: 0,
        models: [
          { model_id: 'gpt-4o',            supports_tools: true, auto_disabled: false, since: null },
          { model_id: 'claude-3-5-sonnet', supports_tools: true, auto_disabled: false, since: null },
          { model_id: 'llama-3.1-70b',     supports_tools: true, auto_disabled: false, since: null },
          { model_id: 'mistral-large',     supports_tools: true, auto_disabled: false, since: null },
        ],
      }, null),
      tile('control_plane', 'green', 'v42 · synced 7s ago', 'embedded · 3 policies · 4 attestations', {
        mode: 'embedded',
        policy_cache: { version: 'v42', last_sync_ts: iso(7), age_s: 7, stale: false },
        sync_task_alive: true, attestations_count: 4, policies_count: 3,
      }, 45),
      tile('auth', 'green', 'JWT + API key · JWKS fresh', 'bootstrap key stable', {
        auth_mode: 'both', jwt_configured: true,
        jwks_last_fetch_ts: iso(90), jwks_last_error: null, bootstrap_key_stable: true,
      }, 86400),
      tile('readiness', 'green', 'READY · 0 reds · 0 ambers', '2 degraded rows in last 24h', {
        rollup: 'ready', reds: [], ambers: [], degraded_rows_24h: 2,
      }, 1200),
      tile('streaming', 'green', '18 streams · 0 interruptions', 'interruption rate 0.00%', {
        interruptions_60s: 0, last_interruption: null, streams_60s: 18, interruption_rate_60s: 0.00,
      }, 900),
      tile('intelligence_worker', 'green', 'running · 3 queued', 'oldest job 1.2s · 18,432 rows', {
        running: true, queue_depth: 3, oldest_job_age_s: 1.2, last_error: null,
        last_training_at: iso(3600), verdict_log_rows: 18432,
      }, 240),
    ],
    events: [],
  };

  const amber = {
    ...happy,
    generated_at: iso(0),
    overall_status: 'amber',
    tiles: happy.tiles.map((t) => {
      if (t.id === 'analyzers') {
        return tile('analyzers', 'amber', '4 enabled · 2 fail-opens', 'presidio_pii opened 2× in last 60s', {
          analyzers: {
            llama_guard:       { enabled: true, fail_opens_60s: 0, last_fail_open: null },
            presidio_pii:      { enabled: true, fail_opens_60s: 2, last_fail_open: { ts: iso(38), reason: 'presidio worker timeout' } },
            safety_classifier: { enabled: true, fail_opens_60s: 0, last_fail_open: null },
            prompt_guard:      { enabled: true, fail_opens_60s: 0, last_fail_open: null },
          },
        }, 38);
      }
      if (t.id === 'model_capabilities') {
        return tile('model_capabilities', 'amber', '8 models · 1 auto-disabled', 'llama-3.1-70b: tools disabled', {
          auto_disabled_count: 1,
          models: [
            { model_id: 'gpt-4o',            supports_tools: true,  auto_disabled: false, since: null },
            { model_id: 'claude-3-5-sonnet', supports_tools: true,  auto_disabled: false, since: null },
            { model_id: 'llama-3.1-70b',     supports_tools: false, auto_disabled: true,  since: iso(1800) },
            { model_id: 'mistral-large',     supports_tools: true,  auto_disabled: false, since: null },
          ],
        }, 1800);
      }
      if (t.id === 'auth') {
        return tile('auth', 'amber', 'JWT + API key · bootstrap drift', 'bootstrap key unstable — rotating', {
          auth_mode: 'both', jwt_configured: true,
          jwks_last_fetch_ts: iso(300), jwks_last_error: null, bootstrap_key_stable: false,
        }, 120);
      }
      return t;
    }),
    events: [
      { ts: iso(12),  subsystem: 'analyzers',          severity: 'amber', message: 'presidio_pii fail-open · worker timeout after 8000ms',               session_id: 'sess_8f3c21a4', execution_id: 'exec_9d12', request_id: 'req_a1b2', attributes: { analyzer: 'presidio_pii' } },
      { ts: iso(38),  subsystem: 'analyzers',          severity: 'amber', message: 'presidio_pii fail-open · connection refused',                        session_id: 'sess_41ec9b11', execution_id: null,        request_id: 'req_c3d4', attributes: { analyzer: 'presidio_pii' } },
      { ts: iso(120), subsystem: 'auth',               severity: 'amber', message: 'bootstrap key rotated — stability flag cleared',                      session_id: null,            execution_id: null,        request_id: null,       attributes: { probe: 'SEC-01' } },
      { ts: iso(1800),subsystem: 'model_capabilities', severity: 'info',  message: 'llama-3.1-70b auto-disabled tools after 3 tool-call failures',        session_id: null,            execution_id: null,        request_id: null,       attributes: { model: 'llama-3.1-70b' } },
      { ts: iso(2100),subsystem: 'readiness',          severity: 'info',  message: 'SEC-01 transient amber cleared',                                      session_id: null,            execution_id: null,        request_id: null,       attributes: { check_id: 'SEC-01' } },
    ],
  };

  const red = {
    ...amber,
    generated_at: iso(0),
    overall_status: 'red',
    tiles: amber.tiles.map((t) => {
      if (t.id === 'walacor_delivery') {
        return tile('walacor_delivery', 'red', 'WALACOR UNREACHABLE', '18 pending · last success 3m 14s ago', {
          success_rate_60s: 0.12, pending_writes: 18,
          last_failure: { ts: iso(4), op: 'PUT /envelope', detail: 'connect ETIMEDOUT 10.0.4.12:7443' },
          last_success_ts: iso(194),
          time_since_last_success_s: 194,
        }, 194);
      }
      if (t.id === 'tool_loop') {
        return tile('tool_loop', 'amber', '18 loops · 2 exceptions', 'last: python_exec · AttributeError', {
          exceptions_60s: 2,
          last_exception: { ts: iso(22), tool: 'python_exec', error: "AttributeError: 'NoneType' object has no attribute 'context'" },
          loops_60s: 18, failure_rate_60s: 0.11,
        }, 22);
      }
      return t;
    }),
    events: [
      { ts: iso(4),   subsystem: 'walacor_delivery', severity: 'red',   message: 'envelope PUT failed · connect ETIMEDOUT 10.0.4.12:7443',             session_id: 'sess_b7d441c0', execution_id: 'exec_12cd', request_id: 'req_e5f6', attributes: { op: 'PUT /envelope' } },
      { ts: iso(11),  subsystem: 'walacor_delivery', severity: 'red',   message: 'envelope PUT failed · connect ETIMEDOUT 10.0.4.12:7443',             session_id: 'sess_a2f391be', execution_id: 'exec_33ef', request_id: 'req_g7h8', attributes: { op: 'PUT /envelope' } },
      { ts: iso(22),  subsystem: 'tool_loop',        severity: 'amber', message: "python_exec exception swallowed · AttributeError: 'NoneType'…",     session_id: 'sess_c091e2fa', execution_id: 'exec_77ab', request_id: 'req_i9j0', attributes: { tool: 'python_exec' } },
      { ts: iso(38),  subsystem: 'analyzers',        severity: 'amber', message: 'presidio_pii fail-open · connection refused',                        session_id: 'sess_41ec9b11', execution_id: null,        request_id: 'req_c3d4', attributes: { analyzer: 'presidio_pii' } },
      { ts: iso(44),  subsystem: 'walacor_delivery', severity: 'red',   message: 'envelope PUT failed · connect ETIMEDOUT 10.0.4.12:7443',             session_id: 'sess_3f8cd92e', execution_id: 'exec_ab12', request_id: null,       attributes: { op: 'PUT /envelope' } },
      { ts: iso(62),  subsystem: 'walacor_delivery', severity: 'amber', message: 'pending queue crossed threshold · 12 pending writes',                 session_id: null,            execution_id: null,        request_id: null,       attributes: { pending_writes: 12 } },
      { ts: iso(120), subsystem: 'auth',             severity: 'amber', message: 'bootstrap key rotated — stability flag cleared',                      session_id: null,            execution_id: null,        request_id: null,       attributes: { probe: 'SEC-01' } },
      { ts: iso(194), subsystem: 'walacor_delivery', severity: 'info',  message: 'last successful envelope · kafka topic lag back to 0',                session_id: 'sess_last_good',execution_id: 'exec_last', request_id: null,       attributes: { op: 'PUT /envelope' } },
      { ts: iso(1800),subsystem: 'model_capabilities', severity: 'info', message: 'llama-3.1-70b auto-disabled tools after 3 tool-call failures',       session_id: null,            execution_id: null,        request_id: null,       attributes: { model: 'llama-3.1-70b' } },
    ],
  };

  return { happy, amber, red };
}

/* ── helpers ─────────────────────────────────────────────────────── */

function cxStatusClass(status) {
  if (status === 'green') return 'is-green';
  if (status === 'amber') return 'is-amber';
  if (status === 'red')   return 'is-red';
  return 'is-unknown';
}
function cxStatusPillClass(status) {
  if (status === 'green') return 'pass';
  if (status === 'amber') return 'warn';
  if (status === 'red')   return 'fail';
  return 'dim';
}
function cxStatusLabel(status) {
  if (status === 'green')   return 'HEALTHY';
  if (status === 'amber')   return 'DEGRADED';
  if (status === 'red')     return 'DOWN';
  if (status === 'unknown') return 'UNKNOWN';
  return (status || '—').toUpperCase();
}
function cxSeverityPillClass(sev) {
  if (sev === 'red')   return 'fail';
  if (sev === 'amber') return 'warn';
  return 'dim';
}
function cxAgo(ts) {
  if (!ts) return '—';
  const delta = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
  if (delta < 60)    return `${Math.round(delta)}s ago`;
  if (delta < 3600)  return `${Math.round(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)}h ago`;
  return `${Math.round(delta / 86400)}d ago`;
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

/* ── tiny JSON highlighter (mirrors JsonView.jsx) ────────────────── */

function cxEscapeHtml(s) {
  return s.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]);
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
    }
  );
}

/* ── JsonView + CopyBtn (mirrors src/components) ─────────────────── */

function CxCopyBtn({ value, label = 'copy' }) {
  const [copied, setCopied] = cxUseState(false);
  const onClick = cxUseCallback(async (e) => {
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
  const [open, setOpen] = cxUseState(initialOpen);
  const pretty = cxUseMemo(() => {
    try { return JSON.stringify(data, null, 2); } catch { return String(data); }
  }, [data]);
  const html = cxUseMemo(() => cxHighlightJson(pretty), [pretty]);
  return (
    <div className="exec-json-wrap">
      <div
        className="exec-json-head"
        onClick={() => setOpen(v => !v)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setOpen(v => !v); }}
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

/* ────────────────────────────────────────────────────────────────── */

function ConnectionsView({ navigate }) {
  /* Every hook lives ABOVE any early return. */

  const mocks = cxUseMemo(() => cxBuildMocks(), []);
  const [scenario, setScenario] = cxUseState('amber');
  const [snapshot, setSnapshot] = cxUseState(() => mocks.amber);
  const [loading, setLoading] = cxUseState(false);
  const [error, setError]     = cxUseState(null);
  const [openTileId, setOpenTileId] = cxUseState(null);
  const [, setNowTick] = cxUseState(0);

  cxUseEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.resolve(mocks[scenario]).then((s) => {
      if (cancelled) return;
      setSnapshot(s);
      setLoading(false);
    }).catch((e) => {
      if (cancelled) return;
      setError(String(e && e.message ? e.message : e));
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [scenario, mocks]);

  cxUseEffect(() => {
    const id = setInterval(() => setNowTick((t) => t + 1), CX_POLL_MS);
    return () => clearInterval(id);
  }, []);

  cxUseEffect(() => {
    if (!openTileId) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') setOpenTileId(null); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [openTileId]);

  cxUseEffect(() => {
    if (!openTileId) return undefined;
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = prev; };
  }, [openTileId]);

  const tilesInOrder = cxUseMemo(() => {
    const byId = new Map((snapshot && snapshot.tiles || []).map((t) => [t.id, t]));
    return CX_TILE_ORDER.map((id) => byId.get(id) || {
      id, status: 'unknown',
      headline: 'disabled', subline: 'probe unavailable',
      last_change_ts: null, detail: {},
    });
  }, [snapshot]);

  const events = cxUseMemo(() => {
    const all = (snapshot && snapshot.events) || [];
    return [...all]
      .sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime())
      .slice(0, 50);
  }, [snapshot]);

  const openTile = cxUseMemo(
    () => (openTileId ? tilesInOrder.find((t) => t.id === openTileId) : null),
    [openTileId, tilesInOrder],
  );

  const onEventClick = cxUseCallback((ev) => {
    if (!ev || !ev.session_id) return;
    if (typeof navigate === 'function') navigate('sessions', { q: ev.session_id });
  }, [navigate]);

  return (
    <div className="cx-page">
      <CxIntro
        snapshot={snapshot}
        scenario={scenario}
        setScenario={setScenario}
        loading={loading}
        error={error}
      />

      <div className="cx-section">
        <span className="cx-section-title">◇ Subsystem Status</span>
        <span className="cx-section-hint">10 tiles · click for raw detail</span>
      </div>

      <div className="cx-grid-wrap">
        <div className="cx-grid-inner">
          {tilesInOrder.map((tile, i) => (
            <CxTile
              key={tile.id}
              tile={tile}
              index={i + 1}
              onClick={() => setOpenTileId(tile.id)}
            />
          ))}
        </div>
      </div>

      <div className="cx-section">
        <span className="cx-section-title">◇ Recent Events</span>
        <span className="cx-section-hint">
          newest first · max 50 · {events.length} shown
        </span>
      </div>

      <div className="cx-events">
        <div className="cx-events-head">
          <div className="cx-events-head-left">
            <span className="cx-events-title">Degradation stream</span>
            <span className="cx-events-live">
              <span className="cx-events-live-dot" /> LIVE
            </span>
          </div>
          <span className="cx-events-count">
            {events.length === 0
              ? 'clean'
              : `${events.length} event${events.length === 1 ? '' : 's'}`}
          </span>
        </div>

        {events.length === 0 ? (
          <CxEmptyState minutes={CX_EMPTY_WINDOW_MINUTES} />
        ) : (
          <div className="cx-events-list">
            {events.map((ev, idx) => (
              <CxEventRow
                key={`${ev.ts}-${idx}`}
                ev={ev}
                onClick={onEventClick}
              />
            ))}
          </div>
        )}
      </div>

      {openTile && (
        <CxTilePanel
          tile={openTile}
          onClose={() => setOpenTileId(null)}
        />
      )}
    </div>
  );
}

/* ── Intro ───────────────────────────────────────────────────────── */

function CxIntro({ snapshot, scenario, setScenario, loading, error }) {
  const overall = (snapshot && snapshot.overall_status) || 'unknown';
  const pillClass = cxStatusClass(overall).replace('is-', '');
  const generated = snapshot && snapshot.generated_at;

  return (
    <div className="cx-intro">
      <div className="cx-intro-body">
        <div className="cx-intro-eyebrow">
          <span className="cx-dia">◆</span>
          <span>Connections</span>
          <span className="cx-eyebrow-sep">·</span>
          <span>Silent-failure surface</span>
        </div>
        <h1>Is anything silently broken right now?</h1>
        <p>
          Ten subsystem probes, one scrollback of recent degradation events.
          Everything here is derived from live in-process state —
          no historical storage, no alerting, no per-user filtering.
          Refreshes every {CX_POLL_MS / 1000} seconds.
        </p>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 10 }}>
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

        <div className="cx-rollup" aria-live="polite">
          <span className="cx-rollup-label">Rollup</span>
          <span className={`cx-rollup-state ${pillClass}`}>
            <span className="cx-rollup-dot" />
            {cxStatusLabel(overall)}
          </span>
          <span className="cx-rollup-sep" />
          <span className="cx-rollup-meta">
            <span className="cx-tick" />
            {error
              ? 'snapshot failed'
              : loading
                ? 'loading…'
                : `snapshot ${cxAgo(generated)}`}
          </span>
        </div>
      </div>
    </div>
  );
}

/* ── Tile ────────────────────────────────────────────────────────── */

function CxTile({ tile, index, onClick }) {
  const meta = CX_TILE_META[tile.id] || { label: tile.id, blurb: '' };
  const pill = cxStatusPillClass(tile.status);

  return (
    <button
      type="button"
      className={`cx-tile ${cxStatusClass(tile.status)}`}
      onClick={onClick}
      aria-label={`${meta.label} — ${cxStatusLabel(tile.status)} — ${tile.headline || meta.blurb}`}
    >
      <div className="cx-tile-head">
        <span className="cx-tile-title">{meta.label}</span>
        <span className="cx-tile-index">{String(index).padStart(2, '0')}</span>
      </div>

      <div className="cx-tile-headline">{tile.headline || meta.blurb}</div>
      <div className="cx-tile-subline">{tile.subline || meta.blurb}</div>

      <div className="cx-tile-foot">
        <span className={`cx-pill ${pill}${tile.status === 'red' ? ' solid' : ''}`}>
          <span className="cx-pill-dot" />
          {cxStatusLabel(tile.status)}
        </span>
        <span className="cx-tile-since">
          {tile.last_change_ts ? `changed ${cxAgo(tile.last_change_ts)}` : 'stable'}
        </span>
      </div>
    </button>
  );
}

/* ── Event row ───────────────────────────────────────────────────── */

function CxEventRow({ ev, onClick }) {
  const clickable = !!ev.session_id;
  const pill = cxSeverityPillClass(ev.severity);

  return (
    <div
      className={`cx-event-row${clickable ? ' is-clickable' : ''}`}
      onClick={clickable ? () => onClick(ev) : undefined}
      role={clickable ? 'button' : undefined}
      tabIndex={clickable ? 0 : undefined}
      onKeyDown={clickable
        ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(ev); } }
        : undefined}
      title={clickable ? `Open session ${ev.session_id}` : undefined}
    >
      <span className="cx-event-time">{cxFmtTime(ev.ts)}</span>
      <span className="cx-event-sys">{(ev.subsystem || '').replace(/_/g, ' ')}</span>
      <span className={`cx-pill ${pill}`}>
        <span className="cx-pill-dot" />
        {(ev.severity || 'info').toUpperCase()}
      </span>
      <span className="cx-event-message">{ev.message}</span>
      <span className={`cx-event-session${clickable ? '' : ' dim'}`}>
        {clickable ? (
          <React.Fragment>
            {cxShortId(ev.session_id)}
            <span className="cx-event-session-arrow">→</span>
          </React.Fragment>
        ) : (
          '—'
        )}
      </span>
    </div>
  );
}

/* ── Empty state ─────────────────────────────────────────────────── */

function CxEmptyState({ minutes }) {
  return (
    <div className="cx-empty">
      <div className="cx-empty-icon">✓</div>
      <div className="cx-empty-title">No silent failures in the last {minutes} minutes.</div>
      <div className="cx-empty-sub">
        Fail-open analyzers, dropped Walacor writes, and swallowed tool exceptions
        would all show up here. Nothing so far.
      </div>
    </div>
  );
}

/* ── Slide-over panel ────────────────────────────────────────────── */

function CxTilePanel({ tile, onClose }) {
  const meta = CX_TILE_META[tile.id] || { label: tile.id, blurb: '' };
  const pill = cxStatusPillClass(tile.status);

  return (
    <React.Fragment>
      <button
        type="button"
        className="cx-overlay"
        aria-label="Close panel"
        onClick={onClose}
      />
      <aside
        className="cx-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cx-panel-title"
      >
        <header className="cx-panel-head">
          <div className="cx-panel-head-left">
            <div className="cx-panel-eyebrow">
              <span className="cx-dia">◆</span>
              <span>Subsystem</span>
              <span>·</span>
              <span>{tile.id}</span>
            </div>
            <h2 id="cx-panel-title" className="cx-panel-title">{meta.label}</h2>
            <p className="cx-panel-sub">{meta.blurb}</p>
          </div>
          <button
            type="button"
            className="cx-panel-close"
            onClick={onClose}
            aria-label="Close"
          >
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <path d="M2 2l10 10M12 2L2 12" />
            </svg>
          </button>
        </header>

        <dl className="cx-panel-meta">
          <dt>Status</dt>
          <dd>
            <span className={`cx-pill ${pill}${tile.status === 'red' ? ' solid' : ''}`}>
              <span className="cx-pill-dot" />
              {cxStatusLabel(tile.status)}
            </span>
          </dd>

          <dt>Headline</dt>
          <dd>{tile.headline || '—'}</dd>

          <dt>Subline</dt>
          <dd>{tile.subline || '—'}</dd>

          <dt>Last change</dt>
          <dd>
            {tile.last_change_ts ? (
              <React.Fragment>
                <span>{cxAgo(tile.last_change_ts)}</span>
                <span style={{ color: 'var(--text-muted)', fontSize: 10 }}>
                  {new Date(tile.last_change_ts).toISOString()}
                </span>
              </React.Fragment>
            ) : (
              <span style={{ color: 'var(--text-muted)' }}>stable</span>
            )}
          </dd>

          <dt>Probe id</dt>
          <dd style={{ fontFamily: 'var(--mono)' }}>
            {tile.id}
            <CxCopyBtn value={tile.id} />
          </dd>
        </dl>

        <div className="cx-panel-body">
          <p className="cx-panel-body-label">◇ Raw detail</p>
          <CxJsonView data={tile.detail || {}} label="detail" initialOpen />
        </div>
      </aside>
    </React.Fragment>
  );
}

window.ConnectionsView = ConnectionsView;
