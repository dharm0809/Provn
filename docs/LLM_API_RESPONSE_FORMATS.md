# LLM API Response Format Reference — Training Dataset for ONNX Schema Mapper

**Purpose**: Exact JSON response structures from 19 LLM providers, documenting where
content, usage, tool_calls, thinking/reasoning, and finish_reason live in each.
Use this as ground truth for training an ONNX model that maps any provider response
to a canonical schema.

**Date**: 2026-04-03

---

## Table of Contents

1. [OpenAI (Chat Completions)](#1-openai-chat-completions)
2. [OpenAI (Responses API)](#2-openai-responses-api)
3. [Anthropic (Messages API)](#3-anthropic-messages-api)
4. [Google Gemini (generateContent)](#4-google-gemini-generatecontent)
5. [Ollama (/api/chat)](#5-ollama-apichat)
6. [Ollama (/v1/chat/completions)](#6-ollama-v1chatcompletions)
7. [Cohere (Chat v2)](#7-cohere-chat-v2)
8. [Mistral (Chat Completions)](#8-mistral-chat-completions)
9. [Together AI](#9-together-ai)
10. [Groq](#10-groq)
11. [Perplexity (Sonar)](#11-perplexity-sonar)
12. [DeepSeek](#12-deepseek)
13. [Fireworks AI](#13-fireworks-ai)
14. [AWS Bedrock — Anthropic Claude](#14-aws-bedrock--anthropic-claude)
15. [AWS Bedrock — Amazon Titan](#15-aws-bedrock--amazon-titan)
16. [Azure OpenAI](#16-azure-openai)
17. [HuggingFace Inference API (TGI)](#17-huggingface-inference-api-tgi)
18. [Replicate](#18-replicate)
19. [xAI (Grok)](#19-xai-grok)
20. [AI21 Labs (Jamba)](#20-ai21-labs-jamba)
21. [Writer (Palmyra)](#21-writer-palmyra)
22. [Cerebras](#22-cerebras)
23. [Universal Field Mapping](#universal-field-mapping)
24. [Provider-Specific Unique Fields](#provider-specific-unique-fields)
25. [Streaming (SSE) Format Differences](#streaming-sse-format-differences)
26. [Canonical Schema Proposal](#canonical-schema-proposal)

---

## 1. OpenAI (Chat Completions)

**Endpoint**: `POST /v1/chat/completions`
**Format family**: OpenAI-standard (most providers clone this)

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1677858242,
  "model": "gpt-4o-2024-08-06",
  "system_fingerprint": "fp_44709d6fcb",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you today?",
        "refusal": null,
        "tool_calls": [
          {
            "id": "call_abc123",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"location\": \"Boston, MA\"}"
            }
          }
        ]
      },
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 9,
    "completion_tokens": 12,
    "total_tokens": 21,
    "prompt_tokens_details": {
      "cached_tokens": 0,
      "audio_tokens": 0
    },
    "completion_tokens_details": {
      "reasoning_tokens": 0,
      "audio_tokens": 0,
      "accepted_prediction_tokens": 0,
      "rejected_prediction_tokens": 0
    }
  }
}
```

**Field locations**:
- Content: `choices[0].message.content`
- Usage: `usage.prompt_tokens`, `usage.completion_tokens`, `usage.total_tokens`
- Tool calls: `choices[0].message.tool_calls[]` (each has `id`, `type`, `function.name`, `function.arguments`)
- Finish reason: `choices[0].finish_reason` — values: `stop`, `length`, `tool_calls`, `content_filter`, `function_call`
- Thinking/reasoning: `usage.completion_tokens_details.reasoning_tokens` (token count only; no text exposed for o1/o3/o4-mini via Chat Completions)
- Unique: `system_fingerprint`, `logprobs`, `refusal`, `prompt_tokens_details.cached_tokens`, `completion_tokens_details.accepted_prediction_tokens`

---

## 2. OpenAI (Responses API)

**Endpoint**: `POST /v1/responses`
**Note**: Successor to Chat Completions for reasoning models (o1, o3, o4-mini)

```json
{
  "id": "resp_abc123",
  "object": "response",
  "created_at": 1710000000,
  "status": "completed",
  "model": "o3-2025-04-16",
  "output": [
    {
      "type": "reasoning",
      "id": "rs_abc123",
      "summary": [
        {
          "type": "summary_text",
          "text": "I need to think about this step by step..."
        }
      ]
    },
    {
      "type": "message",
      "id": "msg_abc123",
      "status": "completed",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "The answer is 42.",
          "annotations": []
        }
      ]
    },
    {
      "type": "web_search_call",
      "id": "ws_abc123",
      "status": "completed",
      "action": {
        "type": "search",
        "queries": ["latest AI news"],
        "sources": [
          {"url": "https://example.com", "title": "Example"}
        ]
      }
    },
    {
      "type": "function_call",
      "id": "fc_abc123",
      "call_id": "call_abc123",
      "name": "get_weather",
      "arguments": "{\"location\": \"Boston\"}"
    }
  ],
  "usage": {
    "input_tokens": 50,
    "output_tokens": 200,
    "total_tokens": 250,
    "output_tokens_details": {
      "reasoning_tokens": 150
    }
  },
  "temperature": 1.0,
  "top_p": 1.0,
  "max_output_tokens": null,
  "previous_response_id": null,
  "reasoning": {
    "effort": "medium",
    "summary": "auto"
  },
  "incomplete_details": null,
  "instructions": null,
  "metadata": {},
  "tools": [],
  "tool_choice": "auto",
  "parallel_tool_calls": true,
  "truncation": "disabled",
  "text": {
    "format": {"type": "text"}
  }
}
```

**Field locations**:
- Content: `output[].content[].text` (where `output[].type == "message"`)
- Usage: `usage.input_tokens`, `usage.output_tokens` (NOT `prompt_tokens`/`completion_tokens`)
- Tool calls: `output[]` where `type` is `function_call`, `web_search_call`, `code_interpreter_call`, `file_search_call`
- Finish reason: `status` at response level (`completed`, `incomplete`, `failed`)
- Thinking/reasoning: `output[]` where `type == "reasoning"`, text in `summary[].text`; also `usage.output_tokens_details.reasoning_tokens`
- Unique: `previous_response_id` (conversation chaining), `reasoning.effort`, `reasoning.summary`, `output[].type` polymorphism, `web_search_call.action.sources`

---

## 3. Anthropic (Messages API)

**Endpoint**: `POST /v1/messages`
**Format family**: Anthropic-native (completely different from OpenAI)

```json
{
  "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
  "type": "message",
  "role": "assistant",
  "content": [
    {
      "type": "thinking",
      "thinking": "Let me reason through this step by step...",
      "signature": "ErUBCk..."
    },
    {
      "type": "text",
      "text": "Hello! How can I help you today?"
    },
    {
      "type": "tool_use",
      "id": "toolu_01A09q90qw90lq917835lq9",
      "name": "get_weather",
      "input": {"location": "Boston, MA"}
    }
  ],
  "model": "claude-opus-4-6",
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {
    "input_tokens": 25,
    "output_tokens": 150,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0
  }
}
```

**Field locations**:
- Content: `content[]` where `type == "text"` -> `.text` (content is an ARRAY of blocks, not a string)
- Usage: `usage.input_tokens`, `usage.output_tokens` (NOT `prompt_tokens`/`completion_tokens`)
- Tool calls: `content[]` where `type == "tool_use"` -> `.id`, `.name`, `.input` (NOT nested under `function`)
- Finish reason: `stop_reason` (NOT `finish_reason`) — values: `end_turn`, `max_tokens`, `stop_sequence`, `tool_use`
- Thinking/reasoning: `content[]` where `type == "thinking"` -> `.thinking` (full text), `.signature` (encrypted state)
- Unique: `cache_creation_input_tokens`, `cache_read_input_tokens` (prompt caching), `stop_sequence`, `signature` on thinking blocks, `server_tool_use` (for Anthropic-hosted tools like computer_use), `type: "redacted_thinking"` block

**Key differences from OpenAI**:
- Content is always an array of typed blocks, never a plain string
- Tool calls are content blocks (same level as text), not a separate field
- `input` (not `arguments`); already parsed dict (not JSON string)
- `stop_reason` (not `finish_reason`)
- `input_tokens`/`output_tokens` (not `prompt_tokens`/`completion_tokens`)
- No `choices[]` wrapper — single response at top level
- No `created` timestamp in response
- `tool_use` has `id` at block level (not `call_id`)

---

## 4. Google Gemini (generateContent)

**Endpoint**: `POST /v1beta/models/{model}:generateContent`
**Format family**: Google-native (camelCase, deeply nested)

```json
{
  "candidates": [
    {
      "content": {
        "parts": [
          {
            "text": "Hello! How can I help you today?"
          },
          {
            "functionCall": {
              "name": "get_weather",
              "args": {
                "location": "Boston, MA"
              }
            }
          },
          {
            "thought": true,
            "text": "Let me think about this step by step..."
          }
        ],
        "role": "model"
      },
      "finishReason": "STOP",
      "index": 0,
      "safetyRatings": [
        {
          "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
          "probability": "NEGLIGIBLE",
          "blocked": false
        },
        {
          "category": "HARM_CATEGORY_HATE_SPEECH",
          "probability": "NEGLIGIBLE",
          "blocked": false
        },
        {
          "category": "HARM_CATEGORY_HARASSMENT",
          "probability": "NEGLIGIBLE",
          "blocked": false
        },
        {
          "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
          "probability": "NEGLIGIBLE",
          "blocked": false
        }
      ],
      "citationMetadata": {
        "citations": [
          {
            "startIndex": 0,
            "endIndex": 100,
            "uri": "https://example.com",
            "title": "Example Source",
            "license": "",
            "publicationDate": {
              "year": 2024,
              "month": 1,
              "day": 15
            }
          }
        ]
      }
    }
  ],
  "usageMetadata": {
    "promptTokenCount": 10,
    "candidatesTokenCount": 50,
    "totalTokenCount": 60,
    "thoughtsTokenCount": 120,
    "cachedContentTokenCount": 0
  },
  "modelVersion": "gemini-2.0-flash",
  "promptFeedback": {
    "blockReason": "SAFETY",
    "safetyRatings": []
  }
}
```

**Field locations**:
- Content: `candidates[0].content.parts[]` where `"text"` key exists and `"thought"` is not true
- Usage: `usageMetadata.promptTokenCount`, `usageMetadata.candidatesTokenCount`, `usageMetadata.totalTokenCount`
- Tool calls: `candidates[0].content.parts[]` where `"functionCall"` key exists -> `.functionCall.name`, `.functionCall.args`
- Finish reason: `candidates[0].finishReason` — values: `STOP`, `MAX_TOKENS`, `SAFETY`, `RECITATION`, `OTHER` (SCREAMING_CASE)
- Thinking/reasoning: `candidates[0].content.parts[]` where `thought == true` -> `.text`; also `usageMetadata.thoughtsTokenCount`
- Unique: `safetyRatings[]` per candidate (per-category harm scores), `citationMetadata`, `promptFeedback.blockReason`, `cachedContentTokenCount`, `groundingMetadata` (for grounded responses)

**Key differences from OpenAI**:
- camelCase everywhere (not snake_case)
- `candidates[]` (not `choices[]`)
- `parts[]` array for content (similar to Anthropic blocks but different structure)
- `functionCall` (not `tool_calls[]`); `args` is a dict (not JSON string `arguments`)
- Token names: `promptTokenCount` / `candidatesTokenCount` (not `prompt_tokens` / `completion_tokens`)
- Safety ratings are per-candidate with category-level granularity
- No top-level `id` field in response
- `thought: true` flag on parts (not separate block type)

---

## 5. Ollama (/api/chat)

**Endpoint**: `POST /api/chat`
**Format family**: Ollama-native (unique structure with timing metrics)

```json
{
  "model": "llama3.2",
  "created_at": "2023-08-04T19:22:45.499127Z",
  "message": {
    "role": "assistant",
    "content": "Hello! How can I help you today?",
    "tool_calls": [
      {
        "function": {
          "name": "get_weather",
          "arguments": {
            "location": "Boston, MA"
          }
        }
      }
    ]
  },
  "done": true,
  "done_reason": "stop",
  "total_duration": 5043500667,
  "load_duration": 5025959,
  "prompt_eval_count": 26,
  "prompt_eval_duration": 325953000,
  "eval_count": 290,
  "eval_duration": 4709213000,
  "context": [1, 2, 3]
}
```

**Field locations**:
- Content: `message.content`
- Usage: `prompt_eval_count` (prompt tokens), `eval_count` (completion tokens) — NO `usage` object
- Tool calls: `message.tool_calls[]` -> `.function.name`, `.function.arguments` (arguments is a dict, NOT JSON string)
- Finish reason: `done_reason` — values: `stop`, `length`; also `done` boolean
- Thinking/reasoning: Not natively supported in /api/chat; `<think>` tags appear inline in `message.content`
- Unique: `total_duration`, `load_duration`, `prompt_eval_duration`, `eval_duration` (nanosecond timing), `context` (KV cache state), `created_at` (ISO 8601, not unix timestamp)

**Key differences from OpenAI**:
- Flat structure, no `choices[]` wrapper
- `message` (singular, not array)
- Duration fields in nanoseconds
- Token counts are flat fields, not nested in `usage`
- `done` boolean + `done_reason` (not `finish_reason`)
- `arguments` is already a parsed dict (not JSON string)
- `context` array for KV cache continuation
- No `id` field

---

## 6. Ollama (/v1/chat/completions)

**Endpoint**: `POST /v1/chat/completions`
**Format family**: OpenAI-compatible (Ollama's compatibility layer)

```json
{
  "id": "chatcmpl-123",
  "object": "chat.completion",
  "created": 1677652288,
  "model": "llama3.2",
  "system_fingerprint": "fp_ollama",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you today?",
        "reasoning": "Let me think about this...",
        "tool_calls": [
          {
            "id": "call_abc123",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"location\": \"Boston, MA\"}"
            }
          }
        ]
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 26,
    "completion_tokens": 290,
    "total_tokens": 316
  }
}
```

**Field locations**: Same as OpenAI Chat Completions, plus:
- Thinking/reasoning: `choices[0].message.reasoning` (Ollama natively separates `<think>` blocks for thinking models)
- Unique: `message.reasoning` field (not present in standard OpenAI)

**Note**: Ollama's OpenAI-compat layer converts nanosecond durations to standard `usage` tokens. The `reasoning` field was added in newer Ollama versions for models like qwen3 and deepseek-r1.

---

## 7. Cohere (Chat v2)

**Endpoint**: `POST /v2/chat`
**Format family**: Cohere-native (hybrid — some OpenAI-like structure with unique fields)

```json
{
  "id": "c14ee9da-5f1e-4b02-88a8-75e09e288544",
  "finish_reason": "COMPLETE",
  "message": {
    "role": "assistant",
    "content": [
      {
        "type": "text",
        "text": "Hello! How can I help you today?"
      }
    ],
    "tool_plan": "I need to look up the weather for this request.",
    "tool_calls": [
      {
        "id": "call_abc123",
        "type": "function",
        "function": {
          "name": "get_weather",
          "arguments": "{\"location\": \"Boston, MA\"}"
        }
      }
    ],
    "citations": [
      {
        "start": 0,
        "end": 50,
        "text": "...",
        "sources": [
          {
            "type": "document",
            "id": "doc_1",
            "document": {"id": "1", "title": "..."}
          }
        ]
      }
    ]
  },
  "usage": {
    "billed_units": {
      "input_tokens": 25,
      "output_tokens": 50
    },
    "tokens": {
      "input_tokens": 25,
      "output_tokens": 50
    }
  }
}
```

**Field locations**:
- Content: `message.content[0].text` (content is array of typed blocks, like Anthropic)
- Usage: `usage.billed_units.input_tokens`, `usage.billed_units.output_tokens` AND `usage.tokens.input_tokens`, `usage.tokens.output_tokens`
- Tool calls: `message.tool_calls[]` -> `.function.name`, `.function.arguments` (OpenAI-compatible structure)
- Finish reason: `finish_reason` at TOP level (not inside choices) — values: `COMPLETE`, `MAX_TOKENS`, `STOP_SEQUENCE`, `TOOL_CALL`, `ERROR`, `TIMEOUT` (SCREAMING_CASE)
- Thinking/reasoning: `message.tool_plan` (a text string showing the model's plan before tool calls)
- Unique: `message.tool_plan`, `message.citations[]` (inline citations with start/end character offsets), `usage.billed_units` (separate from `usage.tokens`), `finish_reason` is SCREAMING_CASE

**Key differences from OpenAI**:
- No `choices[]` wrapper — single `message` at top level
- Content is array of typed blocks (like Anthropic)
- `finish_reason` at top level, not inside a choice
- SCREAMING_CASE finish reasons (`COMPLETE` not `stop`)
- Dual usage: `billed_units` + `tokens`
- `tool_plan` field (model's reasoning about tool usage)
- Built-in `citations` with character-level offsets
- `input_tokens`/`output_tokens` (not `prompt_tokens`/`completion_tokens`)

---

## 8. Mistral (Chat Completions)

**Endpoint**: `POST /v1/chat/completions`
**Format family**: OpenAI-compatible (very close clone)

```json
{
  "id": "cf79f7daaee244b1a0ae5c7b1444424a",
  "object": "chat.completion",
  "model": "mistral-large-latest",
  "created": 1759500534,
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you today?",
        "tool_calls": [
          {
            "id": "call_abc123",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"location\": \"Boston\"}"
            }
          }
        ],
        "prefix": false
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 24,
    "completion_tokens": 27,
    "total_tokens": 51
  }
}
```

**Field locations**: Identical to OpenAI Chat Completions, except:
- Unique: `message.prefix` (boolean, for FIM/prefix completion mode)
- No `system_fingerprint`
- No `logprobs`
- No `completion_tokens_details`

---

## 9. Together AI

**Endpoint**: `POST /v1/chat/completions`
**Format family**: OpenAI-compatible

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1699000000,
  "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you?",
        "tool_calls": [
          {
            "id": "call_abc123",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"location\": \"Boston\"}"
            }
          }
        ]
      },
      "finish_reason": "stop",
      "logprobs": null
    }
  ],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 15,
    "total_tokens": 35
  }
}
```

**Field locations**: Identical to OpenAI Chat Completions.
- No `system_fingerprint`
- No `completion_tokens_details`
- Supports `response_format.schema` for structured output (provider-specific extension)

---

## 10. Groq

**Endpoint**: `POST /openai/v1/chat/completions`
**Format family**: OpenAI-compatible with timing extensions

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1699000000,
  "model": "llama-3.3-70b-versatile",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you?"
      },
      "finish_reason": "stop",
      "logprobs": null
    }
  ],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 15,
    "total_tokens": 35,
    "queue_time": 0.018393202,
    "prompt_time": 0.003652567,
    "completion_time": 2.567331286,
    "total_time": 2.570983853
  },
  "system_fingerprint": "fp_groq_abc123",
  "x_groq": {
    "id": "req_abc123"
  }
}
```

**Field locations**: Same as OpenAI Chat Completions, plus:
- Unique: `usage.queue_time`, `usage.prompt_time`, `usage.completion_time`, `usage.total_time` (timing in seconds inside usage object)
- Unique: `x_groq.id` (Groq-specific request ID)
- Timing fields enable tokens-per-second calculation without separate API call

---

## 11. Perplexity (Sonar)

**Endpoint**: `POST /chat/completions`
**Format family**: OpenAI-compatible with citation extensions

```json
{
  "id": "pplx-abc123",
  "model": "sonar-pro",
  "object": "chat.completion",
  "created": 1699000000,
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "According to recent sources, the answer is..."
      },
      "finish_reason": "stop"
    }
  ],
  "citations": [
    "https://example.com/article1",
    "https://example.com/article2",
    "https://example.com/article3"
  ],
  "search_results": [
    {
      "title": "Article Title",
      "url": "https://example.com/article1",
      "snippet": "Relevant excerpt from the article..."
    }
  ],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 150,
    "total_tokens": 170,
    "search_context_size": 5000,
    "cost": {
      "input_tokens_cost": 0.001,
      "output_tokens_cost": 0.003,
      "request_cost": 0.005,
      "total_cost": 0.009
    }
  }
}
```

**Field locations**: Same as OpenAI Chat Completions, plus:
- Unique: `citations[]` (array of URL strings at top level)
- Unique: `search_results[]` (structured search context with title, url, snippet)
- Unique: `usage.search_context_size` (number of context tokens from search)
- Unique: `usage.cost` object (input_tokens_cost, output_tokens_cost, request_cost, total_cost)
- Thinking: sonar-reasoning models include `<think>` blocks inline in `content`

---

## 12. DeepSeek

**Endpoint**: `POST /v1/chat/completions`
**Format family**: OpenAI-compatible with reasoning extension

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1699000000,
  "model": "deepseek-reasoner",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "The answer to 2+2 is 4.",
        "reasoning_content": "Let me think about this. 2+2 is a basic arithmetic operation. The sum of 2 and 2 equals 4."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 50,
    "total_tokens": 60,
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 10,
    "completion_tokens_details": {
      "reasoning_tokens": 35,
      "accepted_prediction_tokens": null,
      "rejected_prediction_tokens": null
    }
  }
}
```

**Field locations**: Same as OpenAI Chat Completions, plus:
- Thinking/reasoning: `choices[0].message.reasoning_content` (FULL reasoning text as a string alongside `content`)
- Unique: `usage.prompt_cache_hit_tokens`, `usage.prompt_cache_miss_tokens`
- Unique: `usage.completion_tokens_details.reasoning_tokens` (count of CoT tokens)
- `max_tokens` includes reasoning tokens (CoT + final answer)

**Streaming**: `delta.reasoning_content` for reasoning chunks, `delta.content` for answer chunks. Reasoning is emitted first, then content.

---

## 13. Fireworks AI

**Endpoint**: `POST /inference/v1/chat/completions`
**Format family**: OpenAI-compatible

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1699000000,
  "model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you?"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 15,
    "total_tokens": 35
  }
}
```

**Field locations**: Identical to OpenAI Chat Completions.
- Model IDs use `accounts/{org}/models/{model}` format
- Supports grammar-based structured output (BNF grammar, provider-specific extension)
- Reasoning models emit `<think>...</think>` inline in content

---

## 14. AWS Bedrock -- Anthropic Claude

**Endpoint**: `POST /model/{modelId}/invoke`
**Format family**: Anthropic Messages (wrapped in Bedrock envelope)

```json
{
  "id": "msg_bdrk_01A09q90qw90lq917835lq9",
  "type": "message",
  "role": "assistant",
  "content": [
    {
      "type": "text",
      "text": "Hello! How can I help you?"
    },
    {
      "type": "tool_use",
      "id": "toolu_bdrk_01A09q90",
      "name": "get_weather",
      "input": {"location": "Boston, MA"}
    }
  ],
  "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {
    "input_tokens": 25,
    "output_tokens": 100
  }
}
```

**Field locations**: Identical to Anthropic Messages API.
- Bedrock uses `anthropic_version: "bedrock-2023-05-31"` in request
- Model ID format: `anthropic.claude-{version}` (Bedrock ARN format)
- Authentication via AWS Sig v4 (not API key)
- Response body is identical to native Anthropic Messages API

---

## 15. AWS Bedrock -- Amazon Titan

**Endpoint**: `POST /model/{modelId}/invoke`
**Format family**: Amazon-native (unique structure)

```json
{
  "inputTextTokenCount": 26,
  "results": [
    {
      "tokenCount": 290,
      "outputText": "Hello! How can I help you today?",
      "completionReason": "FINISHED"
    }
  ]
}
```

**Field locations**:
- Content: `results[0].outputText`
- Usage: `inputTextTokenCount` (prompt), `results[0].tokenCount` (completion) — flat, not nested
- Tool calls: NOT SUPPORTED natively (Titan Text does not support function calling)
- Finish reason: `results[0].completionReason` — values: `FINISHED`, `LENGTH`, `STOP_CRITERIA_MET`, `CONTENT_FILTERED` (SCREAMING_CASE)
- Thinking/reasoning: NOT SUPPORTED
- Unique: Extremely minimal response — no `id`, no timestamp, no `model` field in response

**Key differences from everything else**:
- No `id` field
- `results[]` (not `choices[]` or `candidates[]`)
- `outputText` (not `content`)
- `completionReason` (not `finish_reason` or `stop_reason`)
- `inputTextTokenCount` at top level (not in a `usage` object)
- No tool/function calling support

---

## 16. Azure OpenAI

**Endpoint**: `POST /openai/deployments/{deployment-id}/chat/completions?api-version=2024-10-21`
**Format family**: OpenAI-compatible (nearly identical)

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1699000000,
  "model": "gpt-4o",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you?",
        "tool_calls": [
          {
            "id": "call_abc123",
            "type": "function",
            "function": {
              "name": "get_weather",
              "arguments": "{\"location\": \"Boston\"}"
            }
          }
        ]
      },
      "finish_reason": "stop",
      "content_filter_results": {
        "hate": {"filtered": false, "severity": "safe"},
        "self_harm": {"filtered": false, "severity": "safe"},
        "sexual": {"filtered": false, "severity": "safe"},
        "violence": {"filtered": false, "severity": "safe"}
      }
    }
  ],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 15,
    "total_tokens": 35
  },
  "system_fingerprint": "fp_abc123",
  "prompt_filter_results": [
    {
      "prompt_index": 0,
      "content_filter_results": {
        "hate": {"filtered": false, "severity": "safe"},
        "self_harm": {"filtered": false, "severity": "safe"},
        "sexual": {"filtered": false, "severity": "safe"},
        "violence": {"filtered": false, "severity": "safe"}
      }
    }
  ]
}
```

**Field locations**: Identical to OpenAI Chat Completions, plus:
- Unique: `choices[0].content_filter_results` (per-category safety with `filtered` and `severity`)
- Unique: `prompt_filter_results[]` (safety evaluation of the input prompt)
- `api-version` query parameter required on every request
- Deployment-based routing (not model name in URL)
- Azure also supports the Responses API as of 2025

---

## 17. HuggingFace Inference API (TGI)

**Format family**: Two formats — OpenAI-compatible (`/v1/chat/completions`) and TGI-native (`/generate`)

### TGI-native /generate endpoint:

```json
{
  "generated_text": "Hello! How can I help you today?",
  "details": {
    "finish_reason": "length",
    "generated_tokens": 50,
    "seed": null,
    "prefill": [
      {"id": 1, "text": "<s>", "logprob": null}
    ],
    "tokens": [
      {
        "id": 22557,
        "text": "Hello",
        "logprob": -0.5,
        "special": false
      }
    ],
    "best_of_sequences": null
  }
}
```

### TGI OpenAI-compatible /v1/chat/completions endpoint:

```json
{
  "id": "",
  "object": "chat.completion",
  "created": 1699000000,
  "model": "tgi",
  "system_fingerprint": "2.4.1-native",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you today?",
        "tool_calls": null
      },
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 50,
    "total_tokens": 70
  }
}
```

### TGI-native streaming (SSE via /generate_stream):

```json
{
  "token": {
    "id": 22557,
    "text": "Hello",
    "logprob": -0.5,
    "special": false
  },
  "generated_text": null,
  "details": null
}
```

**Field locations (TGI-native)**:
- Content: `generated_text` (top-level string)
- Usage: `details.generated_tokens` (completion only; no prompt token count)
- Tool calls: Not supported in /generate
- Finish reason: `details.finish_reason` — values: `length`, `eos_token`, `stop_sequence`
- Thinking/reasoning: Not supported
- Unique: `details.tokens[]` (per-token logprobs with IDs), `details.prefill[]`, `details.seed`, `details.best_of_sequences`

**Key differences**:
- TGI-native has no `usage` object, just `details.generated_tokens`
- No prompt token count in TGI-native response
- Per-token detail available by default (not opt-in like OpenAI `logprobs`)
- Streaming uses `token.text` (not `delta.content`)
- OpenAI-compat endpoint matches standard OpenAI format

---

## 18. Replicate

**Endpoint**: `POST /v1/predictions`
**Format family**: Replicate-native (async prediction model, not real-time chat)

```json
{
  "id": "gm3qorzdhgbfurvjtvhg6dckhu",
  "model": "replicate/hello-world",
  "version": "5c7d5dc6dd8bf75c1acaa8565735e7986bc5b66206b55cca93cb72c9bf15ccaa",
  "input": {
    "text": "What is the meaning of life?"
  },
  "logs": "Running prediction...\nTokens generated: 50",
  "output": "The meaning of life is a deeply philosophical question...",
  "error": null,
  "status": "succeeded",
  "created_at": "2023-09-08T16:19:34.765994Z",
  "data_removed": false,
  "started_at": "2023-09-08T16:19:34.779176Z",
  "completed_at": "2023-09-08T16:19:34.791859Z",
  "metrics": {
    "predict_time": 0.012683,
    "total_time": 0.025366
  },
  "urls": {
    "cancel": "https://api.replicate.com/v1/predictions/gm3.../cancel",
    "get": "https://api.replicate.com/v1/predictions/gm3..."
  }
}
```

**Field locations**:
- Content: `output` (can be string, array, or any JSON type depending on model)
- Usage: No token counts in response; `metrics.predict_time`, `metrics.total_time` only
- Tool calls: NOT SUPPORTED
- Finish reason: `status` — values: `starting`, `processing`, `succeeded`, `failed`, `canceled`
- Thinking/reasoning: NOT SUPPORTED (model-dependent; some output `<think>` inline)
- Unique: `version` (model version SHA), `logs` (inference logs), `metrics.predict_time`, `urls.cancel`, `urls.get`, `data_removed`, `started_at`, `completed_at`, async polling model

**Key differences from everything else**:
- NOT a chat completion API — it is an async prediction system
- `output` type varies per model (string, list, object)
- No token counts whatsoever
- `status` is a job status, not a finish reason
- Must poll `urls.get` for results (or use webhooks)
- `version` pins exact model weights (content-addressable)

---

## 19. xAI (Grok)

**Endpoint**: `POST /v1/chat/completions`
**Format family**: OpenAI-compatible

```json
{
  "id": "0daf962f-a275-4a3c-839a-047854645532",
  "object": "chat.completion",
  "created": 1739301120,
  "model": "grok-3-latest",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you today?"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 41,
    "completion_tokens": 104,
    "total_tokens": 145,
    "prompt_tokens_details": {
      "text_tokens": 41,
      "audio_tokens": 0,
      "image_tokens": 0,
      "cached_tokens": 0
    }
  },
  "system_fingerprint": "fp_84ff176447"
}
```

**Field locations**: Identical to OpenAI Chat Completions.
- Unique: `prompt_tokens_details` includes `text_tokens`, `audio_tokens`, `image_tokens` breakdown
- Uses UUID format for `id` (not `chatcmpl-` prefix)
- Supports tool_calls in standard OpenAI format

---

## 20. AI21 Labs (Jamba)

**Endpoint**: `POST /v1/chat/completions`
**Format family**: OpenAI-compatible

```json
{
  "id": "chatcmpl-8zLI4FFBAAApK2mGJ1BJOrMrPZQ8N",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you today?"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 15,
    "total_tokens": 35
  }
}
```

**Field locations**: Identical to OpenAI Chat Completions.
- Finish reason values: `stop`, `length`
- No `object`, `created`, or `system_fingerprint` fields (minimal response)
- Supports streaming with standard SSE delta format

---

## 21. Writer (Palmyra)

**Endpoint**: `POST /v1/chat`
**Format family**: OpenAI-compatible with RAG extensions

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1699000000,
  "model": "palmyra-x5",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I help you today?",
        "refusal": null,
        "tool_calls": null,
        "graph_data": {
          "sources": [
            {"file_id": "abc123", "snippet": "..."}
          ],
          "status": "completed",
          "subqueries": ["related query 1"]
        },
        "llm_data": {
          "prompt": "...",
          "model": "palmyra-x5"
        },
        "translation_data": null
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 20,
    "completion_tokens": 15,
    "total_tokens": 35,
    "prompt_token_details": {
      "cached_tokens": 0
    },
    "completion_token_details": {
      "reasoning_tokens": 0
    }
  },
  "system_fingerprint": "fp_abc123"
}
```

**Field locations**: Same as OpenAI Chat Completions, plus:
- Unique: `message.graph_data` (Knowledge Graph RAG results with sources, status, subqueries)
- Unique: `message.llm_data` (prompt and model metadata)
- Unique: `message.translation_data` (for translation tasks)
- Unique: `usage.completion_token_details.reasoning_tokens`

---

## 22. Cerebras

**Endpoint**: `POST /v1/chat/completions`
**Format family**: OpenAI-compatible with timing extensions

```json
{
  "id": "chatcmpl-292e278f-514e-4186-9010-91ce6a14168b",
  "object": "chat.completion",
  "created": 1723733419,
  "model": "llama3.1-8b",
  "system_fingerprint": "fp_70185065a4",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hello! How can I assist you today?"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 10,
    "total_tokens": 22
  },
  "time_info": {
    "queue_time": 0.000073161,
    "prompt_time": 0.0010744798888888889,
    "completion_time": 0.005658071111111111,
    "total_time": 0.022224903106689453,
    "created": 1723733419
  }
}
```

**Field locations**: Same as OpenAI Chat Completions, plus:
- Unique: `time_info` object (separate from `usage`) with `queue_time`, `prompt_time`, `completion_time`, `total_time`, `created`
- Uses UUID format for `id`
- Timing precision enables exact tokens/second calculation

---

## Universal Field Mapping

Fields that exist in ALL or MOST providers, mapped to a canonical name:

| Canonical Name | OpenAI | Anthropic | Gemini | Ollama-native | Cohere | Bedrock Titan | Replicate |
|---|---|---|---|---|---|---|---|
| **response_id** | `id` | `id` | (none) | (none) | `id` | (none) | `id` |
| **content** | `choices[0].message.content` | `content[0].text` | `candidates[0].content.parts[0].text` | `message.content` | `message.content[0].text` | `results[0].outputText` | `output` |
| **prompt_tokens** | `usage.prompt_tokens` | `usage.input_tokens` | `usageMetadata.promptTokenCount` | `prompt_eval_count` | `usage.tokens.input_tokens` | `inputTextTokenCount` | (none) |
| **completion_tokens** | `usage.completion_tokens` | `usage.output_tokens` | `usageMetadata.candidatesTokenCount` | `eval_count` | `usage.tokens.output_tokens` | `results[0].tokenCount` | (none) |
| **total_tokens** | `usage.total_tokens` | (computed) | `usageMetadata.totalTokenCount` | (computed) | (computed) | (computed) | (none) |
| **finish_reason** | `choices[0].finish_reason` | `stop_reason` | `candidates[0].finishReason` | `done_reason` | `finish_reason` | `results[0].completionReason` | `status` |
| **model** | `model` | `model` | `modelVersion` | `model` | (none) | (none) | `model` |
| **tool_calls** | `choices[0].message.tool_calls` | `content[].{type:tool_use}` | `parts[].functionCall` | `message.tool_calls` | `message.tool_calls` | N/A | N/A |
| **thinking** | (token count only) | `content[].{type:thinking}` | `parts[].{thought:true}` | (inline `<think>`) | `message.tool_plan` | N/A | N/A |

---

## Provider-Specific Unique Fields

| Provider | Unique Field | Purpose |
|---|---|---|
| **OpenAI** | `system_fingerprint` | Backend version tracking |
| **OpenAI** | `completion_tokens_details.reasoning_tokens` | CoT token count |
| **OpenAI** | `prompt_tokens_details.cached_tokens` | Prompt cache hit |
| **OpenAI Responses** | `previous_response_id` | Conversation chaining |
| **OpenAI Responses** | `output[].type` polymorphism | Multi-type output items |
| **Anthropic** | `cache_creation_input_tokens` | Prompt cache write cost |
| **Anthropic** | `cache_read_input_tokens` | Prompt cache read benefit |
| **Anthropic** | `content[].signature` | Encrypted thinking state |
| **Anthropic** | `stop_sequence` | Which stop string matched |
| **Gemini** | `safetyRatings[]` | Per-category harm scores |
| **Gemini** | `citationMetadata.citations[]` | Source citations with ranges |
| **Gemini** | `promptFeedback.blockReason` | Input-level safety block |
| **Gemini** | `usageMetadata.thoughtsTokenCount` | Thinking token count |
| **Ollama** | `total_duration` / `eval_duration` etc. | Nanosecond timing breakdown |
| **Ollama** | `context` | KV cache state for continuation |
| **Ollama** | `message.reasoning` (compat) | Native `<think>` separation |
| **Cohere** | `message.tool_plan` | Model reasoning about tools |
| **Cohere** | `message.citations[]` | Inline citations with char offsets |
| **Cohere** | `usage.billed_units` | Separate billing metrics |
| **Groq** | `usage.queue_time/prompt_time/completion_time` | Inference timing in usage |
| **Groq** | `x_groq.id` | Groq request tracking |
| **Perplexity** | `citations[]` | Source URL array |
| **Perplexity** | `search_results[]` | Structured search context |
| **Perplexity** | `usage.cost` | Per-request cost breakdown |
| **DeepSeek** | `message.reasoning_content` | Full CoT text |
| **DeepSeek** | `usage.prompt_cache_hit_tokens` | KV cache hit count |
| **Azure OpenAI** | `content_filter_results` | Per-category safety per choice |
| **Azure OpenAI** | `prompt_filter_results[]` | Input safety evaluation |
| **HuggingFace TGI** | `details.tokens[]` | Per-token logprob array |
| **HuggingFace TGI** | `details.prefill[]` | Prefill token details |
| **Replicate** | `metrics.predict_time` | Inference duration |
| **Replicate** | `urls.cancel/get` | Async job management |
| **Replicate** | `version` | Content-addressed model SHA |
| **Replicate** | `logs` | Inference runtime logs |
| **Writer** | `message.graph_data` | Knowledge Graph RAG results |
| **Writer** | `message.llm_data` | Internal prompt/model metadata |
| **Cerebras** | `time_info` | Separate timing object |
| **xAI** | `prompt_tokens_details.image_tokens` | Multimodal token breakdown |
| **Mistral** | `message.prefix` | FIM prefix mode flag |

---

## Streaming (SSE) Format Differences

All providers that support streaming use Server-Sent Events (SSE) with `text/event-stream` content type and `data: ` prefix per line. Key differences:

### OpenAI-family (OpenAI, Azure, Mistral, Together, Groq, Fireworks, xAI, AI21, Cerebras, DeepSeek)
```
data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"delta":{"content":"Hello"},"index":0,"finish_reason":null}]}

