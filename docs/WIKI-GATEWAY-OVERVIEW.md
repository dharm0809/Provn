# TruzenAI

**AI Governance for the Enterprise**

One secure entry point for every AI interaction. Complete visibility, enforced policies, tamper-evident records.

---

## Executive Summary

TruzenAI is an enterprise governance layer for artificial intelligence. It sits between your people and the AI models they use, providing a single secure entry point for every interaction. Every question asked, every answer returned, and every decision made by an AI passes through TruzenAI — where it is authorized, inspected, recorded, and permanently sealed against tampering.

The result: organizations can adopt AI with the same level of oversight, accountability, and auditability they already apply to their financial and legal systems.

---

## The Challenge

Enterprises adopting AI today face a set of problems that existing tools do not fully solve.

| The Question Leadership Is Asking | The Reality Today |
|---|---|
| "What are our people actually asking AI models?" | No visibility. Interactions flow directly between employees and third-party AI services. |
| "Can we prove to a regulator that our AI use is governed?" | Not easily. There is usually no unified, trustworthy record to produce. |
| "How would we know if our logs had been altered?" | There is no reliable way to detect tampering after the fact. |
| "Are we spending within budget on AI services?" | Costs are typically discovered in the next invoice cycle. |
| "Did sensitive information leave our organization through an AI prompt or response?" | Usually discovered weeks later, if at all. |
| "Are employees using only the AI models we have approved?" | Without central enforcement, anything with an API can be used. |

These concerns are not theoretical. They are the daily reality of CISOs, CFOs, compliance officers, and boards at every organization adopting AI at scale.

---

## The Solution

TruzenAI places a single intelligent checkpoint between users and AI providers.

Every interaction flowing through TruzenAI is:

1. **Authorized** — the person and the AI model they are trying to use must both be approved
2. **Measured** — token usage and cost are tracked in real time, with budgets enforced automatically
3. **Inspected** — prompts and responses are checked against safety and content rules
4. **Recorded** — the complete interaction is saved to two independent storage systems
5. **Sealed** — records are cryptographically linked so any later alteration becomes immediately detectable

There is no change required on the user side. Employees keep using their existing tools — chat interfaces, coding assistants, internal applications — and TruzenAI handles governance invisibly in the background.

---

## Architecture at a Glance

```
                         TRUZENAI ARCHITECTURE

    ┌──────────────────┐                                  ┌──────────────────┐
    │                  │                                  │                  │
    │    YOUR USERS    │                                  │   AI PROVIDERS   │
    │                  │                                  │                  │
    │  Chat tools      │                                  │  OpenAI          │
    │  Applications    │     ┌────────────────────┐       │  Anthropic       │
    │  Mobile apps     │─────│      TRUZENAI      │───────│  Self-hosted     │
    │  Internal        │     │                    │       │  HuggingFace     │
    │    services      │     │  Identity          │       │  Any REST API    │
    │                  │     │  Authorization     │       │                  │
    └──────────────────┘     │  Policy Engine     │       └──────────────────┘
                             │  Budget Control    │
                             │  Content Safety    │
                             │  Audit Recorder    │
                             │  Chain of Custody  │
                             └──────────┬─────────┘
                                        │
                         ┌──────────────┼──────────────┐
                         ▼              ▼              ▼
                  ┌──────────┐  ┌──────────┐  ┌──────────┐
                  │  LOCAL   │  │  CLOUD   │  │ DASHBOARD│
                  │ STORAGE  │  │ STORAGE  │  │          │
                  │          │  │          │  │  Live    │
                  │ Encrypted│  │ Walacor  │  │ Insight  │
                  │          │  │ Backend  │  │          │
                  └──────────┘  └──────────┘  └──────────┘

          Every interaction is written to both storage systems at once.
          The Dashboard reads independently and never interrupts traffic.
```

---

## The Journey of a Request

Here is what happens, in plain language, when an employee sends a message to an AI model through TruzenAI.

