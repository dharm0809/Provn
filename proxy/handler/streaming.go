package handler

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	pb "walacor-gateway-proxy/pb"
)

// handleStreaming forwards a streaming (SSE) request to the LLM provider,
// relays chunks to the client in real-time, accumulates the full response
// for post-inference analysis, and fires a goroutine for record execution.
func (p *Proxy) handleStreaming(
	w http.ResponseWriter,
	r *http.Request,
	providerURL string,
	body []byte,
	preResult *pb.PreInferenceResult,
	modelID, provider, promptText string,
	start time.Time,
) error {
	// Build upstream request — no context timeout for streaming.
	upstream, err := http.NewRequestWithContext(r.Context(), http.MethodPost, providerURL, bytes.NewReader(body))
	if err != nil {
		http.Error(w, `{"error":"internal","message":"failed to build upstream request"}`, http.StatusInternalServerError)
		return fmt.Errorf("build upstream request: %w", err)
	}
	copyHeaders(r.Header, upstream.Header)

	resp, err := p.client.Do(upstream)
	if err != nil {
		http.Error(w, `{"error":"provider_error","message":"failed to reach LLM provider"}`, http.StatusBadGateway)
		return fmt.Errorf("upstream request: %w", err)
	}
	defer resp.Body.Close()

	// If the provider returned an error, forward it as-is.
	if resp.StatusCode >= 400 {
		w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
		w.WriteHeader(resp.StatusCode)
		io.Copy(w, resp.Body)
		return nil
	}

	// Set SSE headers.
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)

	flusher, ok := w.(http.Flusher)
	if !ok {
		return fmt.Errorf("response writer does not support flushing")
	}

	// Accumulate response content for post-inference analysis.
	var contentBuf strings.Builder
	var promptTokens, completionTokens int32

	scanner := bufio.NewScanner(resp.Body)
	// Increase scanner buffer for large chunks.
	scanner.Buffer(make([]byte, 64*1024), 256*1024)

	for scanner.Scan() {
		line := scanner.Text()

		// Relay the line to the client immediately.
		fmt.Fprintf(w, "%s\n", line)
		flusher.Flush()

		// Parse SSE data lines to accumulate content.
		if strings.HasPrefix(line, "data: ") {
			data := strings.TrimPrefix(line, "data: ")
			if data == "[DONE]" {
				continue
			}
			content, pt, ct := extractStreamChunk(data, provider)
			contentBuf.WriteString(content)
			if pt > 0 {
				promptTokens = pt
			}
			if ct > 0 {
				completionTokens = ct
			}
		}
	}

	if err := scanner.Err(); err != nil {
		slog.Warn("stream scanner error", "error", err)
	}

	latencyMs := float64(time.Since(start).Milliseconds())
	fullContent := contentBuf.String()

	// Fire-and-forget: post-inference + record execution in a goroutine.
	go p.postInferenceAndRecord(
		modelID, provider, promptText, fullContent, "",
		preResult, latencyMs, promptTokens, completionTokens,
		r,
	)

	return nil
}

// extractStreamChunk parses a single SSE data chunk and returns the content delta
// and token usage if present in this chunk.
func extractStreamChunk(data, provider string) (content string, promptTokens, completionTokens int32) {
	switch provider {
	case "ollama":
		return extractOllamaStreamChunk(data)
	case "anthropic":
		return extractAnthropicStreamChunk(data)
	default:
		// OpenAI-compatible format.
		return extractOpenAIStreamChunk(data)
	}
}

// extractOpenAIStreamChunk handles OpenAI-format streaming chunks.
func extractOpenAIStreamChunk(data string) (string, int32, int32) {
	var chunk struct {
		Choices []struct {
			Delta struct {
				Content string `json:"content"`
			} `json:"delta"`
		} `json:"choices"`
		Usage *struct {
			PromptTokens     int32 `json:"prompt_tokens"`
			CompletionTokens int32 `json:"completion_tokens"`
		} `json:"usage"`
	}

	if err := json.Unmarshal([]byte(data), &chunk); err != nil {
		return "", 0, 0
	}

	var content string
	if len(chunk.Choices) > 0 {
		content = chunk.Choices[0].Delta.Content
	}

	var pt, ct int32
	if chunk.Usage != nil {
		pt = chunk.Usage.PromptTokens
		ct = chunk.Usage.CompletionTokens
	}
	return content, pt, ct
}

