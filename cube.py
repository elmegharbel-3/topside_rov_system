"""cube.py - everything about talking to the Cube (ArduSub), in one file.

This file never opens a port. YOUR file opens the connection and hands it over, so a program that uses
this file still starts fine when no Cube (or no joystick) is plugged in.

HOW TO USE IT IN ANY OTHER FILE
-------------------------------
    from pymavlink import mavutil
    from cube import Cube

    cube = Cube()                                    # works with no hardware at all
    master = mavutil.mavlink_connection("COM8")      # YOU open the connection (see main_example.py)
    cube.attach(master)                              # hand it over, the Cube class does the rest

    data = cube.get_data()                           # a dictionary, safe to call from anywhere, any time
    data["armed"], data["mode"], data["roll"], data["depth"], data["accel_z"] ...
    data["water_temp_c"], data["board_temp_c"]       # Bar30 (water) temperature vs Cube (enclosure) temperature

    cube.set_manual(surge, sway, heave, yaw, roll)   # sticks, each -1 to +1 (heave + = up)
    cube.set_relay(0, True)                          # relay 1 on (relays are 0 to 3)
    cube.arm(); cube.disarm(); cube.toggle_arm()
    cube.set_mode("MANUAL")                          # the real mode shows up in data["mode"]
    for text in cube.pop_events(): print(text)       # refusals, warnings, leak alerts ...

    cube.close()                                     # neutral controls, disarm if armed, stop threads

THREADS
-------
The Cube object runs two threads of its own, so nothing in your program waits for it:
   reader   - the ONLY place that reads from the Cube; it keeps the newest value of every message
   control  - once a second sends the heartbeat, 30 times a second sends your sticks, and does housekeeping
Every method you call returns immediately (True = sent, False = could not send).

DEPTH SOURCES (depth_source in cube_settings.json)
-------------
   ekf     - LOCAL_POSITION_NED.z: the EKF depth ArduSub itself uses for ALT_HOLD. Works without GPS. (default)
   ahrs2   - AHRS2.altitude: the backup (DCM) estimate, close to the raw primary barometer
   vfr_hud - VFR_HUD.alt: needs a global position (GPS / EKF origin), stays 0 on an ROV without one
   bar30   - computed here from the Bar30 pressure; call zero_bar30() at the surface first
"""
import json
import math
import os
import threading
import time

os.environ.setdefault("MAVLINK20", "1")          # MAVLink version 2 (must be set before importing pymavlink)
from pymavlink import mavutil

mavlink = mavutil.mavlink
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cube_settings.json")

# ArduSub mode numbers (from the MAVLink definitions and QGroundControl's source)
MODE_NAMES = {0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED", 7: "CIRCLE",
              9: "SURFACE", 16: "POSHOLD", 19: "MANUAL", 20: "MOTORDETECT", 21: "SURFTRAK"}
MODE_IDS = {name: number for number, name in MODE_NAMES.items()}
MODE_ALIASES = {"DEPTH_HOLD": "ALT_HOLD", "DEPTHHOLD": "ALT_HOLD"}

MG_TO_MS2 = 0.00980665                           # the IMU reports milli-g
NEUTRAL = (0.0, 0.0, 0.0, 0.0, 0.0)              # surge, sway, heave, yaw, roll
DEPTH_SOURCES = ("ekf", "ahrs2", "vfr_hud", "bar30")

DEFAULT_SETTINGS = {
    "control_rate_hz": 30, "neutral_timeout_s": 0.5, "stale_after_s": 2.0, "link_timeout_s": 3.0,
    "mode_confirm_timeout_s": 3.0, "send_roll": True, "sync_relays_on_connect": True,
    "disarm_on_link_loss": True, "depth_source": "ekf", "bar30_message": 3, "water_density": 997,
    "allowed_modes": ["MANUAL", "STABILIZE", "ALT_HOLD", "SURFACE"],
    "telemetry_rates_hz": {"ATTITUDE": 10, "VFR_HUD": 5, "AHRS2": 5, "LOCAL_POSITION_NED": 5,
                           "SCALED_PRESSURE": 1, "SCALED_PRESSURE2": 1, "SCALED_PRESSURE3": 5,
                           "SCALED_IMU": 10, "SCALED_IMU2": 10, "SYS_STATUS": 2, "SERVO_OUTPUT_RAW": 5,
                           "RC_CHANNELS": 5},
}

