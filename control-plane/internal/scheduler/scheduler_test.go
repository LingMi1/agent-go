package scheduler

import (
	"context"
	"testing"
	"time"

	"my-agent/control-plane/internal/event"
	"my-agent/control-plane/internal/stream"
)

func TestNullSink(t *testing.T) {
	var s stream.NullSink
	if err := s.WriteFrame(event.Envelope{}); err != nil {
		t.Errorf("WriteFrame: %v", err)
	}
	if err := s.WriteHeartbeat(); err != nil {
		t.Errorf("WriteHeartbeat: %v", err)
	}
	if err := s.WriteDone(); err != nil {
		t.Errorf("WriteDone: %v", err)
	}
}

func TestNewDefaults(t *testing.T) {
	s := New(nil, nil, nil, 30*time.Second, 0, 0, nil)
	if cap(s.slots) != 2 {
		t.Errorf("maxConcurrent default: got %d, want 2", cap(s.slots))
	}
	if s.tick != 30*time.Second {
		t.Errorf("tick default: got %v, want 30s", s.tick)
	}
}

func TestNewCustomValues(t *testing.T) {
	s := New(nil, nil, nil, 60*time.Second, 10*time.Second, 4, nil)
	if cap(s.slots) != 4 {
		t.Errorf("maxConcurrent: got %d, want 4", cap(s.slots))
	}
	if s.tick != 10*time.Second {
		t.Errorf("tick: got %v, want 10s", s.tick)
	}
}

func TestRunCancellation_AlreadyCanceledCtx(t *testing.T) {
	// fireDue requires a non-nil repo, so we only test that Run exits
	// immediately when ctx is already canceled (before first tick).
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // cancel immediately
	s := New(nil, nil, nil, 30*time.Second, 1*time.Hour, 1, nil)

	// Run with already-canceled ctx should return immediately.
	s.Run(ctx)
	// No timeout needed — if Run didn't return, this line never executes.
}

func TestNew_NegativeMaxConcurrent(t *testing.T) {
	s := New(nil, nil, nil, 30*time.Second, 30*time.Second, -1, nil)
	if cap(s.slots) != 2 {
		t.Errorf("negative maxConcurrent: got %d, want default 2", cap(s.slots))
	}
}

func TestNew_ZeroTick(t *testing.T) {
	s := New(nil, nil, nil, 30*time.Second, 0, 2, nil)
	if s.tick != 30*time.Second {
		t.Errorf("zero tick: got %v, want default 30s", s.tick)
	}
}
