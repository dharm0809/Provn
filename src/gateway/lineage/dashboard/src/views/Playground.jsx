/* Walacor Gateway — Playground (from design zip stubs.jsx) */

import React, { useState } from 'react';
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

export default function Playground() {
  const [prompt, setPrompt] = useState('Summarize the governance posture for our AI gateway in 3 bullets.');
  const [system, setSystem] = useState('');
  const [model, setModel] = useState('claude-sonnet-4.5');
  const [creativity, setCreativity] = useState(0.7);
  const [maxTokens, setMaxTokens] = useState(1024);

  return (
    <StubScaffold
      icon={<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 6l8 6-8 6M13 18h8"/>
      </svg>}
      title="Playground"
      subtitle="Test prompts against any provisioned model. Every request here generates a real audit record, so you can inspect governance decisions in isolation.">

      <div className="pg-grid">
        <div className="card pg-left">
          <div className="pg-form-head">◆ Prompt</div>

          <div className="pg-form-row">
            <label>Model</label>
            <select className="form-input" value={model} onChange={e => setModel(e.target.value)}>
              <option>claude-sonnet-4.5</option>
              <option>claude-opus-4</option>
              <option>gpt-4.1</option>
              <option>gpt-4o</option>
              <option>gemini-2.5-pro</option>
            </select>
          </div>

          <div className="pg-form-row">
            <label>System</label>
            <textarea className="form-input pg-textarea" rows={2}
              placeholder="You are a helpful assistant…"
              value={system}
              onChange={e => setSystem(e.target.value)} />
          </div>

          <div className="pg-form-row">
            <label>User prompt</label>
            <textarea className="form-input pg-textarea" rows={5}
              placeholder="Type your prompt here…"
              value={prompt}
              onChange={e => setPrompt(e.target.value)} />
          </div>

          <div className="pg-form-row-inline">
            <div>
              <label className="small">Creativity</label>
              <input type="range" min="0" max="2" step="0.1"
                value={creativity}
                onChange={e => setCreativity(parseFloat(e.target.value))} />
            </div>
            <div>
              <label className="small">Max tokens</label>
              <input className="form-input mono" style={{ width: 100 }}
                value={maxTokens}
                onChange={e => setMaxTokens(parseInt(e.target.value, 10) || 0)} />
            </div>
            <button className="btn-wal btn-primary">
              ▶ Send <span className="small" style={{ opacity: 0.6, marginLeft: 4 }}>⌘↵</span>
            </button>
          </div>
        </div>

        <div className="card pg-right">
          <div className="pg-form-head">◇ Response</div>
          <div className="pg-response-preview">
            <p>The AI gateway enforces policy-level access controls across all inbound requests, with redaction and blocking paths wired to three active analyzers.</p>
            <p>Chain-of-custody is preserved through Walacor; 1,842 sessions verified in the current window with zero integrity failures.</p>
            <p>Two candidate models are currently in shadow validation; one has passed its automated gate and is ready for review.</p>
          </div>
          <div className="pg-governance">
            <div className="pg-gov-title">◆ GOVERNANCE READOUT</div>
            <div className="pg-gov-grid">
              <span className="pg-gov-k">EXEC</span><span className="pg-gov-v mono">exec_8a91b4c2e5f0</span>
              <span className="pg-gov-k">ATTEST</span><span className="pg-gov-v mono">att_3f7de1a8</span>
              <span className="pg-gov-k">POLICY</span><span className="pg-gov-v"><span className="badge-wal badge-pass">allow</span></span>
              <span className="pg-gov-k">CHAIN</span><span className="pg-gov-v mono">seq #247,019</span>
              <span className="pg-gov-k">LATENCY</span><span className="pg-gov-v mono">342ms</span>
              <span className="pg-gov-k">TOKENS</span><span className="pg-gov-v mono">82 in / 147 out</span>
              <span className="pg-gov-k">ANALYSIS</span><span className="pg-gov-v">
                <span className="badge-wal badge-pass">safe</span>{' '}
                <span className="badge-wal badge-pass">no_pii</span>{' '}
                <span className="badge-wal badge-pass">within_budget</span>
              </span>
            </div>
          </div>
        </div>
      </div>
    </StubScaffold>
  );
}
