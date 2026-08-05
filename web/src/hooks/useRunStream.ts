// 驱动一个会话视图：timeline（已完成 RunTurn 列表，含历史回放）+ live（当前流式 run）。
// M7：单 RunState 升级为 timeline+live——loadSession 在 hook 内完成"取 run 列表→逐 run
// 回放"（全程同一代际，晚到的旧会话响应不会覆盖新选择）；start() 归档上一轮、只重置
// live，不清 timeline → 进入历史会话后可直接继续对话。
// 用 rAF 合并同一动画帧内的多次 applyFrame，避免高频 token 帧引起 DOM 风暴。
// reducer.applyFrame / parseSSE 纯函数不动（仍是 per-run 归并）。

import { useCallback, useRef, useState } from "react";
import {
  listSessionRuns,
  replay,
  resolveApproval,
  startRun,
  type AttachmentRef,
  type SessionRunMeta,
} from "../lib/api/client";
import { iterFrames } from "../lib/api/stream";
import { applyFrame } from "../lib/sse/reducer";
import { emptyRunState, type RunState } from "../lib/sse/frameTypes";

export type RunStatus = "idle" | "running" | "done" | "error";

// 一轮对话 = 一个 run：用户 query + 归并后的 RunState。
// failed=true 表示该轮未走到终态（中断/出错/仍在服务端运行），UI 加标记提示。
// attachments：实时轮来自 start() 入参；历史回放轮来自 run 元数据（服务端落库返还，
// 刷新/重进会话后附件 chips 与工作区「上传内容」段不再丢失——M8 已知限制已补齐）。
// inherited/status 来自 loadSession 的 run 元数据（docs/14）：inherited=继承自父会话的
// 只读投影轮（分叉分界线/角标据此渲染）；status=服务端 run 终态（分叉锚点只对已终态轮开放）。
export interface RunTurn {
  runId: string;
  query: string;
  state: RunState;
  failed?: boolean;
  attachments?: AttachmentRef[];
  inherited?: boolean;
  status?: string;
}

