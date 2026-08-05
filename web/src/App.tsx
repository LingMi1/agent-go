import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "./hooks/useAuth";
import { useHealth } from "./hooks/useHealth";
import { useRunStream } from "./hooks/useRunStream";
import { LoginView } from "./views/LoginView";
import { ChatView } from "./views/ChatView";
import { UsageDialog } from "./components/UsageDialog";
import { Sidebar, SidebarRail } from "./components/Sidebar";
import { KnowledgePanel } from "./components/FilesPanel";
import { SchedulesPanel } from "./components/SchedulesPanel";
import { ConnectorsPanel } from "./components/ConnectorsPanel";
import { ArtifactsGallery } from "./components/ArtifactsGallery";
import { AdminPanel } from "./components/AdminPanel";
import { GenerateWorkspace } from "./components/GenerateWorkspace";
import { deleteSession, forkSession, listServerSessions, type AttachmentRef, type ServerSession } from "./lib/api/client";
import {
  createSession,
  listSessions as listLocalSessions,
  mergeSessions,
  pruneSessions,
  removeLocalSession,
  type SessionMeta,
} from "./lib/sessions";
import { loadUiPrefs, saveUiPrefs, clampArtifactsWidth, type NavView } from "./lib/uiPrefs";
import { toast } from "sonner";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";

