import { type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
// 代码高亮双主题样式在 styles/hljs.css（随 index.css 引入，.dark 切换）
import remarkGfm from "remark-gfm";
import { Badge } from "@/components/ui/badge";
import {
  Collapsible as UICollapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";

export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-sm leading-relaxed break-words [&_pre]:overflow-auto [&_pre]:rounded-lg [&_pre]:bg-code-bg [&_pre]:p-3 [&_:not(pre)>code]:bg-code-bg [&_:not(pre)>code]:text-foreground/90 [&_:not(pre)>code]:font-mono [&_:not(pre)>code]:text-[0.9em] [&_pre_code]:bg-transparent [&_a]:text-primary [&_a]:underline [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_h1]:font-semibold [&_h2]:font-semibold [&_h3]:font-semibold">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>{children}</ReactMarkdown>
    </div>
  );
}

// 换装说明：导出名与 props 签名保持 M6 原样（调用方 chat.tsx 零改动），
// 内部改为 shadcn/Radix Collapsible；▶ caret 用 data-state 驱动旋转。
export function Collapsible({
  title,
  defaultOpen = false,
  children,
  right,
  ghost = false,
}: {
  title: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
  right?: ReactNode;
  /** ghost：无边框轻量形态（思考行等过程性内容，与紧凑工具行同一视觉份量） */
  ghost?: boolean;
}) {
  return (
    <UICollapsible defaultOpen={defaultOpen} className={ghost ? "rounded-lg" : "rounded-xl border bg-card"}>
      <CollapsibleTrigger
        className={`group flex w-full items-center gap-2 text-left text-sm ${
          ghost ? "rounded-md px-1.5 py-1 hover:bg-accent/50" : "px-3 py-2 hover:bg-accent"
        }`}
      >
        <ChevronRight className="size-3.5 shrink-0 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
        <span className="min-w-0 flex-1 truncate">{title}</span>
        {right}
      </CollapsibleTrigger>
      <CollapsibleContent className={ghost ? "py-1 pr-2 pl-7 text-sm" : "border-t px-3 py-2 text-sm"}>
        {children}
      </CollapsibleContent>
    </UICollapsible>
  );
}

const STATUS_STYLE: Record<string, string> = {
  running: "bg-info/10 text-info border-info/25",
  success: "bg-success/10 text-success border-success/25",
  failed: "bg-destructive/10 text-destructive border-destructive/25",
  awaiting_approval: "bg-warning/10 text-warning border-warning/25", // M11 HITL
};

export function ToolStatusBadge({ status }: { status: string }) {
  const cls = STATUS_STYLE[status] ?? "bg-accent/50 text-muted-foreground border";
  return (
    <Badge className={`gap-1 rounded-full font-normal ${cls}`}>
      {status === "running" && <span className="pulse-dot">●</span>}
      {status === "awaiting_approval" ? "待审批" : status}
    </Badge>
  );
}

export function ProviderTag({ provider }: { provider: string }) {
  const cls =
    provider === "mcp"
      ? "bg-mcp/10 text-mcp"
      : provider === "skill"
        ? "bg-warning/10 text-warning"
        : "bg-accent/50 text-muted-foreground";
  return (
    <Badge variant="secondary" className={`rounded-md px-1.5 py-0.5 text-[10px] uppercase ${cls}`}>
      {provider}
    </Badge>
  );
}
