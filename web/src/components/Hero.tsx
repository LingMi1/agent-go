import { ChartColumn, FileText, FolderGit2, ImagePlus, Telescope } from "lucide-react";
import { BrandMark } from "./BrandMark";
import { SAMPLE_QUESTIONS } from "../config";

// 首屏空态（claude.ai 风）：星芒 + 衬线大标题 + 能力建议 chips（交错入场）。
// 标题文案「有什么可以帮上忙？」为 e2e 锚点，不可改。
const CHIP_ICONS = [Telescope, FolderGit2, ChartColumn, ImagePlus, FileText];

export function Hero({ onAsk, disabled }: { onAsk: (q: string) => void; disabled?: boolean }) {
  return (
    <div className="mx-auto mt-[16vh] max-w-xl px-4 text-center">
      <div className="mb-4 flex justify-center text-foreground">
        <BrandMark className="size-9" />
      </div>
      <h1 className="mb-2 font-display text-[2rem] leading-tight font-medium tracking-tight text-foreground">
        What can I help you with?
      </h1>
      <p className="mb-8 text-sm text-muted-foreground">
        Research, reports, charts, image generation — just ask.
      </p>
      <div className="flex flex-col items-center gap-2">
        {SAMPLE_QUESTIONS.map((q, i) => {
          const Icon = CHIP_ICONS[i % CHIP_ICONS.length];
          return (
            <button
              key={q}
              onClick={() => onAsk(q)}
              disabled={disabled}
              style={{ animationDelay: `${i * 60}ms` }}
              className="animate-in fade-in slide-in-from-bottom-2 fill-mode-both flex w-full max-w-md items-center gap-2.5 rounded-xl border bg-card px-3.5 py-2.5 text-left text-sm text-foreground/85 shadow-xs transition-colors duration-500 hover:border-primary/35 hover:bg-accent/50"
            >
              <Icon className="size-4 shrink-0 text-primary/80" />
              <span className="min-w-0">{q}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
