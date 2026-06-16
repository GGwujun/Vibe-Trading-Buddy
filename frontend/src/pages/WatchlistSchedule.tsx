import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CalendarClock,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  History,
  Loader2,
  Plus,
  Play,
  Trash2,
  X,
  XCircle,
} from "lucide-react";
import { toast } from "sonner";
import {
  api,
  isAuthRequiredError,
  type AnalysisRun,
  type Horizon,
  type ScheduledTask,
  type TrackingWatchlistItem,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const fieldClass =
  "w-full rounded-md border bg-background px-3 py-2 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/20 disabled:cursor-not-allowed disabled:opacity-60";
const labelClass = "text-sm font-medium";
const hintClass = "text-xs text-muted-foreground";

const HORIZONS: Horizon[] = ["短线", "中线", "长线"];

const DECISION_COLORS: Record<string, string> = {
  strong_buy: "text-success bg-success/10 border-success/20",
  buy: "text-success bg-success/10 border-success/20",
  hold: "text-warning bg-warning/10 border-warning/20",
  sell: "text-danger bg-danger/10 border-danger/20",
  strong_sell: "text-danger bg-danger/10 border-danger/20",
};

function normalizeCode(input: string): string {
  const cleaned = input.trim().toUpperCase();
  if (/^\d{6}\.(SZ|SH)$/.test(cleaned)) return cleaned;
  if (!/^\d{6}$/.test(cleaned)) return "";
  const prefix = cleaned.slice(0, 3);
  if (["000", "001", "002", "003", "004", "159", "300", "301"].includes(prefix))
    return `${cleaned}.SZ`;
  return `${cleaned}.SH`;
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString("zh-CN", { hour12: false });
  } catch {
    return iso;
  }
}

function scoreTone(score: number): string {
  if (score >= 0.6) return "text-success";
  if (score >= 0.4) return "text-warning";
  return "text-danger";
}

function unknownError(e: unknown): string {
  if (e instanceof Error) return e.message;
  return String(e);
}

// ---------------------------------------------------------------------------
// Left column — independent watchlist
// ---------------------------------------------------------------------------

