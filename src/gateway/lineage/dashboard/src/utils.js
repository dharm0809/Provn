// Skip work when the tab isn't visible. Polling timers still fire on schedule
// so the UI snaps back the instant the user returns, but we don't burn
// bandwidth or server CPU while the tab is in the background.
export function isTabVisible() {
  return typeof document === 'undefined' || document.visibilityState !== 'hidden';
}

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
  if (d === 'allowed' || d === 'forwarded') return 'ALLOWED';
  if (d === 'error_provider') return 'ERROR PROVIDER';
  if (d.startsWith('error')) return 'ERROR';
  return d.replace(/_/g, ' ').toUpperCase();
}

/** Readable labels for summary strips (avoid ALL CAPS like `ERROR PARSE` being misread as “parse error”). */
export function dispositionSummaryLabel(d) {
  if (!d) return '—';
  const known = {
    allowed: 'Allowed',
    forwarded: 'Forwarded',
    denied_auth: 'Denied · auth',
    denied_policy: 'Denied · policy',
    denied_attestation: 'Denied · attestation',
    error_parse: 'Rejected · invalid JSON body',
    error_provider: 'Error · upstream provider',
  };
  if (known[d]) return known[d];
  if (d.startsWith('denied_')) return `Denied · ${d.slice(7).replace(/_/g, ' ')}`;
  if (d.startsWith('error_')) return `Error · ${d.slice(6).replace(/_/g, ' ')}`;
  return d.replace(/_/g, ' ');
}

/**
 * User-facing explanation for the Attempts disposition popover.
 * Attempt rows do not store a per-request message; this maps disposition (+ HTTP status) to guidance.
 */