data: {"id":"chatcmpl-abc","object":"chat.completion.chunk","choices":[{"delta":{},"index":0,"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":12,"total_tokens":21}}

data: [DONE]
```
- Object type: `chat.completion.chunk`
- Content in: `choices[0].delta.content`
- Tool calls in: `choices[0].delta.tool_calls[].function.arguments` (accumulated across chunks)
- Usage in final chunk (when `stream_options.include_usage: true`)
- Terminal: `data: [DONE]`
- DeepSeek adds: `delta.reasoning_content`
- Ollama compat adds: `delta.reasoning`

### Anthropic
```
event: message_start
data: {"type":"message_start","message":{"id":"msg_abc","type":"message","role":"assistant","content":[],"model":"claude-opus-4-6","stop_reason":null,"usage":{"input_tokens":25,"output_tokens":1}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}

event: content_block_start
data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_abc","name":"get_weather"}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"loc"}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":15}}

event: message_stop
data: {"type":"message_stop"}
```
- Uses `event:` field (not just `data:`)
- Named event types: `message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`
- Content in: `delta.text` (where `delta.type == "text_delta"`)
- Tool JSON in: `delta.partial_json` (where `delta.type == "input_json_delta"`)
- Thinking in: `delta.thinking` (where `delta.type == "thinking_delta"`)
- Usage split: input in `message_start`, output in `message_delta`
- NO `data: [DONE]` — uses `event: message_stop`

### Google Gemini
```json
[
  {
    "candidates": [
      {
        "content": {
          "parts": [{"text": "Hello"}],
          "role": "model"
        },
        "finishReason": "STOP",
        "index": 0
      }
    ],
    "usageMetadata": {
      "promptTokenCount": 10,
      "candidatesTokenCount": 5,
      "totalTokenCount": 15
    }
  }
]
```
- Returns JSON array of partial responses (not SSE by default)
- SSE mode (`alt=sse`) uses `data:` prefix with same structure
- Content in: `candidates[0].content.parts[0].text`
- Each chunk is a complete response structure
- Usage in every chunk (cumulative)

### HuggingFace TGI /generate_stream
```
data: {"token":{"id":22557,"text":"Hello","logprob":-0.5,"special":false},"generated_text":null,"details":null}

