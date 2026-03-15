package middleware

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func okHandler(w http.ResponseWriter, _ *http.Request) {
	w.WriteHeader(http.StatusOK)
	w.Write([]byte("ok"))
}

func TestAuthBearerToken(t *testing.T) {
	keys := map[string]bool{"valid-key": true}
	handler := Auth(keys)(http.HandlerFunc(okHandler))

	req := httptest.NewRequest(http.MethodGet, "/api/test", nil)
	req.Header.Set("Authorization", "Bearer valid-key")
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200", rec.Code)
	}
}

func TestAuthXAPIKey(t *testing.T) {
	keys := map[string]bool{"api-key-123": true}
	handler := Auth(keys)(http.HandlerFunc(okHandler))

	req := httptest.NewRequest(http.MethodGet, "/api/test", nil)
	req.Header.Set("X-API-Key", "api-key-123")
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200", rec.Code)
	}
}

func TestAuthInvalidKey(t *testing.T) {
	keys := map[string]bool{"valid-key": true}
	handler := Auth(keys)(http.HandlerFunc(okHandler))

	req := httptest.NewRequest(http.MethodGet, "/api/test", nil)
	req.Header.Set("Authorization", "Bearer wrong-key")
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want 401", rec.Code)
	}
}

func TestAuthNoKeyProvided(t *testing.T) {
	keys := map[string]bool{"valid-key": true}
	handler := Auth(keys)(http.HandlerFunc(okHandler))

	req := httptest.NewRequest(http.MethodGet, "/api/test", nil)
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want 401", rec.Code)
	}
}

func TestAuthSkipHealth(t *testing.T) {
	keys := map[string]bool{"valid-key": true}
	handler := Auth(keys)(http.HandlerFunc(okHandler))

	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200 (health bypass)", rec.Code)
	}
}

func TestAuthSkipMetrics(t *testing.T) {
	keys := map[string]bool{"valid-key": true}
	handler := Auth(keys)(http.HandlerFunc(okHandler))

	req := httptest.NewRequest(http.MethodGet, "/metrics", nil)
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200 (metrics bypass)", rec.Code)
	}
}

func TestAuthDisabledNoKeys(t *testing.T) {
	keys := map[string]bool{}
	handler := Auth(keys)(http.HandlerFunc(okHandler))

	req := httptest.NewRequest(http.MethodGet, "/api/test", nil)
	rec := httptest.NewRecorder()

	handler.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200 (auth disabled)", rec.Code)
	}
}
