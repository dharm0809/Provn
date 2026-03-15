// Walacor Gateway Go Proxy — HTTP front-end that forwards LLM requests through
// the Python governance sidecar via gRPC.
//
// Usage:
//
//	PROXY_LLM_PROVIDERS="ollama:http://localhost:11434,openai:https://api.openai.com" \
//	PROXY_BRAIN_ADDR="localhost:50051" \
//	./proxy
package main

import (
	"context"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"walacor-gateway-proxy/brain"
	"walacor-gateway-proxy/config"
	"walacor-gateway-proxy/handler"
	"walacor-gateway-proxy/health"
	"walacor-gateway-proxy/middleware"
)

func main() {
	// Structured logging.
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	})))

	cfg := config.Load()

	// Parse providers.
	providers, err := cfg.ParseProviders()
	if err != nil {
		slog.Error("failed to parse LLM providers", "error", err)
		os.Exit(1)
	}
	slog.Info("loaded providers", "count", len(providers))
	for name, url := range providers {
		slog.Info("provider", "name", name, "url", url)
	}

	// Connect to the Python governance sidecar.
	brainClient, err := brain.NewClient(cfg.BrainAddr, cfg.GRPCTimeoutSec)
	if err != nil {
		slog.Error("failed to connect to governance sidecar", "error", err, "addr", cfg.BrainAddr)
		os.Exit(1)
	}
	defer brainClient.Close()
	slog.Info("connected to governance sidecar", "addr", cfg.BrainAddr)

	// Build proxy handler.
	proxy := handler.NewProxy(brainClient, providers, cfg.CacheEnabled, cfg.LLMForwardTimeoutSec, cfg.MaxBodySize, cfg.PostInferenceAsync)

	// Build middleware chain.
	apiKeys := cfg.ParseAPIKeys()
	var h http.Handler = proxy
	h = middleware.RateLimit(cfg.RateLimitRPS)(h)
	h = middleware.Auth(apiKeys)(h)
	h = middleware.Logging(h)

	// Register routes.
	mux := http.NewServeMux()
	mux.Handle("/health", health.Handler(brainClient))
	mux.Handle("/", h)

	server := &http.Server{
		Addr:         cfg.ListenAddr,
		Handler:      mux,
		ReadTimeout:  cfg.ReadTimeout,
		WriteTimeout: cfg.WriteTimeout,
	}

	// Graceful shutdown.
	errCh := make(chan error, 1)
	go func() {
		slog.Info("proxy server starting", "addr", cfg.ListenAddr)
		errCh <- server.ListenAndServe()
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)

	select {
	case sig := <-quit:
		slog.Info("received shutdown signal", "signal", sig)
	case err := <-errCh:
		if err != http.ErrServerClosed {
			slog.Error("server error", "error", err)
		}
	}

	// Shutdown with a 10-second deadline.
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	if err := server.Shutdown(ctx); err != nil {
		slog.Error("server shutdown error", "error", err)
	}

	slog.Info("proxy server stopped")
}
