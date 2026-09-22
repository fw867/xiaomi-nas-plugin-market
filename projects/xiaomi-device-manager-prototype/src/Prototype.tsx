import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowLeftIcon, ChevronDownIcon, ChevronRightIcon, ReloadIcon } from "@radix-ui/react-icons";
import "./prototype.css";

type WebViewHost = {
  callHandler?: (name: string, method: string, data: unknown, requestId: string) => unknown;
  _callHandler?: (name: string, requestId: number, argsJson: string) => unknown;
};

let clientCallSeq = 0;

/* 小米客户端在 WebView 里注入 flutter_inappwebview / android_webview，官方插件靠自带的
   js-bridge.js 调宿主方法。这里只实现我们用到的那一小部分：调宿主方法并忽略返回结果。 */
function callClient(method: string, params: Record<string, unknown> = {}): boolean {
  const scope = window as unknown as {
    flutter_inappwebview?: WebViewHost;
    android_webview?: WebViewHost;
  };
  const host = scope.flutter_inappwebview ?? scope.android_webview;
  if (!host) return false;
  if (!host.callHandler) {
    // 部分客户端版本只暴露 _callHandler，照 js-bridge.js 的方式补一层
    host.callHandler = function (name, method, data, requestId) {
      const id = window.setTimeout(() => {});
      host._callHandler?.(name, id, JSON.stringify([method, data, requestId]));
      return new Promise((resolve) => {
        (host as unknown as Record<number, unknown>)[id] = resolve;
      });
    };
  }
  const payload = host === scope.android_webview ? JSON.stringify(params) : params;
  host.callHandler?.("hs_webCallAppHandler", method, payload, String(++clientCallSeq));
  return true;
}

// 客户端里没有页面历史，window.close() 也会被 WebView 忽略，只能请宿主关掉当前页面
function leavePlugin() {
  if (callClient("normal_goback")) return;
  if (window.history.length > 1) {
    window.history.back();
    return;
  }
  window.close();
}

(window as unknown as Record<string, unknown>).hs_appCallBackToWeb ??= () => {};

type MetricKey = "cpu" | "memory" | "storage" | "temperature" | "network" | "diskIo";

type Metric = {
  key: MetricKey;
  label: string;
  hint: string;
  value: string;
  badge: string;
  tone: string;
  current: number;
  chartMinSpread: number;
  formatValue: (value: number) => string;
};

type ContainerInfo = {
  name: string;
  image: string;
  status: string;
  id?: string;
  state?: string;
  createdAt?: string;
  runningFor?: string;
  ports?: string;
  networks?: string;
  mounts?: string;
  cpuPercent?: string;
  memoryPercent?: string;
  memoryUsage?: string;
  networkIo?: string;
  blockIo?: string;
  pids?: string;
};

type DriveInfo = {
  device: string;
  model: string;
  serial: string;
  capacityBytes: number;
  temperature: number | null;
  smartPassed: boolean | null;
  powerOnHours: number | null;
  reallocatedSectors: number;
  pendingSectors: number;
  offlineUncorrectable: number;
  crcErrors: number;
  rotationRate: number | null;
};

type HistoryPoint = {
  at: number;
  cpu: number;
  memory: number;
  storage: number;
  temperature: number | null;
  network?: number;
  diskIo?: number;
};

type ServicesData = {
  docker: { active: boolean; version: string; running: number };
  containers: ContainerInfo[];
};

type DeviceInfo = {
  hostname: string;
  cpuModel: string;
  cores: number;
  uptimeSeconds: number;
  uptimeLabel?: string;
  kernel?: string;
  ips: { iface: string; addr: string }[];
};

type NasStatus = {
  ok: boolean;
  source: string;
  probeMode?: "local";
  hostname?: string;
  updatedAt: string;
  live: boolean;
  history: HistoryPoint[];
  device?: DeviceInfo;
  metrics: {
    cpu: { percent: number; source?: string; load: string; cores: number };
    memory: { percent: number; usedMb: number; totalMb: number };
    storage: { percent: number; used: string; total: string; mount?: string };
    temperature: { celsius: number | null };
    network: { interface: string; rxBps: number; txBps: number };
    diskIo: { devices: string[]; readBps: number; writeBps: number };
  };
};

type MainTab = "overview" | "services" | "drives" | "check";

type LiveMetrics = Pick<NasStatus, "ok" | "updatedAt" | "metrics"> & { sampledAt: number };

