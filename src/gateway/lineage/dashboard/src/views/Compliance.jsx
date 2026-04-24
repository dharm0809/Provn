/* Walacor Gateway — Compliance */

import React, { useEffect, useMemo, useState } from 'react';
import {
  getComplianceReport,
  complianceExportUrl,
} from '../api';
import '../styles/stubs.css';

function StubScaffold({ icon, title, subtitle, children }) {
  return (
    <div className="stub-view">
      <div className="stub-hero card card-accent">
        <div className="stub-hero-inner">
          <div className="stub-icon-wrap">
            <div className="stub-icon">{icon}</div>
          </div>
          <div className="stub-hero-text">
            <div className="stub-eyebrow">◆ WALACOR GATEWAY · COMPLIANCE</div>
            <h1 className="stub-title">{title}</h1>
            <p className="stub-subtitle">{subtitle}</p>
          </div>
        </div>
      </div>
      {children}
    </div>
  );
}

const FRAMEWORKS = [
  { id: 'eu_ai_act', label: 'EU AI Act' },
  { id: 'nist',      label: 'NIST AI RMF' },
  { id: 'soc2',      label: 'SOC 2 Type II' },
  { id: 'iso42001',  label: 'ISO 42001' },
];

// Last 30 days of inclusive coverage. Keeping the window here so all four
// framework cards + the chain-integrity panel agree on the same slice.
function windowForPreset(preset) {
  const end = new Date();
  const start = new Date(end);
  if (preset === 'today') start.setDate(start.getDate() - 0);
  else if (preset === '7d') start.setDate(start.getDate() - 7);
  else if (preset === '30d') start.setDate(start.getDate() - 30);
  else if (preset === '90d') start.setDate(start.getDate() - 90);
  else if (preset === '365d') start.setDate(start.getDate() - 365);
  else start.setDate(start.getDate() - 30);
  const iso = (d) => d.toISOString().slice(0, 10);
  return { start: iso(start), end: iso(end) };
}

const PRESETS = [
  { id: 'today', label: 'Today' },
  { id: '7d',    label: '7d' },
  { id: '30d',   label: '30d' },
  { id: '90d',   label: '90d' },
  { id: '365d',  label: '1y' },
  { id: 'custom', label: 'Custom' },
];

function RangePicker({ preset, start, end, onPreset, onCustom }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap',
      padding: '10px 14px', margin: '14px 0',
      background: 'var(--surface-sunken, rgba(0,0,0,0.2))',
      borderRadius: 4,
      fontFamily: 'var(--mono)', fontSize: 11,
    }}>
      <span style={{ color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.08em' }}>Window</span>
      <div style={{ display: 'inline-flex', gap: 4 }}>
        {PRESETS.map(p => (
          <button
            key={p.id}
            className={`dl-btn${preset === p.id ? ' is-active' : ''}`}
            style={preset === p.id ? { borderColor: 'var(--gold, #b8860b)', color: 'var(--gold, #b8860b)' } : undefined}
            onClick={() => onPreset(p.id)}
          >{p.label}</button>
        ))}
      </div>
      {preset === 'custom' && (
        <>
          <input
            type="date" value={start} max={end}
            onChange={(e) => onCustom({ start: e.target.value, end })}
            style={{ fontFamily: 'var(--mono)', fontSize: 11, padding: '4px 6px', background: 'transparent', color: 'inherit', border: '1px solid var(--border-soft, rgba(255,255,255,0.12))', borderRadius: 3 }}
          />
          <span style={{ color: 'var(--text-muted)' }}>→</span>
          <input
            type="date" value={end} min={start}
            onChange={(e) => onCustom({ start, end: e.target.value })}
            style={{ fontFamily: 'var(--mono)', fontSize: 11, padding: '4px 6px', background: 'transparent', color: 'inherit', border: '1px solid var(--border-soft, rgba(255,255,255,0.12))', borderRadius: 3 }}
          />
        </>
      )}
      <span style={{ marginLeft: 'auto', color: 'var(--text-muted)' }}>
        {start} → {end}
      </span>
    </div>
  );
}

function gradeColor(grade) {
  if (grade === 'A') return 'var(--green, #3f8a3f)';
  if (grade === 'B') return 'var(--green, #3f8a3f)';
  if (grade === 'C') return 'var(--amber, #b8860b)';
  if (grade === 'D') return 'var(--amber, #b8860b)';
  if (grade === 'F') return 'var(--red, #a13a3a)';
  return 'var(--text-muted)';
}

