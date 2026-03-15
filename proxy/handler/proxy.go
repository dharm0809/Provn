// Package handler implements the main proxy handler that forwards requests
// to LLM providers with governance checks via the Python sidecar.
package handler

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"walacor-gateway-proxy/brain"
	pb "walacor-gateway-proxy/pb"
)

// Proxy is the main HTTP handler that forwards LLM requests through the
// governance pipeline: pre-inference check -> cache check -> LLM forward ->
// post-inference check -> record execution.
type Proxy struct {
	brain        *brain.Client
	providers    map[string]string // provider name -> base URL
	client       *http.Client     // reusable HTTP client with connection pooling
	cacheEnabled bool
	forwardTimeout time.Duration
}

// NewProxy creates a proxy handler with the given configuration.
func NewProxy(brainClient *brain.Client, providers map[string]string, cacheEnabled bool, forwardTimeoutSec int) *Proxy {
	if forwardTimeoutSec <= 0 {
		forwardTimeoutSec = 30
	}

	transport := &http.Transport{
		MaxIdleConns:        100,
		MaxIdleConnsPerHost: 20,
		IdleConnTimeout:     90 * time.Second,
	}

	return &Proxy{
		brain:        brainClient,
		providers:    providers,
		cacheEnabled: cacheEnabled,
		forwardTimeout: time.Duration(forwardTimeoutSec) * time.Second,
		client: &http.Client{
			Transport: transport,
			// No global timeout — streaming responses can run long.
			// Per-request timeouts are set via context for non-streaming.
		},
	}
}

// ServeHTTP implements http.Handler.
func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, `{"error":"method_not_allowed","message":"only POST is supported"}`, http.StatusMethodNotAllowed)
		return
	}

	// Read and limit request body.
	body, err := io.ReadAll(io.LimitReader(r.Body, 10*1024*1024))
	if err != nil {
		http.Error(w, `{"error":"bad_request","message":"failed to read request body"}`, http.StatusBadRequest)
		return
	}
	defer r.Body.Close()

	// Parse model_id, prompt, and stream flag from the request body.
	modelID, promptText, isStream := parseRequestBody(body)

	// Resolve provider from URL path.
	provider, providerPath := resolveProvider(r.URL.Path)
	baseURL, ok := p.providers[provider]
	if !ok {
		slog.Warn("unknown provider", "provider", provider, "path", r.URL.Path)
		http.Error(w, fmt.Sprintf(`{"error":"bad_request","message":"unknown provider %q for path %s"}`, provider, r.URL.Path), http.StatusBadRequest)
		return
	}
	providerURL := strings.TrimRight(baseURL, "/") + providerPath

	// Extract API key for sidecar auth context.
	apiKey := extractAPIKey(r)

	// --- Pre-inference governance check ---
	preResult, err := p.brain.EvaluatePreInference(r.Context(), &pb.PreInferenceRequest{
		ApiKey:    apiKey,
		ModelId:   modelID,
		Provider:  provider,
		PromptText: promptText,
		SessionId: r.Header.Get("X-Session-Id"),
		UserId:    r.Header.Get("X-User-Id"),
		TenantId:  r.Header.Get("X-Tenant-Id"),
		Metadata:  extractMetadata(r),
	})
	if err != nil {
		// Fail-open: if sidecar is unreachable, forward to LLM anyway.
		slog.Error("pre-inference check failed, forwarding anyway (fail-open)",
			"error", err, "model", modelID, "provider", provider)
		preResult = &pb.PreInferenceResult{
			Allowed:      true,
			PolicyResult: "bypass",
		}
	}

	if !preResult.Allowed {
		statusCode := int(preResult.DenialStatusCode)
		if statusCode == 0 {
			statusCode = http.StatusForbidden
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(statusCode)
		resp := map[string]string{
			"error":   "request_denied",
			"message": preResult.DenialReason,
		}
		json.NewEncoder(w).Encode(resp)
		return
	}

	// Use sanitized prompt if PII sanitization is active.
	if preResult.SanitizedPrompt != nil {
		body = replacePomptInBody(body, *preResult.SanitizedPrompt)
	}

	// Use rewritten model if A/B test changed it.
	if preResult.ModelId != "" && preResult.ModelId != modelID {
		body = replaceModelInBody(body, preResult.ModelId)
		modelID = preResult.ModelId
	}

	// --- Cache check ---
	if p.cacheEnabled {
		cacheResp, cacheErr := p.brain.CacheGet(r.Context(), &pb.CacheKey{
			PromptText: promptText,
			ModelId:    modelID,
		})
		if cacheErr == nil && cacheResp.Hit {
			slog.Info("cache hit", "model", modelID)
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("X-Cache", "HIT")
			w.Write([]byte(cacheResp.ResponseBody))
			return
		}
	}

	// --- Forward to LLM provider ---
	start := time.Now()

	if isStream {
		err = p.handleStreaming(w, r, providerURL, body, preResult, modelID, provider, promptText, start)
	} else {
		err = p.handleNonStreaming(w, r, providerURL, body, preResult, modelID, provider, promptText, start)
	}
	if err != nil {
		slog.Error("forward error", "error", err, "provider", provider, "model", modelID)
		// Response already started for streaming; for non-streaming the handler writes the error.
	}
}

