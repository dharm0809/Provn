/* Walacor Gateway — Connections v2 (Grouped / swim-lanes).
   Groups tiles by PROVIDERS / INFRA / POLICY and gives each lane a
   sub-rollup. Scales to 20–30 tiles by letting each lane flow its own
   row. Events stream unchanged from v1. */

/* eslint-disable react/prop-types */
const { useState, useEffect, useMemo, useCallback } = React;

const V2_POLL_MS = 3000;
const V2_GROUPS = ['Providers', 'Infra', 'Policy'];
const V2_GROUP_BLURB = {
  Providers: 'Upstream dependencies — the gateway does not own these.',
  Infra:    'The gateway\'s own machinery — delivery, auth, streaming.',
  Policy:   'Governance plane — analyzers, readiness, control plane.',
};

function V2View({ navigate }) {
  const { TILE_ORDER, TILE_GROUP, TILE_META, scenarios } = window.ConnectionsMocks;

  const [scenario, setScenario] = useState('amber');
  const [snapshot, setSnapshot] = useState(() => scenarios.amber);
  const [openTileId, setOpenTileId] = useState(null);
  const [, setTick] = useState(0);

  useEffect(() => { setSnapshot(scenarios[scenario]); }, [scenario, scenarios]);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), V2_POLL_MS);
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

  const events = useMemo(() => {
    const all = (snapshot && snapshot.events) || [];
    return [...all].sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime()).slice(0, 50);
  }, [snapshot]);

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
      {/* Intro — same chrome as v1, scenario picker on the right */}
      <div className="cx-intro">
        <div className="cx-intro-body">
          <div className="cx-intro-eyebrow">
            <span className="cx-dia">◆</span>
            <span>Connections</span>
            <span className="cx-eyebrow-sep">·</span>
            <span>Grouped by plane</span>
          </div>
          <h1>Which plane is silently broken?</h1>
          <p>
            The same ten probes as v1, but grouped into three swim-lanes:
            Providers (upstream you don't control), Infra (the gateway's own
            machinery), and Policy (governance & compliance). Each lane has
            its own sub-rollup so you can triage without reading every tile.
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

      {/* Swim-lanes */}
      {V2_GROUPS.map((group) => {
        const summary = cxGroupSummary(tilesInOrder, group, TILE_GROUP);
        return (
          <section key={group} className={`v2-lane is-${summary.worst}`}>
            <header className="v2-lane-head">
              <div className="v2-lane-head-left">
                <span className="v2-lane-mark" aria-hidden>◇</span>
                <h2 className="v2-lane-title">{group}</h2>
                <span className="v2-lane-count">{summary.members.length} probe{summary.members.length === 1 ? '' : 's'}</span>
              </div>
              <div className="v2-lane-head-right">
                <span className="v2-lane-blurb">{V2_GROUP_BLURB[group]}</span>
                <span className={`cx-pill ${cxPillClass(summary.worst)}${summary.worst === 'red' ? ' solid' : ''}`}>
                  <span className="cx-pill-dot" />
                  {summary.reds > 0 && `${summary.reds} down`}
                  {summary.reds === 0 && summary.ambers > 0 && `${summary.ambers} degraded`}
                  {summary.reds === 0 && summary.ambers === 0 && 'all healthy'}
                </span>
              </div>
            </header>
            <div className="v2-lane-body">
              {summary.members.map((tile, i) => {
                const meta = TILE_META[tile.id];
                const pill = cxPillClass(tile.status);
                return (
                  <button
                    type="button"
                    key={tile.id}
                    className={`v2-row ${cxStatusClass(tile.status)}`}
                    onClick={() => setOpenTileId(tile.id)}
                  >
                    <span className="v2-row-rail" aria-hidden />
                    <span className="v2-row-idx">{String(i + 1).padStart(2, '0')}</span>
                    <span className="v2-row-id">
                      <span className="v2-row-label">{meta.label}</span>
                      <span className="v2-row-blurb">{meta.blurb}</span>
                    </span>
                    <span className="v2-row-headline">{tile.headline}</span>
                    <span className="v2-row-subline">{tile.subline}</span>
                    <span className="v2-row-meta">
                      <span className={`cx-pill ${pill}${tile.status === 'red' ? ' solid' : ''}`}>
                        <span className="cx-pill-dot" />
                        {cxStatusLabel(tile.status)}
                      </span>
                      <span className="v2-row-since">
                        {tile.last_change_ts ? cxAgo(tile.last_change_ts) : 'stable'}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          </section>
        );
      })}

      {/* Events — unchanged from v1 */}
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
              <V2EventRow key={`${ev.ts}-${idx}`} ev={ev} onClick={onEventClick} TILE_GROUP={TILE_GROUP} />
            ))}
          </div>
        )}
      </div>

      {openTile && <V2Panel tile={openTile} onClose={() => setOpenTileId(null)} TILE_META={TILE_META} TILE_GROUP={TILE_GROUP} />}
    </div>
  );
}

function V2EventRow({ ev, onClick, TILE_GROUP }) {
  const clickable = !!ev.session_id;
  const group = TILE_GROUP[ev.subsystem] || '—';
  return (
    <div
      className={`cx-event-row v2-event-row${clickable ? ' is-clickable' : ''}`}
      onClick={clickable ? () => onClick(ev) : undefined}
      role={clickable ? 'button' : undefined}
      tabIndex={clickable ? 0 : undefined}
    >
      <span className="cx-event-time">{cxFmtTime(ev.ts)}</span>
      <span className="cx-event-sys">
        <span className={`v2-lane-chip v2-chip-${group.toLowerCase()}`}>{group}</span>
        {(ev.subsystem || '').replace(/_/g, ' ')}
      </span>
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

function V2Panel({ tile, onClose, TILE_META, TILE_GROUP }) {
  const meta = TILE_META[tile.id] || { label: tile.id, blurb: '' };
  const pill = cxPillClass(tile.status);
  const group = TILE_GROUP[tile.id];
  return (
    <>
      <button type="button" className="cx-overlay" aria-label="Close" onClick={onClose} />
      <aside className="cx-panel" role="dialog" aria-modal="true">
        <header className="cx-panel-head">
          <div className="cx-panel-head-left">
            <div className="cx-panel-eyebrow">
              <span className="cx-dia">◆</span><span>{group}</span><span>·</span><span>{tile.id}</span>
            </div>
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
          <dt>Plane</dt>
          <dd>{group}</dd>
          <dt>Headline</dt>
          <dd>{tile.headline || '—'}</dd>
          <dt>Subline</dt>
          <dd>{tile.subline || '—'}</dd>
          <dt>Last change</dt>
          <dd>{tile.last_change_ts ? cxAgo(tile.last_change_ts) : 'stable'}</dd>
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
    </>
  );
}

window.V2View = V2View;
