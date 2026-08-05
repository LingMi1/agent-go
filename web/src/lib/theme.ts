// 主题状态（light/dark）：uiPrefs 持久化 + html.dark class 切换 + useSyncExternalStore 订阅。
// index.html 里有同款读取逻辑的防闪烁内联脚本（先于 React 挂 class），两处 key/字段必须一致。

import { useSyncExternalStore } from "react";
import { loadUiPrefs, saveUiPrefs, type Theme } from "@/lib/uiPrefs";

let current: Theme = loadUiPrefs().theme;
const listeners = new Set<() => void>();

function applyClass(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
}

// 模块加载即对齐一次（防闪烁脚本可能因 localStorage 异常没跑到）
if (typeof document !== "undefined") {
  applyClass(current);
}

export function getTheme(): Theme {
  return current;
}

export function setTheme(theme: Theme): void {
  if (theme === current) return;
  current = theme;
  applyClass(theme);
  saveUiPrefs({ ...loadUiPrefs(), theme });
  listeners.forEach((fn) => fn());
}

export function toggleTheme(): void {
  setTheme(current === "dark" ? "light" : "dark");
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, getTheme, () => "light" as Theme);
}
