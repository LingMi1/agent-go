package connector

// GitHubConnector：GitHub PAT 轮询实现（docs/16 §3.3）。
// 免 OAuth 回调、免公网 webhook——一个 PAT 即可拉。最小拉取面 = notifications
// （被 @/被 assign/订阅），标准库 net/http + Authorization: token <PAT> + since 游标增量。
//
// 可测：baseURL 与 *http.Client 可注入（NewGitHub 选项），测试用 httptest.Server
// 或固定 transport 喂 JSON，无需真 GitHub。

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const defaultGitHubBaseURL = "https://api.github.com"

// GitHubConnector 实现 Connector。
type GitHubConnector struct {
	baseURL string
	http    *http.Client
}

// Option 配置 GitHubConnector（测试注入 baseURL/http）。
type Option func(*GitHubConnector)

// WithBaseURL 覆盖 API 基址（测试指向 httptest.Server）。
func WithBaseURL(u string) Option {
	return func(g *GitHubConnector) { g.baseURL = strings.TrimRight(u, "/") }
}

// WithHTTPClient 注入 http.Client（测试用固定 transport）。
func WithHTTPClient(c *http.Client) Option { return func(g *GitHubConnector) { g.http = c } }

// NewGitHub 构造 GitHub 连接器（默认真 api.github.com + 15s 超时 client）。
func NewGitHub(opts ...Option) *GitHubConnector {
	g := &GitHubConnector{
		baseURL: defaultGitHubBaseURL,
		http:    &http.Client{Timeout: 15 * time.Second},
	}
	for _, o := range opts {
		o(g)
	}
	return g
}

func (g *GitHubConnector) newRequest(ctx context.Context, path, pat string) (*http.Request, error) {
	return g.newRequestURL(ctx, g.baseURL+path, pat)
}

// newRequestURL 用绝对 URL 建请求（翻页跟随 Link 头的 next URL 时用——GitHub 返回绝对地址）。
func (g *GitHubConnector) newRequestURL(ctx context.Context, fullURL, pat string) (*http.Request, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, fullURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "token "+pat)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	return req, nil
}

// Authorize 校验 PAT：GET /user，非 2xx 视为无效凭据。
func (g *GitHubConnector) Authorize(ctx context.Context, ownerID, pat string) error {
	if strings.TrimSpace(pat) == "" {
		return fmt.Errorf("connector: empty PAT")
	}
	req, err := g.newRequest(ctx, "/user", pat)
	if err != nil {
		return err
	}
	resp, err := g.http.Do(req)
	if err != nil {
		return fmt.Errorf("connector: authorize request: %w", err)
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("connector: authorize failed: github status %d", resp.StatusCode)
	}
	return nil
}

// githubNotification 是 /notifications 响应元素的最小投影。
type githubNotification struct {
	ID        string `json:"id"`
	Reason    string `json:"reason"`
	UpdatedAt string `json:"updated_at"`
	Subject   struct {
		Title string `json:"title"`
		URL   string `json:"url"`
		Type  string `json:"type"`
	} `json:"subject"`
	Repository struct {
		FullName string `json:"full_name"`
	} `json:"repository"`
}

// maxPollPages 是单轮询翻页上限（防止异常大积压把一拍拖垮；每页 per_page=50，
// 上限 20 页 = 1000 条，远超正常 poll 间隔内的通知量，仍有界）。
const maxPollPages = 20

// pollPerPage 是每页条数（GitHub notifications 默认 50，最大 100）。
const pollPerPage = "50"