type SectionKey = "services" | "drives";

function resolveNasApi(path: string) {
  const [name, query] = path.split("?");
  const suffix = query ? `?${query}` : "";
  const configured = import.meta.env.VITE_NAS_STATUS_API as string | undefined;
  if (configured) {
    const base = name === "nas-status" ? configured : configured.replace(/nas-status$/, name);
    return `${base}${suffix}`;
  }

  const host = window.location.hostname;
  const isLocalPreview = host === "127.0.0.1" || host === "localhost";
  if (isLocalPreview && window.location.port === "5177") {
    return `http://127.0.0.1:5188/api/${name}${suffix}`;
  }
  // Windows 客户端 location 可能带盘符（/D:/plugin/...），不能用相对 api/。
  // 产物在 assets/*.js 下，import.meta.url 上一级即插件根目录。
  const pluginRoot = new URL("../", import.meta.url).href;
  return new URL(`api/${name}${suffix}`, pluginRoot).href;
}

function sourceLabel(status: NasStatus) {
  return status.hostname || "NAS 本机";
}

function formatTime(timestamp: number) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(timestamp));
}

// 数值和单位之间用不换行空格：窄屏折行时不会把「MB」「B/s」单独甩到下一行
const NBSP = "\u00A0";

function formatRate(bytesPerSecond: number) {
  if (!Number.isFinite(bytesPerSecond) || bytesPerSecond <= 0) return `0${NBSP}B/s`;
  if (bytesPerSecond >= 1024 ** 3) return `${(bytesPerSecond / 1024 ** 3).toFixed(1)}${NBSP}GB/s`;
  if (bytesPerSecond >= 1024 ** 2) return `${(bytesPerSecond / 1024 ** 2).toFixed(1)}${NBSP}MB/s`;
  if (bytesPerSecond >= 1024) return `${Math.round(bytesPerSecond / 1024)}${NBSP}KB/s`;
  return `${Math.round(bytesPerSecond)}${NBSP}B/s`;
}

function formatCapacity(bytes: number) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "容量未知";
  return bytes >= 10 ** 12 ? `${(bytes / 10 ** 12).toFixed(1)}${NBSP}TB` : `${Math.round(bytes / 10 ** 9)}${NBSP}GB`;
}

const fallbackStatus: NasStatus = {
  ok: true,
  source: "127.0.0.1",
  probeMode: "local",
  hostname: "NAS 本机",
  updatedAt: "14:42",
  live: false,
  history: [],
  device: {
    hostname: "NAS 本机",
    cpuModel: "4 核处理器",
    cores: 4,
    uptimeSeconds: 284400,
    uptimeLabel: "3 天 7 小时",
    kernel: "",
    ips: [{ iface: "eth0", addr: "192.168.1.8" }],
  },
  metrics: {
    cpu: { percent: 20, load: "2.55 / 2.73 / 2.57", cores: 4 },
    memory: { percent: 46, usedMb: 1792, totalMb: 3814 },
    storage: { percent: 8, used: "569G", total: "7.3T" },
    temperature: { celsius: 58.2 },
    network: { interface: "enu1u3", rxBps: 0, txBps: 0 },
    diskIo: { devices: ["sda", "sdb"], readBps: 0, writeBps: 0 },
  },
};

function MetricCard({
  metric,
  selected,
  onSelect,
}: {
  metric: Metric;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      className={`metric-card ${selected ? "is-selected" : ""}`}
      onClick={onSelect}
      aria-expanded={selected}
      aria-controls="metric-trend"
    >
      <span className={`metric-badge metric-${metric.tone}`}>{metric.badge}</span>
      <span className="metric-copy">
        <span>{metric.label}</span>
        <strong>{metric.value}</strong>
        <small>{metric.hint}</small>
      </span>
      <ChevronRightIcon className="metric-chevron" width={17} height={17} aria-hidden="true" />
    </button>
  );
}

function ServiceRow({
  badge,
  tone,
  title,
  subtitle,
  value,
  onClick,
  expanded,
}: {
  badge: string;
  tone: string;
  title: string;
  subtitle: string;
  value: string;
  onClick?: () => void;
  expanded?: boolean;
}) {
  const content = (
    <>
      <span className={`metric-badge metric-${tone}`}>{badge}</span>
      <span className="service-copy">
        <strong>{title}</strong>
        <small>{subtitle}</small>
      </span>
      <span className="service-value">{value}</span>
      {onClick ? <ChevronRightIcon className={`service-chevron ${expanded ? "is-open" : ""}`} width={17} height={17} /> : null}
    </>
  );

  return onClick ? (
    <button type="button" className="service-row service-row-button" onClick={onClick} aria-expanded={expanded}>
      {content}
    </button>
  ) : (
    <div className="service-row">{content}</div>
  );
}

