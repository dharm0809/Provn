import { Component } from 'react';

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  render() {
    const { error } = this.state;
    if (error) {
      const msg = error?.message || String(error);
      return (
        <div
          style={{
            minHeight: '100vh',
            padding: 32,
            fontFamily: 'system-ui, sans-serif',
            background: 'var(--bg-root, #f4f1ea)',
            color: 'var(--text-primary, #1a1714)',
          }}
        >
          <h1 style={{ fontSize: 20, marginBottom: 12 }}>Lineage dashboard crashed</h1>
          <p style={{ marginBottom: 16, opacity: 0.85 }}>
            Open the browser console (F12 → Console) for the full stack trace. Details:
          </p>
          <pre
            style={{
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              padding: 16,
              borderRadius: 8,
              background: 'var(--bg-inset, #edeae2)',
              border: '1px solid var(--border, #ddd8cd)',
              fontSize: 13,
              maxHeight: '50vh',
              overflow: 'auto',
            }}
          >
            {msg}
          </pre>
          <button
            type="button"
            style={{ marginTop: 20, padding: '10px 18px', cursor: 'pointer', fontWeight: 600 }}
            onClick={() => window.location.reload()}
          >
            Reload page
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
