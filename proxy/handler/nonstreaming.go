package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"

	pb "walacor-gateway-proxy/pb"
)

// handleNonStreaming forwards a non-streaming request to the LLM provider,
// reads the full response, runs post-inference evaluation, records the execution,
// and returns the response to the client.
func (p *Proxy) handleNonStreaming(
	w http.ResponseWriter,
	r *http.Request,
	providerURL string,
	body []byte,
	preResult *pb.PreInferenceResult,
	modelID, provider, promptText string,
	start time.Time,
) error {
	// Apply forward timeout for non-streaming requests.
	ctx, cancel := context.WithTimeout(r.Context(), p.forwardTimeout)
	defer cancel()

	upstream, err := http.NewRequestWithContext(ctx, http.MethodPost, providerURL, bytes.NewReader(body))
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

	respBody, err := io.ReadAll(io.LimitReader(resp.Body, 50*1024*1024))
	if err != nil {
		http.Error(w, `{"error":"internal","message":"failed to read provider response"}`, http.StatusInternalServerError)
		return fmt.Errorf("read response: %w", err)
	}

	latencyMs := float64(time.Since(start).Milliseconds())

	// If provider returned an error, forward it and still record.
	if resp.StatusCode >= 400 {
		w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
		w.WriteHeader(resp.StatusCode)
		w.Write(respBody)

		// Still record the failed execution.
		go p.postInferenceAndRecord(
			modelID, provider, promptText, string(respBody), "",
			preResult, latencyMs, 0, 0, r,
		)
		return nil
	}

	// Extract content and token counts from the response.
	content, thinkingContent, promptTokens, completionTokens := extractResponseContent(respBody, provider)

	// --- Post-inference evaluation (synchronous for non-streaming) ---
	postResult, postErr := p.brain.EvaluatePostInference(r.Context(), &pb.PostInferenceRequest{
		Content:          content,
		ThinkingContent:  thinkingContent,
		ModelId:          modelID,
		Provider:         provider,
		PromptTokens:     promptTokens,
		CompletionTokens: completionTokens,
		LatencyMs:        latencyMs,
		SessionId:        r.Header.Get("X-Session-Id"),
	})
	if postErr != nil {
		slog.Error("post-inference evaluation failed (forwarding response anyway)", "error", postErr)
	}

	// Check if response should be blocked.
	if postResult != nil && postResult.Blocked {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		blockResp := map[string]string{
			"error":   "response_blocked",
			"message": postResult.BlockReason,
		}
		json.NewEncoder(w).Encode(blockResp)

		// Record the blocked execution.
		go p.recordExecution(
			modelID, provider, promptText, content, thinkingContent,
			preResult, "block", latencyMs, promptTokens, completionTokens, r,
		)
		return nil
	}

	// Use PII-restored content if available.
	if postResult != nil && postResult.RestoredContent != nil {
		respBody = replaceContentInResponse(respBody, *postResult.RestoredContent, provider)
	}

	// Forward the provider response to the client.
	for _, h := range []string{"Content-Type", "X-Request-Id"} {
		if v := resp.Header.Get(h); v != "" {
			w.Header().Set(h, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	w.Write(respBody)

	// Fire-and-forget: record execution.
	policyResult := "pass"
	if postResult != nil {
		policyResult = postResult.PolicyResult
	}
	go p.recordExecution(
		modelID, provider, promptText, content, thinkingContent,
		preResult, policyResult, latencyMs, promptTokens, completionTokens, r,
	)

	return nil
}

// recordExecution persists an audit record via the sidecar.
// Runs in a goroutine; recovers from panics.
func (p *Proxy) recordExecution(
	modelID, provider, promptText, content, thinkingContent string,
	preResult *pb.PreInferenceResult,
	policyResult string,
	latencyMs float64,
	promptTokens, completionTokens int32,
	r *http.Request,
) {
	defer func() {
		if rv := recover(); rv != nil {
			slog.Error("panic in record execution goroutine", "recover", rv)
		}
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	_, err := p.brain.RecordExecution(ctx, &pb.ExecutionRecord{
		ModelId:          modelID,
		Provider:         provider,
		PromptText:       promptText,
		ResponseContent:  content,
		ThinkingContent:  thinkingContent,
		AttestationId:    preResult.AttestationId,
		PolicyVersion:    preResult.PolicyVersion,
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

	// Cache put if enabled.
	if p.cacheEnabled && content != "" {
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

// extractResponseContent parses the LLM provider response to extract content,
// thinking content, and token usage.
func extractResponseContent(body []byte, provider string) (content, thinkingContent string, promptTokens, completionTokens int32) {
	switch provider {
	case "anthropic":
		return extractAnthropicResponse(body)
	case "ollama":
		return extractOllamaResponse(body)
	default:
		return extractOpenAIResponse(body)
	}
}

// extractOpenAIResponse parses an OpenAI-format response.
func extractOpenAIResponse(body []byte) (string, string, int32, int32) {
	var resp struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
		Usage *struct {
			PromptTokens     int32 `json:"prompt_tokens"`
			CompletionTokens int32 `json:"completion_tokens"`
		} `json:"usage"`
	}

	if err := json.Unmarshal(body, &resp); err != nil {
		return "", "", 0, 0
	}

	var content string
	if len(resp.Choices) > 0 {
		content = resp.Choices[0].Message.Content
	}

	var pt, ct int32
	if resp.Usage != nil {
		pt = resp.Usage.PromptTokens
		ct = resp.Usage.CompletionTokens
	}
	return content, "", pt, ct
}

// extractOllamaResponse parses an Ollama-format response.
func extractOllamaResponse(body []byte) (string, string, int32, int32) {
	var resp struct {
		Message *struct {
			Content   string `json:"content"`
			Reasoning string `json:"reasoning"`
		} `json:"message"`
		Response        string `json:"response"`
		PromptEvalCount int32  `json:"prompt_eval_count"`
		EvalCount       int32  `json:"eval_count"`
	}

	if err := json.Unmarshal(body, &resp); err != nil {
		return "", "", 0, 0
	}

	var content, thinking string
	if resp.Message != nil {
		content = resp.Message.Content
		thinking = resp.Message.Reasoning
	} else {
		content = resp.Response
	}

	return content, thinking, resp.PromptEvalCount, resp.EvalCount
}

// extractAnthropicResponse parses an Anthropic-format response.
func extractAnthropicResponse(body []byte) (string, string, int32, int32) {
	var resp struct {
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
		Usage *struct {
			InputTokens  int32 `json:"input_tokens"`
			OutputTokens int32 `json:"output_tokens"`
		} `json:"usage"`
	}

	if err := json.Unmarshal(body, &resp); err != nil {
		return "", "", 0, 0
	}

	var content string
	for _, block := range resp.Content {
		if block.Type == "text" {
			content += block.Text
		}
	}

	var pt, ct int32
	if resp.Usage != nil {
		pt = resp.Usage.InputTokens
		ct = resp.Usage.OutputTokens
	}
	return content, "", pt, ct
}

// replaceContentInResponse replaces the content field in a provider response body
// with PII-restored content.
func replaceContentInResponse(body []byte, restored string, provider string) []byte {
	switch provider {
	case "anthropic":
		return replaceAnthropicContent(body, restored)
	case "ollama":
		return replaceOllamaContent(body, restored)
	default:
		return replaceOpenAIContent(body, restored)
	}
}

func replaceOpenAIContent(body []byte, content string) []byte {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		return body
	}
	var choices []map[string]json.RawMessage
	if err := json.Unmarshal(raw["choices"], &choices); err != nil || len(choices) == 0 {
		return body
	}
	var msg map[string]json.RawMessage
	if err := json.Unmarshal(choices[0]["message"], &msg); err != nil {
		return body
	}
	b, _ := json.Marshal(content)
	msg["content"] = b
	choices[0]["message"], _ = json.Marshal(msg)
	raw["choices"], _ = json.Marshal(choices)
	result, _ := json.Marshal(raw)
	return result
}

func replaceOllamaContent(body []byte, content string) []byte {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		return body
	}
	if _, ok := raw["message"]; ok {
		var msg map[string]json.RawMessage
		if err := json.Unmarshal(raw["message"], &msg); err == nil {
			b, _ := json.Marshal(content)
			msg["content"] = b
			raw["message"], _ = json.Marshal(msg)
		}
	} else {
		b, _ := json.Marshal(content)
		raw["response"] = b
	}
	result, _ := json.Marshal(raw)
	return result
}

func replaceAnthropicContent(body []byte, content string) []byte {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		return body
	}
	var blocks []map[string]json.RawMessage
	if err := json.Unmarshal(raw["content"], &blocks); err != nil || len(blocks) == 0 {
		return body
	}
	b, _ := json.Marshal(content)
	blocks[0]["text"] = b
	raw["content"], _ = json.Marshal(blocks)
	result, _ := json.Marshal(raw)
	return result
}