function TrendChart({ metric, points }: { metric: Metric; points: HistoryPoint[] }) {
  const values = points
    .map((point) => point[metric.key])
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value))
    .slice(-240);
  const graphValues = values.length > 1 ? values : [Number.isFinite(metric.current) ? metric.current : 0];
  const width = 620;
  const height = 190;
  const padding = { top: 18, right: 12, bottom: 28, left: metric.key === "network" || metric.key === "diskIo" ? 68 : 38 };
  const rawMin = Math.min(...graphValues);
  const rawMax = Math.max(...graphValues);
  const spread = Math.max(rawMax - rawMin, metric.chartMinSpread);
  const min = Math.max(0, rawMin - spread * 0.2);
  const max = rawMax + spread * 0.2;
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const x = (index: number) =>
    graphValues.length === 1
      ? padding.left + chartWidth / 2
      : padding.left + (index / (graphValues.length - 1)) * chartWidth;
  const y = (value: number) => padding.top + ((max - value) / (max - min || 1)) * chartHeight;
  const path = graphValues.map((value, index) => `${index === 0 ? "M" : "L"}${x(index)} ${y(value)}`).join(" ");
  const latest = graphValues[graphValues.length - 1];
  const firstTime = points.length > 1 ? formatTime(points[Math.max(0, points.length - graphValues.length)].at) : "开始";
  const lastTime = points.length ? formatTime(points[points.length - 1].at) : "刚刚";
  const gridValues = [0, 0.5, 1].map((ratio) => max - (max - min) * ratio);

  return (
    <section className="trend-panel" id="metric-trend" aria-live="polite">
      <div className="detail-heading">
        <div>
          <p>趋势图</p>
          <h3>{metric.label} · 最近 24 小时</h3>
        </div>
        <strong>{metric.formatValue(latest)}</strong>
      </div>
      <div className="trend-chart-wrap">
        <svg className="trend-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${metric.label}趋势图`}>
          {gridValues.map((gridValue) => (
            <g key={gridValue}>
              <line
                x1={padding.left}
                x2={width - padding.right}
                y1={y(gridValue)}
                y2={y(gridValue)}
                className="trend-grid-line"
              />
              <text x={padding.left - 9} y={y(gridValue) + 4} className="trend-axis-label" textAnchor="end">
                {metric.formatValue(gridValue)}
              </text>
            </g>
          ))}
          <path d={path} className={`trend-line trend-${metric.tone}`} />
          <circle cx={x(graphValues.length - 1)} cy={y(latest)} r="4" className={`trend-dot trend-${metric.tone}`} />
          <text x={padding.left} y={height - 7} className="trend-axis-label">{firstTime}</text>
          <text x={width - padding.right} y={height - 7} className="trend-axis-label" textAnchor="end">{lastTime}</text>
        </svg>
      </div>
      <p className="trend-note">
        {points.length < 2 ? "已开始在 NAS 本机采样，趋势数据会随运行时间逐渐完整。" : `已记录 ${points.length} 个本机采样点。`}
      </p>
    </section>
  );
}

function DeviceHeroCard({ status }: { status: NasStatus }) {
  const device = status.device;
  const ips = device?.ips?.length ? device.ips : [{ iface: "lan", addr: "—" }];
  const ipText = ips.map((item) => `${item.iface} ${item.addr}`).join(" · ");

  return (
    <section className="hero-card" aria-label="设备信息">
      <div className="hero-top">
        <div className="hero-ident">
          <span className="hero-logo" aria-hidden="true">
            NAS
          </span>
          <div>
            <h1>{device?.hostname || status.hostname || "小米智能存储"}</h1>
            <p>
              <span className={`live-dot ${status.live ? "is-live" : ""}`} aria-hidden="true" />
              {status.live ? "在线 · 本机采集" : "演示数据"}
            </p>
          </div>
        </div>
        <div className="hero-uptime">
          <small>已运行</small>
          <strong>{device?.uptimeLabel || "—"}</strong>
        </div>
      </div>

      <dl className="hero-facts">
        <div>
          <dt>处理器</dt>
          <dd title={device?.cpuModel || ""}>
            {device?.cpuModel || "—"}
            {device?.cores ? ` · ${device.cores} 核` : ""}
          </dd>
        </div>
        <div>
          <dt>网络地址</dt>
          <dd title={ipText}>{ipText}</dd>
        </div>
        {device?.kernel ? (
          <div>
            <dt>内核</dt>
            <dd title={device.kernel}>{device.kernel}</dd>
          </div>
        ) : null}
      </dl>
    </section>
  );
}

