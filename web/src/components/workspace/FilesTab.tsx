// 工作区「文件」页签：全高只渲染当前选中文件的 ArtifactPreview；
// 顶部紧凑头部（当前文件名 ▾ · ARTIFACTS n · 上传 m）点击弹出 Popover 文件选择器
// （Artifacts / 上传内容两段可滚动列表，点击项即切换并关闭）。
// 选中态由父层（WorkspacePanel）托管，focus(artifact) 在 artifacts 与 uploads 全集中匹配。

import { useMemo, useState } from "react";
import { BarChart3, ChevronDown, FileText, Image as ImageIcon } from "lucide-react";
import type { ArtifactRef } from "../../lib/sse/frameTypes";
import type { AttachmentRef } from "../../lib/api/client";
import { isChartArtifact } from "../../lib/workspaceFeed";
import { ArtifactPreview } from "../ArtifactWorkspace";
import { FileCard, uploadToRef } from "../FilesPanel";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

function human(size: number) {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

// 选择器里的产物项：类型小图标（图表/图片/文件）+ 文件名 + 大小 + 来源工具名；选中高亮。
function ArtifactPickRow({
  art,
  toolName,
  active,
  onClick,
}: {
  art: ArtifactRef;
  toolName?: string;
  active: boolean;
  onClick: () => void;
}) {
  const name = art.fileName || art.name;
  const mime = art.mimeType || "";
  const Icon = isChartArtifact({ name, mimeType: mime })
    ? BarChart3
    : mime.startsWith("image/")
      ? ImageIcon
      : FileText;
  return (
    <button
      onClick={onClick}
      title={name}
      className={`flex w-full items-center gap-2 rounded-lg border px-2 py-1.5 text-left transition-colors ${
        active ? "border-primary/50 bg-primary/10" : "border-border/60 hover:border-border hover:bg-accent/50"
      }`}
    >
      <Icon className="size-4 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs text-foreground">{name}</span>
        <span className="block truncate text-[10px] text-muted-foreground">
          {human(art.size)}
          {toolName ? ` · ${toolName}` : ""}
        </span>
      </span>
    </button>
  );
}

export function FilesTab({
  artifacts,
  uploads,
  selectedKey,
  onSelect,
  toolNameByKey,
}: {
  artifacts: ArtifactRef[];
  uploads: AttachmentRef[];
  selectedKey: string | null;
  onSelect: (resourceKey: string) => void;
  toolNameByKey?: Map<string, string>; // resourceKey → 产出工具名（父层从 activity 汇总）
}) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const uploadRefs = useMemo(() => uploads.map(uploadToRef), [uploads]);
  const all = useMemo(() => [...artifacts, ...uploadRefs], [artifacts, uploadRefs]);
  // 默认选中：最新一份产物；无产物则第一份上传（与 FilesPanel 一致）。
  const selected =
    all.find((a) => a.resourceKey === selectedKey) ??
    (artifacts.length > 0 ? artifacts[artifacts.length - 1] : uploadRefs[0]);
  const total = all.length;

  if (total === 0)
    return (
      <div className="flex h-full items-center justify-center p-6 text-center text-sm leading-relaxed text-muted-foreground">
        Artifacts and uploaded files from this chat
        <br />
        are shown here for preview and download.
      </div>
    );

  const pick = (resourceKey: string) => {
    onSelect(resourceKey);
    setPickerOpen(false);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <Popover open={pickerOpen} onOpenChange={setPickerOpen}>
        <PopoverTrigger asChild>
          <button
            aria-label="选择文件"
            className="flex w-full shrink-0 items-center gap-1.5 border-b px-3 py-2 text-left transition-colors hover:bg-accent/50"
          >
            <span className="min-w-0 flex-1 truncate text-xs text-foreground">
              {selected ? selected.fileName || selected.name : "选择文件"}
            </span>
            <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
            <span className="shrink-0 text-[10px] tracking-wide text-muted-foreground uppercase">
              {artifacts.length > 0 ? ` · Artifacts ${artifacts.length}` : ""}
              {uploadRefs.length > 0 ? ` · Uploaded ${uploadRefs.length}` : ""}
            </span>
          </button>
        </PopoverTrigger>
        <PopoverContent align="start" sideOffset={2} className="w-(--radix-popover-trigger-width) p-0">
          <div className="max-h-80 space-y-1 overflow-y-auto p-2">
            {artifacts.length > 0 && (
              <>
                <div className="px-1 pt-0.5 pb-1 text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
                  Artifacts · {artifacts.length}
                </div>
                {artifacts.map((a) => (
                  <ArtifactPickRow
                    key={a.resourceKey}
                    art={a}
                    toolName={toolNameByKey?.get(a.resourceKey)}
                    active={selected?.resourceKey === a.resourceKey}
                    onClick={() => pick(a.resourceKey)}
                  />
                ))}
              </>
            )}
            {uploadRefs.length > 0 && (
              <>
                <div className="px-1 pt-1.5 pb-1 text-[10px] font-medium tracking-wide text-muted-foreground uppercase">
                  Uploads · {uploadRefs.length}
                </div>
                {uploadRefs.map((a) => (
                  <FileCard
                    key={a.resourceKey}
                    art={a}
                    active={selected?.resourceKey === a.resourceKey}
                    onClick={() => pick(a.resourceKey)}
                  />
                ))}
              </>
            )}
          </div>
        </PopoverContent>
      </Popover>
      <div className="min-h-0 flex-1">{selected && <ArtifactPreview art={selected} />}</div>
    </div>
  );
}
