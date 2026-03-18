import { useState, useEffect, useRef, useCallback } from 'react';
import { getExecution } from '../api';
import { displayModel, verdictBadgeClass } from '../utils';


function generateUUID() {
  // generateUUID() requires secure context (HTTPS or localhost).
  // Fallback for plain HTTP deployments.
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    try { return generateUUID(); } catch {}
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

function getApiKey() {
  return sessionStorage.getItem('cp_api_key') || '';
}

function GovernanceReadout({ meta, record, loading }) {
  if (loading) return <div className="skeleton-block" style={{ height: 100 }} />;
  if (!meta && !record) return null;

  const decisions = record?.metadata?.analyzer_decisions || [];

  return (
    <div className="pg-governance">
      <div className="pg-section-label">◆ Governance Readout</div>
      <div className="pg-gov-grid">
        {meta?.executionId && <><span className="pg-gov-key">EXEC</span><span className="pg-gov-val mono">{meta.executionId}</span></>}
        {meta?.attestationId && <><span className="pg-gov-key">ATTEST</span><span className="pg-gov-val mono">{meta.attestationId}</span></>}
        {meta?.policyResult && (
          <><span className="pg-gov-key">POLICY</span><span className="pg-gov-val"><span className={`badge ${meta.policyResult === 'allow' ? 'badge-pass' : 'badge-fail'}`}>{meta.policyResult}</span></span></>
        )}
        {meta?.chainSeq != null && <><span className="pg-gov-key">CHAIN</span><span className="pg-gov-val mono">seq #{meta.chainSeq}</span></>}
        {record?.latency_ms != null && <><span className="pg-gov-key">LATENCY</span><span className="pg-gov-val mono">{record.latency_ms.toFixed(0)}ms</span></>}
        {(record?.prompt_tokens > 0 || record?.completion_tokens > 0) && (
          <><span className="pg-gov-key">TOKENS</span><span className="pg-gov-val mono">{record.prompt_tokens} in / {record.completion_tokens} out</span></>
        )}
        {record?.cache_hit && (
          <><span className="pg-gov-key">CACHE</span><span className="pg-gov-val"><span className="badge badge-gold">HIT</span> <span className="mono">{record.cached_tokens} tokens</span></span></>
        )}
      </div>
      {decisions.length > 0 && (
        <div className="pg-gov-analysis">
          <span className="pg-gov-key" style={{ marginRight: 8 }}>ANALYSIS</span>
          {decisions.map((d, i) => (
            <span key={i} style={{ marginRight: 8 }}>
              <span className={`badge ${verdictBadgeClass(d.verdict)}`}>{d.verdict}</span>
              <span className="mono" style={{ fontSize: 10, color: 'var(--text-muted)', marginLeft: 4 }}>{d.analyzer_id}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function ConversationHistory({ messages }) {
  const endRef = useRef(null);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);
  if (messages.length === 0) return null;
  return (
    <div className="pg-conversation">
      {messages.map((msg, i) => (
        <div key={i} className={`pg-msg pg-msg-${msg.role}`}>
          <span className="pg-msg-role">{msg.role === 'user' ? '▸ You' : '◂ Assistant'}</span>
          <div className="pg-msg-content">{msg.content}</div>
        </div>
      ))}
      <div ref={endRef} />
    </div>
  );
}

function ResponsePane({ label, response, streaming, loading, error, governanceMeta, governanceRecord, govLoading, conversation }) {
  return (
    <div className="pg-response-pane">
      {label && <div className="pg-pane-label">{label}</div>}
      {conversation && conversation.length > 0 && <ConversationHistory messages={conversation} />}
      <div className="pg-response-body">
        {loading && !streaming && (
          <div className="pg-response-loading">
            <div className="pg-loading-bar" />
            <span>Connecting to provider...</span>
          </div>
        )}
        {error && <div className="error-card" style={{ margin: 0 }}>{error}</div>}
        {(streaming || (!loading && !error && response)) && (
          <div className="pg-response-text">
            {response}
            {streaming && <span className="pg-cursor">▌</span>}
          </div>
        )}
        {!loading && !streaming && !error && !response && (!conversation || conversation.length === 0) && (
          <div className="pg-response-empty">
            <div className="pg-response-empty-icon">◇</div>
            <div>Every request here generates a real audit record.</div>
            <div style={{ fontSize: 11, marginTop: 4 }}>Send a prompt to begin.</div>
          </div>
        )}
      </div>
      <GovernanceReadout meta={governanceMeta} record={governanceRecord} loading={govLoading} />
    </div>
  );
}

/** Parse one SSE data line into a content delta string (or null). */
function parseSseDelta(line) {
  if (!line.startsWith('data: ')) return null;
  const payload = line.slice(6).trim();
  if (payload === '[DONE]') return null;
  try {
    const obj = JSON.parse(payload);
    return obj?.choices?.[0]?.delta?.content || null;
  } catch {
    return null;
  }
}

export default function Playground({ navigate }) {
  const [models, setModels] = useState([]);
  const [compare, setCompare] = useState(false);
  const [modelA, setModelA] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');
  const [userPrompt, setUserPrompt] = useState('');
  const [temperature, setTemperature] = useState(0.7);
  const [maxTokens, setMaxTokens] = useState(1024);
  const [userId, setUserId] = useState('playground-user');

  // Session ID — new UUID on mount and on clear
  const [sessionIdA, setSessionIdA] = useState(() => generateUUID());
  const [sessionIdB, setSessionIdB] = useState(() => generateUUID());

  // Model A state
  const [responseA, setResponseA] = useState('');
  const [streamingA, setStreamingA] = useState(false);
  const [loadingA, setLoadingA] = useState(false);
  const [errorA, setErrorA] = useState(null);
  const [govMetaA, setGovMetaA] = useState(null);
  const [govRecordA, setGovRecordA] = useState(null);
  const [govLoadingA, setGovLoadingA] = useState(false);
  const [conversationA, setConversationA] = useState([]);

  // Model B state
  const [modelB, setModelB] = useState('');
  const [responseB, setResponseB] = useState('');
  const [streamingB, setStreamingB] = useState(false);
  const [loadingB, setLoadingB] = useState(false);
  const [errorB, setErrorB] = useState(null);
  const [govMetaB, setGovMetaB] = useState(null);
  const [govRecordB, setGovRecordB] = useState(null);
  const [govLoadingB, setGovLoadingB] = useState(false);
  const [conversationB, setConversationB] = useState([]);

  // Abort controller ref for cancelling in-flight streams
  const abortRef = useRef(null);

  useEffect(() => {
    (async () => {
      try {
        const resp = await fetch('/v1/models');
        if (resp.ok) {
          const data = await resp.json();
          const ids = (data?.data || []).map(m => m.id).filter(Boolean);
          if (ids.length > 0) setModels(ids);
        }
      } catch {}
    })();
  }, []);

  useEffect(() => {
    if (!modelA && models.length > 0) setModelA(models[0]);
    if (!modelB && models.length > 1) setModelB(models[1]);
  }, [models]);

  const clearConversation = useCallback(() => {
    if (abortRef.current) { abortRef.current.abort(); abortRef.current = null; }
    setConversationA([]); setConversationB([]);
    setResponseA(''); setResponseB('');
    setErrorA(null); setErrorB(null);
    setGovMetaA(null); setGovMetaB(null);
    setGovRecordA(null); setGovRecordB(null);
    setStreamingA(false); setStreamingB(false);
    setLoadingA(false); setLoadingB(false);
    setSessionIdA(generateUUID());
    setSessionIdB(generateUUID());
  }, []);

  const sendRequest = useCallback(async (
    model, sessionId, conversation, setConversation,
    setResponse, setStreaming, setLoading, setError,
    setGovMeta, setGovRecord, setGovLoading, abortSignal,
  ) => {
    const prompt = userPrompt.trim();
    if (!prompt) return;

    setLoading(true);
    setError(null);
    setResponse('');
    setStreaming(false);
    setGovMeta(null);
    setGovRecord(null);

    // Build full message array with history
    const messages = [];
    if (systemPrompt.trim()) messages.push({ role: 'system', content: systemPrompt.trim() });
    messages.push(...conversation);
    messages.push({ role: 'user', content: prompt });

    // Add user message to conversation immediately
    setConversation(prev => [...prev, { role: 'user', content: prompt }]);

    const headers = { 'Content-Type': 'application/json' };
    if (sessionId) headers['X-Session-ID'] = sessionId;
    if (userId.trim()) headers['X-User-Id'] = userId.trim();
    const apiKey = getApiKey();
    if (apiKey) headers['X-API-Key'] = apiKey;

    try {
      const resp = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers,
        body: JSON.stringify({
          model,
          messages,
          temperature: parseFloat(temperature),
          max_tokens: parseInt(maxTokens, 10),
          stream: true,
        }),
        signal: abortSignal,
      });

      // Extract governance headers immediately
      const meta = {
        executionId: resp.headers.get('x-walacor-execution-id'),
        attestationId: resp.headers.get('x-walacor-attestation-id'),
        policyResult: resp.headers.get('x-walacor-policy-result'),
        chainSeq: resp.headers.get('x-walacor-chain-seq'),
      };
      setGovMeta(meta);

      if (!resp.ok) {
        const body = await resp.text();
        setError(`HTTP ${resp.status}: ${body}`);
        setLoading(false);
        // Remove the user message we optimistically added
        setConversation(prev => prev.slice(0, -1));
        return;
      }

      // Stream the response
      setLoading(false);
      setStreaming(true);
      let fullContent = '';

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let rawAccumulator = '';  // full raw response for JSON fallback

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        rawAccumulator += chunk;
        buffer += chunk;
        const lines = buffer.split('\n');
        buffer = lines.pop(); // keep incomplete line in buffer

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed || trimmed === 'data: [DONE]') continue;
          const delta = parseSseDelta(trimmed);
          if (delta) {
            fullContent += delta;
            setResponse(fullContent);
          }
        }
      }

      // Process any remaining buffer (SSE)
      if (buffer.trim() && buffer.trim() !== 'data: [DONE]') {
        const delta = parseSseDelta(buffer.trim());
        if (delta) {
          fullContent += delta;
          setResponse(fullContent);
        }
      }

      // Fallback: gateway may return plain JSON instead of SSE
      // (thinking models buffer <think> tokens → non-streaming JSON response)
      if (!fullContent) {
        const raw = (rawAccumulator + buffer).trim();
        try {
          const json = JSON.parse(raw);
          const msg = json?.choices?.[0]?.message;
          if (msg?.content) {
            fullContent = msg.content;
            setResponse(fullContent);
          }
        } catch {}
      }

      setStreaming(false);

      // Add assistant response to conversation
      if (fullContent) {
        setConversation(prev => [...prev, { role: 'assistant', content: fullContent }]);
      }

      // Fetch full execution record
      if (meta.executionId) {
        setGovLoading(true);
        try {
          const execData = await getExecution(meta.executionId);
          setGovRecord(execData?.record || null);
        } catch {}
        setGovLoading(false);
      }
    } catch (e) {
      if (e.name === 'AbortError') return;
      setError(e.message);
      setLoading(false);
      setStreaming(false);
      // Remove the user message we optimistically added
      setConversation(prev => prev.slice(0, -1));
    }
  }, [userPrompt, systemPrompt, temperature, maxTokens, userId]);

  const handleSend = useCallback(() => {
    if (!userPrompt.trim()) return;
    const controller = new AbortController();
    abortRef.current = controller;

    sendRequest(
      modelA, sessionIdA, conversationA, setConversationA,
      setResponseA, setStreamingA, setLoadingA, setErrorA,
      setGovMetaA, setGovRecordA, setGovLoadingA, controller.signal,
    );
    if (compare && modelB) {
      sendRequest(
        modelB, sessionIdB, conversationB, setConversationB,
        setResponseB, setStreamingB, setLoadingB, setErrorB,
        setGovMetaB, setGovRecordB, setGovLoadingB, controller.signal,
      );
    }

    setUserPrompt('');
  }, [userPrompt, modelA, modelB, sessionIdA, sessionIdB, conversationA, conversationB, compare, sendRequest]);

  const busy = loadingA || loadingB || streamingA || streamingB;

  return (
    <div className="fade-child">
      {/* Input controls */}
      <div className="pg-controls card">
        <div className="pg-controls-header">
          <div className="pg-section-label" style={{ marginBottom: 0 }}>◆ Prompt Playground</div>
          <div className="pg-header-actions">
            {(conversationA.length > 0 || conversationB.length > 0) && (
              <button className="pg-clear-btn" onClick={clearConversation} disabled={busy}>
                ✕ Clear
              </button>
            )}
            <button
              className={`pg-compare-toggle ${compare ? 'active' : ''}`}
              onClick={() => setCompare(!compare)}
            >
              <span className="pg-compare-icon">{compare ? '◆◆' : '◇◇'}</span>
              {compare ? 'Comparison Active' : 'Compare Models'}
            </button>
          </div>
        </div>

        {/* Model selectors */}
        <div className="pg-model-row">
          <div className="pg-field">
            <label className="pg-label">Model {compare ? 'A' : ''}</label>
            <select value={modelA} onChange={e => setModelA(e.target.value)} className="pg-select">
              {models.map(m => <option key={m} value={m}>{displayModel(m)}</option>)}
            </select>
          </div>
          {compare && (
            <div className="pg-field">
              <label className="pg-label">Model B</label>
              <select value={modelB} onChange={e => setModelB(e.target.value)} className="pg-select">
                {models.map(m => <option key={m} value={m}>{displayModel(m)}</option>)}
              </select>
            </div>
          )}
        </div>

        {/* Identity settings */}
        <div className="pg-identity-row">
          <div className="pg-field pg-field-inline">
            <label className="pg-label" title="Identifies who sent this request in the audit trail">User ID</label>
            <input
              type="text"
              value={userId}
              onChange={e => setUserId(e.target.value)}
              className="pg-text-input"
              placeholder="playground-user"
            />
          </div>
          <div className="pg-field pg-field-inline">
            <label className="pg-label">Session</label>
            <span className="pg-session-id mono">{sessionIdA.slice(0, 8)}…</span>
          </div>
          {getApiKey() && (
            <div className="pg-field pg-field-inline">
              <label className="pg-label">Auth</label>
              <span className="badge badge-pass" style={{ fontSize: 10 }}>API Key ✓</span>
            </div>
          )}
        </div>

        {/* System prompt */}
        <div className="pg-field">
          <label className="pg-label">
            System Prompt <span style={{ opacity: 0.5 }}>(optional)</span>
            <span className="pg-hint">Sets the personality or role for the AI. Example: "You are a helpful legal assistant."</span>
          </label>
          <textarea
            value={systemPrompt}
            onChange={e => setSystemPrompt(e.target.value)}
            className="pg-textarea"
            rows={2}
            placeholder="You are a helpful assistant..."
          />
        </div>

        {/* User prompt */}
        <div className="pg-field">
          <label className="pg-label">{conversationA.length > 0 ? 'Next Message' : 'User Prompt'}</label>
          <textarea
            value={userPrompt}
            onChange={e => setUserPrompt(e.target.value)}
            className="pg-textarea pg-textarea-main"
            rows={3}
            placeholder={conversationA.length > 0 ? 'Continue the conversation...' : 'Type your prompt here...'}
            onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !busy) handleSend(); }}
          />
        </div>

        {/* Parameters + send */}
        <div className="pg-params-row">
          <div className="pg-param">
            <label className="pg-label">
              Creativity
              <span className="pg-hint">How random or creative the response should be. Low = focused and predictable. High = varied and creative.</span>
            </label>
            <div className="pg-param-control">
              <span className="pg-range-label">Precise</span>
              <input type="range" min="0" max="2" step="0.1" value={temperature} onChange={e => setTemperature(e.target.value)} className="pg-slider" />
              <span className="pg-range-label">Creative</span>
              <span className="pg-param-value">{parseFloat(temperature).toFixed(1)}</span>
            </div>
          </div>
          <div className="pg-param">
            <label className="pg-label">
              Response Length
              <span className="pg-hint">Maximum number of words/tokens the model can generate. Higher = longer responses allowed.</span>
            </label>
            <input
              type="number" min="1" max="128000" value={maxTokens}
              onChange={e => setMaxTokens(e.target.value)}
              className="pg-number-input"
            />
          </div>
          <button
            onClick={handleSend}
            disabled={!userPrompt.trim() || busy}
            className="pg-send-btn"
          >
            <span className="pg-send-icon">▶</span>
            {busy ? 'Streaming...' : 'Send'}
            {!busy && <span className="pg-send-hint">⌘↵</span>}
          </button>
        </div>
      </div>

      {/* Response area */}
      <div className={`pg-results ${compare ? 'pg-results-compare' : ''}`}>
        <ResponsePane
          label={compare ? 'Model A' : null}
          response={responseA} streaming={streamingA} loading={loadingA} error={errorA}
          governanceMeta={govMetaA} governanceRecord={govRecordA} govLoading={govLoadingA}
          conversation={conversationA}
        />
        {compare && (
          <ResponsePane
            label="Model B"
            response={responseB} streaming={streamingB} loading={loadingB} error={errorB}
            governanceMeta={govMetaB} governanceRecord={govRecordB} govLoading={govLoadingB}
            conversation={conversationB}
          />
        )}
      </div>
    </div>
  );
}
