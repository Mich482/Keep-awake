import ctypes
import ctypes.wintypes
import json
import os
import sys
import threading
import time
import winreg
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import win32event
import win32api
import win32con

import pystray
from PIL import Image

APP_NAME = "Keep Awake"

# Behavior
IDLE_THRESHOLD_SECONDS = 240     # 4 minutes
CHECK_INTERVAL = 2              # how often we check (seconds)
HISTORY_INTERVAL = 2            # history sampling (seconds)
HISTORY_LEN = 240               # points (~8 minutes at 2s)
INTERCEPT_WINDOW_SECONDS = 8    # show "armed/intercept" for this long after jiggle

# Risk thresholds (ratios of threshold)
RISK_LOW_MAX = 0.50
RISK_MID_MAX = 0.75
RISK_HIGH_MAX = 0.95
# critical: >= 0.95

# Active threshold (below this we consider "ACTIVE" / "not really idle")
ACTIVE_IDLE_SECONDS = 5

_SINGLE_INSTANCE_MUTEX = None
ERROR_ALREADY_EXISTS = 183  # WinAPI constant

def ensure_single_instance(name=r"Local\KeepAwakeMutex") -> bool:
    global _SINGLE_INSTANCE_MUTEX

    _SINGLE_INSTANCE_MUTEX = win32event.CreateMutex(None, True, name)

    last_err = ctypes.windll.kernel32.GetLastError()

    if last_err == ERROR_ALREADY_EXISTS:
        return False

    return True

# ---------------- Windows idle + lock state ----------------

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_idle_seconds() -> int:
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
    tick = ctypes.windll.kernel32.GetTickCount()
    return max(0, int((tick - lii.dwTime) / 1000))


def is_workstation_unlocked() -> bool:
    """
    True when input desktop is 'Default'. When locked it's usually 'Winlogon'.
    Conservative: if uncertain, return False to avoid jiggle.
    """
    user32 = ctypes.windll.user32
    OpenInputDesktop = user32.OpenInputDesktop
    OpenInputDesktop.restype = ctypes.wintypes.HANDLE

    GetUserObjectInformationW = user32.GetUserObjectInformationW
    CloseDesktop = user32.CloseDesktop

    UOI_NAME = 2
    DESKTOP_READOBJECTS = 0x0001

    hdesk = OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not hdesk:
        return False

    try:
        needed = ctypes.c_uint(0)
        GetUserObjectInformationW(hdesk, UOI_NAME, None, 0, ctypes.byref(needed))
        if needed.value == 0:
            return False

        buf = ctypes.create_unicode_buffer(needed.value // ctypes.sizeof(ctypes.c_wchar))
        ok = GetUserObjectInformationW(hdesk, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed))
        if not ok:
            return False

        return buf.value.lower() == "default"
    finally:
        CloseDesktop(hdesk)


def jiggle_mouse():
    """Micro move 1px right then back."""
    win32api.mouse_event(win32con.MOUSEEVENTF_MOVE, 1, 0, 0, 0)
    time.sleep(0.03)
    win32api.mouse_event(win32con.MOUSEEVENTF_MOVE, -1, 0, 0, 0)


# ---------------- Startup (no admin) ----------------

def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_path(rel: str) -> str:
    """
    Resolve resource paths in:
      - normal run (relative to app.py)
      - PyInstaller onedir (resources may be in dist\\app\\ or dist\\app\\_internal\\)
      - PyInstaller onefile (sys._MEIPASS)
    """
    if is_frozen():
        # onefile
        if hasattr(sys, "_MEIPASS"):
            base = sys._MEIPASS
            return os.path.join(base, rel)

        # onedir: exe folder; resources sometimes end up in _internal
        exe_dir = os.path.dirname(sys.executable)

        candidates = [
            os.path.join(exe_dir, rel),
            os.path.join(exe_dir, "_internal", rel),
        ]
        for p in candidates:
            if os.path.exists(p):
                return p

        # fallback (still return first candidate)
        return candidates[0]

    # script mode
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