function BottomTabs({
  active,
  onChange,
  servicesReady,
  drivesReady,
}: {
  active: MainTab;
  onChange: (tab: MainTab) => void;
  servicesReady: boolean;
  drivesReady: boolean;
}) {
  const tabs: { id: MainTab; label: string; badge?: string }[] = [
    { id: "overview", label: "概览" },
    { id: "services", label: "服务", badge: servicesReady ? "·" : undefined },
    { id: "drives", label: "硬盘", badge: drivesReady ? "·" : undefined },
    { id: "check", label: "检查" },
  ];
  return (
    <nav className="bottom-tabs" aria-label="数据视图">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          className={`bottom-tab ${active === tab.id ? "is-active" : ""}`}
          onClick={() => onChange(tab.id)}
          aria-current={active === tab.id ? "page" : undefined}
        >
          <span className="bottom-tab-label">{tab.label}</span>
          {tab.badge ? <span className="bottom-tab-dot" aria-hidden="true" /> : null}
        </button>
      ))}
    </nav>
  );
}

function LazyPanel({
  panelId,
  title,
  summary,
  open,
  loading,
  error,
  onToggle,
  onRetry,
  children,
}: {
  panelId: string;
  title: string;
  summary: string;
  open: boolean;
  loading: boolean;
  error: string | null;
  onToggle: () => void;
  onRetry: () => void;
  children: ReactNode;
}) {
  return (
    <section className={`lazy-panel ${open ? "is-open" : ""}`}>
      <button
        type="button"
        className="lazy-summary"
        onClick={onToggle}
        aria-expanded={open}
        aria-controls={panelId}
      >
        <span>
          <strong>{title}</strong>
          <small>{summary}</small>
        </span>
        <ChevronDownIcon className={`lazy-chevron ${open ? "is-open" : ""}`} width={20} height={20} />
      </button>
      {open ? (
        <div className="lazy-body" id={panelId}>
          {loading ? (
            <div className="lazy-loading" role="status" aria-live="polite">
              <span className="lazy-spinner" aria-hidden="true" />
              <p>正在读取…</p>
            </div>
          ) : error ? (
            <div className="lazy-error" role="alert">
              <p>{error}</p>
              <button type="button" onClick={onRetry}>
                重试
              </button>
            </div>
          ) : (
            children
          )}
        </div>
      ) : null}
    </section>
  );
}

function DriveHealth({ drives }: { drives: DriveInfo[] }) {
  if (!drives.length) {
    return <p className="empty-detail">未检测到物理硬盘</p>;
  }
  return (
    <div className="drive-grid">
      {drives.map((drive) => {
        const warningCount = drive.reallocatedSectors + drive.pendingSectors + drive.offlineUncorrectable;
        const healthy = drive.smartPassed !== false && warningCount === 0;
        return (
          <article className="drive-card" key={drive.device}>
            <div className="drive-title">
              <span className={`drive-status ${healthy ? "is-healthy" : "is-warning"}`}>HD</span>
              <div>
                <strong>{drive.device.replace("/dev/", "硬盘 ").toUpperCase()}</strong>
                <small>{drive.model}</small>
              </div>
              <em className={healthy ? "is-healthy" : "is-warning"}>{healthy ? "SMART 正常" : "需要检查"}</em>
            </div>
            <dl className="drive-facts">
              <div><dt>容量</dt><dd>{formatCapacity(drive.capacityBytes)}</dd></div>
              <div><dt>温度</dt><dd>{drive.temperature === null ? "未知" : `${drive.temperature}°C`}</dd></div>
              <div><dt>通电时间</dt><dd>{drive.powerOnHours === null ? "未知" : `${drive.powerOnHours} 小时`}</dd></div>
              <div><dt>转速</dt><dd>{drive.rotationRate ? `${drive.rotationRate} RPM` : "固态硬盘"}</dd></div>
              <div><dt>重映射扇区</dt><dd>{drive.reallocatedSectors}</dd></div>
              <div><dt>待处理扇区</dt><dd>{drive.pendingSectors}</dd></div>
              <div><dt>不可校正扇区</dt><dd>{drive.offlineUncorrectable}</dd></div>
              <div><dt>接口错误</dt><dd>{drive.crcErrors}</dd></div>
            </dl>
            {drive.serial ? <p className="drive-serial">序列号 {drive.serial}</p> : null}
          </article>
        );
      })}
    </div>
  );
}

