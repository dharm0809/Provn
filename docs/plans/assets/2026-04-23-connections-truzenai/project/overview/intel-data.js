/* Mock data for Intelligence view — production models, candidates,
   promotion history, verdict inspector.
   Models: intent / schema_mapper / safety (the walacor intelligence layer) */

(function() {
  const rand = (min, max) => min + Math.random() * (max - min);
  const pick = arr => arr[Math.floor(Math.random() * arr.length)];
  const uuid = () => 'xxxxxxxxxxxx4xxxyxxxxxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });

  const APPROVERS = ['alex.chen@acme.io', 'svc-promoter', 'm.patel@acme.io', 'd.okafor@acme.io'];

  function genVersion(gen) {
    const d = new Date(Date.now() - gen * 86400e3 * 9 - rand(0, 4) * 3600e3);
    const stamp = d.toISOString().replace(/[-:]/g, '').replace(/\..+/, '').replace('T', '_');
    return `v${gen.toString().padStart(3, '0')}_${stamp}`;
  }

  function genDatasetHash() {
    const chars = '0123456789abcdef';
    let s = 'sha256:';
    for (let i = 0; i < 40; i++) s += chars[Math.floor(Math.random() * 16)];
    return s;
  }

  const INTEL_MODELS = [
    {
      model_name: 'intent',
      description: 'Classifies user intent across 14 action categories',
      generation: 12,
      size_bytes: 84_312_448,
      parameters: '22M',
      architecture: 'DistilBERT-multi',
      accuracy: 0.947,
      predictions_24h: 18_402,
      predictions_7d: 142_680,
      trailing_accuracy: 0.932,
      drift: 'stable',
    },
    {
      model_name: 'schema_mapper',
      description: 'Maps freeform queries → structured schema fields',
      generation: 9,
      size_bytes: 112_845_312,
      parameters: '44M',
      architecture: 'T5-small-distilled',
      accuracy: 0.883,
      predictions_24h: 12_194,
      predictions_7d: 91_450,
      trailing_accuracy: 0.871,
      drift: 'minor',
    },
    {
      model_name: 'safety',
      description: 'Policy violation + prompt-injection detection',
      generation: 18,
      size_bytes: 56_230_912,
      parameters: '14M',
      architecture: 'MiniLM-v6',
      accuracy: 0.961,
      predictions_24h: 22_811,
      predictions_7d: 168_204,
      trailing_accuracy: 0.958,
      drift: 'stable',
    },
  ];

  // Attach per-model production metadata (active version + last_promotion)
  INTEL_MODELS.forEach(m => {
    m.active_version = genVersion(m.generation);
    const daysAgo = rand(3, 18);
    m.last_promotion = {
      candidate_version: m.active_version,
      approver: pick(APPROVERS),
      timestamp: new Date(Date.now() - daysAgo * 86400e3).toISOString(),
      dataset_hash: genDatasetHash(),
    };
    // Time-series accuracy for spark (last 14 days)
    m.accuracy_series = [];
    let acc = m.accuracy - 0.015;
    for (let i = 0; i < 14; i++) {
      acc += (Math.random() - 0.5) * 0.012;
      acc = Math.min(0.995, Math.max(0.8, acc));
      m.accuracy_series.push(acc);
    }
    m.accuracy_series[m.accuracy_series.length - 1] = m.accuracy;
  });

  // ── Candidates ─────────────────────────────────────────────
  const CANDIDATES = [
    {
      model_name: 'intent',
      version: genVersion(13),
      created_at: new Date(Date.now() - 6 * 3600e3).toISOString(),
      dataset_hash: genDatasetHash(),
      active_shadow: true,
      shadow_validation: {
        completed: true,
        passed: true,
        metrics: {
          sample_count: 8_420,
          labeled_count: 2_104,
          candidate_accuracy: 0.962,
          production_accuracy: 0.947,
          candidate_error_rate: 0.038,
          disagreement_rate: 0.046,
          mcnemar_p_value: 0.0072,
        },
      },
    },
    {
      model_name: 'schema_mapper',
      version: genVersion(10),
      created_at: new Date(Date.now() - 28 * 3600e3).toISOString(),
      dataset_hash: genDatasetHash(),
      active_shadow: true,
      shadow_validation: {
        completed: true,
        passed: false,
        metrics: {
          sample_count: 6_180,
          labeled_count: 1_430,
          candidate_accuracy: 0.879,
          production_accuracy: 0.883,
          candidate_error_rate: 0.121,
          disagreement_rate: 0.082,
          mcnemar_p_value: 0.341,
        },
      },
    },
    {
      model_name: 'safety',
      version: genVersion(19),
      created_at: new Date(Date.now() - 2 * 3600e3).toISOString(),
      dataset_hash: genDatasetHash(),
      active_shadow: true,
      shadow_validation: {
        completed: false,
        metrics: {
          sample_count: 1_210,
          labeled_count: 288,
        },
      },
    },
    {
      model_name: 'intent',
      version: genVersion(12) + '_exp',
      created_at: new Date(Date.now() - 52 * 3600e3).toISOString(),
      dataset_hash: genDatasetHash(),
      active_shadow: false,
      shadow_validation: {
        completed: true,
        passed: false,
        metrics: {
          sample_count: 12_804,
          labeled_count: 3_048,
          candidate_accuracy: 0.941,
          production_accuracy: 0.947,
          candidate_error_rate: 0.059,
          disagreement_rate: 0.038,
          mcnemar_p_value: 0.512,
        },
      },
    },
  ];

  // ── Promotion history events per model ─────────────────────
  function genHistory(modelName) {
    const events = [];
    const now = Date.now();
    let gen = {intent: 12, schema_mapper: 9, safety: 18}[modelName] || 10;

    // Interleave candidate_created → shadow_validation_complete → (promoted|rejected)
    for (let i = 0; i < 8; i++) {
      const ageDays = i * rand(6, 14);
      const baseT = now - ageDays * 86400e3;
      const v = genVersion(gen - i);
      const dh = genDatasetHash();

      // candidate_created
      events.push({
        event_type: 'candidate_created',
        timestamp: new Date(baseT - 3600e3 * 4).toISOString(),
        payload: { candidate_version: v, dataset_hash: dh, model: modelName },
        write_status: 'written',
        attempts: 1,
        walacor_record_id: uuid(),
      });

      // training_dataset_fingerprint (sometimes)
      if (Math.random() > 0.4) {
        events.push({
          event_type: 'training_dataset_fingerprint',
          timestamp: new Date(baseT - 3600e3 * 3.5).toISOString(),
          payload: { dataset_hash: dh, model: modelName, sample_count: Math.round(rand(20000, 80000)) },
          write_status: 'written',
          attempts: 1,
          walacor_record_id: uuid(),
        });
      }

      // shadow_validation
      const cAcc = rand(0.87, 0.97);
      const pAcc = rand(0.87, 0.96);
      const passed = cAcc > pAcc + 0.005 && Math.random() > 0.25;
      events.push({
        event_type: 'shadow_validation_complete',
        timestamp: new Date(baseT - 3600e3 * 2).toISOString(),
        payload: {
          candidate_version: v,
          passed,
          shadow_metrics: {
            sample_count: Math.round(rand(4000, 14000)),
            candidate_accuracy: cAcc,
            production_accuracy: pAcc,
            disagreement_rate: rand(0.02, 0.09),
            mcnemar_p_value: passed ? rand(0.001, 0.04) : rand(0.1, 0.6),
          },
        },
        write_status: 'written',
        attempts: 1,
        walacor_record_id: uuid(),
      });

      // promotion/rejection
      if (passed && Math.random() > 0.15) {
        events.push({
          event_type: 'model_promoted',
          timestamp: new Date(baseT).toISOString(),
          payload: {
            candidate_version: i === 0 && modelName === 'safety' ? 'rollback:' + genVersion(gen - i - 1) : v,
            approver: pick(APPROVERS),
            dataset_hash: dh,
            shadow_metrics: { sample_count: Math.round(rand(4000, 14000)), candidate_accuracy: cAcc, production_accuracy: pAcc },
          },
          write_status: 'written',
          attempts: 1,
          walacor_record_id: uuid(),
        });
      } else {
        events.push({
          event_type: 'model_rejected',
          timestamp: new Date(baseT).toISOString(),
          payload: {
            candidate_version: v,
            approver: pick(APPROVERS),
            reason: pick([
              'accuracy regression on web_search class',
              'disagreement rate exceeds threshold',
              'manual_rejection',
              'failed stability check',
              'drift signal on safety slice',
            ]),
          },
          write_status: Math.random() > 0.92 ? 'failed' : 'written',
          attempts: Math.random() > 0.92 ? 3 : 1,
          error_reason: Math.random() > 0.92 ? 'walacor chain timeout' : undefined,
          walacor_record_id: uuid(),
        });
      }
    }

    return events.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
  }

  // ── Verdict inspector rows ─────────────────────────────────
  const DIVERGENCE_SIGNALS = [
    { signal: 'user_rejected_output', source: 'feedback' },
    { signal: 'analyzer_override', source: 'content_safety' },
    { signal: 'human_correction', source: 'playground_label' },
    { signal: 'downstream_error', source: 'execution_log' },
    { signal: 'retry_with_different_intent', source: 'session_harvester' },
    { signal: 'schema_validation_failed', source: 'schema_check' },
  ];

  function genVerdicts(modelName, divergenceOnly, limit) {
    const rows = [];
    const now = Date.now();
    const predictions = modelName === 'intent'
      ? ['file_search', 'web_search', 'code_execution', 'schema_query', 'summarize', 'translate', 'none']
      : modelName === 'schema_mapper'
      ? ['users.email', 'users.id', 'orders.total', 'orders.date', 'products.sku', 'none']
      : ['safe', 'prompt_injection', 'policy_violation', 'borderline', 'spam'];
    for (let i = 0; i < limit; i++) {
      const isDiv = divergenceOnly ? true : Math.random() > 0.7;
      const sig = isDiv ? pick(DIVERGENCE_SIGNALS) : null;
      rows.push({
        id: uuid(),
        timestamp: new Date(now - rand(60, 86400 * 3) * 1000).toISOString(),
        input_hash: uuid().replace(/-/g, '').slice(0, 32),
        prediction: pick(predictions),
        confidence: rand(0.52, 0.99),
        divergence_signal: sig?.signal || null,
        divergence_source: sig?.source || null,
        request_id: uuid().slice(0, 16),
      });
    }

    // Top divergence types for bar chart
    const topMap = {};
    for (const r of rows) {
      if (r.divergence_signal) {
        topMap[r.divergence_signal] = (topMap[r.divergence_signal] || 0) + 1;
      }
    }
    const top = Object.entries(topMap)
      .map(([signal, count]) => ({ signal, count }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 6);

    return { rows: rows.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp)), top_divergence_types: top };
  }

  window.IntelData = {
    models: INTEL_MODELS,
    candidates: CANDIDATES,
    genHistory,
    genVerdicts,
    APPROVERS,
    genDatasetHash,
    genVersion,
  };

  window.truncHash = function(h, len) {
    if (!h) return '-';
    if (h.length <= len) return h;
    return h.slice(0, len) + '…';
  };

  window.formatBytes = function(b) {
    if (!b) return '-';
    if (b < 1024) return b + 'B';
    if (b < 1024 * 1024) return (b / 1024).toFixed(1) + 'KB';
    if (b < 1024 * 1024 * 1024) return (b / 1024 / 1024).toFixed(1) + 'MB';
    return (b / 1024 / 1024 / 1024).toFixed(2) + 'GB';
  };

  window.fmtPct = function(x, digits = 1) {
    if (x == null || Number.isNaN(x)) return '—';
    return (Number(x) * 100).toFixed(digits) + '%';
  };

  window.fmtDelta = function(cand, prod) {
    if (cand == null || prod == null) return '—';
    const d = Number(cand) - Number(prod);
    if (Number.isNaN(d)) return '—';
    const sign = d >= 0 ? '+' : '';
    return sign + (d * 100).toFixed(2) + 'pp';
  };
})();
