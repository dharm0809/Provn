import React, { useState, useEffect, useMemo, useCallback } from 'react';
import JsonView from '../components/JsonView.jsx';
import CopyBtn from '../components/CopyBtn.jsx';
import { getConnections } from '../api';
import '../styles/connections.css';

/* ────────────────────────────────────────────────────────────
   Connections — ops surface for silent failures.

   Reads a single /v1/connections snapshot (3s TTL) and renders:
   • 10 subsystem tiles in the fixed spec order
   • events stream (newest first, max 50), clickable into a
     session when session_id is present

   Hooks-before-early-return per project convention (see
   Overview.jsx). Violating this produces React Error #310.
   ──────────────────────────────────────────────────────────── */

const POLL_MS = 3000;
const EMPTY_WINDOW_MINUTES = 10;

/* Fixed tile order per design spec §Tiles. */
const TILE_ORDER = [
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

const TILE_META = {
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

/* ── Mock scenarios removed (backend live at /v1/connections) ────── */

/* eslint-disable no-unused-vars */
const _UNUSED_MOCKS = () => {
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
          { model_id: 'gpt-4o',                supports_tools: true,  auto_disabled: false, since: null },
          { model_id: 'claude-3-5-sonnet',     supports_tools: true,  auto_disabled: false, since: null },
          { model_id: 'llama-3.1-70b',         supports_tools: true,  auto_disabled: false, since: null },
          { model_id: 'mistral-large',         supports_tools: true,  auto_disabled: false, since: null },
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

  /* ── amber scenario: a few fail-opens, JWKS stumble, auto-disabled model ── */
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
            { model_id: 'gpt-4o',                supports_tools: true,  auto_disabled: false, since: null },
            { model_id: 'claude-3-5-sonnet',     supports_tools: true,  auto_disabled: false, since: null },
            { model_id: 'llama-3.1-70b',         supports_tools: false, auto_disabled: true,  since: iso(1800) },
            { model_id: 'mistral-large',         supports_tools: true,  auto_disabled: false, since: null },
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
      { ts: iso(12),  subsystem: 'analyzers',          severity: 'amber', message: 'presidio_pii fail-open · worker timeout after 8000ms', session_id: 'sess_8f3c21a4', execution_id: 'exec_9d12...', request_id: 'req_a1b2', attributes: { analyzer: 'presidio_pii' } },
      { ts: iso(38),  subsystem: 'analyzers',          severity: 'amber', message: 'presidio_pii fail-open · connection refused',          session_id: 'sess_41ec9b11', execution_id: null, request_id: 'req_c3d4', attributes: { analyzer: 'presidio_pii' } },
      { ts: iso(120), subsystem: 'auth',               severity: 'amber', message: 'bootstrap key rotated — stability flag cleared',       session_id: null, execution_id: null, request_id: null, attributes: { probe: 'SEC-01' } },
      { ts: iso(1800),subsystem: 'model_capabilities', severity: 'info',  message: 'llama-3.1-70b auto-disabled tools after 3 tool-call failures', session_id: null, execution_id: null, request_id: null, attributes: { model: 'llama-3.1-70b' } },
      { ts: iso(2100),subsystem: 'readiness',          severity: 'info',  message: 'SEC-01 transient amber cleared',                       session_id: null, execution_id: null, request_id: null, attributes: { check_id: 'SEC-01' } },
    ],
  };

  /* ── red scenario: walacor delivery outage plus analyzer cascade ── */
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
      { ts: iso(4),   subsystem: 'walacor_delivery', severity: 'red',   message: 'envelope PUT failed · connect ETIMEDOUT 10.0.4.12:7443',             session_id: 'sess_b7d441c0', execution_id: 'exec_12cd...', request_id: 'req_e5f6', attributes: { op: 'PUT /envelope' } },
      { ts: iso(11),  subsystem: 'walacor_delivery', severity: 'red',   message: 'envelope PUT failed · connect ETIMEDOUT 10.0.4.12:7443',             session_id: 'sess_a2f391be', execution_id: 'exec_33ef...', request_id: 'req_g7h8', attributes: { op: 'PUT /envelope' } },
      { ts: iso(22),  subsystem: 'tool_loop',        severity: 'amber', message: "python_exec exception swallowed · AttributeError: 'NoneType'…",     session_id: 'sess_c091e2fa', execution_id: 'exec_77ab...', request_id: 'req_i9j0', attributes: { tool: 'python_exec' } },
      { ts: iso(38),  subsystem: 'analyzers',        severity: 'amber', message: 'presidio_pii fail-open · connection refused',                        session_id: 'sess_41ec9b11', execution_id: null, request_id: 'req_c3d4', attributes: { analyzer: 'presidio_pii' } },
      { ts: iso(44),  subsystem: 'walacor_delivery', severity: 'red',   message: 'envelope PUT failed · connect ETIMEDOUT 10.0.4.12:7443',             session_id: 'sess_3f8cd92e', execution_id: 'exec_ab12...', request_id: null, attributes: { op: 'PUT /envelope' } },
      { ts: iso(62),  subsystem: 'walacor_delivery', severity: 'amber', message: 'pending queue crossed threshold · 12 pending writes',                session_id: null, execution_id: null, request_id: null, attributes: { pending_writes: 12 } },
      { ts: iso(120), subsystem: 'auth',             severity: 'amber', message: 'bootstrap key rotated — stability flag cleared',                      session_id: null, execution_id: null, request_id: null, attributes: { probe: 'SEC-01' } },
      { ts: iso(194), subsystem: 'walacor_delivery', severity: 'info',  message: 'last successful envelope · kafka topic lag back to 0',                session_id: 'sess_last_good', execution_id: 'exec_last...', request_id: null, attributes: { op: 'PUT /envelope' } },
      { ts: iso(1800),subsystem: 'model_capabilities', severity: 'info', message: 'llama-3.1-70b auto-disabled tools after 3 tool-call failures',       session_id: null, execution_id: null, request_id: null, attributes: { model: 'llama-3.1-70b' } },
    ],
  };

  return { happy, amber, red };
};
void _UNUSED_MOCKS;
/* eslint-enable no-unused-vars */

