package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestRateLimitAllows(t *testing.T) {
	handler := RateLimit(100)(http.HandlerFunc(okHandler))

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200", rec.Code)
	}
}

func TestRateLimitExceeded(t *testing.T) {
	// Set rate limit to 1 RPS with burst of 1.
	handler := RateLimit(1)(http.HandlerFunc(okHandler))

	// First request should succeed.
	req1 := httptest.NewRequest(http.MethodGet, "/", nil)
	rec1 := httptest.NewRecorder()
	handler.ServeHTTP(rec1, req1)

	if rec1.Code != http.StatusOK {
		t.Errorf("first request: status = %d, want 200", rec1.Code)
	}

	// Burst subsequent requests to exhaust the bucket.
	var blocked bool
	for i := 0; i < 10; i++ {
		req := httptest.NewRequest(http.MethodGet, "/", nil)
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if rec.Code == http.StatusTooManyRequests {
			blocked = true
			// Verify Retry-After header.
			if rec.Header().Get("Retry-After") == "" {
				t.Error("missing Retry-After header on 429")
			}
			break
		}
	}

	if !blocked {
		t.Error("rate limiter never returned 429")
	}
}

func TestRateLimitZeroFallback(t *testing.T) {
	// A zero or negative RPS should fall back to 100.
	handler := RateLimit(0)(http.HandlerFunc(okHandler))

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200", rec.Code)
	}
}
