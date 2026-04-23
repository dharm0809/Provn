/* Walacor Gateway — Connections v3 (Severity-ranked).
   Single column triage queue: reds at top with inline "what / why /
   who's hit / what now"; ambers as one-liners; greens collapsed into
   a single strip. Events stream unchanged. */

/* eslint-disable react/prop-types */
const { useState, useEffect, useMemo, useCallback } = React;

const V3_POLL_MS = 3000;

/* Per-probe "what does this actually mean" blurbs — red/amber tiles get
   a one-sentence consequence line under the headline so ops know why
   they should care. */
const V3_CONSEQUENCE = {
  providers:           { red: 'Completions will fail for every session routed to the down provider.',
                         amber:'Elevated error rate — some completions will time out or retry.' },
  walacor_delivery:    { red: 'No new envelopes are being sealed. Chain-of-custody paused.',
                         amber:'Writes are succeeding but backlog is growing.' },
  analyzers:           { red: 'Content probes offline — prompts flow unchecked.',
                         amber:'Analyzer fail-opens: PII may be leaking past presidio.' },
  tool_loop:           { red: 'Tool executor crashing — agents will report generic failures.',
                         amber:'Tool exceptions being swallowed — silent failures in agent runs.' },
  model_capabilities:  { red: 'Multiple models disabled — callers hitting "capability unsupported".',
                         amber:'One model auto-disabled its tools — tool-using runs rerouted.' },
  control_plane:       { red: 'Policy cache is stale — enforcement may be running on old rules.',
                         amber:'Policy sync lagging — minor drift possible.' },
  auth:                { red: 'JWKS unreachable — new tokens will be rejected.',
                         amber:'Bootstrap key rotating — transient 401s possible.' },
  readiness:           { red: 'Phase-26 readiness FAILED — a compliance check is in the red.',
                         amber:'One or more readiness probes are degraded.' },
  streaming:           { red: 'Streams dropping — long-running responses cut mid-token.',
                         amber:'Elevated stream interruptions — some SSE clients reconnecting.' },
  intelligence_worker: { red: 'Training worker down — verdict log no longer consuming.',
                         amber:'Queue depth growing — async jobs delayed.' },
};

