/* Walacor Gateway — Compliance (from design zip stubs.jsx) */

import React from 'react';
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
            <div className="stub-eyebrow">◆ WALACOR GATEWAY · COMING NEXT</div>
            <h1 className="stub-title">{title}</h1>
            <p className="stub-subtitle">{subtitle}</p>
          </div>
        </div>
      </div>
      {children}
    </div>
  );
}

// Placeholder frameworks list. Scores/grades/gaps are intentionally blank
// because the compliance-scoring pipeline isn't wired up yet — the previous
// hard-coded values (88/B, 92/A, …) read as real data and that was
// misleading.
const FRAMEWORKS = [
  { id: 'eu_ai_act', label: 'EU AI Act' },
  { id: 'nist',      label: 'NIST AI RMF' },
  { id: 'soc2',      label: 'SOC 2 Type II' },
  { id: 'iso42001',  label: 'ISO 42001' },
];

export default function Compliance() {
  return (
    <StubScaffold
      icon={<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 2L3 6v6c0 5 4 9 9 11 5-2 9-6 9-11V6l-9-4z"/>
        <path d="M8 12l3 3 5-6"/>
      </svg>}
      title="Compliance"
      subtitle="Audit-ready reports mapped to EU AI Act, NIST AI RMF, SOC 2, and ISO 42001. Every control maps back to chain evidence — no gap is hand-waved.">

      <div className="compliance-grid">
        {FRAMEWORKS.map(f => (
          <div key={f.id} className="card compliance-card">
            <div className="compliance-head">
              <div className="compliance-label">{f.label}</div>
              <span className="badge-wal badge-muted mono">{f.id}</span>
            </div>
            <div className="compliance-score">
              <div className="compliance-score-val" style={{ color: 'var(--text-muted)' }}>—</div>
              <div className="compliance-grade" style={{ color: 'var(--text-muted)' }}>—</div>
            </div>
            <div className="compliance-meta">
              <span style={{ color: 'var(--text-muted)' }}>scoring not yet wired up</span>
            </div>
            <div className="compliance-actions">
              <button className="btn-wal btn-ghost btn-sm" disabled>Preview</button>
              <div className="compliance-downloads">
                <button className="dl-btn" disabled>JSON</button>
                <button className="dl-btn" disabled>CSV</button>
                <button className="dl-btn" disabled>PDF</button>
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <div className="intel-card-head">
          <div>
            <div className="intel-card-title">Chain Integrity</div>
            <div className="intel-card-sub">use the Sessions view — "Verify Chain" — for live verification</div>
          </div>
          <span className="chain-chip" style={{ color: 'var(--text-muted)' }}>◇ not connected</span>
        </div>
        <div style={{ padding: '8px 2px 2px', fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-muted)', letterSpacing: '0.06em' }}>
          This panel will aggregate per-session verification once the
          compliance rollup endpoint is in place.
        </div>
      </div>
    </StubScaffold>
  );
}
