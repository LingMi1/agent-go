// web_search 工具观察串的前后端契约（与 cognition/cognition/tools/web_search.py 对齐）：
// 观察串 = LLM 可读的编号列表 + 末行单行哨兵 JSON。前端按哨兵解析出结构化来源列表，
// 展示层（ToolRow 搜索迷你卡 / 工作区来源组）用它渲染，可读文本用 stripWebSearchJson 展示。
// 契约走 TOOL_RESULT 事件原样透传（落账本原样重放），replay ≡ live 天然成立。

export const WEB_SEARCH_JSON_PREFIX = "WEB_SEARCH_RESULTS_JSON:";

export interface WebSearchSource {
  title: string;
  url: string;
  snippet: string;
}

export interface WebSearchPayload {
  query: string;
  provider: string;
  results: WebSearchSource[];
}

/** 从 web_search 观察串解析结构化结果；无哨兵/解析失败一律返回 null（容错，不抛）。 */
export function parseWebSearchResult(text: string | undefined | null): WebSearchPayload | null {
  if (!text) return null;
  const line = text
    .split("\n")
    .map((l) => l.trim())
    .find((l) => l.startsWith(WEB_SEARCH_JSON_PREFIX));
  if (!line) return null;
  try {
    const parsed = JSON.parse(line.slice(WEB_SEARCH_JSON_PREFIX.length)) as Partial<WebSearchPayload>;
    if (typeof parsed !== "object" || parsed === null || !Array.isArray(parsed.results)) return null;
    return {
      query: typeof parsed.query === "string" ? parsed.query : "",
      provider: typeof parsed.provider === "string" ? parsed.provider : "",
      results: parsed.results
        .filter((r): r is WebSearchSource => typeof r === "object" && r !== null && typeof (r as WebSearchSource).url === "string")
        .map((r) => ({
          title: typeof r.title === "string" ? r.title : r.url,
          url: r.url,
          snippet: typeof r.snippet === "string" ? r.snippet : "",
        })),
    };
  } catch {
    return null;
  }
}

/** 展示用：去掉观察串里的哨兵行（保留人类可读部分）。 */
export function stripWebSearchJson(text: string): string {
  return text
    .split("\n")
    .filter((l) => !l.trim().startsWith(WEB_SEARCH_JSON_PREFIX))
    .join("\n")
    .trimEnd();
}

/** URL → hostname（来源 chip 展示用）；解析失败回退原串截断。 */
export function sourceHostname(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url.slice(0, 30);
  }
}