```
   ┌─────────────────────────────────────────────────────────────────┐
   │                                                                 │
   │    "What is our Q4 revenue forecast?"                           │
   │                                                                 │
   └────────────────────────┬────────────────────────────────────────┘
                            │
                            ▼

   ┌─────────────────────────────────────────────────────────────────┐
   │   BEFORE REACHING THE AI MODEL                                  │
   │                                                                 │
   │   1. Identity check     Who is asking? Are they authorized?     │
   │   2. Model check        Is this AI model approved for use?      │
   │   3. Policy check       Does this request violate any rules?    │
   │   4. Budget check       Is there budget remaining?              │
   │                                                                 │
   │   If any check fails, the request is blocked and logged.        │
   └────────────────────────┬────────────────────────────────────────┘
                            │ allowed
                            ▼

   ┌─────────────────────────────────────────────────────────────────┐
   │   AT THE AI MODEL                                               │
   │                                                                 │
   │   The request is forwarded to the selected provider.            │
   │   The AI generates a response.                                  │
   └────────────────────────┬────────────────────────────────────────┘
                            │ response
                            ▼

   ┌─────────────────────────────────────────────────────────────────┐
   │   BEFORE REACHING THE USER                                      │
   │                                                                 │
   │   5. Content safety     Does the response contain sensitive     │
   │                         data, harmful content, or policy        │
   │                         violations?                             │
   │                                                                 │
   │   If unsafe, the response is blocked and logged.                │
   └────────────────────────┬────────────────────────────────────────┘
                            │ safe
                            ▼

   ┌─────────────────────────────────────────────────────────────────┐
   │   RECORD AND SEAL                                               │
   │                                                                 │
   │   6. Chain of custody   Link this interaction to the previous   │
   │                         one with a cryptographic fingerprint    │
   │   7. Dual storage       Save the complete record to two         │
   │                         independent backends                    │
   └────────────────────────┬────────────────────────────────────────┘
                            │
                            ▼

   ┌─────────────────────────────────────────────────────────────────┐
   │   DELIVER TO USER                                               │
   │                                                                 │
   │   The approved response is returned. End-to-end governance      │
   │   has happened in milliseconds, invisibly to the user.          │
   └─────────────────────────────────────────────────────────────────┘
```

**Every request produces a record.** Whether it was allowed, blocked, or errored, there is always a trace. Nothing happens in the dark.

---

## Core Capabilities

### 1. Universal AI Access

Organizations typically use multiple AI providers: one for general-purpose work, another for sensitive data, another for specialized tasks. Managing each one separately is costly and fragmented.

TruzenAI provides **a single entry point** for all of them. Teams keep using the tools they already know; routing and governance happen behind the scenes.

---

### 2. Complete Audit Trail

Every interaction produces a detailed, structured record.

| Recorded Field | What It Contains |
|---|---|
| **Who** | The user identity tied to the request |
| **When** | Precise timestamp |
| **Which model** | The exact AI model that handled it |
| **Full prompt** | The complete question asked |
| **Full response** | The complete answer returned |
| **Decisions** | Which policies applied and how they evaluated |
| **Safety results** | What the content analyzers found |
| **Usage** | Tokens consumed and processing time |
| **Status** | Allowed, blocked, or errored — and why |

The record is written to **two independent storage systems at the same time**, so the loss of one does not compromise the audit trail.

---

### 3. Real-Time Governance

Policies are evaluated **before** the AI model ever sees a request. This is critical: it means risky requests are prevented, not merely logged after the fact.

Organizations can define rules around:

- Which AI models are approved for use
- Which users or teams can access which models
- What kinds of content are allowed
- What monthly or project-level spending limits apply

Rules are versioned. At any point in time, TruzenAI knows — and can prove — exactly which policies were in effect.

---

### 4. Content Safety

Every response from an AI model passes through multiple independent safety layers before it reaches the user.

```
                ┌──────────────────────────────────────┐
                │        CONTENT SAFETY PIPELINE       │
                └──────────────────────────────────────┘

     AI response
          │
          ▼
     ┌────────────┐    Checks for sensitive information:
     │     1.     │    credit card numbers, social security numbers,
     │  PRIVACY   │    API keys, passwords, email addresses,
     │  SCANNER   │    phone numbers
     └─────┬──────┘
           │
           ▼
     ┌────────────┐    Checks for harmful or offensive language
     │     2.     │
     │  HARMFUL   │
     │  CONTENT   │
     │   FILTER   │
     └─────┬──────┘
           │
           ▼
     ┌────────────┐    Checks fourteen categories of unsafe content
     │     3.     │    including violence, self-harm, hate speech,
     │   SAFETY   │    and child safety
     │ CLASSIFIER │
     └─────┬──────┘
           │
           ▼
     To the user (if safe) or blocked (if not)
```

High-risk findings are **blocked** before the response reaches the user. Lower-risk findings are **flagged** in the audit trail for review.

---

### 5. Tamper-Evident History

Each interaction within a conversation is cryptographically linked to the one before it. The result is a sealed chain of custody for the entire conversation.

```
       Turn 1                Turn 2                Turn 3
    ┌─────────┐           ┌─────────┐           ┌─────────┐
    │ Prompt  │           │ Prompt  │           │ Prompt  │
    │         │           │         │           │         │
    │ Answer  │──links──▶ │ Answer  │──links──▶ │ Answer  │
    │         │           │         │           │         │
    └─────────┘           └─────────┘           └─────────┘

    Alter any turn — even a single character — and every turn
    after it is immediately identifiable as broken. The break
    can be verified independently, without trusting the server.
```

