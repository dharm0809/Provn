/* Mock data + helpers for Walacor Gateway Overview demo */

const MODELS = [
  'claude-sonnet-4.5', 'claude-opus-4', 'gpt-4.1', 'gpt-4o', 'gemini-2.5-pro',
  'llama-3.3-70b', 'mistral-large-2', 'claude-haiku-4.5', 'deepseek-v3',
];
const PATHS = [
  '/v1/messages', '/v1/chat/completions', '/v1/completions',
  '/v1/embeddings', '/v1/messages', '/v1/chat/completions',
  '/v1/messages:stream', '/v1/chat/completions',
];
const DISPOSITIONS = [
  { d: 'allowed', w: 82 },
  { d: 'allowed', w: 0 },
  { d: 'denied_policy', w: 6 },
  { d: 'denied_auth', w: 4 },
  { d: 'denied_budget', w: 2 },
  { d: 'error_provider', w: 3 },
  { d: 'denied_rate_limit', w: 3 },
];

function pickWeighted(items) {
  const total = items.reduce((s, i) => s + i.w, 0);
  let r = Math.random() * total;
  for (const it of items) { r -= it.w; if (r <= 0) return it.d; }
  return items[0].d;
}
function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
function uuid() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    const v = c === 'x' ? r : (r & 0x3 | 0x8);
    return v.toString(16);
  });
}

window.MockData = {
  health: {
    status: 'healthy',
    enforcement_mode: 'enforced',
    content_analyzers: 7,
    session_chain: true,
    uptime_seconds: 3 * 86400 + 14 * 3600 + 22 * 60,
    storage: true,
    wal: true,
  },

  genThroughput(range) {
    // Returns N buckets across the range with realistic req/s shape
    const POINTS = { '1h': 60, '24h': 48, '7d': 56, '30d': 60 };
    const n = POINTS[range] || 60;
    const out = [];
    const base = range === '1h' ? 3.2 : range === '24h' ? 2.8 : range === '7d' ? 2.1 : 1.7;
    for (let i = 0; i < n; i++) {
      const phase = (i / n) * Math.PI * 4;
      const trend = Math.sin(phase) * 0.6 + Math.sin(phase * 2.3) * 0.3;
      const noise = (Math.random() - 0.5) * 0.4;
      const rps = Math.max(0.05, base + trend + noise);
      const total = rps * 60; // per minute
      const blockRate = 0.04 + Math.random() * 0.05 + (i === Math.floor(n * 0.7) ? 0.15 : 0);
      const blocked = Math.round(total * blockRate);
      const allowed = Math.round(total - blocked);
      out.push({ i, t: labelFor(range, i, n), rps, allowed, blocked, total: allowed + blocked });
    }
    return out;
  },

  genTokens(range) {
    const POINTS = { '1h': 60, '24h': 48, '7d': 56, '30d': 60 };
    const n = POINTS[range] || 60;
    const out = [];
    const pBase = range === '1h' ? 4200 : 3400;
    const cBase = range === '1h' ? 1600 : 1300;
    for (let i = 0; i < n; i++) {
      const phase = (i / n) * Math.PI * 4;
      const trend = Math.sin(phase) * 0.4 + Math.sin(phase * 1.7 + 1) * 0.3;
      const noise = (Math.random() - 0.5) * 0.3;
      const mult = 1 + trend + noise;
      const prompt = Math.max(200, Math.round(pBase * mult));
      const completion = Math.max(80, Math.round(cBase * mult * (0.8 + Math.random() * 0.4)));
      const latency = 240 + trend * 180 + noise * 80 + (i === Math.floor(n * 0.7) ? 260 : 0);
      out.push({
        i, t: labelFor(range, i, n),
        prompt, completion,
        avg: Math.max(80, Math.round(latency)),
        count: Math.round(20 + mult * 40),
      });
    }
    return out;
  },

  genSessions() {
    const out = [];
    for (let i = 0; i < 6; i++) {
      const ago = (i + 1) * (30 + Math.random() * 240);
      out.push({
        session_id: uuid(),
        model: pick(MODELS),
        record_count: Math.floor(Math.random() * 40) + 2,
        last_activity: new Date(Date.now() - ago * 1000).toISOString(),
      });
    }
    return out;
  },

  genActivity() {
    const out = [];
    for (let i = 0; i < 10; i++) {
      const ago = (i + 1) * (8 + Math.random() * 40);
      const disp = pickWeighted(DISPOSITIONS);
      out.push({
        execution_id: uuid(),
        disposition: disp,
        model_id: pick(MODELS),
        path: pick(PATHS),
        method: Math.random() > 0.1 ? 'POST' : 'GET',
        timestamp: new Date(Date.now() - ago * 1000).toISOString(),
      });
    }
    return out;
  },
};

function labelFor(range, i, n) {
  const now = Date.now();
  const total = range === '1h' ? 3600e3 : range === '24h' ? 86400e3 : range === '7d' ? 7 * 86400e3 : 30 * 86400e3;
  const step = total / n;
  const t = new Date(now - total + i * step);
  if (range === '1h' || range === '24h') {
    return t.toTimeString().slice(0, 5);
  }
  return (t.getMonth() + 1) + '/' + t.getDate();
}

window.labelFor = labelFor;
window.pickWeighted = pickWeighted;
window.pick = pick;
window.uuid = uuid;

// Helpers
window.timeAgo = function(ts) {
  if (!ts) return '-';
  const diff = (Date.now() - new Date(ts).getTime()) / 1000;
  if (diff < 60) return Math.max(1, Math.floor(diff)) + 's ago';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
};

window.formatNumber = function(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n));
};

window.formatUptime = function(s) {
  if (s < 60) return Math.floor(s) + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h';
};

window.formatSessionId = function(id) {
  if (!id) return '-';
  return id.substring(0, 13) + '…';
};

window.displayModel = function(m) {
  if (!m) return '';
  return m;
};

window.dispositionMeta = function(d) {
  if (!d) return { cls: 'disp-allowed', label: '-' };
  if (d === 'allowed' || d === 'forwarded') return { cls: 'disp-allowed', label: 'ALLOW' };
  if (d.startsWith('denied')) return { cls: 'disp-blocked', label: 'BLOCK' };
  if (d.startsWith('error')) return { cls: 'disp-error', label: 'ERROR' };
  return { cls: 'disp-allowed', label: d.toUpperCase() };
};
