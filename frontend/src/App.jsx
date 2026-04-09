import { useDeferredValue, useEffect, useMemo, useRef, useState, startTransition } from "react";
import { motion } from "framer-motion";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";

const REFRESH_MS = 2000;
const HISTORY_REFRESH_MS = 6000;
const ANALYTICS_REFRESH_MS = 10000;
const NEWS_REFRESH_MS = 10000;
const LIVE_FRESHNESS_MS = 20 * 60 * 1000;

const pageMotion = {
  initial: { opacity: 0, y: 22 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.45, ease: "easeOut" }
};

const featureItems = [
  {
    title: "Trade-First Interface",
    copy: "The live desk keeps the highest-priority setup at the top, with entry, target, risk, score, and market context visible immediately."
  },
  {
    title: "Clear Operational Surfaces",
    copy: "Trading, history, market, news, and controls are separated into focused pages instead of competing inside one crowded dashboard."
  },
  {
    title: "Realtime Backend Sync",
    copy: "Every route reads from the same live Python API layer, so the product stays aligned with the running paper-trading engine."
  },
  {
    title: "Persistent Runtime Controls",
    copy: "Watchlists, focus symbols, and alert controls stay inside the product, so runtime operations do not depend on manual file edits."
  }
];

const workflowSteps = [
  "Start on the home page to read the latest desk status and the strongest active setup.",
  "Move to trades to inspect current opportunities, setup quality, and timing.",
  "Review closed results and market headlines in their own decision-support pages.",
  "Manage the focus symbol, watchlist, and alerts from the controls surface."
];

const navItems = [
  { to: "/", label: "Home" },
  { to: "/trades", label: "Trades" },
  { to: "/history", label: "History" },
  { to: "/news", label: "News" },
  { to: "/market", label: "Market" },
  { to: "/features", label: "Features" },
  { to: "/controls", label: "Controls" }
];

function fmtNumber(value, digits = 4) {
  if (value === null || value === undefined || value === "") return "-";
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return num.toFixed(digits);
}

function fmtPercent(value) {
  if (value === null || value === undefined || value === "") return "-";
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return `${(num * 100).toFixed(1)}%`;
}

function fmtTime(value) {
  if (!value) return "-";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString();
}

function timeframeMinutes(tf) {
  if (!tf) return null;
  const match = tf.match(/^(\d+)(m|h|d)$/i);
  if (!match) return null;
  const n = parseInt(match[1], 10);
  const unit = match[2].toLowerCase();
  if (unit === "m") return n;
  if (unit === "h") return n * 60;
  if (unit === "d") return n * 1440;
  return null;
}

function fmtExpectedClose(openTime, timeframe) {
  if (!openTime || !timeframe) return "-";
  const start = new Date(openTime);
  if (Number.isNaN(start.getTime())) return "-";
  const tfMin = timeframeMinutes(timeframe);
  if (!tfMin) return "-";
  const closeMs = start.getTime() + tfMin * 60000;
  return new Date(closeMs).toLocaleString();
}

function fmtElapsed(value) {
  if (!value) return "-";
  const start = new Date(value);
  if (Number.isNaN(start.getTime())) return "-";
  const diffMs = Math.max(0, Date.now() - start.getTime());
  const totalMinutes = Math.floor(diffMs / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours > 0) return `${hours}h ${minutes}m open`;
  return `${minutes}m open`;
}

function getTradeCloseMs(trade) {
  if (!trade) return null;
  return toEpochMs(trade.closed_at_ms || trade.event_time);
}

function toEpochMs(value) {
  if (!value && value !== 0) return null;
  if (typeof value === "number" && Number.isFinite(value)) {
    return value > 1e12 ? value : value * 1000;
  }
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function isFresh(value, maxAgeMs = LIVE_FRESHNESS_MS) {
  const ts = toEpochMs(value);
  if (!ts) return false;
  return Date.now() - ts <= maxAgeMs;
}

function isTradeOpen(trade) {
  if (!trade) return false;
  if (String(trade.signal_state || "").toUpperCase() === "CLOSED") return false;
  if (trade.closed_result || trade.closed_at_ms) return false;
  // Expire trades that have been open longer than max_wait_candles * timeframe
  // Default: 4 candles. For 15m = 60 min max.
  const openedAt = toEpochMs(trade.updated_at || trade.time);
  if (openedAt) {
    const tfMin = timeframeMinutes(trade.timeframe) || 15;
    const maxOpenMs = tfMin * 2 * 60000; // expire after 2 candles on frontend
    if (Date.now() - openedAt > maxOpenMs) return false;
  }
  return true;
}

function tradeStatus(trade) {
  if (!trade) return "waiting";
  if (String(trade.signal_state || "").toUpperCase() === "CLOSED") return "closed";
  if (trade.closed_result || trade.closed_at_ms) return "closed";
  const openedAt = toEpochMs(trade.updated_at || trade.time);
  if (openedAt) {
    const tfMin = timeframeMinutes(trade.timeframe) || 15;
    const maxOpenMs = tfMin * 4 * 60000;
    if (Date.now() - openedAt > maxOpenMs) return "expired";
  }
  return "active";
}

function playAlertSound() {
  if (typeof window === "undefined") return;
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) return;
  try {
    const ctx = new AudioCtx();
    const notes = [
      { frequency: 880, start: 0, duration: 0.08 },
      { frequency: 1174, start: 0.1, duration: 0.12 }
    ];
    notes.forEach((note) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = note.frequency;
      gain.gain.setValueAtTime(0.0001, ctx.currentTime + note.start);
      gain.gain.exponentialRampToValueAtTime(0.08, ctx.currentTime + note.start + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + note.start + note.duration);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(ctx.currentTime + note.start);
      osc.stop(ctx.currentTime + note.start + note.duration);
    });
    window.setTimeout(() => {
      ctx.close().catch(() => {});
    }, 500);
  } catch {
    // Browser blocked autoplay or audio context
  }
}

async function sendTradeNotification(event) {
  if (typeof window === "undefined" || !("Notification" in window)) return;
  try {
    let permission = Notification.permission;
    if (permission === "default") {
      permission = await Notification.requestPermission();
    }
    if (permission !== "granted") return;
    const side = String(event?.side || "").toUpperCase();
    const timeframe = event?.timeframe || "-";
    const symbol = event?.symbol || "New trade";
    const body = `${side} ${timeframe} at ${formatPrice(event?.entry)} | confidence ${fmtPercent(event?.confidence)}`;
    new Notification(symbol, { body, tag: `trade-${symbol}-${event?.time || Date.now()}` });
  } catch {
    // Ignore notification failures
  }
}

async function sendOpportunityNotification(trade) {
  if (typeof window === "undefined" || !("Notification" in window)) return;
  try {
    if (Notification.permission !== "granted") return;
    const side = String(trade?.side || "").toUpperCase();
    const timeframe = trade?.timeframe || "-";
    const symbol = trade?.symbol || "New setup";
    const body = `${side} ${timeframe} setup | entry ${formatPrice(trade?.entry)} | confidence ${fmtPercent(trade?.confidence)}`;
    new Notification(`Setup: ${symbol}`, { body, tag: `setup-${symbol}-${timeframe}-${side}` });
  } catch {
    // Ignore notification failures
  }
}

function getAlertSupportStatus() {
  if (typeof window === "undefined") return "Alerts unavailable";
  if (!("Notification" in window)) return "Browser notifications unavailable";
  return `Notifications ${Notification.permission}`;
}

