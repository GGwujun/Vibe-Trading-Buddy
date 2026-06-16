import { useEffect, useState } from "react";
import { Link, Outlet, useLocation, useSearchParams } from "react-router-dom";
import { BarChart3, Bot, Moon, Sun, Plus, Trash2, Pencil, MessageSquare, ChevronsLeft, ChevronsRight, Settings, Layers, Loader2, TrendingUp, Target, Newspaper, Lightbulb, GitBranch, Zap, Coins, Search, User, CalendarClock } from "lucide-react";
import { cn } from "@/lib/utils";
import { useDarkMode } from "@/hooks/useDarkMode";
import { api, type SessionItem } from "@/lib/api";
import { isAdmin } from "@/lib/apiAuth";
import { useAgentStore } from "@/stores/agent";
import { ConnectionBanner } from "@/components/layout/ConnectionBanner";
import { SigmXLogo } from "@/components/brand/SigmXLogo";
import { Watermark } from "@/components/Watermark";

// App version injected at build time from package.json (see vite.config.ts `define`).
// Declared globally so TS doesn't complain about the runtime constant.
declare const __APP_VERSION__: string;
const APP_VERSION = `v${__APP_VERSION__}`;

const NAV_GROUPS = [
  {
    title: "工作台",
    items: [
      { to: "/", icon: BarChart3, label: "今日总览" },
    ],
  },
  {
    title: "AI 投研",
    items: [
      { to: "/agent", icon: Bot, label: "智能体" },
      { to: "/alpha-forge", icon: Zap, label: "AlphaForge" },
      { to: "/fund-arbitrage", icon: Coins, label: "套利分析" },
    ],
  },
  {
    title: "交易决策",
    items: [
      { to: "/tracking-dashboard", icon: Target, label: "跟踪看板" },
      { to: "/watchlist-schedule", icon: CalendarClock, label: "自选 & 定时" },
    ],
  },
  {
    title: "市场情报",
    items: [
      { to: "/news", icon: Newspaper, label: "新闻线索" },
      { to: "/events", icon: TrendingUp, label: "事件雷达" },
      { to: "/opportunity", icon: Lightbulb, label: "机会清单" },
      { to: "/logic-chain", icon: GitBranch, label: "逻辑链" },
      { to: "/fund-opportunity", icon: Search, label: "套利机会" },
      { to: "/correlation", icon: BarChart3, label: "相关性矩阵" },
    ],
  },
  {
    title: "策略实验室",
    items: [
      { to: "/alpha-zoo", icon: Layers, label: "因子工厂" },
    ],
  },
];

