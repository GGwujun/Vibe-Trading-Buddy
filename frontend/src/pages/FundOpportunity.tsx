import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ChevronLeft, ChevronRight, Coins, Loader2, RefreshCw, Search, ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { toast } from "sonner";
import { api, type FundScanItem } from "@/lib/api";
import { cn } from "@/lib/utils";
import { fmtYi } from "@/components/dashboard/primitives";

const FUND_TYPES = [
  { value: "ETF", label: "ETF" },
  { value: "LOF", label: "LOF" },
  { value: "ALL", label: "全部" },
];

const PAGE_SIZES = [10, 20, 50];

type SortKey = "premium_abs" | "premium_desc" | "premium_asc" | "amount_desc";
const SORT_OPTIONS: { value: SortKey; label: string }[] = [
  { value: "premium_abs", label: "折溢价空间（大→小）" },
  { value: "amount_desc", label: "成交额（高→低）" },
  { value: "premium_desc", label: "溢价优先（申购）" },
  { value: "premium_asc", label: "折价优先（赎回）" },
];

function fmtTime(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function premiumClass(p: number): string {
  if (p > 1.5) return "text-danger font-semibold";
  if (p > 0.5) return "text-warning";
  if (p < -1.5) return "text-success font-semibold";
  if (p < -0.5) return "text-success";
  return "text-muted-foreground";
}

export function FundOpportunity() {
  const navigate = useNavigate();
  const [fundType, setFundType] = useState("ETF");
  const [minPremium, setMinPremium] = useState(0.5);
  const [items, setItems] = useState<FundScanItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [total, setTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(1);
  const [source, setSource] = useState<"snapshot" | "live">("snapshot");
  const [serverUpdatedAt, setServerUpdatedAt] = useState<string | null>(null);
  const [sort, setSort] = useState<SortKey>("premium_abs");

  const doScan = useCallback(async (targetPage: number, targetSize: number, typeVal: string, minVal: number, sortVal: SortKey, showToast = false) => {
    setLoading(true);
    try {
      const res = await api.scanFunds(typeVal, minVal, targetPage, targetSize, sortVal);
      setItems(res.items || []);
      setTotal(res.count || 0);
      setTotalPages(res.total_pages || 1);
      setSource(res.source || "snapshot");
      setServerUpdatedAt(res.updated_at);
      if (showToast) toast.success(`扫描到 ${res.count} 只基金`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "扫描失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    doScan(page, pageSize, fundType, minPremium, sort); // auto-scan on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const goAnalyze = (item: FundScanItem) => {
    navigate(`/fund-arbitrage?code=${encodeURIComponent(item.code)}&type=${encodeURIComponent(item.type)}`);
  };

  // Filter/sort change → reset to page 1, then scan.
  const onTypeChange = (v: string) => {
    setFundType(v);
    setPage(1);
    doScan(1, pageSize, v, minPremium, sort, true);
  };
  const onMinChange = (v: number) => {
    setMinPremium(v);
    setPage(1);
    doScan(1, pageSize, fundType, v, sort, true);
  };
  const onSortChange = (v: SortKey) => {
    setSort(v);
    setPage(1);
    doScan(1, pageSize, fundType, minPremium, v, true);
  };
  const onPageSizeChange = (v: number) => {
    setPageSize(v);
    setPage(1);
    doScan(1, v, fundType, minPremium, sort, true);
  };
  const onPageChange = (p: number) => {
    setPage(p);
    doScan(p, pageSize, fundType, minPremium, sort);
  };
  const onRefresh = () => doScan(page, pageSize, fundType, minPremium, sort, true);

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* header */}
      <header className="border-b px-6 py-4 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-3">
          <div className="h-8 w-8 rounded-lg bg-primary/10 flex items-center justify-center">
            <Search className="h-4 w-4 text-primary" />
          </div>
          <div>
            <h1 className="text-lg font-bold flex items-center gap-2">
              套利机会
              {source === "live" && (
                <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-600 dark:text-amber-400">实时</span>
              )}
            </h1>
            <p className="text-xs text-muted-foreground">
              LOF/ETF 折溢价扫描
              {fmtTime(serverUpdatedAt) ? ` · 更新于 ${fmtTime(serverUpdatedAt)}` : ""}
            </p>
          </div>
        </div>
        <button onClick={onRefresh} disabled={loading}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:opacity-90 disabled:opacity-40">
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          刷新
        </button>
      </header>

      {/* filters */}
      <div className="border-b px-6 py-3 flex flex-wrap items-center gap-3 shrink-0 bg-muted/20">
        <div className="flex items-center gap-1.5">
          <span className="text-xs text-muted-foreground">类型</span>
          <select value={fundType} onChange={e => onTypeChange(e.target.value)}
            className="px-2.5 py-1.5 rounded-lg border bg-background text-sm">
            {FUND_TYPES.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-xs text-muted-foreground">最小折溢价</span>
          <input type="number" step="0.1" value={minPremium} onChange={e => onMinChange(parseFloat(e.target.value) || 0)}
            className="w-20 px-2 py-1.5 rounded-lg border bg-background text-sm" />
          <span className="text-xs text-muted-foreground">%</span>
        </div>
        <div className="flex items-center gap-1.5">
          <ArrowUpDown className="h-3.5 w-3.5 text-muted-foreground" />
          <select value={sort} onChange={e => onSortChange(e.target.value as SortKey)}
            className="px-2.5 py-1.5 rounded-lg border bg-background text-sm">
            {SORT_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
        <button onClick={onRefresh} disabled={loading}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-sm font-medium hover:bg-background disabled:opacity-40">
          <Search className="h-3.5 w-3.5" /> 扫描
        </button>
      </div>

      {/* table */}
      <div className="flex-1 overflow-auto p-6">
        {loading && items.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-muted-foreground gap-3">
            <Loader2 className="h-8 w-8 animate-spin text-primary" />
            <p className="text-sm">扫描中…</p>
          </div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-muted-foreground gap-3">
            <Coins className="h-12 w-12 opacity-30" />
            <p className="text-sm">未发现符合条件的套利机会</p>
            <p className="text-xs opacity-60">尝试降低最小折溢价阈值，或切换基金类型</p>
          </div>
        ) : (
          <div className="rounded-lg border overflow-hidden bg-card">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 text-xs text-muted-foreground sticky top-0">
                <tr>
                  <th className="text-left px-3 py-2.5 font-medium">代码</th>
                  <th className="text-left px-3 py-2.5 font-medium">名称</th>
                  <th className="text-right px-3 py-2.5 font-medium">场内价</th>
                  <th className="text-right px-3 py-2.5 font-medium">净值</th>
                  <th className="text-center px-3 py-2.5 font-medium">方向</th>
                  <th className="px-3 py-2.5 font-medium">
                    <div className="flex items-center justify-end gap-1">
                      <button onClick={() => {
                        const next = sort === "premium_abs" ? "premium_desc"
                          : sort === "premium_desc" ? "premium_asc"
                          : sort === "premium_asc" ? "amount_desc" : "premium_abs";
                        onSortChange(next);
                      }} className="inline-flex items-center gap-1 hover:text-foreground">
                        折溢价
                        {sort === "premium_desc" ? <ArrowDown className="h-3 w-3" />
                          : sort === "premium_asc" ? <ArrowUp className="h-3 w-3" />
                          : <ArrowUpDown className="h-3 w-3 opacity-50" />}
                      </button>
                    </div>
                  </th>
                  <th className="text-right px-3 py-2.5 font-medium">净收益</th>
                  <th className="px-3 py-2.5 font-medium">
                    <div className="flex items-center justify-end gap-1">
                      <button onClick={() => onSortChange(sort === "amount_desc" ? "premium_abs" : "amount_desc")}
                        className="inline-flex items-center gap-1 hover:text-foreground">
                        成交额
                        {sort === "amount_desc" ? <ArrowDown className="h-3 w-3" /> : <ArrowUpDown className="h-3 w-3 opacity-50" />}
                      </button>
                    </div>
                  </th>
                  <th className="text-center px-3 py-2.5 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map(item => {
                  const blocked = item.can_trade === false;
                  return (
                  <tr key={item.code} className={cn("border-t transition-colors", blocked ? "opacity-40 hover:opacity-70" : "hover:bg-muted/30")}>
                    <td className="px-3 py-2 font-mono text-xs">{item.code}</td>
                    <td className="px-3 py-2 truncate max-w-[220px]" title={item.name}>
                      <div className="flex items-center gap-1.5">
                        <span className="truncate">{item.name}</span>
                        {blocked && <span className="shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded bg-red-500/15 text-red-500">停申</span>}
                        {item.is_stale_nav && <span className="shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-600 dark:text-amber-400" title="净值可能滞后">净值滞后</span>}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-right font-mono">{item.price}</td>
                    <td className="px-3 py-2 text-right font-mono text-muted-foreground">{item.nav}</td>
                    <td className="px-3 py-2 text-center">
                      {item.direction === "申购套利" ? (
                        <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-red-500/15 text-red-500">申购</span>
                      ) : item.direction === "赎回套利" ? (
                        <span className="text-[10px] font-medium px-1.5 py-0.5 rounded bg-green-500/15 text-green-600 dark:text-green-400">赎回</span>
                      ) : <span className="text-muted-foreground/50">—</span>}
                    </td>
                    <td className={cn("px-3 py-2 text-right font-mono", premiumClass(item.premium_rate))}>
                      {item.premium_rate > 0 ? "+" : ""}{item.premium_rate}%
                      {item.premium_percentile != null && item.premium_percentile >= 90 && (
                        <span className="ml-1 text-[10px] text-amber-600 dark:text-amber-400" title="折溢价处近20日高位">高位</span>
                      )}
                    </td>
                    <td className={cn("px-3 py-2 text-right font-mono text-xs", (item.net_return ?? 0) > 0 ? "text-success font-medium" : "text-muted-foreground/50")}>
                      {(item.net_return ?? 0) > 0 ? `+${item.net_return}%` : "—"}
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-xs text-muted-foreground">{fmtYi(item.amount)}</td>
                    <td className="px-3 py-2 text-center">
                      <button onClick={() => goAnalyze(item)}
                        className="text-xs px-2.5 py-1 rounded-md border border-primary/40 text-primary hover:bg-primary/10 transition-colors">
                        深度分析
                      </button>
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* pagination */}
      {total > 0 && (
        <div className="border-t px-6 py-2.5 flex items-center justify-between shrink-0 bg-muted/20">
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span>共 {total} 只</span>
            <span className="text-muted-foreground/50">·</span>
            <span>每页</span>
            <select value={pageSize} onChange={e => onPageSizeChange(parseInt(e.target.value) || 20)}
              className="px-1.5 py-1 rounded border bg-background text-xs">
              {PAGE_SIZES.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <button onClick={() => onPageChange(page - 1)} disabled={page <= 1 || loading}
              className="flex items-center gap-1 px-2 py-1 rounded border hover:bg-background disabled:opacity-40 transition-colors">
              <ChevronLeft className="h-3.5 w-3.5" /> 上一页
            </button>
            <span className="text-muted-foreground">第 <span className="text-foreground font-medium">{page}</span> / {totalPages} 页</span>
            <button onClick={() => onPageChange(page + 1)} disabled={page >= totalPages || loading}
              className="flex items-center gap-1 px-2 py-1 rounded border hover:bg-background disabled:opacity-40 transition-colors">
              下一页 <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
