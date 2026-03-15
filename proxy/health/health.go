// Package health provides the /health endpoint for the proxy.
package health

import (
	"encoding/json"
	"log/slog"
	"net/http"

	"walacor-gateway-proxy/brain"
)

// response is the JSON structure returned by the health endpoint.
type response struct {
	Status  string         `json:"status"`
	Proxy   string         `json:"proxy"`
	Brain   *brainStatus   `json:"brain,omitempty"`
}

type brainStatus struct {
	Healthy            bool              `json:"healthy"`
	Version            string            `json:"version"`
	GovernanceEnabled  bool              `json:"governance_enabled"`
	ActiveSessions     int32             `json:"active_sessions"`
	ContentAnalyzers   int32             `json:"content_analyzers"`
	ModelCapabilities  map[string]string `json:"model_capabilities,omitempty"`
}

// Handler returns an http.HandlerFunc that reports proxy and sidecar health.
func Handler(brainClient *brain.Client) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		resp := response{
			Status: "ok",
			Proxy:  "healthy",
		}

		if brainClient != nil {
			hs, err := brainClient.HealthCheck(r.Context())
			if err != nil {
				slog.Warn("brain health check failed", "error", err)
				resp.Status = "degraded"
				resp.Brain = &brainStatus{
					Healthy: false,
				}
			} else {
				resp.Brain = &brainStatus{
					Healthy:           hs.Healthy,
					Version:           hs.Version,
					GovernanceEnabled: hs.GovernanceEnabled,
					ActiveSessions:    hs.ActiveSessions,
					ContentAnalyzers:  hs.ContentAnalyzerCount,
					ModelCapabilities: hs.ModelCapabilities,
				}
				if !hs.Healthy {
					resp.Status = "degraded"
				}
			}
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
	}
}
