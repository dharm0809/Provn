import { normalizeTimelineRecords } from './utils';

const API = '/v1/lineage';
const CTRL_API = '/v1/control';
const HEALTH_URL = '/health';
const METRICS_URL = '/metrics';

// ─── Lineage API ────────────────────────────────────────────────

async function fetchJSON(url) {
  const key = getControlKey();
  const resp = await fetch(url, key ? { headers: { 'X-API-Key': key } } : {});
  if (!resp.ok) {
    const body = await resp.text().catch(() => '');
    throw new Error(`HTTP ${resp.status}${body ? ': ' + body : ''}`);
  }
  return resp.json();
}

export async function getHealth() {
  return fetchJSON(HEALTH_URL);
}

export async function getMetrics() {
  const resp = await fetch(METRICS_URL);
  if (!resp.ok) throw new Error('metrics fetch failed');
  return resp.text();
}

export async function getSessions(limit = 50, offset = 0, opts = {}) {
  const sp = new URLSearchParams();
  sp.set('limit', String(limit));
  sp.set('offset', String(offset));
  if (opts.q != null && String(opts.q).trim()) sp.set('q', String(opts.q).trim());
  if (opts.sort) sp.set('sort', opts.sort);
  if (opts.order) sp.set('order', opts.order);
  return fetchJSON(`${API}/sessions?${sp.toString()}`);
}

export async function getSession(sessionId) {
  const enc = encodeURIComponent(String(sessionId ?? ''));
  const data = await fetchJSON(`${API}/sessions/${enc}`);
  if (data && data.records != null) {
    data.records = normalizeTimelineRecords(data.records);
  }
  return data;
}

export async function getExecution(executionId) {
  return fetchJSON(`${API}/executions/${executionId}`);
}

export async function getAttempts(limit = 100, offset = 0, opts = {}) {
  const sp = new URLSearchParams();
  sp.set('limit', String(limit));
  sp.set('offset', String(offset));
  if (opts.q != null && String(opts.q).trim()) sp.set('q', String(opts.q).trim());
  if (opts.sort) sp.set('sort', opts.sort);
  if (opts.order) sp.set('order', opts.order);
  return fetchJSON(`${API}/attempts?${sp.toString()}`);
}

export async function getTokenLatency(range) {
  const sp = new URLSearchParams({ range: String(range) });
  return fetchJSON(`${API}/token-latency?${sp.toString()}`);
}

export async function getThroughputHistory(range) {
  const sp = new URLSearchParams({ range: String(range) });
  const data = await fetchJSON(`${API}/metrics?${sp.toString()}`);
  if (data.buckets) {
    data.buckets = data.buckets.map(b => ({ ...b, request_count: b.total }));
  }
  return data;
}

// Progressive-enhancement SSE subscription for live throughput.
//
// Returns a cancellation function. The caller registers an onData handler
// and receives the exact same shape `getThroughputHistory` returns.
//
// If the browser has no EventSource (ancient environments), or the backend
// returns 404/5xx on first connect, we fall through to a standard
// setInterval poll of the REST endpoint so the UI is never left without
// data. All connected browsers share a single backend single-flight cache
// regardless of which transport they use.
export function subscribeThroughput(range, onData, { pollMs = 3000 } = {}) {
  let cancelled = false;
  const normalize = (data) => {
    if (data && data.buckets) {
      data.buckets = data.buckets.map(b => ({ ...b, request_count: b.total }));
    }
    return data;
  };

  if (typeof EventSource === 'undefined') {
    return _pollFallback(range, onData, normalize, pollMs, () => cancelled);
  }

  let es = null;
  let fallbackCleanup = null;
  try {
    es = new EventSource(`${API}/metrics/stream?${new URLSearchParams({ range: String(range) }).toString()}`);
    es.onmessage = (e) => {
      if (cancelled) return;
      try { onData(normalize(JSON.parse(e.data))); } catch { /* ignore malformed frame */ }
    };
    es.onerror = () => {
      // First error after open: server likely lacks the endpoint or proxy
      // is buffering. Close SSE and fall back to polling transparently.
      if (cancelled || fallbackCleanup) return;
      try { es.close(); } catch {}
      fallbackCleanup = _pollFallback(range, onData, normalize, pollMs, () => cancelled);
    };
  } catch {
    fallbackCleanup = _pollFallback(range, onData, normalize, pollMs, () => cancelled);
  }

  return () => {
    cancelled = true;
    if (es) { try { es.close(); } catch {} }
    if (fallbackCleanup) fallbackCleanup();
  };
}

function _pollFallback(range, onData, normalize, pollMs, isCancelled) {
  const tick = async () => {
    if (isCancelled()) return;
    try {
      const data = await fetchJSON(`${API}/metrics?${new URLSearchParams({ range: String(range) }).toString()}`);
      if (!isCancelled()) onData(normalize(data));
    } catch { /* swallow; next tick retries */ }
  };
  tick();
  const id = setInterval(tick, pollMs);
  return () => clearInterval(id);
}

