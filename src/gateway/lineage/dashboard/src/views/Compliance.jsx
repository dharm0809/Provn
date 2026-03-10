import { useState } from 'react';

const COMPLIANCE_API = '/v1/compliance';
const FRAMEWORKS = [
  { id: 'eu_ai_act', label: 'EU AI Act' },
  { id: 'nist', label: 'NIST AI RMF' },
  { id: 'soc2', label: 'SOC 2 Type II' },
  { id: 'iso42001', label: 'ISO 42001' },
];

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
  const [error, setError] = useState(null);

  const fetchPreview = async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch(
        `${COMPLIANCE_API}/export?format=json&framework=${framework}&start=${start}&end=${end}`
      );
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${resp.status}`);
      }
      setPreview(await resp.json());
    } catch (e) {
      setError(e.message);
      setPreview(null);
    } finally {
      setLoading(false);
    }
  };

  const downloadUrl = (fmt) =>
    `${COMPLIANCE_API}/export?format=${fmt}&framework=${framework}&start=${start}&end=${end}`;

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

          <p>
            <strong>Chain Integrity:</strong>{' '}
            {preview.chain_integrity?.all_valid
              ? <span className="badge-compliant">ALL VALID</span>
              : <span className="badge-error">INTEGRITY ISSUES</span>
            }
          </p>

          <h3>Download</h3>
          <div className="compliance-downloads">
            <a href={downloadUrl('json')} download className="btn-download">JSON</a>
            <a href={downloadUrl('csv')} download className="btn-download">CSV</a>
            <a href={downloadUrl('pdf')} download className="btn-download">PDF</a>
          </div>
        </div>
      )}
    </div>
  );
}
