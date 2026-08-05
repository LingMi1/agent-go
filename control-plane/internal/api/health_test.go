package api_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"my-agent/control-plane/internal/api"
	"my-agent/control-plane/internal/health"
)

func getHealthz(t *testing.T, checks map[string]health.Check) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	router := api.NewRouter(nil, nil, nil, nil, nil, checks, nil, nil, nil, nil, nil, nil, nil, nil, nil, time.Minute, "", discardLogger())
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	var body map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	return rec, body
}

func TestHealthz_AllOK(t *testing.T) {
	rec, body := getHealthz(t, map[string]health.Check{
		"postgres":  func(context.Context) error { return nil },
		"cognition": func(context.Context) error { return nil },
	})
	if rec.Code != http.StatusOK || body["healthy"] != true {
		t.Fatalf("expected healthy 200, got %d %v", rec.Code, body)
	}
}

func TestHealthz_DependencyDown(t *testing.T) {
	rec, body := getHealthz(t, map[string]health.Check{
		"postgres":  func(context.Context) error { return nil },
		"cognition": func(context.Context) error { return errors.New("not serving") },
	})
	if rec.Code != http.StatusServiceUnavailable || body["healthy"] != false {
		t.Fatalf("expected unhealthy 503, got %d %v", rec.Code, body)
	}
	checks, _ := body["checks"].(map[string]any)
	if checks["cognition"] != "not serving" {
		t.Fatalf("expected failure reason in body, got %v", body["checks"])
	}
}

func TestHealthz_NoChecks200(t *testing.T) {
	rec, body := getHealthz(t, nil)
	if rec.Code != http.StatusOK || body["healthy"] != true {
		t.Fatalf("nil checks should be healthy 200, got %d %v", rec.Code, body)
	}
}