This is what "tamper-evident" means in practice. No one can silently rewrite history. If they try, the change is visible and provable.

---

### 6. Enterprise Identity

TruzenAI integrates with the identity systems your organization already uses — Okta, Azure Active Directory, Google Workspace, and other standards-based providers.

Every AI interaction is automatically tied to a real person, including user ID, email, team or department, and assigned roles. This turns the audit trail from a stream of anonymous events into a clear, attributable record suitable for HR, legal, and compliance review.

---

## The Dashboard

A built-in web dashboard provides real-time insight and administrative control. No additional tooling required.

| View | Purpose |
|---|---|
| **Overview** | Live system health, request volume, and high-level metrics |
| **Sessions** | Searchable list of every conversation, filterable by user, team, model, and date |
| **Timeline** | Visual history of a single conversation, turn by turn |
| **Execution Detail** | Full inspection of any interaction: prompt, response, policies, safety findings |
| **Attempts** | Every attempted request, including those that were blocked |
| **Compliance** | Regulatory reports aligned to EU AI Act, NIST, SOC 2, and ISO 42001 |
| **Control Panel** | Manage approved models, policies, budgets, and safety rules |
| **Playground** | Test prompts safely against governed models |

---

## Compliance Alignment

TruzenAI is designed to help organizations meet the record-keeping, oversight, and risk management requirements of major AI and data governance frameworks.

| Framework | Area Addressed | How TruzenAI Helps |
|---|---|---|
| **EU AI Act** | Record-keeping, human oversight, risk management | Every interaction is automatically recorded; the dashboard provides real-time oversight; risk-based policies are enforced in real time |
| **NIST AI Risk Management Framework** | Govern, Map, Measure, Manage | Policy governance, model approval, usage measurement, and runtime risk management |
| **SOC 2** | Security, availability, integrity, confidentiality | Encrypted storage, fail-safe behavior, dual-write durability, access control |
| **ISO 42001** | AI management system standard | Structured controls for model approval, monitoring, and incident tracking |

The dashboard can generate a formatted compliance report on demand, suitable for sharing with auditors or regulators.

---

## Supported AI Providers

| Provider | Examples |
|---|---|
| **OpenAI** | GPT-4, GPT-4o, reasoning models |
| **Anthropic** | Claude 3.5, Claude 4, Claude 4.5 |
| **Self-Hosted Models** | Llama, Qwen, Gemma, Mistral, and other open-source models |
| **HuggingFace** | Models hosted on HuggingFace Inference Endpoints |
| **Custom and Enterprise** | Any AI service that exposes a standard REST API |

All providers are accessed through a single unified entry point. Users and applications do not need to know which provider is handling a given request — TruzenAI routes it automatically.

---

## Who This Is For

### By Role

| Role | What TruzenAI Delivers |
|---|---|
| **Chief Information Security Officer** | Centralized visibility and control over all AI traffic, with an audit trail that withstands scrutiny |
| **Chief Technology Officer** | A governance layer that does not require application rewrites or new integrations |
| **Chief Financial Officer** | Enforced budgets per team, model, and time period |
| **Compliance Officer** | Direct alignment with EU AI Act, NIST, SOC 2, and ISO 42001, with reports available on demand |
| **Chief Executive Officer** | Verifiable assurance that AI policy is being enforced, not merely documented |
| **Legal and Data Protection Officer** | Structural protection against sensitive data leaving through AI interactions |
| **Head of AI** | A unified control surface for every AI model the organization uses |

### By Industry

| Industry | Primary Concern | How TruzenAI Addresses It |
|---|---|---|
| **Financial Services** | Proving that AI-assisted decisions are governed and auditable | Complete, tamper-evident trail of every AI interaction |
| **Healthcare** | Preventing patient data from leaking through AI interactions | Real-time privacy scanning blocks sensitive data before it leaves |
| **Government and Defense** | Ensuring only approved models run on approved networks | Formal model approval with enforcement at the point of use |
| **Legal Services** | Preserving confidentiality and professional privilege | Complete, uneditable recording of every client-related exchange |
| **Insurance** | Demonstrating how AI reached a particular conclusion | Full conversation history available for review and audit |
| **Pharmaceutical and Life Sciences** | Regulatory traceability for AI-assisted research | Durable, dual-written records with independent verification |
| **Education** | Oversight of student and faculty AI use | Usage tracking, policy enforcement, and budget controls |
| **Any Enterprise** | "We are using AI and we lack visibility" | Dashboard-first visibility with no engineering effort required |

---

## Deployment

TruzenAI can be deployed to fit any infrastructure.

| Option | Best For |
|---|---|
| **Docker** | Single-server production deployments |
| **Kubernetes** | Multi-replica, highly available environments |
| **Native Installation** | Development, testing, and air-gapped environments |

