import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeft,
  BarChart3,
  Bell,
  CalendarClock,
  CheckCircle2,
  ClipboardList,
  Gauge,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Target,
  TrendingDown,
  TrendingUp,
  Zap,
} from "lucide-react";
import { api, type MarketDashboardStage, type MarketDashboardStageResponse } from "@/lib/api";
import { cn } from "@/lib/utils";

type AnyRecord = Record<string, any>;

const STAGE_META: Record<MarketDashboardStage, { title: string; desc: string; nav: string }> = {
  "morning-brief": {
    title: "早盘内参",
    desc: "盘前把指数、隔夜线索、新闻事件和今日关注标的压缩成可执行清单。",
    nav: "08:50 推送",
  },
  "intraday-monitor": {
    title: "盘中监控",
    desc: "实时盯情绪温度、行业板块、异动标的和定时任务，帮助判断当下阶段。",
    nav: "实时监控",
  },
  "tail-strategy": {
    title: "尾盘策略",
    desc: "收盘前给出尾盘观察清单、候选动作卡和仓位约束。",
    nav: "14:30 决策",
  },
  "close-review": {
    title: "收盘复盘",
    desc: "用问答方式复盘主线、亏钱效应、情绪阶段和明日攻防方向。",
    nav: "盘后归档",
  },
};

function num(value: unknown, fallback = 0): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function pct(value: unknown): string {
  const n = num(value, NaN);
  if (!Number.isFinite(n)) return "-";
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
}

function pctTone(value: unknown): string {
  const n = num(value, 0);
  if (n > 0) return "text-red-500";
  if (n < 0) return "text-green-500";
  return "text-muted-foreground";
}

function amount(value: unknown): string {
  const n = num(value, NaN);
  if (!Number.isFinite(n)) return "-";
  if (Math.abs(n) >= 1e8) return `${(n / 1e8).toFixed(1)} 亿`;
  if (Math.abs(n) >= 1e4) return `${(n / 1e4).toFixed(1)} 万`;
  return n.toFixed(0);
}

function asArray<T = AnyRecord>(value: unknown): T[] {
  return Array.isArray(value) ? value as T[] : [];
}

function StageShell({
  stage,
  payload,
  loading,
  refreshing,
  error,
  onRefresh,
}: {
  stage: MarketDashboardStage;
  payload: MarketDashboardStageResponse | null;
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  onRefresh: () => void;
}) {
  const meta = STAGE_META[stage];
  const data = (payload?.data ?? {}) as AnyRecord;
  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col bg-[#080b0f] text-slate-100">
      <header className="shrink-0 border-b border-white/10 bg-[#090d12] px-4 py-4 md:px-6">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
          <div className="min-w-0">
            <Link to="/market-dashboard" className="inline-flex items-center gap-1 text-xs text-slate-400 hover:text-slate-100">
              <ArrowLeft className="h-3.5 w-3.5" />
              AI 盘面
            </Link>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <h1 className="text-2xl font-semibold tracking-tight">{meta.title}</h1>
              <span className="rounded border border-amber-400/30 bg-amber-400/10 px-2 py-0.5 text-[11px] text-amber-200">
                {meta.nav}
              </span>
              {payload?.updated_at && (
                <span className="text-[11px] text-slate-500">
                  更新 {new Date(payload.updated_at).toLocaleTimeString("zh-CN", { hour12: false })}
                </span>
              )}
            </div>
            <p className="mt-1 max-w-4xl text-xs leading-relaxed text-slate-400">{meta.desc}</p>
          </div>
          <button
            type="button"
            onClick={onRefresh}
            disabled={refreshing}
            className="inline-flex h-9 items-center gap-1.5 rounded-md border border-white/10 bg-white/[0.03] px-3 text-xs text-slate-300 transition hover:bg-white/[0.07] disabled:opacity-50"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", refreshing && "animate-spin")} />
            刷新
          </button>
        </div>
      </header>

      <main className="min-h-0 flex-1 overflow-y-auto p-4 md:p-6">
        {loading ? (
          <div className="flex min-h-[360px] items-center justify-center gap-2 text-sm text-slate-400">
            <Loader2 className="h-5 w-5 animate-spin" />
            正在加载
          </div>
        ) : error ? (
          <div className="rounded-md border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-200">{error}</div>
        ) : (
          <div className="mx-auto max-w-7xl space-y-4">
            {payload?.errors?.length ? <SourceWarning errors={payload.errors} /> : null}
            {stage === "morning-brief" && <MorningBriefView data={data} />}
            {stage === "intraday-monitor" && <IntradayView data={data} />}
            {stage === "tail-strategy" && <TailStrategyView data={data} />}
            {stage === "close-review" && <CloseReviewView data={data} />}
          </div>
        )}
      </main>
    </div>
  );
}