/* ── helpers ─────────────────────────────────────────────────────── */

function statusClass(status) {
  if (status === 'green') return 'is-green';
  if (status === 'amber') return 'is-amber';
  if (status === 'red')   return 'is-red';
  return 'is-unknown';
}

function statusPillClass(status) {
  if (status === 'green') return 'pass';
  if (status === 'amber') return 'warn';
  if (status === 'red')   return 'fail';
  return 'dim';
}

function statusLabel(status) {
  if (status === 'green')   return 'HEALTHY';
  if (status === 'amber')   return 'DEGRADED';
  if (status === 'red')     return 'DOWN';
  if (status === 'unknown') return 'UNKNOWN';
  return (status || '—').toUpperCase();
}

function severityPillClass(sev) {
  if (sev === 'red')   return 'fail';
  if (sev === 'amber') return 'warn';
  return 'dim';
}

/* Relative "Nm ago" / "Ns ago". Returns '—' if ts is null. */
function ago(ts) {
  if (!ts) return '—';
  const delta = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
  if (delta < 60)     return `${Math.round(delta)}s ago`;
  if (delta < 3600)   return `${Math.round(delta / 60)}m ago`;
  if (delta < 86400)  return `${Math.round(delta / 3600)}h ago`;
  return `${Math.round(delta / 86400)}d ago`;
}

/* Compact HH:MM:SS local time for event rows. */
function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/* Shortened session id for event rows. */
function shortId(id) {
  if (!id) return '';
  if (id.length <= 14) return id;
  return `${id.slice(0, 10)}…${id.slice(-3)}`;
}

/* ────────────────────────────────────────────────────────────────── */

