package health

import (
	"context"
	"errors"
	"net/http"
	"testing"
)

func TestAggregate_AllHealthy(t *testing.T) {
	r := Aggregate(map[string]error{"postgres": nil, "cognition": nil})
	if !r.Healthy || r.HTTPStatus != http.StatusOK {
		t.Fatalf("expected healthy 200, got %+v", r)
	}
	if r.Body["postgres"] != "ok" || r.Body["cognition"] != "ok" {
		t.Fatalf("body should mark ok, got %v", r.Body)
	}
}

func TestAggregate_OneFailed(t *testing.T) {
	r := Aggregate(map[string]error{"postgres": nil, "cognition": errors.New("not serving")})
	if r.Healthy || r.HTTPStatus != http.StatusServiceUnavailable {
		t.Fatalf("expected unhealthy 503, got %+v", r)
	}
	if r.Body["cognition"] != "not serving" {
		t.Fatalf("body should carry failure reason, got %v", r.Body)
	}
}

func TestAggregate_Empty(t *testing.T) {
	r := Aggregate(nil)
	if !r.Healthy || r.HTTPStatus != http.StatusOK {
		t.Fatalf("empty should be healthy, got %+v", r)
	}
}

func TestRunChecks_Concurrent(t *testing.T) {
	r := RunChecks(context.Background(), map[string]Check{
		"a": func(context.Context) error { return nil },
		"b": func(context.Context) error { return errors.New("down") },
	})
	if r.Healthy || r.Body["a"] != "ok" || r.Body["b"] != "down" {
		t.Fatalf("unexpected report %+v", r)
	}
}
