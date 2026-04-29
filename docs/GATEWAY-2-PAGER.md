# Walacor Gateway


Every company using AI today faces the same problem: there's no way to prove what happened. No record of what was asked, what came back, or whether anyone changed the logs after the fact. Walacor Gateway fixes that.

It's a security layer that sits between your people and whatever AI models they use. It records every interaction, enforces your policies, and chains everything together cryptographically so nobody can tamper with the trail — not even us.

One port. All providers. No code changes on your side.

```
                          ARCHITECTURE
 ────────────────────────────────────────────────────────────

 CLIENTS                   GATEWAY                    PROVIDERS
 ┌────────────┐                                    ┌──────────┐
 │ Open WebUI │─┐                               ┌──│  OpenAI  │
 │ (Chat UI)  │ │   ┌────────────────────────┐  │  │ GPT-4o   │
 └────────────┘ │   │                        │  │  └──────────┘
                ├──>│   WALACOR GATEWAY      │──┤
 ┌────────────┐ │   │   (Single Port)        │  │  ┌──────────┐
 │ Your App   │─┤   │                        │  ├──│ Anthropic│
 │ (API call) │ │   │  Authentication        │  │  │ Claude   │
 └────────────┘ │   │  Policy Engine         │  │  └──────────┘
                │   │  Budget Tracking       │  │
 ┌────────────┐ │   │  Content Safety        │  │  ┌──────────┐
 │ Mobile App │─┘   │  Session Chains        │  ├──│  Ollama  │
 └────────────┘     │  Tool Execution        │  │  │  Llama   │
                    └───────────┬────────────┘  │  └──────────┘
                                │               │
              ┌─────────────────┼───────────┐   │  ┌──────────┐
              ▼                 ▼           ▼   └──│  Any LLM │
     ┌─────────────┐  ┌─────────────┐ ┌─────────┐ └──────────┘
     │ Local Store │  │   Cloud     │ │Dashboard│
     │ (encrypted) │  │  Backend    │ │(built-in│
     │             │  │             │ │  UI)    │
     └─────────────┘  └─────────────┘ └─────────┘
         Dual-write: both stores get every record.
         Dashboard reads locally — no pipeline impact.
 ────────────────────────────────────────────────────────────
```

---

## Why This Matters

We keep hearing the same concerns from enterprises:

- "Our people are using AI, but we have zero visibility into what's being asked or answered."
- "If a regulator asks us to prove our AI governance, we can't."
- "Someone could edit the logs and we'd never know."
- "We blew through our AI budget last quarter because nobody was tracking token usage."
- "A model returned a customer's SSN in a response. We found out weeks later."

These aren't hypothetical. They're happening now, at scale, across industries.

| Situation | Today | With Gateway |
|-----------|-------|-------------|
| Employee asks AI a sensitive question | No record exists | Full prompt recorded with user ID and timestamp |
| AI returns PII in a response | Nobody catches it | Blocked before it reaches the user |
| Someone edits the audit logs | No way to detect it | Cryptographic chain breaks — tampering is visible |
| Unapproved model gets used | Nobody knows | Request blocked, attempt logged |
| AI spend exceeds budget | Found out next billing cycle | Enforced in real-time, request denied at limit |
| Regulator asks for AI interaction records | Scramble to produce something | Export from dashboard, chain-verified |

---

## What Happens When Someone Sends a Message

Here's the actual flow. Nothing is skipped, nothing is optional.

```
 User asks: "What is our revenue forecast?"
                      │
                      ▼
          ┌─────────────────────────┐
     1.   │  MODEL CHECK            │─── Not approved? Block it.
          │  Is this model allowed? │    Log the attempt.
          └────────────┬────────────┘
                      YES
                      ▼
          ┌─────────────────────────┐
     2.   │  POLICY CHECK           │─── Violates rules? Block it.
          │  Does this pass         │    Log the violation.
          │  our rules?             │
          └────────────┬────────────┘
                      YES
                      ▼
          ┌─────────────────────────┐
     3.   │  BUDGET CHECK           │─── Over limit? Block it.
          │  Tokens remaining?      │    Log the denial.
          └────────────┬────────────┘
                      YES
                      ▼
          ┌─────────────────────────┐
     4.   │  FORWARD TO AI          │
          │  Send request to the    │
          │  model provider         │
          └────────────┬────────────┘
                       │
                       ▼  (AI responds)
          ┌─────────────────────────┐
     5.   │  SAFETY SCAN            │─── Unsafe? Block the
          │  Check for PII,         │    response. Log it.
          │  toxicity, harmful      │
          │  content                │
          └────────────┬────────────┘
                     SAFE
                      ▼
          ┌─────────────────────────┐
     6.   │  CHAIN IT               │  Link this turn to the
          │  ID-pointer chain       │  previous one. Tampering
          │  + Walacor DH on ingest │  breaks the chain.
          └────────────┬────────────┘
                      ▼
          ┌─────────────────────────┐
     7.   │  SAVE THE RECORD        │  Written to local store
          │  Dual-write to both     │  AND cloud backend.
          │  storage backends       │  Neither can lose it alone.
          └────────────┬────────────┘
                      ▼
          ┌─────────────────────────┐
     8.   │  DELIVER RESPONSE       │  User gets their answer.
          └─────────────────────────┘

 Bottom line: every request creates an audit record.
 Allowed, denied, or errored — doesn't matter. Nothing slips through.
```

