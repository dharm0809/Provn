/* AgentRuns view — list of signed AgentRunManifest records, drill-down into
   the recon-event tree using the same chain-card UX as the Sessions view.
   The classification badge per row reflects the §9.4 cascade with the
   trigger reason on hover.

   Hooks discipline: every useState / useEffect / useMemo / useCallback runs
   BEFORE any if (...) return, per dashboard rules. */

import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { getAgentRuns, getAgentRun } from '../api';
import { timeAgo } from '../utils';
import '../styles/sessions-v2.css';
import '../styles/agent-runs.css';

function shortId(id, head = 8, tail = 4) {
  if (!id) return '—';
  if (id.length <= head + tail + 1) return id;
  return id.slice(0, head) + '…' + id.slice(-tail);
}

function fmtDuration(startTs, endTs) {
  if (!startTs || !endTs) return '—';
  const ms = new Date(endTs).getTime() - new Date(startTs).getTime();
  if (Number.isNaN(ms)) return '—';
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  return m + 'm ' + String(s % 60).padStart(2, '0') + 's';
}

const INTENT_COPY = {
  agent_run_step: { label: 'agent run step', tone: 'agent' },
  tool_loop_step: { label: 'tool loop turn', tone: 'tool' },
  tool_call_emitted: { label: 'tool call emitted', tone: 'tool' },
  chat: { label: 'plain chat', tone: 'chat' },
};

function IntentBadge({ intent, reason }) {
  const cfg = INTENT_COPY[intent] || INTENT_COPY.chat;
  return (
    <span
      className={`agt-badge agt-badge-${cfg.tone}`}
      title={reason || ''}
    >
      {cfg.label}
    </span>
  );
}

function ReasonsRow({ run }) {
  const reasons = [];
  if (run.framework_name && run.framework_name !== 'unknown') {
    reasons.push(<span key="fw">framework: {run.framework_name}</span>);
  }
  if (run.trace_id) reasons.push(<span key="tr">traceparent: {shortId(run.trace_id)}</span>);
  if (run.signed) reasons.push(<span key="s" className="agt-good">Ed25519 signed</span>);
  else reasons.push(<span key="s" className="agt-warn">unsigned</span>);
  if (!reasons.length) return null;
  return <div className="agt-reasons">{reasons.map((r, i) => (
    <React.Fragment key={i}>{i > 0 ? ' · ' : ''}{r}</React.Fragment>
  ))}</div>;
}

function RunCard({ run, onOpen }) {
  return (
    <div className="ses-chain-card agt-run-card" onClick={() => onOpen(run.run_id)}>
      <div className="ses-chain-card-head">
        <div className="ses-chain-card-id">
          <span className="agt-rid-mono">{shortId(run.run_id, 10, 6)}</span>
          <span className="agt-meta-sep"> · </span>
          <IntentBadge intent="agent_run_step" reason="Pillar 4 manifest" />
        </div>
        <div className="ses-chain-card-time">{timeAgo(run.end_ts)}</div>
      </div>
      <div className="ses-chain-card-body">
        <div className="agt-stat-row">
          <span><strong>{run.llm_call_count ?? 0}</strong> LLM calls</span>
          <span><strong>{run.tool_event_count ?? 0}</strong> tool events</span>
          <span>duration {fmtDuration(run.start_ts, run.end_ts)}</span>
          <span>ended via {run.end_reason || 'unknown'}</span>
        </div>
        <ReasonsRow run={run} />
      </div>
    </div>
  );
}

function EventNode({ ev }) {
  const intent =
    ev.kind === 'tool_call_observed' ? 'tool_call_emitted' :
    ev.kind === 'tool_result_observed' ? 'tool_loop_step' :
    'chat';
  const reason =
    ev.kind === 'tool_call_observed'
      ? `tool call ${ev.tool_name || ''} (id ${shortId(ev.tool_call_id)})`
      : ev.kind === 'tool_result_observed'
      ? `tool result for ${shortId(ev.tool_call_id)}`
      : 'observed message';
  return (
    <div className="ses-chain-card agt-event-card">
      <div className="ses-chain-card-head">
        <div className="ses-chain-card-id">
          <span className="agt-event-kind">{ev.kind}</span>
          <span className="agt-meta-sep"> · </span>
          <IntentBadge intent={intent} reason={reason} />
        </div>
        <div className="ses-chain-card-time">{ev.timestamp ? timeAgo(ev.timestamp) : '—'}</div>
      </div>
      <div className="ses-chain-card-body">
        <div className="agt-stat-row">
          {ev.tool_name ? <span>tool {ev.tool_name}</span> : null}
          {ev.tool_call_id ? <span>call id {shortId(ev.tool_call_id)}</span> : null}
          {ev.args_hash ? <span>args {shortId(ev.args_hash)}</span> : null}
          {ev.content_hash ? <span>result {shortId(ev.content_hash)}</span> : null}
          {ev.execution_id ? <span>exec {shortId(ev.execution_id)}</span> : null}
        </div>
      </div>
    </div>
  );
}

