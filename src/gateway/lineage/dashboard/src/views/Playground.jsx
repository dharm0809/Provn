/* Walacor Gateway — Playground.
   Sends a real /v1/chat/completions request through the gateway and pulls
   the governance readout from /v1/lineage/executions/<id> using the
   x-walacor-execution-id response header. */

import React, { useState } from 'react';
import { getExecution } from '../api';
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
            <div className="stub-eyebrow">◆ WALACOR GATEWAY · PLAYGROUND</div>
            <h1 className="stub-title">{title}</h1>
            <p className="stub-subtitle">{subtitle}</p>
          </div>
        </div>
      </div>
      {children}
    </div>
  );
}

function getApiKey() {
  return (localStorage.getItem('cp_api_key') || sessionStorage.getItem('cp_api_key')) || '';
}

function fmtMs(n) {
  if (n == null) return '—';
  if (n < 1000) return `${n} ms`;
  return `${(n / 1000).toFixed(2)} s`;
}

export default function Playground({ navigate }) {
  const [prompt, setPrompt] = useState('Summarize the governance posture for our AI gateway in 3 bullets.');
  const [system, setSystem] = useState('');
  const [model, setModel] = useState('llama3.1:8b');
  const [creativity, setCreativity] = useState(0.7);
  const [maxTokens, setMaxTokens] = useState(1024);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [response, setResponse] = useState(null);   // { text, executionId, latencyMs }
  const [governance, setGovernance] = useState(null); // record from /v1/lineage/executions/<id>

  const send = async () => {
    setBusy(true);
    setError(null);
    setResponse(null);
    setGovernance(null);
    const t0 = performance.now();
    try {
      const messages = [];
      if (system.trim()) messages.push({ role: 'system', content: system });
      messages.push({ role: 'user', content: prompt });
      const key = getApiKey();
      const resp = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(key ? { 'X-API-Key': key } : {}),
        },
        body: JSON.stringify({
          model,
          messages,
          temperature: creativity,
          max_tokens: maxTokens,
          stream: false,
        }),
      });
      const latencyMs = Math.round(performance.now() - t0);
      const executionId = resp.headers.get('x-walacor-execution-id') || null;
      const bodyText = await resp.text();
      let body;
      try { body = JSON.parse(bodyText); } catch { body = { _raw: bodyText }; }
      if (!resp.ok) {
        const detail = body?.error?.message || body?.error || body?.detail || bodyText.slice(0, 400);
        throw new Error(`HTTP ${resp.status}: ${detail}`);
      }
      const text = body?.choices?.[0]?.message?.content
        ?? body?.choices?.[0]?.text
        ?? JSON.stringify(body, null, 2);
      setResponse({ text, executionId, latencyMs, body });

      // Pull the governance readout. Lineage reader is eventually-consistent
      // with the WAL writer; one short retry covers the common race.
      if (executionId) {
        for (let attempt = 0; attempt < 2; attempt++) {
          try {
            const data = await getExecution(executionId);
            setGovernance(data);
            break;
          } catch (e) {
            if (attempt === 0) await new Promise(r => setTimeout(r, 600));
            else setGovernance({ _error: e.message });
          }
        }
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const r = governance?.record;
  const usage = r?.metadata?.token_usage || response?.body?.usage;
  const promptTokens = usage?.prompt_tokens ?? usage?.input_tokens;
  const completionTokens = usage?.completion_tokens ?? usage?.output_tokens;
  const totalTokens = usage?.total_tokens ?? (promptTokens != null && completionTokens != null ? promptTokens + completionTokens : null);
  const analyzers = (r?.metadata?.analyzer_decisions || []).map(a => a.verdict || a.analyzer_id).filter(Boolean);

  return (
    <StubScaffold
      icon={<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 6l8 6-8 6M13 18h8"/>
      </svg>}
      title="Playground"
      subtitle="Test prompts against any provisioned model. Every request here generates a real audit record under Sessions and Attempts.">

      <div className="pg-grid">
        <div className="card pg-left">
          <div className="pg-form-head">◆ Prompt</div>

          <div className="pg-form-row">
            <label>Model</label>
            <input
              className="form-input mono"
              value={model}
              onChange={e => setModel(e.target.value)}
              placeholder="llama3.1:8b"
              list="pg-model-suggestions"
            />
            <datalist id="pg-model-suggestions">
              <option value="llama3.1:8b" />
              <option value="claude-sonnet-4-6" />
              <option value="claude-opus-4-7" />
              <option value="gpt-4o" />
              <option value="gemini-2.5-pro" />
            </datalist>
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
              <span className="mono small" style={{ marginLeft: 6 }}>{creativity.toFixed(1)}</span>
            </div>
            <div>
              <label className="small">Max tokens</label>
              <input className="form-input mono" style={{ width: 100 }}
                value={maxTokens}
                onChange={e => setMaxTokens(parseInt(e.target.value, 10) || 0)} />
            </div>
            <button
              className="btn-wal btn-primary"
              disabled={busy || !prompt.trim()}
              onClick={send}
              title="Send through the gateway">
              {busy ? '◌ sending…' : '▶ Send'}
            </button>
          </div>

          {error && (
            <div className="pg-form-row" style={{ color: 'var(--red, #a13a3a)', marginTop: 10 }}>
              <strong>Error:</strong> <span className="mono small">{error}</span>
            </div>
          )}
        </div>

        <div className="card pg-right">
          <div className="pg-form-head">◇ Response</div>
          <div className="pg-response-preview">
            {response?.text
              ? <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontFamily: 'inherit' }}>{response.text}</pre>
              : <p style={{ color: 'var(--text-muted)' }}>Submit a prompt to see the model's response and the gateway's governance readout.</p>}
          </div>

          <div className="pg-governance">
            <div className="pg-gov-title">◆ GOVERNANCE READOUT</div>
            <div className="pg-gov-grid">
              <span className="pg-gov-k">EXEC</span>
              <span className="pg-gov-v mono">
                {response?.executionId
                  ? <a onClick={() => navigate?.('execution', { executionId: response.executionId })} style={{ cursor: 'pointer' }}>{response.executionId.slice(0, 18)}…</a>
                  : '—'}
              </span>
              <span className="pg-gov-k">ATTEST</span>
              <span className="pg-gov-v mono">{r?.model_attestation_id || r?.model_id || '—'}</span>
              <span className="pg-gov-k">POLICY</span>
              <span className="pg-gov-v mono">{r?.policy_result || '—'}</span>
              <span className="pg-gov-k">CHAIN</span>
              <span className="pg-gov-v mono">{r?.record_id ? 'sealed' : (response?.executionId ? 'pending' : '—')}</span>
              <span className="pg-gov-k">LATENCY</span>
              <span className="pg-gov-v mono">{fmtMs(response?.latencyMs)}</span>
              <span className="pg-gov-k">TOKENS</span>
              <span className="pg-gov-v mono">
                {totalTokens != null
                  ? `${totalTokens} (${promptTokens ?? '—'}/${completionTokens ?? '—'})`
                  : '—'}
              </span>
              <span className="pg-gov-k">ANALYSIS</span>
              <span className="pg-gov-v mono">{analyzers.length ? analyzers.join(' · ') : (governance?._error ? 'lineage unavailable' : '—')}</span>
            </div>
          </div>
        </div>
      </div>
    </StubScaffold>
  );
}