// extractOllamaStreamChunk handles Ollama-format streaming chunks.
func extractOllamaStreamChunk(data string) (string, int32, int32) {
	var chunk struct {
		Message *struct {
			Content string `json:"content"`
		} `json:"message"`
		Response string `json:"response"`
		Done     bool   `json:"done"`
		PromptEvalCount   int32 `json:"prompt_eval_count"`
		EvalCount         int32 `json:"eval_count"`
	}

	if err := json.Unmarshal([]byte(data), &chunk); err != nil {
		return "", 0, 0
	}

	var content string
	if chunk.Message != nil {
		content = chunk.Message.Content
	} else {
		content = chunk.Response
	}

	return content, chunk.PromptEvalCount, chunk.EvalCount
}

// extractAnthropicStreamChunk handles Anthropic-format streaming chunks.
func extractAnthropicStreamChunk(data string) (string, int32, int32) {
	var chunk struct {
		Type  string `json:"type"`
		Delta *struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"delta"`
		Usage *struct {
			InputTokens  int32 `json:"input_tokens"`
			OutputTokens int32 `json:"output_tokens"`
		} `json:"usage"`
	}

	if err := json.Unmarshal([]byte(data), &chunk); err != nil {
		return "", 0, 0
	}

	var content string
	if chunk.Delta != nil {
		content = chunk.Delta.Text
	}

	var pt, ct int32
	if chunk.Usage != nil {
		pt = chunk.Usage.InputTokens
		ct = chunk.Usage.OutputTokens
	}
	return content, pt, ct
}

// postInferenceAndRecord runs post-inference evaluation and records the execution.
// It runs in a goroutine and recovers from panics.
func (p *Proxy) postInferenceAndRecord(
	modelID, provider, promptText, content, thinkingContent string,
	preResult *pb.PreInferenceResult,
	latencyMs float64,
	promptTokens, completionTokens int32,
	r *http.Request,
) {
	defer func() {
		if rv := recover(); rv != nil {
			slog.Error("panic in post-inference goroutine", "recover", rv)
		}
	}()

	// Use a detached context — the request context may be cancelled.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	// --- Post-inference evaluation ---
	postResult, err := p.brain.EvaluatePostInference(ctx, &pb.PostInferenceRequest{
		Content:          content,
		ThinkingContent:  thinkingContent,
		ModelId:          modelID,
		Provider:         provider,
		PromptTokens:     promptTokens,
		CompletionTokens: completionTokens,
		LatencyMs:        latencyMs,
		SessionId:        r.Header.Get("X-Session-Id"),
	})
	if err != nil {
		slog.Error("post-inference evaluation failed", "error", err)
	}

	policyResult := "pass"
	policyVersion := preResult.PolicyVersion
	if postResult != nil {
		policyResult = postResult.PolicyResult
	}

	// --- Record execution ---
	_, err = p.brain.RecordExecution(ctx, &pb.ExecutionRecord{
		ModelId:          modelID,
		Provider:         provider,
		PromptText:       promptText,
		ResponseContent:  content,
		ThinkingContent:  thinkingContent,
		AttestationId:    preResult.AttestationId,
		PolicyVersion:    policyVersion,
		PolicyResult:     policyResult,
		LatencyMs:        latencyMs,
		PromptTokens:     promptTokens,
		CompletionTokens: completionTokens,
		TotalTokens:      promptTokens + completionTokens,
		SessionId:        r.Header.Get("X-Session-Id"),
		User:             r.Header.Get("X-User-Id"),
		TenantId:         r.Header.Get("X-Tenant-Id"),
	})
	if err != nil {
		slog.Error("record execution failed", "error", err)
	}

	// --- Cache put (if enabled and response allowed) ---
	if p.cacheEnabled && content != "" && (postResult == nil || !postResult.Blocked) {
		_, cacheErr := p.brain.CachePut(ctx, &pb.CachePutRequest{
			PromptText:   promptText,
			ModelId:      modelID,
			ResponseBody: content,
		})
		if cacheErr != nil {
			slog.Debug("cache put failed", "error", cacheErr)
		}
	}
}

// copyHeaders copies relevant headers from the client request to the upstream request.
func copyHeaders(src, dst http.Header) {
	// Forward standard headers to the LLM provider.
	forward := []string{
		"Content-Type",
		"Authorization",
		"X-API-Key",
		"X-Request-Id",
		"X-Session-Id",
		"Accept",
		"User-Agent",
	}
	for _, h := range forward {
		if v := src.Get(h); v != "" {
			dst.Set(h, v)
		}
	}
}