function DockerDetails({ services }: { services: ServicesData }) {
  return (
    <section className="docker-details" aria-live="polite">
      <div className="detail-heading">
        <div>
          <p>服务详情</p>
          <h3>Docker 容器</h3>
        </div>
        <strong>{services.docker.running} 个运行中</strong>
      </div>
      <div className="docker-meta">
        <span>服务 {services.docker.active ? "active" : "inactive"}</span>
        <span>Docker {services.docker.version || "版本未知"}</span>
      </div>
      <div className="container-list">
        {services.containers.length ? (
          services.containers.map((container) => {
            const isCentral = container.name === "miot_central";
            return (
              <article className={`container-item ${isCentral ? "is-protected" : ""}`} key={container.id || container.name}>
                <div className="container-title">
                  <div>
                    <strong>{container.name}</strong>
                    <span>{container.status || container.state || "状态未知"}</span>
                  </div>
                  {isCentral ? <em>系统容器 · 只读</em> : null}
                </div>
                <dl className="container-facts">
                  <div><dt>镜像</dt><dd>{container.image || "未知"}</dd></div>
                  <div><dt>容器 ID</dt><dd>{container.id || "未知"}</dd></div>
                  <div><dt>运行时间</dt><dd>{container.runningFor || container.createdAt || "未知"}</dd></div>
                  <div><dt>网络</dt><dd>{container.networks || "默认"}</dd></div>
                  <div><dt>CPU</dt><dd>{container.cpuPercent || "未知"}</dd></div>
                  <div><dt>内存</dt><dd>{container.memoryUsage || "未知"} {container.memoryPercent || ""}</dd></div>
                  <div><dt>网络 I/O</dt><dd>{container.networkIo || "未知"}</dd></div>
                  <div><dt>磁盘 I/O</dt><dd>{container.blockIo || "未知"}</dd></div>
                  <div><dt>进程数</dt><dd>{container.pids || "未知"}</dd></div>
                  {container.ports ? <div><dt>端口</dt><dd>{container.ports}</dd></div> : null}
                </dl>
              </article>
            );
          })
        ) : (
          <p className="empty-detail">当前没有正在运行的 Docker 容器。</p>
        )}
      </div>
    </section>
  );
}