export default function Connections({ navigate }) {
  /* ── all hooks go ABOVE any early return. ── */

  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState(null);
  const [openTileId, setOpenTileId] = useState(null);
  const [nowTick, setNowTick] = useState(0); // forces "N ago" recomputation

  /* Real backend poll — 3s cadence matches /v1/connections TTL. */
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const data = await getConnections();
        if (!cancelled) { setSnapshot(data); setError(null); setLoading(false); }
      } catch (e) {
        if (!cancelled) { setError(e?.message || 'probe failed'); setLoading(false); }
      }
    }
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  /* Wall-clock tick so "N ago" labels refresh between polls. */
  useEffect(() => {
    const id = setInterval(() => setNowTick((t) => t + 1), POLL_MS);
    return () => clearInterval(id);
  }, []);

  /* Esc closes the drill-in panel. */
  useEffect(() => {
    if (!openTileId) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') setOpenTileId(null); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [openTileId]);

  /* Lock body scroll while panel is open. */
  useEffect(() => {
    if (!openTileId) return undefined;
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = prev; };
  }, [openTileId]);

  const tilesInOrder = useMemo(() => {
    const byId = new Map((snapshot?.tiles || []).map((t) => [t.id, t]));
    return TILE_ORDER.map((id) => byId.get(id) || {
      id, status: 'unknown',
      headline: 'disabled', subline: 'probe unavailable',
      last_change_ts: null, detail: {},
    });
  }, [snapshot]);

  const events = useMemo(() => {
    const all = snapshot?.events || [];
    // defensive sort — backend already guarantees ts desc, but cheap to
    // enforce here so a clock-skewed entry doesn't land mid-stream.
    return [...all]
      .sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime())
      .slice(0, 50);
  }, [snapshot]);

  const openTile = useMemo(
    () => (openTileId ? tilesInOrder.find((t) => t.id === openTileId) : null),
    [openTileId, tilesInOrder],
  );

  /* v4 grafts: counts by status, incident driver, blast radius. */
  const counts = useMemo(() => {
    const out = { green: 0, amber: 0, red: 0, unknown: 0 };
    tilesInOrder.forEach((t) => {
      out[t.status] = (out[t.status] || 0) + 1;
    });
    return out;
  }, [tilesInOrder]);

  const reds   = useMemo(() => tilesInOrder.filter((t) => t.status === 'red'),   [tilesInOrder]);
  const ambers = useMemo(() => tilesInOrder.filter((t) => t.status === 'amber'), [tilesInOrder]);
  const primary = useMemo(() => reds[0] || ambers[0] || null, [reds, ambers]);

  const blastRadius = useMemo(() => {
    if (!primary) return { sessions: [], executions: [], requests: [] };
    const sessions = new Set();
    const executions = new Set();
    const requests = new Set();
    events.forEach((e) => {
      if (e.subsystem === primary.id) {
        if (e.session_id)   sessions.add(e.session_id);
        if (e.execution_id) executions.add(e.execution_id);
        if (e.request_id)   requests.add(e.request_id);
      }
    });
    return {
      sessions:   [...sessions],
      executions: [...executions],
      requests:   [...requests],
    };
  }, [events, primary]);

  const onEventClick = useCallback((ev) => {
    if (!ev?.session_id || typeof navigate !== 'function') return;
    navigate('sessions', { q: ev.session_id });
  }, [navigate]);

  // Touch nowTick so lints don't mark it unused; ago() reads wall clock
  // on every render, so this suffices to refresh the labels.
  void nowTick;

  /* ── render ── */

  return (
    <div className="cx-page">

      <Intro
        snapshot={snapshot}
        loading={loading}
        error={error}
      />

      {snapshot && snapshot.overall_status !== 'green' && (
        <div className="v4-banner-stats-standalone">
          <V4Stat n={counts.red}    label="DOWN"     tone="red" />
          <V4Stat n={counts.amber}  label="DEGRADED" tone="amber" />
          <V4Stat n={counts.green}  label="HEALTHY"  tone="green" />
          <span className="v4-banner-sep" />
          <V4Stat n={blastRadius.sessions.length}   label="SESSIONS HIT"   tone="neutral" />
          <V4Stat n={blastRadius.executions.length} label="EXECUTIONS HIT" tone="neutral" />
          <V4Stat n={blastRadius.requests.length}   label="REQUESTS HIT"   tone="neutral" />
        </div>
      )}

      {counts.red >= 1 && primary && (
        <div className={`v4-banner v4-banner-${primary.status}`}>
          <div className="v4-banner-bar" aria-hidden>
            <span className="v4-banner-bar-fill" />
          </div>
          <div className="v4-banner-main">
            <div className="v4-banner-eyebrow">
              <span className="v4-dia">◆</span>
              <span>Active incident</span>
              <span className="v4-sep">·</span>
              <span>started {primary.last_change_ts ? ago(primary.last_change_ts) : '—'}</span>
            </div>
            <h1 className="v4-banner-title">
              {primary.headline || (TILE_META[primary.id] && TILE_META[primary.id].label) || primary.id}
            </h1>
            <p className="v4-banner-sub">
              {(TILE_META[primary.id] && TILE_META[primary.id].label) || primary.id} — {primary.subline}
            </p>
          </div>
          <div className="v4-banner-side">
            <button
              type="button"
              className="v4-banner-cta"
              onClick={() => setOpenTileId(primary.id)}
            >
              Open probe detail →
            </button>
          </div>
        </div>
      )}

      <div className="cx-section">
        <span className="cx-section-title">◇ Subsystem Status</span>
        <span className="cx-section-hint">10 tiles · click for raw detail</span>
      </div>

      <div className="cx-grid-wrap">
        <div className="cx-grid-inner">
          {tilesInOrder.map((tile, i) => (
            <Tile
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
          <EmptyState minutes={EMPTY_WINDOW_MINUTES} />
        ) : (
          <div className="cx-events-list">
            {events.map((ev, idx) => (
              <EventRow
                key={`${ev.ts}-${idx}`}
                ev={ev}
                onClick={onEventClick}
              />
            ))}
          </div>
        )}
      </div>

      {openTile && (
        <TilePanel
          tile={openTile}
          onClose={() => setOpenTileId(null)}
        />
      )}
    </div>
  );
}