---

## The Five Guarantees

We built the gateway around five properties that hold up under scrutiny.

**1. Model Attestation** — A model has to be registered before it can run. Unregistered model? Blocked. Revoked model? Blocked. Every blocked attempt is logged so you can see who tried.

**2. Full Audit Trail** — The complete prompt, the complete response, who sent it, which model answered, how many tokens it used, how long it took, what policies applied, and what the safety scanners found. All of it. Not a summary, not a sample — the whole thing. Saved in two places at once.

**3. Policy Enforcement Before Inference** — Rules get checked before the AI model ever sees the request. Not after. This means you can block things proactively instead of cleaning up afterward. Policies are versioned, so you always know which rules were active at the time.

**4. Content Safety After Inference** — Three checks run on every response before it goes back to the user:
- PII scanner catches credit cards, SSNs, API keys (blocks them), plus emails and phone numbers (flags them)
- Toxicity filter catches harmful language
- Llama Guard covers 14 safety categories including violence, self-harm, and child safety

**5. Tamper-Evident Conversation Chains** — Each turn in a conversation is linked to the previous one through an ID pointer (`previous_record_id`), and the Walacor backend issues a tamper-evident `DH` (data hash) on ingest. Remove, insert, or reorder a turn and the linkage breaks at that point. Verification walks the chain server-side; the Walacor `DH` provides the independent cryptographic checkpoint.

```
 Turn 1 ──prev_id──> Turn 2 ──prev_id──> Turn 3 ──prev_id──> Turn 4
   │                    │                    │                    │
   └── Change anything here, and the linkage breaks at that point.
```

---

## The Dashboard

There's a built-in web dashboard. No extra tools needed.

| View | What you see |
|------|-------------|
| **Overview** | Live request rate, how many allowed vs blocked, system health at a glance |
| **Sessions** | Every conversation, searchable by user, model, and date |
| **Execution Detail** | The actual prompt and response, safety results, tokens used, time taken |
| **Chain Verification** | Click "verify" — the system recomputes every hash and tells you if the chain is intact |
| **Usage Charts** | Token consumption and latency over time (last hour to last 30 days) |
| **Control Panel** | Add or remove approved models, create policies, set budgets, configure safety rules |

---

## Supported Providers

| Provider | Models | Streaming |
|----------|--------|-----------|
| **OpenAI** | GPT-4, GPT-4o, o1, o3, o4 | Yes |
| **Anthropic** | Claude 3.5, Claude 4 | Yes |
| **Ollama** | Llama 3, Qwen, Gemma, Mistral (self-hosted) | Yes |
| **HuggingFace** | Any Inference Endpoint | Yes |
| **Any REST API** | Custom models via generic adapter | Configurable |

Everything runs through one port. The gateway figures out which provider to hit based on the model name in the request.

---

## Who Needs This

### By Industry

| Industry | The problem they have | How we solve it |
|----------|----------------------|----------------|
| **Financial Services** | Regulators want proof that AI-driven advice was governed | Tamper-proof audit trail that holds up to examination |
| **Healthcare** | Patient data can't leak through AI interactions | PII scanner blocks sensitive data before it reaches anyone |
| **Government** | Only attested models on approved networks | Model attestation — nothing runs unless it's registered |
| **Legal** | Attorney-client privilege extends to AI conversations | Complete, uneditable recording of every exchange |
| **Insurance** | State regulators need to see how AI reached its conclusions | Session chains show the exact conversation, unaltered |
| **Pharma** | FDA wants traceability for AI-assisted R&D | Dual-write records with cryptographic integrity |
| **Education** | Need to monitor and cap student AI usage | Policy engine plus budget enforcement |
| **Any Enterprise** | "We're using AI and we have no idea what's going on" | Full visibility from a dashboard — no setup required |

### By Buyer