function V3View({ navigate }) {
  const { TILE_ORDER, TILE_META, scenarios } = window.ConnectionsMocks;

  const [scenario, setScenario] = useState('amber');
  const [snapshot, setSnapshot] = useState(() => scenarios.amber);
  const [greenOpen, setGreenOpen] = useState(false);
  const [openTileId, setOpenTileId] = useState(null);
  const [, setTick] = useState(0);

  useEffect(() => { setSnapshot(scenarios[scenario]); }, [scenario, scenarios]);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), V3_POLL_MS);
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

  /* Severity-ranked: reds → ambers → unknowns (greens collapsed separately). */
  const SEV_RANK = { red: 0, amber: 1, unknown: 2, green: 3 };
  const nonGreen = useMemo(
    () => tilesInOrder.filter((t) => t.status !== 'green')
      .sort((a, b) => {
        const r = (SEV_RANK[a.status] ?? 99) - (SEV_RANK[b.status] ?? 99);
        if (r !== 0) return r;
        // within same severity, most-recently-changed first
        const at = a.last_change_ts ? new Date(a.last_change_ts).getTime() : 0;
        const bt = b.last_change_ts ? new Date(b.last_change_ts).getTime() : 0;
        return bt - at;
      }),
    [tilesInOrder],
  );
  const greens = useMemo(() => tilesInOrder.filter((t) => t.status === 'green'), [tilesInOrder]);

  const events = useMemo(() => {
    const all = (snapshot && snapshot.events) || [];
    return [...all].sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime()).slice(0, 50);
  }, [snapshot]);

  const eventsBySubsystem = useMemo(() => {
    const m = {};
    events.forEach((e) => {
      if (!m[e.subsystem]) m[e.subsystem] = [];
      m[e.subsystem].push(e);
    });
    return m;
  }, [events]);

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

  return (
    <div className="cx-page">
      <div className="cx-intro">
        <div className="cx-intro-body">
          <div className="cx-intro-eyebrow">
            <span className="cx-dia">◆</span>
            <span>Connections</span>
            <span className="cx-eyebrow-sep">·</span>
            <span>Triage queue</span>
          </div>
          <h1>What's broken, what's complaining, what's fine.</h1>
          <p>
            Ranked by severity, newest change first. Reds expand with a
            consequence line, related events, and blast-radius. Ambers are
            one-liners. Healthy probes collapse into a single strip at the
            bottom — out of sight unless you ask for them.
          </p>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 10 }}>
          <CxScenarioPicker scenario={scenario} setScenario={setScenario} />
          <div className="cx-rollup">
            <span className="cx-rollup-label">Rollup</span>
            <span className={`cx-rollup-state ${overall}`}>
              <span className="cx-rollup-dot" />
              {cxStatusLabel(overall)}
            </span>
            <span className="cx-rollup-sep" />
            <span className="cx-rollup-meta">
              <span className="cx-tick" />
              snapshot {cxAgo(snapshot.generated_at)}
            </span>
          </div>
        </div>
      </div>

      {/* Severity counts */}
      <div className="v3-counts">
        <V3Count tone="red"   count={counts.red}     label="Down" />
        <V3Count tone="amber" count={counts.amber}   label="Degraded" />
        <V3Count tone="green" count={counts.green}   label="Healthy" />
        {counts.unknown > 0 && <V3Count tone="dim" count={counts.unknown} label="Unknown" />}
      </div>

      {/* Triage queue */}
      <div className="cx-section">
        <span className="cx-section-title">◇ Triage queue</span>
        <span className="cx-section-hint">
          {nonGreen.length === 0 ? 'nothing to triage' : `${nonGreen.length} requiring attention · reds first`}
        </span>
      </div>

      <div className="v3-queue">
        {nonGreen.length === 0 ? (
          <div className="v3-empty">
            <span className="v3-empty-icon">✓</span>
            All {counts.green} subsystems reporting healthy.
          </div>
        ) : nonGreen.map((tile) => (
          <V3QueueItem
            key={tile.id}
            tile={tile}
            events={eventsBySubsystem[tile.id] || []}
            onOpenDetail={() => setOpenTileId(tile.id)}
            onEventClick={onEventClick}
          />
        ))}
      </div>

      {/* Collapsed greens */}
      {greens.length > 0 && (
        <div className={`v3-greens${greenOpen ? ' is-open' : ''}`}>
          <button
            type="button"
            className="v3-greens-bar"
            onClick={() => setGreenOpen((v) => !v)}
            aria-expanded={greenOpen}
          >
            <span className="v3-greens-left">
              <span className="v3-greens-check">✓</span>
              <span className="v3-greens-count">{greens.length}</span>
              <span className="v3-greens-label">healthy subsystem{greens.length === 1 ? '' : 's'}</span>
            </span>
            <span className="v3-greens-names">
              {greens.map((t) => TILE_META[t.id].label).join(' · ')}
            </span>
            <span className="v3-greens-caret">{greenOpen ? '▾' : '▸'}</span>
          </button>
          {greenOpen && (
            <div className="v3-greens-body">
              {greens.map((tile) => (
                <button
                  key={tile.id}
                  type="button"
                  className="v3-green-row"
                  onClick={() => setOpenTileId(tile.id)}
                >
                  <span className="v3-green-row-dot" />
                  <span className="v3-green-row-label">{TILE_META[tile.id].label}</span>
                  <span className="v3-green-row-headline">{tile.headline}</span>
                  <span className="v3-green-row-since">
                    {tile.last_change_ts ? `stable ${cxAgo(tile.last_change_ts)}` : 'stable'}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Events stream */}
      <div className="cx-section">
        <span className="cx-section-title">◇ Recent Events</span>
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
              <V3EventRow key={`${ev.ts}-${idx}`} ev={ev} onClick={onEventClick} />
            ))}
          </div>
        )}
      </div>

      {openTile && <V3Panel tile={openTile} onClose={() => setOpenTileId(null)} TILE_META={TILE_META} />}
    </div>
  );
}

/* ── severity count cards ──────────────────────── */
function V3Count({ tone, count, label }) {
  return (
    <div className={`v3-count v3-count-${tone}`}>
      <span className="v3-count-n">{count}</span>
      <span className="v3-count-label">{label}</span>
    </div>
  );
}