function RunDetail({ runId, onBack }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let live = true;
    setData(null);
    setError(null);
    getAgentRun(runId)
      .then((d) => { if (live) setData(d); })
      .catch((e) => { if (live) setError(String(e)); });
    return () => { live = false; };
  }, [runId]);

  const tree = useMemo(() => (data?.reconstructed_tool_events || []), [data]);

  if (error) return <div className="agt-err">Failed to load run: {error}</div>;
  if (!data) return <div className="agt-loading">Loading run…</div>;

  return (
    <div className="agt-detail">
      <div className="agt-detail-head">
        <button className="agt-back" onClick={onBack}>← Agent runs</button>
        <h2>Run {shortId(data.run_id, 12, 8)}</h2>
        <ReasonsRow run={data} />
      </div>
      <div className="agt-detail-meta">
        <span>{(data.manifest?.llm_calls?.length) || data.llm_call_count || 0} LLM calls</span>
        <span>{tree.length} reconstructed events</span>
        <span>start {data.start_ts}</span>
        <span>end {data.end_ts}</span>
        <span>via {data.end_reason}</span>
      </div>
      <h3 className="agt-section-h">LLM calls</h3>
      <div className="agt-llm-list">
        {(data.manifest?.llm_calls || []).map((c) => (
          <div key={c.record_id} className="ses-chain-card">
            <div className="ses-chain-card-head">
              <div className="ses-chain-card-id">
                <span className="agt-rid-mono">{shortId(c.record_id, 10, 4)}</span>
              </div>
              <div className="ses-chain-card-time">{c.timestamp ? timeAgo(c.timestamp) : '—'}</div>
            </div>
            <div className="ses-chain-card-body">
              <div className="agt-stat-row">
                <span>model {c.model || '—'}</span>
                <span>{c.walacor_dh ? 'anchored ' + shortId(c.walacor_dh) : 'pending anchor'}</span>
              </div>
            </div>
          </div>
        ))}
      </div>
      <h3 className="agt-section-h">Reconstructed tool events</h3>
      <div className="agt-event-list">
        {tree.length === 0
          ? <div className="agt-empty">no reconstructed events for this run</div>
          : tree.map((ev) => <EventNode key={ev.event_id} ev={ev} />)}
      </div>
    </div>
  );
}

export default function AgentRuns({ navigate, params }) {
  const [list, setList] = useState(null);
  const [error, setError] = useState(null);
  const [openRunId, setOpenRunId] = useState(params?.runId || null);

  const refetch = useCallback(() => {
    setError(null);
    getAgentRuns()
      .then((d) => setList(d))
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => { refetch(); }, [refetch]);
  useEffect(() => {
    setOpenRunId(params?.runId || null);
  }, [params?.runId]);

  const onOpen = useCallback((runId) => {
    setOpenRunId(runId);
    if (navigate) navigate('agent-runs', { runId });
  }, [navigate]);

  const onBack = useCallback(() => {
    setOpenRunId(null);
    if (navigate) navigate('agent-runs', {});
  }, [navigate]);

  // Hooks above this line — early returns OK below.
  if (openRunId) {
    return <RunDetail runId={openRunId} onBack={onBack} />;
  }
  if (error) return <div className="agt-err">Failed: {error}</div>;
  if (list == null) return <div className="agt-loading">Loading…</div>;

  const runs = list.runs || [];

  return (
    <div className="agt-runs">
      <div className="agt-runs-head">
        <h2>Agent runs</h2>
        <span className="agt-count">{list.total ?? runs.length} total</span>
      </div>
      {runs.length === 0
        ? <div className="agt-empty">
            No agent runs yet. Tag your traffic with a W3C traceparent or a
            metadata.agent_run_id to start producing signed manifests.
          </div>
        : runs.map((run) => (
            <RunCard key={run.run_id} run={run} onOpen={onOpen} />
          ))}
    </div>
  );
}