export default function Prototype() {
  const [refreshing, setRefreshing] = useState(false);
  const [activeMetric, setActiveMetric] = useState<MetricKey | null>(null);
  const [mainTab, setMainTab] = useState<MainTab>("overview");
  const [dockerOpen, setDockerOpen] = useState(false);
  const [status, setStatus] = useState<NasStatus>(fallbackStatus);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [services, setServices] = useState<ServicesData | null>(null);
  const [servicesLoading, setServicesLoading] = useState(false);
  const [servicesError, setServicesError] = useState<string | null>(null);
  const [drives, setDrives] = useState<DriveInfo[] | null>(null);
  const [drivesLoading, setDrivesLoading] = useState(false);
  const [drivesError, setDrivesError] = useState<string | null>(null);
  const liveRefreshInFlight = useRef(false);
  const statusSourceLabel = sourceLabel(status);

  const metrics = useMemo<Metric[]>(
    () => [
      {
        key: "cpu",
        label: "处理器",
        hint: `${status.metrics.cpu.cores} 核 · 系统负载\n${status.metrics.cpu.load.replace(/ /g, NBSP)}`,
        value: `${status.metrics.cpu.percent}%`,
        badge: "CPU",
        tone: "cpu",
        current: status.metrics.cpu.percent,
        chartMinSpread: 10,
        formatValue: (value) => `${Math.round(value)}%`,
      },
      {
        key: "memory",
        label: "内存",
        hint: `已用 ${status.metrics.memory.usedMb}${NBSP}MB ·\n共 ${status.metrics.memory.totalMb}${NBSP}MB`,
        value: `${status.metrics.memory.percent}%`,
        badge: "RAM",
        tone: "ram",
        current: status.metrics.memory.percent,
        chartMinSpread: 10,
        formatValue: (value) => `${Math.round(value)}%`,
      },
      {
        key: "storage",
        label: "存储",
        hint: `${status.metrics.storage.mount ?? "存储池"} ·\n${status.metrics.storage.used} / ${status.metrics.storage.total}`,
        value: `${status.metrics.storage.percent}%`,
        badge: "SSD",
        tone: "ssd",
        current: status.metrics.storage.percent,
        chartMinSpread: 10,
        formatValue: (value) => `${Math.round(value)}%`,
      },
      {
        key: "temperature",
        label: "温度",
        hint: "系统散热状态",
        value:
          status.metrics.temperature.celsius === null
            ? "未知"
            : `${status.metrics.temperature.celsius.toFixed(1)}°C`,
        badge: "°C",
        tone: "temp",
        current: status.metrics.temperature.celsius ?? 0,
        chartMinSpread: 2,
        formatValue: (value) => `${value.toFixed(1)}°C`,
      },
      {
        key: "network",
        label: "网络",
        hint: `接收 ${formatRate(status.metrics.network.rxBps)} ·\n发送 ${formatRate(status.metrics.network.txBps)}`,
        value: formatRate(status.metrics.network.rxBps + status.metrics.network.txBps),
        badge: "NET",
        tone: "network",
        current: status.metrics.network.rxBps + status.metrics.network.txBps,
        chartMinSpread: 1024 * 100,
        formatValue: formatRate,
      },
      {
        key: "diskIo",
        label: "磁盘 I/O",
        hint: `读取 ${formatRate(status.metrics.diskIo.readBps)} ·\n写入 ${formatRate(status.metrics.diskIo.writeBps)}`,
        value: formatRate(status.metrics.diskIo.readBps + status.metrics.diskIo.writeBps),
        badge: "I/O",
        tone: "diskio",
        current: status.metrics.diskIo.readBps + status.metrics.diskIo.writeBps,
        chartMinSpread: 1024 * 100,
        formatValue: formatRate,
      },
    ],
    [status],
  );

  const selectedMetric = metrics.find((metric) => metric.key === activeMetric) ?? null;

  const serviceRows = useMemo(() => {
    if (!services) return [];
    const rows = [
      {
        badge: "DO",
        tone: "docker",
        title: "Docker 服务",
        subtitle: `服务 ${services.docker.active ? "active" : "inactive"} · ${services.docker.version || "版本未知"}`,
        value: `${services.docker.running} 个运行中`,
      },
    ];

    for (const container of services.containers) {
      const isCentral = container.name === "miot_central";
      const isOpenClaw = container.name.toLowerCase().includes("openclaw");
      rows.push({
        badge: isOpenClaw
          ? "OC"
          : container.name
              .split(/[_-]/)
              .map((part) => part[0])
              .join("")
              .slice(0, 2)
              .toUpperCase() || "CT",
        tone: isOpenClaw ? "openclaw" : isCentral ? "central" : "container",
        title: isOpenClaw ? "OpenClaw" : container.name,
        subtitle: isCentral ? "系统容器 · 请勿操作" : `Docker 容器 · ${container.image}`,
        value: container.status.startsWith("Up") ? "运行中" : container.status || "未知",
      });
    }

    return rows;
  }, [services]);

  async function refresh() {
    setRefreshing(true);
    try {
      const response = await fetch(resolveNasApi("nas-status?section=metrics"), { cache: "no-store" });
      if (!response.ok) throw new Error(`API ${response.status}`);

      const payload = (await response.json()) as Omit<NasStatus, "live">;
      if (!payload.ok) throw new Error("NAS 状态读取失败");

      setStatus({ ...payload, history: payload.history ?? [], live: true });
      setStatusError(null);
    } catch (error) {
      setStatus((current) => ({ ...current, live: false }));
      setStatusError(error instanceof Error ? error.message : "本机 API 暂不可用");
    } finally {
      setRefreshing(false);
    }
  }

  async function loadServices(force = false) {
    if (!force && services) return;
    setServicesLoading(true);
    setServicesError(null);
    try {
      const response = await fetch(resolveNasApi("nas-status?section=services"), { cache: "no-store" });
      if (!response.ok) throw new Error(`API ${response.status}`);
      const payload = (await response.json()) as { ok: boolean; services?: ServicesData; error?: string };
      if (!payload.ok || !payload.services) throw new Error(payload.error || "服务状态读取失败");
      setServices(payload.services);
    } catch (error) {
      setServicesError(error instanceof Error ? error.message : "服务状态暂不可用");
    } finally {
      setServicesLoading(false);
    }
  }

  async function loadDrives(force = false) {
    if (!force && drives) return;
    setDrivesLoading(true);
    setDrivesError(null);
    try {
      const response = await fetch(resolveNasApi("nas-status?section=drives"), { cache: "no-store" });
      if (!response.ok) throw new Error(`API ${response.status}`);
      const payload = (await response.json()) as { ok: boolean; drives?: DriveInfo[]; error?: string };
      if (!payload.ok || !payload.drives) throw new Error(payload.error || "硬盘状态读取失败");
      setDrives(payload.drives);
    } catch (error) {
      setDrivesError(error instanceof Error ? error.message : "硬盘状态暂不可用");
    } finally {
      setDrivesLoading(false);
    }
  }

  async function refreshLive() {
    if (liveRefreshInFlight.current) return;
    liveRefreshInFlight.current = true;
    try {
      const response = await fetch(resolveNasApi("live-metrics"), { cache: "no-store" });
      if (!response.ok) throw new Error(`API ${response.status}`);
      const payload = (await response.json()) as LiveMetrics;
      if (!payload.ok) throw new Error("NAS 实时状态读取失败");
      setStatus((current) => {
        const point: HistoryPoint = {
          at: payload.sampledAt,
          cpu: payload.metrics.cpu.percent,
          memory: payload.metrics.memory.percent,
          storage: payload.metrics.storage.percent,
          temperature: payload.metrics.temperature.celsius,
          network: payload.metrics.network.rxBps + payload.metrics.network.txBps,
          diskIo: payload.metrics.diskIo.readBps + payload.metrics.diskIo.writeBps,
        };
        const history = [...current.history];
        if (history.length && point.at - history[history.length - 1].at < 60_000) history[history.length - 1] = point;
        else history.push(point);
        return {
          ...current,
          updatedAt: payload.updatedAt,
          metrics: payload.metrics,
          history: history.slice(-1440),
          live: true,
        };
      });
      setStatusError(null);
    } catch (error) {
      setStatus((current) => ({ ...current, live: false }));
      setStatusError(error instanceof Error ? error.message : "实时监控暂不可用");
    } finally {
      liveRefreshInFlight.current = false;
    }
  }

  function selectMetric(key: MetricKey) {
    setActiveMetric((current) => (current === key ? null : key));
    setDockerOpen(false);
  }

  function toggleDockerDetails() {
    setDockerOpen((current) => !current);
    setActiveMetric(null);
  }

  function changeTab(tab: MainTab) {
    setMainTab(tab);
    if (tab === "services") void loadServices();
    if (tab === "drives") void loadDrives();
    if (tab === "overview") setActiveMetric(null);
  }

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refreshLive();
    }, 2000);
    return () => window.clearInterval(timer);
  }, []);

  const servicesSummary = services
    ? `Docker ${services.docker.active ? "运行中" : "未运行"} · ${services.docker.running} 个容器`
    : servicesLoading
      ? "正在读取服务…"
      : "点击加载 Docker 与容器";
  const drivesSummary = drives
    ? drives.length
      ? `${drives.length} 块物理硬盘`
      : "未检测到物理硬盘"
    : drivesLoading
      ? "正在读取硬盘…"
      : "点击加载 SMART 健康";

  return (
    <div className="manager-app">
      <header className="manager-toolbar">
        <button className="toolbar-button back-button" onClick={leavePlugin} aria-label="返回">
          <ArrowLeftIcon width={21} height={21} />
        </button>
        <span className="toolbar-title">设备管家</span>
        <button
          className={`toolbar-button ${refreshing ? "is-spinning" : ""}`}
          onClick={() => {
            void refresh();
            if (services) void loadServices(true);
            if (drives) void loadDrives(true);
          }}
          aria-label="刷新状态"
        >
          <ReloadIcon width={20} height={20} />
        </button>
      </header>

      <main className="manager-main">
        {mainTab === "overview" ? (
          <>
            <DeviceHeroCard status={status} />
            <section className="manager-section system-section">
              <div className="section-heading">
                <h2>系统状态</h2>
                <span className={`live-chip ${status.live ? "is-live" : "is-demo"}`}>
                  {status.live ? `实时 · ${statusSourceLabel}` : "离线演示"}
                </span>
              </div>
              <div className="metrics-grid">
                {metrics.map((metric) => (
                  <Fragment key={metric.key}>
                    <MetricCard
                      metric={metric}
                      selected={metric.key === activeMetric}
                      onSelect={() => selectMetric(metric.key)}
                    />
                    {metric.key === activeMetric ? <TrendChart metric={metric} points={status.history} /> : null}
                  </Fragment>
                ))}
              </div>
            </section>
          </>
        ) : null}

        {mainTab === "services" ? (
          <section className="manager-section">
            <div className="section-heading">
              <h2>服务</h2>
              <span>{servicesSummary}</span>
            </div>
            {servicesLoading && !services ? (
              <div className="lazy-panel">
                <div className="lazy-body">
                  <div className="lazy-loading" role="status" aria-live="polite">
                    <span className="lazy-spinner" aria-hidden="true" />
                    <p>正在读取…</p>
                  </div>
                </div>
              </div>
            ) : servicesError ? (
              <div className="lazy-panel">
                <div className="lazy-body">
                  <div className="lazy-error" role="alert">
                    <p>{servicesError}</p>
                    <button type="button" onClick={() => void loadServices(true)}>
                      重试
                    </button>
                  </div>
                </div>
              </div>
            ) : services ? (
              <div className="service-card">
                <ServiceRow {...serviceRows[0]} onClick={toggleDockerDetails} expanded={dockerOpen} />
                {serviceRows.slice(1).map((service) => (
                  <ServiceRow key={service.title} {...service} />
                ))}
                {dockerOpen ? <DockerDetails services={services} /> : null}
              </div>
            ) : (
              <div className="lazy-panel">
                <div className="lazy-body">
                  <div className="lazy-loading">
                    <p>切换到本页后开始读取服务状态</p>
                  </div>
                </div>
              </div>
            )}
          </section>
        ) : null}

        {mainTab === "drives" ? (
          <section className="manager-section">
            <div className="section-heading">
              <h2>硬盘健康</h2>
              <span>{drivesSummary}</span>
            </div>
            {drivesLoading && !drives ? (
              <div className="lazy-panel">
                <div className="lazy-body">
                  <div className="lazy-loading" role="status" aria-live="polite">
                    <span className="lazy-spinner" aria-hidden="true" />
                    <p>正在读取…</p>
                  </div>
                </div>
              </div>
            ) : drivesError ? (
              <div className="lazy-panel">
                <div className="lazy-body">
                  <div className="lazy-error" role="alert">
                    <p>{drivesError}</p>
                    <button type="button" onClick={() => void loadDrives(true)}>
                      重试
                    </button>
                  </div>
                </div>
              </div>
            ) : (
              <DriveHealth drives={drives ?? []} />
            )}
          </section>
        ) : null}

        {mainTab === "check" ? (
          <section className="manager-section">
            <div className="section-heading">
              <h2>最近检查</h2>
              <span>{refreshing ? "正在读取设备状态" : `${status.updatedAt} 更新`}</span>
            </div>
            <div className="check-card is-open">
              <div className="check-details">
                <p>
                  {statusError
                    ? `本机 API 暂不可用，当前保留最近一次数据：${statusError}。`
                    : `来自 ${statusSourceLabel} 的只读系统状态${services ? `：Docker ${services.docker.active ? "运行中" : "未运行"}` : ""}。`}
                </p>
                <div className="check-pill-row">
                  <span>CPU {status.metrics.cpu.percent < 85 ? "正常" : "偏高"}</span>
                  <span>内存 {status.metrics.memory.percent < 85 ? "正常" : "偏高"}</span>
                  {services ? <span>服务 {services.docker.active ? "正常" : "异常"}</span> : null}
                </div>
              </div>
            </div>
            <p className="manager-footnote">设备管家 · 让设备状态一目了然</p>
          </section>
        ) : null}
      </main>

      <BottomTabs
        active={mainTab}
        onChange={changeTab}
        servicesReady={Boolean(services)}
        drivesReady={Boolean(drives)}
      />
    </div>
  );
}
