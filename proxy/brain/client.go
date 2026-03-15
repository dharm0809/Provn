// Package brain wraps the gRPC connection to the Python governance sidecar.
package brain

import (
	"context"
	"fmt"
	"time"

	pb "walacor-gateway-proxy/pb"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// Client is a thin wrapper around the GovernanceEngine gRPC client.
type Client struct {
	conn   *grpc.ClientConn
	engine pb.GovernanceEngineClient

	// callTimeout is the per-call deadline for gRPC calls to the sidecar.
	callTimeout time.Duration
}

// NewClient establishes a gRPC connection to the sidecar at addr.
func NewClient(addr string, callTimeoutSec int) (*Client, error) {
	if callTimeoutSec <= 0 {
		callTimeoutSec = 5
	}

	conn, err := grpc.NewClient(
		addr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		return nil, fmt.Errorf("grpc dial %s: %w", addr, err)
	}

	return &Client{
		conn:        conn,
		engine:      pb.NewGovernanceEngineClient(conn),
		callTimeout: time.Duration(callTimeoutSec) * time.Second,
	}, nil
}

// EvaluatePreInference calls the sidecar pre-inference evaluation.
func (c *Client) EvaluatePreInference(ctx context.Context, req *pb.PreInferenceRequest) (*pb.PreInferenceResult, error) {
	ctx, cancel := context.WithTimeout(ctx, c.callTimeout)
	defer cancel()
	return c.engine.EvaluatePreInference(ctx, req)
}

// EvaluatePostInference calls the sidecar post-inference evaluation.
func (c *Client) EvaluatePostInference(ctx context.Context, req *pb.PostInferenceRequest) (*pb.PostInferenceResult, error) {
	ctx, cancel := context.WithTimeout(ctx, c.callTimeout)
	defer cancel()
	return c.engine.EvaluatePostInference(ctx, req)
}

// RecordExecution persists an execution record via the sidecar (dual-write).
func (c *Client) RecordExecution(ctx context.Context, req *pb.ExecutionRecord) (*pb.WriteResult, error) {
	// Use a longer timeout for writes — they may hit Walacor backend.
	ctx, cancel := context.WithTimeout(ctx, c.callTimeout*2)
	defer cancel()
	return c.engine.RecordExecution(ctx, req)
}

// CacheGet checks the semantic cache for a cached response.
func (c *Client) CacheGet(ctx context.Context, req *pb.CacheKey) (*pb.CacheResponse, error) {
	ctx, cancel := context.WithTimeout(ctx, c.callTimeout)
	defer cancel()
	return c.engine.CacheGet(ctx, req)
}

// CachePut stores a response in the semantic cache.
func (c *Client) CachePut(ctx context.Context, req *pb.CachePutRequest) (*pb.CacheResult, error) {
	ctx, cancel := context.WithTimeout(ctx, c.callTimeout)
	defer cancel()
	return c.engine.CachePut(ctx, req)
}

// HealthCheck queries the sidecar health status.
func (c *Client) HealthCheck(ctx context.Context) (*pb.HealthStatus, error) {
	ctx, cancel := context.WithTimeout(ctx, c.callTimeout)
	defer cancel()
	return c.engine.HealthCheck(ctx, &pb.Empty{})
}

// Close terminates the gRPC connection.
func (c *Client) Close() error {
	if c.conn != nil {
		return c.conn.Close()
	}
	return nil
}
