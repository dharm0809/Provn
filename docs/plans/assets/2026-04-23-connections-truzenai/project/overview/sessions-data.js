/* Mock session + record data — enriched for the Sessions view.
   Kept as pure data so it renders deterministically per session. */

(function () {
  // Seedable PRNG so "generated" sessions look varied but stable within a session_id
  function seedRng(seed) {
    let s = 0;
    for (let i = 0; i < seed.length; i++) s = (s * 31 + seed.charCodeAt(i)) >>> 0;
    return function () {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 0xffffffff;
    };
  }

  const MODELS = [
    'claude-sonnet-4.5', 'claude-opus-4', 'gpt-4.1', 'gpt-4o',
    'gemini-2.5-pro', 'llama-3.3-70b', 'mistral-large-2',
    'claude-haiku-4.5', 'deepseek-v3',
  ];
  const USERS = [
    'alicia.chen', 'marcus.webb', 'priya.ram', 'j.kowalski',
    'dev-agent-07', 'sarah.oconnor', 'tom.nguyen', 'r.delacroix',
  ];
  const QUESTIONS = [
    'Summarize the Q3 risk register and flag items missing mitigation owners.',
    'Draft an email to the compliance working group about the model attestation change.',
    'What tool interactions are currently denied under the finance-prod policy?',
    'Generate unit tests for the retry helper in gateway/executor.py.',
    'Compare three model options for the support chatbot — cost vs latency.',
    'Explain why record 47291 was redacted and which clause applied.',
    'Translate the attached PDF section about data residency into plain English.',
    'Walk me through how the chain verification works end-to-end.',
    'Check the open PRs for changes that touch policy evaluation.',
    'Build a slide on attestation drift incidents for the board deck.',
  ];
  const POLICIES = [
    { r: 'allow', w: 74 },
    { r: 'allow_with_redaction', w: 12 },
    { r: 'block', w: 6 },
    { r: 'warn', w: 8 },
  ];
  const TOOLS = [
    { name: 'web_search', source: 'gateway' },
    { name: 'code_interpreter', source: 'gateway' },
    { name: 'filesystem.read', source: 'mcp' },
    { name: 'github.list_prs', source: 'mcp' },
    { name: 'slack.post', source: 'mcp' },
    { name: 'jira.search', source: 'mcp' },
    { name: 'db.query_readonly', source: 'mcp' },
  ];

  function pickW(rng, items) {
    const total = items.reduce((s, i) => s + i.w, 0);
    let r = rng() * total;
    for (const it of items) { r -= it.w; if (r <= 0) return it.r; }
    return items[0].r;
  }
  function pick(rng, arr) { return arr[Math.floor(rng() * arr.length)]; }

  function hex(rng, n) {
    let out = '';
    for (let i = 0; i < n; i++) out += '0123456789abcdef'[Math.floor(rng() * 16)];
    return out;
  }

  function makeSessionList() {
    const sessions = [];
    for (let i = 0; i < 24; i++) {
      const sid = hex(Math.random, 32);
      const rng = seedRng(sid);
      const turns = Math.floor(rng() * 22) + 2;
      const records = Math.floor(turns * (1 + rng())) + 1;
      const ago = i < 4
        ? Math.floor(rng() * 55 * 60)              // first 4 in last hour
        : Math.floor(rng() * 60 * 60 * 28);        // rest spread over ~28h
      const model = pick(rng, MODELS);
      const user = pick(rng, USERS);
      const question = pick(rng, QUESTIONS);
      const hasRag = rng() < 0.35;
      const hasFiles = rng() < 0.28;
      const hasImages = hasFiles && rng() < 0.35;
      const toolList = [];
      const nTools = Math.floor(rng() * 3);
      for (let t = 0; t < nTools; t++) toolList.push(pick(rng, TOOLS));
      const chainStatus = rng() < 0.04 ? 'warn' : 'verified';
      const durationSec = Math.floor(turns * 15 + rng() * 180);
      const blocked = rng() < 0.09;

      sessions.push({
        session_id: sid,
        user,
        model,
        record_count: records,
        user_message_count: turns,
        user_question: question,
        has_rag_context: hasRag,
        has_files: hasFiles,
        has_images: hasImages,
        tools: toolList,
        chain_status: chainStatus,
        duration_sec: durationSec,
        last_activity: new Date(Date.now() - ago * 1000).toISOString(),
        started_at: new Date(Date.now() - ago * 1000 - durationSec * 1000).toISOString(),
        policy_summary: blocked ? 'blocked' : 'clean',
        blocked_count: blocked ? 1 + Math.floor(rng() * 2) : 0,
      });
    }
    return sessions.sort((a, b) => new Date(b.last_activity) - new Date(a.last_activity));
  }

  /* ── Build a detailed, cryptographically-linked chain of records for a session ── */
  function makeSessionRecords(sid) {
    const rng = seedRng(sid);
    const turns = Math.max(3, Math.floor(rng() * 8) + 3);
    const model = pick(rng, MODELS);
    const user = pick(rng, USERS);
    const topic = pick(rng, QUESTIONS);
    const records = [];

    const GENESIS = '0'.repeat(64);
    let prev = GENESIS;
    const startMs = Date.now() - Math.floor(rng() * 60 * 60 * 24 * 1000);

    const prompts = [
      { p: topic, r: 'Here is an overview with the key points broken out section by section...' },
      { p: 'Can you expand on the second point, especially around ownership gaps?', r: 'Of the nine items flagged, three have no assigned owner as of this cycle...' },
      { p: 'What should the remediation plan look like for the ones missing owners?', r: 'I\'d recommend a two-week assignment window with weekly status in the risk sync...' },
      { p: 'Pull the current owners and draft a short note we can send to each lead.', r: 'Drafted — see below. Tone is matter-of-fact, no blame, clear ask and deadline...' },
      { p: 'Also check the last three incident reports for recurring themes.', r: 'Two themes appear in all three reports: late detection and unclear escalation paths...' },
      { p: 'Wrap this up with three one-line recommendations for the board readout.', r: '1) Close the ownership gap this cycle. 2) Shorten detection SLOs. 3) Publish an escalation matrix...' },
    ];

    for (let i = 0; i < turns; i++) {
      const turn = prompts[i % prompts.length];
      const policy = pickW(rng, POLICIES);
      const promptTok = Math.floor(120 + rng() * 800);
      const completionTok = Math.floor(80 + rng() * 520);
      const recordHash = hex(rng, 64);
      const signature = hex(rng, 128);
      const eid = hex(rng, 24);
      const blockId = hex(rng, 40);
      const dataHash = hex(rng, 64);
      const ts = new Date(startMs + i * (20 + Math.floor(rng() * 40)) * 1000).toISOString();

      const tools = [];
      if (rng() < 0.55) {
        const nT = Math.max(1, Math.floor(rng() * 2) + 1);
        for (let t = 0; t < nT; t++) {
          const tool = pick(rng, TOOLS);
          tools.push({
            tool_name: tool.name,
            tool_source: tool.source,
            is_error: rng() < 0.05,
            sources: tool.name === 'web_search' ? new Array(Math.floor(rng() * 5)).fill(0) : [],
          });
        }
      }

      records.push({
        execution_id: hex(rng, 16),
        sequence_number: i + 1,
        session_id: sid,
        timestamp: ts,
        prompt_text: turn.p,
        response_content: turn.r,
        model_id: model,
        user,
        policy_result: policy,
        policy_version: 'v2.3.1',
        prompt_tokens: promptTok,
        completion_tokens: completionTok,
        tokens: promptTok + completionTok,
        previous_record_hash: prev,
        record_hash: recordHash,
        record_signature: signature,
        metadata: {
          tool_interactions: tools,
          request_type: 'chat',
        },
        file_metadata: (i === 0 && rng() < 0.4)
          ? [{
              filename: 'q3-risk-register.pdf',
              mimetype: 'application/pdf',
              size_bytes: Math.floor(80000 + rng() * 400000),
              hash_sha3_512: hex(rng, 128),
              source: 'upload',
            }]
          : null,
        _envelope: {
          block_id: blockId,
          data_hash: dataHash,
        },
        _walacor_eid: eid,
      });

      prev = recordHash;
    }

    return records;
  }

  window.SessionsData = {
    list: makeSessionList(),
    getRecords: makeSessionRecords,
  };
})();