data: {"token":{"id":330,"text":"!","logprob":-1.2,"special":false},"generated_text":"Hello!","details":{"finish_reason":"eos_token","generated_tokens":2}}
```
- Content in: `token.text` (per-token)
- `generated_text` is null until final chunk
- Per-token logprobs by default
- No `[DONE]` marker; stream ends when `generated_text` is non-null

### Cohere v2 streaming
```
event: message-start
data: {"type":"message-start","id":"abc123"}

event: content-delta
data: {"type":"content-delta","index":0,"delta":{"type":"text-delta","text":"Hello"}}

event: tool-call-start
data: {"type":"tool-call-start","index":0,"delta":{"message":{"tool_calls":{"id":"call_1","type":"function","function":{"name":"get_weather","arguments":""}}}}}

event: tool-call-delta
data: {"type":"tool-call-delta","index":0,"delta":{"message":{"tool_calls":{"function":{"arguments":"{\"lo"}}}}}

event: message-end
data: {"type":"message-end","delta":{"finish_reason":"COMPLETE","usage":{"billed_units":{"input_tokens":10,"output_tokens":20}}}}
```
- Uses named events with hyphenated names (not underscores)
- Content in: `delta.text` (where `type == "text-delta"`)
- Tool args in: `delta.message.tool_calls.function.arguments`
- Usage only in `message-end` event

### Ollama /api/chat streaming
```json
{"model":"llama3.2","created_at":"2023-08-04T19:22:45.499127Z","message":{"role":"assistant","content":"Hello"},"done":false}
{"model":"llama3.2","created_at":"2023-08-04T19:22:46.499127Z","message":{"role":"assistant","content":"!"},"done":false}
{"model":"llama3.2","created_at":"2023-08-04T19:22:47.499127Z","message":{"role":"assistant","content":""},"done":true,"done_reason":"stop","total_duration":5043500667,"prompt_eval_count":26,"eval_count":290,"eval_duration":4709213000}
```
- NDJSON (newline-delimited JSON), NOT SSE (no `data:` prefix)
- Content in: `message.content`
- `done: true` on final chunk (with timing metrics)
- No `[DONE]` marker

---

## Canonical Schema Proposal

Based on the analysis of all 19 providers, the ONNX model should map every response to this canonical structure:

```json
{
  "canonical": {
    "response_id": "string | null",
    "model": "string | null",
    "provider": "string",

    "content": "string",
    "thinking_content": "string | null",

    "tool_calls": [
      {
        "id": "string",
        "type": "string",
        "name": "string",
        "arguments": "object | string"
      }
    ],

    "finish_reason": "stop | length | tool_calls | content_filter | error",

    "usage": {
      "prompt_tokens": "int | null",
      "completion_tokens": "int | null",
      "total_tokens": "int | null",
      "reasoning_tokens": "int | null",
      "cached_tokens": "int | null"
    },

    "citations": [
      {
        "url": "string",
        "title": "string | null",
        "snippet": "string | null"
      }
    ],

    "safety": {
      "blocked": "bool",
      "categories": {}
    },

    "timing": {
      "total_time_ms": "float | null",
      "prompt_time_ms": "float | null",
      "completion_time_ms": "float | null",
      "queue_time_ms": "float | null"
    },

    "metadata": {
      "system_fingerprint": "string | null",
      "provider_specific": {}
    }
  }
}
```

### Mapping Rules by Provider Family

**Family 1 — OpenAI-compatible** (OpenAI, Azure, Mistral, Together, Groq, Fireworks, xAI, AI21, Cerebras, Ollama-compat, DeepSeek):
- `content` = `choices[0].message.content`
- `finish_reason` = `choices[0].finish_reason`
- `prompt_tokens` = `usage.prompt_tokens`
- `completion_tokens` = `usage.completion_tokens`
- `tool_calls` = `choices[0].message.tool_calls` (reformat `function.arguments` from JSON string to object)
- `thinking_content` = `choices[0].message.reasoning_content` (DeepSeek) OR `choices[0].message.reasoning` (Ollama)

**Family 2 — Anthropic** (Anthropic, Bedrock Claude):
- `content` = join all `content[].text` where `type == "text"`
- `finish_reason` = map `stop_reason`: `end_turn` -> `stop`, `tool_use` -> `tool_calls`, `max_tokens` -> `length`
- `prompt_tokens` = `usage.input_tokens`
- `completion_tokens` = `usage.output_tokens`
- `tool_calls` = `content[]` where `type == "tool_use"`, remap `input` -> `arguments`
- `thinking_content` = join all `content[].thinking` where `type == "thinking"`

**Family 3 — Google Gemini**:
- `content` = join all `candidates[0].content.parts[].text` where `thought` is not true
- `finish_reason` = map `finishReason`: `STOP` -> `stop`, `MAX_TOKENS` -> `length`, `SAFETY` -> `content_filter`
- `prompt_tokens` = `usageMetadata.promptTokenCount`
- `completion_tokens` = `usageMetadata.candidatesTokenCount`
- `tool_calls` = `parts[].functionCall`, remap `name` + `args` -> canonical format
- `thinking_content` = join `parts[].text` where `thought == true`

**Family 4 — Cohere**:
- `content` = `message.content[0].text`
- `finish_reason` = map: `COMPLETE` -> `stop`, `MAX_TOKENS` -> `length`, `TOOL_CALL` -> `tool_calls`
- `prompt_tokens` = `usage.tokens.input_tokens`
- `completion_tokens` = `usage.tokens.output_tokens`
- `tool_calls` = `message.tool_calls` (same structure as OpenAI)
- `thinking_content` = `message.tool_plan`

**Family 5 — Ollama-native**:
- `content` = `message.content`
- `finish_reason` = map `done_reason`: `stop` -> `stop`, `length` -> `length`
- `prompt_tokens` = `prompt_eval_count`
- `completion_tokens` = `eval_count`
- `tool_calls` = `message.tool_calls`, remap `function.arguments` (dict -> canonical)

**Family 6 — HuggingFace TGI-native**:
- `content` = `generated_text`
- `finish_reason` = map `details.finish_reason`: `eos_token` -> `stop`, `length` -> `length`, `stop_sequence` -> `stop`
- `prompt_tokens` = null (not provided)
- `completion_tokens` = `details.generated_tokens`

**Family 7 — AWS Titan**:
- `content` = `results[0].outputText`
- `finish_reason` = map `completionReason`: `FINISHED` -> `stop`, `LENGTH` -> `length`, `CONTENT_FILTERED` -> `content_filter`
- `prompt_tokens` = `inputTextTokenCount`
- `completion_tokens` = `results[0].tokenCount`

**Family 8 — Replicate**:
- `content` = `output` (type varies)
- `finish_reason` = map `status`: `succeeded` -> `stop`, `failed` -> `error`, `canceled` -> `stop`
- `prompt_tokens` = null
- `completion_tokens` = null

**Family 9 — OpenAI Responses API**:
- `content` = `output[].content[].text` where `output[].type == "message"`
- `finish_reason` = map `status`: `completed` -> `stop`, `incomplete` -> `length`, `failed` -> `error`
- `prompt_tokens` = `usage.input_tokens`
- `completion_tokens` = `usage.output_tokens`
- `tool_calls` = `output[]` where `type == "function_call"`, remap fields
- `thinking_content` = `output[].summary[].text` where `output[].type == "reasoning"`

---

## Detection Heuristics

For an ONNX classifier that identifies which provider format a response uses:

| Signal | Provider/Family |
|---|---|
| `choices[]` + `usage.prompt_tokens` | OpenAI-family |
| `choices[]` + `time_info` | Cerebras |
| `choices[]` + `x_groq` | Groq |
| `choices[]` + `citations[]` top-level | Perplexity |
| `choices[]` + `message.reasoning_content` | DeepSeek |
| `choices[]` + `message.graph_data` | Writer |
| `choices[]` + `content_filter_results` | Azure OpenAI |
| `choices[]` + `message.prefix` | Mistral |
| `content[]` array + `stop_reason` + `usage.input_tokens` | Anthropic |
| `candidates[]` + `usageMetadata` | Google Gemini |
| `message` + `done` + `eval_count` | Ollama-native |
| `message` + `finish_reason` (top-level, SCREAMING_CASE) | Cohere v2 |
| `results[]` + `inputTextTokenCount` | AWS Titan |
| `generated_text` + `details` | HuggingFace TGI |
| `output` + `status` + `metrics` + `version` | Replicate |
| `output[]` + `status` + `usage.input_tokens` | OpenAI Responses API |

### Feature Vector for ONNX Model

Binary features to extract from any JSON response for classification:

```
has_choices: bool              # choices[] exists
has_candidates: bool           # candidates[] exists
has_content_array: bool        # top-level content[] array
has_results_array: bool        # results[] exists
has_output_field: bool         # output field exists (Replicate or Responses API)
has_output_array: bool         # output[] is an array (Responses API)
has_message_singular: bool     # singular message object (Cohere/Ollama)
has_generated_text: bool       # generated_text field
has_usage_object: bool         # usage{} exists
has_usage_metadata: bool       # usageMetadata{} exists
has_prompt_tokens: bool        # usage.prompt_tokens
has_input_tokens: bool         # usage.input_tokens
has_prompt_token_count: bool   # usageMetadata.promptTokenCount
has_eval_count: bool           # prompt_eval_count/eval_count
has_input_text_token_count: bool  # inputTextTokenCount
has_stop_reason: bool          # stop_reason field (Anthropic)
has_finish_reason: bool        # finish_reason field
has_done_reason: bool          # done_reason field (Ollama)
has_completion_reason: bool    # completionReason field (Titan)
has_status_field: bool         # status field (Replicate/Responses)
has_tool_calls_in_message: bool   # message.tool_calls
has_tool_use_in_content: bool     # content block with type=tool_use
has_function_call_in_parts: bool  # parts[].functionCall
has_time_info: bool            # time_info object (Cerebras)
has_x_groq: bool               # x_groq object
has_citations: bool            # citations field
has_safety_ratings: bool       # safetyRatings field
has_reasoning_content: bool    # message.reasoning_content (DeepSeek)
has_thinking_block: bool       # content block with type=thinking (Anthropic)
has_thought_part: bool         # parts with thought=true (Gemini)
has_graph_data: bool           # message.graph_data (Writer)
has_metrics_predict: bool      # metrics.predict_time (Replicate)
finish_reason_case: enum       # lowercase / SCREAMING_CASE / camelCase
```
