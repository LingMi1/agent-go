// UI 偏好（侧栏开合/Artifacts 面板开合与宽度）：localStorage 持久化，
// key 沿用 `my-agent.` 前缀约定（identity/sessions 同款）。纯函数 + 容错读写。

const KEY = "my-agent.ui";

// 左侧栏导航项 = 主区视图路由（对齐 Claude 官网侧栏导航）。
// "admin" 仅 admin 角色可见（Sidebar 用 isAdmin 门控），但必须在 NAV_VIEWS 里，
// 否则 admin 停在后台刷新时持久化的 activeNav 会被打回 chat（docs/17 §3.4）。
export type NavView = "chat" | "generate" | "artifacts" | "kb" | "schedules" | "connectors" | "admin";
const NAV_VIEWS: NavView[] = ["chat", "generate", "artifacts", "kb", "schedules", "connectors", "admin"];

export type Theme = "light" | "dark";

export interface UiPrefs {
  sidebarOpen: boolean;
  artifactsWidth: number;
  activeNav: NavView;
  theme: Theme;
  workspaceSplit: number; // 工作区「动态」页预览区高度占比（预览/(预览+feed)）
}

export const ARTIFACTS_MIN_W = 320;
export const ARTIFACTS_MAX_W = 720;

export function clampArtifactsWidth(w: number): number {
  if (!Number.isFinite(w)) return 384;
  return Math.min(ARTIFACTS_MAX_W, Math.max(ARTIFACTS_MIN_W, Math.round(w)));
}

export const WORKSPACE_SPLIT_MIN = 0.2;
export const WORKSPACE_SPLIT_MAX = 0.9;
export const WORKSPACE_SPLIT_DEFAULT = 0.55;

export function clampWorkspaceSplit(v: number): number {
  if (!Number.isFinite(v)) return WORKSPACE_SPLIT_DEFAULT;
  return Math.min(WORKSPACE_SPLIT_MAX, Math.max(WORKSPACE_SPLIT_MIN, v));
}

// 浅色为新默认（对齐 claude.ai 奶油米观感）；历史用户无 theme 键 → 落到 light，一键可切回。
const DEFAULTS: UiPrefs = {
  sidebarOpen: true,
  artifactsWidth: 384,
  activeNav: "chat",
  theme: "light",
  workspaceSplit: WORKSPACE_SPLIT_DEFAULT,
};

export function loadUiPrefs(): UiPrefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULTS };
    const parsed = JSON.parse(raw) as Partial<UiPrefs>;
    return {
      sidebarOpen: typeof parsed.sidebarOpen === "boolean" ? parsed.sidebarOpen : DEFAULTS.sidebarOpen,
      artifactsWidth: clampArtifactsWidth(parsed.artifactsWidth ?? DEFAULTS.artifactsWidth),
      activeNav: NAV_VIEWS.includes(parsed.activeNav as NavView) ? (parsed.activeNav as NavView) : "chat",
      theme: parsed.theme === "dark" ? "dark" : "light",
      workspaceSplit: clampWorkspaceSplit(parsed.workspaceSplit ?? DEFAULTS.workspaceSplit),
    };
  } catch {
    return { ...DEFAULTS };
  }
}

export function saveUiPrefs(prefs: UiPrefs): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(prefs));
  } catch {
    /* 隐私模式等存不进就算了，仅影响下次打开的记忆 */
  }
}
