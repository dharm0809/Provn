/* Walacor Gateway — Connections mock scenarios + tile metadata.
   Shared between v1 / v2 / v3 / v4 preview pages so they read the same
   three scenarios and any tweak here propagates everywhere. */

(function () {
  const now = Date.now();
  const iso = (sec) => new Date(now - sec * 1000).toISOString();

  const TILE_ORDER = [
    'providers',
    'walacor_delivery',
    'analyzers',
    'tool_loop',
    'model_capabilities',
    'control_plane',
    'auth',
    'readiness',
    'streaming',
    'intelligence_worker',
  ];

  /* Group assignment for v2 swim-lanes and v4 blast-radius.
     Providers = upstream you don't control.
     Infra     = the gateway's own machinery.
     Policy    = governance & compliance plane. */
  const TILE_GROUP = {
    providers:           'Providers',
    walacor_delivery:    'Infra',
    analyzers:           'Policy',
    tool_loop:           'Infra',
    model_capabilities:  'Policy',
    control_plane:       'Policy',
    auth:                'Infra',
    readiness:           'Policy',
    streaming:           'Infra',
    intelligence_worker: 'Infra',
  };

  const TILE_META = {
    providers:           { label: 'Providers',           blurb: 'Upstream LLM backends' },
    walacor_delivery:    { label: 'Walacor Delivery',    blurb: 'Sealed write pipeline'  },
    analyzers:           { label: 'Analyzers',           blurb: 'Content safety probes'  },
    tool_loop:           { label: 'Tool Loop',           blurb: 'Tool-executor pipeline' },
    model_capabilities:  { label: 'Model Capabilities',  blurb: 'Per-model feature flags'},
    control_plane:       { label: 'Control Plane',       blurb: 'Policy cache & sync'    },
    auth:                { label: 'Auth',                blurb: 'API-key / JWT / JWKS'   },
    readiness:           { label: 'Readiness',           blurb: 'Phase-26 rollup'        },
    streaming:           { label: 'Streaming',           blurb: 'SSE interruption tally' },
    intelligence_worker: { label: 'Intelligence Worker', blurb: 'Async training queue'   },
  };

  const tile = (id, status, headline, subline, detail, last_change = 300) => ({
    id, status, headline, subline,
    last_change_ts: last_change == null ? null : iso(last_change),
    detail,
  });

  const happy = {
    generated_at: iso(0),
    ttl_seconds: 3,
    overall_status: 'green',
    tiles: [
      tile('providers', 'green', '3 providers · 0.4% err', 'openai · anthropic · ollama', {
        providers: {
          openai:    { error_rate_60s: 0.004, cooldown_until: null, last_error: null },
          anthropic: { error_rate_60s: 0.002, cooldown_until: null, last_error: null },
          ollama:    { error_rate_60s: 0.000, cooldown_until: null, last_error: null },
        },
      }, 1800),
      tile('walacor_delivery', 'green', '99.8% success · 0 pending', 'last success 1.2s ago', {
        success_rate_60s: 0.998, pending_writes: 0,
        last_failure: null,
        last_success_ts: iso(1.2),
        time_since_last_success_s: 1.2,
      }, 3600),
      tile('analyzers', 'green', '4 enabled · 0 fail-opens', 'llama_guard · presidio_pii · safety · prompt_guard', {
        analyzers: {
          llama_guard:       { enabled: true, fail_opens_60s: 0, last_fail_open: null },
          presidio_pii:      { enabled: true, fail_opens_60s: 0, last_fail_open: null },
          safety_classifier: { enabled: true, fail_opens_60s: 0, last_fail_open: null },
          prompt_guard:      { enabled: true, fail_opens_60s: 0, last_fail_open: null },
        },
      }, 7200),
      tile('tool_loop', 'green', '42 loops · 0 exceptions', 'failure rate 0.00%', {
        exceptions_60s: 0, last_exception: null, loops_60s: 42, failure_rate_60s: 0.00,
      }, 600),
      tile('model_capabilities', 'green', '8 models · 0 auto-disabled', 'all capabilities intact', {
        auto_disabled_count: 0,
        models: [
          { model_id: 'gpt-4o',            supports_tools: true, auto_disabled: false, since: null },
          { model_id: 'claude-3-5-sonnet', supports_tools: true, auto_disabled: false, since: null },
          { model_id: 'llama-3.1-70b',     supports_tools: true, auto_disabled: false, since: null },
          { model_id: 'mistral-large',     supports_tools: true, auto_disabled: false, since: null },
        ],
      }, null),
      tile('control_plane', 'green', 'v42 · synced 7s ago', 'embedded · 3 policies · 4 attestations', {
        mode: 'embedded',
        policy_cache: { version: 'v42', last_sync_ts: iso(7), age_s: 7, stale: false },
        sync_task_alive: true, attestations_count: 4, policies_count: 3,
      }, 45),
      tile('auth', 'green', 'JWT + API key · JWKS fresh', 'bootstrap key stable', {
        auth_mode: 'both', jwt_configured: true,
        jwks_last_fetch_ts: iso(90), jwks_last_error: null, bootstrap_key_stable: true,
      }, 86400),
      tile('readiness', 'green', 'READY · 0 reds · 0 ambers', '2 degraded rows in last 24h', {
        rollup: 'ready', reds: [], ambers: [], degraded_rows_24h: 2,
      }, 1200),
      tile('streaming', 'green', '18 streams · 0 interruptions', 'interruption rate 0.00%', {
        interruptions_60s: 0, last_interruption: null, streams_60s: 18, interruption_rate_60s: 0.00,
      }, 900),
      tile('intelligence_worker', 'green', 'running · 3 queued', 'oldest job 1.2s · 18,432 rows', {
        running: true, queue_depth: 3, oldest_job_age_s: 1.2, last_error: null,
        last_training_at: iso(3600), verdict_log_rows: 18432,
      }, 240),
    ],
    events: [],
  };

  const amber = {
    ...happy,
    generated_at: iso(0),
    overall_status: 'amber',
    tiles: happy.tiles.map((t) => {
      if (t.id === 'analyzers') {
        return tile('analyzers', 'amber', '4 enabled · 2 fail-opens', 'presidio_pii opened 2× in last 60s', {
          analyzers: {
            llama_guard:       { enabled: true, fail_opens_60s: 0, last_fail_open: null },
            presidio_pii:      { enabled: true, fail_opens_60s: 2, last_fail_open: { ts: iso(38), reason: 'presidio worker timeout' } },
            safety_classifier: { enabled: true, fail_opens_60s: 0, last_fail_open: null },
            prompt_guard:      { enabled: true, fail_opens_60s: 0, last_fail_open: null },
          },
        }, 38);
      }
      if (t.id === 'model_capabilities') {
        return tile('model_capabilities', 'amber', '8 models · 1 auto-disabled', 'llama-3.1-70b: tools disabled', {
          auto_disabled_count: 1,
          models: [
            { model_id: 'gpt-4o',            supports_tools: true,  auto_disabled: false, since: null },
            { model_id: 'claude-3-5-sonnet', supports_tools: true,  auto_disabled: false, since: null },
            { model_id: 'llama-3.1-70b',     supports_tools: false, auto_disabled: true,  since: iso(1800) },
            { model_id: 'mistral-large',     supports_tools: true,  auto_disabled: false, since: null },
          ],
        }, 1800);
      }
      if (t.id === 'auth') {
        return tile('auth', 'amber', 'JWT + API key · bootstrap drift', 'bootstrap key unstable — rotating', {
          auth_mode: 'both', jwt_configured: true,
          jwks_last_fetch_ts: iso(300), jwks_last_error: null, bootstrap_key_stable: false,
        }, 120);
      }
      return t;
    }),
    events: [
      { ts: iso(12),  subsystem: 'analyzers',          severity: 'amber', message: 'presidio_pii fail-open · worker timeout after 8000ms',     session_id: 'sess_8f3c21a4', execution_id: 'exec_9d12', request_id: 'req_a1b2' },
      { ts: iso(38),  subsystem: 'analyzers',          severity: 'amber', message: 'presidio_pii fail-open · connection refused',              session_id: 'sess_41ec9b11', execution_id: null,        request_id: 'req_c3d4' },
      { ts: iso(120), subsystem: 'auth',               severity: 'amber', message: 'bootstrap key rotated — stability flag cleared',            session_id: null,            execution_id: null,        request_id: null },
      { ts: iso(1800),subsystem: 'model_capabilities', severity: 'info',  message: 'llama-3.1-70b auto-disabled tools after 3 tool-call failures', session_id: null,          execution_id: null,        request_id: null },
      { ts: iso(2100),subsystem: 'readiness',          severity: 'info',  message: 'SEC-01 transient amber cleared',                             session_id: null,            execution_id: null,        request_id: null },
    ],
  };

  const red = {
    ...amber,
    generated_at: iso(0),
    overall_status: 'red',
    tiles: amber.tiles.map((t) => {
      if (t.id === 'walacor_delivery') {
        return tile('walacor_delivery', 'red', 'WALACOR UNREACHABLE', '18 pending · last success 3m 14s ago', {
          success_rate_60s: 0.12, pending_writes: 18,
          last_failure: { ts: iso(4), op: 'PUT /envelope', detail: 'connect ETIMEDOUT 10.0.4.12:7443' },
          last_success_ts: iso(194),
          time_since_last_success_s: 194,
        }, 194);
      }
      if (t.id === 'tool_loop') {
        return tile('tool_loop', 'amber', '18 loops · 2 exceptions', 'last: python_exec · AttributeError', {
          exceptions_60s: 2,
          last_exception: { ts: iso(22), tool: 'python_exec', error: "AttributeError: 'NoneType' object has no attribute 'context'" },
          loops_60s: 18, failure_rate_60s: 0.11,
        }, 22);
      }
      return t;
    }),
    events: [
      { ts: iso(4),   subsystem: 'walacor_delivery', severity: 'red',   message: 'envelope PUT failed · connect ETIMEDOUT 10.0.4.12:7443', session_id: 'sess_b7d441c0', execution_id: 'exec_12cd', request_id: 'req_e5f6' },
      { ts: iso(11),  subsystem: 'walacor_delivery', severity: 'red',   message: 'envelope PUT failed · connect ETIMEDOUT 10.0.4.12:7443', session_id: 'sess_a2f391be', execution_id: 'exec_33ef', request_id: 'req_g7h8' },
      { ts: iso(22),  subsystem: 'tool_loop',        severity: 'amber', message: "python_exec exception swallowed · AttributeError: 'NoneType'…", session_id: 'sess_c091e2fa', execution_id: 'exec_77ab', request_id: 'req_i9j0' },
      { ts: iso(38),  subsystem: 'analyzers',        severity: 'amber', message: 'presidio_pii fail-open · connection refused',            session_id: 'sess_41ec9b11', execution_id: null, request_id: 'req_c3d4' },
      { ts: iso(44),  subsystem: 'walacor_delivery', severity: 'red',   message: 'envelope PUT failed · connect ETIMEDOUT 10.0.4.12:7443', session_id: 'sess_3f8cd92e', execution_id: 'exec_ab12', request_id: null },
      { ts: iso(62),  subsystem: 'walacor_delivery', severity: 'amber', message: 'pending queue crossed threshold · 12 pending writes',    session_id: null, execution_id: null, request_id: null },
      { ts: iso(120), subsystem: 'auth',             severity: 'amber', message: 'bootstrap key rotated — stability flag cleared',         session_id: null, execution_id: null, request_id: null },
      { ts: iso(194), subsystem: 'walacor_delivery', severity: 'info',  message: 'last successful envelope · kafka topic lag back to 0',   session_id: 'sess_last_good', execution_id: 'exec_last', request_id: null },
      { ts: iso(1800),subsystem: 'model_capabilities',severity: 'info', message: 'llama-3.1-70b auto-disabled tools after 3 tool-call failures', session_id: null, execution_id: null, request_id: null },
    ],
  };

  /* Recent-changes lane for v4 incident cockpit.
     Not in the backend contract — these are policy / config / deploy
     events surfaced from other systems. Shown only in v4's red mode. */
  const RECENT_CHANGES = [
    { ts: iso(180),   kind: 'deploy',   title: 'gateway deployed · sha 4a7b21',          actor: 'cd-pipeline',   risk: 'medium' },
    { ts: iso(420),   kind: 'policy',   title: 'content policy updated · pii-strict',    actor: 'ops@walacor',   risk: 'low' },
    { ts: iso(1200),  kind: 'infra',    title: 'walacor cluster 10.0.4.12 · restart',    actor: 'platform',      risk: 'high' },
    { ts: iso(3600),  kind: 'secret',   title: 'bootstrap key rotated',                  actor: 'sec-auto',      risk: 'medium' },
    { ts: iso(7200),  kind: 'model',    title: 'llama-3.1-70b tools disabled (auto)',    actor: 'guardian',      risk: 'low' },
  ];

  window.ConnectionsMocks = {
    TILE_ORDER,
    TILE_GROUP,
    TILE_META,
    RECENT_CHANGES,
    scenarios: { happy, amber, red },
  };
})();
