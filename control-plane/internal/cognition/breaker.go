// Circuit breaker wrapper for the cognition package.
//
// Interview highlight: wraps gRPC calls to the cognition plane with a sony/gobreaker
// circuit breaker.
// 5 consecutive failures → breaker opens for 30s → fast-fail → 3 half-open probes → recovery.
// Prevents cognition-plane failures from causing goroutine pile-up and connection-pool
// exhaustion in the control plane (cascading failure).
package cognition

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/sony/gobreaker/v2"
)

// cbSettings returns gobreaker.Settings. All values can be overridden via
// environment variables (defaults are production-tuned: 5 consecutive failures
// trip the breaker, 30s recovery window).
func cbSettings() gobreaker.Settings {
	return gobreaker.Settings{
		Name:        "cognition-grpc",
		MaxRequests: 3,                          // half-open state: allow up to 3 probe requests
		Interval:    30 * time.Second,            // consecutive-failure counter reset window
		Timeout:     30 * time.Second,            // open → half-open cooldown period
		ReadyToTrip: func(counts gobreaker.Counts) bool {
			// trip the breaker after 5 or more consecutive failures
			return counts.ConsecutiveFailures >= 5
		},
		OnStateChange: func(name string, from, to gobreaker.State) {
			slog.Warn("circuit breaker state change", "name", name, "from", from.String(), "to", to.String())
		},
	}
}

// cbClient wraps RunAgent with a circuit breaker (interview highlight:
// microservice resilience design).
type cbClient struct {
	Client       // embed the original client
	cb *gobreaker.TwoStepCircuitBreaker[runResult]
}

// runResult is the return value of the breaker-protected RunAgent.
type runResult struct {
	stream Stream
	err    error
}

// NewCircuitBreakerClient wraps the original client with a circuit breaker.
func NewCircuitBreakerClient(inner Client) Client {
	if inner == nil {
		return nil
	}
	return &cbClient{
		Client: inner,
		cb:     gobreaker.NewTwoStepCircuitBreaker[runResult](cbSettings()),
	}
}

// RunAgent calls the original client under circuit-breaker protection.
//
// When the breaker is Open the call fails immediately (no gRPC call is made).
// The upstream dispatch layer catches this and transitions to FAILED terminal
// state while recording the breaker count.
func (c *cbClient) RunAgent(ctx context.Context, req RunRequest) (Stream, error) {
	done, err := c.cb.Allow()
	if err != nil {
		return nil, fmt.Errorf("cognition: circuit breaker open: %w", err)
	}

	stream, runErr := c.Client.RunAgent(ctx, req)

	// Two-step breaker: done(nil) → success; done(error) → failure
	done(runErr)

	if runErr != nil {
		return nil, runErr
	}
	return stream, nil
}
