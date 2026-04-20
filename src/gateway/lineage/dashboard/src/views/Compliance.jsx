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

const FRAMEWORKS = [
  { id: 'eu_ai_act', label: 'EU AI Act',    score: 88, grade: 'B', gaps: 2 },
  { id: 'nist',      label: 'NIST AI RMF',  score: 92, grade: 'A', gaps: 1 },
  { id: 'soc2',      label: 'SOC 2 Type II', score: 95, grade: 'A', gaps: 0 },
  { id: 'iso42001',  label: 'ISO 42001',    score: 79, grade: 'C', gaps: 4 },
];

const gradeColor = (g) => ({
  A: 'var(--green)', B: 'var(--gold)', C: 'var(--amber)', D: 'var(--red)', F: 'var(--red)',
})[g] || 'var(--text-muted)';

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
              <div className="compliance-score-val" style={{ color: gradeColor(f.grade) }}>{f.score}</div>
              <div className="compliance-grade" style={{ color: gradeColor(f.grade) }}>{f.grade}</div>
            </div>
            <div className="compliance-meta">
              {f.gaps === 0
                ? <span style={{ color: 'var(--green)' }}>✓ 0 gaps identified</span>
                : <span style={{ color: 'var(--amber)' }}>⚠ {f.gaps} gap{f.gaps > 1 ? 's' : ''} to close</span>}
            </div>
            <div className="compliance-actions">
              <button className="btn-wal btn-ghost btn-sm">Preview</button>
              <div className="compliance-downloads">
                <button className="dl-btn">JSON</button>
                <button className="dl-btn">CSV</button>
                <button className="dl-btn">PDF</button>
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <div className="intel-card-head">
          <div>
            <div className="intel-card-title">Chain Integrity</div>
            <div className="intel-card-sub">continuous verification across all sessions</div>
          </div>
          <span className="chain-chip chain-ok">◆ 1,842 / 1,842 VERIFIED</span>
        </div>
        <div style={{ padding: '8px 2px 2px', fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--text-muted)', letterSpacing: '0.06em' }}>
          Last verification: 2 minutes ago · depth 247,019 records · merkle root{' '}
          <span style={{ color: 'var(--gold)' }}>sha256:4a7b…e01c</span>
        </div>
      </div>
    </StubScaffold>
  );
}
