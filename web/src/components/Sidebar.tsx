import { useEffect, useRef, useState } from "react";
import {
  CalendarClock,
  ChartColumn,
  Database,
  FolderOpen,
  GitBranch,
  LogOut,
  ImagePlus,
  MessageSquare,
  Moon,
  PanelLeft,
  Plug,
  Plus,
  Shield,
  Sun,
  Trash2,
} from "lucide-react";
import { toggleTheme, useTheme } from "@/lib/theme";
import { BrandMark } from "./BrandMark";
import type { SessionView } from "../lib/sessions";
import type { NavView } from "../lib/uiPrefs";
import type { HealthReport } from "../lib/api/client";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

// 侧栏 = 导航中枢（对齐 Claude 官网）：品牌+折叠 / 新对话 / 导航项(对话·生图·产物·知识库·定时·连接器·管理)
// / 会话列表（仅"对话"视图下）/ 账号底栏（健康·成本·用户·退出）。顶栏已移除。
// 折叠态 = 窄图标条 SidebarRail（同一 NAV_ITEMS，只留图标+tooltip）——展开与折叠共用一份导航定义。

// 导航项单一事实源：展开态渲染为带文字的行、折叠态渲染为图标钮，避免两处漂移。
type NavDef = { view: NavView; label: string; icon: React.ReactNode; adminOnly?: boolean };
const NAV_ITEMS: NavDef[] = [
  { view: "chat", label: "Chat", icon: <MessageSquare className="size-4" /> },
  { view: "generate", label: "Generate", icon: <ImagePlus className="size-4" /> },
  { view: "artifacts", label: "Artifacts", icon: <FolderOpen className="size-4" /> },
  { view: "kb", label: "Knowledge", icon: <Database className="size-4" /> },
  { view: "schedules", label: "Schedules", icon: <CalendarClock className="size-4" /> },
  { view: "connectors", label: "Connectors", icon: <Plug className="size-4" /> },
  { view: "admin", label: "Admin", icon: <Shield className="size-4" />, adminOnly: true },
];

