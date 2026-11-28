/* ===================================================================
   BACKEND.JS: the backend every page talks to. Load it after common.js:
     <script src="common.js"></script><script src="backend.js"></script>
   Today this is a MOCK (fake data made in the browser, so each open page has its own fake state).
   When app.py exists, this file is replaced by a real backend class with the same functions: it asks the Python
   process (live stream + POST commands), so every page sees the same Cube, joystick and Jetson.
   =================================================================== */
/* ===================================================================
   THE BACKEND (mock for now)
   The page only talks to `backend`. get_data() returns the same keys as Cube.get_data() in cube.py
   (only the ones used so far); the real backend will replace this class and nothing else changes.
     get_data() -> { connected, armed, mode, mode_request, roll, pitch, yaw, depth, water_temp_c, jetson_cpu_c, jetson_gpu_c, sent, joystick_enabled, motors }   (null when there is no link or the data is stale)
                    roll and pitch in degrees (roll + = right side down, pitch + = nose up), yaw 0..360 like Cube.get_data(), depth in metres (positive = deeper)
                    water_temp_c comes from the Cube; jetson_cpu_c / jetson_gpu_c come from a separate Jetson service (null = not read)
                    sent = [surge, sway, heave, yaw, roll], each -1..1, the values being sent to the Cube (all 0 while the joystick is unplugged or disabled; null = no backend)
                    joystick_connected = true / false (the joystick is plugged in or not; null = unknown). Unplugged while armed -> the backend disarms and sends zeros
                    joystick_enabled = false: sticks AND joystick buttons (arm, relays) are ignored, zeros are sent
                    motors = [M1..M6] PWM in microseconds (1100..1900, 1500 = stopped) from SERVO_OUTPUT_RAW, null = no data
                    pop_events() -> [text, ...]  new messages since the last call (refusals, Cube warnings, leak alerts); the page adds the time
                    jetson = { ssh, cameras }   ssh: "disconnected" | "connecting" | "connected" | "failed"
                                                cameras: "unknown" | "stopped" | "starting" | "running" | "stopping" | "failed"   ("unknown" = not connected, or not checked yet)
     jetson_pop_events() -> [text, ...]   new Jetson messages (SSH, the camera script and its output), like pop_events()
     jetson_connect(), jetson_disconnect(), jetson_start_cameras(), jetson_stop_cameras()    return at once; the REAL state comes back with get_data().jetson
     set_joystick_enabled(on)    same as arm(): returns at once, the real state comes back with get_data()
     arm(), disarm(), set_mode(name)    all return at once; the REAL state comes back with get_data()
   =================================================================== */
const MODES = ["MANUAL", "STABILIZE", "ALT_HOLD", "SURFACE"];      // = allowed_modes in cube_settings.json
/* ===================================================================
   THE REAL BACKEND: talks to app.py. Same functions as MockBackend, so no page changes.
   Live data arrives as a stream (Server-Sent Events) and is kept here, so get_data() answers at once.
   Commands are POSTs; they return at once and the REAL state comes back through the stream, like with the mock.
   =================================================================== */
const NO_DATA = { connected: false, armed: null, mode: null, mode_request: null, roll: null, pitch: null, yaw: null, depth: null,depth_ekf: null,board_temp_c: null,
                  water_temp_c: null, jetson_cpu_c: null, jetson_gpu_c: null, sent: null, joystick_enabled: null, joystick_connected: null,
                  motors: null, jetson: { ssh: "disconnected", cameras: "unknown" }, jetson_camera_urls: null };
class RemoteBackend {
  constructor() {
    this.d = null; this.events = []; this.jEvents = []; this.up = false; this.wasUp = false;
    const es = new EventSource("/api/stream");
    es.onmessage = (m) => {
      const p = JSON.parse(m.data);
      this.d = p.d;
      if (!this.up && this.wasUp) this.events.push("Connection to app.py restored");
      this.up = this.wasUp = true;
      (p.ev || []).forEach((e) => this.events.push(e[1]));
      (p.jev || []).forEach((e) => this.jEvents.push(e[1]));
    };
    es.onerror = () => {                                              // the browser retries by itself every second
      if (!this.up) return;
      this.up = false; this.d = null;
      this.events.push("Lost connection to app.py (is it still running?)");
    };
  }
  get_data() { return this.d || NO_DATA; }
  pop_events() { const e = this.events; this.events = []; return e; }
  jetson_pop_events() { const e = this.jEvents; this.jEvents = []; return e; }
  _post(path, body) {
    fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
      .catch(() => this.events.push("Could not reach app.py: the command was not sent"));
    return true;
  }
  arm() { return this._post("/api/arm"); }
  disarm() { return this._post("/api/disarm"); }
  set_mode(name) { return this._post("/api/mode", { name }); }
  set_joystick_enabled(on) { return this._post("/api/joystick", { enabled: on }); }
  jetson_connect() { return this._post("/api/jetson/connect"); }
  jetson_disconnect() { return this._post("/api/jetson/disconnect"); }
  jetson_start_cameras() { return this._post("/api/jetson/start_cameras"); }
  jetson_stop_cameras() { return this._post("/api/jetson/stop_cameras"); }
  jetson_refresh_temperatures() { return this._post("/api/jetson/refresh_temperatures"); }
  jetson_set_fan_speed(percent) { return this._post("/api/jetson/fan", { percent }); }
  jetson_set_fan_mode(mode, percent) { return this._post("/api/jetson/fan/mode", { mode, percent }); }
}

/* Which one? Opened from the file system (double-click) or with ?mock in the address -> the mock. Served by app.py -> the real one. */
const backend = new RemoteBackend();

