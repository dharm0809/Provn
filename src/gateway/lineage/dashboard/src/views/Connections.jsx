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
  readiness:           { label: 'Readiness',           blurb: 'Gateway self-check rollup' },
  streaming:           { label: 'Streaming',           blurb: 'SSE interruption tally' },
  intelligence_worker: { label: 'Intelligence Worker', blurb: 'Async training queue'   },
};

/* ── Mock scenarios removed (backend live at /v1/connections) ────── */


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
  const [errorStatus, setErrorStatus] = useState(null); // HTTP status if known
  const [openTileId, setOpenTileId] = useState(null);
  const [nowTick, setNowTick] = useState(0); // forces "N ago" recomputation

  /* Real backend poll — 3s cadence. Stops on auth/disabled; exponential
     backoff (3s→30s) on network/5xx so a revoked key or backend outage
     doesn't melt the IP rate limiter. */
  useEffect(() => {
    let cancelled = false;
    let timer = null;
    let delay = POLL_MS;
    const MAX_DELAY = 30000;

    async function poll() {
      try {
        const data = await getConnections();
        if (cancelled) return;
        setSnapshot(data);
        setError(null);
        setErrorStatus(null);
        setLoading(false);
        delay = POLL_MS; // reset backoff on success
        timer = setTimeout(poll, delay);
      } catch (e) {
        if (cancelled) return;
        const status = e?.status ?? null;
        setError(e?.message || 'probe failed');
        setErrorStatus(status);
        setSnapshot(null); // don't show stale-green tiles while errored
        setLoading(false);
        // Terminal states: stop polling. Operator acts via CTA.
        if (status === 401 || status === 403 || status === 503) return;
        // Transient — back off.
        delay = Math.min(delay * 2, MAX_DELAY);
        timer = setTimeout(poll, delay);
      }
    }
    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
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
        errorStatus={errorStatus}
      />

      {(errorStatus === 401 || errorStatus === 403) && (
        <div className="cx-fatal-card cx-fatal-auth">
          <div className="cx-fatal-title">◆ Authentication lost</div>
          <p className="cx-fatal-body">
            Your API key was rejected. Tiles are cleared to avoid showing stale
            green state. Poll has been stopped.
          </p>
          <button
            type="button"
            className="v4-banner-cta"
            onClick={() => {
              try { localStorage.removeItem('cp_api_key'); sessionStorage.removeItem('cp_api_key'); } catch (_e) { /* empty */ }
              window.location.reload();
            }}
          >
            Re-authenticate →
          </button>
        </div>
      )}

      {errorStatus === 503 && (
        <div className="cx-fatal-card cx-fatal-disabled">
          <div className="cx-fatal-title">◆ Feature disabled</div>
          <p className="cx-fatal-body">
            Connections is turned off on this gateway
            (<code>WALACOR_CONNECTIONS_ENABLED=false</code>). Poll has been
            stopped. Restart with the flag enabled to see live state.
          </p>
        </div>
      )}

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

function Intro({ snapshot, loading, error, errorStatus }) {
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
              ? (errorStatus === 401 || errorStatus === 403
                  ? 'unauthorized'
                  : errorStatus === 503
                    ? 'feature disabled'
                    : 'snapshot failed')
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

/* Runbook commands use shell placeholders so they're portable across
   deployments. Set these in your shell before running the commands:
     GATEWAY_URL   — e.g. http://localhost:8000 or https://gw.example.com
     WAL_PATH      — gateway WAL directory, e.g. /tmp/walacor-wal-local
     GATEWAY_LOG   — log file or journalctl unit
     CP_KEY        — control-plane API key */
const V4_RUNBOOK = {
  walacor_delivery: {
    oneliner: 'Walacor cluster is the sole sink for sealed envelopes. When it blocks, compliance pauses.',
    checks: [
      { label: 'Check delivery success rate on this gateway',   cmd: 'curl -s -H "X-API-Key: $CP_KEY" "$GATEWAY_URL/v1/connections" | jq ".tiles[] | select(.id==\\"walacor_delivery\\")"' },
      { label: 'TCP reach gateway → walacor endpoint',          cmd: 'nc -zv $WALACOR_HOST $WALACOR_PORT' },
      { label: 'Tail recent delivery failures in WAL',          cmd: 'sqlite3 $WAL_PATH/gateway.db "select count(*) from gateway_attempts where disposition like \'%walacor%\'"' },
    ],
    escalation: 'If delivery success_rate_60s <95% for 5 min, escalate to #platform-walacor.',
  },
  auth: {
    oneliner: 'All inbound API calls depend on the API key / JWT path. An auth outage is a full outage.',
    checks: [
      { label: 'Check auth state from /v1/connections',         cmd: 'curl -s -H "X-API-Key: $CP_KEY" "$GATEWAY_URL/v1/connections" | jq ".tiles[] | select(.id==\\"auth\\")"' },
      { label: 'Verify JWKS URL resolves (if JWT mode)',        cmd: 'curl -sI "$JWKS_URI"' },
      { label: 'Inspect SEC-01 readiness check (bootstrap key)', cmd: 'curl -s -H "X-API-Key: $CP_KEY" "$GATEWAY_URL/v1/readiness" | jq ".checks[] | select(.id==\\"SEC-01\\")"' },
    ],
    escalation: 'If bootstrap_key_stable=false for >5 min, persist a stable key on disk and restart. Page #sec on-call if JWKS unreachable.',
  },
  providers: {
    oneliner: 'An upstream LLM outage cascades to every session routed through that provider.',
    checks: [
      { label: 'Inspect per-provider error rate and cooldown', cmd: 'curl -s -H "X-API-Key: $CP_KEY" "$GATEWAY_URL/v1/connections" | jq ".tiles[] | select(.id==\\"providers\\")"' },
      { label: 'Ping Ollama (if using local LLM)',             cmd: 'curl -s "$OLLAMA_URL/api/tags" | jq ".models[].name"' },
      { label: 'Check provider status pages',                   cmd: '# openai: https://status.openai.com — anthropic: https://status.anthropic.com' },
    ],
    escalation: 'If multi-provider outage: freeze new sessions, post advisory in #customer-ops.',
  },
  analyzers: {
    oneliner: 'Analyzers fail-open by design — a silent outage means PII/toxicity/Llama Guard verdicts stop landing on records while traffic keeps flowing.',
    checks: [
      { label: 'Inspect per-analyzer fail-open counters',         cmd: 'curl -s -H "X-API-Key: $CP_KEY" "$GATEWAY_URL/v1/connections" | jq ".tiles[] | select(.id==\\"analyzers\\")"' },
      { label: 'Confirm Llama Guard model is available',          cmd: 'curl -s "$OLLAMA_URL/api/tags" | jq ".models[].name" | grep -i llama-guard' },
      { label: 'Tail gateway log for fail-open warnings',         cmd: 'grep -i "fail.open\\|analyzer timeout" "$GATEWAY_LOG" | tail -50' },
    ],
    escalation: 'If fail_opens_60s ≥5 sustained for 10 min, page #ml-safety — records are being written without safety verdicts.',
  },
  tool_loop: {
    oneliner: 'Unhandled exceptions in the tool executor get swallowed and fall back to the raw model reply — users see answers that look fine but never ran the tool.',
    checks: [
      { label: 'Inspect current tool-loop exception rate',        cmd: 'curl -s -H "X-API-Key: $CP_KEY" "$GATEWAY_URL/v1/connections" | jq ".tiles[] | select(.id==\\"tool_loop\\")"' },
      { label: 'Grep the last tool-loop exception trace',         cmd: 'grep -A 20 "_run_active_tool_loop\\|tool_loop" "$GATEWAY_LOG" | tail -40' },
      { label: 'Check registered tools via /health',              cmd: 'curl -s "$GATEWAY_URL/health" | jq ".tools? // .tool_registry?"' },
    ],
    escalation: 'If failure_rate_60s crosses 10% for a single tool, disable it via control plane and open an incident in #gateway-tools.',
  },
  model_capabilities: {
    oneliner: 'When the gateway auto-disables tools on a model, every subsequent request to that model loses web-search/function-calling silently.',
    checks: [
      { label: 'Dump model capability flags',                     cmd: 'curl -s "$GATEWAY_URL/health" | jq .model_capabilities' },
      { label: 'See which models are auto-disabled',              cmd: 'curl -s -H "X-API-Key: $CP_KEY" "$GATEWAY_URL/v1/connections" | jq ".tiles[] | select(.id==\\"model_capabilities\\")"' },
      { label: 'Review last tool-unsupported errors',             cmd: 'grep "tool_unsupported\\|supports_tools=False" "$GATEWAY_LOG" | tail -20' },
    ],
    escalation: 'If a flagship model auto-disables, restart the gateway to clear the cache; if it re-disables within 5 min, page #model-ops.',
  },
  control_plane: {
    oneliner: 'Control-plane drift means in-memory caches no longer match the SQLite store — governance decisions drift from ground truth.',
    checks: [
      { label: 'Check control-plane status',                      cmd: 'curl -s -H "X-API-Key: $CP_KEY" "$GATEWAY_URL/v1/control/status" | jq' },
      { label: 'Policy-cache age + sync loop state',              cmd: 'curl -s -H "X-API-Key: $CP_KEY" "$GATEWAY_URL/v1/connections" | jq ".tiles[] | select(.id==\\"control_plane\\")"' },
      { label: 'Confirm control.db is readable',                  cmd: 'sqlite3 "$WAL_PATH/control.db" ".tables"' },
    ],
    escalation: 'If sync_task_alive=false or cache age >10× interval, restart the gateway; if drift persists, escalate to #gateway-control.',
  },
  readiness: {
    oneliner: 'The 31-check readiness rollup is the single source of truth for whether this gateway should take traffic — red security/integrity checks mean keep-out.',
    checks: [
      { label: 'Fetch the full readiness report',                 cmd: 'curl -s -H "X-API-Key: $CP_KEY" "$GATEWAY_URL/v1/readiness" | jq' },
      { label: 'List only red/amber checks',                      cmd: 'curl -s -H "X-API-Key: $CP_KEY" "$GATEWAY_URL/v1/readiness" | jq \'.checks[] | select(.status!="green")\'' },
      { label: 'Audit recent readiness-drift rows in WAL',        cmd: 'sqlite3 "$WAL_PATH/gateway.db" "select timestamp, reason from gateway_attempts where disposition=\'readiness_degraded\' order by timestamp desc limit 20"' },
    ],
    escalation: 'Any SEC-* or INT-* red → pull this node out of the LB immediately; page #gateway-oncall with the check id in the subject.',
  },
  streaming: {
    oneliner: 'Stream interruptions mean SSE responses are being cut mid-flight — users see truncated completions and audit records miss the final chunk.',
    checks: [
      { label: 'Inspect streaming tile state',                    cmd: 'curl -s -H "X-API-Key: $CP_KEY" "$GATEWAY_URL/v1/connections" | jq ".tiles[] | select(.id==\\"streaming\\")"' },
      { label: 'Count recent stream interruptions in log',        cmd: 'grep "stream interrupted\\|record_stream_interruption" "$GATEWAY_LOG" | tail -30' },
      { label: 'Test a live SSE round-trip',                      cmd: 'curl -N -H "Content-Type: application/json" -d \'{"model":"qwen3:4b","stream":true,"messages":[{"role":"user","content":"hi"}]}\' "$GATEWAY_URL/v1/chat/completions"' },
    ],
    escalation: 'If interruption_rate_60s >5% persistent, check provider health first, then network; escalate to #platform-net if upstream is clean.',
  },
  intelligence_worker: {
    oneliner: 'The ONNX self-learning worker trains off the verdict log asynchronously — if it stalls, adaptive classifiers drift stale but inference keeps serving (fail-open by design).',
    checks: [
      { label: 'Worker heartbeat + queue depth',                  cmd: 'curl -s -H "X-API-Key: $CP_KEY" "$GATEWAY_URL/v1/connections" | jq ".tiles[] | select(.id==\\"intelligence_worker\\")"' },
      { label: 'Verdict-log row count',                           cmd: 'sqlite3 "$WAL_PATH/gateway.db" "select count(*) from verdict_log"' },
      { label: 'Tail the last training run',                      cmd: 'grep -i "intelligence\\|training run" "$GATEWAY_LOG" | tail -20' },
    ],
    escalation: 'If queue_depth keeps climbing or oldest_job_age_s >1h, restart the worker; page #ml-platform only if a second restart does not drain it.',
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
