// Package health 提供最小健康检查：把若干依赖探测结果聚合成单一 /healthz 判定。
// 判定逻辑（Aggregate）是纯函数、可完整单测；RunChecks 只做并发执行的薄封装。
package health

import (
	"context"
	"net/http"
	"sync"
)

// Check 是一个依赖探测：健康返回 nil，否则返回原因。
type Check func(context.Context) error

// Report 是聚合后的健康结论。
type Report struct {
	Healthy    bool
	HTTPStatus int
	Body       map[string]string // name -> "ok" | 错误文本
}

// Aggregate 把 name→err 结果聚合：全 nil→200 healthy；任一非 nil→503 且 body 标注失败项。
func Aggregate(results map[string]error) Report {
	body := make(map[string]string, len(results))
	healthy := true
	for name, err := range results {
		if err != nil {
			healthy = false
			body[name] = err.Error()
		} else {
			body[name] = "ok"
		}
	}
	status := http.StatusOK
	if !healthy {
		status = http.StatusServiceUnavailable
	}
	return Report{Healthy: healthy, HTTPStatus: status, Body: body}
}

// RunChecks 并发执行全部 checks（各自复用同一 ctx），再 Aggregate。空 checks→healthy。
func RunChecks(ctx context.Context, checks map[string]Check) Report {
	results := make(map[string]error, len(checks))
	var mu sync.Mutex
	var wg sync.WaitGroup
	for name, check := range checks {
		wg.Add(1)
		go func(name string, check Check) {
			defer wg.Done()
			err := check(ctx)
			mu.Lock()
			results[name] = err
			mu.Unlock()
		}(name, check)
	}
	wg.Wait()
	return Aggregate(results)
}
