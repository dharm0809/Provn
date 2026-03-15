package config

import (
	"os"
	"testing"
	"time"
)

func TestLoadDefaults(t *testing.T) {
	// Clear any existing env vars to test defaults.
	for _, k := range []string{
		"PROXY_LISTEN_ADDR", "PROXY_BRAIN_ADDR", "PROXY_LLM_PROVIDERS",
		"PROXY_MAX_BODY_SIZE", "PROXY_API_KEYS", "PROXY_RATE_LIMIT_RPS",
		"PROXY_CACHE_ENABLED", "PROXY_GRPC_TIMEOUT_SEC",
		"PROXY_LLM_FORWARD_TIMEOUT_SEC",
		"PROXY_READ_TIMEOUT_SEC", "PROXY_WRITE_TIMEOUT_SEC",
		"PROXY_POST_INFERENCE_ASYNC",
	} {
		os.Unsetenv(k)
	}

	cfg := Load()

	if cfg.ListenAddr != ":8080" {
		t.Errorf("ListenAddr = %q, want :8080", cfg.ListenAddr)
	}
	if cfg.BrainAddr != "localhost:50051" {
		t.Errorf("BrainAddr = %q, want localhost:50051", cfg.BrainAddr)
	}
	if cfg.MaxBodySize != 10*1024*1024 {
		t.Errorf("MaxBodySize = %d, want %d", cfg.MaxBodySize, 10*1024*1024)
	}
	if cfg.RateLimitRPS != 100 {
		t.Errorf("RateLimitRPS = %d, want 100", cfg.RateLimitRPS)
	}
	if cfg.CacheEnabled {
		t.Error("CacheEnabled should default to false")
	}
	if cfg.GRPCTimeoutSec != 5 {
		t.Errorf("GRPCTimeoutSec = %d, want 5", cfg.GRPCTimeoutSec)
	}
	if cfg.LLMForwardTimeoutSec != 30 {
		t.Errorf("LLMForwardTimeoutSec = %d, want 30", cfg.LLMForwardTimeoutSec)
	}
	if !cfg.PostInferenceAsync {
		t.Error("PostInferenceAsync should default to true")
	}
	if cfg.ReadTimeout != 30*time.Second {
		t.Errorf("ReadTimeout = %v, want 30s", cfg.ReadTimeout)
	}
	if cfg.WriteTimeout != 120*time.Second {
		t.Errorf("WriteTimeout = %v, want 120s", cfg.WriteTimeout)
	}
}

func TestLoadFromEnv(t *testing.T) {
	t.Setenv("PROXY_LISTEN_ADDR", ":9090")
	t.Setenv("PROXY_BRAIN_ADDR", "sidecar:50052")
	t.Setenv("PROXY_RATE_LIMIT_RPS", "50")
	t.Setenv("PROXY_CACHE_ENABLED", "true")
	t.Setenv("PROXY_MAX_BODY_SIZE", "5242880")

	cfg := Load()

	if cfg.ListenAddr != ":9090" {
		t.Errorf("ListenAddr = %q, want :9090", cfg.ListenAddr)
	}
	if cfg.BrainAddr != "sidecar:50052" {
		t.Errorf("BrainAddr = %q, want sidecar:50052", cfg.BrainAddr)
	}
	if cfg.RateLimitRPS != 50 {
		t.Errorf("RateLimitRPS = %d, want 50", cfg.RateLimitRPS)
	}
	if !cfg.CacheEnabled {
		t.Error("CacheEnabled should be true")
	}
	if cfg.MaxBodySize != 5242880 {
		t.Errorf("MaxBodySize = %d, want 5242880", cfg.MaxBodySize)
	}
}

func TestParseProviders(t *testing.T) {
	tests := []struct {
		name    string
		input   string
		want    map[string]string
		wantErr bool
	}{
		{
			name:  "empty",
			input: "",
			want:  map[string]string{},
		},
		{
			name:  "single provider",
			input: "ollama:http://localhost:11434",
			want:  map[string]string{"ollama": "http://localhost:11434"},
		},
		{
			name:  "multiple providers",
			input: "ollama:http://localhost:11434,openai:https://api.openai.com",
			want: map[string]string{
				"ollama": "http://localhost:11434",
				"openai": "https://api.openai.com",
			},
		},
		{
			name:  "whitespace trimmed",
			input: " ollama:http://localhost:11434 , openai:https://api.openai.com ",
			want: map[string]string{
				"ollama": "http://localhost:11434",
				"openai": "https://api.openai.com",
			},
		},
		{
			name:    "missing url",
			input:   "ollama",
			wantErr: true,
		},
		{
			name:    "empty name",
			input:   ":http://localhost",
			wantErr: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := &Config{LLMProviders: tt.input}
			got, err := cfg.ParseProviders()
			if (err != nil) != tt.wantErr {
				t.Errorf("ParseProviders() error = %v, wantErr %v", err, tt.wantErr)
				return
			}
			if tt.wantErr {
				return
			}
			if len(got) != len(tt.want) {
				t.Errorf("ParseProviders() got %d providers, want %d", len(got), len(tt.want))
				return
			}
			for k, v := range tt.want {
				if got[k] != v {
					t.Errorf("ParseProviders()[%q] = %q, want %q", k, got[k], v)
				}
			}
		})
	}
}

func TestParseAPIKeys(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  int
	}{
		{"empty", "", 0},
		{"single", "key1", 1},
		{"multiple", "key1,key2,key3", 3},
		{"whitespace", " key1 , key2 ", 2},
		{"empty entries", "key1,,key2,", 2},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := &Config{APIKeys: tt.input}
			got := cfg.ParseAPIKeys()
			if len(got) != tt.want {
				t.Errorf("ParseAPIKeys() got %d keys, want %d", len(got), tt.want)
			}
		})
	}
}

func TestEnvInvalidValues(t *testing.T) {
	// Invalid int should fall back to default.
	t.Setenv("PROXY_RATE_LIMIT_RPS", "not-a-number")
	t.Setenv("PROXY_CACHE_ENABLED", "not-a-bool")

	cfg := Load()

	if cfg.RateLimitRPS != 100 {
		t.Errorf("RateLimitRPS = %d, want 100 (default on parse error)", cfg.RateLimitRPS)
	}
	if cfg.CacheEnabled {
		t.Error("CacheEnabled should default to false on parse error")
	}
}