export async function getTrace(executionId) {
  return fetchJSON(`${API}/trace/${executionId}`);
}

// Live Walacor envelope for a sealed record.
// Returns { execution_id, envelope, local, match } — envelope may be null
// when Walacor is unreachable or the record isn't yet delivered; the UI
// should render "seal pending" / "unreachable" in those cases based on HTTP
// status (502 / 503 / 200-with-envelope=null) but we still surface the body.
export async function getSealEnvelope(executionId) {
  const key = getControlKey();
  const resp = await fetch(
    `${API}/envelope/${encodeURIComponent(executionId)}`,
    key ? { headers: { 'X-API-Key': key } } : {},
  );
  // 502 / 503 still include a body with the fallback local anchor — parse
  // and return it rather than throwing, so the drawer can degrade gracefully.
  if (resp.status === 401 || resp.status === 403) {
    clearControlKey();
    throw new Error('AUTH');
  }
  if (resp.status === 404) {
    throw new Error('NOT_FOUND');
  }
  try {
    return await resp.json();
  } catch {
    throw new Error(`HTTP ${resp.status}`);
  }
}

export async function verifySession(sessionId) {
  return fetchJSON(`${API}/verify/${sessionId}`);
}

// ─── Control Plane API ──────────────────────────────────────────

function getControlKey() {
  return sessionStorage.getItem('cp_api_key') || '';
}

export function setControlKey(key) {
  sessionStorage.setItem('cp_api_key', key);
}

export function clearControlKey() {
  sessionStorage.removeItem('cp_api_key');
}

export function hasControlKey() {
  return !!getControlKey();
}

export { fetchControlJSON as fetchAuthJSON };
async function fetchControlJSON(url) {
  const key = getControlKey();
  const resp = await fetch(url, {
    headers: key ? { 'X-API-Key': key } : {},
  });
  if (resp.status === 401 || resp.status === 403) {
    clearControlKey();
    throw new Error('AUTH');
  }
  if (!resp.ok) {
    const body = await resp.text().catch(() => '');
    throw new Error(`HTTP ${resp.status}${body ? ': ' + body : ''}`);
  }
  return resp.json();
}

async function controlFetch(url, options = {}) {
  const key = getControlKey();
  options.headers = {
    'Content-Type': 'application/json',
    ...(key ? { 'X-API-Key': key } : {}),
    ...(options.headers || {}),
  };
  const resp = await fetch(url, options);
  if (resp.status === 401 || resp.status === 403) {
    clearControlKey();
    throw new Error('AUTH');
  }
  if (!resp.ok) {
    const body = await resp.text().catch(() => '');
    throw new Error(`HTTP ${resp.status}${body ? ': ' + body : ''}`);
  }
  return resp.json();
}

export async function getControlStatus() {
  return fetchControlJSON(`${CTRL_API}/status`);
}

// ─── Readiness API ──────────────────────────────────────────────

export async function getReadiness({ fresh = false } = {}) {
  const qs = fresh ? '?fresh=1' : '';
  const key = getControlKey();
  const resp = await fetch(`/v1/readiness${qs}`, key ? { headers: { 'X-API-Key': key } } : {});
  if (resp.status === 401 || resp.status === 403) {
    clearControlKey();
    throw new Error('AUTH');
  }
  if (resp.status === 503) {
    throw new Error('DISABLED');
  }
  if (!resp.ok) {
    const body = await resp.text().catch(() => '');
    throw new Error(`HTTP ${resp.status}${body ? ': ' + body : ''}`);
  }
  return resp.json();
}

export async function getAttestations() {
  return fetchControlJSON(`${CTRL_API}/attestations`);
}

export async function createAttestation(data) {
  return controlFetch(`${CTRL_API}/attestations`, { method: 'POST', body: JSON.stringify(data) });
}

export async function revokeAttestation(id) {
  return controlFetch(`${CTRL_API}/attestations/${id}/revoke`, { method: 'POST' });
}

export async function approveAttestation(data) {
  return controlFetch(`${CTRL_API}/attestations`, { method: 'POST', body: JSON.stringify(data) });
}

export async function removeAttestation(id) {
  return controlFetch(`${CTRL_API}/attestations/${id}`, { method: 'DELETE' });
}

export async function discoverModels() {
  return fetchControlJSON(`${CTRL_API}/discover`);
}

export async function getPolicies() {
  return fetchControlJSON(`${CTRL_API}/policies`);
}

export async function createPolicy(data) {
  return controlFetch(`${CTRL_API}/policies`, { method: 'POST', body: JSON.stringify(data) });
}

export async function deletePolicy(id) {
  return controlFetch(`${CTRL_API}/policies/${id}`, { method: 'DELETE' });
}