TruzenAI runs on standard infrastructure, requires no special hardware, and can be deployed on-premises, in a private cloud, or in a commercial cloud.

---

## Rolling It Out

TruzenAI is designed for gradual adoption. Organizations do not need to enforce everything on day one.

```
   PHASE 1              PHASE 2              PHASE 3              PHASE 4
   ───────              ───────              ───────              ───────

    OBSERVE              SHADOW             SELECTIVE              FULL
                                           ENFORCEMENT          GOVERNANCE

   Record              Evaluate             Enforce              Enforce
   everything.         policies in          model approval,      all policies.
   Enforce             silent mode.         budgets, and         Full audit.
   nothing.            Log what would       content safety.      Full safety.
                       have been blocked.
                                                                 Full chain
                                                                 integrity.

      ▼                    ▼                    ▼                    ▼

   Teams are           Rule accuracy        Unauthorized         Ongoing
   unaffected.         is validated         activity is          governance
   Baseline            before any user      blocked.             is managed
   visibility          impact.              Everything else      from the
   is established.                          continues to         dashboard.
                                            work.
```

This phased approach lets organizations build confidence with real data before any user-facing enforcement takes effect.

---

## Glossary

Key terms used throughout this document, in plain language.

| Term | Meaning |
|---|---|
| **AI Model / Large Language Model (LLM)** | The software that generates answers from questions. Examples include ChatGPT, Claude, and open-source models such as Llama. |
| **Provider** | The company or platform that hosts an AI model (for example, OpenAI, Anthropic, or an internal server). |
| **Prompt** | The question or instruction sent to an AI model. |
| **Response** | The answer returned by an AI model. |
| **Inference** | The technical term for the moment when an AI model produces an answer from a prompt. |
| **Gateway** | Software that sits between users and a service, controlling and inspecting traffic as it flows through. Comparable to a security checkpoint at an airport. |
| **Audit Trail** | A complete record of every action taken in a system, used for review, investigation, and regulatory compliance. |
| **Personally Identifiable Information (PII)** | Information that can identify a person, such as names, email addresses, phone numbers, social security numbers, and credit card numbers. |
| **Policy** | A rule defining what is or is not allowed — for example, "employees may only use approved AI models." |
| **Attestation** | A formal statement that something has been verified and approved. In this context, it means a particular AI model has been registered and authorized for use. |
| **Token** | The unit AI providers use to measure and charge for usage. A typical sentence is roughly fifteen to twenty-five tokens. |
| **Streaming** | When an AI model delivers its answer word by word as it is generated, rather than waiting for the full response. This is the typing effect familiar from consumer chat tools. |
| **Cryptographic Fingerprint (Hash)** | A digital signature of data. Any change to the data, no matter how small, produces a completely different fingerprint, making tampering immediately detectable. |
| **SHA3-512** | An international standard, approved by the US National Institute of Standards and Technology, for creating cryptographic fingerprints. The Walacor backend uses this when it issues the `DH` (data hash) on ingest. |
| **Tamper-Evident** | Not the same as tamper-proof. It means that if someone does alter the data, the alteration becomes immediately visible and provable. |
| **Single Sign-On (SSO)** | A login system that lets employees use one corporate identity to access many applications, the same way they log into Microsoft 365 or Google Workspace. |
| **Session** | A single conversation, made up of one or more turns between a user and an AI model. |
| **Dashboard** | A web interface for monitoring and managing a system. |
| **Dual-Write** | Saving the same record to two independent storage systems at the same time, so no single failure can lose data. |
| **Compliance Framework** | A published set of standards, such as EU AI Act, SOC 2, or NIST AI RMF, that organizations must follow to satisfy regulators or auditors. |
| **On-Premises** | Software running on infrastructure that the organization owns and controls directly, as opposed to running in a third-party cloud. |

---

## At a Glance

| | |
|---|---|
| **Providers supported** | OpenAI, Anthropic, self-hosted models, HuggingFace, any REST API |
| **Safety layers** | Privacy scanner, harmful content filter, fourteen-category safety classifier |
| **Audit completeness** | Every request produces a record — allowed, blocked, or errored |
| **Integrity guarantee** | Cryptographically chained; tampering is detectable and provable |
| **Identity integration** | Single Sign-On (Okta, Azure Active Directory, Google Workspace, and others) |
| **Compliance alignment** | EU AI Act, NIST AI RMF, SOC 2, ISO 42001 |
| **Deployment** | Docker, Kubernetes, or native installation |
| **Hosting** | On-premises, private cloud, or commercial cloud |
| **User impact** | No code changes required on the user side |

---

**TruzenAI — The enterprise governance layer for artificial intelligence.**
