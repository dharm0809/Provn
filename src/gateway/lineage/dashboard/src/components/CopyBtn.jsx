import React, { useState, useCallback } from 'react';

/**
 * Inline copy button. Monospace, ghost-bordered, flips to green
 * "copied" state for 1200ms on success.
 *
 * Props:
 *   value     string to copy
 *   label     optional override label, default "copy"
 *   compact   if true, render as single icon-sized affordance
 */
export default function CopyBtn({ value, label = 'copy', compact = false }) {
  const [copied, setCopied] = useState(false);

  const onClick = useCallback(async (e) => {
    e.stopPropagation();
    if (!value) return;
    try {
      await navigator.clipboard.writeText(String(value));
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* noop */
    }
  }, [value]);

  return (
    <button
      type="button"
      className={'exec-copy-btn' + (copied ? ' is-copied' : '')}
      onClick={onClick}
      title={copied ? 'Copied' : 'Copy to clipboard'}
    >
      {copied ? '✓ copied' : (compact ? '⧉' : label)}
    </button>
  );
}
