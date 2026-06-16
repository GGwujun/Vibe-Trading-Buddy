import { Suspense, lazy, type ComponentType } from "react";
import { Navigate, Outlet, createBrowserRouter } from "react-router-dom";
import { Layout } from "@/components/layout/Layout";
import { DisclaimerModal } from "@/components/DisclaimerModal";
import { isAdmin } from "@/lib/apiAuth";
import { useAuthState } from "@/hooks/useAuthState";

const Home = lazy(() => import("@/pages/Home").then((m) => ({ default: m.Home })));
const Agent = lazy(() => import("@/pages/Agent").then((m) => ({ default: m.Agent })));
const RunDetail = lazy(() =>
  import("@/pages/RunDetail").then((m) => ({ default: m.RunDetail })),
);
const Compare = lazy(() =>
  import("@/pages/Compare").then((m) => ({ default: m.Compare })),
);
const Settings = lazy(() =>
  import("@/pages/Settings").then((m) => ({ default: m.Settings })),
);
const Correlation = lazy(() =>
  import("@/pages/Correlation").then((m) => ({ default: m.Correlation })),
);
const AlphaZoo = lazy(() =>
  import("@/pages/AlphaZoo").then((m) => ({ default: m.AlphaZoo })),
);
const Events = lazy(() =>
  import("@/pages/Events").then((m) => ({ default: m.Events })),
);
const TrackingDashboard = lazy(() =>
  import("@/pages/TrackingDashboard").then((m) => ({ default: m.TrackingDashboard })),
);
const WatchlistSchedule = lazy(() =>
  import("@/pages/WatchlistSchedule").then((m) => ({ default: m.WatchlistSchedule })),
);
const News = lazy(() =>
  import("@/pages/News").then((m) => ({ default: m.News })),
);
const Opportunity = lazy(() =>
  import("@/pages/Opportunity").then((m) => ({ default: m.Opportunity })),
);
const LogicChain = lazy(() =>
  import("@/pages/LogicChain").then((m) => ({ default: m.LogicChain })),
);
const AlphaForge = lazy(() =>
  import("@/pages/AlphaForge").then((m) => ({ default: m.AlphaForge })),
);
const FundArbitrage = lazy(() =>
  import("@/pages/FundArbitrage").then((m) => ({ default: m.FundArbitrage })),
);
const FundOpportunity = lazy(() =>
  import("@/pages/FundOpportunity").then((m) => ({ default: m.FundOpportunity })),
);
const LoginPage = lazy(() =>
  import("@/pages/auth/LoginPage").then((m) => ({ default: m.LoginPage })),
);
const RegisterPage = lazy(() =>
  import("@/pages/auth/RegisterPage").then((m) => ({ default: m.RegisterPage })),
);
const Account = lazy(() =>
  import("@/pages/Account").then((m) => ({ default: m.Account })),
);

function PageLoader() {
  return (
    <div className="flex h-[60vh] items-center justify-center text-muted-foreground">
      加载中…
    </div>
  );
}

function wrap(Component: ComponentType) {
  return (
    <Suspense fallback={<PageLoader />}>
      <Component />
    </Suspense>
  );
}

/**
 * Auth guard. Validates a token exists (and refreshes the user via /auth/me).
 * If not logged in → redirect to /login. If logged in but disclaimer not yet
 * accepted → render the app behind a blocking DisclaimerModal.
 */
function RequireAuth() {
  const { authed, disclaimerAccepted, recheck, loading } = useAuthState();

  if (loading) {
    return (
      <div className="flex h-[60vh] items-center justify-center text-muted-foreground">
        加载中…
      </div>
    );
  }
  if (!authed) {
    return <Navigate to="/login" replace />;
  }
  return (
    <>
      <Outlet />
      {!disclaimerAccepted && <DisclaimerModal onAccepted={recheck} />}
    </>
  );
}

/** Admin guard. Non-admins are bounced to home. */
function RequireAdmin() {
  if (!isAdmin()) {
    return <Navigate to="/" replace />;
  }
  return <Outlet />;
}

export const router = createBrowserRouter([
  // Public routes — no auth guard
  { path: "/login", element: wrap(LoginPage) },
  { path: "/register", element: wrap(RegisterPage) },
  // Protected app
  {
    element: <RequireAuth />,
    children: [
      {
        element: <Layout />,
        children: [
          { path: "/", element: wrap(Home) },
          { path: "/agent", element: wrap(Agent) },
          { path: "/settings", element: wrap(Settings) },
          { path: "/runs/:runId", element: wrap(RunDetail) },
          { path: "/compare", element: wrap(Compare) },
          { path: "/correlation", element: wrap(Correlation) },
          { path: "/events", element: wrap(Events) },
          { path: "/tracking-dashboard", element: wrap(TrackingDashboard) },
          { path: "/watchlist-schedule", element: wrap(WatchlistSchedule) },
          { path: "/news", element: wrap(News) },
          { path: "/opportunity", element: wrap(Opportunity) },
          { path: "/logic-chain", element: wrap(LogicChain) },
          { path: "/alpha-forge", element: wrap(AlphaForge) },
          { path: "/fund-arbitrage", element: wrap(FundArbitrage) },
          { path: "/fund-opportunity", element: wrap(FundOpportunity) },
          { path: "/account", element: wrap(Account) },
        ],
      },
      // Admin-only routes (Factor Zoo) — wrapped in RequireAdmin.
      {
        element: <RequireAdmin />,
        children: [
          {
            element: <Layout />,
            children: [
              { path: "/alpha-zoo", element: wrap(AlphaZoo) },
              { path: "/alpha-zoo/bench", element: wrap(AlphaZoo) },
              { path: "/alpha-zoo/compare", element: wrap(AlphaZoo) },
              { path: "/alpha-zoo/:alphaId", element: wrap(AlphaZoo) },
            ],
          },
        ],
      },
    ],
  },
]);