/* ── Intro ───────────────────────────────────────────────────────── */

function Intro({ snapshot, loading, error }) {
  const overall = snapshot?.overall_status || 'unknown';
  const pillClass = statusClass(overall).replace('is-', '');
  const generated = snapshot?.generated_at;

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
          Refreshes every {POLL_MS / 1000} seconds.
        </p>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 10 }}>
        <div className="cx-rollup" aria-live="polite">
          <span className="cx-rollup-label">Rollup</span>
          <span className={`cx-rollup-state ${pillClass}`}>
            <span className="cx-rollup-dot" />
            {statusLabel(overall)}
          </span>
          <span className="cx-rollup-sep" />
          <span className="cx-rollup-meta">
            <span className="cx-tick" />
            {error
              ? 'snapshot failed'
              : loading
                ? 'loading…'
                : `snapshot ${ago(generated)}`}
          </span>
        </div>
      </div>
    </div>
  );
}

/* ── Tile ────────────────────────────────────────────────────────── */

function Tile({ tile, index, onClick }) {
  const meta = TILE_META[tile.id] || { label: tile.id, blurb: '' };
  const pill = statusPillClass(tile.status);

  return (
    <button
      type="button"
      className={`cx-tile ${statusClass(tile.status)}`}
      onClick={onClick}
      aria-label={`${meta.label} — ${statusLabel(tile.status)} — ${tile.headline || meta.blurb}`}
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
          {statusLabel(tile.status)}
        </span>
        <span className="cx-tile-since">
          {tile.last_change_ts ? `changed ${ago(tile.last_change_ts)}` : 'stable'}
        </span>
      </div>
    </button>
  );
}

/* ── Event row ───────────────────────────────────────────────────── */

function EventRow({ ev, onClick }) {
  const clickable = !!ev.session_id;
  const pill = severityPillClass(ev.severity);

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
      <span className="cx-event-time">{fmtTime(ev.ts)}</span>
      <span className="cx-event-sys">{(ev.subsystem || '').replace(/_/g, ' ')}</span>
      <span className={`cx-pill ${pill}`}>
        <span className="cx-pill-dot" />
        {(ev.severity || 'info').toUpperCase()}
      </span>
      <span className="cx-event-message">{ev.message}</span>
      <span className={`cx-event-session${clickable ? '' : ' dim'}`}>
        {clickable ? (
          <>
            {shortId(ev.session_id)}
            <span className="cx-event-session-arrow">→</span>
          </>
        ) : (
          '—'
        )}
      </span>
    </div>
  );
}

/* ── Empty state (reassuring copy per spec) ──────────────────────── */