export function dispositionDetailedHelp(disposition, statusCode) {
  const d = disposition || '';
  const code = statusCode != null && !Number.isNaN(Number(statusCode)) ? Number(statusCode) : null;
  const httpLine =
    code === 401
      ? 'HTTP 401 — authentication failed or credentials were missing.'
      : code === 403
        ? 'HTTP 403 — the caller was not permitted to perform this action.'
        : code === 429
          ? 'HTTP 429 — rate limiting or quota may have applied.'
          : code === 413
            ? 'HTTP 413 — the request body was too large.'
            : code != null && code >= 500
              ? `HTTP ${code} — the gateway or an upstream component returned a server error.`
              : code != null && code >= 400
                ? `HTTP ${code} — the request did not complete successfully.`
                : null;

  const paragraphs = [];

  const explain = (body) => {
    paragraphs.push(body);
  };

  if (d === 'allowed' || d === 'forwarded') {
    explain(
      d === 'forwarded'
        ? 'This request was accepted and forwarded toward the model provider according to your routing rules. No gateway block was applied for this attempt.'
        : 'This request passed gateway checks (auth, policy, and storage where applicable) and was processed. If you need the full prompt, tools, and timings, open the execution trace from this row.'
    );
  } else if (d === 'denied_auth') {
    explain(
      'The gateway rejected the caller before policy or forwarding. Typical causes: missing or invalid API key, wrong key for this route, or auth middleware failure.'
    );
  } else if (d === 'denied_policy') {
    explain(
      'A configured policy blocked this request (for example OPA/Rego rules, model allowlists, or content constraints). Adjust policies or the request payload to proceed.'
    );
  } else if (d === 'denied_by_opa') {
    explain('Open Policy Agent (OPA) evaluated this request and returned a deny decision for your configured rules.');
  } else if (d === 'denied_attestation') {
    explain(
      'Attestation or integrity checks failed (for example model or deployment attestation did not satisfy required proofs).'
    );
  } else if (d === 'denied_budget') {
    explain('Token or spend budget for this tenant or key was exceeded or reserved capacity was unavailable.');
  } else if (d === 'denied_rate_limit') {
    explain('This request exceeded configured per-key or global rate limits for the gateway.');
  } else if (d === 'denied_wal_full') {
    explain('The gateway could not persist audit state (for example WAL or storage back-pressure). The request was rejected to preserve the completeness invariant.');
  } else if (d === 'denied_response_policy') {
    explain('A response policy blocked or modified the outcome after the upstream model returned (output-side enforcement).');
  } else if (d.startsWith('denied_')) {
    explain(
      `The gateway recorded disposition “${d.replace(/_/g, ' ')}”. This is a block or reject path before a normal completion; check gateway logs and configuration for this category.`
    );
  } else if (d === 'error_parse') {
    explain('The gateway could not parse the request body as valid JSON (or the expected chat/completions shape).');
  } else if (d === 'error_provider') {
    explain(
      'The upstream model provider returned an error or unreachable response after the gateway accepted the request. Verify provider status, model id, and network path.'
    );
  } else if (d === 'error_config') {
    explain('Gateway configuration is inconsistent or incomplete for this route (for example missing adapter, model mapping, or provider URL).');
  } else if (d === 'error_no_adapter') {
    explain('No adapter was available for this path or content type; routing or OpenAPI mapping may be missing.');
  } else if (d === 'error_method_not_allowed') {
    explain('The HTTP method is not allowed for this endpoint.');
  } else if (d === 'error_overloaded') {
    explain('The gateway reported overload and declined to take this request; retry later or scale capacity.');
  } else if (d === 'error_gateway') {
    explain('An internal gateway error occurred while handling this request.');
  } else if (d.startsWith('error_')) {
    explain(
      `The gateway recorded “${d.replace(/_/g, ' ')}”. Inspect gateway logs around this request_id for the underlying exception or status.`
    );
  } else if (d === 'audit_only_allowed') {
    explain('The request was allowed in an audit-only mode (logging without full forward or with restricted forward), per gateway configuration.');
  } else if (d) {
    explain(`Disposition “${d.replace(/_/g, ' ')}” — see gateway documentation or logs for this category.`);
  } else {
    explain('No disposition was recorded for this attempt.');
  }

  if (httpLine && (d.startsWith('denied') || d.startsWith('error') || (code != null && code >= 400))) {
    paragraphs.push(httpLine);
  }

  return { paragraphs };
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

export function copyToClipboard(text) {
  if (navigator.clipboard) {
    return navigator.clipboard.writeText(text);
  }
  const el = document.createElement('textarea');
  el.value = text;
  el.style.position = 'fixed';
  el.style.opacity = '0';
  document.body.appendChild(el);
  el.select();
  document.execCommand('copy');
  document.body.removeChild(el);
  return Promise.resolve();
}

export function formatBytes(bytes) {
  if (!bytes || bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

/** Returns { icon, label, badgeClass } for a file based on mimetype and filename. */
export function fileTypeInfo(mimetype, filename) {
  const mime = (mimetype || '').toLowerCase();
  const ext = (filename || '').split('.').pop().toLowerCase();

  if (mime.startsWith('image/'))                                    return { icon: '🖼️', label: 'Image',       badgeClass: 'badge-blue' };
  if (mime === 'application/pdf' || ext === 'pdf')                  return { icon: '📕', label: 'PDF',         badgeClass: 'badge-file' };
  if (mime.includes('spreadsheet') || mime.includes('csv')
      || ext === 'csv' || ext === 'xlsx' || ext === 'xls')         return { icon: '📊', label: 'Spreadsheet', badgeClass: 'badge-file' };
  if (mime.includes('word') || mime.includes('document')
      || ext === 'docx' || ext === 'doc')                          return { icon: '📝', label: 'Document',    badgeClass: 'badge-file' };
  if (mime.includes('presentation') || ext === 'pptx'
      || ext === 'ppt')                                            return { icon: '📽️', label: 'Slides',      badgeClass: 'badge-file' };
  if (mime.startsWith('text/') || ext === 'txt' || ext === 'md'
      || ext === 'log')                                            return { icon: '📄', label: 'Text',        badgeClass: 'badge-muted' };
  if (mime.includes('json') || ext === 'json')                     return { icon: '{ }', label: 'JSON',       badgeClass: 'badge-muted' };
  if (mime.includes('xml') || ext === 'xml')                       return { icon: '📋', label: 'XML',         badgeClass: 'badge-muted' };
  if (mime.startsWith('audio/'))                                    return { icon: '🎵', label: 'Audio',       badgeClass: 'badge-blue' };
  if (mime.startsWith('video/'))                                    return { icon: '🎬', label: 'Video',       badgeClass: 'badge-blue' };
  if (mime.includes('zip') || mime.includes('tar')
      || mime.includes('gzip') || ext === 'zip' || ext === 'gz')   return { icon: '📦', label: 'Archive',     badgeClass: 'badge-muted' };
  if (ext === 'py' || ext === 'js' || ext === 'ts' || ext === 'java'
      || ext === 'c' || ext === 'cpp' || ext === 'go'
      || ext === 'rs' || ext === 'rb' || ext === 'sh')             return { icon: '💻', label: 'Code',        badgeClass: 'badge-gold' };
  return                                                             { icon: '📄', label: 'File',        badgeClass: 'badge-muted' };
}

export function fmtPct(x, digits = 1) {
  if (x == null || Number.isNaN(Number(x))) return '—';
  return (Number(x) * 100).toFixed(digits) + '%';
}

export function fmtDelta(cand, prod) {
  if (cand == null || prod == null) return '—';
  const d = Number(cand) - Number(prod);
  if (Number.isNaN(d)) return '—';
  const sign = d >= 0 ? '+' : '';
  return sign + (d * 100).toFixed(2) + 'pp';
}

export function asArray(v) {
  if (!v) return [];
  if (Array.isArray(v)) return v;
  try { const p = JSON.parse(v); return Array.isArray(p) ? p : []; } catch { return []; }
}
