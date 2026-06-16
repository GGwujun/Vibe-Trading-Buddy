import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import pkg from "./package.json";

// 纯 API 路由：不会作为浏览器页面打开，始终代理到后端。
const API_ONLY_PATHS = [
  "/sessions",
  "/swarm/presets",
  "/swarm/runs",
  "/settings/llm",
  "/settings/data-sources",
  "/mandate",
  "/live",
  "/upload",
  "/shadow-reports",
  "/alpha-forge",
  "/auth",
  "/credits",
  "/notify",
  "/tpdog",
  "/data-health",
  "/market-sync",
  "/tracking",
  // Fund API uses specific sub-paths (NOT bare "/fund" — that prefix would
  // swallow the /fund-opportunity and /fund-arbitrage SPA pages on refresh).
  "/fund/scan",
  "/fund/source-status",
  "/fund/analyze",
  "/fund/runs",
  "/fund/reports"
];

// SPA page paths that ALSO have API endpoints at the same URL.
// 使用 HTML 兜底：浏览器刷新返回 index.html，JS fetch 返回 JSON。
const SPA_WITH_API_PATHS = [
  "/events",
  "/news",
  "/opportunity",
  "/logic-chain",
  "/tracking-dashboard",
  "/account"
];

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.VITE_API_URL || "http://localhost:8000";
  const apiProxy = { target: apiTarget, changeOrigin: true };
  const apiProxyWithHtmlFallback = {
    ...apiProxy,
    bypass(req: { headers: { accept?: string } }) {
      if (req.headers.accept?.includes("text/html")) {
        return "/index.html";
      }
    },
  };

  return {
    plugins: [react()],
    // Inject app version from package.json as a build-time constant so the
    // UI footer stays in sync without a hardcoded duplicate.
    define: {
      __APP_VERSION__: JSON.stringify(pkg.version),
    },
    resolve: {
      alias: { "@": path.resolve(__dirname, "./src") },
    },
    server: {
      port: 5899,
      proxy: {
        // API-only routes: always proxy to backend
        ...Object.fromEntries(API_ONLY_PATHS.map((p) => [p, apiProxy])),
        // Single-fund detail API: /fund/{6-digit-code} (dynamic, not a fixed prefix)
        "^/fund/\\d{6}/?$": apiProxy,
        // /tracking-dashboard 是前端页面，不能被 /position 前缀误代理。
        "^/position/": apiProxy,
        // SPA pages that share a URL with API: HTML fallback on browser refresh
        ...Object.fromEntries(SPA_WITH_API_PATHS.map((p) => [p, apiProxyWithHtmlFallback])),
        // SPA RunDetail page — only the two-segment ``/runs/{id}``
        // form should fall back to ``index.html`` on browser navigation.
        // ``/runs/{id}/code`` and ``/runs/{id}/pine`` are API-only and
        // must keep proxying to the backend even when Accept is text/html.
        "^/runs/[^/]+/?$": apiProxyWithHtmlFallback,
        "/runs": apiProxy,
        "/correlation": apiProxyWithHtmlFallback,
        "^/alpha(?:/|$)": apiProxy,
      },
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks: {
            "vendor-react": ["react", "react-dom", "react-router-dom"],
            "vendor-charts": ["echarts"],
          },
        },
      },
    },
  };
});
