// Package config loads proxy configuration from environment variables.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config holds all proxy configuration values loaded from the environment.
type Config struct {
	// ListenAddr is the address the HTTP server listens on (default ":8080").
	ListenAddr string

	// BrainAddr is the gRPC address of the Python governance sidecar (default "localhost:50051").
	BrainAddr string

	// LLMProviders is a comma-separated list of "provider:base_url" pairs.
	// Example: "ollama:http://localhost:11434,openai:https://api.openai.com"
	LLMProviders string

	// ReadTimeout for the HTTP server.
	ReadTimeout time.Duration

	// WriteTimeout for the HTTP server.
	WriteTimeout time.Duration

	// MaxBodySize is the maximum allowed request body size in bytes (default 10MB).
	MaxBodySize int64

	// APIKeys is a comma-separated list of valid API keys.
	APIKeys string

	// RateLimitRPS is the maximum requests per second (default 100).
	RateLimitRPS int

	// CacheEnabled controls whether semantic cache lookups are performed (default false).
	CacheEnabled bool

	// GRPCTimeoutSec is the timeout for gRPC calls to the sidecar in seconds (default 5).
	GRPCTimeoutSec int

	// LLMForwardTimeoutSec is the timeout for forwarding requests to LLM providers in seconds (default 30).
	LLMForwardTimeoutSec int
}

// Load reads configuration from environment variables, applying defaults where unset.
func Load() *Config {
	return &Config{
		ListenAddr:           envOr("PROXY_LISTEN_ADDR", ":8080"),
		BrainAddr:            envOr("PROXY_BRAIN_ADDR", "localhost:50051"),
		LLMProviders:         envOr("PROXY_LLM_PROVIDERS", ""),
		ReadTimeout:          envDuration("PROXY_READ_TIMEOUT_SEC", 30),
		WriteTimeout:         envDuration("PROXY_WRITE_TIMEOUT_SEC", 120),
		MaxBodySize:          envInt64("PROXY_MAX_BODY_SIZE", 10*1024*1024),
		APIKeys:              envOr("PROXY_API_KEYS", ""),
		RateLimitRPS:         envInt("PROXY_RATE_LIMIT_RPS", 100),
		CacheEnabled:         envBool("PROXY_CACHE_ENABLED", false),
		GRPCTimeoutSec:       envInt("PROXY_GRPC_TIMEOUT_SEC", 5),
		LLMForwardTimeoutSec: envInt("PROXY_LLM_FORWARD_TIMEOUT_SEC", 30),
	}
}

// ParseProviders parses LLMProviders into a map of provider_name -> base_url.
func (c *Config) ParseProviders() (map[string]string, error) {
	result := make(map[string]string)
	if c.LLMProviders == "" {
		return result, nil
	}

	pairs := strings.Split(c.LLMProviders, ",")
	for _, pair := range pairs {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		// Split on first colon only — URLs contain colons.
		idx := strings.Index(pair, ":")
		if idx < 0 {
			return nil, fmt.Errorf("invalid provider spec %q: expected name:url", pair)
		}
		name := pair[:idx]
		url := pair[idx+1:]
		if name == "" || url == "" {
			return nil, fmt.Errorf("invalid provider spec %q: name and url must be non-empty", pair)
		}
		result[name] = url
	}
	return result, nil
}

// ParseAPIKeys returns the set of valid API keys.
func (c *Config) ParseAPIKeys() map[string]bool {
	keys := make(map[string]bool)
	if c.APIKeys == "" {
		return keys
	}
	for _, k := range strings.Split(c.APIKeys, ",") {
		k = strings.TrimSpace(k)
		if k != "" {
			keys[k] = true
		}
	}
	return keys
}

// --- helpers ---

func envOr(key, fallback string) string {
	if v, ok := os.LookupEnv(key); ok {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	v, ok := os.LookupEnv(key)
	if !ok {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}

func envInt64(key string, fallback int64) int64 {
	v, ok := os.LookupEnv(key)
	if !ok {
		return fallback
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil {
		return fallback
	}
	return n
}

func envBool(key string, fallback bool) bool {
	v, ok := os.LookupEnv(key)
	if !ok {
		return fallback
	}
	b, err := strconv.ParseBool(v)
	if err != nil {
		return fallback
	}
	return b
}

func envDuration(key string, fallbackSec int) time.Duration {
	return time.Duration(envInt(key, fallbackSec)) * time.Second
}
