import { describe, expect, it } from "vitest";
import { Calculator, BookOpen, Globe, Plug, Search, Wrench } from "lucide-react";
import { WEB_SEARCH_JSON_PREFIX } from "./sse/toolPayloads";
import { countCitations, toolMeta } from "./toolPresentation";

describe("toolMeta 映射", () => {
  it("calculator：图标/动词/expression target 截断", () => {
    const m = toolMeta("calculator", "local");
    expect(m.icon).toBe(Calculator);
    expect(m.kind).toBe("generic");
    expect(m.runningVerb()).toBe("正在计算…");
    expect(m.doneVerb()).toBe("计算完成");
    expect(m.target?.({ expression: "1+1" })).toBe("1+1");
    const long = "9".repeat(60);
    expect(m.target?.({ expression: long })).toBe(`${"9".repeat(48)}…`);
    expect(m.target?.({})).toBeUndefined();
  });

  it("input 为 JSON 串同样可解析；坏 JSON 安全降级", () => {
    const m = toolMeta("calculator", "local");
    expect(m.target?.('{"expression":"2*3"}')).toBe("2*3");
    expect(m.target?.("{bad json")).toBeUndefined();
    expect(m.target?.(undefined)).toBeUndefined();
    expect(m.target?.(42)).toBeUndefined();
  });

  it("write_report / code_interpreter / image_generate 动词与 kind", () => {
    expect(toolMeta("write_report", "local").doneVerb()).toBe("报告已生成");
    expect(toolMeta("code_interpreter", "local").runningVerb()).toBe("正在执行代码…");
    const img = toolMeta("image_generate", "local");
    expect(img.kind).toBe("image");
    expect(img.runningVerb()).toBe("正在生成图片…");
    expect(img.doneVerb()).toBe("已生成图片");
  });

  it("knowledge_search：resultText 里数〔n〕引用", () => {
    const m = toolMeta("knowledge_search", "local");
    expect(m.runningVerb()).toBe("Searching knowledge base…");
    expect(m.doneVerb(undefined, "答案A〔1〕，答案B〔2〕，再提〔1〕")).toBe("检索完成 · 2 段引用");
    expect(m.doneVerb(undefined, "没有引用标记")).toBe("检索完成");
    expect(m.doneVerb(undefined, undefined)).toBe("检索完成");
  });

  it("web_fetch：从 input.url 提 hostname（含 JSON 串与非法 URL 降级）", () => {
    const m = toolMeta("web_fetch", "local");
    expect(m.icon).toBe(Globe);
    expect(m.runningVerb({ url: "https://www.example.com/a/b" })).toBe("正在抓取 example.com…");
    expect(m.doneVerb('{"url":"https://docs.python.org/3/"}')).toBe("抓取完成 · docs.python.org");
    expect(m.runningVerb({ url: "not a url" })).toBe("正在抓取…");
    expect(m.doneVerb({})).toBe("抓取完成");
  });

  it("script_runner：技能名动词 + chart-visualization 特例", () => {
    const m = toolMeta("script_runner", "skill");
    expect(m.runningVerb({ skill: "ppt-builder" })).toBe("正在运行技能 ppt-builder…");
    expect(m.doneVerb({ skill: "ppt-builder" })).toBe("技能 ppt-builder 完成");
    expect(m.doneVerb({ skill: "chart-visualization" })).toBe("图表已生成");
    expect(m.runningVerb({})).toBe("正在运行技能…");
  });

  it("skill 文档族 → BookOpen 统一动词", () => {
    for (const name of ["skill", "skill_read", "skill_list", "skill_glob", "skill_grep"]) {
      const m = toolMeta(name, "skill");
      expect(m.icon).toBe(BookOpen);
      expect(m.runningVerb()).toBe("正在查阅技能文档…");
      expect(m.doneVerb()).toBe("已查阅技能文档");
    }
  });

  it("web_search：kind search、query 入动词、N 用哨兵 JSON 解析", () => {
    const m = toolMeta("web_search", "local");
    expect(m.icon).toBe(Search);
    expect(m.kind).toBe("search");
    expect(m.runningVerb({ query: "LangGraph checkpoint" })).toBe("正在搜索「LangGraph checkpoint」…");
    expect(m.runningVerb({})).toBe("正在搜索…");
    const sentinel =
      WEB_SEARCH_JSON_PREFIX +
      JSON.stringify({
        query: "q",
        provider: "tavily",
        results: [
          { title: "A", url: "https://a.com", snippet: "" },
          { title: "B", url: "https://b.com", snippet: "" },
        ],
      });
    expect(m.doneVerb(undefined, `1. A\n2. B\n${sentinel}`)).toBe("搜索完成 · 2 条来源");
    expect(m.doneVerb(undefined, "无哨兵观察串")).toBe("搜索完成");
  });

  it("provider=mcp 未知工具 → Plug 调用动词", () => {
    const m = toolMeta("github_create_issue", "mcp");
    expect(m.icon).toBe(Plug);
    expect(m.runningVerb()).toBe("正在调用 github_create_issue…");
    expect(m.doneVerb()).toBe("github_create_issue 完成");
  });

  it("命名映射优先于 mcp provider", () => {
    expect(toolMeta("calculator", "mcp").icon).toBe(Calculator);
  });

  it("fallback → Wrench 通用动词", () => {
    const m = toolMeta("mystery_tool", "local");
    expect(m.icon).toBe(Wrench);
    expect(m.kind).toBe("generic");
    expect(m.runningVerb()).toBe("正在运行 mystery_tool…");
    expect(m.doneVerb()).toBe("mystery_tool 完成");
  });
});

describe("countCitations", () => {
  it("去重计数〔n〕标记", () => {
    expect(countCitations("〔1〕〔2〕〔3〕〔2〕")).toBe(3);
    expect(countCitations("")).toBe(0);
    expect(countCitations(undefined)).toBe(0);
  });
});
