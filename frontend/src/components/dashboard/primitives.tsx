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

export function pctBg(value: number | null | undefined): string {
  if (value === null || value === undefined) return "transparent";
  if (value > 5) return "rgba(239,68,68,0.55)";
  if (value > 2) return "rgba(239,68,68,0.32)";
  if (value > 0) return "rgba(239,68,68,0.16)";
  if (value < -5) return "rgba(34,197,94,0.55)";
  if (value < -2) return "rgba(34,197,94,0.32)";
  if (value < 0) return "rgba(34,197,94,0.16)";
  return "transparent";
}
