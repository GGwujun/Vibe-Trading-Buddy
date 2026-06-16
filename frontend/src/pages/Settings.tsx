import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Bell, Database, KeyRound, Loader2, RotateCcw, Save, Send, Server, ShieldCheck, SlidersHorizontal, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import { api, isAuthRequiredError, type DataSourceSettings, type DataHealthReport, type SourceHealth, type LLMProviderOption, type LLMSettings, type NotifyConfig, type PlatformConfig } from "@/lib/api";
import { getApiAuthKey, isAdmin, setApiAuthKey } from "@/lib/apiAuth";
import { cn } from "@/lib/utils";

interface LLMFormState {
  provider: string;
  model_name: string;
  base_url: string;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
  reasoning_effort: string;
}

const fieldClass =
  "w-full rounded-md border bg-background px-3 py-2 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/20 disabled:cursor-not-allowed disabled:opacity-60";
const labelClass = "text-sm font-medium";
const hintClass = "text-xs text-muted-foreground";

function toForm(settings: LLMSettings): LLMFormState {
  return {
    provider: settings.provider,
    model_name: settings.model_name,
    base_url: settings.base_url,
    temperature: settings.temperature,
    timeout_seconds: settings.timeout_seconds,
    max_retries: settings.max_retries,
    reasoning_effort: settings.reasoning_effort || "",
  };
}

function unknownError(error: unknown): string {
  return error instanceof Error ? error.message : "未知错误";
}

