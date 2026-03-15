package handler

import (
	"encoding/json"
	"testing"
)

func TestResolveProvider(t *testing.T) {
	tests := []struct {
		path         string
		wantProvider string
		wantForward  string
	}{
		{"/v1/chat/completions", "openai", "/v1/chat/completions"},
		{"/v1/completions", "openai", "/v1/completions"},
		{"/v1/embeddings", "openai", "/v1/embeddings"},
		{"/v1/messages", "anthropic", "/v1/messages"},
		{"/api/chat", "ollama", "/api/chat"},
		{"/api/generate", "ollama", "/api/generate"},
		{"/api/tags", "ollama", "/api/tags"},
		{"/unknown/path", "openai", "/unknown/path"},
	}

	for _, tt := range tests {
		t.Run(tt.path, func(t *testing.T) {
			provider, forward := resolveProvider(tt.path)
			if provider != tt.wantProvider {
				t.Errorf("resolveProvider(%q) provider = %q, want %q", tt.path, provider, tt.wantProvider)
			}
			if forward != tt.wantForward {
				t.Errorf("resolveProvider(%q) forward = %q, want %q", tt.path, forward, tt.wantForward)
			}
		})
	}
}

func TestParseRequestBody(t *testing.T) {
	tests := []struct {
		name       string
		body       string
		wantModel  string
		wantPrompt string
		wantStream bool
	}{
		{
			name:       "openai chat format",
			body:       `{"model":"gpt-4","messages":[{"role":"user","content":"hello"}],"stream":false}`,
			wantModel:  "gpt-4",
			wantPrompt: "hello",
			wantStream: false,
		},
		{
			name:       "streaming enabled",
			body:       `{"model":"gpt-4","messages":[{"role":"user","content":"hello"}],"stream":true}`,
			wantModel:  "gpt-4",
			wantPrompt: "hello",
			wantStream: true,
		},
		{
			name:       "ollama generate format",
			body:       `{"model":"qwen3:4b","prompt":"test prompt"}`,
			wantModel:  "qwen3:4b",
			wantPrompt: "test prompt",
			wantStream: false,
		},
		{
			name:       "multiple messages picks last user",
			body:       `{"model":"gpt-4","messages":[{"role":"system","content":"sys"},{"role":"user","content":"first"},{"role":"assistant","content":"resp"},{"role":"user","content":"second"}]}`,
			wantModel:  "gpt-4",
			wantPrompt: "second",
			wantStream: false,
		},
		{
			name:       "stream not set defaults to false",
			body:       `{"model":"test","messages":[{"role":"user","content":"hi"}]}`,
			wantModel:  "test",
			wantPrompt: "hi",
			wantStream: false,
		},
		{
			name:       "invalid json",
			body:       `not json`,
			wantModel:  "",
			wantPrompt: "",
			wantStream: false,
		},
		{
			name:       "empty body",
			body:       `{}`,
			wantModel:  "",
			wantPrompt: "",
			wantStream: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			model, prompt, stream := parseRequestBody([]byte(tt.body))
			if model != tt.wantModel {
				t.Errorf("model = %q, want %q", model, tt.wantModel)
			}
			if prompt != tt.wantPrompt {
				t.Errorf("prompt = %q, want %q", prompt, tt.wantPrompt)
			}
			if stream != tt.wantStream {
				t.Errorf("stream = %v, want %v", stream, tt.wantStream)
			}
		})
	}
}

func TestExtractOpenAIStreamChunk(t *testing.T) {
	tests := []struct {
		name        string
		data        string
		wantContent string
		wantPT      int32
		wantCT      int32
	}{
		{
			name:        "content delta",
			data:        `{"choices":[{"delta":{"content":"Hello"}}]}`,
			wantContent: "Hello",
		},
		{
			name:   "usage chunk",
			data:   `{"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20}}`,
			wantPT: 10,
			wantCT: 20,
		},
		{
			name: "invalid json",
			data: `not json`,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			content, pt, ct := extractOpenAIStreamChunk(tt.data)
			if content != tt.wantContent {
				t.Errorf("content = %q, want %q", content, tt.wantContent)
			}
			if pt != tt.wantPT {
				t.Errorf("promptTokens = %d, want %d", pt, tt.wantPT)
			}
			if ct != tt.wantCT {
				t.Errorf("completionTokens = %d, want %d", ct, tt.wantCT)
			}
		})
	}
}

func TestExtractOllamaStreamChunk(t *testing.T) {
	tests := []struct {
		name        string
		data        string
		wantContent string
	}{
		{
			name:        "message format",
			data:        `{"message":{"content":"Hello"}}`,
			wantContent: "Hello",
		},
		{
			name:        "response format",
			data:        `{"response":"World"}`,
			wantContent: "World",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			content, _, _ := extractOllamaStreamChunk(tt.data)
			if content != tt.wantContent {
				t.Errorf("content = %q, want %q", content, tt.wantContent)
			}
		})
	}
}