// Poll 增量拉取通知：GET /notifications?all=false&per_page=50[&since=<cursor>]，
// **跟随 Link 头 rel="next" 翻完所有页**（#5）。GitHub 按 updated_at 降序返回，单页容量有限；
// 若只取第一页并把游标推到本页最大 updated_at，则第 2 页及更旧的事件会被下拍 since 过滤器
// 永久跳过（丢事件）。这里跨所有页累积事件，游标只取「全部页」的最大 updated_at。
// 返回原始事件、推进后的新游标（全页最大 updated_at，无新事件则沿用旧游标）、错误。
// 304（未变更，仅可能出现在首页）视作空批。任一页出错即整轮失败并沿用旧游标（不推进，下拍重试）。
func (g *GitHubConnector) Poll(ctx context.Context, conn Conn) ([]RawEvent, string, error) {
	q := url.Values{"all": {"false"}, "per_page": {pollPerPage}}
	if conn.Cursor != "" {
		q.Set("since", conn.Cursor)
	}
	nextURL := g.baseURL + "/notifications?" + q.Encode()

	events := []RawEvent{}
	newCursor := conn.Cursor
	for page := 0; page < maxPollPages && nextURL != ""; page++ {
		req, err := g.newRequestURL(ctx, nextURL, conn.PAT)
		if err != nil {
			return nil, conn.Cursor, err
		}
		notifs, link, status, err := g.fetchPage(req)
		if err != nil {
			return nil, conn.Cursor, err
		}
		if status == http.StatusNotModified { // 首页 304：空批沿用旧游标
			return events, conn.Cursor, nil
		}
		for _, n := range notifs {
			events = append(events, RawEvent{
				ID:          n.ID,
				SubjectType: n.Subject.Type,
				Reason:      n.Reason,
				Title:       n.Subject.Title,
				URL:         n.Subject.URL,
				Repo:        n.Repository.FullName,
				UpdatedAt:   n.UpdatedAt,
			})
			if n.UpdatedAt > newCursor { // RFC3339 字典序 == 时间序
				newCursor = n.UpdatedAt
			}
		}
		nextURL = parseNextLink(link)
	}
	return events, newCursor, nil
}

// fetchPage 发一页请求并解码；返回该页通知、Link 头、HTTP 状态码、错误。
func (g *GitHubConnector) fetchPage(req *http.Request) ([]githubNotification, string, int, error) {
	resp, err := g.http.Do(req)
	if err != nil {
		return nil, "", 0, fmt.Errorf("connector: poll request: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotModified {
		return nil, "", http.StatusNotModified, nil
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))
		return nil, "", resp.StatusCode, fmt.Errorf("connector: poll failed: github status %d", resp.StatusCode)
	}
	var notifs []githubNotification
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&notifs); err != nil {
		return nil, "", resp.StatusCode, fmt.Errorf("connector: decode notifications: %w", err)
	}
	return notifs, resp.Header.Get("Link"), resp.StatusCode, nil
}

// parseNextLink 从 RFC 5988 Link 头（GitHub 分页）解析 rel="next" 的 URL；无则返回空串。
// 形如：`<https://api.github.com/…?page=2>; rel="next", <…?page=5>; rel="last"`。
func parseNextLink(link string) string {
	if link == "" {
		return ""
	}
	for _, seg := range strings.Split(link, ",") {
		parts := strings.Split(seg, ";")
		if len(parts) < 2 {
			continue
		}
		isNext := false
		for _, p := range parts[1:] {
			p = strings.TrimSpace(p)
			if p == `rel="next"` || p == "rel=next" {
				isNext = true
				break
			}
		}
		if !isNext {
			continue
		}
		u := strings.TrimSpace(parts[0])
		u = strings.TrimPrefix(u, "<")
		u = strings.TrimSuffix(u, ">")
		if u != "" {
			return u
		}
	}
	return ""
}

// Normalize 规整成内部事件。event_type 用 subject.type 归一：
// 'Issue'→'issue'、'PullRequest'→'pull_request'（其它转小写）。
// Fields 暴露 title/url/repo/reason/type/author（notifications 无正文/作者，body/author 留空）。
func (g *GitHubConnector) Normalize(raw RawEvent) InternalEvent {
	return InternalEvent{
		Type: normalizeSubjectType(raw.SubjectType),
		Fields: map[string]string{
			"title":  raw.Title,
			"body":   "",
			"url":    webURL(raw.URL),
			"repo":   raw.Repo,
			"reason": raw.Reason,
			"type":   raw.SubjectType,
			"author": "",
		},
	}
}

func normalizeSubjectType(t string) string {
	switch t {
	case "Issue":
		return "issue"
	case "PullRequest":
		return "pull_request"
	default:
		return strings.ToLower(t)
	}
}

// webURL 把 GitHub API url（api.github.com/repos/o/r/issues/5）转成网页 url
// （github.com/o/r/issues/5），便于模板里放可点链接。非 API url 原样返回。
func webURL(apiURL string) string {
	const marker = "://api.github.com/repos/"
	i := strings.Index(apiURL, marker)
	if i < 0 {
		return apiURL
	}
	scheme := apiURL[:i]
	rest := apiURL[i+len(marker):]
	rest = strings.Replace(rest, "/pulls/", "/pull/", 1)
	return scheme + "://github.com/" + rest
}