export async function getBudgets() {
  return fetchControlJSON(`${CTRL_API}/budgets`);
}

export async function createBudget(data) {
  return controlFetch(`${CTRL_API}/budgets`, { method: 'POST', body: JSON.stringify(data) });
}

export async function deleteBudget(id) {
  return controlFetch(`${CTRL_API}/budgets/${id}`, { method: 'DELETE' });
}

// ─── Content policies (analyzer thresholds) ──────────────────────

export async function getContentPolicies() {
  return fetchControlJSON(`${CTRL_API}/content-policies`);
}

export async function upsertContentPolicy(data) {
  return controlFetch(`${CTRL_API}/content-policies`, { method: 'POST', body: JSON.stringify(data) });
}

export async function deleteContentPolicy(id) {
  return controlFetch(`${CTRL_API}/content-policies/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

// ─── Model pricing ───────────────────────────────────────────────

export async function getPricing() {
  return fetchControlJSON(`${CTRL_API}/pricing`);
}

export async function upsertPricing(data) {
  return controlFetch(`${CTRL_API}/pricing`, { method: 'POST', body: JSON.stringify(data) });
}

export async function deletePricing(id) {
  return controlFetch(`${CTRL_API}/pricing/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

// ─── Policy templates ────────────────────────────────────────────

export async function listTemplates() {
  return fetchControlJSON(`${CTRL_API}/templates`);
}

export async function applyTemplate(name) {
  return controlFetch(`${CTRL_API}/templates/${encodeURIComponent(name)}/apply`, { method: 'POST' });
}

// ─── Intelligence API (Phase 25) ───────────────────────────────

const INTEL_API = `${CTRL_API}/intelligence`;

export async function getIntelligenceModels() {
  return fetchControlJSON(`${INTEL_API}/models`);
}

export async function getIntelligenceCandidates() {
  return fetchControlJSON(`${INTEL_API}/candidates`);
}

export async function getIntelligenceHistory(model, limit = 50) {
  const sp = new URLSearchParams({ limit: String(limit) });
  return fetchControlJSON(`${INTEL_API}/history/${encodeURIComponent(model)}?${sp.toString()}`);
}

export async function promoteCandidate(model, version) {
  return controlFetch(
    `${INTEL_API}/promote/${encodeURIComponent(model)}/${encodeURIComponent(version)}`,
    { method: 'POST' },
  );
}

export async function rejectCandidate(model, version, reason) {
  const sp = new URLSearchParams();
  if (reason) sp.set('reason', reason);
  const qs = sp.toString();
  return controlFetch(
    `${INTEL_API}/reject/${encodeURIComponent(model)}/${encodeURIComponent(version)}${qs ? '?' + qs : ''}`,
    { method: 'POST' },
  );
}

export async function rollbackModel(model) {
  return controlFetch(
    `${INTEL_API}/rollback/${encodeURIComponent(model)}`,
    { method: 'POST' },
  );
}

export async function forceRetrain(model) {
  return controlFetch(
    `${INTEL_API}/retrain/${encodeURIComponent(model)}`,
    { method: 'POST' },
  );
}

export async function getIntelligenceVerdicts(model, opts = {}) {
  const sp = new URLSearchParams();
  sp.set('model', String(model ?? ''));
  if (opts.divergence_only) sp.set('divergence_only', 'true');
  if (opts.limit != null) sp.set('limit', String(opts.limit));
  return fetchControlJSON(`${INTEL_API}/verdicts?${sp.toString()}`);
}

// ─── Prometheus Metrics Parser ──────────────────────────────────

export function parsePrometheusMetrics(text) {
  const metrics = {};
  for (const line of text.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed[0] === '#') continue;
    const braceIdx = trimmed.indexOf('{');
    let name, labels, value;
    if (braceIdx >= 0) {
      name = trimmed.substring(0, braceIdx);
      const closeIdx = trimmed.indexOf('}', braceIdx);
      labels = trimmed.substring(braceIdx + 1, closeIdx);
      value = parseFloat(trimmed.substring(trimmed.indexOf(' ', closeIdx) + 1));
    } else {
      const spaceIdx = trimmed.indexOf(' ');
      if (spaceIdx < 0) continue;
      name = trimmed.substring(0, spaceIdx);
      labels = '';
      value = parseFloat(trimmed.substring(spaceIdx + 1));
    }
    if (isNaN(value)) continue;
    if (!metrics[name]) metrics[name] = [];
    metrics[name].push({ labels, value });
  }
  return metrics;
}

export function sumMetric(metrics, name, filter) {
  const entries = metrics[name];
  if (!entries) return 0;
  let total = 0;
  for (const e of entries) {
    if (filter && !e.labels.includes(filter)) continue;
    total += e.value;
  }
  return total;
}