function EmptyState({ minutes }) {
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

/* ── Tile drill-in slide-over panel ──────────────────────────────── */

function TilePanel({ tile, onClose }) {
  const meta = TILE_META[tile.id] || { label: tile.id, blurb: '' };
  const pill = statusPillClass(tile.status);

  return (
    <>
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
              {statusLabel(tile.status)}
            </span>
          </dd>

          <dt>Headline</dt>
          <dd>{tile.headline || '—'}</dd>

          <dt>Subline</dt>
          <dd>{tile.subline || '—'}</dd>

          <dt>Last change</dt>
          <dd>
            {tile.last_change_ts ? (
              <>
                <span>{ago(tile.last_change_ts)}</span>
                <span style={{ color: 'var(--fg-dim)', fontSize: 10 }}>
                  {new Date(tile.last_change_ts).toISOString()}
                </span>
              </>
            ) : (
              <span style={{ color: 'var(--fg-dim)' }}>stable</span>
            )}
          </dd>

          <dt>Probe id</dt>
          <dd style={{ fontFamily: 'var(--mono)' }}>
            {tile.id}
            <CopyBtn value={tile.id} />
          </dd>
        </dl>

        <div className="cx-panel-body">
          <p className="cx-panel-body-label">◇ Raw detail</p>
          <JsonView data={tile.detail || {}} label="detail" initialOpen />
        </div>

        {V4_RUNBOOK[tile.id] ? (
          <div className="cx-panel-body">
            <p className="cx-panel-body-label">◇ Runbook</p>
            <V4Runbook runbook={V4_RUNBOOK[tile.id]} />
          </div>
        ) : (
          <div className="cx-panel-body">
            <p className="cx-panel-body-label">◇ Runbook</p>
            <p className="cx-runbook-empty">No curated runbook for this subsystem yet.</p>
          </div>
        )}
      </aside>
    </>
  );
}

/* ── v4 grafts: runbook data + components ────────────────────────── */

const V4_RUNBOOK = {
  walacor_delivery: {
    oneliner: 'Walacor cluster is the sole sink for sealed envelopes. When it blocks, compliance pauses.',
    checks: [
      { label: 'TCP reach from gateway → 10.0.4.12:7443',   cmd: 'nc -zv 10.0.4.12 7443' },
      { label: 'Inspect pending-write queue size',           cmd: 'gw cli delivery queue --tail' },
      { label: 'Confirm walacor-cluster k8s pods are Ready', cmd: 'kubectl -n walacor get pods' },
    ],
    escalation: 'If pods Ready but TCP still fails, escalate to #platform-walacor within 5 min.',
  },
  auth: {
    oneliner: 'All inbound API calls depend on JWKS. Unreachable JWKS → full auth outage.',
    checks: [
      { label: 'Verify jwks_uri resolves and returns 200', cmd: 'curl -sI $JWKS_URI' },
      { label: 'Check bootstrap key stability flag',        cmd: 'gw cli auth status' },
    ],
    escalation: 'Rotate bootstrap key; page #sec on-call if stability flag does not recover in 60s.',
  },
  providers: {
    oneliner: 'An upstream LLM outage will cascade to every session routed through that provider.',
    checks: [
      { label: 'Check provider status page',          cmd: '—' },
      { label: 'Force-reroute to healthy providers',  cmd: 'gw cli providers reroute --avoid <id>' },
    ],
    escalation: 'If multi-provider outage: freeze new sessions, post advisory in #customer-ops.',
  },
};

function V4Stat({ n, label, tone }) {
  return (
    <span className={`v4-stat v4-stat-${tone}`}>
      <span className="v4-stat-n">{n}</span>
      <span className="v4-stat-label">{label}</span>
    </span>
  );
}

function V4Runbook({ runbook }) {
  return (
    <div className="v4-runbook">
      <p className="v4-runbook-one">{runbook.oneliner}</p>
      <ol className="v4-runbook-checks">
        {runbook.checks.map((c, i) => (
          <li key={i} className="v4-runbook-check">
            <span className="v4-runbook-n">{i + 1}</span>
            <div className="v4-runbook-body">
              <span className="v4-runbook-label">{c.label}</span>
              {c.cmd && c.cmd !== '—' && (
                <code className="v4-runbook-cmd">
                  <span>{c.cmd}</span>
                  <CopyBtn value={c.cmd} />
                </code>
              )}
            </div>
          </li>
        ))}
      </ol>
      <div className="v4-runbook-escalation">
        <span className="v4-runbook-esc-label">Escalation</span>
        <span className="v4-runbook-esc-text">{runbook.escalation}</span>
      </div>
    </div>
  );
}
