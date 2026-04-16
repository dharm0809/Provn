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
const FRAMEWORK_INFO = {
  eu_ai_act: {
    summary: 'EU AI Act readiness report focused on governance, transparency, human oversight, and technical robustness.',
    includes: [
      'Risk and policy enforcement evidence',
      'Audit trail and chain integrity verification',
      'Model usage and accountability metadata',
      'Content safety and monitoring posture',
    ],
  },
  nist: {
    summary: 'NIST AI RMF mapping report aligned to Govern, Map, Measure, and Manage outcomes.',
    includes: [
      'Operational governance controls and gaps',
      'Measurement evidence from requests and decisions',
      'Monitoring and incident-readiness indicators',
      'Recommended improvements by risk area',
    ],
  },
  soc2: {
    summary: 'SOC 2 Type II-oriented control evidence summary for security, availability, and processing integrity.',
    includes: [
      'Access/auth mode and security configuration snapshot',
      'Audit logging and retention evidence',
      'Change and policy enforcement signals',
      'Integrity checks and operational reliability metrics',
    ],
  },
  iso42001: {
    summary: 'ISO 42001 AI management system evidence package for policy, risk, operations, and continual improvement.',
    includes: [
      'AI governance process evidence',
      'Risk and control implementation posture',
      'Traceability and lifecycle accountability artifacts',
      'Monitoring metrics and improvement recommendations',
    ],
  },
};

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
  const selectedFramework = FRAMEWORK_INFO[framework];

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
      {selectedFramework && (
        <div className="compliance-framework-info">
          <div className="compliance-framework-title">
            {FRAMEWORKS.find(f => f.id === framework)?.label} report
          </div>
          <p className="compliance-framework-summary">{selectedFramework.summary}</p>
          <div className="compliance-framework-includes-title">This report includes:</div>
          <ul className="compliance-framework-list">
            {selectedFramework.includes.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {loading && (
        <div className="card" style={{ padding: '32px 24px', textAlign: 'center', marginBottom: 16 }}>
          <div style={{ display: 'inline-block', width: 24, height: 24, border: '3px solid var(--border)', borderTopColor: 'var(--gold)', borderRadius: '50%', animation: 'spin 0.8s linear infinite', marginBottom: 12 }} />
          <div style={{ fontFamily: 'var(--mono)', fontSize: 13, color: 'var(--text-secondary)' }}>
            Generating {FRAMEWORKS.find(f => f.id === framework)?.label} report…
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 6 }}>
            Verifying chain integrity across all sessions. This may take 30–60 seconds.
          </div>
        </div>
      )}

      {error && <div className="compliance-error">{error}</div>}

      {preview && (
        <div className="compliance-preview">
          {/* ── Audit Readiness Score ─────────────────────────────── */}
          {preview.audit_readiness && (() => {
            const ar = preview.audit_readiness;
            const gradeColor = { A: '#22c55e', B: '#84cc16', C: '#eab308', D: '#f97316', F: '#ef4444' }[ar.grade] || 'var(--text-muted)';
            return (
              <div className="card" style={{ marginBottom: 20, padding: 20 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 24, flexWrap: 'wrap', marginBottom: 20 }}>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: 48, fontWeight: 700, color: gradeColor, lineHeight: 1 }}>{ar.score}</div>
                    <div style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '1px', color: 'var(--text-muted)', marginTop: 4 }}>Readiness Score</div>
                  </div>
                  <div style={{ fontSize: 64, fontWeight: 800, color: gradeColor, lineHeight: 1, opacity: 0.3 }}>{ar.grade}</div>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 8 }}>Audit Readiness Assessment</div>
                    <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                      {ar.strengths?.length > 0 && <span style={{ color: '#22c55e' }}>{ar.strengths.length} strengths</span>}
                      {ar.gaps?.length > 0 && <span style={{ marginLeft: 12, color: '#f97316' }}>{ar.gaps.length} gaps</span>}
                      {ar.recommendations?.length > 0 && <span style={{ marginLeft: 12 }}>{ar.recommendations.length} recommendations</span>}
                    </div>
                  </div>
                </div>

                {/* Dimension bars */}
                <div style={{ display: 'grid', gap: 8 }}>
                  {ar.dimensions?.map((dim, i) => (
                    <div key={i}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 3 }}>
                        <span style={{ fontWeight: 500 }}>{dim.name}</span>
                        <span style={{ fontFamily: 'var(--mono)', color: dim.score >= 80 ? '#22c55e' : dim.score >= 50 ? '#eab308' : '#ef4444' }}>{dim.score}%</span>
                      </div>
                      <div style={{ height: 6, background: 'var(--bg-inset)', borderRadius: 3, overflow: 'hidden' }}>
                        <div style={{ height: '100%', width: `${dim.score}%`, background: dim.score >= 80 ? '#22c55e' : dim.score >= 50 ? '#eab308' : '#ef4444', borderRadius: 3, transition: 'width 0.5s ease' }} />
                      </div>
                      {dim.evidence?.length > 0 && (
                        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
                          {dim.evidence.join(' · ')}
                        </div>
                      )}
                    </div>
                  ))}
                </div>

                {/* Gaps */}
                {ar.gaps?.length > 0 && (
                  <div style={{ marginTop: 16, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
                    <div style={{ fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.8px', color: 'var(--text-muted)', marginBottom: 8 }}>Gaps & Recommendations</div>
                    {ar.gaps.map((gap, i) => (
                      <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 8, fontSize: 12 }}>
                        <span className={`badge ${gap.severity === 'critical' ? 'badge-fail' : gap.severity === 'warning' ? 'badge-warn' : 'badge-muted'}`} style={{ fontSize: 10, flexShrink: 0 }}>
                          {gap.severity}
                        </span>
                        <div>
                          <div style={{ fontWeight: 500, color: 'var(--text-primary)' }}>{gap.issue}</div>
                          <div style={{ color: 'var(--text-muted)', marginTop: 2 }}>{gap.fix}</div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {/* Strengths */}
                {ar.strengths?.length > 0 && (
                  <div style={{ marginTop: 12, paddingTop: 12, borderTop: '1px solid var(--border)' }}>
                    <div style={{ fontSize: 12, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.8px', color: 'var(--text-muted)', marginBottom: 8 }}>Strengths</div>
                    {ar.strengths.map((s, i) => (
                      <div key={i} style={{ fontSize: 12, color: '#22c55e', marginBottom: 4 }}>
                        {s}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })()}

          {/* ── Summary Stats ────────────────────────────────────── */}
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
                  <details style={{ marginTop: 8 }}>
                    <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--text-muted)' }}>
                      {invalid.length} session{invalid.length > 1 ? 's' : ''} with issues
                    </summary>
                    <div className="card" style={{ marginTop: 8, padding: 12 }}>
                      {invalid.slice(0, 10).map((s, i) => (
                        <div key={i} style={{ fontSize: 12, fontFamily: 'var(--mono)', color: 'var(--text-secondary)', marginBottom: 4 }}>
                          <span style={{ color: 'var(--red)' }}>{s.session_id.slice(0, 12)}...</span>
                          {' — '}{(s.errors || []).join('; ') || 'unknown error'}
                        </div>
                      ))}
                      {invalid.length > 10 && <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>...and {invalid.length - 10} more</div>}
                    </div>
                  </details>
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