export default function App() {
  const { userId, isAuthed, isAdmin, login, register, logout } = useAuth();
  const health = useHealth();
  const run = useRunStream();

  const [agentType, setAgentType] = useState("react");
  // 布局状态：侧栏开合/宽度/当前导航视图持久化；Artifacts 右 dock 默认关（生成时自动开）。
  const [prefs] = useState(loadUiPrefs);
  const [sidebarOpen, setSidebarOpen] = useState(prefs.sidebarOpen);
  const [activeNav, setActiveNav] = useState<NavView>(prefs.activeNav);
  const [artifactsOpen, setArtifactsOpen] = useState(false);
  const [usageOpen, setUsageOpen] = useState(false);
  const [artifactsWidth, setArtifactsWidth] = useState(prefs.artifactsWidth);
  useEffect(() => {
    // theme 由 lib/theme.ts 独立管理，这里透传当前值避免覆盖
    saveUiPrefs({ ...loadUiPrefs(), sidebarOpen, artifactsWidth, activeNav });
  }, [sidebarOpen, artifactsWidth, activeNav]);
  // 会话列表 = 服务端（权威）+ 本地草稿（未落库新会话）两个状态的纯函数派生，
  // 不再手工同步——任何一侧更新，列表自动重算。
  const [serverSessions, setServerSessions] = useState<ServerSession[]>([]);
  const [drafts, setDrafts] = useState<SessionMeta[]>(() => listLocalSessions());
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const sessions = useMemo(() => mergeSessions(serverSessions, drafts), [serverSessions, drafts]);
  // 首次拿到列表后自动预热最近会话的标记（换用户时须复位——见下方身份边界清态 effect）。
  const didAutoSelect = useRef(false);

  const refreshSessions = useCallback(async () => {
    try {
      const server = await listServerSessions();
      setServerSessions(server);
      // 已落库的会话由服务端接管，从本地草稿修剪（防无界膨胀）。
      setDrafts(pruneSessions(server.map((s) => s.sessionId)));
    } catch {
      /* 服务端暂不可达：沿用现有列表（本地草稿兜底），不清空视图 */
    }
  }, []);

  // 身份边界清态（#13，docs/17）：userId 变化（登出置空 / 同浏览器换用户登录）→ 强制清空
  // 一切内存用户态——对话 timeline/live、当前会话、会话列表、导航视图、自动预热标记——
  // 杜绝换用户后主区/侧栏残留上一用户的对话/会话/产物（跨租户数据暴露）。
  // ref 守卫：仅在 userId 真正切换时触发；初次挂载/整页刷新（prev===当前）不重置，
  // 保住持久化的 activeNav 等零回归。清态在下方登录重载 effect 之前跑，随后由其按新用户重填。
  const prevUserRef = useRef(userId);
  useEffect(() => {
    if (prevUserRef.current === userId) return;
    prevUserRef.current = userId;
    run.resetAll();
    setCurrentSessionId(null);
    setServerSessions([]);
    setDrafts([]);
    setActiveNav("chat");
    didAutoSelect.current = false;
  }, [userId, run]);

  // 登录后：重读该用户的本地草稿（按用户隔离）并拉服务端列表。
  useEffect(() => {
    if (!isAuthed) return;
    setDrafts(listLocalSessions());
    void refreshSessions();
  }, [isAuthed, refreshSessions]);

  // run 结束后再刷一次列表（状态/lastActiveAt 已落库）。
  useEffect(() => {
    if (run.status === "done" || run.status === "error") void refreshSessions();
  }, [run.status, refreshSessions]);

  // 进入会话：载入整段历史（hook 内取列表+逐 run 回放，同一代际防切换竞态）→
  // 可直接继续对话。重复点击同一会话 = 重新载入（失败后的重试入口）。
  const selectSession = useCallback(
    async (id: string, switchToChat = true) => {
      // 用户主动点会话 → 切回对话视图；但刷新后的自动预热(switchToChat=false)不能覆盖
      // 用户持久化的 activeNav(如停在"产物"画廊)——否则每次刷新都被拽回对话。
      if (switchToChat) setActiveNav("chat");
      setCurrentSessionId(id);
      const view = sessions.find((s) => s.id === id);
      if (view?.pendingLocal) {
        if (view.agentType) setAgentType(view.agentType);
        run.resetAll(); // 本地草稿会话：还没有 run，无历史可载
        return;
      }
      const metas = await run.loadSession(id);
      if (metas && metas.length > 0) {
        const last = metas[metas.length - 1];
        if (last.agentType) setAgentType(last.agentType); // 恢复该会话最近使用的 agent
      }
    },
    [sessions, run],
  );

  // 首次拿到会话列表且尚未选中任何会话 → 自动进入最近会话（刷新页面后直接续聊）。
  useEffect(() => {
    if (didAutoSelect.current || currentSessionId || sessions.length === 0) return;
    didAutoSelect.current = true;
    void selectSession(sessions[0].id, false); // 只预热会话，不改用户持久化的 activeNav
  }, [sessions, currentSessionId, selectSession]);

  // currentSessionId 一定出自本 UI（新建草稿或会话列表），存在即信任——
  // 不查 sessions 派生列表（异步刷新未落地时会误判不存在而重复建会话）。
  const ensureSessionId = useCallback((): string => {
    if (currentSessionId) return currentSessionId;
    const s = createSession("New Session", agentType);
    setDrafts(listLocalSessions());
    setCurrentSessionId(s.id);
    return s.id;
  }, [currentSessionId, agentType]);

  const onSubmit = useCallback(
    async (q: string, attachments?: AttachmentRef[], outputFormat?: string, imageGen?: boolean) => {
      const sid = ensureSessionId();
      const runId = await run.start(q, agentType, sid, attachments, outputFormat, imageGen);
      if (runId) void refreshSessions(); // run 已落库：标题/runCount/lastActiveAt 即时更新
    },
    [ensureSessionId, agentType, run, refreshSessions],
  );

  // docs/14 会话分叉：登记 fork（新会话）→ 刷列表（分叉标记/0-run 会话可见）→ 切入
  // 新会话（loadSession 回放继承轮 + 分界线）。复用 selectSession 既有单向数据流。
  const onForkTurn = useCallback(
    async (afterRunId: string) => {
      if (!currentSessionId) return;
      try {
        const newId = await forkSession(currentSessionId, afterRunId);
        toast.success("Forked new session from this turn");
        void refreshSessions();
        void selectSession(newId);
      } catch {
        toast.error("Fork failed: the turn may still be running or was deleted");
      }
    },
    [currentSessionId, refreshSessions, selectSession],
  );

  // M11 HITL：审批决议（稳定引用——MessageList memo 纪律）。
  const onApprovalDecision = useCallback(
    async (runId: string, approvalId: string, approved: boolean, comment?: string): Promise<boolean> => {
      const newRunId = await run.resumeApproval(runId, approvalId, approved, comment);
      if (newRunId) void refreshSessions();
      return !!newRunId; // 供 ApprovalCard 失败复位 busy
    },
    [run, refreshSessions],
  );

  // 删除会话：总是打服务端删 + 本地草稿删；删的是当前会话则退回空白态。
  const onDeleteSession = useCallback(
    async (id: string) => {
      try {
        // 总是打服务端删除：pendingLocal 是快照派生、可能过期（run 已落库但刷新未到达），
        // 据它跳过服务端删会让"已删"的会话下次刷新又冒回来。端点对不存在的会话 404-容忍。
        await deleteSession(id);
      } catch {
        toast.error("Failed to delete session");
        return;
      }
      setDrafts(removeLocalSession(id)); // 本地草稿一并清（纯函数，按当前 user 隔离）
      if (id === currentSessionId) {
        setCurrentSessionId(null);
        run.resetAll();
      }
      void refreshSessions();
      toast.success("Session deleted");
    },
    [currentSessionId, run, refreshSessions],
  );

  const onNewSession = useCallback(() => {
    setActiveNav("chat"); // 从画廊/知识库点"新对话"须切回对话视图，否则新会话搁浅在别的视图后
    // 已停在一个空草稿上就复用它，避免连点"新会话"堆积幽灵草稿。
    const currentView = sessions.find((s) => s.id === currentSessionId);
    if (currentView?.pendingLocal) {
      run.resetAll();
      return;
    }
    const s = createSession("New Session", agentType);
    setDrafts(listLocalSessions());
    setCurrentSessionId(s.id);
    run.resetAll();
  }, [sessions, currentSessionId, agentType, run]);

  if (!isAuthed) return <LoginView onLogin={login} onRegister={register} />;
  // D3：非 admin 若持久化的 activeNav 停在 "admin"（如降权后），回退到对话——不渲染后台。
  const effectiveNav: NavView = activeNav === "admin" && !isAdmin ? "chat" : activeNav;
  return (
    <TooltipProvider delayDuration={200}>
    <div className="flex h-full">
      <Toaster position="top-center" />
      <UsageDialog open={usageOpen} onOpenChange={setUsageOpen} />
      {sidebarOpen ? (
        <Sidebar
          sessions={sessions}
          currentSessionId={currentSessionId}
          activeNav={effectiveNav}
          onNavChange={setActiveNav}
          onNewSession={onNewSession}
          onSelectSession={(id) => void selectSession(id)}
          onDeleteSession={(id) => void onDeleteSession(id)}
          onToggleSidebar={() => setSidebarOpen(false)}
          health={health}
          userId={userId}
          isAdmin={isAdmin}
          onOpenUsage={() => setUsageOpen(true)}
          onLogout={logout}
        />
      ) : (
        // 侧栏收起时收敛为窄图标条（保留完整导航的图标形态，对齐 Claude；不再是单个悬浮钮）。
        <SidebarRail
          activeNav={effectiveNav}
          onNavChange={setActiveNav}
          onNewSession={onNewSession}
          onExpand={() => setSidebarOpen(true)}
          health={health}
          isAdmin={isAdmin}
          onOpenUsage={() => setUsageOpen(true)}
          onLogout={logout}
        />
      )}
      {/* min-w-0 必不可少：flex item 默认 min-width:auto 会以内容 min-content 为下限拒绝收缩，
          聊天列+右 dock 的固定宽会把整行撑出视口（dock 拖宽时右缘溢出窗口的根因）。 */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {/* ChatView 常挂载（保住流式 DOM/滚动位置），非 chat 视图用 hidden 盖住 */}
        <div className={effectiveNav === "chat" ? "flex min-h-0 min-w-0 flex-1" : "hidden"}>
          <ChatView
            visible={effectiveNav === "chat"}
            timeline={run.timeline}
            live={run.live}
            status={run.status}
            loadingHistory={run.loadingHistory}
            onSubmit={onSubmit}
            agentType={agentType}
            onAgentType={setAgentType}
            uploadSessionId={currentSessionId ?? ""}
            artifactsOpen={artifactsOpen}
            onArtifactsOpenChange={setArtifactsOpen}
            artifactsWidth={artifactsWidth}
            onArtifactsWidthChange={(w) => setArtifactsWidth(clampArtifactsWidth(w))}
            onApprovalDecision={onApprovalDecision}
            onStop={run.stop}
            onForkTurn={(afterRunId) => void onForkTurn(afterRunId)}
          />
        </div>
        {effectiveNav === "generate" && <GenerateWorkspace />}
        {effectiveNav === "artifacts" && <ArtifactsGallery onOpenSession={(id) => void selectSession(id)} />}
        {effectiveNav === "kb" && (
          <div className="mx-auto min-h-0 w-full max-w-4xl flex-1">
            <KnowledgePanel />
          </div>
        )}
        {effectiveNav === "schedules" && (
          <div className="mx-auto min-h-0 w-full max-w-4xl flex-1">
            <SchedulesPanel onOpenSession={(id) => void selectSession(id)} />
          </div>
        )}
        {effectiveNav === "connectors" && (
          <div className="mx-auto min-h-0 w-full max-w-4xl flex-1">
            <ConnectorsPanel onOpenSession={(id) => void selectSession(id)} />
          </div>
        )}
        {/* D3：管理后台仅 admin（effectiveNav 已把非 admin 的 "admin" 回退到 chat）。 */}
        {effectiveNav === "admin" && isAdmin && <AdminPanel currentUserId={userId} />}
      </div>
    </div>
    </TooltipProvider>
  );
}