| Person | What resonates | The outcome they want |
|--------|---------------|----------------------|
| **CISO** | "Think of it as a WAF for AI traffic" | Audit trail that survives a security review |
| **CTO** | "One config change, no SDK, no code rewrite" | Governance without slowing down engineering |
| **CFO** | "Hard budget caps per team, per model, per month" | No more surprise AI invoices |
| **Compliance Lead** | "Direct mapping to EU AI Act and NIST AI RMF" | Audit-ready from day one |
| **CEO** | "Proof that our AI policy is actually enforced" | Board-level confidence, not just a paper policy |
| **Legal / DPO** | "PII gets caught before it leaves the pipeline" | Data protection that's structural, not aspirational |
| **Head of AI** | "Central control panel for all models and policies" | Governance that doesn't bottleneck the AI team |

### Talking Points for Sales Conversations

When you're in front of a **CISO**, say: *"It's the same idea as a web application firewall, but for AI. Every request gets inspected, every response gets scanned, and every conversation gets chained so nobody can alter the record."*

When you're in front of a **CTO**, say: *"You change one URL — point at the gateway instead of OpenAI directly. That's the entire integration. Your team keeps using the same tools, same SDKs, same workflows."*

When you're in front of a **CFO**, say: *"You tell us the monthly budget. We enforce it. When it's gone, requests get denied. No overages, no surprises."*

When you're in front of **Compliance**, say: *"Article 12 of the EU AI Act requires systematic record-keeping for high-risk AI. Our completeness guarantee means every single request produces a record. Not most. Every one."*

When you're in front of the **Board**, say: *"Most companies have an AI usage policy. We have cryptographic proof that the policy is being followed."*

### How We're Different

| What competitors typically offer | What we do instead |
|----------------------------------|-------------------|
| Logging AI requests to a database | Cryptographic chains — if someone edits a log, the chain breaks |
| Monitoring after the fact | Blocking before the model ever sees the request |
| SDK integration required | Transparent proxy — zero code changes |
| Single provider support | All major providers through one port |
| Dashboards and analytics | Dashboards plus mathematical proof that the trail is complete |
| SaaS-only, per-seat pricing | Open-source core — runs on your infrastructure |

---

## Deployment

Three ways to run it. Pick whatever fits.

| Option | Use case | How long |
|--------|---------|---------|
| **Docker Compose** | Production single-server | About 5 minutes |
| **Native install** | Dev and testing | About 2 minutes |
| **Kubernetes** | Multi-replica production | Standard Helm deploy |

To get started:
```
docker compose up -d
```
That brings up the gateway, an AI model server (Ollama), and a chat interface (Open WebUI). Open a browser and go.

---

## Rolling It Out

You don't flip a switch and enforce everything. The gateway is designed for gradual rollout.

| Phase | What happens | Impact on users |
|-------|-------------|----------------|
| **Observe** | Gateway records everything but blocks nothing. You watch. | None. Users don't even notice it's there. |
| **Shadow** | Gateway evaluates policies and logs what it would block. Still allows everything. | None. You're testing your rules. |
| **Selective** | Turn on model approval, budgets, and content safety. Violations get blocked. | Minimal. Only unauthorized activity is affected. |
| **Full** | All policies active. Full audit. Full safety. Full chain integrity. | Managed. Everything is visible in the dashboard. |

Most teams go from Phase 1 to Phase 4 in two to three weeks.

---

## Regulatory Mapping

For compliance teams who need to map capabilities to frameworks:

| Framework | Requirement | What we do |
|-----------|------------|-----------|
| **EU AI Act, Art. 9** | Risk management for high-risk AI | Content safety pipeline with three independent analyzers |
| **EU AI Act, Art. 12** | Automatic logging / record-keeping | Every request produces an audit record — guaranteed completeness |
| **EU AI Act, Art. 14** | Human oversight capability | Real-time dashboard plus control panel for policy management |
| **EU AI Act, Art. 15** | Accuracy, robustness, cybersecurity | Session chains (ID-pointer + Walacor DH), encrypted storage, auth enforcement |
| **NIST AI RMF** | Govern, Map, Measure, Manage | Policy engine, model routing, safety analysis, budget enforcement |
| **SOC 2** | Security, availability, integrity | Encrypted audit trail, fail-closed behavior, dual-write durability |

---

## At a Glance

| | |
|---|---|
| **Providers** | OpenAI, Anthropic, Ollama, HuggingFace, any REST API |
| **Safety checks** | PII detection, toxicity filtering, Llama Guard (14 categories) |
| **Audit completeness** | 100% of requests — no exceptions |
| **Streaming overhead** | Zero added latency |
| **Hash algorithm** | SHA3-512 (NIST standard), issued by Walacor backend on ingest |
| **Verification** | Client-side and server-side — zero trust in the server required |
| **Deployment** | Docker, native, or Kubernetes |
| **License** | Open-source core (Apache 2.0) |

---

*Walacor Gateway — AI governance you can prove.*
