// Package middleware provides HTTP middleware for the proxy.
package middleware

import (
	"net/http"
	"strings"
)

// skipAuthPaths are paths that do not require authentication.
var skipAuthPaths = map[string]bool{
	"/health":  true,
	"/metrics": true,
}

// Auth returns middleware that validates API key authentication.
// It checks the Authorization header (Bearer token) and the X-API-Key header.
// Requests to /health and /metrics bypass authentication.
// If validKeys is empty, authentication is disabled (all requests pass).
func Auth(validKeys map[string]bool) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// Skip auth for health/metrics endpoints.
			if skipAuthPaths[r.URL.Path] {
				next.ServeHTTP(w, r)
				return
			}

			// If no keys configured, auth is disabled.
			if len(validKeys) == 0 {
				next.ServeHTTP(w, r)
				return
			}

			// Try Authorization: Bearer <key>
			if auth := r.Header.Get("Authorization"); auth != "" {
				if strings.HasPrefix(auth, "Bearer ") {
					key := strings.TrimPrefix(auth, "Bearer ")
					if validKeys[key] {
						next.ServeHTTP(w, r)
						return
					}
				}
			}

			// Try X-API-Key header.
			if key := r.Header.Get("X-API-Key"); key != "" {
				if validKeys[key] {
					next.ServeHTTP(w, r)
					return
				}
			}

			http.Error(w, `{"error":"unauthorized","message":"valid API key required"}`, http.StatusUnauthorized)
		})
	}
}
