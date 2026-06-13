/**
 * Auth token + user storage and request injection.
 *
 * Two credential modes:
 *  1. **User JWT** (primary): stored under TOKEN_KEY after login/register.
 *     Sent as `Authorization: Bearer <jwt>`. The backend validates it as a JWT
 *     first, then falls back to the legacy API key.
 *  2. **Legacy API key** (remote deployments): stored under APIKEY_KEY for
 *     backwards compatibility. Used only if no JWT is present.
 *
 * All sensitive requests go through `authHeaders()` / `withAuthQuery()` which
 * prefer the JWT, so every existing api.ts call inherits login gating for free.
 */

const TOKEN_KEY = "sigmx_auth_token";
const USER_KEY = "sigmx_user";
const APIKEY_KEY = "vibe_trading_api_auth_key"; // legacy

export interface AuthUser {
  id: string;
  email: string;
  disclaimer_accepted_at: string | null;
  created_at: string;
  is_admin?: boolean;
}

/* ---------- JWT token ---------- */
export function getToken(): string {
  return window.localStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(token: string): void {
  const t = token.trim();
  if (t) window.localStorage.setItem(TOKEN_KEY, t);
  else window.localStorage.removeItem(TOKEN_KEY);
}

/* ---------- user ---------- */
export function getUser(): AuthUser | null {
  const raw = window.localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as AuthUser;
  } catch {
    return null;
  }
}

export function setUser(user: AuthUser | null): void {
  if (user) window.localStorage.setItem(USER_KEY, JSON.stringify(user));
  else window.localStorage.removeItem(USER_KEY);
}

export function disclaimerAccepted(): boolean {
  return !!getUser()?.disclaimer_accepted_at;
}

export function isAdmin(): boolean {
  return !!getUser()?.is_admin;
}

export function isAuthenticated(): boolean {
  return !!getToken();
}

/** Clear all auth state (logout). */
export function clearAuth(): void {
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(USER_KEY);
}

/* ---------- legacy API key (backwards compat) ---------- */
function getApiKey(): string {
  return window.localStorage.getItem(APIKEY_KEY) || "";
}

/** Read the legacy API key (used by the Settings page for remote deployments). */
export function getApiAuthKey(): string {
  return getApiKey();
}

export function setApiAuthKey(value: string): void {
  const trimmed = value.trim();
  if (trimmed) window.localStorage.setItem(APIKEY_KEY, trimmed);
  else window.localStorage.removeItem(APIKEY_KEY);
}

/** The active credential: JWT preferred, fall back to legacy API key. */
function activeCredential(): string {
  return getToken() || getApiKey();
}

/* ---------- request injection ---------- */
export function authHeaders(): Record<string, string> {
  const cred = activeCredential();
  return cred ? { Authorization: `Bearer ${cred}` } : {};
}

export function authQuerySuffix(): string {
  const cred = activeCredential();
  return cred ? `api_key=${encodeURIComponent(cred)}` : "";
}

export function withAuthQuery(url: string): string {
  const suffix = authQuerySuffix();
  if (!suffix) return url;
  return `${url}${url.includes("?") ? "&" : "?"}${suffix}`;
}
