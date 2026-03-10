const API = '/v1/lineage';
const CTRL_API = '/v1/control';
const HEALTH_URL = '/health';
const METRICS_URL = '/metrics';

// ─── Lineage API ────────────────────────────────────────────────

async function fetchJSON(url) {
  const resp = await fetch(url);
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

export async function getSessions(limit = 50, offset = 0) {
  return fetchJSON(`${API}/sessions?limit=${limit}&offset=${offset}`);
}

export async function getSession(sessionId) {
  return fetchJSON(`${API}/sessions/${sessionId}`);
}

export async function getExecution(executionId) {
  return fetchJSON(`${API}/executions/${executionId}`);
}

export async function getAttempts(limit = 100, offset = 0) {
  return fetchJSON(`${API}/attempts?limit=${limit}&offset=${offset}`);
}

export async function getTokenLatency(range) {
  return fetchJSON(`${API}/token-latency?range=${range}`);
}

export async function getThroughputHistory(range) {
  const data = await fetchJSON(`${API}/metrics?range=${range}`);
  if (data.buckets) {
    data.buckets = data.buckets.map(b => ({ ...b, request_count: b.total }));
  }
  return data;
}

export async function getTrace(executionId) {
  return fetchJSON(`${API}/trace/${executionId}`);
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