# Every key that get_data() returns. A value is None when it is unknown or older than stale_after_s.
DATA_KEYS = (
    "attached", "connected", "firmware",
    "armed", "mode", "mode_id", "mode_request",
    "roll", "pitch", "yaw", "heading", "roll_rate", "pitch_rate", "yaw_rate",
    "depth", "depth_ekf", "depth_vfr", "depth_ahrs", "depth_bar30", "climb",
    "pressure_hpa", "pressure_temp_c", "water_temp_c", "board_temp_c",
    "press1_hpa", "press1_temp_c", "press2_hpa", "press2_temp_c", "press3_hpa", "press3_temp_c",
    "accel_x", "accel_y", "accel_z",
    "battery_v", "battery_a", "battery_pct",
    "motors", "rc", "relays", "leak",
)


# ======================================================================================
# PART 1: the settings file
# ======================================================================================
def load_settings(path=SETTINGS_FILE):
    """Read cube_settings.json. Anything missing from the file uses the default."""
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Settings file not found: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"The settings file is not valid JSON: {e} (check for a missing comma or quote)")

    settings = dict(DEFAULT_SETTINGS)
    settings.update({k: v for k, v in user.items() if not k.startswith("_")})
    rates = dict(DEFAULT_SETTINGS["telemetry_rates_hz"])
    rates.update(user.get("telemetry_rates_hz", {}))
    settings["telemetry_rates_hz"] = rates

    if settings["depth_source"] not in DEPTH_SOURCES:
        raise ValueError(f"depth_source must be one of: {', '.join(DEPTH_SOURCES)}")
    if settings["bar30_message"] not in (1, 2, 3):
        raise ValueError("bar30_message must be 1, 2 or 3 (SCALED_PRESSURE, SCALED_PRESSURE2, SCALED_PRESSURE3)")
    for mode in settings["allowed_modes"]:
        if mode.upper() not in MODE_IDS:
            raise ValueError(f"allowed_modes: unknown mode '{mode}'. Known: {', '.join(MODE_IDS)}")
    for name in rates:
        if getattr(mavlink, "MAVLINK_MSG_ID_" + name, None) is None:
            raise ValueError(f"telemetry_rates_hz: unknown message '{name}'")
    return settings


def result_name(result):
    try:
        return mavlink.enums["MAV_RESULT"][result].name.replace("MAV_RESULT_", "")
    except KeyError:
        return f"RESULT_{result}"


