package middleware

import (
	"fmt"
	"net/http"

	"golang.org/x/time/rate"
)

// RateLimit returns middleware that applies a token-bucket rate limiter.
// rps is the maximum sustained requests per second. Burst is set to rps
// to absorb short spikes. Returns 429 Too Many Requests when exceeded.
func RateLimit(rps int) func(http.Handler) http.Handler {
	if rps <= 0 {
		rps = 100
	}
	limiter := rate.NewLimiter(rate.Limit(rps), rps)

	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if !limiter.Allow() {
				w.Header().Set("Retry-After", "1")
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusTooManyRequests)
				fmt.Fprint(w, `{"error":"rate_limit_exceeded","message":"too many requests, retry after 1s"}`)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}