function SourceWarning({ errors }: { errors: { source: string; message: string }[] }) {
  return (
    <div className="rounded-md border border-amber-400/20 bg-amber-400/10 px-3 py-2 text-xs text-amber-100">
      部分数据源降级：{errors.map((item) => item.source).join("、")}
    </div>
  );
}

function Section({
  title,
  desc,
  icon,
  right,
  children,
}: {
  title: string;
  desc?: string;
  icon?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-md border border-white/10 bg-white/[0.035]">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-white/10 px-4 py-3">
        <div className="flex items-center gap-2">
          {icon}
          <div>
            <h2 className="text-sm font-semibold text-slate-100">{title}</h2>
            {desc && <p className="mt-0.5 text-[11px] text-slate-500">{desc}</p>}
          </div>
        </div>
        {right}
      </header>
      <div className="p-4">{children}</div>
    </section>
  );
}

function Empty({ children }: { children: ReactNode }) {
  return <div className="flex min-h-[80px] items-center justify-center text-xs text-slate-500">{children}</div>;
}

function Metric({ label, value, tone }: { label: string; value: React.ReactNode; tone?: string }) {
  return (
    <div className="rounded-md border border-white/10 bg-black/20 px-3 py-3">
      <div className={cn("text-2xl font-semibold tabular-nums", tone ?? "text-slate-100")}>{value}</div>
      <div className="mt-1 text-[11px] text-slate-500">{label}</div>
    </div>
  );
}

