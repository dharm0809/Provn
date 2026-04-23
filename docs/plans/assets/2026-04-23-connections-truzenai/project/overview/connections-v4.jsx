/* Walacor Gateway — Connections v4 (Incident-driven).
   The page's shape depends on the overall_status:
     • green  → a minimal health bar + events list. Boring on purpose.
     • amber  → adds a compact "degradation board" above events.
     • red    → full incident cockpit:
                banner, blast-radius, recent-changes lane, runbook,
                then a subsystem checklist at the bottom (not tiles).
   This is the most opinionated layout — it says "the page is an
   alert, not a dashboard". */

/* eslint-disable react/prop-types */
const { useState, useEffect, useMemo, useCallback } = React;

const V4_POLL_MS = 3000;

/* Runbook snippets per subsystem — shown only when that subsystem is
   the incident driver. Hand-written because runbooks are curation
   work, not data. */
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

function V4View({ navigate }) {
  const { TILE_ORDER, TILE_META, TILE_GROUP, RECENT_CHANGES, scenarios } = window.ConnectionsMocks;

  const [scenario, setScenario] = useState('red');
  const [snapshot, setSnapshot] = useState(() => scenarios.red);
  const [openTileId, setOpenTileId] = useState(null);
  const [showAllSubsystems, setShowAllSubsystems] = useState(false);
  const [, setTick] = useState(0);

  useEffect(() => { setSnapshot(scenarios[scenario]); }, [scenario, scenarios]);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), V4_POLL_MS);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    if (!openTileId) return undefined;
    const onKey = (e) => { if (e.key === 'Escape') setOpenTileId(null); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [openTileId]);

  const tilesInOrder = useMemo(() => {
    const byId = new Map((snapshot.tiles || []).map((t) => [t.id, t]));
    return TILE_ORDER.map((id) => byId.get(id) || {
      id, status: 'unknown', headline: 'disabled', subline: 'probe unavailable',
      last_change_ts: null, detail: {},
    });
  }, [snapshot, TILE_ORDER]);

  const counts = useMemo(() => cxCountsByStatus(tilesInOrder), [tilesInOrder]);

  const reds   = useMemo(() => tilesInOrder.filter((t) => t.status === 'red'), [tilesInOrder]);
  const ambers = useMemo(() => tilesInOrder.filter((t) => t.status === 'amber'), [tilesInOrder]);
  const primary = reds[0] || ambers[0] || null; // incident driver

  const events = useMemo(() => {
    const all = (snapshot && snapshot.events) || [];
    return [...all].sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime()).slice(0, 50);
  }, [snapshot]);

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

  const primaryEvents = useMemo(
    () => (primary ? events.filter((e) => e.subsystem === primary.id) : []),
    [events, primary],
  );

  const openTile = useMemo(
    () => (openTileId ? tilesInOrder.find((t) => t.id === openTileId) : null),
    [openTileId, tilesInOrder],
  );

  const onEventClick = useCallback((ev) => {
    if (ev && ev.session_id && typeof navigate === 'function') {
      navigate('sessions', { q: ev.session_id });
    }
  }, [navigate]);

  const overall = snapshot.overall_status || 'unknown';
  const mode = overall; // 'green' | 'amber' | 'red' | 'unknown'
  const runbook = primary ? V4_RUNBOOK[primary.id] : null;

  const sinceIncident = primary && primary.last_change_ts
    ? cxAgo(primary.last_change_ts)
    : '—';

  return (
    <div className={`cx-page v4-page v4-mode-${mode}`}>

      {/* === GREEN HEADER (calm) ============================== */}
      {mode === 'green' && (
        <div className="v4-calm">
          <div className="v4-calm-strip">
            <span className="v4-calm-dot" />
            <div className="v4-calm-msg">
              <span className="v4-calm-title">No active incidents.</span>
              <span className="v4-calm-sub">
                All {counts.green} probes reporting healthy · snapshot {cxAgo(snapshot.generated_at)}
              </span>
            </div>
            <CxScenarioPicker scenario={scenario} setScenario={setScenario} />
          </div>
        </div>
      )}

      {/* === RED / AMBER COCKPIT BANNER ======================= */}
      {primary && (
        <div className={`v4-banner v4-banner-${primary.status}`}>
          <div className="v4-banner-bar" aria-hidden>
            <span className="v4-banner-bar-fill" />
          </div>

          <div className="v4-banner-main">
            <div className="v4-banner-eyebrow">
              <span className="v4-dia">◆</span>
              <span>Active incident</span>
              <span className="v4-sep">·</span>
              <span>started {sinceIncident}</span>
              <span className="v4-sep">·</span>
              <span>{TILE_GROUP[primary.id]} plane</span>
            </div>

            <h1 className="v4-banner-title">
              {primary.headline || TILE_META[primary.id].label}
            </h1>
            <p className="v4-banner-sub">
              {TILE_META[primary.id].label} — {primary.subline}
            </p>

            <div className="v4-banner-stats">
              <V4Stat n={counts.red}    label="DOWN"     tone="red" />
              <V4Stat n={counts.amber}  label="DEGRADED" tone="amber" />
              <V4Stat n={counts.green}  label="HEALTHY"  tone="green" />
              <span className="v4-banner-sep" />
              <V4Stat n={blastRadius.sessions.length}   label="SESSIONS HIT"   tone="neutral" />
              <V4Stat n={blastRadius.executions.length} label="EXECUTIONS HIT" tone="neutral" />
              <V4Stat n={blastRadius.requests.length}   label="REQUESTS HIT"   tone="neutral" />
            </div>
          </div>

          <div className="v4-banner-side">
            <CxScenarioPicker scenario={scenario} setScenario={setScenario} />
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

      {/* === RED: cockpit columns ============================= */}
      {mode === 'red' && primary && (
        <div className="v4-cockpit">
          {/* LEFT — incident stream */}
          <section className="v4-col v4-col-stream">
            <header className="v4-col-head">
              <span className="v4-col-mark" aria-hidden>◇</span>
              <h2 className="v4-col-title">Incident events</h2>
              <span className="v4-col-hint">
                {primary.id.replace(/_/g, ' ')} · {primaryEvents.length} event{primaryEvents.length === 1 ? '' : 's'}
              </span>
            </header>
            <div className="v4-stream">
              {primaryEvents.length === 0 ? (
                <div className="v4-stream-empty">No events emitted for this subsystem yet.</div>
              ) : primaryEvents.map((e, i) => (
                <V4StreamRow key={i} ev={e} onClick={onEventClick} />
              ))}
            </div>
          </section>

          {/* MIDDLE — blast radius + recent changes */}
          <section className="v4-col v4-col-context">
            <header className="v4-col-head">
              <span className="v4-col-mark" aria-hidden>◇</span>
              <h2 className="v4-col-title">Blast radius</h2>
              <span className="v4-col-hint">sessions · executions · requests</span>
            </header>
            <V4Radius blastRadius={blastRadius} onSessionClick={(sid) => onEventClick({ session_id: sid })} />

            <header className="v4-col-head v4-col-head-2">
              <span className="v4-col-mark" aria-hidden>◇</span>
              <h2 className="v4-col-title">Recent changes</h2>
              <span className="v4-col-hint">what shipped before this went red</span>
            </header>
            <V4Changes changes={RECENT_CHANGES} sinceIncident={primary.last_change_ts} />
          </section>

          {/* RIGHT — runbook */}
          <section className="v4-col v4-col-runbook">
            <header className="v4-col-head">
              <span className="v4-col-mark" aria-hidden>◇</span>
              <h2 className="v4-col-title">Runbook</h2>
              <span className="v4-col-hint">{primary.id}</span>
            </header>
            {runbook ? (
              <V4Runbook runbook={runbook} />
            ) : (
              <div className="v4-runbook-empty">
                No curated runbook for this subsystem yet. Open the probe
                detail to inspect raw state.
              </div>
            )}
          </section>
        </div>
      )}

      {/* === AMBER: compact degradation board ================ */}
      {mode === 'amber' && (
        <div className="v4-amber-board">
          <div className="cx-section">
            <span className="cx-section-title">◇ Degradations</span>
            <span className="cx-section-hint">
              {ambers.length} amber · other subsystems healthy
            </span>
          </div>
          <div className="v4-amber-grid">
            {ambers.map((tile) => (
              <V4AmberCard key={tile.id} tile={tile} onClick={() => setOpenTileId(tile.id)} />
            ))}
          </div>
        </div>
      )}

      {/* === Events stream (always visible, below cockpit) === */}
      <div className="cx-section v4-events-section">
        <span className="cx-section-title">◇ {mode === 'green' ? 'All Events' : 'Full Event Stream'}</span>
        <span className="cx-section-hint">newest first · max 50 · {events.length} shown</span>
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
            {events.length === 0 ? 'clean' : `${events.length} event${events.length === 1 ? '' : 's'}`}
          </span>
        </div>
        {events.length === 0 ? (
          <div className="cx-empty">
            <div className="cx-empty-icon">✓</div>
            <div className="cx-empty-title">No silent failures in the last 10 minutes.</div>
            <div className="cx-empty-sub">Fail-open analyzers, dropped writes, and swallowed tool exceptions would show up here.</div>
          </div>
        ) : (
          <div className="cx-events-list">
            {events.map((ev, idx) => (
              <V4EventRow key={`${ev.ts}-${idx}`} ev={ev} onClick={onEventClick} />
            ))}
          </div>
        )}
      </div>

      {/* === Subsystem checklist (footer, collapsible in red) === */}
      <div className="v4-footer">
        <button
          type="button"
          className="v4-footer-toggle"
          onClick={() => setShowAllSubsystems((v) => !v)}
          aria-expanded={showAllSubsystems}
        >
          <span className="v4-footer-caret">{showAllSubsystems ? '▾' : '▸'}</span>
          <span className="v4-footer-label">
            All {TILE_ORDER.length} subsystems
          </span>
          <span className="v4-footer-summary">
            {counts.red} down · {counts.amber} degraded · {counts.green} healthy
          </span>
        </button>
        {showAllSubsystems && (
          <div className="v4-checklist">
            {tilesInOrder.map((tile) => (
              <button
                key={tile.id}
                type="button"
                className={`v4-check ${cxStatusClass(tile.status)}`}
                onClick={() => setOpenTileId(tile.id)}
              >
                <span className="v4-check-dot" />
                <span className="v4-check-label">{TILE_META[tile.id].label}</span>
                <span className="v4-check-head">{tile.headline}</span>
                <span className="v4-check-since">
                  {tile.last_change_ts ? cxAgo(tile.last_change_ts) : 'stable'}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>

      {openTile && <V4Panel tile={openTile} onClose={() => setOpenTileId(null)} TILE_META={TILE_META} />}
    </div>
  );
}

/* ── small stat bubble in banner ───────────── */
function V4Stat({ n, label, tone }) {
  return (
    <span className={`v4-stat v4-stat-${tone}`}>
      <span className="v4-stat-n">{n}</span>
      <span className="v4-stat-label">{label}</span>
    </span>
  );
}

/* ── stream row (red column) ───────────────── */
function V4StreamRow({ ev, onClick }) {
  const clickable = !!ev.session_id;
  return (
    <div
      className={`v4-stream-row v4-sev-${ev.severity || 'info'}${clickable ? ' is-clickable' : ''}`}
      onClick={clickable ? () => onClick(ev) : undefined}
      role={clickable ? 'button' : undefined}
      tabIndex={clickable ? 0 : undefined}
    >
      <span className="v4-stream-time">{cxFmtTime(ev.ts)}</span>
      <span className="v4-stream-msg">{ev.message}</span>
      {clickable && (
        <span className="v4-stream-sess">{cxShortId(ev.session_id)} →</span>
      )}
    </div>
  );
}

/* ── blast radius panel ────────────────────── */
function V4Radius({ blastRadius, onSessionClick }) {
  const { sessions, executions, requests } = blastRadius;
  return (
    <div className="v4-radius">
      <div className="v4-radius-stats">
        <div className="v4-radius-stat">
          <span className="v4-radius-n">{sessions.length}</span>
          <span className="v4-radius-label">sessions</span>
        </div>
        <div className="v4-radius-stat">
          <span className="v4-radius-n">{executions.length}</span>
          <span className="v4-radius-label">executions</span>
        </div>
        <div className="v4-radius-stat">
          <span className="v4-radius-n">{requests.length}</span>
          <span className="v4-radius-label">requests</span>
        </div>
      </div>
      {sessions.length > 0 && (
        <div className="v4-radius-sessions">
          <span className="v4-radius-sublabel">Sessions touched</span>
          <div className="v4-radius-chips">
            {sessions.slice(0, 8).map((sid) => (
              <button
                key={sid}
                type="button"
                className="v4-radius-chip"
                onClick={() => onSessionClick(sid)}
                title={`Open session ${sid}`}
              >
                {cxShortId(sid)} →
              </button>
            ))}
            {sessions.length > 8 && (
              <span className="v4-radius-more">+{sessions.length - 8}</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/* ── recent changes lane ───────────────────── */
function V4Changes({ changes, sinceIncident }) {
  const sinceMs = sinceIncident ? new Date(sinceIncident).getTime() : null;
  return (
    <div className="v4-changes">
      {changes.map((c, i) => {
        const cMs = new Date(c.ts).getTime();
        const beforeIncident = sinceMs && cMs < sinceMs;
        return (
          <div key={i} className={`v4-change v4-change-${c.kind} v4-risk-${c.risk}${beforeIncident ? ' is-suspect' : ''}`}>
            <span className="v4-change-kind">{c.kind}</span>
            <div className="v4-change-body">
              <span className="v4-change-title">{c.title}</span>
              <span className="v4-change-meta">
                {cxAgo(c.ts)} · {c.actor}
                {beforeIncident && <span className="v4-change-flag"> · before incident</span>}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ── runbook ───────────────────────────────── */
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
                  <CxCopyBtn value={c.cmd} />
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

/* ── amber card ────────────────────────────── */
function V4AmberCard({ tile, onClick }) {
  const { TILE_META } = window.ConnectionsMocks;
  const meta = TILE_META[tile.id];
  return (
    <button type="button" className="v4-amber-card" onClick={onClick}>
      <div className="v4-amber-card-head">
        <span className="cx-pill warn">
          <span className="cx-pill-dot" />
          DEGRADED
        </span>
        <span className="v4-amber-card-label">{meta.label}</span>
      </div>
      <span className="v4-amber-card-headline">{tile.headline}</span>
      <span className="v4-amber-card-sub">{tile.subline}</span>
      <span className="v4-amber-card-since">
        {tile.last_change_ts ? `changed ${cxAgo(tile.last_change_ts)}` : 'stable'}
      </span>
    </button>
  );
}

/* ── event row (same as v1) ─────────────── */
function V4EventRow({ ev, onClick }) {
  const clickable = !!ev.session_id;
  return (
    <div
      className={`cx-event-row${clickable ? ' is-clickable' : ''}`}
      onClick={clickable ? () => onClick(ev) : undefined}
      role={clickable ? 'button' : undefined}
      tabIndex={clickable ? 0 : undefined}
    >
      <span className="cx-event-time">{cxFmtTime(ev.ts)}</span>
      <span className="cx-event-sys">{(ev.subsystem || '').replace(/_/g, ' ')}</span>
      <span className={`cx-pill ${cxSeverityClass(ev.severity)}`}>
        <span className="cx-pill-dot" />
        {(ev.severity || 'info').toUpperCase()}
      </span>
      <span className="cx-event-message">{ev.message}</span>
      <span className={`cx-event-session${clickable ? '' : ' dim'}`}>
        {clickable ? <>{cxShortId(ev.session_id)}<span className="cx-event-session-arrow">→</span></> : '—'}
      </span>
    </div>
  );
}

/* ── slide-over panel ───────────────────── */
function V4Panel({ tile, onClose, TILE_META }) {
  const meta = TILE_META[tile.id] || { label: tile.id, blurb: '' };
  const pill = cxPillClass(tile.status);
  return (
    <>
      <button type="button" className="cx-overlay" onClick={onClose} aria-label="Close" />
      <aside className="cx-panel" role="dialog" aria-modal="true">
        <header className="cx-panel-head">
          <div className="cx-panel-head-left">
            <div className="cx-panel-eyebrow"><span className="cx-dia">◆</span><span>Subsystem</span><span>·</span><span>{tile.id}</span></div>
            <h2 className="cx-panel-title">{meta.label}</h2>
            <p className="cx-panel-sub">{meta.blurb}</p>
          </div>
          <button type="button" className="cx-panel-close" onClick={onClose} aria-label="Close">
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
          <dt>Headline</dt><dd>{tile.headline || '—'}</dd>
          <dt>Subline</dt><dd>{tile.subline || '—'}</dd>
          <dt>Last change</dt><dd>{tile.last_change_ts ? cxAgo(tile.last_change_ts) : 'stable'}</dd>
          <dt>Probe id</dt>
          <dd style={{ fontFamily: 'var(--mono)' }}>{tile.id}<CxCopyBtn value={tile.id} /></dd>
        </dl>
        <div className="cx-panel-body">
          <p className="cx-panel-body-label">◇ Raw detail</p>
          <CxJsonView data={tile.detail || {}} label="detail" initialOpen />
        </div>
      </aside>
    </>
  );
}

window.V4View = V4View;
