/* ===================================================================
   COMMON.JS: code and settings shared by every page.
   Load it first:  <script src="common.js"></script>
   =================================================================== */
const $ = (id) => document.getElementById(id);
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ===================================================================
   COLOR MODE: dark (default) or light. The button at the right end of the top bar switches it and the choice is
   remembered. The colors themselves live in common.css (:root and html[data-mode="light"]).
   =================================================================== */
const MODE = (() => {
  let m = null;
  try { m = localStorage.getItem("rov-mode"); } catch (e) {}
  return m === "light" ? "light" : "dark";
})();
document.documentElement.dataset.mode = MODE;

/* ===================================================================
   THE PAGES (the links in the top bar). To add, rename or reorder a page, edit this list: every page follows.
   href = the file name. Every link opens in a new browser tab; the page you are on is shown but is not a link.
   =================================================================== */
const PAGES = [
  { href: "index.html",    name: "Dashboard",   blurb: "Cameras, controls and live data on one screen." },
  { href: "camera.html",   name: "Camera view", blurb: "One camera large, for a second monitor." },
  { href: "sensors.html",  name: "All sensors", blurb: "Every value the Cube reports." },
  { href: "jetson.html",   name: "Jetson",      blurb: "SSH terminal, Jetson log and more (to be decided)." },
  { href: "settings.html", name: "Settings",    blurb: "Joystick, Cube and calibration settings." },
];
function buildNav() {
  const nav = $("nav"), here = location.pathname.split("/").pop() || "index.html";
  nav.textContent = "";
  PAGES.forEach((p) => {
    if (p.href === here) {
      const s = document.createElement("span"); s.textContent = p.name; s.setAttribute("aria-current", "page"); nav.append(s);
    } else {
      const a = document.createElement("a"); a.href = p.href; a.textContent = p.name; a.target = "_blank"; a.rel = "noopener"; a.title = "Opens in a new tab"; nav.append(a);
    }
  });
  // dark / light button at the right end of the top bar (the page reloads so the canvases pick up the new colors)
  const btn = document.createElement("button"); btn.type = "button"; btn.className = "modebtn";
  btn.setAttribute("role", "switch"); btn.setAttribute("aria-checked", String(MODE === "light"));
  btn.textContent = "Light mode"; btn.title = "Light mode";
  btn.onclick = () => {                      // slide the knob first, then reload once the animation has played
    btn.setAttribute("aria-checked", String(MODE !== "light"));
    try { localStorage.setItem("rov-mode", MODE === "dark" ? "light" : "dark"); } catch (e) {}
    setTimeout(() => location.reload(), reduceMotion ? 0 : 250);
  };
  nav.parentElement.append(btn);
}

/* ===================================================================
   CAMERA SETTINGS
   Edit this list. url = the HTTP address of the stream (for MJPEG the <img> tag can show it directly).
   url: null  = placeholder picture, so the page works with no cameras at all.
   =================================================================== */
const SOURCES = [
  { name: "Camera 1", url: "http://192.168.137.132:8081/stream" },
  { name: "Camera 2", url: "http://192.168.137.132:8082/stream" },
  { name: "Camera 3", url: "http://192.168.137.132:8083/stream" },
  { name: "Camera 4", url: "http://192.168.137.132:8084/stream" },
];
const RETRY_MS = 3000;                       // how often a dead stream is retried
const STORAGE_KEY = "rov.cameraSlots";       // remembers which camera is in which box after a reload

/* ===================================================================
   LOG HELPERS (used by every log box)
   =================================================================== */
const LOG_MAX = 200;
function logLevel(text) {
  const t = text.toLowerCase();
  if (/refused|fail|leak|lost|error|critical|denied/.test(t)) return "err";
  if (/warn|low |prearm|not healthy|timeout|no data/.test(t)) return "warn";
  return "";
}
function makeLogger(box) {                                  // returns a function that adds one line at the top of that log box
  return function add(text) {
    if (box.firstChild && box.firstChild.classList && box.firstChild.classList.contains("none")) box.textContent = "";
    const row = document.createElement("div"); row.className = "row " + logLevel(text);
    const t = document.createElement("span"); t.className = "t"; t.textContent = new Date().toLocaleTimeString([], { hour12: false });
    const m = document.createElement("span"); m.textContent = text;
    row.append(t, m);
    box.prepend(row);
    while (box.children.length > LOG_MAX) box.lastChild.remove();
  };
}

/* ===================================================================
   JETSON STATE NAMES (text and dot colour for each state the backend can report)
   =================================================================== */
const SSH_TEXT = { disconnected: "Disconnected", connecting: "Connecting...", connected: "Connected", failed: "Connection failed" };
const SSH_DOT = { disconnected: "", connecting: "wait", connected: "on", failed: "bad" };
const CAM_TEXT = { unknown: "unknown", stopped: "stopped", starting: "starting...", running: "running", stopping: "stopping...", failed: "failed" };
const CAM_DOT = { unknown: "", stopped: "", starting: "wait", running: "on", stopping: "wait", failed: "bad" };
function setText(el, text) { if (el.textContent !== text) el.textContent = text; }       // only when it changed, so screen readers don't repeat it