/* ── queue item (expanded for red, inline one-liner for amber) ── */
function V3QueueItem({ tile, events, onOpenDetail, onEventClick }) {
  const { TILE_META } = window.ConnectionsMocks;
  const meta = TILE_META[tile.id] || { label: tile.id, blurb: '' };
  const pill = cxPillClass(tile.status);
  const consequence = V3_CONSEQUENCE[tile.id] && V3_CONSEQUENCE[tile.id][tile.status];

  // Blast-radius: unique session IDs on events for this subsystem.
  const sessions = useMemo(() => {
    const set = new Set();
    events.forEach((e) => { if (e.session_id) set.add(e.session_id); });
    return [...set];
  }, [events]);

  const isExpanded = tile.status === 'red';

  if (!isExpanded) {
    // Amber/unknown one-liner
    return (
      <button
        type="button"
        className={`v3-line ${cxStatusClass(tile.status)}`}
        onClick={onOpenDetail}
      >
        <span className="v3-line-rail" aria-hidden />
        <span className={`cx-pill ${pill}`}>
          <span className="cx-pill-dot" />
          {cxStatusLabel(tile.status)}
        </span>
        <span className="v3-line-label">{meta.label}</span>
        <span className="v3-line-headline">{tile.headline}</span>
        {consequence && <span className="v3-line-consequence">{consequence}</span>}
        <span className="v3-line-since">
          {tile.last_change_ts ? cxAgo(tile.last_change_ts) : 'stable'}
        </span>
      </button>
    );
  }

  // Red — expanded triage block
  return (
    <article className={`v3-block ${cxStatusClass(tile.status)}`}>
      <header className="v3-block-head">
        <div className="v3-block-head-left">
          <span className={`cx-pill ${pill}${tile.status === 'red' ? ' solid' : ''}`}>
            <span className="cx-pill-dot" />
            {cxStatusLabel(tile.status)}
          </span>
          <h3 className="v3-block-title">{meta.label}</h3>
          <span className="v3-block-id">{tile.id}</span>
        </div>
        <div className="v3-block-head-right">
          <span className="v3-block-since">
            {tile.last_change_ts ? `changed ${cxAgo(tile.last_change_ts)}` : 'stable'}
          </span>
          <button type="button" className="v3-block-detail" onClick={onOpenDetail}>
            raw detail →
          </button>
        </div>
      </header>

      <div className="v3-block-body">
        <div className="v3-block-what">
          <span className="v3-block-what-label">What</span>
          <span className="v3-block-what-text">{tile.headline}</span>
          <span className="v3-block-what-sub">{tile.subline}</span>
        </div>

        {consequence && (
          <div className="v3-block-why">
            <span className="v3-block-why-label">Consequence</span>
            <p className="v3-block-why-text">{consequence}</p>
          </div>
        )}

        {sessions.length > 0 && (
          <div className="v3-block-radius">
            <span className="v3-block-radius-label">Blast radius · observed in</span>
            <div className="v3-block-radius-chips">
              {sessions.slice(0, 6).map((sid) => (
                <button
                  key={sid}
                  type="button"
                  className="v3-block-radius-chip"
                  onClick={() => onEventClick({ session_id: sid })}
                  title={`Open session ${sid}`}
                >
                  {cxShortId(sid)} →
                </button>
              ))}
              {sessions.length > 6 && (
                <span className="v3-block-radius-more">+{sessions.length - 6} more</span>
              )}
            </div>
          </div>
        )}

        {events.length > 0 && (
          <div className="v3-block-events">
            <span className="v3-block-events-label">Last {Math.min(events.length, 4)} events</span>
            <div className="v3-block-events-list">
              {events.slice(0, 4).map((e, i) => (
                <div key={i} className={`v3-block-event v3-sev-${e.severity || 'info'}`}>
                  <span className="v3-block-event-time">{cxFmtTime(e.ts)}</span>
                  <span className="v3-block-event-msg">{e.message}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </article>
  );
}

/* ── event row — identical to v1 but with seen-this-subsystem cue ── */
function V3EventRow({ ev, onClick }) {
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

/* ── slide-over panel (minimal, shared chrome) ─── */
function V3Panel({ tile, onClose, TILE_META }) {
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
          <dt>Headline</dt>
          <dd>{tile.headline || '—'}</dd>
          <dt>Subline</dt>
          <dd>{tile.subline || '—'}</dd>
          <dt>Last change</dt>
          <dd>{tile.last_change_ts ? cxAgo(tile.last_change_ts) : 'stable'}</dd>
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

window.V3View = V3View;
