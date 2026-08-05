import { describe, expect, it } from "vitest";
import {
  WEB_SEARCH_JSON_PREFIX,
  parseWebSearchResult,
  sourceHostname,
  stripWebSearchJson,
} from "./toolPayloads";

// 与 cognition/tests/test_web_search.py 的 format_observation 输出结构对齐的 fixture
const OBSERVATION = [
  "搜索「2026 国产大模型」共 2 条结果（tavily）：",
  "1. DeepSeek 发布新一代模型",
  "   https://example.com/a",
  "   摘要 A",
  "2. 通义千问更新",
  "   https://example.com/b",
  "   摘要 B",
  "",
  `${WEB_SEARCH_JSON_PREFIX}{"query":"2026 国产大模型","provider":"tavily","results":[{"title":"DeepSeek 发布新一代模型","url":"https://example.com/a","snippet":"摘要 A"},{"title":"通义千问更新","url":"https://example.com/b","snippet":"摘要 B"}]}`,
].join("\n");

describe("parseWebSearchResult", () => {
  it("解析哨兵 JSON（中文原样）", () => {
    const p = parseWebSearchResult(OBSERVATION);
    expect(p).not.toBeNull();
    expect(p!.query).toBe("2026 国产大模型");
    expect(p!.provider).toBe("tavily");
    expect(p!.results).toHaveLength(2);
    expect(p!.results[0]).toEqual({
      title: "DeepSeek 发布新一代模型",
      url: "https://example.com/a",
      snippet: "摘要 A",
    });
  });

  it("无哨兵 / 空入参 / 坏 JSON → null", () => {
    expect(parseWebSearchResult("普通工具输出")).toBeNull();
    expect(parseWebSearchResult("")).toBeNull();
    expect(parseWebSearchResult(undefined)).toBeNull();
    expect(parseWebSearchResult(`${WEB_SEARCH_JSON_PREFIX}{broken`)).toBeNull();
    expect(parseWebSearchResult(`${WEB_SEARCH_JSON_PREFIX}{"query":"x"}`)).toBeNull(); // 缺 results
  });

  it("results 项容错：缺 title 回退 url，非对象项过滤", () => {
    const p = parseWebSearchResult(
      `${WEB_SEARCH_JSON_PREFIX}{"query":"q","provider":"ddg","results":[{"url":"https://x.cn/1"},null,{"nope":1}]}`,
    );
    expect(p!.results).toEqual([{ title: "https://x.cn/1", url: "https://x.cn/1", snippet: "" }]);
  });
});

describe("stripWebSearchJson", () => {
  it("去掉哨兵行保留可读文本", () => {
    const s = stripWebSearchJson(OBSERVATION);
    expect(s).toContain("搜索「2026 国产大模型」");
    expect(s).not.toContain(WEB_SEARCH_JSON_PREFIX);
    expect(s.endsWith("摘要 B")).toBe(true);
  });
});

describe("sourceHostname", () => {
  it("取 hostname 去 www；坏 URL 截断兜底", () => {
    expect(sourceHostname("https://www.zhihu.com/question/1")).toBe("zhihu.com");
    expect(sourceHostname("not a url")).toBe("not a url");
  });
});
