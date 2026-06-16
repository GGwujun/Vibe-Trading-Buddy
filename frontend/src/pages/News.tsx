import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ChevronRight, ExternalLink, Newspaper } from "lucide-react";
import { api, type NewsArticle } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  buildIntelPath,
  MarketEmptyState,
  MarketErrorState,
  MarketIntelHeader,
  MarketLoadingState,
  normalizeChinaSymbol,
  plainSymbol,
  SymbolActionBar,
} from "@/components/market/MarketIntelShell";
import { toast } from "sonner";

const REFRESH_MS = 300_000;
const LS_NEWS_KEY = "vibe-news-watchlist";

function loadWatchlist(): string[] {
  try {
    const raw = localStorage.getItem(LS_NEWS_KEY);
    if (raw) return JSON.parse(raw).map((p: { symbol: string }) => p.symbol);
  } catch {
    return [];
  }
  return [];
}

function timeAgo(dateStr: string): string {
  if (!dateStr) return "";
  const date = new Date(dateStr);
  if (Number.isNaN(date.getTime())) return dateStr;
  const diff = Date.now() - date.getTime();
  const mins = Math.max(0, Math.floor(diff / 60000));
  if (mins < 60) return `${mins} 分钟前`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} 小时前`;
  return `${Math.floor(hrs / 24)} 天前`;
}

function extractSymbols(article: NewsArticle): string[] {
  const text = `${article.title} ${article.snippet}`;
  const matches = text.match(/\b(?:00|30|60|68)\d{4}\b/g) ?? [];
  return Array.from(new Set(matches)).slice(0, 3);
}

export function News() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [articles, setArticles] = useState<NewsArticle[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState(searchParams.get("q") ?? "");
  const [symbol, setSymbol] = useState(plainSymbol(searchParams.get("symbol") ?? ""));
  const [activeQuery, setActiveQuery] = useState(searchParams.get("q") ?? "");
  const [updatedAt, setUpdatedAt] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [stockNews, setStockNews] = useState<{ code: string; name: string; articles: NewsArticle[] } | null>(null);
  const [stockLoading, setStockLoading] = useState(false);
  const [mode, setMode] = useState<"market" | "watchlist">("market");
  const [watchlistStocks, setWatchlistStocks] = useState<{ code: string; name: string }[]>([]);
  const [selectedStock, setSelectedStock] = useState<string | null>(searchParams.get("symbol"));
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchNews = useCallback((nextQuery?: string, silent = false) => {
    if (!silent) setRefreshing(true);
    api.listNews(nextQuery || undefined)
      .then((res) => {
        setArticles(res.articles);
        setActiveQuery(res.query);
        setUpdatedAt(res.updated_at);
        setError(null);
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "获取新闻线索失败");
      })
      .finally(() => {
        setLoading(false);
        setRefreshing(false);
      });
  }, []);

  const fetchStockNews = useCallback((code: string) => {
    const normalized = normalizeChinaSymbol(code);
    setStockLoading(true);
    setSelectedStock(normalized);
    setMode("watchlist");
    api.getStockNews(normalized)
      .then((res) => {
        setStockNews({ code: res.code, name: res.name, articles: res.articles });
        setUpdatedAt(res.updated_at);
      })
      .catch(() => toast.error("获取标的相关新闻失败"))
      .finally(() => setStockLoading(false));
  }, []);

  const applySearch = useCallback(() => {
    const next = new URLSearchParams();
    if (query.trim()) next.set("q", query.trim());
    if (symbol.trim()) next.set("symbol", normalizeChinaSymbol(symbol));
    setSearchParams(next);
    if (symbol.trim()) fetchStockNews(symbol);
    else {
      setMode("market");
      setStockNews(null);
      fetchNews(query.trim() || undefined);
    }
  }, [fetchNews, fetchStockNews, query, setSearchParams, symbol]);

  useEffect(() => {
    setQuery(searchParams.get("q") ?? "");
    setSymbol(plainSymbol(searchParams.get("symbol") ?? ""));
  }, [searchParams]);

  useEffect(() => {
    const q = searchParams.get("q") ?? "";
    const sym = searchParams.get("symbol");
    fetchNews(q || undefined, true);
    if (sym) fetchStockNews(sym);
  }, [fetchNews, fetchStockNews, searchParams]);

  useEffect(() => {
    intervalRef.current = setInterval(() => fetchNews(activeQuery || undefined, true), REFRESH_MS);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [activeQuery, fetchNews]);

  useEffect(() => {
    const codes = loadWatchlist();
    Promise.all(codes.slice(0, 10).map(async (code) => {
      try {
        const snap = await api.getPositionSnapshot(code);
        return { code, name: snap.name };
      } catch {
        return { code, name: code };
      }
    })).then(setWatchlistStocks);
  }, []);

  const displayArticles = mode === "watchlist" && stockNews ? stockNews.articles : articles;
  const flowParams = new URLSearchParams(searchParams);
  if (selectedStock) flowParams.set("symbol", selectedStock);

  return (
    <div className="flex h-[calc(100vh-3.5rem)] flex-col">
      <MarketIntelHeader
        active="news"
        query={query}
        symbol={symbol}
        onQueryChange={setQuery}
        onSymbolChange={setSymbol}
        onSearch={applySearch}
        onRefresh={() => fetchNews(activeQuery || undefined)}
        refreshing={refreshing}
        updatedAt={updatedAt}
      />

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <main className="min-w-0 flex-1 overflow-y-auto">
          <div className="border-b px-4 py-3 md:px-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-sm font-medium">{mode === "watchlist" && stockNews ? `${stockNews.name} 新闻` : "市场新闻线索"}</p>
                <p className="text-xs text-muted-foreground">
                  {activeQuery ? `当前关键词：${activeQuery}` : "来自财经新闻和搜索源的最新线索"}
                </p>
              </div>
              <div className="flex items-center gap-1 rounded-lg bg-muted/40 p-1">
                {([
                  ["market", "市场要闻"],
                  ["watchlist", "自选相关"],
                ] as const).map(([id, label]) => (
                  <button
                    key={id}
                    type="button"
                    onClick={() => {
                      setMode(id);
                      if (id === "market") setStockNews(null);
                    }}
                    className={cn(
                      "rounded-md px-3 py-1.5 text-xs transition-colors",
                      mode === id ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="p-4 md:p-6">
            {loading ? (
              <MarketLoadingState label="正在加载新闻线索" />
            ) : error ? (
              <MarketErrorState message={error} onRetry={() => fetchNews(activeQuery || undefined)} />
            ) : displayArticles.length === 0 ? (
              <MarketEmptyState
                icon={Newspaper}
                title="暂无新闻线索"
                description="可以换一个关键词，或从自选股入口查看单个标的新闻。"
              />
            ) : (
              <div className="space-y-3">
                {displayArticles.map((article, index) => {
                  const symbols = extractSymbols(article);
                  return (
                    <article key={article.url || index} className="rounded-lg border bg-card p-4 transition-colors hover:bg-muted/20">
                      <div className="flex items-start gap-3">
                        <div className="min-w-0 flex-1">
                          <a
                            href={article.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="group inline-flex items-start gap-1.5 text-sm font-medium leading-snug hover:text-primary"
                          >
                            <span className="line-clamp-2">{article.title}</span>
                            <ExternalLink className="mt-0.5 h-3.5 w-3.5 shrink-0 opacity-45 group-hover:opacity-100" />
                          </a>
                          {article.snippet && (
                            <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-muted-foreground">{article.snippet}</p>
                          )}
                          <div className="mt-3 flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground">
                            {article.source && <span className="rounded bg-muted px-1.5 py-0.5">{article.source}</span>}
                            {article.published && <span>{timeAgo(article.published)}</span>}
                            {symbols.map((item) => {
                              const next = new URLSearchParams(searchParams);
                              next.set("symbol", normalizeChinaSymbol(item));
                              return (
                                <Link
                                  key={item}
                                  to={buildIntelPath("/logic-chain", next)}
                                  className="rounded bg-primary/10 px-1.5 py-0.5 font-mono text-primary hover:bg-primary/15"
                                >
                                  {item}
                                </Link>
                              );
                            })}
                          </div>
                        </div>
                        <Link
                          to={buildIntelPath("/events", flowParams)}
                          className="hidden rounded-md border px-2.5 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground sm:block"
                        >
                          验事件
                        </Link>
                      </div>
                    </article>
                  );
                })}
              </div>
            )}
          </div>
        </main>

        <aside className="hidden w-[260px] shrink-0 overflow-y-auto border-l bg-muted/10 p-4 lg:block">
          <div className="space-y-4">
            <section className="rounded-lg border bg-card p-3">
              <p className="text-sm font-medium">研究下一步</p>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                新闻负责发现线索，后续需要到事件雷达验证影响，再沉淀到机会清单。
              </p>
              <SymbolActionBar
                symbol={selectedStock || symbol || "600519"}
                label="快捷流转"
                params={flowParams}
                className="mt-3"
              />
            </section>

            <section className="rounded-lg border bg-card p-3">
              <div className="mb-2 flex items-center justify-between">
                <p className="text-sm font-medium">自选股新闻</p>
                {stockLoading && <span className="text-xs text-muted-foreground">加载中</span>}
              </div>
              {watchlistStocks.length === 0 ? (
                <p className="text-xs text-muted-foreground">暂无自选股，可先在跟踪看板中维护关注标的。</p>
              ) : (
                <div className="space-y-1">
                  {watchlistStocks.map((item) => (
                    <button
                      key={item.code}
                      type="button"
                      onClick={() => fetchStockNews(item.code)}
                      className={cn(
                        "flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-xs transition-colors",
                        selectedStock === normalizeChinaSymbol(item.code)
                          ? "bg-primary/10 text-primary"
                          : "text-muted-foreground hover:bg-muted hover:text-foreground",
                      )}
                    >
                      <span className="truncate">{item.name}</span>
                      <ChevronRight className="h-3.5 w-3.5 shrink-0 opacity-50" />
                    </button>
                  ))}
                </div>
              )}
            </section>
          </div>
        </aside>
      </div>
    </div>
  );
}
