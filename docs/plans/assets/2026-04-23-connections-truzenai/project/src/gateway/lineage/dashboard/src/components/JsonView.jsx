import React, { useMemo } from 'react';
import CopyBtn from './CopyBtn.jsx';

/**
 * Collapsible, syntax-highlighted JSON viewer.
 * Hand-rolled highlighter (no highlight.js, no deps) coloured via
 * existing --gold / --green / --amber tokens.
 *
 * Props:
 *   data        any JSON-serialisable value
 *   label       head label, default "JSON"
 *   initialOpen default false
 */
export default function JsonView({ data, label = 'JSON', initialOpen = false }) {
  const [open, setOpen] = React.useState(initialOpen);

  const pretty = useMemo(() => {
    try { return JSON.stringify(data, null, 2); } catch { return String(data); }
  }, [data]);

  const html = useMemo(() => highlight(pretty), [pretty]);

  return (
    <div className="exec-json-wrap">
      <div
        className="exec-json-head"
        onClick={() => setOpen(v => !v)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setOpen(v => !v); }}
      >
        <span className="exec-json-head-label">
          {open ? '▾' : '▸'}&nbsp;&nbsp;{label}
        </span>
        <CopyBtn value={pretty} />
      </div>
      {open && (
        <pre
          className="exec-json-body"
          dangerouslySetInnerHTML={{ __html: html }}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* tiny JSON highlighter                                              */
/* ------------------------------------------------------------------ */

function escapeHtml(s) {
  return s.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c]);
}

function highlight(src) {
  const safe = escapeHtml(src);
  // order matters: strings first (so keywords inside strings don't match)
  return safe.replace(
    /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(?:\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      if (/^"/.test(match)) {
        if (/:$/.test(match)) return `<span class="j-key">${match.replace(/:$/, '')}</span><span class="j-punct">:</span>`;
        return `<span class="j-str">${match}</span>`;
      }
      if (/true|false/.test(match)) return `<span class="j-bool">${match}</span>`;
      if (/null/.test(match))      return `<span class="j-null">${match}</span>`;
      return `<span class="j-num">${match}</span>`;
    }
  );
}
