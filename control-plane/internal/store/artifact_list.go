package store

// 跨会话产物列举（Files 侧栏"产物"画廊）：runs+events 聚合，owner 域只读。
// 独立只读接口（循 SessionRepository/StatsRepository），不动 RunRepository 既有 fake。
//
// 产物落在 events.payload JSONB 的 "artifacts" 键（tool_result 与终态 result 都带 →
// 同一 resource_key 出现 2×，多图更多）：**必须 SQL 内先 DISTINCT ON 去重再限量**，
// 否则 LIMIT 落在未去重行上、回给前端的唯一产物数不稳。

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5/pgxpool"
)

// OwnerArtifact 是画廊一格：一个去重后的产物 + 它最近一次出现的 run/会话/时间。
type OwnerArtifact struct {
	RunID       string `json:"runId"`
	SessionID   string `json:"sessionId"` // 供画廊"打开来源会话"跳转
	ResourceKey string `json:"resourceKey"`
	Name        string `json:"name"`
	FileName    string `json:"fileName"`
	DownloadURL string `json:"downloadUrl"`
	PreviewURL  string `json:"previewUrl"`
	MimeType    string `json:"mimeType"`
	Size        int64  `json:"size"`
	TSUnixMs    int64  `json:"tsUnixMs"`
}

// ArtifactListRepository 是产物画廊的只读端口。
type ArtifactListRepository interface {
	// beforeTS/beforeKey 是游标（上一页最后一项的 ts_unix_ms + resource_key）：只返回严格更旧的。
	// beforeTS<=0 表示首页。用复合 tie-breaker 防同 ts 产物在页边界丢/重。
	// mimePrefix 非空时服务端按 mime 前缀过滤（如 "image/"）——否则客户端在单页里过滤会漏更旧的图。
	ListByOwner(ctx context.Context, ownerID string, limit int, beforeTS int64, beforeKey, mimePrefix string) ([]OwnerArtifact, error)
}

type pgArtifactListRepo struct{ pool *pgxpool.Pool }

// NewArtifactListRepository 构造产物画廊仓库。
func NewArtifactListRepository(pool *pgxpool.Pool) ArtifactListRepository {
	return &pgArtifactListRepo{pool: pool}
}

// 先按 resource_key DISTINCT ON 取每个产物最近一次出现（去 tool_result/result 双份），
// 跳过 missing，再对去重集按时间排序并限量。owner 经 join runs 强隔离。
const artifactListSQL = `
WITH expanded AS (
    SELECT DISTINCT ON (a->>'resourceKey')
           e.run_id,
           r.session_id,
           a->>'resourceKey' AS resource_key,
           COALESCE(a->>'name', '')        AS name,
           COALESCE(a->>'fileName', '')    AS file_name,
           COALESCE(a->>'downloadUrl', '') AS download_url,
           COALESCE(a->>'previewUrl', '')  AS preview_url,
           COALESCE(a->>'mimeType', '')    AS mime_type,
           COALESCE((a->>'size')::bigint, 0) AS size,
           e.ts_unix_ms
      FROM runs r
      JOIN events e ON e.run_id = r.run_id
      CROSS JOIN LATERAL jsonb_array_elements(e.payload -> 'artifacts') AS a
     WHERE r.owner_id = $1
       AND e.message_type IN ('tool_result', 'result')
       AND jsonb_typeof(e.payload -> 'artifacts') = 'array'
       AND COALESCE(a->>'resourceKey', '') <> ''
       AND (a->>'missing') IS DISTINCT FROM 'true'
     ORDER BY a->>'resourceKey', e.ts_unix_ms DESC
)
SELECT run_id, session_id, resource_key, name, file_name, download_url, preview_url, mime_type, size, ts_unix_ms
  FROM expanded
 WHERE ($3 <= 0 OR (ts_unix_ms, resource_key) < ($3, $4))  -- 游标：严格更旧（复合 tie-breaker）
   AND ($5 = '' OR mime_type LIKE $5 || '%')                -- mime 前缀过滤（服务端，防单页漏）
 ORDER BY ts_unix_ms DESC, resource_key DESC
 LIMIT $2`

func (r *pgArtifactListRepo) ListByOwner(ctx context.Context, ownerID string, limit int, beforeTS int64, beforeKey, mimePrefix string) ([]OwnerArtifact, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	rows, err := r.pool.Query(ctx, artifactListSQL, ownerID, limit, beforeTS, beforeKey, mimePrefix)
	if err != nil {
		return nil, fmt.Errorf("store: list artifacts: %w", err)
	}
	defer rows.Close()
	out := []OwnerArtifact{}
	for rows.Next() {
		var a OwnerArtifact
		if err := rows.Scan(&a.RunID, &a.SessionID, &a.ResourceKey, &a.Name, &a.FileName,
			&a.DownloadURL, &a.PreviewURL, &a.MimeType, &a.Size, &a.TSUnixMs); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}
