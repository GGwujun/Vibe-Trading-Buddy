import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function Panel({
  title,
  icon,
  right,
  className,
  bodyClassName,
  children,
}: {
  title?: ReactNode;
  icon?: ReactNode;
  right?: ReactNode;
  className?: string;
  bodyClassName?: string;
  children: ReactNode;
}) {
  return (
    <section className={cn("rounded-md border bg-background", className)}>
      {title && (
        <header className="flex items-center justify-between gap-2 border-b px-3 py-2">
          <div className="flex min-w-0 items-center gap-1.5 text-xs font-medium text-foreground">
            {icon}
            <span className="truncate">{title}</span>
          </div>
          {right}
        </header>
      )}
      <div className={cn("p-3", bodyClassName)}>{children}</div>
    </section>
  );
}

export function EmptyHint({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-[60px] items-center justify-center text-[11px] text-muted-foreground/70">
      {children}
    </div>
  );
}

export function Pct({ value, className }: { value: number | null | undefined; className?: string }) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return <span className={cn("text-muted-foreground", className)}>-</span>;
  }
  const tone = value > 0 ? "text-red-500" : value < 0 ? "text-green-500" : "text-muted-foreground";
  const sign = value > 0 ? "+" : "";
  return <span className={cn(tone, className)}>{sign}{value.toFixed(2)}%</span>;
}

export function fmtYi(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  const abs = Math.abs(v);
  if (abs >= 1e8) return (v / 1e8).toFixed(2) + "亿";
  if (abs >= 1e4) return (v / 1e4).toFixed(1) + "万";
  return v.toFixed(0);
}

// 热力图涨跌背景色：涨红、跌绿（A 股习惯），颜色饱和度随 |涨跌幅| 连续加深，
// 上限在约 8% 处达到最深，避免小幅波动时整片灰白看不出区分。
export function pctBg(value: number | null | undefined): string {
  if (value === null || value === undefined) return "rgba(100,116,139,0.55)";
  if (value === 0) return "rgba(100,116,139,0.55)";
  const abs = Math.min(Math.abs(value), 8);
  // 深度 0.5 ~ 1：abs=0 时 0.5，abs>=8 时 1，平滑过渡
  const depth = 0.5 + (abs / 8) * 0.5;
  const alpha = depth.toFixed(2);
  return value > 0 ? `rgba(239,68,68,${alpha})` : `rgba(22,163,74,${alpha})`;
}