function NavItem({
  icon,
  label,
  active,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  active?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm transition-colors ${
        active ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
      }`}
    >
      {icon}
      {label}
    </button>
  );
}

// 折叠态图标钮：仅图标 + tooltip（label），active 高亮。
function RailButton({
  label,
  active,
  onClick,
  children,
  "aria-label": ariaLabel,
}: {
  label: string;
  active?: boolean;
  onClick: () => void;
  children: React.ReactNode;
  "aria-label"?: string;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          onClick={onClick}
          aria-label={ariaLabel ?? label}
          className={`flex size-9 items-center justify-center rounded-lg transition-colors ${
            active ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
          }`}
        >
          {children}
        </button>
      </TooltipTrigger>
      <TooltipContent side="right">{label}</TooltipContent>
    </Tooltip>
  );
}

// 折叠态窄图标条（w-14）：展开钮 / 新对话 / 导航图标 / 账号底栏图标。
// 修复"折叠后完全没有图标"——折叠不再是单个悬浮钮，而是保留完整导航的图标形态（对齐 Claude）。
export function SidebarRail({
  activeNav,
  onNavChange,
  onNewSession,
  onExpand,
  health,
  isAdmin,
  onOpenUsage,
  onLogout,
}: {
  activeNav: NavView;
  onNavChange: (v: NavView) => void;
  onNewSession: () => void;
  onExpand: () => void;
  health: HealthReport | null;
  isAdmin: boolean;
  onOpenUsage: () => void;
  onLogout: () => void;
}) {
  const healthColor = !health ? "bg-muted-foreground/50" : health.healthy ? "bg-success" : "bg-destructive";
  const theme = useTheme();
  return (
    <aside className="flex w-14 shrink-0 flex-col items-center gap-1 border-r bg-background py-3">
      <RailButton label="Expand sidebar" onClick={onExpand}>
        <PanelLeft className="size-4" />
      </RailButton>
      <RailButton label="New Chat" onClick={onNewSession}>
        <Plus className="size-4 text-primary" />
      </RailButton>
      <div className="my-1 h-px w-6 bg-border" />
      {NAV_ITEMS.filter((n) => !n.adminOnly || isAdmin).map((n) => (
        <RailButton key={n.view} label={n.label} active={activeNav === n.view} onClick={() => onNavChange(n.view)}>
          {n.icon}
        </RailButton>
      ))}
      <div className="mt-auto flex flex-col items-center gap-1">
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="flex size-9 items-center justify-center" aria-label="Health status">
              <span className={`h-2 w-2 rounded-full ${healthColor}`} />
            </span>
          </TooltipTrigger>
          <TooltipContent side="right">{!health ? "Checking" : health.healthy ? "Healthy" : "Unhealthy"}</TooltipContent>
        </Tooltip>
        <RailButton label={theme === "dark" ? "Light mode" : "Dark mode"} onClick={toggleTheme}>
          {theme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}
        </RailButton>
        <RailButton label="Usage & Cost" onClick={onOpenUsage}>
          <ChartColumn className="size-4" />
        </RailButton>
        <RailButton label="Logout" onClick={onLogout}>
          <LogOut className="size-4" />
        </RailButton>
      </div>
    </aside>
  );
}

export // 会话行：hover 出删除钮，两步确认（再点一次才真删）。
function SessionRow({
  s,
  active,
  onSelect,
  onDelete,
}: {
  s: SessionView;
  active: boolean;
  onSelect: () => void;
  onDelete: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);
  return (
    <div
      className={`group relative mb-1 rounded-lg border transition-colors ${
        active ? "border-primary/40 bg-primary/10" : "border-transparent hover:bg-accent/50"
      }`}
    >
      <button onClick={onSelect} className="block w-full overflow-hidden p-2 pr-8 text-left">
        <div className="flex min-w-0 items-center gap-1">
          {/* docs/14：分叉会话标记（继承父会话历史的新时间线） */}
          {s.forkedFrom && (
            <span title="Fork session" className="shrink-0 text-muted-foreground/70">
              <GitBranch className="size-3" />
            </span>
          )}
          <div className="min-w-0 flex-1 truncate text-sm text-foreground">{s.title || "(Untitled)"}</div>
        </div>
        <div className="mt-0.5 flex items-center gap-1 text-[10px] text-muted-foreground/70">
          <span>{s.agentType}</span>
          <span>·</span>
          <span>{s.pendingLocal ? "New session" : `${s.runCount} turns`}</span>
        </div>
      </button>
      <button
        onClick={(e) => {
          e.stopPropagation();
          if (!confirming) {
            setConfirming(true);
            if (timerRef.current) clearTimeout(timerRef.current);
            timerRef.current = setTimeout(() => setConfirming(false), 3000);
            return;
          }
          onDelete();
        }}
        title={confirming ? "Click again to confirm delete" : "Delete session"}
        className={`absolute right-1.5 top-1.5 rounded p-1 transition-colors ${
          confirming
            ? "bg-destructive/20 text-destructive"
            : "text-muted-foreground/70 opacity-0 hover:text-destructive group-hover:opacity-100"
        }`}
      >
        <Trash2 className="size-3.5" />
      </button>
    </div>
  );
}

export function Sidebar({
  sessions,
  currentSessionId,
  activeNav,
  onNavChange,
  onNewSession,
  onSelectSession,
  onDeleteSession,
  onToggleSidebar,
  health,
  userId,
  isAdmin,
  onOpenUsage,
  onLogout,
}: {
  sessions: SessionView[];
  currentSessionId: string | null;
  activeNav: NavView;
  onNavChange: (v: NavView) => void;
  onNewSession: () => void;
  onSelectSession: (id: string) => void;
  onDeleteSession: (id: string) => void;
  onToggleSidebar: () => void;
  health: HealthReport | null;
  userId: string;
  isAdmin: boolean;
  onOpenUsage: () => void;
  onLogout: () => void;
}) {
  const theme = useTheme();
  const healthColor = !health ? "bg-muted-foreground/50" : health.healthy ? "bg-success" : "bg-destructive";
  const healthLabel = !health ? "Checking" : health.healthy ? "Healthy" : "Unhealthy";
  const healthDetail = health
    ? Object.entries(health.checks)
        .map(([k, v]) => `${k}: ${v}`)
        .join("\n")
    : "";

  return (
    <aside className="flex w-64 shrink-0 flex-col border-r bg-background">
      {/* 品牌 + 折叠 */}
      <div className="flex items-center gap-2 px-3 py-3">
        <BrandMark className="size-6" />
        <span className="flex-1 font-display text-lg font-semibold tracking-tight">
          Agent-go
        </span>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              onClick={onToggleSidebar}
              aria-label="Collapse sidebar"
              className="size-7 text-muted-foreground hover:text-foreground"
            >
              <PanelLeft />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Collapse sidebar</TooltipContent>
        </Tooltip>
      </div>

      {/* 新对话 + 导航项 */}
      <div className="space-y-0.5 px-2">
        <Button
          onClick={onNewSession}
          className="mb-1 w-full justify-start gap-2 bg-primary text-primary-foreground hover:bg-primary/85"
        >
          <Plus className="size-4" />
          New Chat
        </Button>
        {NAV_ITEMS.filter((n) => !n.adminOnly || isAdmin).map((n) => (
          <NavItem
            key={n.view}
            icon={n.icon}
            label={n.label}
            active={activeNav === n.view}
            onClick={() => onNavChange(n.view)}
          />
        ))}
      </div>

      {/* 会话列表（仅"对话"视图下显示） */}
      {activeNav === "chat" && (
        <>
          <div className="px-3 pt-3 pb-1 text-[10px] font-medium tracking-wide text-muted-foreground/60 uppercase">
            Recent Chats
          </div>
          {/* viewport 子层默认 display:table 会被长标题撑宽（横向滚动 bug），覆写为 block 让 truncate 生效 */}
          <ScrollArea className="min-h-0 flex-1 [&_[data-radix-scroll-area-viewport]>div]:!block">
            <div className="px-2 pb-2">
              {sessions.length === 0 && <div className="px-2 py-3 text-xs text-muted-foreground/70">No sessions yet</div>}
              {sessions.map((s) => (
                <SessionRow
                  key={s.id}
                  s={s}
                  active={s.id === currentSessionId}
                  onSelect={() => onSelectSession(s.id)}
                  onDelete={() => onDeleteSession(s.id)}
                />
              ))}
            </div>
          </ScrollArea>
        </>
      )}
      {activeNav !== "chat" && <div className="flex-1" />}

      {/* 账号底栏：健康 · 成本 · 用户 · 退出 */}
      <div className="flex items-center gap-1.5 border-t px-3 py-2">
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <span className={`h-2 w-2 rounded-full ${healthColor}`} />
              {healthLabel}
            </span>
          </TooltipTrigger>
          {healthDetail && (
            <TooltipContent align="start" className="whitespace-pre text-xs">
              {healthDetail}
            </TooltipContent>
          )}
        </Tooltip>
        <div className="ml-auto flex items-center gap-0.5">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                onClick={toggleTheme}
                aria-label={theme === "dark" ? "Light mode" : "Dark mode"}
                data-testid="theme-toggle"
                className="size-7 text-muted-foreground hover:text-foreground"
              >
                {theme === "dark" ? <Sun /> : <Moon />}
              </Button>
            </TooltipTrigger>
            <TooltipContent>{theme === "dark" ? "Light mode" : "Dark mode"}</TooltipContent>
          </Tooltip>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="ghost" size="icon" onClick={onOpenUsage} aria-label="Usage & Cost" className="size-7 text-muted-foreground hover:text-foreground">
                <ChartColumn />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Usage & Cost</TooltipContent>
          </Tooltip>
          <span className="max-w-20 truncate px-1 text-xs text-muted-foreground" title={userId}>
            {userId}
          </span>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button variant="ghost" size="icon" onClick={onLogout} aria-label="Logout" className="size-7 text-muted-foreground hover:text-foreground">
                <LogOut />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Logout</TooltipContent>
          </Tooltip>
        </div>
      </div>
    </aside>
  );
}