// resolveProvider maps a URL path to a provider name and the path to forward.
// Returns (provider, forwardPath).
func resolveProvider(path string) (string, string) {
	switch {
	case strings.HasPrefix(path, "/v1/chat/completions"):
		return "openai", path
	case strings.HasPrefix(path, "/v1/completions"):
		return "openai", path
	case strings.HasPrefix(path, "/v1/embeddings"):
		return "openai", path
	case strings.HasPrefix(path, "/v1/messages"):
		return "anthropic", path
	case strings.HasPrefix(path, "/api/chat"):
		return "ollama", path
	case strings.HasPrefix(path, "/api/generate"):
		return "ollama", path
	case strings.HasPrefix(path, "/api/tags"):
		return "ollama", path
	default:
		// Default to openai-compatible for unknown paths.
		return "openai", path
	}
}

// parseRequestBody extracts model_id, prompt text, and stream flag from a JSON body.
func parseRequestBody(body []byte) (modelID, promptText string, isStream bool) {
	var parsed struct {
		Model    string `json:"model"`
		Stream   *bool  `json:"stream"`
		Messages []struct {
			Role    string `json:"role"`
			Content string `json:"content"`
		} `json:"messages"`
		Prompt string `json:"prompt"`
	}

	if err := json.Unmarshal(body, &parsed); err != nil {
		return "", "", false
	}

	modelID = parsed.Model

	// Extract prompt: prefer last user message, fall back to prompt field.
	if len(parsed.Messages) > 0 {
		for i := len(parsed.Messages) - 1; i >= 0; i-- {
			if parsed.Messages[i].Role == "user" {
				promptText = parsed.Messages[i].Content
				break
			}
		}
		// If no user message found, take the last message.
		if promptText == "" {
			promptText = parsed.Messages[len(parsed.Messages)-1].Content
		}
	} else if parsed.Prompt != "" {
		promptText = parsed.Prompt
	}

	if parsed.Stream != nil {
		isStream = *parsed.Stream
	}

	return modelID, promptText, isStream
}

// extractAPIKey gets the API key from standard auth headers.
func extractAPIKey(r *http.Request) string {
	if auth := r.Header.Get("Authorization"); auth != "" {
		if strings.HasPrefix(auth, "Bearer ") {
			return strings.TrimPrefix(auth, "Bearer ")
		}
	}
	return r.Header.Get("X-API-Key")
}

// extractMetadata collects request metadata from headers.
func extractMetadata(r *http.Request) map[string]string {
	meta := make(map[string]string)

	pairs := [][2]string{
		{"user_agent", r.Header.Get("User-Agent")},
		{"x_request_id", r.Header.Get("X-Request-Id")},
		{"x_session_id", r.Header.Get("X-Session-Id")},
		{"x_user_roles", r.Header.Get("X-User-Roles")},
		{"x_team_id", r.Header.Get("X-Team-Id")},
	}
	for _, p := range pairs {
		if p[1] != "" {
			meta[p[0]] = p[1]
		}
	}
	return meta
}

// replacePomptInBody replaces the prompt text in a JSON body with sanitized text.
func replacePomptInBody(body []byte, sanitized string) []byte {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		return body
	}

	// Try to replace in messages array.
	if messagesRaw, ok := raw["messages"]; ok {
		var messages []map[string]json.RawMessage
		if err := json.Unmarshal(messagesRaw, &messages); err == nil {
			for i := len(messages) - 1; i >= 0; i-- {
				var role string
				json.Unmarshal(messages[i]["role"], &role)
				if role == "user" {
					b, _ := json.Marshal(sanitized)
					messages[i]["content"] = b
					break
				}
			}
			b, _ := json.Marshal(messages)
			raw["messages"] = b
		}
	}

	// Try to replace prompt field.
	if _, ok := raw["prompt"]; ok {
		b, _ := json.Marshal(sanitized)
		raw["prompt"] = b
	}

	result, err := json.Marshal(raw)
	if err != nil {
		return body
	}
	return result
}

// replaceModelInBody replaces the model field in a JSON body.
func replaceModelInBody(body []byte, newModel string) []byte {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		return body
	}
	b, _ := json.Marshal(newModel)
	raw["model"] = b
	result, err := json.Marshal(raw)
	if err != nil {
		return body
	}
	return result
}
