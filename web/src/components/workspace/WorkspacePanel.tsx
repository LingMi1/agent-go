// 智能体工作区：动态（产物翻页预览 + 可拖拽分隔 + 时序 feed）/ 文件（Popover 选择器 + 全高预览）双页签。
// focus 由外部（B5 集成：工具卡/来源 chip 点击）驱动：artifact → 切文件页签并选中；
// sources → 切动态页签滚到对应组；消费后回调 onFocusConsumed()（父层清空，一次性）。
// memo：ChatView 流式帧高频重渲，入参引用稳定时整个工作区（含 iframe/图表）不重渲。

import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";
import type { ArtifactRef } from "../../lib/sse/frameTypes";
import type { AttachmentRef } from "../../lib/api/client";
import type { ActivityItem } from "../../lib/workspaceFeed";
import { ActivityTab } from "./ActivityTab";
import { FilesTab } from "./FilesTab";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export type WorkspaceFocus =
  | { kind: "artifact"; resourceKey: string }
  | { kind: "sources"; toolCallId: string };

export const WorkspacePanel = memo(function WorkspacePanel({
  artifacts,
  uploads,
  activity,
  focus,
  onFocusConsumed,
  onClose,
}: {
  artifacts: ArtifactRef[];
  uploads: AttachmentRef[];
  activity: ActivityItem[];
  focus?: WorkspaceFocus | null;
  onFocusConsumed: () => void;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<"activity" | "files">("activity");
  const [fileKey, setFileKey] = useState<string | null>(null); // 文件页签选中项
  const [scrollTo, setScrollTo] = useState<string | null>(null); // 动态页签滚动目标（toolCallId）
  const onScrolled = useCallback(() => setScrollTo(null), []);

  // focus 变化：artifact → 文件页签+选中；sources → 动态页签+滚到组；随即消费。
  // resourceKey 在 artifacts 与 uploads（消息气泡缩略图点击）全集中匹配：
  // FilesTab 的查找集合就是 [artifacts, uploads]，两类 key 都能选中显示；
  // 都找不到时 FilesTab 回落最新产物，页签切换仍成立。
  useEffect(() => {
    if (!focus) return;
    if (focus.kind === "artifact") {
      setTab("files");
      setFileKey(focus.resourceKey);
    } else {
      setTab("activity");
      setScrollTo(focus.toolCallId);
    }
    onFocusConsumed();
  }, [focus, onFocusConsumed]);

  // resourceKey → 产出工具名（供文件选择器展示来源；activity 里 artifact 项自带 toolName）。
  const toolNameByKey = useMemo(() => {
    const m = new Map<string, string>();
    for (const it of activity) {
      if (it.kind === "artifact" && it.toolName) m.set(it.art.resourceKey, it.toolName);
    }
    return m;
  }, [activity]);

  // 新产物到达："生成即所见"，文件页签清手动选择回落最新（与 FilesPanel 同规则）。
  const prevLen = useRef(artifacts.length);
  useEffect(() => {
    if (artifacts.length > prevLen.current) setFileKey(null);
    prevLen.current = artifacts.length;
  }, [artifacts.length]);

  return (
    <div className="flex h-full flex-col border-l bg-background">
      <div className="flex items-center gap-1.5 border-b px-3 py-2">
        <span className="min-w-0 flex-1 truncate text-sm text-foreground">智能体工作区</span>
        <button
          onClick={onClose}
          aria-label="关闭"
          className="rounded p-1.5 text-muted-foreground transition-colors hover:text-foreground"
        >
          <X className="size-4" />
        </button>
      </div>
      <Tabs
        value={tab}
        onValueChange={(v) => setTab(v as "activity" | "files")}
        className="min-h-0 flex-1 gap-0"
      >
        <TabsList data-testid="workspace-tabs" className="mx-2 mt-2 grid w-auto grid-cols-2 self-stretch">
          <TabsTrigger value="activity">动态</TabsTrigger>
          <TabsTrigger value="files">文件</TabsTrigger>
        </TabsList>
        <TabsContent value="activity" className="mt-2 min-h-0 flex-1">
          <ActivityTab
            artifacts={artifacts}
            activity={activity}
            scrollTo={scrollTo}
            onScrolled={onScrolled}
          />
        </TabsContent>
        <TabsContent value="files" className="mt-2 min-h-0 flex-1">
          <FilesTab
            artifacts={artifacts}
            uploads={uploads}
            selectedKey={fileKey}
            onSelect={setFileKey}
            toolNameByKey={toolNameByKey}
          />
        </TabsContent>
      </Tabs>
    </div>
  );
});