function forceDownload(url, filename) {
  const key = localStorage.getItem('cp_api_key') || sessionStorage.getItem('cp_api_key') || '';
  // /v1/compliance is behind lineage_auth_required; need the key as a
  // header. fetch→blob→<a download> preserves that; a plain <a href> loses
  // the X-API-Key.
  fetch(url, key ? { headers: { 'X-API-Key': key } } : {})
    .then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.blob().then(b => ({ blob: b, resp: r }));
    })
    .then(({ blob, resp }) => {
      const cd = resp.headers.get('Content-Disposition') || '';
      const match = cd.match(/filename="?([^"]+)"?/i);
      const name = match ? match[1] : filename;
      const href = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = href; a.download = name;
      document.body.appendChild(a); a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(href);
    })
    .catch(err => alert(`Download failed: ${err.message}`));
}

function PreviewDrawer({ framework, report, onClose }) {
  const readiness = report?.audit_readiness || null;
  const articles = report?.framework_mapping?.articles || [];
  const chain = report?.chain_integrity || null;
  return (
    <div className="cx-overlay-wrap" onClick={onClose}>
      <div
        className="card"
        style={{
          position: 'fixed', top: '5vh', right: '3vw', bottom: '5vh', width: 'min(720px, 92vw)',
          overflow: 'auto', padding: 22, zIndex: 1100,
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 14 }}>
          <div>
            <div className="stub-eyebrow">◆ COMPLIANCE · {framework.id}</div>
            <h2 style={{ margin: '4px 0 0' }}>{framework.label}</h2>
          </div>
          <button className="btn-wal btn-ghost btn-sm" onClick={onClose}>close</button>
        </div>

        {readiness && (
          <>
            <div style={{ display: 'flex', gap: 24, alignItems: 'baseline', marginBottom: 10 }}>
              <div style={{ fontSize: 42, fontFamily: 'var(--mono)', fontWeight: 600, color: gradeColor(readiness.grade) }}>
                {readiness.score}
              </div>
              <div style={{ fontSize: 22, fontFamily: 'var(--mono)', color: gradeColor(readiness.grade) }}>
                {readiness.grade}
              </div>
              <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-muted)' }}>
                assessed {new Date(readiness.assessed_at).toISOString().replace('T', ' ').slice(0, 19)}Z
              </div>
            </div>
            <div className="intel-card-sub" style={{ marginBottom: 8 }}>◇ readiness dimensions</div>
            <div style={{ display: 'grid', gap: 6, marginBottom: 18 }}>
              {(readiness.dimensions || []).map((d, i) => (
                <div key={i} style={{
                  display: 'grid', gridTemplateColumns: '1fr 60px 40px',
                  fontFamily: 'var(--mono)', fontSize: 12, alignItems: 'baseline',
                  padding: '6px 0', borderBottom: '1px dashed var(--border-soft, rgba(255,255,255,0.06))',
                }}>
                  <div>
                    <div>{d.name}</div>
                    <div style={{ color: 'var(--text-muted)', fontSize: 10 }}>{d.description}</div>
                    {(d.evidence || []).map((e, j) => (
                      <div key={j} style={{ color: 'var(--text-muted)', fontSize: 10 }}>· {e}</div>
                    ))}
                  </div>
                  <div style={{ textAlign: 'right', color: gradeColor(d.score >= 80 ? 'A' : d.score >= 60 ? 'C' : 'F') }}>{d.score}</div>
                  <div style={{ textAlign: 'right', color: 'var(--text-muted)' }}>w{d.weight}</div>
                </div>
              ))}
            </div>
          </>
        )}

        {chain && (
          <>
            <div className="intel-card-sub" style={{ marginBottom: 8 }}>◇ chain integrity</div>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 12, marginBottom: 16 }}>
              <div>{chain.sessions_verified} session(s) verified · {chain.all_valid ? 'all valid' : 'failures present'}</div>
            </div>
          </>
        )}

        {articles.length > 0 && (
          <>
            <div className="intel-card-sub" style={{ marginBottom: 8 }}>◇ control mapping ({articles.length})</div>
            <div style={{ display: 'grid', gap: 6 }}>
              {articles.map((a, i) => (
                <div key={i} style={{
                  fontFamily: 'var(--mono)', fontSize: 11,
                  padding: '8px 10px',
                  background: 'var(--surface-sunken, rgba(0,0,0,0.2))',
                  borderRadius: 4,
                }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                    <span>{a.id || a.article || '—'} {a.title ? `· ${a.title}` : ''}</span>
                    <span style={{ color: gradeColor(a.status === 'compliant' ? 'A' : a.status === 'partial' ? 'C' : 'F') }}>
                      {a.status || '—'}
                    </span>
                  </div>
                  {a.description && <div style={{ color: 'var(--text-muted)', marginTop: 3 }}>{a.description}</div>}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default function Compliance() {
  const [preset, setPreset] = useState('30d');
  const [range, setRange] = useState(() => windowForPreset('30d'));
  const { start, end } = range;
  const [reports, setReports] = useState({});   // { framework_id: report | { error } }
  const [loading, setLoading] = useState(true);
  const [preview, setPreview] = useState(null); // { framework, report }

  // Preset change recomputes window from "now"; custom leaves the window
  // untouched for the user to edit via date inputs.
  function choosePreset(id) {
    setPreset(id);
    if (id !== 'custom') setRange(windowForPreset(id));
  }
  function setCustomRange(next) {
    setPreset('custom');
    setRange(next);
  }

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    Promise.all(FRAMEWORKS.map(f =>
      getComplianceReport(f.id, start, end)
        .then(r => [f.id, r])
        .catch(e => [f.id, { __error: e.message }])
    )).then(entries => {
      if (cancelled) return;
      setReports(Object.fromEntries(entries));
      setLoading(false);
    });
    return () => { cancelled = true; };
  }, [start, end]);

  const chainPanel = useMemo(() => {
    // Pick any successful report's chain_integrity — all four share the
    // same underlying verification against the same WAL.
    for (const f of FRAMEWORKS) {
      const r = reports[f.id];
      if (r && !r.__error && r.chain_integrity) return r.chain_integrity;
    }
    return null;
  }, [reports]);

  return (
    <StubScaffold
      icon={<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 2L3 6v6c0 5 4 9 9 11 5-2 9-6 9-11V6l-9-4z"/>
        <path d="M8 12l3 3 5-6"/>
      </svg>}
      title="Compliance"
      subtitle="Audit-ready reports mapped to EU AI Act, NIST AI RMF, SOC 2, and ISO 42001. Each card's readiness score is computed from the same underlying signals (record completeness, cryptographic chain integrity, analyzer coverage, model governance, user identity, persistence, enforcement) — per-framework details diverge in the control mapping, not the score.">

      <RangePicker
        preset={preset}
        start={start}
        end={end}
        onPreset={choosePreset}
        onCustom={setCustomRange}
      />

      <div className="compliance-grid">
        {FRAMEWORKS.map(f => {
          const r = reports[f.id];
          const errored = r && r.__error;
          const ready = r && !errored;
          const readiness = ready ? r.audit_readiness : null;
          const score = readiness ? readiness.score : null;
          const grade = readiness ? readiness.grade : null;
          const canDownload = ready && !loading;
          return (
            <div key={f.id} className="card compliance-card">
              <div className="compliance-head">
                <div className="compliance-label">{f.label}</div>
                <span className="badge-wal badge-muted mono">{f.id}</span>
              </div>
              <div className="compliance-score">
                <div className="compliance-score-val" style={{ color: gradeColor(grade) }}>
                  {loading ? '…' : (score != null ? score : '—')}
                </div>
                <div className="compliance-grade" style={{ color: gradeColor(grade) }}>
                  {loading ? '' : (grade || '—')}
                </div>
              </div>
              <div className="compliance-meta">
                <span style={{ color: errored ? 'var(--red)' : 'var(--text-muted)' }}>
                  {loading
                    ? 'loading…'
                    : errored
                      ? `error: ${r.__error.slice(0, 60)}`
                      : `${r.summary?.total_requests ?? 0} request(s) · ${r.chain_integrity?.sessions_verified ?? 0} session(s) verified`}
                </span>
              </div>
              <div className="compliance-actions">
                <button
                  className="btn-wal btn-ghost btn-sm"
                  disabled={!ready}
                  onClick={() => setPreview({ framework: f, report: r })}
                >Preview</button>
                <div className="compliance-downloads">
                  {['json', 'csv', 'pdf'].map(fmt => (
                    <button
                      key={fmt}
                      className="dl-btn"
                      disabled={!canDownload}
                      onClick={() => forceDownload(
                        complianceExportUrl({ framework: f.id, start, end, format: fmt }),
                        `walacor-${f.id}-${start}-to-${end}.${fmt}`,
                      )}
                    >{fmt.toUpperCase()}</button>
                  ))}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <div className="intel-card-head">
          <div>
            <div className="intel-card-title">Chain Integrity</div>
            <div className="intel-card-sub">Per-session Merkle/ID-pointer chain verification from the lineage reader</div>
          </div>
          <span
            className="chain-chip"
            style={{ color: chainPanel ? (chainPanel.all_valid ? 'var(--green)' : 'var(--red)') : 'var(--text-muted)' }}
          >
            {chainPanel
              ? (chainPanel.all_valid ? '◆ all valid' : '◆ failures present')
              : (loading ? '◇ loading' : '◇ no data')}
          </span>
        </div>
        {chainPanel && (
          <div style={{ padding: '8px 2px 2px', fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-muted)' }}>
            {chainPanel.sessions_verified} session(s) verified in window.
            {!chainPanel.all_valid && ' Some sessions have chain errors — open the Sessions view to drill in.'}
          </div>
        )}
      </div>

      {preview && (
        <PreviewDrawer
          framework={preview.framework}
          report={preview.report}
          onClose={() => setPreview(null)}
        />
      )}
    </StubScaffold>
  );
}
