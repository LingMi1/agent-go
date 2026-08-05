// ECharts 按需注册 + claude-light/claude-dark 双主题（dataviz 技能校准）。
// 只在 EChartsPreview 里动态 import（懒加载，vite manualChunks 单独分包）。
//
// 主题要点（dataviz 规范）：
// - 色板与 index.css --chart-1..5 同源，固定顺序分配（绝不循环生成新色相）。
//   validate_palette 提示 chart-4/5 相邻 CVD ΔE 偏低（deutan），按规范以次级编码兜底：
//   legend 常显 + tooltip 全系列读数 + 柱间/堆叠 2px 面色间隙 + 折线点 2px 面色描边环。
// - 网格线：一档偏离面色的发丝线（1px 实线，不用虚线），退居背景；类目轴不画分隔线。
// - 柱 ≤24px、数据端 4px 圆角（基线端直角）；折线 2px；标记点 ≥8px 带面色环。
// - tooltip：暖底卡面、1px 细边、8px 圆角、值优先（fontSize 12），轻投影。
// - 文字永远用文字 token（浅 #57544c / 暗 #e8e6e0），不穿系列色。

import * as echarts from "echarts/core";
import { BarChart, LineChart, PieChart } from "echarts/charts";
import {
  DatasetComponent,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  DatasetComponent,
  CanvasRenderer,
]);

// 与 index.css --font-sans 一致
const FONT =
  '"Inter Variable", system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';

interface ThemeVars {
  palette: string[]; // --chart-1..5
  text: string; // 主文字（title/legend/pie label）
  textMuted: string; // 轴标签
  hairline: string; // 轴线/分隔线（发丝线）
  surface: string; // 卡面色：marker 环 / pie 段间隙
  tooltipBg: string;
  tooltipBorder: string;
  tooltipText: string;
  tooltipShadow: string;
  pointerShade: string; // 柱状 axisPointer 阴影
}

function makeTheme(v: ThemeVars): object {
  return {
    color: v.palette,
    backgroundColor: "transparent",
    textStyle: { color: v.text, fontFamily: FONT },
    title: {
      textStyle: { color: v.text, fontSize: 14, fontWeight: 600, fontFamily: FONT },
      subtextStyle: { color: v.textMuted, fontSize: 11, fontFamily: FONT },
    },
    legend: {
      textStyle: { color: v.text, fontSize: 11, fontFamily: FONT },
      itemWidth: 12,
      itemHeight: 8,
      itemGap: 12,
    },
    tooltip: {
      backgroundColor: v.tooltipBg,
      borderColor: v.tooltipBorder,
      borderWidth: 1,
      borderRadius: 8,
      padding: [8, 12],
      textStyle: { color: v.tooltipText, fontSize: 12, fontFamily: FONT },
      extraCssText: `box-shadow:${v.tooltipShadow};`,
      axisPointer: {
        lineStyle: { color: v.hairline, width: 1 },
        crossStyle: { color: v.hairline, width: 1 },
        shadowStyle: { color: v.pointerShade },
      },
    },
    // 留白：上方给 title/legend 呼吸位，containLabel 防轴标签溢出
    grid: { top: 44, right: 16, bottom: 36, left: 12, containLabel: true },
    categoryAxis: {
      axisLine: { show: true, lineStyle: { color: v.hairline, width: 1 } },
      axisTick: { show: false },
      axisLabel: { color: v.textMuted, fontSize: 11, fontFamily: FONT },
      splitLine: { show: false },
      splitArea: { show: false },
    },
    valueAxis: {
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: v.textMuted, fontSize: 11, fontFamily: FONT },
      splitLine: { show: true, lineStyle: { color: v.hairline, width: 1, type: "solid" } },
      splitArea: { show: false },
    },
    timeAxis: {
      axisLine: { show: true, lineStyle: { color: v.hairline, width: 1 } },
      axisTick: { show: false },
      axisLabel: { color: v.textMuted, fontSize: 11, fontFamily: FONT },
      splitLine: { show: false },
    },
    bar: {
      barMaxWidth: 24,
      itemStyle: { borderRadius: [4, 4, 0, 0] }, // 数据端圆角、基线端直角
    },
    line: {
      lineStyle: { width: 2, cap: "round", join: "round" },
      symbol: "circle",
      symbolSize: 8,
      itemStyle: { borderWidth: 2, borderColor: v.surface }, // 2px 面色环
    },
    pie: {
      itemStyle: { borderColor: v.surface, borderWidth: 2, borderRadius: 4 }, // 2px 面色间隙
      label: { color: v.text, fontSize: 11, fontFamily: FONT },
      labelLine: { lineStyle: { color: v.hairline } },
    },
  };
}

echarts.registerTheme(
  "claude-light",
  makeTheme({
    palette: ["#d97757", "#6a9b8e", "#c2a878", "#8b7db8", "#5f8fb4"],
    text: "#57544c",
    textMuted: "#706d64",
    hairline: "rgba(31, 30, 29, 0.12)",
    surface: "#fefdfb",
    tooltipBg: "#fefdfb",
    tooltipBorder: "rgba(31, 30, 29, 0.12)",
    tooltipText: "#1f1e1d",
    tooltipShadow: "0 4px 16px rgba(31, 30, 29, 0.10)",
    pointerShade: "rgba(31, 30, 29, 0.04)",
  }),
);

echarts.registerTheme(
  "claude-dark",
  makeTheme({
    palette: ["#e08b6d", "#7fb3a5", "#d4bd90", "#a294cc", "#7ba6c9"],
    text: "#e8e6e0",
    textMuted: "#a8a49b",
    hairline: "rgba(255, 255, 255, 0.10)",
    surface: "#262521",
    tooltipBg: "#2e2c27",
    tooltipBorder: "rgba(255, 255, 255, 0.10)",
    tooltipText: "#e8e6e0",
    tooltipShadow: "0 4px 16px rgba(0, 0, 0, 0.35)",
    pointerShade: "rgba(255, 255, 255, 0.04)",
  }),
);

export { echarts };