func TestExtractAnthropicStreamChunk(t *testing.T) {
	data := `{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}`
	content, _, _ := extractAnthropicStreamChunk(data)
	if content != "Hi" {
		t.Errorf("content = %q, want Hi", content)
	}
}

func TestExtractResponseContent(t *testing.T) {
	t.Run("openai", func(t *testing.T) {
		body := `{"choices":[{"message":{"content":"response text"}}],"usage":{"prompt_tokens":5,"completion_tokens":10}}`
		content, _, pt, ct := extractResponseContent([]byte(body), "openai")
		if content != "response text" {
			t.Errorf("content = %q, want 'response text'", content)
		}
		if pt != 5 {
			t.Errorf("promptTokens = %d, want 5", pt)
		}
		if ct != 10 {
			t.Errorf("completionTokens = %d, want 10", ct)
		}
	})

	t.Run("ollama", func(t *testing.T) {
		body := `{"message":{"content":"ollama response","reasoning":"thinking"},"prompt_eval_count":3,"eval_count":7}`
		content, thinking, pt, ct := extractResponseContent([]byte(body), "ollama")
		if content != "ollama response" {
			t.Errorf("content = %q, want 'ollama response'", content)
		}
		if thinking != "thinking" {
			t.Errorf("thinking = %q, want 'thinking'", thinking)
		}
		if pt != 3 {
			t.Errorf("promptTokens = %d, want 3", pt)
		}
		if ct != 7 {
			t.Errorf("completionTokens = %d, want 7", ct)
		}
	})

	t.Run("anthropic", func(t *testing.T) {
		body := `{"content":[{"type":"text","text":"anthropic response"}],"usage":{"input_tokens":4,"output_tokens":8}}`
		content, _, pt, ct := extractResponseContent([]byte(body), "anthropic")
		if content != "anthropic response" {
			t.Errorf("content = %q, want 'anthropic response'", content)
		}
		if pt != 4 {
			t.Errorf("promptTokens = %d, want 4", pt)
		}
		if ct != 8 {
			t.Errorf("completionTokens = %d, want 8", ct)
		}
	})
}

func TestReplaceModelInBody(t *testing.T) {
	body := `{"model":"old-model","messages":[]}`
	result := replaceModelInBody([]byte(body), "new-model")

	var parsed map[string]json.RawMessage
	if err := json.Unmarshal(result, &parsed); err != nil {
		t.Fatalf("failed to parse result: %v", err)
	}

	var model string
	json.Unmarshal(parsed["model"], &model)
	if model != "new-model" {
		t.Errorf("model = %q, want 'new-model'", model)
	}
}

func TestReplacePomptInBody(t *testing.T) {
	t.Run("messages format", func(t *testing.T) {
		body := `{"messages":[{"role":"user","content":"original prompt"}]}`
		result := replacePomptInBody([]byte(body), "sanitized prompt")

		var parsed struct {
			Messages []struct {
				Content string `json:"content"`
			} `json:"messages"`
		}
		if err := json.Unmarshal(result, &parsed); err != nil {
			t.Fatalf("failed to parse result: %v", err)
		}
		if len(parsed.Messages) == 0 {
			t.Fatal("no messages in result")
		}
		if parsed.Messages[0].Content != "sanitized prompt" {
			t.Errorf("content = %q, want 'sanitized prompt'", parsed.Messages[0].Content)
		}
	})

	t.Run("prompt format", func(t *testing.T) {
		body := `{"prompt":"original"}`
		result := replacePomptInBody([]byte(body), "sanitized")

		var parsed struct {
			Prompt string `json:"prompt"`
		}
		if err := json.Unmarshal(result, &parsed); err != nil {
			t.Fatalf("failed to parse result: %v", err)
		}
		if parsed.Prompt != "sanitized" {
			t.Errorf("prompt = %q, want 'sanitized'", parsed.Prompt)
		}
	})
}

func TestReplaceContentInResponse(t *testing.T) {
	t.Run("openai", func(t *testing.T) {
		body := `{"choices":[{"message":{"content":"original"}}]}`
		result := replaceContentInResponse([]byte(body), "restored", "openai")
		content, _, _, _ := extractOpenAIResponse(result)
		if content != "restored" {
			t.Errorf("content = %q, want 'restored'", content)
		}
	})

	t.Run("ollama message", func(t *testing.T) {
		body := `{"message":{"content":"original"}}`
		result := replaceContentInResponse([]byte(body), "restored", "ollama")
		content, _, _, _ := extractOllamaResponse(result)
		if content != "restored" {
			t.Errorf("content = %q, want 'restored'", content)
		}
	})

	t.Run("anthropic", func(t *testing.T) {
		body := `{"content":[{"type":"text","text":"original"}]}`
		result := replaceContentInResponse([]byte(body), "restored", "anthropic")
		content, _, _, _ := extractAnthropicResponse(result)
		if content != "restored" {
			t.Errorf("content = %q, want 'restored'", content)
		}
	})
}