function WatchlistColumn({
  items,
  onAdd,
  onRemove,
  onUse,
}: {
  items: TrackingWatchlistItem[];
  onAdd: (symbol: string) => Promise<void>;
  onRemove: (symbol: string) => Promise<void>;
  onUse: (item: TrackingWatchlistItem) => void;
}) {
  const [input, setInput] = useState("");
  const [adding, setAdding] = useState(false);

  const submit = async () => {
    const code = normalizeCode(input);
    if (!code) {
      toast.error("请输入 6 位代码（如 000001 或 000001.SZ）");
      return;
    }
    setAdding(true);
    try {
      await onAdd(code);
      setInput("");
    } catch (e) {
      if (!isAuthRequiredError(e)) toast.error(`添加失败：${unknownError(e)}`);
    } finally {
      setAdding(false);
    }
  };

  return (
    <section className="rounded-lg border bg-card p-4">
      <h2 className="text-sm font-semibold">自选列表</h2>
      <p className={cn(hintClass, "mt-0.5")}>为关注的标的配置每日定时分析。</p>

      <div className="mt-3 flex gap-2">
        <input
          className={fieldClass}
          placeholder="代码，如 000001"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
          disabled={adding}
        />
        <button
          type="button"
          className="inline-flex shrink-0 items-center gap-1 rounded-md bg-primary px-3 py-2 text-xs font-medium text-primary-foreground hover:opacity-90 disabled:opacity-70"
          onClick={submit}
          disabled={adding}
        >
          {adding ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
          添加
        </button>
      </div>

      <div className="mt-3 space-y-1.5">
        {items.length === 0 ? (
          <p className={cn(hintClass, "py-4 text-center")}>暂无自选标的</p>
        ) : (
          items.map((it) => (
            <div
              key={it.symbol}
              className="flex items-center justify-between rounded-md border bg-background px-3 py-2"
            >
              <div className="min-w-0">
                <div className="truncate text-sm font-medium">{it.name || it.symbol}</div>
                <div className={hintClass}>{it.symbol}</div>
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <button
                  type="button"
                  className="rounded-md border px-2 py-1 text-xs hover:bg-muted"
                  onClick={() => onUse(it)}
                  title="加入定时分析"
                >
                  <CalendarClock className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  className="rounded-md border px-2 py-1 text-xs text-danger hover:bg-danger/10"
                  onClick={() => onRemove(it.symbol)}
                  title="删除"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          ))
        )}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Right column — task row
// ---------------------------------------------------------------------------

// Table-style column header (hidden on narrow screens; rows stack instead).
function TaskTableHeader() {
  return (
    <div className="hidden items-center gap-3 rounded-lg border bg-muted/40 px-3 py-2 text-[11px] font-medium text-muted-foreground md:flex">
      <span className="min-w-[140px] flex-1">股票</span>
      <span className="w-[88px] shrink-0 text-center">长短线</span>
      <span className="w-[96px] shrink-0 text-center">分析时间</span>
      <span className="w-[120px] shrink-0">上次运行</span>
      <span className="w-[52px] shrink-0 text-center">开启</span>
      <span className="w-[112px] shrink-0 text-right">操作</span>
    </div>
  );
}

function HorizonSwitch({
  value,
  disabled,
  onChange,
}: {
  value: Horizon;
  disabled?: boolean;
  onChange: (v: Horizon) => void;
}) {
  // Segmented short/medium/long toggle — compact, table-friendly.
  return (
    <div className="inline-flex overflow-hidden rounded-md border bg-background text-xs">
      {HORIZONS.map((h) => (
        <button
          key={h}
          type="button"
          disabled={disabled}
          onClick={() => onChange(h)}
          className={cn(
            "px-2 py-1 transition-colors disabled:opacity-50",
            value === h ? "bg-primary text-primary-foreground" : "hover:bg-muted",
          )}
        >
          {h}
        </button>
      ))}
    </div>
  );
}

function Toggle({
  checked,
  disabled,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative h-5 w-9 shrink-0 rounded-full border border-transparent transition-colors disabled:opacity-50",
        checked ? "bg-success" : "bg-muted-foreground/30",
      )}
    >
      <span
        className={cn(
          "absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform",
          checked ? "translate-x-4" : "translate-x-0",
        )}
      />
    </button>
  );
}

function TaskRow({
  task,
  onUpdate,
  onDelete,
  onRun,
  onViewHistory,
}: {
  task: ScheduledTask;
  onUpdate: (patch: Partial<Pick<ScheduledTask, "horizon" | "time" | "enabled">>) => Promise<void>;
  onDelete: () => Promise<void>;
  onRun: () => Promise<void>;
  onViewHistory: () => void;
}) {
  const [busy, setBusy] = useState(false);

  const patch = async (p: Partial<Pick<ScheduledTask, "horizon" | "time" | "enabled">>) => {
    setBusy(true);
    try {
      await onUpdate(p);
    } finally {
      setBusy(false);
    }
  };

  const lastRun = task.last_run_at ? fmtTime(task.last_run_at) : "—";

  return (
    <div
      className={cn(
        "rounded-lg border bg-card px-3 py-2.5 transition-colors",
        task.enabled ? "" : "opacity-60",
        busy && "animate-pulse",
      )}
    >
      <div className="flex flex-col gap-3 md:flex-row md:items-center">
        {/* Stock */}
        <div className="min-w-[140px] flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium">{task.name || task.symbol}</span>
            <span className="rounded bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
              {task.horizon}
            </span>
          </div>
          <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">{task.symbol}</p>
          {/* Mobile-only: last run under the symbol */}
          <p className="mt-0.5 text-[10px] text-muted-foreground md:hidden">
            上次运行：{lastRun}
            {task.last_status === "ok" && <CheckCircle2 className="ml-1 inline h-3 w-3 text-success" />}
            {task.last_status === "error" && <XCircle className="ml-1 inline h-3 w-3 text-danger" />}
          </p>
        </div>

        {/* Horizon (desktop) */}
        <div className="hidden w-[88px] shrink-0 justify-center md:flex">
          <HorizonSwitch
            value={task.horizon}
            disabled={busy}
            onChange={(v) => patch({ horizon: v })}
          />
        </div>

        {/* Time (desktop) */}
        <div className="hidden w-[96px] shrink-0 justify-center md:flex">
          <input
            type="time"
            className="w-[88px] rounded-md border bg-background px-2 py-1 text-xs outline-none focus:border-primary focus:ring-2 focus:ring-primary/20 disabled:opacity-60"
            value={task.time}
            onChange={(e) => patch({ time: e.target.value })}
            disabled={busy}
          />
        </div>

        {/* Last run (desktop) */}
        <div className="hidden w-[120px] shrink-0 items-center gap-1 text-[11px] text-muted-foreground md:flex">
          <Clock className="h-3 w-3 shrink-0" />
          <span className="truncate">{lastRun}</span>
          {task.last_status === "ok" && <CheckCircle2 className="h-3 w-3 shrink-0 text-success" />}
          {task.last_status === "error" && <XCircle className="h-3 w-3 shrink-0 text-danger" />}
        </div>

        {/* Toggle (desktop) */}
        <div className="hidden w-[52px] shrink-0 justify-center md:flex">
          <Toggle checked={task.enabled} disabled={busy} onChange={(v) => patch({ enabled: v })} />
        </div>

        {/* Actions */}
        <div className="flex shrink-0 items-center gap-1 md:w-[112px] md:justify-end">
          <button
            type="button"
            className="rounded-md border px-2 py-1 text-xs hover:bg-muted disabled:opacity-60"
            onClick={onRun}
            disabled={busy}
            title="立即运行"
          >
            <Play className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            className="rounded-md border px-2 py-1 text-xs hover:bg-muted disabled:opacity-60"
            onClick={onViewHistory}
            title="查看历史"
          >
            <History className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            className="rounded-md border px-2 py-1 text-xs text-danger hover:bg-danger/10 disabled:opacity-60"
            onClick={onDelete}
            disabled={busy}
            title="删除任务"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* Mobile-only inline controls (stack under the row on narrow screens) */}
      <div className="mt-2 grid grid-cols-3 gap-2 md:hidden">
        <HorizonSwitch value={task.horizon} disabled={busy} onChange={(v) => patch({ horizon: v })} />
        <input
          type="time"
          className="rounded-md border bg-background px-2 py-1 text-xs outline-none focus:border-primary disabled:opacity-60"
          value={task.time}
          onChange={(e) => patch({ time: e.target.value })}
          disabled={busy}
        />
        <label className="flex items-center justify-center gap-1.5 text-xs">
          <Toggle checked={task.enabled} disabled={busy} onChange={(v) => patch({ enabled: v })} />
          {task.enabled ? "开启" : "暂停"}
        </label>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// History drawer
// ---------------------------------------------------------------------------

function HistoryDrawer({
  symbol,
  name,
  onClose,
}: {
  symbol: string;
  name: string;
  onClose: () => void;
}) {
  const [items, setItems] = useState<AnalysisRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.getTrackingHistory(symbol);
      setItems(res.items);
    } catch (e) {
      if (!isAuthRequiredError(e)) toast.error(`加载历史失败：${unknownError(e)}`);
    } finally {
      setLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    load();
  }, [load]);

  const clear = async () => {
    try {
      await api.clearTrackingHistory(symbol);
      setItems([]);
      toast.success("已清除历史");
    } catch (e) {
      if (!isAuthRequiredError(e)) toast.error(`清除失败：${unknownError(e)}`);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/40" onClick={onClose}>
      <div
        className="flex h-full w-full max-w-md flex-col bg-background shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b px-4 py-3">
          <div>
            <div className="text-sm font-semibold">{name || symbol} · 分析历史</div>
            <div className={hintClass}>{symbol}</div>
          </div>
          <button
            type="button"
            className="rounded-md border px-2 py-1 text-xs hover:bg-muted"
            onClick={onClose}
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              加载中
            </div>
          ) : items.length === 0 ? (
            <p className={cn(hintClass, "py-8 text-center")}>暂无分析记录</p>
          ) : (
            <div className="space-y-2">
              {items.map((run) => {
                const r = run.result;
                const isOpen = expanded[run.run_id];
                return (
                  <div key={run.run_id} className="rounded-lg border bg-card p-3">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs text-muted-foreground">{fmtTime(run.run_at)}</span>
                      {run.status === "error" ? (
                        <span className="rounded border border-danger/20 bg-danger/10 px-1.5 py-0.5 text-xs text-danger">
                          失败
                        </span>
                      ) : r ? (
                        <span
                          className={cn(
                            "rounded border px-1.5 py-0.5 text-xs",
                            DECISION_COLORS[r.decision] ?? "border bg-muted",
                          )}
                        >
                          {r.decision_label}
                        </span>
                      ) : null}
                    </div>

                    {run.status === "error" ? (
                      <p className="mt-1 text-xs text-danger">{run.error || "运行出错"}</p>
                    ) : r ? (
                      <>
                        <div className="mt-1 flex items-center gap-3 text-xs">
                          <span>
                            评分：
                            <span className={cn("font-medium", scoreTone(r.overall_score))}>
                              {Math.round(r.overall_score * 100)}
                            </span>
                          </span>
                          <span>置信：{r.confidence}</span>
                          {r.stop_loss ? <span>止损 ¥{r.stop_loss}</span> : null}
                          {r.take_profit ? <span>止盈 ¥{r.take_profit}</span> : null}
                        </div>
                        <button
                          type="button"
                          className="mt-2 flex items-center gap-1 text-xs text-primary hover:underline"
                          onClick={() => setExpanded((m) => ({ ...m, [run.run_id]: !m[run.run_id] }))}
                        >
                          {isOpen ? (
                            <ChevronDown className="h-3.5 w-3.5" />
                          ) : (
                            <ChevronRight className="h-3.5 w-3.5" />
                          )}
                          维度明细
                        </button>
                        {isOpen && r.dimensions?.length ? (
                          <div className="mt-1.5 space-y-1">
                            {r.dimensions.map((d) => (
                              <div
                                key={d.id}
                                className="flex items-center justify-between rounded bg-background px-2 py-1 text-xs"
                              >
                                <span>
                                  {d.signal} {d.label}
                                </span>
                                <span className={scoreTone(d.score)}>{Math.round(d.score * 100)}</span>
                              </div>
                            ))}
                          </div>
                        ) : null}
                      </>
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div className="border-t p-3">
          <button
            type="button"
            className="w-full rounded-md border px-3 py-2 text-xs hover:bg-muted disabled:opacity-60"
            onClick={clear}
            disabled={loading || items.length === 0}
          >
            清除历史
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export function WatchlistSchedule() {
  const [watchlist, setWatchlist] = useState<TrackingWatchlistItem[]>([]);
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState<{ symbol: string; name: string } | null>(null);
  const [draftForm, setDraftForm] = useState<{ horizon: Horizon; time: string; enabled: boolean }>({
    horizon: "短线",
    time: "15:05",
    enabled: true,
  });
  const [submitting, setSubmitting] = useState(false);
  const [history, setHistory] = useState<{ symbol: string; name: string } | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [wl, tk] = await Promise.all([
        api.getTrackingWatchlist(),
        api.listTrackingTasks(),
      ]);
      setWatchlist(wl.items);
      setTasks(tk.items);
    } catch (e) {
      if (!isAuthRequiredError(e)) {
        setError(unknownError(e));
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  // ---- watchlist ops ----
  const addWatchlist = async (symbol: string) => {
    const res = await api.addTrackingWatchlistItem(symbol);
    toast.success(`已添加 ${res.item.name || symbol}`);
    setWatchlist((prev) => {
      const next = prev.filter((i) => i.symbol !== res.item.symbol);
      next.push(res.item);
      return next;
    });
  };

  const removeWatchlist = async (symbol: string) => {
    try {
      await api.deleteTrackingWatchlistItem(symbol);
      setWatchlist((prev) => prev.filter((i) => i.symbol !== symbol));
      toast.success("已删除");
    } catch (e) {
      if (!isAuthRequiredError(e)) toast.error(`删除失败：${unknownError(e)}`);
    }
  };

  const startCreate = (item: TrackingWatchlistItem) => {
    // If a task already exists for this symbol, just surface it.
    const existing = tasks.find((t) => t.symbol === item.symbol);
    if (existing) {
      toast.message(`${item.name || item.symbol} 已有定时任务`);
      return;
    }
    setDraft({ symbol: item.symbol, name: item.name });
    setDraftForm({ horizon: "短线", time: "15:05", enabled: true });
    setCreating(true);
  };

  const submitCreate = async () => {
    if (!draft) return;
    setSubmitting(true);
    try {
      const res = await api.createTrackingTask({
        symbol: draft.symbol,
        horizon: draftForm.horizon,
        time: draftForm.time,
        enabled: draftForm.enabled,
      });
      setTasks((prev) => {
        const next = prev.filter((t) => t.task_id !== res.task.task_id);
        next.push(res.task);
        return next;
      });
      toast.success(`已创建 ${res.task.name || draft.symbol} 的定时任务`);
      setCreating(false);
      setDraft(null);
    } catch (e) {
      if (!isAuthRequiredError(e)) toast.error(`创建失败：${unknownError(e)}`);
    } finally {
      setSubmitting(false);
    }
  };

  // ---- task ops ----
  const updateTask = async (
    taskId: string,
    patch: Partial<Pick<ScheduledTask, "horizon" | "time" | "enabled">>,
  ) => {
    try {
      const res = await api.updateTrackingTask(taskId, patch);
      setTasks((prev) => prev.map((t) => (t.task_id === taskId ? res.task : t)));
      toast.success("已更新");
    } catch (e) {
      if (!isAuthRequiredError(e)) toast.error(`更新失败：${unknownError(e)}`);
      throw e;
    }
  };

  const deleteTask = async (taskId: string) => {
    try {
      await api.deleteTrackingTask(taskId);
      setTasks((prev) => prev.filter((t) => t.task_id !== taskId));
      toast.success("已删除任务");
    } catch (e) {
      if (!isAuthRequiredError(e)) toast.error(`删除失败：${unknownError(e)}`);
    }
  };

  const runNow = async (taskId: string) => {
    const t = tasks.find((x) => x.task_id === taskId);
    toast.message(`正在分析 ${t?.name || t?.symbol || ""}…`);
    try {
      const res = await api.runTrackingTaskNow(taskId);
      if (res.run.status === "ok") {
        toast.success("分析完成，结果已存入历史");
      } else {
        toast.error(`分析失败：${res.run.error || ""}`);
      }
      // Refresh tasks (last_run_at/last_status) + history drawer if open.
      const tk = await api.listTrackingTasks();
      setTasks(tk.items);
      if (history) setHistory({ ...history }); // trigger drawer reload via identity change is not enough; user can reopen
    } catch (e) {
      if (!isAuthRequiredError(e)) toast.error(`运行失败：${unknownError(e)}`);
    }
  };

  // ---- render ----
  if (loading) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
        正在加载
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="max-w-sm rounded-lg border border-danger/20 bg-danger/5 p-6 text-center">
          <AlertTriangle className="mx-auto h-8 w-8 text-danger/70" />
          <p className="mt-3 text-sm font-medium">加载失败</p>
          <p className="mt-1 text-xs text-muted-foreground">{error}</p>
          <button
            type="button"
            onClick={reload}
            className="mt-4 rounded-md border px-3 py-1.5 text-xs hover:bg-muted"
          >
            重试
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col">
      <div className="shrink-0 border-b px-4 py-3 md:px-6">
        <div className="flex items-center gap-2">
          <CalendarClock className="h-5 w-5 text-primary" />
          <h1 className="text-xl font-semibold tracking-tight">自选 & 定时分析</h1>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          为自选标的配置每日定时分析任务，到点（北京时间）自动跑规则引擎并保存结果，可回看历史。
        </p>
      </div>

      <div className="flex-1 overflow-y-auto p-4 md:p-6">
        <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
          <WatchlistColumn
            items={watchlist}
            onAdd={addWatchlist}
            onRemove={removeWatchlist}
            onUse={startCreate}
          />

          <section className="space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold">定时分析任务</h2>
              <span className={hintClass}>
                {tasks.length} 个任务 · 已开启 {tasks.filter((t) => t.enabled).length}
              </span>
            </div>

            {creating && draft ? (
              <div className="rounded-lg border border-primary/30 bg-card p-3">
                <div className="mb-2 flex items-center justify-between">
                  <span className="text-sm font-medium">新建任务：{draft.name || draft.symbol}</span>
                  <button
                    type="button"
                    className="rounded-md border px-2 py-1 text-xs hover:bg-muted"
                    onClick={() => {
                      setCreating(false);
                      setDraft(null);
                    }}
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
                <div className="grid gap-2 sm:grid-cols-3">
                  <label className="grid gap-1">
                    <span className={cn(labelClass, "text-xs")}>长短线</span>
                    <select
                      className={fieldClass}
                      value={draftForm.horizon}
                      onChange={(e) =>
                        setDraftForm((f) => ({ ...f, horizon: e.target.value as Horizon }))
                      }
                    >
                      {HORIZONS.map((h) => (
                        <option key={h} value={h}>
                          {h}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="grid gap-1">
                    <span className={cn(labelClass, "text-xs")}>分析时间</span>
                    <input
                      type="time"
                      className={fieldClass}
                      value={draftForm.time}
                      onChange={(e) => setDraftForm((f) => ({ ...f, time: e.target.value }))}
                    />
                  </label>
                  <label className="flex items-end gap-2 pb-1">
                    <input
                      type="checkbox"
                      className="h-4 w-4 accent-primary"
                      checked={draftForm.enabled}
                      onChange={(e) =>
                        setDraftForm((f) => ({ ...f, enabled: e.target.checked }))
                      }
                    />
                    <span className={cn(labelClass, "text-xs")}>开启</span>
                  </label>
                </div>
                <div className="mt-2 flex justify-end">
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:opacity-90 disabled:opacity-70"
                    onClick={submitCreate}
                    disabled={submitting}
                  >
                    {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
                    保存任务
                  </button>
                </div>
              </div>
            ) : null}

            {tasks.length === 0 && !creating ? (
              <div className="rounded-lg border border-dashed bg-card p-8 text-center">
                <CalendarClock className="mx-auto h-8 w-8 text-muted-foreground/60" />
                <p className="mt-2 text-sm font-medium">暂无定时任务</p>
                <p className={cn(hintClass, "mt-1")}>
                  从左侧自选列表点击 <CalendarClock className="inline h-3 w-3" /> 把标的加入定时分析。
                </p>
              </div>
            ) : (
              <div className="space-y-1.5">
                <TaskTableHeader />
                {tasks.map((t) => (
                  <TaskRow
                    key={t.task_id}
                    task={t}
                    onUpdate={(p) => updateTask(t.task_id, p)}
                    onDelete={() => deleteTask(t.task_id)}
                    onRun={() => runNow(t.task_id)}
                    onViewHistory={() => setHistory({ symbol: t.symbol, name: t.name })}
                  />
                ))}
              </div>
            )}
          </section>
        </div>
      </div>

      {history ? (
        <HistoryDrawer
          symbol={history.symbol}
          name={history.name}
          onClose={() => setHistory(null)}
        />
      ) : null}
    </div>
  );
}
