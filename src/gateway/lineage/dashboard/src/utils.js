export function timeAgo(ts) {
  if (!ts) return '-';
  const diff = (Date.now() - new Date(ts).getTime()) / 1000;
  if (diff < 0) return 'just now';
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

export function formatTime(ts) {
  if (!ts) return '-';
  try { return new Date(ts).toLocaleString(); } catch { return ts; }
}

export function formatUptime(seconds) {
  if (seconds < 60) return Math.floor(seconds) + 's';
  if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
  if (seconds < 86400) return Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm';
  return Math.floor(seconds / 86400) + 'd ' + Math.floor((seconds % 86400) / 3600) + 'h';
}

export function formatNumber(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}

export function displayModel(m) {
  if (!m) return '';
  m = m.replace(/^self-attested:/, '');
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(m)) {
    return m.substring(0, 8) + '…';
  }
  return m;
}

export function formatSessionId(id) {
  if (!id) return '-';
  if (/^[0-9a-f]{8}-/.test(id)) return id.substring(0, 13) + '…';
  if (id.length > 28) return id.substring(0, 28) + '…';
  return id;
}

export function truncId(id, len = 16) {
  if (!id) return '-';
  return id.length > len ? id.substring(0, len) + '…' : id;
}

export function truncHash(h, len = 16) {
  if (!h) return '-';
  return h.substring(0, len) + '…';
}

export function getTokenCount(record) {
  const m = record.metadata;
  if (m && m.token_usage && m.token_usage.total_tokens) return m.token_usage.total_tokens;
  return null;
}

export function dispositionClass(d) {
  if (!d) return 'badge-muted';
  if (d === 'allowed' || d === 'forwarded') return 'badge-pass';
  if (d.startsWith('denied')) return 'badge-fail';
  if (d.startsWith('error')) return 'badge-fail';
  return 'badge-muted';
}

export function dispositionLabel(d) {
  if (!d) return '-';
  return d.replace(/_/g, ' ').toUpperCase();
}

export function policyBadgeClass(result) {
  if (!result) return 'badge-muted';
  if (result === 'pass') return 'badge-pass';
  if (result === 'denied' || result === 'blocked') return 'badge-fail';
  if (result.includes('flag')) return 'badge-warn';
  return 'badge-muted';
}

export function verdictBadgeClass(verdict) {
  if (!verdict) return 'badge-muted';
  const v = verdict.toLowerCase();
  if (v === 'pass') return 'badge-pass';
  if (v === 'block') return 'badge-fail';
  if (v === 'warn') return 'badge-warn';
  return 'badge-muted';
}

export function statusCodeClass(code) {
  if (code < 300) return 'badge-pass';
  if (code < 500) return 'badge-warn';
  return 'badge-fail';
}
