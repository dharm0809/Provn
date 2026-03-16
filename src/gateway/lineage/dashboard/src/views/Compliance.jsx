import { useState } from 'react';
const COMPLIANCE_API = '/v1/compliance';

async function fetchJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) {
    const body = await resp.text().catch(() => '');
    throw new Error(`HTTP ${resp.status}${body ? ': ' + body : ''}`);
  }
  return resp.json();
}
const FRAMEWORKS = [
  { id: 'eu_ai_act', label: 'EU AI Act' },
  { id: 'nist', label: 'NIST AI RMF' },
  { id: 'soc2', label: 'SOC 2 Type II' },
  { id: 'iso42001', label: 'ISO 42001' },
];

const FORMAT_MIME = { json: 'application/json', csv: 'text/csv', pdf: 'application/pdf' };

function today() {
  return new Date().toISOString().slice(0, 10);
}
function thirtyDaysAgo() {
  const d = new Date();
  d.setDate(d.getDate() - 30);
  return d.toISOString().slice(0, 10);
}

export default function Compliance() {
  const [start, setStart] = useState(thirtyDaysAgo);
  const [end, setEnd] = useState(today);
  const [framework, setFramework] = useState('eu_ai_act');
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [downloading, setDownloading] = useState(null);
  const [error, setError] = useState(null);

  const fetchPreview = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchJSON(
        `${COMPLIANCE_API}/export?format=json&framework=${framework}&start=${start}&end=${end}`
      );
      setPreview(data);
    } catch (e) {
      setError(e.message);
      setPreview(null);
    } finally {
      setLoading(false);
    }
  };

  const handleDownload = async (fmt) => {
    setDownloading(fmt);
    setError(null);
    try {
      const url = `${COMPLIANCE_API}/export?format=${fmt}&framework=${framework}&start=${start}&end=${end}`;
      const resp = await fetch(url);
      if (!resp.ok) {
        if (resp.status === 501) {
          throw new Error(`${fmt.toUpperCase()} export is not available on this server (requires system libraries)`);
        }
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${resp.status}`);
      }
      const blob = await resp.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `compliance-${framework}-${start}-${end}.${fmt}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      setError(`Download failed: ${e.message}`);
    } finally {
      setDownloading(null);
    }
  };

  return (
    <div className="compliance-view">
      <h2 style={{ marginTop: 0 }}>Compliance Export</h2>

      <div className="compliance-controls">
        <div className="compliance-field">
          <label>Start Date</label>
          <input type="date" value={start} onChange={e => setStart(e.target.value)} />
        </div>
        <div className="compliance-field">
          <label>End Date</label>
          <input type="date" value={end} onChange={e => setEnd(e.target.value)} />
        </div>
        <div className="compliance-field">
          <label>Framework</label>
          <select value={framework} onChange={e => setFramework(e.target.value)}>
            {FRAMEWORKS.map(f => (
              <option key={f.id} value={f.id}>{f.label}</option>
            ))}
          </select>
        </div>
        <div className="compliance-field" style={{ alignSelf: 'flex-end' }}>
          <button className="btn-primary" onClick={fetchPreview} disabled={loading}>
            {loading ? 'Loading...' : 'Preview'}
          </button>
        </div>
      </div>

      {error && <div className="compliance-error">{error}</div>}

      {preview && (
        <div className="compliance-preview">
          <h3>Summary ({preview.report?.period?.start} — {preview.report?.period?.end})</h3>
          <div className="compliance-stats">
            <div className="stat-card">
              <div className="stat-value">{preview.summary?.total_requests ?? 0}</div>
              <div className="stat-label">Total Requests</div>
            </div>
            <div className="stat-card">
              <div className="stat-value">{preview.summary?.allowed ?? 0}</div>
              <div className="stat-label">Allowed</div>
            </div>
            <div className="stat-card">
              <div className="stat-value">{preview.summary?.denied ?? 0}</div>
              <div className="stat-label">Denied</div>
            </div>
            <div className="stat-card">
              <div className="stat-value">{preview.chain_integrity?.sessions_verified ?? 0}</div>
              <div className="stat-label">Sessions Verified</div>
            </div>
          </div>

          {preview.summary?.models_used?.length > 0 && (
            <p><strong>Models:</strong> {preview.summary.models_used.join(', ')}</p>
          )}

          {(() => {
            const ci = preview.chain_integrity;
            const total = ci?.sessions_verified ?? 0;
            const invalid = (ci?.sessions || []).filter(s => !s.valid);
            const valid = total - invalid.length;
            return (
              <div style={{ marginBottom: 16 }}>
                <p>
                  <strong>Chain Integrity:</strong>{' '}
                  {ci?.all_valid
                    ? <span className="badge-compliant">ALL VALID</span>
                    : <span className="badge-error">{valid} / {total} VALID</span>
                  }
                </p>
                {invalid.length > 0 && (
                  <div className="card" style={{ marginTop: 8, padding: 12 }}>
                    <div style={{ fontSize: 11, fontFamily: 'var(--mono)', color: 'var(--text-muted)', marginBottom: 6, letterSpacing: '1px', textTransform: 'uppercase' }}>
                      {invalid.length} session{invalid.length > 1 ? 's' : ''} with issues
                    </div>
                    {invalid.map((s, i) => (
                      <div key={i} style={{ fontSize: 12, fontFamily: 'var(--mono)', color: 'var(--text-secondary)', marginBottom: 4 }}>
                        <span style={{ color: 'var(--red)' }}>{s.session_id.slice(0, 12)}...</span>
                        {' — '}{(s.errors || []).join('; ') || 'unknown error'}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })()}

          <h3>Download</h3>
          <div className="compliance-downloads">
            {['json', 'csv', 'pdf'].map(fmt => (
              <button
                key={fmt}
                className="btn-download"
                onClick={() => handleDownload(fmt)}
                disabled={downloading === fmt}
              >
                {downloading === fmt ? 'Downloading...' : fmt.toUpperCase()}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
