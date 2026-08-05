package store

// 用量统计只读投影（M11 成本面板）：runs 表聚合，owner 域内。
// 与 SessionRepository 同款"独立只读接口"模式——不动 RunRepository，既有 fake 零改。

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5/pgxpool"
)

// UsageTotals 是一段时间窗内的合计。
type UsageTotals struct {
	Runs         int64 `json:"runs"`
	InputTokens  int64 `json:"inputTokens"`
	OutputTokens int64 `json:"outputTokens"`
	ModelCalls   int64 `json:"modelCalls"`
}

// UsageDay 是按天聚合的一行（date 为 YYYY-MM-DD）。
type UsageDay struct {
	Date         string `json:"date"`
	Runs         int64  `json:"runs"`
	InputTokens  int64  `json:"inputTokens"`
	OutputTokens int64  `json:"outputTokens"`
}

// UsageByAgent 是按模式聚合的一行。
type UsageByAgent struct {
	AgentType    string `json:"agentType"`
	Runs         int64  `json:"runs"`
	InputTokens  int64  `json:"inputTokens"`
	OutputTokens int64  `json:"outputTokens"`
}

// UsageReport 是 GET /stats/usage 的完整载荷。
type UsageReport struct {
	Days    int            `json:"days"`
	Totals  UsageTotals    `json:"totals"`
	Daily   []UsageDay     `json:"daily"`
	ByAgent []UsageByAgent `json:"byAgent"`
}

// StatsRepository 是用量统计的只读端口。
type StatsRepository interface {
	UsageReport(ctx context.Context, ownerID string, days int) (UsageReport, error)
}

// AdminStatsReporter 是 admin 后台的**跨 owner** 系统级用量端口（docs/17 §3.3）。
// 与 StatsRepository 分开：既有 owner 域 UsageReport 一字不动（零弱化）；pgStatsRepo 同时实现，
// api 层对已装配的 StatsRepository 做类型断言取用（fake/nil 自然降级 503）。
type AdminStatsReporter interface {
	AdminUsageReport(ctx context.Context, days int) (UsageReport, error)
}

type pgStatsRepo struct{ pool *pgxpool.Pool }

// NewStatsRepository 构造统计仓库。
func NewStatsRepository(pool *pgxpool.Pool) StatsRepository { return &pgStatsRepo{pool: pool} }

func (r *pgStatsRepo) UsageReport(ctx context.Context, ownerID string, days int) (UsageReport, error) {
	if days <= 0 || days > 365 {
		days = 30
	}
	report := UsageReport{Days: days, Daily: []UsageDay{}, ByAgent: []UsageByAgent{}}

	err := r.pool.QueryRow(ctx, `
		SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(model_calls),0)
		  FROM runs
		 WHERE owner_id = $1 AND created_at >= now() - make_interval(days => $2)`,
		ownerID, days,
	).Scan(&report.Totals.Runs, &report.Totals.InputTokens, &report.Totals.OutputTokens, &report.Totals.ModelCalls)
	if err != nil {
		return report, fmt.Errorf("store: usage totals: %w", err)
	}

	rows, err := r.pool.Query(ctx, `
		SELECT to_char(created_at::date, 'YYYY-MM-DD') AS d, COUNT(*),
		       COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0)
		  FROM runs
		 WHERE owner_id = $1 AND created_at >= now() - make_interval(days => $2)
		 GROUP BY d ORDER BY d`,
		ownerID, days)
	if err != nil {
		return report, fmt.Errorf("store: usage daily: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var d UsageDay
		if err := rows.Scan(&d.Date, &d.Runs, &d.InputTokens, &d.OutputTokens); err != nil {
			return report, err
		}
		report.Daily = append(report.Daily, d)
	}

	rows2, err := r.pool.Query(ctx, `
		SELECT entry_agent, COUNT(*),
		       COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0)
		  FROM runs
		 WHERE owner_id = $1 AND created_at >= now() - make_interval(days => $2)
		 GROUP BY entry_agent ORDER BY 3 DESC`,
		ownerID, days)
	if err != nil {
		return report, fmt.Errorf("store: usage by agent: %w", err)
	}
	defer rows2.Close()
	for rows2.Next() {
		var a UsageByAgent
		if err := rows2.Scan(&a.AgentType, &a.Runs, &a.InputTokens, &a.OutputTokens); err != nil {
			return report, err
		}
		report.ByAgent = append(report.ByAgent, a)
	}
	return report, nil
}

// AdminUsageReport 是 UsageReport 的**跨 owner** 孪生（无 WHERE owner_id）：系统级合计/按天/
// 按模式聚合，供 admin 后台。刻意复制 SQL 而非给 UsageReport 加 owner="" 分支——既有 owner
// 域方法零改动、零回归风险（docs/17 §4：admin 跨 owner 读用新方法，绝不放宽既有过滤）。
func (r *pgStatsRepo) AdminUsageReport(ctx context.Context, days int) (UsageReport, error) {
	if days <= 0 || days > 365 {
		days = 30
	}
	report := UsageReport{Days: days, Daily: []UsageDay{}, ByAgent: []UsageByAgent{}}

	err := r.pool.QueryRow(ctx, `
		SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(model_calls),0)
		  FROM runs
		 WHERE created_at >= now() - make_interval(days => $1)`,
		days,
	).Scan(&report.Totals.Runs, &report.Totals.InputTokens, &report.Totals.OutputTokens, &report.Totals.ModelCalls)
	if err != nil {
		return report, fmt.Errorf("store: admin usage totals: %w", err)
	}

	rows, err := r.pool.Query(ctx, `
		SELECT to_char(created_at::date, 'YYYY-MM-DD') AS d, COUNT(*),
		       COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0)
		  FROM runs
		 WHERE created_at >= now() - make_interval(days => $1)
		 GROUP BY d ORDER BY d`, days)
	if err != nil {
		return report, fmt.Errorf("store: admin usage daily: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var d UsageDay
		if err := rows.Scan(&d.Date, &d.Runs, &d.InputTokens, &d.OutputTokens); err != nil {
			return report, err
		}
		report.Daily = append(report.Daily, d)
	}

	rows2, err := r.pool.Query(ctx, `
		SELECT entry_agent, COUNT(*),
		       COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0)
		  FROM runs
		 WHERE created_at >= now() - make_interval(days => $1)
		 GROUP BY entry_agent ORDER BY 3 DESC`, days)
	if err != nil {
		return report, fmt.Errorf("store: admin usage by agent: %w", err)
	}
	defer rows2.Close()
	for rows2.Next() {
		var a UsageByAgent
		if err := rows2.Scan(&a.AgentType, &a.Runs, &a.InputTokens, &a.OutputTokens); err != nil {
			return report, err
		}
		report.ByAgent = append(report.ByAgent, a)
	}
	return report, nil
}