# ======================================================================================
# PART 2: the Cube class
# ======================================================================================
class Cube:
    def __init__(self, settings_file=SETTINGS_FILE):
        self._s = load_settings(settings_file)
        self._lock = threading.RLock()           # protects everything below
        self._send_lock = threading.Lock()       # only one thread writes to the Cube at a time

        # ---- the connection (None until attach() is called)
        self._master = None
        self._autopilot = None                   # (system id, component id) once the Cube has been found

        # ---- what we know about the Cube (only the reader thread writes this)
        self._msgs = {}                          # message type -> (newest message, time it arrived)
        self._armed = None
        self._mode_id = None
        self._last_heartbeat = 0.0
        self._fw = None
        self._leak = False
        self._zero_pressure = None

        # ---- what we asked the Cube to do
        self._mode_request = None                # (name, mode number, time asked)
        self._relay_sent = [None] * 4            # the last state we SENT for each relay
        self._pending = {}                       # command number -> description (to explain refusals)
        self._events = []                        # text messages for you, read with pop_events()
        self._event_times = {}

        # ---- the sticks
        self._manual = NEUTRAL
        self._manual_time = 0.0
        self._control_enabled = False            # the stream starts with the first set_manual() call

        # ---- housekeeping
        self._need_setup = False
        self._last_rerequest = 0.0
        self._link_was_ok = False                # was the link fine at the last check?
        self._in_loss = False                    # True from "link lost" until "link restored"
        self._last_loss_disarm = 0.0
        self._closed = False
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._reader_loop, name="cube-reader", daemon=True),
                         threading.Thread(target=self._control_loop, name="cube-control", daemon=True)]
        for t in self._threads:
            t.start()

    # ---------------------------------------------------------------- connection
    def attach(self, master):
        """Give the Cube an open pymavlink connection. It finds the autopilot by itself."""
        with self._lock:
            self._master = master
            self._autopilot = None
            self._msgs = {}
            self._armed = None
            self._mode_id = None
            self._last_heartbeat = 0.0
            self._mode_request = None
            self._relay_sent = [None] * 4
            self._need_setup = False
            self._link_was_ok = False
            self._in_loss = False
            self._fw = None

    def detach(self):
        """Forget the connection (you close it yourself). The Cube object keeps working, reporting 'not connected'."""
        with self._lock:
            self._master = None
            self._autopilot = None
            self._msgs = {}
            self._last_heartbeat = 0.0
            self._link_was_ok = False
            self._in_loss = False

    def close(self):
        """Neutral controls, disarm if armed, stop the threads. Call this when your program ends."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._autopilot is not None:
                if self._control_enabled:
                    for _ in range(3):
                        self._send_manual_control(NEUTRAL)
                if self._armed:
                    self._command(mavlink.MAV_CMD_COMPONENT_ARM_DISARM, (0,) * 7, "disarm")
                time.sleep(0.2)                  # give the packets time to leave
        finally:
            self._stop.set()
            for t in self._threads:
                t.join(timeout=1.5)

    # ---------------------------------------------------------------- reading data
    def get_data(self):
        """A snapshot of everything we know, as a dictionary with ALWAYS the same keys (see DATA_KEYS).

        A value is None when it is unknown or hasn't been updated for stale_after_s seconds.
        If the Cube isn't connected, everything except attached/connected/leak/relays/mode_request is None.
        Units: degrees, deg/s, metres (depth positive = deeper), m/s, m/s2 (includes gravity), hPa, C, V, A, %.
        """
        s = self._s
        with self._lock:
            now = time.monotonic()
            connected = self._is_connected(now)

            def fresh(kind):
                entry = self._msgs.get(kind)
                return entry[0] if entry is not None and now - entry[1] < s["stale_after_s"] else None

            d = dict.fromkeys(DATA_KEYS)
            d["attached"] = self._master is not None
            d["connected"] = connected
            d["leak"] = self._leak
            d["relays"] = list(self._relay_sent)
            d["mode_request"] = self._mode_request[0] if self._mode_request else None
            d["firmware"] = self._fw
            if not connected:
                return d

            d["armed"] = self._armed
            d["mode_id"] = self._mode_id
            d["mode"] = MODE_NAMES.get(self._mode_id, f"UNKNOWN({self._mode_id})")

            att = fresh("ATTITUDE")
            if att is not None:
                d["roll"], d["pitch"] = math.degrees(att.roll), math.degrees(att.pitch)
                d["yaw"] = math.degrees(att.yaw) % 360
                d["roll_rate"], d["pitch_rate"] = math.degrees(att.rollspeed), math.degrees(att.pitchspeed)
                d["yaw_rate"] = math.degrees(att.yawspeed)

            vfr = fresh("VFR_HUD")
            if vfr is not None:
                d["heading"] = vfr.heading
                d["depth_vfr"] = -vfr.alt            # needs a global position: stays 0 without GPS / EKF origin
                d["climb"] = vfr.climb
            ahrs = fresh("AHRS2")
            if ahrs is not None:
                d["depth_ahrs"] = -ahrs.altitude
            lpos = fresh("LOCAL_POSITION_NED")
            if lpos is not None:
                d["depth_ekf"] = lpos.z              # NED: z positive = down = deeper

            presses = {1: fresh("SCALED_PRESSURE"), 2: fresh("SCALED_PRESSURE2"), 3: fresh("SCALED_PRESSURE3")}
            for n, p in presses.items():
                if p is not None:
                    d[f"press{n}_hpa"], d[f"press{n}_temp_c"] = p.press_abs, p.temperature / 100
            board = presses[1] or presses[2]         # the internal baros: temperature inside the enclosure
            if board is not None:
                d["board_temp_c"] = board.temperature / 100
            chosen = presses[s["bar30_message"]]     # the external Bar30 (message 3 on a Cube Orange)
            if chosen is not None:
                d["pressure_hpa"], d["pressure_temp_c"] = chosen.press_abs, chosen.temperature / 100
                d["water_temp_c"] = chosen.temperature / 100
                if self._zero_pressure is not None:
                    d["depth_bar30"] = (chosen.press_abs - self._zero_pressure) * 100 / (s["water_density"] * 9.80665)
            d["depth"] = {"ekf": d["depth_ekf"], "vfr_hud": d["depth_vfr"], "ahrs2": d["depth_ahrs"],
                          "bar30": d["depth_bar30"]}[s["depth_source"]]

            imu = fresh("SCALED_IMU")
            if imu is None:
                imu = fresh("SCALED_IMU2")
            if imu is not None:
                d["accel_x"], d["accel_y"], d["accel_z"] = (imu.xacc * MG_TO_MS2, imu.yacc * MG_TO_MS2,
                                                              imu.zacc * MG_TO_MS2)

            sysst = fresh("SYS_STATUS")
            if sysst is not None:
                if sysst.voltage_battery != 0xFFFF:
                    d["battery_v"] = sysst.voltage_battery / 1000
                if sysst.current_battery != -1:
                    d["battery_a"] = sysst.current_battery / 100
                if sysst.battery_remaining != -1:
                    d["battery_pct"] = sysst.battery_remaining

            servo = fresh("SERVO_OUTPUT_RAW")
            if servo is not None:
                d["motors"] = [servo.servo1_raw, servo.servo2_raw, servo.servo3_raw, servo.servo4_raw,
                               servo.servo5_raw, servo.servo6_raw, servo.servo7_raw, servo.servo8_raw]
            rc = fresh("RC_CHANNELS")
            if rc is not None:
                d["rc"] = [rc.chan1_raw, rc.chan2_raw, rc.chan3_raw, rc.chan4_raw, rc.chan5_raw, rc.chan6_raw]
            return d

    def pop_events(self):
        """New text messages since the last call (refusals, warnings from the Cube, leak alerts ...). Clears the list."""
        with self._lock:
            events, self._events = self._events, []
        return events

    def zero_bar30(self):
        """Use the current pressure as 'zero depth' for depth_bar30. Call it at the surface."""
        with self._lock:
            entry = self._msgs.get(("SCALED_PRESSURE", "SCALED_PRESSURE2", "SCALED_PRESSURE3")[self._s["bar30_message"] - 1])
            if entry is None or time.monotonic() - entry[1] > self._s["stale_after_s"]:
                self._event("zero_bar30: no pressure data yet")
                return False
            self._zero_pressure = entry[0].press_abs
        return True

    def clear_leak(self):
        """Reset the leak flag after you have dealt with the alert."""
        with self._lock:
            self._leak = False

    # ---------------------------------------------------------------- commands (all return at once)
    def set_manual(self, surge=0.0, sway=0.0, heave=0.0, yaw=0.0, roll=0.0):
        """Your sticks, each -1 to +1 (heave + = up). Call it often (e.g. 50x per second).
        If you stop calling it for neutral_timeout_s, the Cube gets neutral controls instead.
        The first call starts the control stream. Returns True if a Cube has been found."""
        if self._closed:                          # close() has started: from now on only neutral is sent
            return False
        values = tuple(max(-1.0, min(1.0, float(v))) for v in (surge, sway, heave, yaw, roll))
        with self._lock:
            self._manual = values
            self._manual_time = time.monotonic()
            self._control_enabled = True
            return self._autopilot is not None

    def stop_control(self):
        """Stop sending controls altogether (e.g. to let another program control the Cube)."""
        with self._lock:
            was_enabled, self._control_enabled = self._control_enabled, False
            self._manual = NEUTRAL
        if was_enabled and self._autopilot is not None:
            self._send_manual_control(NEUTRAL)

    def arm(self):
        return self._command(mavlink.MAV_CMD_COMPONENT_ARM_DISARM, (1, 0, 0, 0, 0, 0, 0), "arm")

    def disarm(self):
        return self._command(mavlink.MAV_CMD_COMPONENT_ARM_DISARM, (0, 0, 0, 0, 0, 0, 0), "disarm")

    def toggle_arm(self):
        """Arm if the Cube is disarmed, disarm if it is armed (decided by the LIVE state)."""
        armed = self.get_data()["armed"]
        if armed is None:
            self._event("toggle_arm: the Cube is not connected")
            return False
        return self.disarm() if armed else self.arm()

    def set_mode(self, name):
        """Ask for a mode (only the modes listed in allowed_modes). The REAL mode is in get_data()['mode'];
        get_data()['mode_request'] shows the pending request until the Cube confirms it."""
        key = str(name).strip().upper().replace(" ", "_")
        key = MODE_ALIASES.get(key, key)
        if key not in [m.upper() for m in self._s["allowed_modes"]]:
            self._event(f"mode '{name}' is not allowed (allowed: {', '.join(self._s['allowed_modes'])})")
            return False
        mode_number = MODE_IDS[key]
        sent = self._command(mavlink.MAV_CMD_DO_SET_MODE,
                             (mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, mode_number, 0, 0, 0, 0, 0),
                             f"mode change to {key}")
        if sent:
            with self._lock:
                self._mode_request = (key, mode_number, time.monotonic())
        return sent

    def set_relay(self, index, on):
        """Relay 0 to 3. Say what you WANT: a command is sent only when it differs from what was sent last,
        so it is fine to call this 50 times a second with the wanted state."""
        if index not in (0, 1, 2, 3):
            self._event(f"set_relay: relay {index} doesn't exist (use 0 to 3)")
            return False
        on = bool(on)
        with self._lock:
            if self._relay_sent[index] == on:
                return True
            if self._autopilot is None:
                return False                      # no Cube yet: stay quiet (callers keep asking every loop)
        if self._send_relay(index, on):
            with self._lock:
                self._relay_sent[index] = on
            return True
        return False

    # ---------------------------------------------------------------- internals: sending
    def _event(self, text, throttle=0.0):
        with self._lock:
            now = time.monotonic()
            if throttle and now - self._event_times.get(text, -1e9) < throttle:
                return
            self._event_times[text] = now
            self._events.append(text)
            del self._events[:-200]

    def _is_connected(self, now=None):
        now = time.monotonic() if now is None else now
        return (self._master is not None and self._autopilot is not None
                and now - self._last_heartbeat < self._s["link_timeout_s"])

    def _send(self, function):
        """Run function(master) with the send lock held. Returns True if it was sent."""
        with self._lock:
            master = self._master
        if master is None:
            return False
        try:
            with self._send_lock:
                function(master)
            return True
        except Exception as e:
            self._event(f"sending failed: {type(e).__name__}: {e}", throttle=5.0)
            return False

    def _command(self, command, params, description):
        with self._lock:
            ap = self._autopilot
            if ap is None:
                self._event(f"{description}: not sent, no Cube found yet", throttle=2.0)
                return False
            self._pending[command] = description
        return self._send(lambda m: m.mav.command_long_send(ap[0], ap[1], command, 0, *params))

    def _send_relay(self, index, on):
        return self._command(mavlink.MAV_CMD_DO_SET_RELAY, (index, 1 if on else 0, 0, 0, 0, 0, 0), "relay command")

    def _send_manual_control(self, values):
        with self._lock:
            ap = self._autopilot
        if ap is None:
            return False
        surge, sway, heave, yaw, roll = values
        x, y, z, r = round(surge * 1000), round(sway * 1000), round(500 + heave * 500), round(yaw * 1000)
        if self._s["send_roll"]:
            return self._send(lambda m: m.mav.manual_control_send(ap[0], x, y, z, r, 0,
                                                                  enabled_extensions=2, s=0, t=round(roll * 1000)))
        return self._send(lambda m: m.mav.manual_control_send(ap[0], x, y, z, r, 0))

    def _request_telemetry(self):
        for name, hz in self._s["telemetry_rates_hz"].items():
            if hz > 0:
                msg_id = getattr(mavlink, "MAVLINK_MSG_ID_" + name)
                self._command(mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, (msg_id, int(1_000_000 / hz), 0, 0, 0, 0, 0),
                              "telemetry request")

    # ---------------------------------------------------------------- the control thread
    def _control_loop(self):
        period = 1.0 / self._s["control_rate_hz"]
        next_tick = time.monotonic()
        last_heartbeat_sent = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                attached = self._master is not None
            if attached:
                try:
                    if now - last_heartbeat_sent >= 1.0:          # tell the Cube a ground station is here
                        self._send(lambda m: m.mav.heartbeat_send(mavlink.MAV_TYPE_GCS, mavlink.MAV_AUTOPILOT_INVALID, 0, 0, 0))
                        last_heartbeat_sent = now
                    self._housekeeping(now)
                    self._stream_controls(now)
                except Exception as e:
                    self._event(f"control thread error: {type(e).__name__}: {e}", throttle=5.0)
            next_tick += period
            delay = next_tick - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_tick = time.monotonic()

    def _stream_controls(self, now):
        with self._lock:
            enabled = self._control_enabled
            values = self._manual
            old = now - self._manual_time > self._s["neutral_timeout_s"]
            connected = self._is_connected(now)
        if not enabled or self._autopilot is None:
            return
        if old or not connected or self._closed:
            values = NEUTRAL                      # nobody is steering, the link is down, or we are closing
        self._send_manual_control(values)

    def _housekeeping(self, now):
        s = self._s
        with self._lock:
            ap, need_setup = self._autopilot, self._need_setup
        if ap is None:
            return

        # ---- first contact: ask for data, firmware version, and switch all relays off
        if need_setup:
            with self._lock:
                self._need_setup = False
                self._last_rerequest = now
            self._request_telemetry()
            self._command(mavlink.MAV_CMD_REQUEST_MESSAGE, (mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION, 0, 0, 0, 0, 0, 0),
                          "firmware request")
            if s["sync_relays_on_connect"]:
                for i in range(4):
                    self._send_relay(i, False)
                with self._lock:
                    self._relay_sent = [False] * 4
        elif now - self._last_rerequest > 10.0:          # the Cube forgets these after a reboot, so repeat
            with self._lock:
                self._last_rerequest = now
            self._request_telemetry()

        # ---- link lost / restored
        with self._lock:
            connected = self._is_connected(now)
            if connected and not self._link_was_ok:
                self._link_was_ok = True
                if self._in_loss:                         # the link is back after a loss
                    self._in_loss = False
                    self._event("Link restored")
                    self._relay_sent = [None] * 4         # the Cube may have rebooted: let set_relay() resend
            elif not connected and self._link_was_ok:
                self._link_was_ok = False
                self._in_loss = True
                self._last_loss_disarm = -1e9             # so the first disarm goes out immediately
                self._event(f"Link lost (no heartbeat for {s['link_timeout_s']:g} s): controls set to neutral"
                            + (", disarming" if s["disarm_on_link_loss"] else ""))
            in_loss = self._in_loss
        if in_loss and s["disarm_on_link_loss"] and now - self._last_loss_disarm > 2.0:
            self._last_loss_disarm = now                  # repeat every 2 s while the link is down
            self._command(mavlink.MAV_CMD_COMPONENT_ARM_DISARM, (0, 0, 0, 0, 0, 0, 0), "disarm (link lost)")

        # ---- a mode request the Cube never confirmed
        with self._lock:
            req = self._mode_request
            if req is not None and now - req[2] > s["mode_confirm_timeout_s"]:
                self._mode_request = None
                self._event(f"mode change to {req[0]} not confirmed after {s['mode_confirm_timeout_s']:g} s")

    # ---------------------------------------------------------------- the reader thread
    def _reader_loop(self):
        while not self._stop.is_set():
            with self._lock:
                master = self._master
            if master is None:
                self._stop.wait(0.1)
                continue
            try:
                msg = master.recv_match(blocking=True, timeout=0.5)
            except Exception as e:
                self._event(f"reading failed: {type(e).__name__}: {e}", throttle=5.0)
                self._stop.wait(0.5)
                continue
            if msg is None:
                continue
            try:
                self._handle(msg)
            except Exception as e:
                self._event(f"error handling {msg.get_type()}: {type(e).__name__}: {e}", throttle=5.0)

    def _handle(self, msg):
        kind = msg.get_type()
        if kind == "BAD_DATA":
            return
        source = (msg.get_srcSystem(), msg.get_srcComponent())
        with self._lock:
            if kind == "HEARTBEAT":
                if msg.type == mavlink.MAV_TYPE_GCS:
                    return
                if self._autopilot is None:
                    if msg.autopilot == mavlink.MAV_AUTOPILOT_INVALID:
                        return                          # some other device (gimbal, companion ...)
                    self._autopilot = source
                    self._need_setup = True
                    self._event(f"Cube found (system {source[0]}, component {source[1]})")
                elif source != self._autopilot:
                    return                              # only the autopilot's heartbeat counts
                self._armed = bool(msg.base_mode & mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                self._mode_id = msg.custom_mode
                self._last_heartbeat = time.monotonic()
                if self._mode_request is not None and self._mode_request[1] == msg.custom_mode:
                    self._mode_request = None           # the Cube really is in the requested mode now
                return

            if self._autopilot is None or source != self._autopilot:
                return                                  # ignore every other sender

            if kind == "COMMAND_ACK":
                description = self._pending.get(msg.command, f"command {msg.command}")
                if msg.result not in (mavlink.MAV_RESULT_ACCEPTED, mavlink.MAV_RESULT_IN_PROGRESS):
                    self._event(f"{description} refused: {result_name(msg.result)}",
                                throttle=60.0 if description == "telemetry request" else 0.0)
            elif kind == "STATUSTEXT":
                text = msg.text
                self._event(f"Cube: {text}")
                if "leak detected" in text.lower():
                    self._leak = True
                    self._event("LEAK DETECTED")
            elif kind == "AUTOPILOT_VERSION":
                v = msg.flight_sw_version
                self._fw = f"{(v >> 24) & 0xFF}.{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}"
            elif kind in self._s["telemetry_rates_hz"]:
                self._msgs[kind] = (msg, time.monotonic())
