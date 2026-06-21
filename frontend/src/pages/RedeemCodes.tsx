import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle,
  Clock3,
  Copy,
  Loader2,
  Plus,
  RefreshCw,
  Ticket,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import { api, type GenerateCodesRequest, type RedeemCodeItem, type RedeemCodesStats } from "@/lib/api";
import { cn } from "@/lib/utils";

type StatusFilter = "all" | "unused" | "used" | "expired";

function formatDate(iso: string | null): string {
  if (!iso) return "-";
  const d = new Date(iso);
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function statusIcon(status: string) {
  if (status === "unused") return <CheckCircle className="h-4 w-4 text-success" />;
  if (status === "used") return <XCircle className="h-4 w-4 text-muted-foreground" />;
  if (status === "expired") return <Clock3 className="h-4 w-4 text-warning" />;
  return <AlertTriangle className="h-4 w-4 text-muted-foreground" />;
}

function statusLabel(status: string): string {
  if (status === "unused") return "未使用";
  if (status === "used") return "已使用";
  if (status === "expired") return "已过期";
  return status;
}

export function RedeemCodes() {
  const [codes, setCodes] = useState<RedeemCodeItem[]>([]);
  const [stats, setStats] = useState<RedeemCodesStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [showGenerate, setShowGenerate] = useState(false);
  const [genCredits, setGenCredits] = useState(100);
  const [genCount, setGenCount] = useState(10);
  const [genPrefix, setGenPrefix] = useState("SIGMX");
  const [genDays, setGenDays] = useState(90);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [listRes, statsRes] = await Promise.all([
        api.listRedeemCodes(statusFilter, 200),
        api.getRedeemCodesStats(),
      ]);
      setCodes(listRes.items);
      setStats(statsRes);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "加载兑换码失败");
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    load();
  }, [load]);

  const handleGenerate = async () => {
    if (genCredits <= 0 || genCount <= 0) {
      toast.error("请输入有效的积分数量和生成数量");
      return;
    }
    setGenerating(true);
    try {
      const body: GenerateCodesRequest = {
        credits: genCredits,
        count: genCount,
        prefix: genPrefix,
        days: genDays,
      };
      const res = await api.generateRedeemCodes(body);
      toast.success(`已生成 ${res.count} 个兑换码，每个 ${res.credits} 积分`);
      setShowGenerate(false);
      load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "生成兑换码失败");
    } finally {
      setGenerating(false);
    }
  };

  const copyCode = async (code: string) => {
    try {
      await navigator.clipboard.writeText(code);
      toast.success("已复制兑换码");
    } catch {
      toast.error("复制失败");
    }
  };

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col">
      {/* Header */}
      <div className="border-b bg-card/80 px-4 py-4 md:px-6">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h1 className="text-xl font-semibold tracking-tight">兑换码管理</h1>
            <p className="mt-1 text-xs text-muted-foreground">生成和管理积分兑换码，仅管理员可见</p>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setShowGenerate(true)}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground transition hover:opacity-90"
            >
              <Plus className="h-4 w-4" />
              生成兑换码
            </button>
            <button
              type="button"
              onClick={() => load()}
              disabled={loading}
              className="inline-flex items-center gap-1.5 rounded-md border bg-background px-2.5 py-1.5 text-sm text-muted-foreground transition hover:border-primary/35 hover:bg-primary/5 hover:text-foreground disabled:opacity-50"
            >
              <RefreshCw className={cn("h-4 w-4", loading && "animate-spin")} />
              刷新
            </button>
          </div>
        </div>
      </div>

      {/* Stats cards */}
      {stats && (
        <div className="border-b px-4 py-3 md:px-6">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <StatCard label="总兑换码" value={stats.total} icon={<Ticket className="h-4 w-4" />} />
            <StatCard label="未使用" value={stats.unused} icon={<CheckCircle className="h-4 w-4 text-success" />} highlight />
            <StatCard label="已使用" value={stats.used} icon={<XCircle className="h-4 w-4 text-muted-foreground" />} />
            <StatCard label="已过期" value={stats.expired} icon={<Clock3 className="h-4 w-4 text-warning" />} />
            <StatCard label="未用积分" value={stats.total_credits_unused} icon={<span className="text-sm font-bold">¥</span>} />
          </div>
        </div>
      )}

      {/* Filter */}
      <div className="border-b px-4 py-2 md:px-6">
        <div className="flex gap-2">
          {(["all", "unused", "used", "expired"] as StatusFilter[]).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setStatusFilter(s)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs transition-colors",
                statusFilter === s
                  ? "border-primary/40 bg-primary/10 text-primary"
                  : "bg-background text-muted-foreground hover:bg-muted hover:text-foreground",
              )}
            >
              {statusIcon(s)}
              {s === "all" ? "全部" : statusLabel(s)}
              {s !== "all" && stats && (
                <span className="tabular-nums opacity-70">
                  {s === "unused" ? stats.unused : s === "used" ? stats.used : stats.expired}
                </span>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4 md:p-6">
        {loading ? (
          <div className="flex items-center justify-center gap-2 py-20 text-sm text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" />
            加载兑换码列表...
          </div>
        ) : codes.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-3 rounded-md border border-dashed bg-muted/10 px-6 py-14 text-center">
            <Ticket className="h-10 w-10 text-muted-foreground/35" />
            <p className="text-sm font-medium">暂无兑换码</p>
            <button
              type="button"
              onClick={() => setShowGenerate(true)}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground transition hover:opacity-90"
            >
              <Plus className="h-4 w-4" />
              生成兑换码
            </button>
          </div>
        ) : (
          <div className="overflow-hidden rounded-md border bg-card">
            <table className="w-full text-sm">
              <thead className="border-b bg-muted/30 text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left font-medium">兑换码</th>
                  <th className="px-3 py-2 text-left font-medium">积分</th>
                  <th className="px-3 py-2 text-left font-medium">状态</th>
                  <th className="px-3 py-2 text-left font-medium">使用者</th>
                  <th className="px-3 py-2 text-left font-medium">兑换时间</th>
                  <th className="px-3 py-2 text-left font-medium">创建时间</th>
                  <th className="px-3 py-2 text-left font-medium">有效期</th>
                  <th className="px-3 py-2 text-left font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {codes.map((code) => (
                  <tr key={code.code} className="border-b last:border-0 hover:bg-muted/25">
                    <td className="px-3 py-2.5 font-mono text-xs">{code.code}</td>
                    <td className="px-3 py-2.5 tabular-nums">{code.credits}</td>
                    <td className="px-3 py-2.5">
                      <span className="inline-flex items-center gap-1">
                        {statusIcon(code.status)}
                        <span className={cn(
                          "text-xs",
                          code.status === "unused" && "text-success",
                          code.status === "used" && "text-muted-foreground",
                          code.status === "expired" && "text-warning",
                        )}>
                          {statusLabel(code.status)}
                        </span>
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-xs text-muted-foreground">
                      {code.redeemed_by || "-"}
                    </td>
                    <td className="px-3 py-2.5 text-xs text-muted-foreground">{formatDate(code.redeemed_at)}</td>
                    <td className="px-3 py-2.5 text-xs text-muted-foreground">{formatDate(code.created_at)}</td>
                    <td className="px-3 py-2.5 text-xs text-muted-foreground">{formatDate(code.expires_at)}</td>
                    <td className="px-3 py-2.5">
                      <button
                        type="button"
                        onClick={() => copyCode(code.code)}
                        className="inline-flex items-center gap-1 rounded border bg-background px-2 py-1 text-xs transition hover:bg-muted"
                      >
                        <Copy className="h-3 w-3" />
                        复制
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Generate modal */}
      {showGenerate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="w-full max-w-md rounded-lg border bg-card p-6 shadow-lg">
            <h2 className="text-lg font-semibold">生成兑换码</h2>
            <p className="mt-1 text-xs text-muted-foreground">批量生成积分兑换码</p>

            <div className="mt-4 space-y-3">
              <div>
                <label className="text-xs font-medium text-muted-foreground">积分数量（每个兑换码）</label>
                <input
                  type="number"
                  value={genCredits}
                  onChange={(e) => setGenCredits(Number(e.target.value))}
                  className="mt-1 h-9 w-full rounded-md border bg-background px-3 text-sm outline-none focus:border-primary/60 focus:ring-2 focus:ring-primary/15"
                  min={1}
                  max={10000}
                />
              </div>
              <div>
                <label className="text-xs font-medium text-muted-foreground">生成数量</label>
                <input
                  type="number"
                  value={genCount}
                  onChange={(e) => setGenCount(Number(e.target.value))}
                  className="mt-1 h-9 w-full rounded-md border bg-background px-3 text-sm outline-none focus:border-primary/60 focus:ring-2 focus:ring-primary/15"
                  min={1}
                  max={100}
                />
              </div>
              <div>
                <label className="text-xs font-medium text-muted-foreground">兑换码前缀</label>
                <input
                  type="text"
                  value={genPrefix}
                  onChange={(e) => setGenPrefix(e.target.value.toUpperCase())}
                  className="mt-1 h-9 w-full rounded-md border bg-background px-3 text-sm font-mono uppercase outline-none focus:border-primary/60 focus:ring-2 focus:ring-primary/15"
                  maxLength={10}
                />
              </div>
              <div>
                <label className="text-xs font-medium text-muted-foreground">有效天数（0=永久）</label>
                <input
                  type="number"
                  value={genDays}
                  onChange={(e) => setGenDays(Number(e.target.value))}
                  className="mt-1 h-9 w-full rounded-md border bg-background px-3 text-sm outline-none focus:border-primary/60 focus:ring-2 focus:ring-primary/15"
                  min={0}
                  max={365}
                />
              </div>
            </div>

            <div className="mt-6 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowGenerate(false)}
                className="rounded-md border bg-background px-3 py-1.5 text-sm transition hover:bg-muted"
              >
                取消
              </button>
              <button
                type="button"
                onClick={handleGenerate}
                disabled={generating}
                className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
              >
                {generating && <Loader2 className="h-4 w-4 animate-spin" />}
                生成
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function StatCard({
  label,
  value,
  icon,
  highlight = false,
}: {
  label: string;
  value: number;
  icon: React.ReactNode;
  highlight?: boolean;
}) {
  return (
    <div className={cn(
      "flex items-center gap-2 rounded-md border px-3 py-2",
      highlight ? "border-success/30 bg-success/10" : "bg-muted/30",
    )}>
      <div className={cn(highlight ? "text-success" : "text-muted-foreground")}>{icon}</div>
      <div>
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className={cn("text-sm font-semibold tabular-nums", highlight && "text-success")}>{value}</p>
      </div>
    </div>
  );
}