function MorningBriefView({ data }: { data: AnyRecord }) {
  const breadth = (data.market_breadth ?? {}) as AnyRecord;
  const indices = asArray(data.indices);
  const focus = asArray(data.focus_symbols);
  const news = asArray(data.top_news);
  const events = asArray(data.key_events);

  const mappedThemes = useMemo(() => {
    const rows = [...focus, ...news, ...events].slice(0, 8);
    return rows.map((item, index) => ({
      name: item.name || item.title || item.symbol || `线索 ${index + 1}`,
      aShare: item.symbol || item.category_label || item.source || "A 股方向",
      reason: item.reason || item.summary || item.description || item.title || "等待更多盘前证据确认",
      score: num(item.score ?? item.confidence, 0.55),
    }));
  }, [events, focus, news]);

  return (
    <>
      <Section
        title="早盘内参 · 08:50 推送"
        desc="把隔夜线索、指数状态和今日关注压缩成开盘前可读清单。"
        icon={<Bell className="h-4 w-4 text-amber-300" />}
      >
        <div className="grid gap-3 lg:grid-cols-[1.8fr_1fr]">
          <div className="rounded-md border-l-2 border-amber-300 bg-black/20 px-4 py-3">
            <div className="text-sm font-medium leading-7">{data.risk_note || "盘前先观察指数与主线承接，避免开盘情绪冲动追高。"}</div>
            <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-400">
              <span className="rounded bg-amber-300/10 px-2 py-1 text-amber-200">隔夜美股强弱</span>
              <span className="rounded bg-white/5 px-2 py-1">A 股映射</span>
              <span className="rounded bg-white/5 px-2 py-1">今日观察</span>
            </div>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <Metric label="上涨家数" value={breadth.advancers ?? "-"} tone="text-red-400" />
            <Metric label="下跌家数" value={breadth.decliners ?? "-"} tone="text-green-400" />
            <Metric label="成交额" value={`${breadth.turnover_billion ?? "-"} 亿`} />
          </div>
        </div>
      </Section>

      <Section title="指数与环境" desc="盘前先看大盘位置，不把单一题材当作全市场信号。" icon={<BarChart3 className="h-4 w-4 text-cyan-300" />}>
        {indices.length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {indices.slice(0, 8).map((item) => <IndexMini key={item.symbol || item.name} item={item} />)}
          </div>
        ) : <Empty>暂无指数数据</Empty>}
      </Section>

      <Section title="美股 / 新闻 → A 股传导分析" desc="按视频里的方式，把外部线索拆到 A 股可跟踪方向。" icon={<TrendingUp className="h-4 w-4 text-red-300" />}>
        {mappedThemes.length ? (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="px-2 py-2 text-left font-medium">方向 / 线索</th>
                  <th className="px-2 py-2 text-left font-medium">A 股映射</th>
                  <th className="px-2 py-2 text-left font-medium">逻辑</th>
                  <th className="px-2 py-2 text-right font-medium">强度</th>
                </tr>
              </thead>
              <tbody>
                {mappedThemes.map((item) => (
                  <tr key={`${item.name}-${item.aShare}`} className="border-t border-white/10">
                    <td className="px-2 py-3 font-medium text-slate-100">{item.name}</td>
                    <td className="px-2 py-3 text-amber-200">{item.aShare}</td>
                    <td className="px-2 py-3 text-xs leading-5 text-slate-400">{item.reason}</td>
                    <td className="px-2 py-3 text-right tabular-nums text-red-300">{Math.round(item.score * 100)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : <Empty>暂无盘前传导线索</Empty>}
      </Section>
    </>
  );
}

function IndexMini({ item }: { item: AnyRecord }) {
  return (
    <div className="rounded-md border border-white/10 bg-black/20 px-4 py-3">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-xs text-slate-400">{item.name || item.symbol}</span>
        <span className={cn("text-xs tabular-nums", pctTone(item.change_pct))}>{pct(item.change_pct)}</span>
      </div>
      <div className="mt-2 text-2xl font-semibold tabular-nums">{num(item.price).toLocaleString()}</div>
      <div className="mt-3 h-9 rounded bg-gradient-to-r from-red-500/20 via-red-400/10 to-transparent" />
    </div>
  );
}

function IntradayView({ data }: { data: AnyRecord }) {
  const breadth = (data.breadth ?? {}) as AnyRecord;
  const hotSectors = asArray(data.hot_sectors);
  const alerts = asArray(data.alerts);
  const tasks = asArray(data.scheduled_tasks);
  const temp = Math.round((num(breadth.advancers) / Math.max(1, num(breadth.total))) * 100);

  return (
    <>
      <Section title="A 股盘中监控" desc="结合实时盘面、资金和板块，判断当前是否适合进攻。" icon={<Gauge className="h-4 w-4 text-amber-300" />}>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <Metric label="情绪温度" value={Number.isFinite(temp) ? temp.toFixed(1) : "-"} tone={temp >= 60 ? "text-amber-300" : "text-slate-100"} />
          <Metric label="涨停" value={breadth.limit_up ?? "-"} tone="text-red-400" />
          <Metric label="跌停" value={breadth.limit_down ?? "-"} tone="text-green-400" />
          <Metric label="成交额" value={`${breadth.turnover_billion ?? "-"} 亿`} />
          <Metric label="上涨 / 下跌" value={`${breadth.advancers ?? "-"} / ${breadth.decliners ?? "-"}`} />
        </div>
      </Section>

      <Section title="行业板块涨幅榜" desc="盘中找强势方向，再看是否有持续扩散。" icon={<TrendingUp className="h-4 w-4 text-red-300" />}>
        {hotSectors.length ? <BarList rows={hotSectors} /> : <Empty>暂无行业板块数据</Empty>}
      </Section>

      <div className="grid gap-4 xl:grid-cols-2">
        <Section title="异动提醒" desc="从机会池提取盘中高波动、高置信度标的。" icon={<Zap className="h-4 w-4 text-amber-300" />}>
          {alerts.length ? (
            <div className="space-y-2">
              {alerts.map((item) => <AlertRow key={`${item.symbol}-${item.message}`} item={item} />)}
            </div>
          ) : <Empty>暂无异动提醒</Empty>}
        </Section>
        <Section title="定时任务" desc="自选股跟踪与盘中自动检查状态。" icon={<CalendarClock className="h-4 w-4 text-cyan-300" />}>
          {tasks.length ? (
            <div className="grid gap-2 md:grid-cols-2">
              {tasks.map((task) => (
                <div key={`${task.symbol}-${task.time}`} className="rounded-md border border-white/10 bg-black/20 px-3 py-3">
                  <div className="flex items-center justify-between gap-2 text-sm">
                    <span className="font-medium">{task.name || task.symbol}</span>
                    <span className={cn("rounded px-1.5 py-0.5 text-[10px]", task.enabled ? "bg-green-500/15 text-green-300" : "bg-slate-500/15 text-slate-400")}>
                      {task.enabled ? "启用" : "暂停"}
                    </span>
                  </div>
                  <div className="mt-1 text-xs text-slate-500">{task.symbol} · {task.time || "未设置时间"}</div>
                </div>
              ))}
            </div>
          ) : <Empty>暂无定时任务</Empty>}
        </Section>
      </div>
    </>
  );
}

function BarList({ rows }: { rows: AnyRecord[] }) {
  const max = Math.max(...rows.map((item) => Math.abs(num(item.change_pct))), 1);
  return (
    <div className="space-y-2">
      {rows.slice(0, 10).map((item) => {
        const value = num(item.change_pct);
        return (
          <div key={item.name} className="grid grid-cols-[120px_1fr_64px] items-center gap-3 text-xs">
            <div className="truncate font-medium text-slate-200">{item.name}</div>
            <div className="h-3 overflow-hidden rounded bg-white/10">
              <div className={cn("h-full", value >= 0 ? "bg-red-500" : "bg-green-500")} style={{ width: `${Math.max(4, Math.abs(value) / max * 100)}%` }} />
            </div>
            <div className={cn("text-right tabular-nums", pctTone(value))}>{pct(value)}</div>
          </div>
        );
      })}
    </div>
  );
}

function AlertRow({ item }: { item: AnyRecord }) {
  const hot = item.level === "hot";
  return (
    <div className="rounded-md border border-white/10 bg-black/20 px-3 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="font-medium">{item.name || item.symbol}</div>
        <span className={cn("rounded px-2 py-0.5 text-[11px]", hot ? "bg-red-500/15 text-red-300" : "bg-amber-400/15 text-amber-200")}>
          {hot ? "高波动" : "关注"}
        </span>
      </div>
      <div className="mt-1 text-xs text-slate-500">{item.symbol} · {pct(item.change_pct)}</div>
      <div className="mt-2 text-xs leading-5 text-slate-400">{item.message}</div>
    </div>
  );
}

function TailStrategyView({ data }: { data: AnyRecord }) {
  const decisions = asArray(data.decisions);
  const rules = asArray<string>(data.rules);
  const breadth = (data.breadth ?? {}) as AnyRecord;
  const groups = data.groups && typeof data.groups === "object" ? data.groups as Record<string, AnyRecord[]> : {};

  return (
    <>
      <Section title="尾盘观察清单" desc="先看市场环境，再决定尾盘是否做动作。" icon={<ShieldAlert className="h-4 w-4 text-amber-300" />}>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <Metric label="上涨 / 下跌" value={`${breadth.advancers ?? "-"} / ${breadth.decliners ?? "-"}`} />
          <Metric label="涨停" value={breadth.limit_up ?? "-"} tone="text-red-400" />
          <Metric label="跌停" value={breadth.limit_down ?? "-"} tone="text-green-400" />
          <Metric label="成交额" value={`${breadth.turnover_billion ?? "-"} 亿`} />
        </div>
        {rules.length ? (
          <div className="mt-4 grid gap-2 lg:grid-cols-3">
            {rules.map((rule, index) => (
              <div key={rule} className="rounded-md border border-amber-300/20 bg-amber-300/10 px-3 py-3 text-xs leading-5 text-amber-50">
                <span className="mr-2 text-amber-300">{index + 1}</span>{rule}
              </div>
            ))}
          </div>
        ) : null}
      </Section>

      <Section title="明日动作卡" desc="本地规则根据推荐、机会池和尾盘风险生成，不直接下单。" icon={<Target className="h-4 w-4 text-red-300" />}>
        {decisions.length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {decisions.map((item) => <DecisionCard key={`${item.symbol}-${item.action}`} item={item} />)}
          </div>
        ) : <Empty>暂无尾盘候选</Empty>}
      </Section>

      <Section title="按动作分组" desc="快速看哪些标的适合跟踪、等待或规避。" icon={<ClipboardList className="h-4 w-4 text-cyan-300" />}>
        {Object.keys(groups).length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {Object.entries(groups).map(([action, items]) => (
              <div key={action} className="rounded-md border border-white/10 bg-black/20 px-3 py-3">
                <div className="text-sm font-semibold text-amber-200">{action}</div>
                <div className="mt-2 space-y-1">
                  {items.slice(0, 5).map((item) => (
                    <div key={item.symbol} className="flex justify-between gap-2 text-xs">
                      <span className="truncate">{item.name || item.symbol}</span>
                      <span className={pctTone(item.change_pct)}>{pct(item.change_pct)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        ) : <Empty>暂无分组</Empty>}
      </Section>
    </>
  );
}

function DecisionCard({ item }: { item: AnyRecord }) {
  return (
    <div className="rounded-md border border-white/10 bg-black/20 px-4 py-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-base font-semibold">{item.name || item.symbol}</div>
          <div className="mt-0.5 text-xs text-slate-500">{item.symbol} · {item.source_label}</div>
        </div>
        <span className="rounded bg-amber-300/15 px-2 py-1 text-[11px] text-amber-200">{item.action}</span>
      </div>
      <div className="mt-3 flex items-center gap-3 text-xs">
        <span>强度 <b className="text-slate-100">{Math.round(num(item.score) * 100)}</b></span>
        <span className={pctTone(item.change_pct)}>{pct(item.change_pct)}</span>
        <span>{item.price ? amount(item.price) : ""}</span>
      </div>
      <p className="mt-3 text-xs leading-5 text-slate-400">{item.reason}</p>
      <p className="mt-2 border-l border-red-400/40 pl-2 text-xs leading-5 text-slate-500">{item.risk_note}</p>
    </div>
  );
}

function CloseReviewView({ data }: { data: AnyRecord }) {
  const summary = (data.summary ?? {}) as AnyRecord;
  const breadth = (data.breadth ?? {}) as AnyRecord;
  const watch = asArray(data.tomorrow_watch);
  const questions = asArray<string>(data.review_questions);
  const qa = [
    { q: "今天的钱在哪？", a: `上涨 ${breadth.advancers ?? "-"} 家，下跌 ${breadth.decliners ?? "-"} 家，成交额 ${breadth.turnover_billion ?? "-"} 亿。` },
    { q: "亏钱效应在哪里？", a: `跌停 ${breadth.limit_down ?? "-"} 家，若跌多涨少，明天先降低追高优先级。` },
    { q: "当前情绪阶段？", a: `推荐样本 ${summary.recommendation_count ?? 0} 个，跟踪收益样本 ${summary.tracked_return_count ?? 0} 个。` },
    { q: "明天进攻还是防守？", a: num(breadth.advancers) >= num(breadth.decliners) ? "进攻但控制仓位，优先看主线承接。" : "防守优先，等指数和赚钱效应共振。" },
    { q: "主线是否还有持续性？", a: watch.length ? "观察尾盘清单是否继续放量、换手和分歧转一致。" : "暂无足够候选，等待新证据。" },
    { q: "数据缺失怎么处理？", a: "缺失项只标注为待确认，不编造结论；下一交易日继续补数据。" },
  ];

  return (
    <>
      <Section title="收盘复盘问答" desc="像视频里一样，用固定问题逼自己把盘面讲清楚。" icon={<CheckCircle2 className="h-4 w-4 text-green-300" />}>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {qa.map((item) => (
            <div key={item.q} className="rounded-md border border-white/10 bg-black/20 px-4 py-4">
              <div className="text-sm font-semibold text-amber-200">{item.q}</div>
              <p className="mt-2 text-xs leading-6 text-slate-400">{item.a}</p>
            </div>
          ))}
        </div>
      </Section>

      <Section title="推荐表现" desc="把今日推荐的收益样本和胜率沉淀下来。" icon={<BarChart3 className="h-4 w-4 text-cyan-300" />}>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Metric label="推荐数量" value={summary.recommendation_count ?? 0} />
          <Metric label="收益样本" value={summary.tracked_return_count ?? 0} />
          <Metric label="平均收益" value={summary.avg_latest_return_pct === null || summary.avg_latest_return_pct === undefined ? "-" : pct(summary.avg_latest_return_pct)} tone={pctTone(summary.avg_latest_return_pct)} />
          <Metric label="胜率" value={summary.win_rate === null || summary.win_rate === undefined ? "-" : `${num(summary.win_rate).toFixed(1)}%`} />
        </div>
      </Section>

      <Section title="明日观察清单" desc="尾盘策略沉淀为明天的第一批观察对象。" icon={<ClipboardList className="h-4 w-4 text-amber-300" />}>
        {watch.length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            {watch.map((item) => (
              <div key={`${item.symbol}-${item.action}`} className="rounded-md border border-white/10 bg-black/20 px-3 py-3">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{item.name || item.symbol}</span>
                  <span className="rounded bg-white/10 px-1.5 py-0.5 text-[10px] text-slate-300">{item.action}</span>
                </div>
                <div className="mt-1 text-xs text-slate-500">{item.symbol}</div>
                <p className="mt-2 text-xs leading-5 text-slate-400">{item.reason || "明日继续观察承接。"}</p>
              </div>
            ))}
          </div>
        ) : <Empty>暂无明日观察清单</Empty>}
      </Section>

      {questions.length ? (
        <Section title="复盘问题库" desc="每天固定追问，形成可归档的复盘记录。" icon={<TrendingDown className="h-4 w-4 text-green-300" />}>
          <div className="grid gap-2 md:grid-cols-3">
            {questions.map((q) => <div key={q} className="rounded border border-white/10 bg-black/20 px-3 py-3 text-xs text-slate-300">{q}</div>)}
          </div>
        </Section>
      ) : null}
    </>
  );
}

function MarketStagePage({ stage }: { stage: MarketDashboardStage }) {
  const [payload, setPayload] = useState<MarketDashboardStageResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setRefreshing(true);
    try {
      const data = await api.getMarketDashboardStage(stage);
      setPayload(data);
      setError(data.status === "error" ? data.error || "加载失败" : null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [stage]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <StageShell
      stage={stage}
      payload={payload}
      loading={loading}
      refreshing={refreshing}
      error={error}
      onRefresh={load}
    />
  );
}

export function MorningBrief() {
  return <MarketStagePage stage="morning-brief" />;
}

export function IntradayMonitor() {
  return <MarketStagePage stage="intraday-monitor" />;
}

export function TailStrategy() {
  return <MarketStagePage stage="tail-strategy" />;
}

export function CloseReview() {
  return <MarketStagePage stage="close-review" />;
}