function getNotificationPermission() {
  if (typeof window === "undefined" || !("Notification" in window)) return "unsupported";
  return Notification.permission;
}

function formatPrice(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "-";
  return num > 100 ? num.toFixed(2) : num.toFixed(6);
}

function rrFromTrade(trade) {
  if (!trade) return "-";
  if (Number.isFinite(Number(trade.rr))) return fmtNumber(trade.rr, 3);
  const entry = Number(trade.entry);
  const tp = Number(trade.take_profit);
  const sl = Number(trade.stop_loss);
  if (!Number.isFinite(entry) || !Number.isFinite(tp) || !Number.isFinite(sl) || entry === sl) return "-";
  return fmtNumber(Math.abs(tp - entry) / Math.abs(entry - sl), 3);
}

function sideTone(side) {
  return String(side || "").toUpperCase() === "SHORT" ? "short" : "long";
}

function resultTone(result) {
  const upper = String(result || "").toUpperCase();
  if (upper.includes("WIN") || upper.startsWith("TP")) return "win";
  if (upper.includes("LOSS") || upper.startsWith("SL")) return "loss";
  return "neutral";
}

function useLiveDeskData() {
  const [state, setState] = useState(null);
  const [analytics, setAnalytics] = useState(null);
  const [history, setHistory] = useState({ items: [], count: 0, generated_at: null });
  const [news, setNews] = useState({ items: [], generated_at: null });
  const [options, setOptions] = useState({ symbols: [], selected_symbols: [] });
  const [watchlistSymbols, setWatchlistSymbols] = useState([]);
  const [symbolCatalog, setSymbolCatalog] = useState([]);
  const [selectedCoin, setSelectedCoin] = useState("ALL");
  const [watchInput, setWatchInput] = useState("");
  const [statusMessage, setStatusMessage] = useState("Control center ready.");
  const [binance, setBinance] = useState(null);
  const [runtimeSettings, setRuntimeSettings] = useState({
    account: {},
    execution: {},
    strategy: {},
    live_loop: {}
  });

  useEffect(() => {
    let cancelled = false;

    async function loadOptions() {
      const response = await fetch("/api/options", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (cancelled) return;
      startTransition(() => {
        setOptions(payload);
        setWatchlistSymbols(Array.isArray(payload.selected_symbols) ? payload.selected_symbols : []);
        setRuntimeSettings(payload.runtime_settings || { account: {}, execution: {}, strategy: {}, live_loop: {} });
        setSelectedCoin("ALL");
      });
    }

    async function loadCatalog() {
      const response = await fetch("/api/symbols?limit=600", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (cancelled) return;
      startTransition(() => setSymbolCatalog(Array.isArray(payload.symbols) ? payload.symbols : []));
    }

    loadOptions().catch((error) => setStatusMessage(`Options load failed: ${error.message}`));
    loadCatalog().catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function tickState() {
      const response = await fetch("/api/state", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (cancelled) return;
      startTransition(() => setState(payload));
    }
    tickState().catch(() => {});
    const id = window.setInterval(() => {
      tickState().catch(() => {});
    }, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function tickAnalytics() {
      const response = await fetch("/api/analytics", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (cancelled) return;
      startTransition(() => setAnalytics(payload));
    }
    tickAnalytics().catch(() => {});
    const id = window.setInterval(() => {
      tickAnalytics().catch(() => {});
    }, ANALYTICS_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function tickHistory() {
      const response = await fetch("/api/history?limit=500", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (cancelled) return;
      startTransition(() => setHistory(payload));
    }
    tickHistory().catch(() => {});
    const id = window.setInterval(() => {
      tickHistory().catch(() => {});
    }, HISTORY_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function tickNews() {
      const response = await fetch("/api/news", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (cancelled) return;
      startTransition(() => setNews(payload));
    }
    tickNews().catch(() => {});
    const id = window.setInterval(() => {
      tickNews().catch(() => {});
    }, NEWS_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function tickBinance() {
      try {
        const response = await fetch("/api/binance", { cache: "no-store" });
        if (!response.ok) return;
        const payload = await response.json();
        if (cancelled) return;
        startTransition(() => setBinance(payload));
      } catch {}
    }
    tickBinance();
    const id = window.setInterval(tickBinance, 15000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  useEffect(() => {
    if (!watchInput || watchInput.trim().length < 2) return;
    const controller = new AbortController();
    const id = window.setTimeout(async () => {
      try {
        const response = await fetch(`/api/symbols?q=${encodeURIComponent(watchInput)}&limit=40`, {
          cache: "no-store",
          signal: controller.signal
        });
        if (!response.ok) return;
        const payload = await response.json();
        startTransition(() => setSymbolCatalog(Array.isArray(payload.symbols) ? payload.symbols : []));
      } catch {
        // keep last good results
      }
    }, 220);

    return () => {
      controller.abort();
      window.clearTimeout(id);
    };
  }, [watchInput]);

  async function savePrimaryCoin() {
    if (selectedCoin === "ALL") {
      setStatusMessage("Choose a specific symbol before saving a live trading focus.");
      return;
    }
    const response = await fetch("/api/config/symbol", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: selectedCoin })
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    setWatchlistSymbols([selectedCoin]);
    setStatusMessage(payload.message || `${selectedCoin} saved for runtime focus.`);
  }

  async function saveWatchlist() {
    if (!watchlistSymbols.length) {
      setStatusMessage("Add at least one symbol before saving the watchlist.");
      return;
    }
    const response = await fetch("/api/config/symbols", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols: watchlistSymbols })
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    setStatusMessage(payload.message || `Saved ${watchlistSymbols.length} symbols to the runtime watchlist.`);
  }

  function updateRuntimeSetting(section, key, value) {
    setRuntimeSettings((current) => ({
      ...current,
      [section]: {
        ...(current?.[section] || {}),
        [key]: value
      }
    }));
  }

  async function saveRuntimeSettings() {
    const response = await fetch("/api/config/runtime-settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(runtimeSettings)
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    if (payload.runtime_settings) {
      setRuntimeSettings(payload.runtime_settings);
    }
    setStatusMessage(payload.message || "Runtime settings saved.");
  }

  function addWatchSymbol() {
    const symbol = String(watchInput || "").trim().toUpperCase();
    if (!symbol) return;
    if (symbolCatalog.length && !symbolCatalog.includes(symbol)) {
      setStatusMessage(`${symbol} is not in the current Binance futures catalog.`);
      return;
    }
    if (watchlistSymbols.includes(symbol)) {
      setStatusMessage(`${symbol} is already in the watchlist.`);
      return;
    }
    setWatchlistSymbols((current) => [...current, symbol]);
    setWatchInput("");
    setStatusMessage(`${symbol} added to the watchlist.`);
  }

  function removeWatchSymbol(symbol) {
    setWatchlistSymbols((current) => current.filter((item) => item !== symbol));
  }

  return {
    state,
    analytics,
    history,
    news,
    binance,
    options,
    watchlistSymbols,
    symbolCatalog,
    selectedCoin,
    setSelectedCoin,
    watchInput,
    setWatchInput,
    statusMessage,
    setStatusMessage,
    runtimeSettings,
    updateRuntimeSetting,
    saveRuntimeSettings,
    savePrimaryCoin,
    saveWatchlist,
    addWatchSymbol,
    removeWatchSymbol
  };
}

function AppShell({ children, connectionLabel, notificationPermission, onEnableNotifications }) {
  return (
    <div className="page-shell">
      <div className="ambient ambient-a" />
      <div className="ambient ambient-b" />
      <div className="ambient ambient-c" />
      <header className="topbar">
        <NavLink className="brand" to="/">
          <span className="brand-mark">C</span>
          <span>
            Crypto Live Control
            <small>{connectionLabel}</small>
          </span>
        </NavLink>
        <nav className="nav-links">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => `nav-pill${isActive ? " active" : ""}`}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </header>
      {notificationPermission !== "granted" && (
        <section className="notification-banner">
          <div className="notification-badge">Alert Setup</div>
          <div className="notification-banner-copy">
            <strong>Enable Trade Notifications</strong>
            <span>
              {notificationPermission === "denied"
                ? "Notifications are blocked in the browser. Re-enable them for this site to receive live trade and setup alerts."
                : "Allow this website to send an audible and visible alert whenever the desk detects a fresh trade or a new qualifying setup."}
            </span>
          </div>
          <div className="notification-banner-actions">
            {notificationPermission !== "denied" && (
              <button className="surface-button notification-action" type="button" onClick={onEnableNotifications}>
                Allow Notifications
              </button>
            )}
            <span className="notification-state">
              {notificationPermission === "denied" ? "Blocked" : "Permission required"}
            </span>
          </div>
        </section>
      )}
      <main className="page-content">{children}</main>
      <footer className="site-footer">
        <div className="site-footer-inner">
          <div className="footer-brand">
            <strong>Crypto Live Control</strong>
            <span>Realtime paper-trading workspace for Binance futures monitoring, setup review, and runtime operations.</span>
          </div>
          <div className="footer-links">
            <strong>Navigation</strong>
            {navItems.map((item) => (
              <NavLink key={item.to} to={item.to}>
                {item.label}
              </NavLink>
            ))}
          </div>
          <div className="footer-meta">
            <strong>Platform</strong>
            <span>Live setups, closed history, market context, and operational controls.</span>
            <span>Built for continuous monitoring with notification support and Mongo-backed storage.</span>
          </div>
        </div>
      </footer>
    </div>
  );
}

function App() {
  const desk = useLiveDeskData();
  const [notificationPermission, setNotificationPermission] = useState(() => getNotificationPermission());
  const connectionLabel = desk.state?.generated_at ? `Last dashboard update ${fmtTime(desk.state.generated_at)}` : "Connecting to live desk";
  const lastAlertKeyRef = useRef(null);
  const lastOpportunityAlertKeyRef = useRef(null);

  useEffect(() => {
    setNotificationPermission(getNotificationPermission());
  }, []);

  async function enableNotificationsFromShell() {
    playAlertSound();
    if (typeof window === "undefined" || !("Notification" in window)) {
      desk.setStatusMessage("Browser notifications are unavailable in this environment.");
      setNotificationPermission("unsupported");
      return;
    }
    try {
      const permission = await Notification.requestPermission();
      setNotificationPermission(permission);
      desk.setStatusMessage(
        permission === "granted"
          ? "Notifications enabled for new trades."
          : "Notification permission was not granted."
      );
    } catch {
      desk.setStatusMessage("Notification permission request failed.");
    }
  }

  useEffect(() => {
    const events = Array.isArray(desk.state?.recent_events) ? desk.state.recent_events : [];
    const latestOpenTrade = events.find((event) => event?.type === "OPEN_TRADE" && event?.time);
    if (!latestOpenTrade) return;
    if (!isFresh(latestOpenTrade.time, 2 * 60 * 1000)) return;
    const alertKey = `${latestOpenTrade.time}|${latestOpenTrade.primary_symbol || latestOpenTrade.symbol || ""}`;
    if (lastAlertKeyRef.current === alertKey) return;
    lastAlertKeyRef.current = alertKey;
    desk.setStatusMessage(latestOpenTrade.message || "New trade opened.");
    playAlertSound();
    sendTradeNotification({
      symbol: latestOpenTrade.primary_symbol || latestOpenTrade.symbol,
      side: latestOpenTrade.message?.includes(" SHORT ") ? "SHORT" : "LONG",
      timeframe: latestOpenTrade.message?.match(/\b(1m|5m|15m|30m|1h|4h)\b/)?.[1],
      entry: latestOpenTrade.message?.match(/@\s*([0-9.]+)/)?.[1],
      confidence: latestOpenTrade.confidence,
      time: latestOpenTrade.time
    });
  }, [desk.state?.recent_events, desk.setStatusMessage]);

  useEffect(() => {
    const liveTrades = Array.isArray(desk.state?.possible_trades_live) ? desk.state.possible_trades_live : [];
    const topTrade = liveTrades[0];
    if (!topTrade) return;
    if (!isFresh(topTrade.last_seen_time || topTrade.updated_at || topTrade.time, 2 * 60 * 1000)) return;
    const opportunityKey = [
      topTrade.symbol,
      topTrade.side,
      topTrade.timeframe,
      topTrade.entry,
      topTrade.take_profit,
      topTrade.stop_loss
    ].join("|");
    if (lastOpportunityAlertKeyRef.current === opportunityKey) return;
    if (isTradeOpen(desk.state?.open_trade)) return;
    lastOpportunityAlertKeyRef.current = opportunityKey;
    desk.setStatusMessage(`New setup detected: ${topTrade.symbol} ${topTrade.side} ${topTrade.timeframe}.`);
    playAlertSound();
    sendOpportunityNotification(topTrade);
  }, [desk.state?.possible_trades_live, desk.state?.open_trade, desk.setStatusMessage]);

  return (
    <BrowserRouter>
      <AppShell
        connectionLabel={connectionLabel}
        notificationPermission={notificationPermission}
        onEnableNotifications={() => enableNotificationsFromShell().catch(() => {})}
      >
        <Routes>
          <Route path="/" element={<HomePage desk={desk} />} />
          <Route path="/trades" element={<TradesPage desk={desk} />} />
          <Route path="/history" element={<HistoryPage desk={desk} />} />
          <Route path="/news" element={<NewsPage desk={desk} />} />
          <Route path="/market" element={<MarketPage desk={desk} />} />
          <Route path="/features" element={<FeaturesPage desk={desk} />} />
          <Route path="/controls" element={<ControlsPage desk={desk} />} />
        </Routes>
      </AppShell>
    </BrowserRouter>
  );
}

function HomePage({ desk }) {
  const isDeskFresh = isFresh(desk.state?.last_update || desk.state?.generated_at);
  const openTrade = isTradeOpen(desk.state?.open_trade) ? desk.state.open_trade : null;
  const possibleTrades = isDeskFresh && Array.isArray(desk.state?.possible_trades_live)
    ? desk.state.possible_trades_live.filter((trade) => isFresh(trade.last_seen_time || trade.updated_at || trade.time))
    : [];
  const spotlightTrade = openTrade || possibleTrades[0] || null;
  const spotlightPrice = desk.state?.market?.find((row) => row.symbol === spotlightTrade?.symbol)?.price;
  const spotlightTime = spotlightTrade?.updated_at || spotlightTrade?.time || null;

  const overviewCards = [
    { label: "Desk Status", value: desk.state?.status || "WAITING", hint: "Realtime backend state" },
    { label: "Open Trade", value: openTrade?.symbol || "None", hint: openTrade ? openTrade.side : "Scanning" },
    { label: "Possible Trades", value: String(desk.state?.possible_trades_live?.length ?? possibleTrades.length ?? 0), hint: "Current live setups" },
    {
      label: "USDT Net PnL",
      value: `${Number(desk.analytics?.summary?.total_net_pnl_usdt || 0) >= 0 ? "+" : "-"}$${Math.abs(Number(desk.analytics?.summary?.total_net_pnl_usdt || 0)).toFixed(2)}`,
      hint: "Closed trades after estimated fees/slippage"
    },
    { label: "Win Rate", value: fmtPercent(desk.analytics?.win_rate ?? desk.state?.summary?.win_rate), hint: "Historical conversion" },
    { label: "Expectancy", value: `${fmtNumber(desk.analytics?.expectancy_r ?? desk.state?.summary?.expectancy_r, 3)}R`, hint: "Per closed trade" },
    { label: "Stored History", value: String(desk.history?.count ?? 0), hint: "Mongo-backed records" }
  ];

  return (
    <>
      <motion.section className="hero-panel" {...pageMotion}>
        <div className="hero-copy">
          <p className="hero-kicker">Live Trading Desk</p>
          <h1>Realtime market intelligence for a serious paper-trading workflow.</h1>
          <p className="hero-text">
            Monitor the current market state, track the strongest live setup, and move directly into
            trades, history, market, news, and controls without fighting unnecessary visual noise.
          </p>
          <div className="hero-editorial">
            <div>
              <span>Realtime state</span>
              <strong>{desk.state?.status || "WAITING"}</strong>
            </div>
            <div>
              <span>Live trade</span>
              <strong>{openTrade?.symbol || "None"}</strong>
            </div>
            <div>
              <span>Stored history</span>
              <strong>{desk.history?.count || 0}</strong>
            </div>
          </div>
          <div className="hero-actions">
            <NavLink className="primary-link" to="/trades">Open Trades</NavLink>
            <NavLink className="secondary-link" to="/history">View History</NavLink>
          </div>
          {!isDeskFresh && (
            <div className="status-banner">
              Live feed is stale. The last market/trade update is older than 20 minutes, so stale opportunities are hidden.
            </div>
          )}
        </div>
        <motion.div className="hero-card" initial={{ opacity: 0, x: 32 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: 0.6, delay: 0.12 }}>
          <div className="hero-card-top">
            <span className="status-dot" />
            <span>{desk.state?.generated_at ? `Snapshot ${fmtTime(desk.state.generated_at)}` : "Connecting"}</span>
          </div>
          <div className="hero-card-main">
            <p className="hero-card-label">Desk Snapshot</p>
            <h2>{spotlightTrade?.symbol || "No live setup yet"}</h2>
            <div className={`tone-pill ${sideTone(spotlightTrade?.side)}`}>{spotlightTrade?.side || "Scanning"}</div>
            <div className="trade-runtime">
              <span>{`Timeframe ${spotlightTrade?.timeframe || "-"}`}</span>
              <span>{spotlightTrade ? `Open ${fmtElapsed(spotlightTime)}` : "Awaiting signal"}</span>
            </div>
          </div>
          <div className="hero-stat-grid">
            <Stat label="Entry" value={formatPrice(spotlightTrade?.entry)} />
            <Stat label="Live Price" value={formatPrice(spotlightPrice)} />
            <Stat label="Confidence" value={fmtPercent(spotlightTrade?.confidence)} />
            <Stat label="Win Likelihood" value={fmtPercent(spotlightTrade?.win_probability)} />
          </div>
        </motion.div>
      </motion.section>

      <motion.section className="overview-grid" {...pageMotion}>
        {overviewCards.map((item, index) => (
          <motion.article
            key={item.label}
            className="overview-card"
            initial={{ opacity: 0, y: 18 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.35, delay: index * 0.05 }}
          >
            <span>{item.label}</span>
            <strong>{item.value}</strong>
            <small>{item.hint}</small>
          </motion.article>
        ))}
      </motion.section>

      {desk.binance?.enabled && (
      <motion.section className="overview-grid" {...pageMotion}>
        <motion.article className="overview-card" initial={{ opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }}>
            <span>Binance Balance</span>
            <strong>${desk.binance.balance?.toFixed(2)}</strong>
            <small>{desk.binance.demo ? "Demo account" : "Live account"}</small>
        </motion.article>
        <motion.article className="overview-card" initial={{ opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }}>
            <span>Binance Total P&L</span>
            <strong style={{ color: desk.binance.total_pnl >= 0 ? "#22c55e" : "#ef4444" }}>
              {desk.binance.total_pnl >= 0 ? "+$" : "-$"}{Math.abs(desk.binance.total_pnl)?.toFixed(2)}
            </strong>
            <small>Total wallet value, not USDT-only trading PnL</small>
        </motion.article>
          <motion.article className="overview-card" initial={{ opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }}>
            <span>Unrealized P&L</span>
            <strong style={{ color: desk.binance.unrealized_pnl >= 0 ? "#22c55e" : "#ef4444" }}>
              {desk.binance.unrealized_pnl >= 0 ? "+$" : "-$"}{Math.abs(desk.binance.unrealized_pnl)?.toFixed(2)}
            </strong>
            <small>{desk.binance.open_positions?.length || 0} open positions</small>
          </motion.article>
          <motion.article className="overview-card" initial={{ opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }}>
            <span>Available</span>
            <strong>${desk.binance.available?.toFixed(2)}</strong>
            <small>Ready to trade</small>
          </motion.article>
        </motion.section>
      )}

      <motion.section className="feature-grid" {...pageMotion}>
        <QuickPageCard to="/trades" title="Trades" copy="See the live trade spotlight and all current opportunities." />
        <QuickPageCard to="/history" title="History" copy="Review stored trade outcomes with cleaner result formatting." />
        <QuickPageCard to="/news" title="News" copy="Read the market headlines in a dedicated decision-support page." />
        <QuickPageCard to="/controls" title="Controls" copy="Manage active symbol focus and the runtime watchlist." />
      </motion.section>
    </>
  );
}

function TradesPage({ desk }) {
  const [tradeSearch, setTradeSearch] = useState("");
  const deferredTradeSearch = useDeferredValue(tradeSearch);
  const isDeskFresh = isFresh(desk.state?.last_update || desk.state?.generated_at);
  const rawOpenTrade = desk.state?.open_trade || null;
  const marketSnaps = desk.state?.market || [];
  const openTradeStatus = tradeStatus(rawOpenTrade);
  // Check if SL/TP is breached by live price — treat as closed
  const slTpBreached = (() => {
    if (!rawOpenTrade || openTradeStatus !== "active") return false;
    const snap = marketSnaps.find((s) => s.symbol === rawOpenTrade.symbol);
    if (!snap?.price) return false;
    const p = snap.price;
    const sl = rawOpenTrade.stop_loss;
    if (rawOpenTrade.side === "LONG" && sl && p <= sl) return true;
    if (rawOpenTrade.side === "SHORT" && sl && p >= sl) return true;
    return false;
  })();
  const openTrade = (openTradeStatus === "active" && !slTpBreached) ? rawOpenTrade : null;
  const openSymbol = openTrade?.symbol || null;
  const possibleTrades = isDeskFresh && Array.isArray(desk.state?.possible_trades_live)
    ? desk.state.possible_trades_live.filter((trade) => isFresh(trade.last_seen_time || trade.updated_at || trade.time) && trade.symbol !== openSymbol)
    : [];
  const spotlightTrade = openTrade || possibleTrades[0] || null;
  const spotlightTime = spotlightTrade?.updated_at || spotlightTrade?.time || spotlightTrade?.last_seen_time || null;
  const filteredTrades = useMemo(
    () =>
      possibleTrades.filter((trade) => {
        const q = deferredTradeSearch.trim().toLowerCase();
        if (!q) return true;
        return `${trade.symbol} ${trade.side} ${trade.reason}`.toLowerCase().includes(q);
      }),
    [possibleTrades, deferredTradeSearch]
  );

  return (
    <PageWrap eyebrow="Trades" title="Live trades and setups" copy="Review the strongest active trade, scan current opportunities, and filter live setups without leaving the trading surface.">
      {!isDeskFresh && (
        <div className="status-banner">
          The live trader is not emitting fresh cycles right now. This page is hiding stale setups instead of showing old trades as live.
        </div>
      )}
      <div className="two-column">
        <motion.article className="feature-panel spotlight-panel" whileHover={{ y: -4 }}>
          <div className="panel-topline">
            <span>Trade Spotlight</span>
            <span>{openTrade ? "Active trade" : "Highest-ranked setup"}</span>
          </div>
          {openTrade && (() => {
            const snap = marketSnaps.find((s) => s.symbol === openTrade.symbol);
            const binance = openTrade.binance_executed;
            return (
              <>
                {binance && (
                  <div className="status-banner" style={{ background: "#14532d", color: "#86efac" }}>
                    Placed on Binance — Qty: {openTrade.binance_quantity} | Entry: {formatPrice(openTrade.binance_entry_price)} | Notional: ${fmtNumber(openTrade.binance_notional, 2)}
                  </div>
                )}
                {!binance && (
                  <div className="status-banner" style={{ background: "#78350f", color: "#fcd34d" }}>
                    Paper trade only — not placed on Binance
                  </div>
                )}
                {snap?.price && (
                  <div className="status-banner" style={{ background: "#1e293b", color: "#94a3b8" }}>
                    Live price: {formatPrice(snap.price)}
                  </div>
                )}
              </>
            );
          })()}
          <div className="spotlight-head">
            <div>
              <h3>{spotlightTrade?.symbol || "No setup live"}</h3>
              <p>{spotlightTrade?.reason || "The desk is scanning for a qualified trade."}</p>
              <div className="trade-runtime">
                <span>{`Timeframe ${spotlightTrade?.timeframe || "-"}`}</span>
                <span>{spotlightTime ? `Signal: ${fmtTime(spotlightTime)}` : "—"}</span>
                <span>{`${fmtElapsed(spotlightTime)} ago`}</span>
              </div>
            </div>
            <div className={`tone-pill ${sideTone(spotlightTrade?.side)}`}>{spotlightTrade?.side || "Waiting"}</div>
          </div>
          <div className="detail-grid">
            <Stat label="Entry" value={formatPrice(spotlightTrade?.entry)} />
            <Stat label="Take Profit" value={formatPrice(spotlightTrade?.take_profit)} />
            <Stat label="Stop Loss" value={formatPrice(spotlightTrade?.stop_loss)} />
            <Stat label="RR" value={rrFromTrade(spotlightTrade)} />
            <Stat label="Confidence" value={fmtPercent(spotlightTrade?.confidence)} />
            <Stat label="Score" value={fmtNumber(spotlightTrade?.score, 3)} />
            <Stat label="Expected Close" value={fmtExpectedClose(spotlightTime, spotlightTrade?.timeframe)} />
          </div>
        </motion.article>

        <motion.article className="feature-panel search-panel" whileHover={{ y: -4 }}>
          <div className="panel-topline">
            <span>Live Opportunities</span>
            <span>{filteredTrades.length} visible</span>
          </div>
          <input
            className="surface-input"
            value={tradeSearch}
            onChange={(event) => setTradeSearch(event.target.value)}
            placeholder="Filter by symbol, side, or reason"
          />
          <div className="trade-list">
            {filteredTrades.slice(0, 12).map((trade) => (
              <div className="trade-item" key={`${trade.symbol}-${trade.timeframe}-${trade.last_seen_time}`}>
                <div>
                  <strong>{trade.symbol}</strong>
                  <p>{trade.reason}</p>
                  <div className="trade-runtime compact">
                    <span>{`Timeframe ${trade.timeframe || "-"}`}</span>
                    <span>{trade.last_seen_time ? fmtTime(trade.last_seen_time) : "—"}</span>
                  </div>
                </div>
                <div className="trade-meta">
                  <span className={`tone-pill ${sideTone(trade.side)}`}>{trade.side}</span>
                  <small>{fmtPercent(trade.confidence)} conf</small>
                </div>
              </div>
            ))}
            {!filteredTrades.length && <p className="empty-text">No live opportunities match the current filter.</p>}
          </div>
        </motion.article>
      </div>
    </PageWrap>
  );
}

function HistoryPage({ desk }) {
  const [historySearch, setHistorySearch] = useState("");
  const [historyWindow, setHistoryWindow] = useState("7d");
  const [page, setPage] = useState(0);
  const PAGE_SIZE = 15;
  const deferredHistorySearch = useDeferredValue(historySearch);
  const historyRangeOptions = [
    { value: "24h", label: "24H" },
    { value: "7d", label: "7D" },
    { value: "30d", label: "30D" },
    { value: "all", label: "All" }
  ];

  const filteredHistory = useMemo(() => {
    const now = Date.now();
    const search = deferredHistorySearch.trim().toLowerCase();
    const maxAgeMs = {
      "24h": 24 * 60 * 60 * 1000,
      "7d": 7 * 24 * 60 * 60 * 1000,
      "30d": 30 * 24 * 60 * 60 * 1000,
      all: Number.POSITIVE_INFINITY
    }[historyWindow] ?? Number.POSITIVE_INFINITY;

    return (desk.history?.items || []).filter((trade) => {
      if (desk.selectedCoin !== "ALL" && trade.symbol !== desk.selectedCoin) return false;
      const closedAtMs = getTradeCloseMs(trade);
      if (Number.isFinite(maxAgeMs) && closedAtMs && now - closedAtMs > maxAgeMs) return false;
      if (!search) return true;
      return `${trade.symbol} ${trade.side} ${trade.result} ${trade.timeframe} ${trade.reason || ""}`
        .toLowerCase()
        .includes(search);
    });
  }, [desk.history?.items, desk.selectedCoin, deferredHistorySearch, historyWindow]);

  const wins = filteredHistory.filter((t) => t.result === "WIN").length;
  const losses = filteredHistory.filter((t) => t.result === "LOSS").length;
  const totalPnlR = filteredHistory.reduce((sum, t) => sum + (t.pnl_r || 0), 0);
  const winRate = filteredHistory.length > 0 ? ((wins / filteredHistory.length) * 100).toFixed(1) : "0.0";

  return (
    <PageWrap eyebrow="History" title="Closed trade history" copy="Review completed trades, outcomes, and realized performance without mixing them with live setups.">
      <div className="history-summary">
        <div className="overview-card history-summary-card">
          <span>Total Trades</span>
          <strong>{filteredHistory.length}</strong>
          <small>{historyWindow === "all" ? "All stored closed trades now shown." : `Closed trades matching the current ${historyWindow} window.`}</small>
        </div>
        <div className="overview-card history-summary-card">
          <span>Wins</span>
          <strong style={{ color: "#22c55e" }}>{wins}</strong>
          <small>Win rate: {winRate}%</small>
        </div>
        <div className="overview-card history-summary-card">
          <span>Losses</span>
          <strong style={{ color: "#ef4444" }}>{losses}</strong>
          <small>Total PnL: {totalPnlR >= 0 ? "+" : ""}{totalPnlR.toFixed(3)}R</small>
        </div>
        <div className="overview-card history-summary-card">
          <span>Net USDT PnL</span>
          <strong style={{ color: Number(desk.analytics?.summary?.total_net_pnl_usdt || 0) >= 0 ? "#22c55e" : "#ef4444" }}>
            {Number(desk.analytics?.summary?.total_net_pnl_usdt || 0) >= 0 ? "+$" : "-$"}
            {Math.abs(Number(desk.analytics?.summary?.total_net_pnl_usdt || 0)).toFixed(2)}
          </strong>
          <small>Closed trades after estimated fees/slippage</small>
        </div>
        <div className="overview-card history-summary-card">
          <span>Latest Close</span>
          <strong>{filteredHistory[0]?.symbol || "None"}</strong>
          <small>{filteredHistory[0] ? fmtTime(getTradeCloseMs(filteredHistory[0])) : "Waiting for a closed result"}</small>
        </div>
      </div>
      <div className="feature-panel history-toolbar">
        <input
          className="surface-input"
          value={historySearch}
          onChange={(event) => { setHistorySearch(event.target.value); setPage(0); }}
          placeholder="Search symbol, side, result, timeframe, or reason"
        />
        <div className="filter-row">
          {historyRangeOptions.map((option) => (
            <button
              key={option.value}
              type="button"
              className={`filter-chip${historyWindow === option.value ? " active" : ""}`}
              onClick={() => { setHistoryWindow(option.value); setPage(0); }}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>
      <div className="feature-panel" style={{ overflowX: "auto" }}>
        <table className="history-table">
          <thead>
            <tr>
              <th>Opened</th>
              <th>Closed</th>
              <th>Symbol</th>
              <th>Side</th>
              <th>TF</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>Result</th>
              <th>Reason</th>
              <th>Binance</th>
              <th>PnL (R)</th>
              <th>PnL ($)</th>
              <th>Fee Est. ($)</th>
              <th>Net USDT ($)</th>
            </tr>
          </thead>
          <tbody>
            {filteredHistory.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map((trade) => {
              const exitReason = (() => {
                const r = (trade.reason || "").toUpperCase();
                if (r.includes("ADVERSE_CUT")) return "Adverse Cut";
                if (r.includes("STAGNATION")) return "Stagnation";
                if (r.includes("MOMENTUM_REVERSAL")) return "Momentum Rev.";
                if (r.includes("TIMEOUT")) return "Timeout";
                if (trade.result === "WIN") return "TP Hit";
                if (trade.result === "LOSS") return "SL Hit";
                return trade.reason ? trade.reason.split("|")[0].trim() : "—";
              })();
              const pnlColor = (trade.pnl_r || 0) > 0 ? "#22c55e" : (trade.pnl_r || 0) < 0 ? "#ef4444" : "#94a3b8";
              return (
                <tr key={`${trade.trade_key || trade.event_time || trade.symbol}-${trade.closed_at_ms}`}>
                  <td>{trade.opened_at_ms ? fmtTime(trade.opened_at_ms) : "—"}</td>
                  <td>{fmtTime(getTradeCloseMs(trade))}</td>
                  <td><strong>{trade.symbol}</strong></td>
                  <td><span className={`tone-pill compact ${sideTone(trade.side)}`}>{trade.side}</span></td>
                  <td>{trade.timeframe}</td>
                  <td>{formatPrice(trade.entry)}</td>
                  <td>{formatPrice(trade.exit_price)}</td>
                  <td><span className={`result-chip ${resultTone(trade.result)}`}>{trade.result}</span></td>
                  <td className="reason-cell">{exitReason}</td>
                  <td>{trade.binance_executed ? <span style={{ color: "#22c55e" }}>Yes</span> : <span style={{ color: "#64748b" }}>No</span>}</td>
                  <td style={{ color: pnlColor, fontWeight: 600 }}>{trade.pnl_r != null ? (trade.pnl_r >= 0 ? "+" : "") + trade.pnl_r.toFixed(3) : "—"}</td>
                  <td style={{ color: pnlColor }}>{trade.pnl_usd != null ? (trade.pnl_usd >= 0 ? "+$" : "-$") + Math.abs(trade.pnl_usd).toFixed(3) : "—"}</td>
                  <td>{trade.est_cost_usd != null ? `$${Number(trade.est_cost_usd).toFixed(3)}` : "—"}</td>
                  <td style={{ color: Number(trade.net_pnl_usdt || 0) >= 0 ? "#22c55e" : "#ef4444" }}>
                    {trade.net_pnl_usdt != null ? (Number(trade.net_pnl_usdt) >= 0 ? "+$" : "-$") + Math.abs(Number(trade.net_pnl_usdt)).toFixed(3) : "—"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!filteredHistory.length && <div className="empty-panel" style={{ padding: "2rem", textAlign: "center" }}>No completed trades are stored yet.</div>}
        {filteredHistory.length > PAGE_SIZE && (
          <div className="pagination">
            <button type="button" className="filter-chip" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
            <span className="pagination-info">Page {page + 1} of {Math.ceil(filteredHistory.length / PAGE_SIZE)} ({filteredHistory.length} trades)</span>
            <button type="button" className="filter-chip" disabled={(page + 1) * PAGE_SIZE >= filteredHistory.length} onClick={() => setPage(page + 1)}>Next</button>
          </div>
        )}
      </div>
    </PageWrap>
  );
}

function NewsPage({ desk }) {
  const [newsSearch, setNewsSearch] = useState("");
  const deferredNewsSearch = useDeferredValue(newsSearch);
  const filteredNews = useMemo(
    () =>
      (desk.news?.items || []).filter((item) => {
        const q = deferredNewsSearch.trim().toLowerCase();
        if (!q) return true;
        return `${item.title || ""} ${item.source || ""}`.toLowerCase().includes(q);
      }),
    [desk.news, deferredNewsSearch]
  );

  return (
    <PageWrap eyebrow="News" title="Market news and headlines" copy="Use a dedicated news surface for context, headlines, and decision support alongside the live desk.">
      <motion.article className="feature-panel" {...pageMotion}>
        <div className="panel-topline">
          <span>Market News</span>
          <span>{fmtTime(desk.news?.generated_at)}</span>
        </div>
        <input
          className="surface-input"
          value={newsSearch}
          onChange={(event) => setNewsSearch(event.target.value)}
          placeholder="Filter headlines or source"
        />
        <div className="news-grid">
          {filteredNews.slice(0, 12).map((item) => (
            <a key={`${item.link}-${item.title}`} className="news-card" href={item.link} target="_blank" rel="noreferrer">
              <span>{item.source || "Source"}</span>
              <strong>{item.title}</strong>
              <small>{fmtTime(item.published_at)}</small>
            </a>
          ))}
          {!filteredNews.length && <p className="empty-text">No headlines match the current filter.</p>}
        </div>
      </motion.article>
    </PageWrap>
  );
}

function MarketPage({ desk }) {
  return (
    <PageWrap eyebrow="Market" title="Live market board" copy="Track the live symbol board in one place for faster scanning and cleaner market awareness.">
      <div className="market-grid">
        {(desk.state?.market || []).slice(0, 30).map((row) => (
          <motion.article key={`${row.symbol}-${row.time}`} className="market-card" whileHover={{ y: -4 }}>
            <span>{row.symbol}</span>
            <strong>{formatPrice(row.price)}</strong>
            <small>{row.time ? fmtTime(Number(row.time)) : "Live snapshot"}</small>
          </motion.article>
        ))}
      </div>
    </PageWrap>
  );
}

function FeaturesPage() {
  return (
    <PageWrap eyebrow="Features" title="Platform capabilities" copy="Understand how the desk is structured, how live data moves through the system, and what the interface is designed to optimize.">
      <div className="feature-grid">
        {featureItems.map((item) => (
          <motion.article key={item.title} className="feature-panel" whileHover={{ y: -4 }}>
            <h3>{item.title}</h3>
            <p>{item.copy}</p>
          </motion.article>
        ))}
      </div>
      <div className="workflow-grid">
        {workflowSteps.map((step, index) => (
          <motion.div key={step} className="workflow-step" whileHover={{ y: -3 }}>
            <span>{String(index + 1).padStart(2, "0")}</span>
            <p>{step}</p>
          </motion.div>
        ))}
      </div>
    </PageWrap>
  );
}

function ControlsPage({ desk }) {
  const [watchlistPage, setWatchlistPage] = useState(0);
  const WATCHLIST_PAGE_SIZE = 12;
  const totalWatchPages = Math.max(1, Math.ceil(desk.watchlistSymbols.length / WATCHLIST_PAGE_SIZE));
  const pagedWatchlist = desk.watchlistSymbols.slice(
    watchlistPage * WATCHLIST_PAGE_SIZE,
    watchlistPage * WATCHLIST_PAGE_SIZE + WATCHLIST_PAGE_SIZE
  );

  useEffect(() => {
    if (watchlistPage >= totalWatchPages) {
      setWatchlistPage(Math.max(0, totalWatchPages - 1));
    }
  }, [watchlistPage, totalWatchPages]);

  async function enableTradeAlerts() {
    playAlertSound();
    if (typeof window !== "undefined" && "Notification" in window) {
      try {
        const permission = await Notification.requestPermission();
        desk.setStatusMessage(`Trade alerts ready. Notification permission: ${permission}.`);
        return;
      } catch {
        // fall through to generic success message
      }
    }
    desk.setStatusMessage("Trade alert sound tested. Browser notifications are unavailable or blocked.");
  }

  return (
    <PageWrap eyebrow="Controls" title="Runtime controls" copy="Manage live desk configuration, watchlists, and alert settings from one operational surface.">
      <div className="controls-overview">
        <div className="overview-card control-overview-card">
          <span>Primary Focus</span>
          <strong>{desk.selectedCoin}</strong>
          <small>Runtime symbol currently selected in the interface.</small>
        </div>
        <div className="overview-card control-overview-card">
          <span>Watchlist Size</span>
          <strong>{desk.watchlistSymbols.length}</strong>
          <small>Symbols available to the live desk runtime.</small>
        </div>
        <div className="overview-card control-overview-card">
          <span>Trade Alerts</span>
          <strong>{getAlertSupportStatus().replace("Notifications ", "")}</strong>
          <small>Browser notification and sound delivery status.</small>
        </div>
      </div>
      <div className="two-column">
        <article className="feature-panel control-panel">
          <div className="panel-topline">
            <span>Primary Symbol</span>
            <span>{desk.selectedCoin}</span>
          </div>
          <div className="control-copy">
            <h3>Trading Focus</h3>
            <p>Select the symbol you want to work with in the operational views and save it as the active runtime focus.</p>
          </div>
          <select className="surface-input" value={desk.selectedCoin} onChange={(event) => desk.setSelectedCoin(event.target.value)}>
            <option value="ALL">ALL</option>
            {(desk.options?.symbols || []).map((symbol) => (
              <option key={symbol} value={symbol}>
                {symbol}
              </option>
            ))}
          </select>
          <button className="surface-button" type="button" onClick={() => desk.savePrimaryCoin().catch((error) => desk.setStatusMessage(error.message))}>
            Save Active Symbol
          </button>
          <div className="control-subsection">
            <div className="control-subhead">
              <strong>Trade Alerts</strong>
              <span>{getAlertSupportStatus()}</span>
            </div>
            <div className="control-actions">
              <button className="surface-button ghost" type="button" onClick={() => enableTradeAlerts().catch((error) => desk.setStatusMessage(error.message))}>
                Enable Trade Alerts
              </button>
            </div>
            <div className="trade-runtime compact">
              <span>Sound + notification</span>
              <span>Triggers on new trade or setup</span>
            </div>
          </div>
        </article>

        <article className="feature-panel control-panel">
          <div className="panel-topline">
            <span>Watchlist Builder</span>
            <span>{desk.watchlistSymbols.length} symbols</span>
          </div>
          <div className="control-copy">
            <h3>Runtime Watchlist</h3>
            <p>Search Binance futures symbols, add them to the runtime watchlist, and save the full set back to the live system.</p>
          </div>
          <input
            className="surface-input"
            list="symbol_catalog"
            value={desk.watchInput}
            onChange={(event) => desk.setWatchInput(event.target.value)}
            placeholder="Search or type a Binance futures symbol"
          />
          <datalist id="symbol_catalog">
            {desk.symbolCatalog.map((symbol) => (
              <option key={symbol} value={symbol} />
            ))}
          </datalist>
          <div className="control-actions">
            <button className="surface-button" type="button" onClick={desk.addWatchSymbol}>
              Add Symbol
            </button>
            <button className="surface-button ghost" type="button" onClick={() => desk.saveWatchlist().catch((error) => desk.setStatusMessage(error.message))}>
              Save Watchlist
            </button>
          </div>
          <div className="pagination-bar">
            <span>
              Page {totalWatchPages ? watchlistPage + 1 : 1} / {totalWatchPages}
            </span>
            <div className="pagination-actions">
              <button
                className="surface-button ghost pagination-button"
                type="button"
                onClick={() => setWatchlistPage((page) => Math.max(0, page - 1))}
                disabled={watchlistPage === 0}
              >
                Previous
              </button>
              <button
                className="surface-button ghost pagination-button"
                type="button"
                onClick={() => setWatchlistPage((page) => Math.min(totalWatchPages - 1, page + 1))}
                disabled={watchlistPage >= totalWatchPages - 1}
              >
                Next
              </button>
            </div>
          </div>
          <div className="watchlist-grid">
            {pagedWatchlist.map((symbol) => (
              <div key={symbol} className="watch-chip">
                <span>{symbol}</span>
                <button
                  className="watch-chip-remove"
                  type="button"
                  aria-label={`Remove ${symbol}`}
                  onClick={() => desk.removeWatchSymbol(symbol)}
                >
                  Remove
                </button>
              </div>
            ))}
          </div>
        </article>
      </div>
      <article className="feature-panel control-panel config-panel">
        <div className="panel-topline">
          <span>Runtime Config</span>
          <span>Live controls</span>
        </div>
        <div className="control-copy">
          <h3>Trading Parameters</h3>
          <p>Update wallet risk, execution cost, and key strategy thresholds from this page instead of editing config files manually.</p>
        </div>
        <div className="config-sections">
          <div className="config-section">
            <div className="control-subhead">
              <strong>Account</strong>
              <span>Wallet-based risk sizing</span>
            </div>
            <div className="config-grid">
              <ConfigField
                label="Starting Balance USD"
                value={desk.runtimeSettings?.account?.starting_balance_usd ?? ""}
                step="0.01"
                onChange={(value) => desk.updateRuntimeSetting("account", "starting_balance_usd", value)}
              />
              <ConfigField
                label="Risk Per Trade %"
                value={desk.runtimeSettings?.account?.risk_per_trade_pct ?? ""}
                step="0.001"
                hint="Use 0.02 for 2%"
                onChange={(value) => desk.updateRuntimeSetting("account", "risk_per_trade_pct", value)}
              />
              <ConfigField
                label="Paper Risk USD"
                value={desk.runtimeSettings?.account?.paper_risk_usd ?? ""}
                step="0.01"
                hint="Optional override"
                onChange={(value) => desk.updateRuntimeSetting("account", "paper_risk_usd", value)}
              />
            </div>
          </div>

          <div className="config-section">
            <div className="control-subhead">
              <strong>Execution</strong>
              <span>Fee and slippage model</span>
            </div>
            <div className="config-grid">
              <ConfigField
                label="Fee Bps Per Side"
                value={desk.runtimeSettings?.execution?.fee_bps_per_side ?? ""}
                step="0.1"
                onChange={(value) => desk.updateRuntimeSetting("execution", "fee_bps_per_side", value)}
              />
              <ConfigField
                label="Slippage Bps Per Side"
                value={desk.runtimeSettings?.execution?.slippage_bps_per_side ?? ""}
                step="0.1"
                onChange={(value) => desk.updateRuntimeSetting("execution", "slippage_bps_per_side", value)}
              />
              <ConfigField
                label="Max Open Trades"
                value={desk.runtimeSettings?.live_loop?.max_open_trades ?? ""}
                step="1"
                onChange={(value) => desk.updateRuntimeSetting("live_loop", "max_open_trades", value)}
              />
            </div>
          </div>

          <div className="config-section">
            <div className="control-subhead">
              <strong>Strategy</strong>
              <span>Signal construction</span>
            </div>
            <div className="config-grid">
              <ConfigField
                label="ATR Multiplier"
                value={desk.runtimeSettings?.strategy?.atr_multiplier ?? ""}
                step="0.1"
                onChange={(value) => desk.updateRuntimeSetting("strategy", "atr_multiplier", value)}
              />
              <ConfigField
                label="Risk Reward"
                value={desk.runtimeSettings?.strategy?.risk_reward ?? ""}
                step="0.1"
                onChange={(value) => desk.updateRuntimeSetting("strategy", "risk_reward", value)}
              />
              <ConfigField
                label="Min Confidence"
                value={desk.runtimeSettings?.strategy?.min_confidence ?? ""}
                step="0.01"
                onChange={(value) => desk.updateRuntimeSetting("strategy", "min_confidence", value)}
              />
            </div>
          </div>

          <div className="config-section">
            <div className="control-subhead">
              <strong>Live Filters</strong>
              <span>Execution gates and exits</span>
            </div>
            <div className="config-grid">
              <ConfigField
                label="Candidate Confidence"
                value={desk.runtimeSettings?.live_loop?.min_candidate_confidence ?? ""}
                step="0.01"
                onChange={(value) => desk.updateRuntimeSetting("live_loop", "min_candidate_confidence", value)}
              />
              <ConfigField
                label="Candidate Expectancy R"
                value={desk.runtimeSettings?.live_loop?.min_candidate_expectancy_r ?? ""}
                step="0.01"
                onChange={(value) => desk.updateRuntimeSetting("live_loop", "min_candidate_expectancy_r", value)}
              />
              <ConfigField
                label="Execute Confidence"
                value={desk.runtimeSettings?.live_loop?.execute_min_confidence ?? ""}
                step="0.01"
                onChange={(value) => desk.updateRuntimeSetting("live_loop", "execute_min_confidence", value)}
              />
              <ConfigField
                label="Execute Expectancy R"
                value={desk.runtimeSettings?.live_loop?.execute_min_expectancy_r ?? ""}
                step="0.01"
                onChange={(value) => desk.updateRuntimeSetting("live_loop", "execute_min_expectancy_r", value)}
              />
              <ConfigField
                label="Execute Score"
                value={desk.runtimeSettings?.live_loop?.execute_min_score ?? ""}
                step="0.01"
                onChange={(value) => desk.updateRuntimeSetting("live_loop", "execute_min_score", value)}
              />
              <ConfigField
                label="Execute Win Probability"
                value={desk.runtimeSettings?.live_loop?.execute_min_win_probability ?? ""}
                step="0.01"
                onChange={(value) => desk.updateRuntimeSetting("live_loop", "execute_min_win_probability", value)}
              />
              <ConfigField
                label="Max Wait Candles"
                value={desk.runtimeSettings?.live_loop?.max_wait_candles ?? ""}
                step="1"
                onChange={(value) => desk.updateRuntimeSetting("live_loop", "max_wait_candles", value)}
              />
              <ConfigField
                label="Trail Trigger R"
                value={desk.runtimeSettings?.live_loop?.trail_trigger_r ?? ""}
                step="0.01"
                onChange={(value) => desk.updateRuntimeSetting("live_loop", "trail_trigger_r", value)}
              />
              <ConfigField
                label="Break-even Trigger R"
                value={desk.runtimeSettings?.live_loop?.break_even_trigger_r ?? ""}
                step="0.01"
                onChange={(value) => desk.updateRuntimeSetting("live_loop", "break_even_trigger_r", value)}
              />
              <ConfigField
                label="Max Adverse Cut R"
                value={desk.runtimeSettings?.live_loop?.max_adverse_r_cut ?? ""}
                step="0.01"
                onChange={(value) => desk.updateRuntimeSetting("live_loop", "max_adverse_r_cut", value)}
              />
            </div>
          </div>
        </div>
        <div className="control-actions">
          <button className="surface-button" type="button" onClick={() => desk.saveRuntimeSettings().catch((error) => desk.setStatusMessage(error.message))}>
            Save Runtime Settings
          </button>
        </div>
      </article>
      <div className="status-banner">{desk.statusMessage}</div>
    </PageWrap>
  );
}

function ConfigField({ label, value, onChange, step = "0.01", hint = "" }) {
  return (
    <label className="config-field">
      <span>{label}</span>
      <input
        className="surface-input"
        type="number"
        inputMode="decimal"
        step={step}
        value={value ?? ""}
        onChange={(event) => onChange(event.target.value)}
      />
      {hint ? <small>{hint}</small> : null}
    </label>
  );
}

function PageWrap({ eyebrow, title, copy, children }) {
  return (
    <motion.section className="route-page" {...pageMotion}>
      <div className="section-header route-header">
        <p>{eyebrow}</p>
        <h1>{title}</h1>
        <span>{copy}</span>
      </div>
      {children}
    </motion.section>
  );
}

function QuickPageCard({ to, title, copy }) {
  return (
    <motion.article className="feature-panel quick-card" whileHover={{ y: -4 }}>
      <h3>{title}</h3>
      <p>{copy}</p>
      <NavLink className="text-link" to={to}>
        Open Page
      </NavLink>
    </motion.article>
  );
}

function Stat({ label, value }) {
  return (
    <div className="stat-block">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export default App;