export function Layout() {
  const { pathname } = useLocation();
  const [searchParams] = useSearchParams();
  const { dark, toggle } = useDarkMode();
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const sseStatus = useAgentStore(s => s.sseStatus);
  const sseRetryAttempt = useAgentStore(s => s.sseRetryAttempt);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("qa-sidebar") === "collapsed");

  const activeSessionId = searchParams.get("session");
  const streamingSessionId = useAgentStore(s => s.streamingSessionId);

  useEffect(() => {
    localStorage.setItem("qa-sidebar", collapsed ? "collapsed" : "expanded");
  }, [collapsed]);

  const loadSessions = () => {
    api.listSessions()
      .then((list) => setSessions(Array.isArray(list) ? list : []))
      .catch(() => {})
      .finally(() => setSessionsLoading(false));
  };

  // Load sessions on mount. Also refresh when navigating TO /agent or when
  // the active session changes (covers new session creation from Agent).
  const isAgentPage = pathname.startsWith("/agent");
  useEffect(() => { loadSessions(); }, [isAgentPage, activeSessionId]);

  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const deleteSession = async (sid: string) => {
    try {
      await api.deleteSession(sid);
      setSessions((prev) => prev.filter((s) => s.session_id !== sid));
    } catch { /* ignore */ }
    setDeleteTarget(null);
  };

  const renameSession = async (sid: string) => {
    if (!renameValue.trim()) { setRenameTarget(null); return; }
    try {
      await api.renameSession(sid, renameValue.trim());
      setSessions((prev) => prev.map((s) => s.session_id === sid ? { ...s, title: renameValue.trim() } : s));
    } catch { /* ignore */ }
    setRenameTarget(null);
  };

  return (
    <div className="flex h-screen bg-background">
      <Watermark />
      {/* Sidebar */}
      <aside
        data-sidebar
        className={cn(
        "border-r bg-card text-muted-foreground dark:border-[#171b24] dark:bg-[#080a0f] dark:text-slate-300 flex flex-col shrink-0 overflow-hidden transition-all duration-200",
        collapsed ? "w-12" : "w-64"
      )}>
        {/* Brand */}
        <div className={cn("border-b dark:border-white/10", collapsed ? "p-2 flex justify-center" : "p-4")}>
          <Link to="/" className={cn("flex items-center font-bold text-base tracking-tight", collapsed ? "justify-center" : "gap-2")}>
            <SigmXLogo className="h-7 w-7" />
            {!collapsed && <span className="text-foreground dark:text-white">SigmX</span>}
          </Link>
        </div>

        {/* Nav */}
        <nav className={cn("shrink-0 overflow-auto", collapsed ? "p-1 space-y-1" : "max-h-[46vh] p-2 space-y-3")}>
          {/* "策略实验室" group (Factor Zoo) is admin-only. */}
          {NAV_GROUPS.filter(g => isAdmin() || g.title !== "策略实验室").map((group) => (
            <div key={group.title} className="space-y-0.5">
              {!collapsed && (
                <div className="px-3 pb-1 pt-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground/70 dark:text-slate-500">
                  {group.title}
                </div>
              )}
              {group.items.map(({ to, icon: Icon, label }) => {
                const active = to === "/" ? pathname === "/" : pathname.startsWith(to);
                return (
                  <Link
                    key={to}
                    to={to}
                    className={cn(
                      "flex items-center rounded-md text-sm transition-colors",
                      collapsed ? "justify-center p-2" : "gap-3 px-3 py-2",
                      active
                        ? "bg-primary/15 text-primary font-medium shadow-[inset_2px_0_0_hsl(var(--primary))]"
                        : "text-muted-foreground hover:bg-muted/70 hover:text-foreground dark:text-slate-400 dark:hover:bg-white/[0.06] dark:hover:text-slate-100"
                    )}
                    title={collapsed ? `${group.title} / ${label}` : undefined}
                  >
                    <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                    {!collapsed && label}
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>

        {/* Agent sessions — always available in the expanded sidebar */}
        {!collapsed && (
          <div className="min-h-0 flex-1 overflow-hidden border-t dark:border-white/10 mt-2 flex flex-col">
            <div className="flex items-center justify-between px-4 py-2">
              <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground/80 dark:text-slate-500">
                <MessageSquare className="h-3.5 w-3.5" />
                会话
              </span>
              <Link
                to="/agent"
                className="flex items-center gap-1 text-xs text-muted-foreground hover:text-primary dark:text-slate-500 transition-colors"
                title="新建会话"
              >
                <Plus className="h-3.5 w-3.5" />
              </Link>
            </div>

            <div className="min-h-0 px-2 pb-2 space-y-0.5 overflow-auto flex-1">
              {sessionsLoading ? (
                <div className="space-y-1.5 px-2 py-1">
                  {[1, 2, 3].map((i) => (
                    <div key={i} className="h-7 rounded-md bg-muted animate-pulse dark:bg-white/[0.06]" />
                  ))}
                </div>
              ) : sessions.length === 0 ? (
                <p className="px-3 py-2 text-xs text-muted-foreground/80 dark:text-slate-500">暂无会话</p>
              ) : null}
              {sessions.map((s) => {
                const isActive = s.session_id === activeSessionId;
                const isDeleting = deleteTarget === s.session_id;
                const isRenaming = renameTarget === s.session_id;
                return (
                  <div key={s.session_id} className="group relative flex items-center">
                    {isRenaming ? (
                      <input
                        autoFocus
                        value={renameValue}
                        onChange={(e) => setRenameValue(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter") renameSession(s.session_id); if (e.key === "Escape") setRenameTarget(null); }}
                        onBlur={() => renameSession(s.session_id)}
                        className="flex-1 min-w-0 pl-3 pr-2 py-1 rounded-md text-xs border border-primary bg-background text-foreground outline-none dark:bg-[#0d1118] dark:text-slate-100"
                      />
                    ) : (
                      <Link
                        to={`/agent?session=${s.session_id}`}
                        className={cn(
                          "flex-1 min-w-0 pl-3 pr-14 py-1.5 rounded-md text-xs transition-colors truncate block border-l-2",
                          isActive
                            ? "border-l-primary bg-primary/15 text-primary font-medium"
                            : "border-l-transparent text-muted-foreground hover:bg-muted/70 hover:text-foreground dark:text-slate-500 dark:hover:bg-white/[0.06] dark:hover:text-slate-100"
                        )}
                        title={s.title || s.session_id}
                      >
                        <span className="flex items-center gap-1.5">
                          {streamingSessionId === s.session_id ? (
                            <Loader2 className="h-3 w-3 shrink-0 animate-spin text-primary" />
                          ) : (
                            <span className={cn(
                              "h-1.5 w-1.5 rounded-full shrink-0",
                              isActive ? "bg-primary/80" : "bg-muted-foreground/50"
                            )} />
                          )}
                          {s.title || s.session_id.slice(0, 16)}
                        </span>
                      </Link>
                    )}
                    {!isRenaming && isDeleting ? (
                      <div className="absolute right-0.5 flex items-center gap-0.5">
                        <button onClick={() => deleteSession(s.session_id)} className="p-1 text-danger hover:bg-danger/10 rounded text-[10px] font-medium">确认</button>
                        <button onClick={() => setDeleteTarget(null)} className="p-1 text-muted-foreground hover:bg-muted rounded text-[10px] dark:text-slate-400 dark:hover:bg-white/[0.06]">取消</button>
                      </div>
                    ) : !isRenaming ? (
                      <div className="absolute right-1 opacity-0 group-hover:opacity-100 flex items-center gap-0.5 transition-opacity">
                        <button
                          onClick={(e) => { e.preventDefault(); e.stopPropagation(); setRenameTarget(s.session_id); setRenameValue(s.title || ""); }}
                          className="p-1 text-muted-foreground hover:text-foreground rounded dark:text-slate-500 dark:hover:text-slate-100"
                          title="重命名"
                        >
                          <Pencil className="h-3 w-3" />
                        </button>
                        <button
                          onClick={(e) => { e.preventDefault(); e.stopPropagation(); setDeleteTarget(s.session_id); }}
                          className="p-1 text-muted-foreground hover:text-danger rounded dark:text-slate-500"
                          title="删除？"
                        >
                          <Trash2 className="h-3 w-3" />
                        </button>
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Spacer keeps the footer pinned down while the sidebar is collapsed. */}
        {collapsed && <div className="flex-1" />}

        {/* Footer */}
        <div className={cn("border-t dark:border-white/10", collapsed ? "p-1 flex flex-col items-center gap-1" : "p-3 space-y-2")}>
          {collapsed ? (
            <>
              <Link to="/account" className={cn("p-1.5 rounded transition-colors", pathname.startsWith("/account") ? "text-primary" : "text-muted-foreground hover:text-foreground dark:text-slate-500 dark:hover:text-slate-100")} title="个人中心">
                <User className="h-3.5 w-3.5" />
              </Link>
              <Link to="/settings" className="p-1.5 text-muted-foreground hover:text-foreground dark:text-slate-500 dark:hover:text-slate-100 rounded transition-colors" title="设置">
                <Settings className="h-3.5 w-3.5" />
              </Link>
              <button onClick={toggle} className="p-1.5 text-muted-foreground hover:text-foreground dark:text-slate-500 dark:hover:text-slate-100 rounded transition-colors" title={dark ? "浅色模式" : "深色模式"}>
                {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
              </button>
              <button onClick={() => setCollapsed(false)} className="p-1.5 text-muted-foreground hover:text-foreground dark:text-slate-500 dark:hover:text-slate-100 rounded transition-colors" title="展开侧栏">
                <ChevronsRight className="h-3.5 w-3.5" />
              </button>
            </>
          ) : (
            <>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Link
                    to="/account"
                    className={cn(
                      "flex items-center gap-1.5 text-xs transition-colors",
                      pathname.startsWith("/account") ? "text-primary" : "text-muted-foreground hover:text-foreground dark:text-slate-500 dark:hover:text-slate-100",
                    )}
                  >
                    <User className="h-3.5 w-3.5" />
                    个人中心
                  </Link>
                  <Link
                    to="/settings"
                    className={cn(
                      "flex items-center gap-1.5 text-xs transition-colors",
                      pathname.startsWith("/settings") ? "text-primary" : "text-muted-foreground hover:text-foreground dark:text-slate-500 dark:hover:text-slate-100",
                    )}
                  >
                    <Settings className="h-3.5 w-3.5" />
                    设置
                  </Link>
                  <button
                    onClick={toggle}
                    className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground dark:text-slate-500 dark:hover:text-slate-100 transition-colors"
                  >
                    {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
                    {dark ? "浅色" : "深色"}
                  </button>
                </div>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => setCollapsed(true)}
                    className="p-1 text-muted-foreground hover:text-foreground dark:text-slate-500 dark:hover:text-slate-100 rounded transition-colors"
                    title="收起侧栏"
                  >
                    <ChevronsLeft className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
              <p className="text-xs text-muted-foreground/70 dark:text-slate-600">{APP_VERSION}</p>
            </>
          )}
        </div>
      </aside>

      {/* Main */}
      <div className="flex-1 flex flex-col overflow-hidden">
        <ConnectionBanner status={sseStatus} retryAttempt={sseRetryAttempt} />
        <main className="flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