def add_to_startup():
    if is_frozen():
        command = f'"{sys.executable}"'
    else:
        command = f'"{sys.executable}" "{os.path.abspath(__file__)}"'

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE,
    )
    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
    winreg.CloseKey(key)


def remove_from_startup():
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.DeleteValue(key, APP_NAME)
        winreg.CloseKey(key)
    except FileNotFoundError:
        pass


def startup_enabled() -> bool:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        )
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


# ---------------- Formatting helpers ----------------

def format_duration(seconds: int) -> str:
    seconds = int(max(0, seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def iso_local(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


# ---------------- State + logic ----------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.start_ts = time.time()
            self.wake_count = 0
            self.last_wake_ts = None         # last time we performed jiggle
            self.last_intercept_ts = None    # last time we *intercepted* (same as wake here)
            self.history = deque(maxlen=HISTORY_LEN)
            self._last_history_sample_ts = 0.0

    def maybe_sample_history(self, idle_s: int, now: float):
        with self.lock:
            if now - self._last_history_sample_ts >= HISTORY_INTERVAL:
                self.history.append(idle_s)
                self._last_history_sample_ts = now


STATE = State()


def compute_risk(idle_s: int, threshold_s: int) -> tuple[str, float]:
    ratio = 0.0 if threshold_s <= 0 else (idle_s / threshold_s)
    if ratio < RISK_LOW_MAX:
        return "low", ratio
    if ratio < RISK_MID_MAX:
        return "mid", ratio
    if ratio < RISK_HIGH_MAX:
        return "high", ratio
    return "critical", ratio


def compute_state(unlocked: bool, idle_s: int, threshold_s: int, now: float, last_intercept_ts: float | None) -> str:
    if not unlocked:
        return "LOCKED"
    if last_intercept_ts and (now - last_intercept_ts) <= INTERCEPT_WINDOW_SECONDS:
        return "INTERCEPT"
    if idle_s <= ACTIVE_IDLE_SECONDS:
        return "ACTIVE"
    ratio = 0.0 if threshold_s <= 0 else (idle_s / threshold_s)
    if ratio < RISK_LOW_MAX:
        return "IDLE"
    if ratio < RISK_MID_MAX:
        return "WARNING"
    if ratio < 1.0:
        return "HIGH"
    return "CRITICAL"


def should_intercept(unlocked: bool, idle_s: int, threshold_s: int) -> bool:
    return unlocked and idle_s >= threshold_s


# ---------------- Local stats server (browser UI) ----------------

class StatsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/stats.json"):
            payload = self.server.get_stats()  # type: ignore[attr-defined]
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        html = self.server.get_page().encode("utf-8")  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, format, *args):
        return


class StatsServer(ThreadingHTTPServer):
    def get_stats(self):
        now = time.time()
        idle = get_idle_seconds()
        unlocked = is_workstation_unlocked()

        STATE.maybe_sample_history(idle, now)

        with STATE.lock:
            uptime = int(now - STATE.start_ts)
            wakes = STATE.wake_count
            last_wake_ts = STATE.last_wake_ts
            last_intercept_ts = STATE.last_intercept_ts
            hist = list(STATE.history)

        risk, ratio = compute_risk(idle, IDLE_THRESHOLD_SECONDS)
        state = compute_state(unlocked, idle, IDLE_THRESHOLD_SECONDS, now, last_intercept_ts)

        # "armed": visible during intercept window
        armed = (state == "INTERCEPT")

        # Precompute "last wake" string
        last_wake_str = "never" if not last_wake_ts else iso_local(last_wake_ts)

        # Useful extras
        avg_idle = int(sum(hist) / len(hist)) if hist else idle
        max_idle = max(hist) if hist else idle

        return {
            "app": APP_NAME,
            "uptime_s": uptime,
            "idle_s": idle,
            "unlocked": unlocked,

            "threshold_s": IDLE_THRESHOLD_SECONDS,
            "idle_ratio": ratio,

            "risk": risk,              # low / mid / high / critical
            "state": state,            # ACTIVE / IDLE / WARNING / HIGH / CRITICAL / INTERCEPT / LOCKED
            "armed": armed,

            "avg_idle_s": avg_idle,
            "max_idle_s": max_idle,

            "wakes": wakes,
            "last_wake": last_wake_str,
            "last_wake_ts": last_wake_ts,             # epoch or null
            "last_intercept_ts": last_intercept_ts,   # epoch or null

            "startup": startup_enabled(),
            "history": hist[-90:],  # last N points for chart
        }

    def get_page(self) -> str:
        # NOTE: not an f-string on purpose (avoid brace escaping hell)
        html = """<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>__APP_NAME__ Stats</title>
  <style>
    :root {
      --bg: #0b0f14;
      --panel: #111827;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --good: #34d399;
      --warn: #fbbf24;
      --bad: #fb7185;
      --info: #60a5fa;
      --border: rgba(255,255,255,0.10);
    }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      margin: 16px;
    }

    h2 { margin: 0 0 10px 0; font-size: 18px; }

    .row { display:flex; gap: 12px; flex-wrap: wrap; align-items: stretch; }

    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 12px;
    }
    .card.wide { flex: 1 1 560px; }
    .card.narrow { flex: 0 0 320px; max-width: 320px; }

    .k { color: var(--muted); display:inline-block; width: 150px; }

    .v.good { color: var(--good); }
    .v.warn { color: var(--warn); }
    .v.bad  { color: var(--bad); }
    .v.info { color: var(--info); }

    canvas { width: 100%; height: 260px; display:block; }

    pre {
      margin: 10px 0 0 0;
      padding: 10px;
      background: rgba(255,255,255,0.04);
      border: 1px solid var(--border);
      border-radius: 12px;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      word-break: break-word;
    }

    button {
      background: rgba(255,255,255,0.06);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 8px 10px;
      cursor: pointer;
    }
    button:hover { background: rgba(255,255,255,0.10); }

    .pill {
      display:inline-block; padding: 2px 8px; border-radius: 999px;
      border: 1px solid var(--border); color: var(--muted);
      margin-right: 6px;
      display: none;
    }
    .pill.good { color: var(--good); }
    .pill.warn { color: var(--warn); }
    .pill.bad  { color: var(--bad); }
    .pill.info { color: var(--info); }
  </style>
</head>
<body>
  <h2>__APP_NAME__ — stats</h2>

  <div class="row">
    <div class="card wide">
      <div class="row" style="gap: 18px;">
        <div style="flex:1;">
          <div><span class="k">Uptime</span> <span id="uptime" class="v"></span></div>
          <div><span class="k">State</span> <span id="state" class="v info"></span></div>
          <div><span class="k">Risk</span> <span id="risk" class="v"></span></div>
          <div><span class="k">Unlocked</span> <span id="unlocked" class="v"></span></div>
          <div><span class="k">Idle now</span> <span id="idle" class="v"></span></div>
          <div><span class="k">Avg / Max idle</span> <span id="avgmax" class="v"></span></div>
          <div><span class="k">Threshold</span> <span id="th" class="v"></span></div>
        </div>
        <div style="flex:1;">
          <div><span class="k">Wakes</span> <span id="wakes" class="v"></span></div>
          <div><span class="k">Wakes / hour</span> <span id="wph" class="v"></span></div>
          <div><span class="k">Last wake</span> <span id="lastwake" class="v"></span></div>
          <div><span class="k">Since last wake</span> <span id="sincewake" class="v"></span></div>
          <div><span class="k">Armed</span> <span id="armedTxt" class="v"></span></div>
          <div><span class="k">Startup</span> <span id="startup" class="v"></span></div>
        </div>
      </div>

      <div style="display:flex; justify-content: space-between; align-items:center; margin-top: 10px;">
        <div>
          <span class="pill" id="armedPill">armed: ?</span>
          <span class="pill" id="riskPill">risk: ?</span>
        </div>
        <div style="display:flex; gap: 8px;">
          <button onclick="fetchStats()">Refresh</button>
          <button onclick="toggleRaw()" id="rawBtn">Show raw</button>
        </div>
      </div>

      <div style="margin-top: 12px;">
        <canvas id="chart"></canvas>
      </div>
    </div>

    <div class="card narrow" id="rawCard">
      <div style="color: var(--muted); margin-bottom: 8px;">Raw history</div>
      <pre id="raw"></pre>
    </div>
  </div>

<script>
function fmt(sec) {
  sec = Math.max(0, sec|0);
  const m = Math.floor(sec/60), s = sec%60;
  const h = Math.floor(m/60), mm = m%60;
  if (h) return `${h}h ${mm}m ${s}s`;
  if (m) return `${m}m ${s}s`;
  return `${s}s`;
}

function clsForRisk(risk) {
  if (risk === "low") return "good";
  if (risk === "mid") return "info";
  if (risk === "high") return "warn";
  return "bad"; // critical
}

function clsForIdle(idle, th) {
  if (idle >= th) return "bad";
  if (idle >= th*0.75) return "warn";
  return "good";
}

function setText(id, txt, cls=null) {
  const el = document.getElementById(id);
  el.textContent = txt;
  el.className = "v" + (cls ? " " + cls : "");
}

function setPill(id, txt, cls=null) {
  const el = document.getElementById(id);
  el.textContent = txt;
  el.className = "pill" + (cls ? " " + cls : "");
}

function drawChart(canvas, history, threshold) {
  const cssW = canvas.clientWidth || 1200;
  const cssH = canvas.clientHeight || 260;

  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.floor(cssW * dpr);
  canvas.height = Math.floor(cssH * dpr);

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const w = cssW, h = cssH;
  ctx.clearRect(0, 0, w, h);

  const padL = 58, padR = 18, padT = 14, padB = 34;
  const iw = w - padL - padR;
  const ih = h - padT - padB;

  const maxHist = history.length ? Math.max(...history) : 0;
  const maxv = Math.max(1, maxHist, threshold);

  const xAt = (i) => padL + iw * (i / Math.max(1, history.length - 1));
  const yAt = (v) => padT + ih * (1 - (v / maxv));

  // background
  ctx.fillStyle = "rgba(255,255,255,0.02)";
  ctx.fillRect(0, 0, w, h);

  // grid + y labels
  const yTicks = 5;
  ctx.strokeStyle = "rgba(255,255,255,0.08)";
  ctx.lineWidth = 1;
  ctx.font = "12px ui-monospace, Menlo, Consolas, monospace";
  ctx.fillStyle = "rgba(156,163,175,0.9)";

  for (let i = 0; i <= yTicks; i++) {
    const v = Math.round((maxv * (yTicks - i)) / yTicks);
    const y = padT + (ih * i) / yTicks;

    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(padL + iw, y);
    ctx.stroke();

    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    ctx.fillText(String(v), padL - 10, y);
  }

  // vertical grid
  const xTicks = 6;
  for (let i = 0; i <= xTicks; i++) {
    const x = padL + (iw * i) / xTicks;
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, padT + ih);
    ctx.stroke();
  }

  // threshold band + line
  const thY = yAt(threshold);
  ctx.fillStyle = "rgba(251,191,36,0.08)";
  ctx.fillRect(padL, thY, iw, padT + ih - thY);

  ctx.strokeStyle = "rgba(251,191,36,0.85)";
  ctx.setLineDash([8, 6]);
  ctx.beginPath();
  ctx.moveTo(padL, thY);
  ctx.lineTo(padL + iw, thY);
  ctx.stroke();
  ctx.setLineDash([]);

  // threshold label (clamped inside plot)
  ctx.fillStyle = "rgba(251,191,36,0.95)";
  ctx.textAlign = "left";
  ctx.textBaseline = "bottom";
  const labelY = Math.max(padT + 14, Math.min(padT + ih - 4, thY - 6));
  ctx.fillText(`threshold = ${threshold}s`, padL + 8, labelY);

  if (!history.length) {
    ctx.fillStyle = "rgba(156,163,175,0.9)";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText("no data yet", padL + iw / 2, padT + ih / 2);
    return;
  }

  const pts = history.map((v, i) => ({ x: xAt(i), y: yAt(v), v }));
  const smooth = pts.map((p, i) => {
    const a = pts[Math.max(0, i - 1)];
    const b = p;
    const c = pts[Math.min(pts.length - 1, i + 1)];
    return { x: b.x, y: (a.y + b.y + c.y) / 3, v: b.v };
  });

  // area gradient
  const grad = ctx.createLinearGradient(0, padT, 0, padT + ih);
  grad.addColorStop(0, "rgba(96,165,250,0.22)");
  grad.addColorStop(1, "rgba(96,165,250,0.02)");

  ctx.beginPath();
  ctx.moveTo(smooth[0].x, smooth[0].y);
  for (let i = 1; i < smooth.length; i++) ctx.lineTo(smooth[i].x, smooth[i].y);
  ctx.lineTo(smooth[smooth.length - 1].x, padT + ih);
  ctx.lineTo(smooth[0].x, padT + ih);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // line
  ctx.beginPath();
  ctx.moveTo(smooth[0].x, smooth[0].y);
  for (let i = 1; i < smooth.length; i++) ctx.lineTo(smooth[i].x, smooth[i].y);
  ctx.strokeStyle = "rgba(96,165,250,0.95)";
  ctx.lineWidth = 2.5;
  ctx.stroke();

  // last point marker
  const last = smooth[smooth.length - 1];
  ctx.fillStyle = "rgba(52,211,153,0.95)";
  ctx.beginPath();
  ctx.arc(last.x, last.y, 4.5, 0, Math.PI * 2);
  ctx.fill();

  // footer
  ctx.fillStyle = "rgba(156,163,175,0.9)";
  ctx.textAlign = "left";
  ctx.textBaseline = "top";
  ctx.fillText(`points: ${history.length}`, padL, padT + ih + 10);

  // tooltip via title (nearest point)
  canvas._chartPts = pts;
}

function installChartHover() {
  const canvas = document.getElementById("chart");
  canvas.addEventListener("mousemove", (e) => {
    const pts = canvas._chartPts;
    if (!pts || !pts.length) return;

    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;

    let best = pts[0], bestDx = Infinity;
    for (const p of pts) {
      const dx = Math.abs(p.x - x);
      if (dx < bestDx) { bestDx = dx; best = p; }
    }
    canvas.title = `idle: ${best.v}s`;
  });
}

function setRawVisible(visible) {
  const card = document.getElementById("rawCard");
  const btn = document.getElementById("rawBtn");
  card.style.display = visible ? "" : "none";
  btn.textContent = visible ? "Hide raw" : "Show raw";
  localStorage.setItem("rawVisible", visible ? "1" : "0");
}

function toggleRaw() {
  const card = document.getElementById("rawCard");
  const isHidden = (card.style.display === "none");
  setRawVisible(isHidden);
}

async function fetchStats() {
  const res = await fetch("/stats.json", { cache: "no-store" });
  const s = await res.json();

  const idleCls = clsForIdle(s.idle_s, s.threshold_s);
  const riskCls = clsForRisk(s.risk);

  setText("uptime", fmt(s.uptime_s));
  setText("state", s.state, "info");
  setText("risk", `${s.risk} (${Math.round((s.idle_ratio || 0)*100)}%)`, riskCls);

  setText("unlocked", String(s.unlocked), s.unlocked ? "good" : "warn");
  setText("idle", fmt(s.idle_s), idleCls);
  setText("avgmax", `${fmt(s.avg_idle_s)} / ${fmt(s.max_idle_s)}`);
  setText("th", `${s.threshold_s}s`);

  setText("wakes", String(s.wakes));
  const wph = (s.uptime_s > 0) ? (s.wakes / (s.uptime_s/3600)) : 0;
  setText("wph", wph.toFixed(2));

  setText("lastwake", s.last_wake);

  if (!s.last_wake_ts) {
    setText("sincewake", "n/a");
  } else {
    const now = Date.now() / 1000;
    const delta = Math.max(0, Math.floor(now - s.last_wake_ts));
    setText("sincewake", fmt(delta));
  }

  setText("armedTxt", String(s.armed), s.armed ? "warn" : "good");
  setText("startup", String(s.startup));

  setPill("armedPill", `armed: ${s.armed}`, s.armed ? "warn" : "good");
  setPill("riskPill", `risk: ${s.risk}`, riskCls);

  document.getElementById("raw").textContent =
    "values (newest last):\\n" + (s.history || []).map(v => String(v)).join("\\n");

  drawChart(document.getElementById("chart"), s.history || [], s.threshold_s);
}

installChartHover();

// raw visibility persisted
const rawVisible = (localStorage.getItem("rawVisible") || "0") === "1";
setRawVisible(rawVisible);

fetchStats();
setInterval(fetchStats, __INTERVAL__);
</script>
</body>
</html>
"""
        return (
            html
            .replace("__APP_NAME__", APP_NAME)
            .replace("__INTERVAL__", str(int(HISTORY_INTERVAL * 1000)))
        )


_stats_server = None
_stats_url = None


def start_stats_server_once():
    global _stats_server, _stats_url
    if _stats_server is not None:
        return

    srv = StatsServer(("127.0.0.1", 0), StatsHandler)
    _stats_server = srv
    _stats_url = f"http://127.0.0.1:{srv.server_address[1]}/"

    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()


def open_stats_in_browser():
    start_stats_server_once()
    webbrowser.open(_stats_url, new=1)  # type: ignore[arg-type]


# ---------------- Tray ----------------

def update_tray_title(icon: pystray.Icon):
    # now = time.time()
    # idle = get_idle_seconds()
    # unlocked = is_workstation_unlocked()
    with STATE.lock:
        # last_intercept_ts = STATE.last_intercept_ts
        wakes = STATE.wake_count

    # state = compute_state(unlocked, idle, IDLE_THRESHOLD_SECONDS, now, last_intercept_ts)
    # risk, ratio = compute_risk(idle, IDLE_THRESHOLD_SECONDS)

    icon.title = f"{APP_NAME} | wakes: {wakes}"


def on_toggle_startup(icon, item):
    if startup_enabled():
        remove_from_startup()
    else:
        add_to_startup()


def on_reset(icon, item):
    STATE.reset()
    update_tray_title(icon)


def on_quit(icon, item):
    icon.stop()


def worker_loop(stop_event: threading.Event, icon: pystray.Icon):
    while not stop_event.is_set():
        now = time.time()
        idle = get_idle_seconds()
        unlocked = is_workstation_unlocked()

        STATE.maybe_sample_history(idle, now)

        if should_intercept(unlocked, idle, IDLE_THRESHOLD_SECONDS):
            jiggle_mouse()
            with STATE.lock:
                STATE.wake_count += 1
                STATE.last_wake_ts = time.time()
                STATE.last_intercept_ts = STATE.last_wake_ts

        update_tray_title(icon)
        stop_event.wait(CHECK_INTERVAL)


def main():
    if not ensure_single_instance():
      return

    # enable startup by default (toggleable)
    add_to_startup()

    ico_path = resource_path(os.path.join("icons", "app.ico"))
    icon_image = Image.open(ico_path)
    icon = pystray.Icon(APP_NAME, icon_image, APP_NAME)

    stop_event = threading.Event()
    t = threading.Thread(target=worker_loop, args=(stop_event, icon), daemon=True)
    t.start()

    def startup_checked(item):
        return startup_enabled()

    menu = pystray.Menu(
        pystray.MenuItem("Show stats", lambda i, it: open_stats_in_browser(), default=True),
        pystray.MenuItem("Reset stats", on_reset),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Run at startup", on_toggle_startup, checked=startup_checked),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )
    icon.menu = menu

    # best-effort: open stats on click (some shells)
    icon.on_clicked = lambda _icon, _item: open_stats_in_browser()

    icon.run()

if __name__ == "__main__":
    main()