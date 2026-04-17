import { useState } from 'react';

const SUB_TABS = [
  { key: 'production', label: 'Production' },
  { key: 'candidates', label: 'Candidates' },
  { key: 'history',    label: 'Promotion History' },
  { key: 'verdicts',   label: 'Verdict Inspector' },
];

function Placeholder({ title, hint }) {
  return (
    <div className="card">
      <div className="card-head">
        <span className="card-title">{title}</span>
      </div>
      <div className="empty-state"><p>{hint}</p></div>
    </div>
  );
}

export default function Intelligence({ refresh }) {
  const [sub, setSub] = useState('production');

  return (
    <div className="fade-child">
      <div className="control-subnav">
        {SUB_TABS.map(t => (
          <button
            key={t.key}
            className={`control-subtab${sub === t.key ? ' active' : ''}`}
            onClick={() => setSub(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {sub === 'production' && (
        <Placeholder
          title="Production Models"
          hint="Loaded ONNX models, prediction counts, and trailing accuracy will appear here."
        />
      )}
      {sub === 'candidates' && (
        <Placeholder
          title="Candidate Models"
          hint="Shadow-validated candidates with promote and reject controls will appear here."
        />
      )}
      {sub === 'history' && (
        <Placeholder
          title="Promotion History"
          hint="Past promotions, rejections, and rollback controls will appear here."
        />
      )}
      {sub === 'verdicts' && (
        <Placeholder
          title="Verdict Inspector"
          hint="Per-model divergence breakdown, verdict log samples, and force-retrain controls will appear here."
        />
      )}
    </div>
  );
}
