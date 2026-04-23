/* Mock request-attempt stream for the Attempts view.
   Generates a realistic mix of allowed/blocked/errored requests with
   enough metadata to populate detail popovers. */

(function () {
  const MODELS = [
    { id: 'claude-sonnet-4.5', provider: 'anthropic' },
    { id: 'claude-opus-4',     provider: 'anthropic' },
    { id: 'claude-haiku-4.5',  provider: 'anthropic' },
    { id: 'gpt-4.1',           provider: 'openai' },
    { id: 'gpt-4o',            provider: 'openai' },
    { id: 'gpt-4o-mini',       provider: 'openai' },
    { id: 'gemini-2.5-pro',    provider: 'google' },
    { id: 'llama-3.3-70b',     provider: 'meta' },
    { id: 'mistral-large-2',   provider: 'mistral' },
    { id: 'deepseek-v3',       provider: 'deepseek' },
  ];

  const USERS = [
    'alicia.chen', 'marcus.webb', 'priya.ram', 'j.kowalski',
    'dev-agent-07', 'sarah.oconnor', 'tom.nguyen', 'r.delacroix',
    'svc-ingest-02', 'svc-rag-03', 'ci-tests', 'customer-bot-prod',
  ];

  const PATHS = [
    { path: '/v1/messages',          method: 'POST', w: 26 },
    { path: '/v1/chat/completions',  method: 'POST', w: 22 },
    { path: '/v1/messages:stream',   method: 'POST', w: 14 },
    { path: '/v1/completions',       method: 'POST', w: 8 },
    { path: '/v1/embeddings',        method: 'POST', w: 12 },
    { path: '/v1/models',            method: 'GET',  w: 6 },
    { path: '/v1/audit/records',     method: 'GET',  w: 4 },
    { path: '/v1/tools/invoke',      method: 'POST', w: 8 },
  ];

  const DISPOSITIONS = [
    { d: 'forwarded',           code: 200, w: 48 },
    { d: 'allowed',             code: 200, w: 18 },
    { d: 'denied_policy',       code: 403, w: 8,  reason: 'policy.block_pii_exfiltration' },
    { d: 'denied_auth',         code: 401, w: 5,  reason: 'missing api key' },
    { d: 'denied_budget',       code: 429, w: 3,  reason: 'tenant spend budget exceeded' },
    { d: 'denied_rate_limit',   code: 429, w: 4,  reason: 'per-key rate limit' },
    { d: 'denied_attestation',  code: 403, w: 2,  reason: 'model attestation mismatch' },
    { d: 'denied_wal_full',     code: 503, w: 1,  reason: 'audit-chain back-pressure' },
    { d: 'error_provider',      code: 502, w: 3,  reason: 'upstream timeout (anthropic)' },
    { d: 'error_parse',         code: 400, w: 2,  reason: 'invalid JSON body' },
    { d: 'error_overloaded',    code: 503, w: 1,  reason: 'gateway at capacity' },
    { d: 'audit_only_allowed',  code: 200, w: 2 },
  ];

  const ANALYZERS = [
    'walacor.integrity', 'pii.detector', 'secret.scanner', 'jailbreak.classifier',
    'toxicity.guard',    'pii.redactor',  'model.allowlist', 'budget.tracker',
  ];

  function pickW(items) {
    const total = items.reduce((s, i) => s + i.w, 0);
    let r = Math.random() * total;
    for (const it of items) { r -= it.w; if (r <= 0) return it; }
    return items[0];
  }
  function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }
  function hex(n) { let o = ''; for (let i = 0; i < n; i++) o += '0123456789abcdef'[Math.floor(Math.random() * 16)]; return o; }

  function generateAttempts(count = 320) {
    const out = [];
    const now = Date.now();
    for (let i = 0; i < count; i++) {
      const dispSpec = pickW(DISPOSITIONS);
      const pathSpec = pickW(PATHS);
      const model = pick(MODELS);
      const user = pick(USERS);

      const denied = dispSpec.d.startsWith('denied') || dispSpec.d.startsWith('error');
      const latency = denied
        ? Math.floor(Math.random() * 120) + 6
        : Math.floor(Math.random() * 900) + 120;

      const tokens = denied ? null : Math.floor(Math.random() * 3500) + 220;
      const promptTokens = tokens ? Math.floor(tokens * (0.55 + Math.random() * 0.3)) : null;
      const completionTokens = tokens ? tokens - promptTokens : null;

      // Analyzers: show a 7-of-7 pass / denials cause specific analyzer fail
      const analyzerTotal = 7;
      let analyzerPassed = analyzerTotal;
      const failedAnalyzers = [];
      if (denied) {
        const nFail = dispSpec.d === 'denied_policy' ? 1 : Math.random() < 0.3 ? 1 : 0;
        analyzerPassed = analyzerTotal - nFail;
        for (let k = 0; k < nFail; k++) {
          failedAnalyzers.push(pick(ANALYZERS));
        }
      }

      const ago = Math.floor(Math.random() * 60 * 60 * 24);  // past 24h
      const timestamp = new Date(now - ago * 1000).toISOString();
      const executionId = denied && dispSpec.code >= 500 ? null : hex(16);

      out.push({
        request_id: hex(24),
        execution_id: executionId,
        disposition: dispSpec.d,
        status_code: dispSpec.code,
        reason: dispSpec.reason || null,
        path: pathSpec.path,
        method: pathSpec.method,
        model_id: model.id,
        provider: model.provider,
        user,
        latency_ms: latency,
        prompt_tokens: promptTokens,
        completion_tokens: completionTokens,
        total_tokens: tokens,
        analyzers_passed: analyzerPassed,
        analyzers_total: analyzerTotal,
        failed_analyzers: failedAnalyzers,
        timestamp,
      });
    }
    return out.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
  }

  window.AttemptsData = {
    items: generateAttempts(320),
  };
})();