export function Settings() {
  const [settings, setSettings] = useState<LLMSettings | null>(null);
  const [dataSettings, setDataSettings] = useState<DataSourceSettings | null>(null);
  const [form, setForm] = useState<LLMFormState | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [localApiKey, setLocalApiKeyState] = useState(() => getApiAuthKey());
  const [clearApiKey, setClearApiKey] = useState(false);
  const [tushareToken, setTushareToken] = useState("");
  const [clearTushareToken, setClearTushareToken] = useState(false);
  const [tpdogToken, setTpdogToken] = useState("");
  const [clearTpdogToken, setClearTpdogToken] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dataSaving, setDataSaving] = useState(false);
  const [settingsLoadError, setSettingsLoadError] = useState<string | null>(null);
  const [tab, setTab] = useState<"system" | "notify">(isAdmin() ? "system" : "notify");

  useEffect(() => {
    let alive = true;
    Promise.all([api.getLLMSettings(), api.getDataSourceSettings()])
      .then(([llmData, dataSourceData]) => {
        if (!alive) return;
        setSettings(llmData);
        setForm(toForm(llmData));
        setDataSettings(dataSourceData);
        setSettingsLoadError(null);
      })
      .catch((error) => {
        const message = unknownError(error);
        setSettingsLoadError(message);
        if (isAuthRequiredError(error)) {
          toast.error(message);
        } else {
          toast.error(`加载模型配置失败：${message}`);
          toast.error(`加载数据源配置失败：${message}`);
        }
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => { alive = false; };
  }, []);

  const providers = settings?.providers ?? [];
  const selectedProvider = useMemo<LLMProviderOption | undefined>(
    () => providers.find((provider) => provider.name === form?.provider),
    [form?.provider, providers],
  );

  const applyProviderDefaults = (provider = selectedProvider) => {
    if (!provider || !form) return;
    setForm({
      ...form,
      model_name: provider.default_model,
      base_url: provider.default_base_url,
    });
  };

  const onProviderChange = (name: string) => {
    const provider = providers.find((item) => item.name === name);
    if (!provider || !form) return;
    setForm({
      ...form,
      provider: provider.name,
      model_name: provider.default_model,
      base_url: provider.default_base_url,
    });
    setApiKey("");
    setClearApiKey(false);
  };

  const submitLocalApiKey = (event: FormEvent) => {
    event.preventDefault();
    setApiAuthKey(localApiKey);
    toast.success("本地 API 密钥已保存");
    window.location.reload();
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!form) return;
    setSaving(true);
    try {
      const updated = await api.updateLLMSettings({
        ...form,
        api_key: apiKey.trim() || undefined,
        clear_api_key: clearApiKey,
      });
      setSettings(updated);
      setForm(toForm(updated));
      setApiKey("");
      setClearApiKey(false);
      toast.success("模型配置已保存");
    } catch (error) {
      toast.error(`保存模型配置失败：${unknownError(error)}`);
    } finally {
      setSaving(false);
    }
  };

  const submitDataSources = async (event: FormEvent) => {
    event.preventDefault();
    setDataSaving(true);
    try {
      const updated = await api.updateDataSourceSettings({
        tushare_token: tushareToken.trim() || undefined,
        clear_tushare_token: clearTushareToken,
        tpdog_token: tpdogToken.trim() || undefined,
        clear_tpdog_token: clearTpdogToken,
      });
      setDataSettings(updated);
      setTushareToken("");
      setClearTushareToken(false);
      setTpdogToken("");
      setClearTpdogToken(false);
      toast.success("数据源配置已保存");
    } catch (error) {
      toast.error(`保存数据源配置失败：${unknownError(error)}`);
    } finally {
      setDataSaving(false);
    }
  };

  const localApiAccessSection = (
    <Section
      icon={ShieldCheck}
      title="本地 API 访问"
      desc="远程或私有 Web UI 部署需要在浏览器中保存服务端 API 密钥；本机 localhost 使用通常可以留空。"
    >
      <form onSubmit={submitLocalApiKey} className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]">
        <label className="grid gap-2">
          <span className={labelClass}>服务端 API 密钥</span>
          <input
            type="password"
            value={localApiKey}
            onChange={(event) => setLocalApiKeyState(event.target.value)}
            className={fieldClass}
            placeholder="仅存储于当前浏览器，留空即清除"
            autoComplete="current-password"
          />
          <span className={hintClass}>这个密钥只保存在浏览器本地，不会写入项目配置文件。</span>
        </label>
        <button
          type="submit"
          className="inline-flex items-center justify-center gap-2 self-end rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90"
        >
          <Save className="h-4 w-4" />
          保存本地密钥
        </button>
      </form>
    </Section>
  );

  if (loading || !form || !settings || !dataSettings) {
    return (
      <div className="mx-auto max-w-6xl space-y-6 p-6">
        <PageHeader />
        {localApiAccessSection}
        <div className="flex min-h-32 items-center justify-center rounded-md border bg-card p-5 text-sm text-muted-foreground">
          {settingsLoadError ? (
            <div className="text-center">
              <div className="font-medium text-foreground">设置暂不可用</div>
              <div className="mt-1">{settingsLoadError}</div>
            </div>
          ) : (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              正在加载设置…
            </>
          )}
        </div>
      </div>
    );
  }

  const keyStatus = settings.api_key_configured
    ? "已配置"
    : settings.api_key_required
      ? "留空表示保留当前密钥"
      : selectedProvider?.auth_type === "oauth" && selectedProvider.login_command
        ? `该供应商使用 OAuth，请运行：${selectedProvider.login_command}`
        : "该供应商不需要 API 密钥";
  const apiKeyDisabled = !selectedProvider?.api_key_required || clearApiKey;
  const tushareStatus = dataSettings.tushare_token_configured
    ? "已配置"
    : "留空表示保留当前 Token";
  const tpdogStatus = dataSettings.tpdog_token_configured
    ? "已配置"
    : "留空表示保留当前 Token";

  return (
    <div className="mx-auto max-w-6xl space-y-6 p-6">
      <PageHeader />

      {/* Tab switch — 系统配置 Tab 仅管理员可见 */}
      <div className="flex gap-1 border-b">
        {([
          { id: "system" as const, label: "系统配置", adminOnly: true },
          { id: "notify" as const, label: "通知配置", adminOnly: false },
        ]).filter(t => !t.adminOnly || isAdmin()).map(t => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={cn(
              "px-4 py-2 text-sm font-medium border-b-2 transition-colors",
              tab === t.id
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "notify" || !isAdmin() ? (
        <NotifyTab />
      ) : (
        <>
      {localApiAccessSection}

      <form onSubmit={submit} className="grid gap-6 lg:grid-cols-[minmax(0,1.35fr)_minmax(320px,0.8fr)]">
        <Section
          icon={Server}
          title="模型连接"
          desc="选择智能体使用的模型供应商、模型 ID、服务地址和鉴权方式。"
        >
          <div className="grid gap-4">
            <label className="grid gap-2">
              <span className={labelClass}>供应商</span>
              <select
                value={form.provider}
                onChange={(event) => onProviderChange(event.target.value)}
                className={fieldClass}
              >
                {providers.map((provider) => (
                  <option key={provider.name} value={provider.name}>{provider.label}</option>
                ))}
              </select>
              <span className={hintClass}>切换供应商会自动填入推荐模型和默认服务地址。</span>
            </label>

            <label className="grid gap-2">
              <span className={labelClass}>模型 ID</span>
              <div className="flex gap-2">
                <input
                  value={form.model_name}
                  onChange={(event) => setForm({ ...form, model_name: event.target.value })}
                  className={fieldClass}
                  required
                />
                <button
                  type="button"
                  onClick={() => applyProviderDefaults()}
                  className="inline-flex shrink-0 items-center gap-2 rounded-md border px-3 py-2 text-sm text-muted-foreground transition hover:bg-muted hover:text-foreground"
                  title="使用供应商默认值"
                >
                  <RotateCcw className="h-4 w-4" />
                  <span className="hidden sm:inline">恢复默认</span>
                </button>
              </div>
              <span className={hintClass}>填写供应商要求的准确模型标识，例如具体版本号或部署名。</span>
            </label>

            <label className="grid gap-2">
              <span className={labelClass}>服务地址</span>
              <input
                value={form.base_url}
                onChange={(event) => setForm({ ...form, base_url: event.target.value })}
                className={fieldClass}
                placeholder={selectedProvider?.default_base_url}
                disabled={selectedProvider?.auth_type === "oauth"}
              />
              <span className={hintClass}>OAuth 供应商通常由登录流程决定服务地址。</span>
            </label>

            <label className="grid gap-2">
              <span className={labelClass}>
                {selectedProvider?.auth_type === "oauth" ? "OAuth 登录" : "API 密钥"}
              </span>
              <div className="relative">
                <KeyRound className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                <input
                  type="password"
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  className={`${fieldClass} pl-9`}
                  placeholder={keyStatus}
                  autoComplete="current-password"
                  disabled={apiKeyDisabled}
                />
              </div>
              <div className="flex items-center justify-between gap-3">
                <span className={hintClass}>{keyStatus}</span>
                {selectedProvider?.api_key_required ? (
                  <label className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                    <input
                      type="checkbox"
                      checked={clearApiKey}
                      onChange={(event) => {
                        setClearApiKey(event.target.checked);
                        if (event.target.checked) setApiKey("");
                      }}
                      className="h-3.5 w-3.5 accent-primary"
                    />
                    清除已保存密钥
                  </label>
                ) : null}
              </div>
            </label>
          </div>
        </Section>

        <Section
          icon={SlidersHorizontal}
          title="生成参数"
          desc="控制智能体调用模型时的温度、超时、重试和推理强度。"
        >
          <div className="grid gap-4">
            <label className="grid gap-2">
              <span className={labelClass}>温度</span>
              <input
                type="number"
                min={0}
                max={2}
                step={0.1}
                value={form.temperature}
                onChange={(event) => setForm({ ...form, temperature: Number(event.target.value) })}
                className={fieldClass}
              />
              <span className={hintClass}>数值越高，回答越发散；投研和回测通常建议保持较低。</span>
            </label>

            <label className="grid gap-2">
              <span className={labelClass}>超时时间（秒）</span>
              <input
                type="number"
                min={1}
                max={3600}
                step={1}
                value={form.timeout_seconds}
                onChange={(event) => setForm({ ...form, timeout_seconds: Number(event.target.value) })}
                className={fieldClass}
              />
            </label>

            <label className="grid gap-2">
              <span className={labelClass}>最大重试次数</span>
              <input
                type="number"
                min={0}
                max={20}
                step={1}
                value={form.max_retries}
                onChange={(event) => setForm({ ...form, max_retries: Number(event.target.value) })}
                className={fieldClass}
              />
            </label>

            <label className="grid gap-2">
              <span className={labelClass}>推理强度</span>
              <select
                value={form.reasoning_effort}
                onChange={(event) => setForm({ ...form, reasoning_effort: event.target.value })}
                className={fieldClass}
              >
                <option value="">关闭</option>
                <option value="low">低</option>
                <option value="medium">中</option>
                <option value="high">高</option>
                <option value="max">最高</option>
              </select>
              <span className={hintClass}>推理越强通常越慢，但复杂研究任务会更稳。</span>
            </label>

            <ConfigPath label="模型配置写入" path={settings.env_path} />

            <button
              type="submit"
              disabled={saving}
              className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-70"
            >
              {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
              {saving ? "正在保存…" : "保存模型配置"}
            </button>
          </div>
        </Section>
      </form>

      <form onSubmit={submitDataSources}>
        <Section
          icon={Database}
          title="数据源配置"
          desc="配置市场数据凭据。它会影响回测、机会扫描、投研报告和持仓分析。"
        >
          <div className="grid gap-5 lg:grid-cols-[minmax(0,1.1fr)_minmax(280px,0.9fr)]">
            <div className="grid gap-4">
              <label className="grid gap-2">
                <span className={labelClass}>Tushare Token</span>
                <div className="relative">
                  <KeyRound className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                  <input
                    type="password"
                    value={tushareToken}
                    onChange={(event) => setTushareToken(event.target.value)}
                    className={`${fieldClass} pl-9`}
                    placeholder={tushareStatus}
                    autoComplete="current-password"
                    disabled={clearTushareToken}
                  />
                </div>
                <div className="flex items-center justify-between gap-3">
                  <span className={hintClass}>用于 A 股、期货、基金和宏观数据；未配置时会尽量回退到 AKShare 等可用数据源。</span>
                  <label className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                    <input
                      type="checkbox"
                      checked={clearTushareToken}
                      onChange={(event) => {
                        setClearTushareToken(event.target.checked);
                        if (event.target.checked) setTushareToken("");
                      }}
                      className="h-3.5 w-3.5 accent-primary"
                    />
                    清除已保存 Token
                  </label>
                </div>
              </label>

              <label className="grid gap-2">
                <span className={labelClass}>TPDog Token（托普量化）</span>
                <div className="relative">
                  <KeyRound className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                  <input
                    type="password"
                    value={tpdogToken}
                    onChange={(event) => setTpdogToken(event.target.value)}
                    className={`${fieldClass} pl-9`}
                    placeholder={tpdogStatus}
                    autoComplete="current-password"
                    disabled={clearTpdogToken}
                  />
                </div>
                <div className="flex items-center justify-between gap-3">
                  <span className={hintClass}>HTTPS 行情/资金流/龙虎榜/股池接口，作 mootdx 的稳定备用源（云主机不依赖 TDX 服务器）。</span>
                  <label className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                    <input
                      type="checkbox"
                      checked={clearTpdogToken}
                      onChange={(event) => {
                        setClearTpdogToken(event.target.checked);
                        if (event.target.checked) setTpdogToken("");
                      }}
                      className="h-3.5 w-3.5 accent-primary"
                    />
                    清除已保存 Token
                  </label>
                </div>
              </label>

              <ConfigPath label="数据源配置写入" path={dataSettings.env_path} />

              <button
                type="submit"
                disabled={dataSaving}
                className="inline-flex items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-70"
              >
                {dataSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                {dataSaving ? "正在保存…" : "保存数据源配置"}
              </button>
            </div>

            <div className="rounded-md border bg-muted/20 p-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <span className="text-sm font-medium">BaoStock</span>
                <span className={cn(
                  "rounded-full px-2 py-0.5 text-xs",
                  dataSettings.baostock_supported ? "bg-success/10 text-success" : "bg-warning/10 text-warning",
                )}>
                  {dataSettings.baostock_supported ? "已可用" : "未安装"}
                </span>
              </div>
              <div className="space-y-2 text-sm text-muted-foreground">
                <p>{dataSettings.baostock_message}</p>
                <p>{dataSettings.baostock_installed ? "Python 包已安装" : "Python 包未安装"}</p>
              </div>
            </div>
          </div>
        </Section>
      </form>

      <DataHealthSection />
        </>
      )}
    </div>
  );
}

/* ─── Data-source health (admin) ─── */

function DataHealthSection() {
  const [report, setReport] = useState<DataHealthReport | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = () => {
    setLoading(true);
    api.getDataHealth()
      .then(setReport)
      .catch((e) => toast.error(`数据源健康检查失败：${unknownError(e)}`))
      .finally(() => setLoading(false));
  };

  useEffect(() => { refresh(); }, []);

  return (
    <Section
      icon={Server}
      title="数据源健康检查"
      desc="实时探测各数据源在本服务器的连通性。部署到阿里云等云主机时，从这里定位哪个数据源被限速 / 封禁 / 超时。"
    >
      <div className="mb-4 flex items-center justify-between gap-3">
        <span className="text-sm text-muted-foreground">
          {report ? (
            <>正常 <span className="font-medium text-success">{report.summary_ok}</span> / {report.summary_total}</>
          ) : loading ? (
            "正在探测…"
          ) : (
            "尚未检测"
          )}
        </span>
        <button
          type="button"
          onClick={refresh}
          disabled={loading}
          className="inline-flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm font-medium transition hover:bg-muted disabled:opacity-60"
        >
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          重新检测
        </button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        {(report?.sources ?? []).map((s) => (
          <HealthCard key={s.name} source={s} />
        ))}
        {report && report.sources.length === 0 && (
          <div className="col-span-full rounded-md border bg-muted/20 p-4 text-sm text-muted-foreground">
            没有可探测的数据源。
          </div>
        )}
      </div>
    </Section>
  );
}

function HealthCard({ source }: { source: SourceHealth }) {
  return (
    <div className="rounded-md border bg-muted/20 p-3">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="text-sm font-medium text-foreground">{source.name}</span>
        <span className={cn(
          "rounded-full px-2 py-0.5 text-xs",
          source.ok ? "bg-success/10 text-success" : "bg-destructive/10 text-destructive",
        )}>
          {source.ok ? "正常" : "异常"}
        </span>
      </div>
      <div className="space-y-1 text-xs text-muted-foreground">
        <div>延迟 {source.latency_ms} ms</div>
        <div className={cn("break-words", !source.ok && "text-destructive/80")}>{source.detail || "—"}</div>
      </div>
    </div>
  );
}

/* ─── Notify tab ─── */

const PLATFORMS: { key: keyof NotifyConfig; label: string; hasSecret: boolean }[] = [
  { key: "feishu", label: "飞书", hasSecret: true },
  { key: "dingtalk", label: "钉钉", hasSecret: true },
  { key: "wechat", label: "企业微信", hasSecret: false },
];

function NotifyTab() {
  const [cfg, setCfg] = useState<NotifyConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState<string | null>(null); // platform key being tested
  const [platform, setPlatform] = useState<keyof NotifyConfig>("feishu");

  useEffect(() => {
    api.getNotifyConfig().then(c => setCfg(c)).catch(e => toast.error(unknownError(e)));
  }, []);

  const patch = (key: keyof NotifyConfig, change: Partial<PlatformConfig>) => {
    setCfg(prev => prev ? { ...prev, [key]: { ...prev[key], ...change } } : prev);
  };

  const save = async () => {
    if (!cfg) return;
    setSaving(true);
    try {
      await api.saveNotifyConfig(cfg);
      toast.success("通知配置已保存");
    } catch (e) {
      toast.error(unknownError(e));
    } finally {
      setSaving(false);
    }
  };

  const test = async (key: keyof NotifyConfig) => {
    if (!cfg) return;
    // Save first so the test uses the latest config.
    setSaving(true);
    try { await api.saveNotifyConfig(cfg); } catch (e) { toast.error(unknownError(e)); setSaving(false); return; }
    setSaving(false);
    setTesting(key);
    try {
      const res = await api.testNotify(key);
      if (res.ok) toast.success(`${key} 测试发送成功：${res.message}`);
      else toast.error(`${key} 测试失败：${res.message}`);
    } catch (e) {
      toast.error(unknownError(e));
    } finally {
      setTesting(null);
    }
  };

  if (!cfg) {
    return <div className="flex min-h-32 items-center justify-center text-sm text-muted-foreground"><Loader2 className="mr-2 h-4 w-4 animate-spin" />加载通知配置…</div>;
  }

  return (
    <div className="space-y-6">
      <p className="text-xs text-muted-foreground">
        配置飞书 / 钉钉 / 企业微信群机器人，用于推送行情摘要。Webhook 在各平台「群机器人」设置中获取。
      </p>

      {/* Platform sub-tabs */}
      <div className="flex gap-1 border-b">
        {PLATFORMS.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => setPlatform(key)}
            className={cn(
              "px-4 py-2 text-sm font-medium border-b-2 transition-colors",
              platform === key ? "border-primary text-primary" : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {(() => {
        const { key, label, hasSecret } = PLATFORMS.find(p => p.key === platform)!;
        const p = cfg[key];
        return (
          <Section key={key} icon={Bell} title={`${label}通知`} desc={`${label}群机器人推送配置`}>
            <div className="space-y-3">
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={p.enabled} onChange={e => patch(key, { enabled: e.target.checked })} className="h-4 w-4" />
                启用{label}通知
              </label>

              <div className="space-y-1">
                <div className={labelClass}>{label}群机器人 Webhook</div>
                <input
                  value={p.webhook_url}
                  onChange={e => patch(key, { webhook_url: e.target.value })}
                  placeholder={key === "feishu" ? "https://open.feishu.cn/open-apis/bot/v2/hook/..." : key === "dingtalk" ? "https://oapi.dingtalk.com/robot/send?access_token=..." : "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."}
                  className={fieldClass}
                />
              </div>

              {hasSecret && (
                <div className="space-y-1">
                  <div className={labelClass}>签名密钥</div>
                  <input
                    type="password"
                    value={p.secret}
                    onChange={e => patch(key, { secret: e.target.value })}
                    placeholder={`${label}机器人安全设置中的签名密钥，可选`}
                    className={fieldClass}
                  />
                </div>
              )}

              {/* Push schedule (stored, but scheduled push is a future feature) */}
              <div className="grid gap-3 sm:grid-cols-3 pt-1">
                {([
                  { en: "pre_market_enabled", t: "pre_market_time", label: "盘前推送" },
                  { en: "after_close_enabled", t: "after_close_time", label: "盘后推送" },
                  { en: "custom_enabled", t: "custom_time", label: "自定义推送" },
                ] as const).map(s => (
                  <div key={s.en} className="space-y-1">
                    <label className="flex items-center gap-2 text-sm">
                      <input type="checkbox" checked={p[s.en]} onChange={e => patch(key, { [s.en]: e.target.checked } as Partial<PlatformConfig>)} className="h-4 w-4" />
                      {s.label}
                    </label>
                    <input type="time" value={p[s.t]} onChange={e => patch(key, { [s.t]: e.target.value } as Partial<PlatformConfig>)} className={fieldClass} />
                  </div>
                ))}
              </div>
              <p className={hintClass}>注：定时自动推送为后续功能，当前仅支持手动测试发送。</p>

              <div className="flex gap-2 pt-1">
                <button
                  onClick={() => test(key)}
                  disabled={testing === key || !p.webhook_url}
                  className="inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm hover:bg-muted disabled:opacity-40"
                >
                  {testing === key ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
                  测试发送
                </button>
              </div>
            </div>
          </Section>
        );
      })()}

      <div className="flex justify-end">
        <button onClick={save} disabled={saving} className="inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-40">
          {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          保存通知配置
        </button>
      </div>
    </div>
  );
}

function PageHeader() {
  return (
    <div className="space-y-2">
      <h1 className="text-2xl font-semibold tracking-tight">设置</h1>
      <p className="max-w-3xl text-sm text-muted-foreground">
        管理模型供应商、生成参数、API 密钥、市场数据源，以及飞书 / 钉钉 / 企业微信通知推送。
      </p>
    </div>
  );
}

function Section({
  icon: Icon,
  title,
  desc,
  children,
}: {
  icon: typeof Server;
  title: string;
  desc?: string;
  children: ReactNode;
}) {
  return (
    <section className="rounded-md border bg-card">
      <div className="border-b px-5 py-4">
        <div className="flex items-center gap-2">
          <Icon className="h-4 w-4 text-primary" />
          <h2 className="text-base font-semibold">{title}</h2>
        </div>
        {desc && <p className="mt-1 text-sm text-muted-foreground">{desc}</p>}
      </div>
      <div className="p-5">{children}</div>
    </section>
  );
}

function ConfigPath({ label, path }: { label: string; path: string }) {
  return (
    <div className="rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
      <span className="font-medium text-foreground">{label}：</span>
      <span className="break-all font-mono">{path}</span>
    </div>
  );
}