export function useRunStream() {
  const [timeline, setTimeline] = useState<RunTurn[]>([]);
  const [live, setLive] = useState<RunTurn | null>(null);
  const [status, setStatusState] = useState<RunStatus>("idle");
  const [error, setError] = useState<string>("");
  const [loadingHistory, setLoadingHistory] = useState(false);

  const liveRef = useRef<RunTurn | null>(null);
  const rafRef = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // 代际计数：start/loadSession/resetAll 各自取新代并 abort 旧流；旧流的迟到回调
  //（abort 引发的 catch、迟到帧）比对代际后直接丢弃，不得污染新视图。
  const genRef = useRef(0);

  const setStatus = useCallback((s: RunStatus) => setStatusState(s), []);

  const flush = useCallback(() => {
    rafRef.current = null;
    setLive(liveRef.current);
  }, []);

  const scheduleFlush = useCallback(() => {
    if (rafRef.current != null) return;
    rafRef.current =
      typeof requestAnimationFrame !== "undefined"
        ? requestAnimationFrame(flush)
        : (setTimeout(flush, 16) as unknown as number);
  }, [flush]);

  // 取新代并掐掉旧流（所有入口统一走这里）。
  const beginGen = useCallback((): number => {
    const gen = ++genRef.current;
    abortRef.current?.abort();
    return gen;
  }, []);

  // 清空视图（不动代际/abort——由 beginGen 负责）。
  const clearView = useCallback(() => {
    liveRef.current = null;
    setLive(null);
    setTimeline([]);
    setStatus("idle");
    setError("");
  }, [setStatus]);

  // 把当前 live 轮归档进 timeline（连同失败/未完成标记，query 不丢）。
  const archiveLive = useCallback(() => {
    const turn = liveRef.current;
    if (turn) {
      setTimeline((t) => [...t, { ...turn, failed: !turn.state.finished }]);
    }
    liveRef.current = null;
    setLive(null);
  }, []);

  // 清空整个会话视图（新建会话/切到本地草稿会话时用）。
  const resetAll = useCallback(() => {
    beginGen();
    clearView();
    setLoadingHistory(false);
  }, [beginGen, clearView]);

  // 用户主动中断当前运行：beginGen() 递增代际并 abort 当前 fetch → 连接关闭 →
  // 后端 r.Context() 取消 → 级联取消 gRPC 流/图执行 → run 落 STOPPED(CLIENT_GONE)。
  // 必须先 beginGen（在 abort 前递增代际）：否则 pump 的断线恢复分支会误触发回放+"连接中断"横幅。
  // 保留已落库内容、标记本轮未终态，status 转 done 以重新启用 Composer。
  const stop = useCallback(() => {
    beginGen();
    const turn = liveRef.current;
    if (turn) {
      liveRef.current = { ...turn, failed: !turn.state.finished };
      setLive(liveRef.current);
    }
    setStatus("done");
  }, [beginGen, setStatus]);

  const pump = useCallback(
    async (reader: ReadableStreamDefaultReader<Uint8Array>, gen: number) => {
      try {
        for await (const frame of iterFrames(reader)) {
          if (genRef.current !== gen || !liveRef.current) return;
          liveRef.current = { ...liveRef.current, state: applyFrame(liveRef.current.state, frame) };
          scheduleFlush();
        }
        if (genRef.current !== gen) return;
        flush();
        setStatus("done");
      } catch (e) {
        if (genRef.current !== gen) return;
        // M12 断线恢复：SSE 中断（网络抖动/代理断开）→ 服务端已按断连取消本 run，
        // 但事件"先落库后推送"——回放一次把已落库帧补齐视图，用户不丢已产生内容。
        const brokenRunId = liveRef.current?.runId;
        if (brokenRunId) {
          try {
            const reader2 = await replay(brokenRunId);
            let st = emptyRunState(brokenRunId);
            for await (const frame of iterFrames(reader2)) {
              if (genRef.current !== gen) return; // 已被切会话/新提问取代 → 丢弃，不污染新视图
              st = applyFrame(st, frame);
            }
            if (genRef.current !== gen) return;
            if (liveRef.current) {
              liveRef.current = { ...liveRef.current, state: st, failed: !st.finished };
            }
            flush();
            setStatus(st.finished ? "done" : "error");
            setError(st.finished ? "" : "连接中断：已恢复落库内容，本轮未走到终态，可重新提问");
            return;
          } catch {
            /* 回放也失败（服务不可达）→ 落到原错误路径 */
          }
        }
        if (genRef.current !== gen) return;
        flush();
        setStatus("error");
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [flush, scheduleFlush, setStatus],
  );

  // 发起新一轮（续聊即复用同一 sessionId：后端 checkpointer 按 thread 续上下文）。
  const start = useCallback(
    async (
      query: string,
      agentType: string,
      sessionId: string,
      attachments?: AttachmentRef[],
      outputFormat?: string,
      imageGen?: boolean,
    ): Promise<string> => {
      const gen = beginGen();
      archiveLive();
      setError("");
      setStatus("running");
      liveRef.current = { runId: "", query, state: emptyRunState(), attachments };
      setLive(liveRef.current);
      const ac = new AbortController();
      abortRef.current = ac;
      try {
        const { runId, reader } = await startRun(
          { query, sessionId, agentType, attachments, outputFormat, imageGen },
          ac.signal,
        );
        if (genRef.current !== gen) return runId;
        // 代际未变 ⇒ liveRef 必仍是本轮（置空必伴随代际递增）。
        liveRef.current = { ...liveRef.current!, runId, state: { ...liveRef.current!.state, runId } };
        setLive(liveRef.current);
        void pump(reader, gen);
        return runId;
      } catch (e) {
        if (genRef.current === gen) {
          setStatus("error");
          setError(e instanceof Error ? e.message : String(e));
        }
        return "";
      }
    },
    [archiveLive, beginGen, pump, setStatus],
  );

  // 打开历史会话：取 run 列表 → 逐 run 回放（复用与实时同一解析/归并），每回放完一个
  // run 就上屏一轮；全部载入后 Composer 即可继续输入。失败置 status=error（决不能把
  // 载入失败静默渲染成"空会话"——用户会在看不见的上下文之上继续对话）。
  // 返回 run 元数据（组合层用其恢复该会话最近的 agentType 等），失败/被取代返回 null。
  const loadSession = useCallback(
    async (sessionId: string): Promise<SessionRunMeta[] | null> => {
      const gen = beginGen();
      clearView();
      setLoadingHistory(true);
      const ac = new AbortController();
      abortRef.current = ac;
      try {
        const metas = await listSessionRuns(sessionId, ac.signal);
        if (genRef.current !== gen) return null;
        const turns: RunTurn[] = [];
        for (const meta of metas) {
          let st = emptyRunState(meta.runId);
          const reader = await replay(meta.runId, ac.signal);
          for await (const frame of iterFrames(reader)) {
            st = applyFrame(st, frame);
          }
          if (genRef.current !== gen) return null;
          turns.push({
            runId: meta.runId,
            query: meta.query,
            state: st,
            failed: !st.finished,
            attachments: meta.attachments,
            inherited: meta.inherited,
            status: meta.status,
          });
          setTimeline([...turns]);
        }
        return metas;
      } catch (e) {
        if (genRef.current === gen && !ac.signal.aborted) {
          setStatus("error");
          setError(e instanceof Error ? e.message : String(e));
        }
        return null;
      } finally {
        if (genRef.current === gen) setLoadingHistory(false);
      }
    },
    [beginGen, clearView, setStatus],
  );

  // 把某轮（live 或 timeline 中）某个审批的状态原位补丁——用于乐观置为 approved/rejected
  // 或失败回滚。挂起轮可能是 live（同会话现点）也可能在 timeline（刷新/切会话后从回放渲染）。
  const patchApproval = useCallback(
    (runId: string, approvalId: string, status: "pending" | "approved" | "rejected") => {
      const patch = (turn: RunTurn): RunTurn => {
        const a = turn.state.approvals[approvalId];
        if (turn.runId !== runId || !a) return turn;
        return {
          ...turn,
          state: { ...turn.state, approvals: { ...turn.state.approvals, [approvalId]: { ...a, status } } },
        };
      };
      if (liveRef.current) {
        liveRef.current = patch(liveRef.current);
        setLive(liveRef.current);
      }
      setTimeline((t) => t.map(patch));
    },
    [],
  );

  // M11 HITL：审批决议 → 恢复 run（新一轮，与 start 同构）。pausedRunId 由调用方按
  // 卡片所在轮传入（**不依赖 liveRef**——刷新/隔夜后挂起轮只存在于 timeline，这正是
  // "跨重启审批"卖点必须支持的路径）。乐观补丁那轮的审批状态；失败回滚 + 抛错让卡复位。
  const resumeApproval = useCallback(
    async (pausedRunId: string, approvalId: string, approved: boolean, comment?: string): Promise<string> => {
      if (!pausedRunId) return "";
      const gen = beginGen();
      archiveLive(); // 若 live 是挂起轮，先归档进 timeline（补丁在其后统一处理）
      patchApproval(pausedRunId, approvalId, approved ? "approved" : "rejected");
      setError("");
      setStatus("running");
      const label = `[审批] ${approved ? "通过" : "拒绝"}${comment ? `：${comment}` : ""}`;
      liveRef.current = { runId: "", query: label, state: emptyRunState() };
      setLive(liveRef.current);
      const ac = new AbortController();
      abortRef.current = ac;
      try {
        const { runId, reader } = await resolveApproval(pausedRunId, approvalId, approved, comment, ac.signal);
        if (genRef.current !== gen) return runId;
        liveRef.current = { ...liveRef.current!, runId, state: { ...liveRef.current!.state, runId } };
        setLive(liveRef.current);
        void pump(reader, gen);
        return runId;
      } catch (e) {
        if (genRef.current === gen) {
          // 回滚乐观补丁（卡回 pending 可重试）+ 撤掉空的恢复轮 live + 报错。
          liveRef.current = null;
          setLive(null);
          patchApproval(pausedRunId, approvalId, "pending");
          setStatus("error");
          setError(e instanceof Error ? e.message : String(e));
        }
        return "";
      }
    },
    [archiveLive, beginGen, patchApproval, pump, setStatus],
  );

  return { timeline, live, status, error, loadingHistory, start, loadSession, resetAll, resumeApproval, stop };
}
