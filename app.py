"""app.py - the one program behind the dashboard.

It runs, in ONE process:
   - the Cube (cube.py) and its connection, kept alive by itself (plug / unplug as often as you like)
   - the joystick (joy_main.py) -> Cube, with the safety rule below
   - the Jetson over SSH (connect, start / stop the camera script, read its temperatures)
   - a small web server: the dashboard pages, a live stream of the data, and the buttons' commands

Run:    python app.py                 then open  http://localhost:5000  (other computers: http://<this PC's IP>:5000)
Stop:   Ctrl+C                        (neutral controls are sent and the Cube is disarmed on the way out)
Needs in this folder:  cube.py, cube_settings.json, joy_main.py, joystick_settings.json, app_settings.json, static/

SAFETY RULE: while the joystick is not plugged in, zeros are sent AND the Cube is disarmed (checked every second, so
arming from the dashboard without a joystick is undone too). "Disable joystick" in the dashboard sends zeros but does NOT
disarm. For bench tests without a joystick set  "disarm_without_joystick": false  in app_settings.json.

HOW THE PAGES TALK TO IT
   GET  /api/stream           live data (Server-Sent Events, 10 times a second) + new log lines
   POST /api/arm  /api/disarm  /api/mode {"name": "MANUAL"}  /api/joystick {"enabled": true}
   POST /api/jetson/connect | disconnect | start_cameras | stop_cameras | refresh_temperatures
   POST /api/jetson/fan {"percent": 0..100}
"""
import json
import os
import shlex
import signal
import socket
import sys
import threading
import time

from flask import Flask, Response, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
APP_SETTINGS_FILE = os.path.join(HERE, "app_settings.json")
STATIC_DIR = os.path.join(HERE, "static")

DEFAULTS = {
    "cube": {"connection": "COM8", "baud": 115200, "retry_s": 2, "lost_s": 5},
    "joystick": {"rate_hz": 50, "disarm_without_joystick": True},
    "web": {"host": "0.0.0.0", "port": 5000},
    "jetson": {"host": "192.168.137.132", "port": 22, "user": "jet", "password": "jet", "key_file": "",
               "start_cmd": "", "stop_cmd": "", "check_cmd": "", "temps_cmd": "", "fan_cmd": "",
               "camera_ports": [8081, 8082, 8083, 8084], "camera_stream_path": "/stream",
               "auto_connect": True, "auto_start_cameras": True, "auto_retry_s": 3,
               "poll_s": 1, "connect_timeout_s": 6},
}


# ======================================================================================
# PART 1: settings
# ======================================================================================
def load_app_settings(path=APP_SETTINGS_FILE):
    """Read app_settings.json. Anything missing uses the default."""
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Settings file not found: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"app_settings.json is not valid JSON: {e} (check for a missing comma or quote)")
    settings = {}
    for section, values in DEFAULTS.items():
        merged = dict(values)
        merged.update({k: v for k, v in user.get(section, {}).items() if not k.startswith("_")})
        settings[section] = merged
    return settings


# ======================================================================================
# PART 2: a log that every open page can read from its own position
# (the Cube class's pop_events() gives each line to ONE reader; with several pages open they would steal from each other)
# ======================================================================================
class EventLog:
    def __init__(self, keep=500):
        self._lock = threading.Lock()
        self._items = []                 # [(id, text)]
        self._next = 1
        self._keep = keep

    def add(self, text):
        with self._lock:
            self._items.append((self._next, str(text)))
            self._next += 1
            del self._items[:-self._keep]

    def last_id(self):
        with self._lock:
            return self._next - 1

    def since(self, last_seen):
        """(the lines newer than last_seen, the new last_seen)"""
        with self._lock:
            new = [[i, t] for i, t in self._items if i > last_seen]
            return new, (new[-1][0] if new else last_seen)


# ======================================================================================
# PART 3: the Jetson over SSH
# ======================================================================================
class JetsonLink:
    """States:  ssh: disconnected | connecting | connected | failed
               cameras: unknown | stopped | starting | running | stopping | failed
    Every method returns at once (True = started). The real state is in state()."""

    def __init__(self, settings, log):
        self._settings = settings
        self._log = log
        self._lock = threading.RLock()
        self._client = None
        self._generation = 0             # changes at every connect / disconnect, so old threads know to stop
        self._ssh = "disconnected"
        self._cameras = "unknown"
        self._fan_mode = "auto"
        self._cpu = None
        self._gpu = None
        self._auto_stop = threading.Event()
        self._auto_thread = None
        self._manual_disconnect = False

    # ------------------------------------------------------------ what the page reads
    def state(self):
        with self._lock:
            return {"ssh": self._ssh, "cameras": self._cameras, "fan_mode": self._fan_mode}, self._cpu, self._gpu

    def camera_urls(self):
        host = self._settings["host"]
        path = self._settings.get("camera_stream_path", "/stream")
        return [
            f"http://{host}:{int(port)}{path}"
            for port in self._settings.get("camera_ports", [])
        ]

    def start_auto_connect(self):
        if not self._settings.get("auto_connect", True):
            return
        if self._auto_thread is not None and self._auto_thread.is_alive():
            return
        self._auto_stop.clear()
        self._auto_thread = threading.Thread(
            target=self._auto_connect_loop, name="jetson-auto-connect", daemon=True
        )
        self._auto_thread.start()

    def stop_auto_connect(self):
        self._auto_stop.set()
        if self._auto_thread is not None:
            self._auto_thread.join(timeout=1.0)

    def _auto_connect_loop(self):
        retry_s = max(1.0, float(self._settings.get("auto_retry_s", 3)))
        while not self._auto_stop.is_set():
            try:
                with socket.create_connection(
                    (self._settings["host"], int(self._settings["port"])), timeout=1.0
                ):
                    reachable = True
            except OSError:
                reachable = False

            if not reachable:
                with self._lock:
                    self._manual_disconnect = False
            else:
                with self._lock:
                    should_connect = (
                        not self._manual_disconnect
                        and self._ssh not in ("connecting", "connected")
                    )
                if should_connect:
                    self.connect(automatic=True)
            self._auto_stop.wait(retry_s)

    # ------------------------------------------------------------ connect / disconnect
    def connect(self, automatic=False):
        with self._lock:
            if self._ssh in ("connecting", "connected"):
                return False
            if not automatic:
                self._manual_disconnect = False
            self._ssh, self._cameras = "connecting", "unknown"
            self._cpu = self._gpu = None
            self._generation += 1
            gen = self._generation
        s = self._settings
        self._log.add(f"SSH: connecting to {s['user']}@{s['host']} ...")
        threading.Thread(target=self._connect_thread, args=(gen,), name="jetson-connect", daemon=True).start()
        return True

    def disconnect(self, manual=True):
        with self._lock:
            if self._ssh == "disconnected":
                if manual:
                    self._manual_disconnect = True
                return False
            if manual:
                self._manual_disconnect = True
            self._generation += 1
            client, self._client = self._client, None
            self._ssh, self._cameras = "disconnected", "unknown"
            self._cpu = self._gpu = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        self._log.add("SSH: disconnected")
        return True

    def _connect_thread(self, gen):
        s = self._settings
        try:
            import paramiko
            client = paramiko.SSHClient()
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())     # first contact: trust the Jetson
            client.connect(s["host"], port=int(s["port"]), username=s["user"],
                           password=s["password"] or None, key_filename=s["key_file"] or None,
                           look_for_keys=not s["password"], allow_agent=not s["password"],
                           timeout=s["connect_timeout_s"], banner_timeout=s["connect_timeout_s"],
                           auth_timeout=s["connect_timeout_s"])
            client.get_transport().set_keepalive(10)
        except Exception as e:
            with self._lock:
                if gen != self._generation:
                    return
                self._ssh, self._cameras = "failed", "unknown"
            self._log.add(f"SSH: connection failed: {type(e).__name__}: {e}")
            return
        with self._lock:
            if gen != self._generation:                  # the user pressed disconnect meanwhile
                client.close()
                return
            self._client, self._ssh = client, "connected"
        self._log.add("SSH: connected")
        try:
            self._check_cameras(announce=True)
        except Exception as e:
            self._lost(gen, f"{type(e).__name__}: {e}")
            return
        threading.Thread(target=self._monitor_thread, args=(gen,), name="jetson-monitor", daemon=True).start()
        self.set_fan_mode("auto")
        self.refresh_temperatures()
        with self._lock:
            should_start_cameras = (
                self._settings.get("auto_start_cameras", True)
                and self._cameras == "stopped"
            )
        if should_start_cameras:
            self.start_cameras()

    # ------------------------------------------------------------ running commands
    def _run(self, command, timeout=10):
        """(exit code, text) of one command on the Jetson. Raises if the link is down."""
        with self._lock:
            client = self._client
        if client is None:
            raise ConnectionError("not connected")
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        code = stdout.channel.recv_exit_status()
        return code, (out + err).strip()

    def _lost(self, gen, why):
        with self._lock:
            if gen != self._generation or self._ssh != "connected":
                return
            self._generation += 1
            client, self._client = self._client, None
            self._ssh, self._cameras = "failed", "unknown"
            self._cpu = self._gpu = None
        try:
            client.close()
        except Exception:
            pass
        self._log.add(f"SSH: connection lost ({why})")

    def _check_cameras(self, announce=False):
        """Ask the Jetson whether the camera script is running."""
        cmd = self._settings["check_cmd"]
        if not cmd:
            return
        code, _ = self._run(cmd, timeout=6)
        running = code == 0
        with self._lock:
            if self._cameras in ("starting", "stopping"):
                return
            before, self._cameras = self._cameras, "running" if running else "stopped"
        if announce:
            self._log.add("Cameras: the stream script is already running" if running else "Cameras: the stream script is not running")
        elif before == "running" and not running:
            self._log.add("Cameras: the stream script has stopped")
        elif before in ("stopped", "unknown", "failed") and running:
            self._log.add("Cameras: the stream script is running")

    def _read_temps(self):
        cmd = self._settings["temps_cmd"]
        if not cmd:
            return
        code, text = self._run(cmd, timeout=6)
        if code != 0:
            with self._lock:
                self._cpu = self._gpu = None
            self._log.add(f"Temperature command exited with code {code}: {text}")
            return
        cpu = gpu = None
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].lstrip("-").isdigit():
                value = int(parts[-1]) / 1000.0
                name = " ".join(parts[:-1]).upper()
                if "CPU" in name and cpu is None:
                    cpu = value
                elif "GPU" in name and gpu is None:
                    gpu = value
        with self._lock:
            self._cpu, self._gpu = cpu, gpu

    def _monitor_thread(self, gen):
        while True:
            time.sleep(self._settings["poll_s"])
            with self._lock:
                if gen != self._generation:
                    return
            try:
                self._check_cameras()
                self._read_temps()
            except Exception as e:
                self._lost(gen, f"{type(e).__name__}: {e}")
                return

    # ------------------------------------------------------------ the camera script
    def start_cameras(self):
        with self._lock:
            if self._ssh != "connected" or self._cameras in ("running", "starting", "stopping"):
                return False
            if not self._settings["start_cmd"]:
                self._l1fog.add("Cameras: no start_cmd in app_settings.json")
                return False
            self._cameras, gen = "starting", self._generation
        self._log.add("Cameras: start command sent")
        threading.Thread(target=self._start_thread, args=(gen,), name="jetson-start", daemon=True).start()
        return True

    def _start_thread(self, gen):
        try:
            code, text = self._run(self._settings["start_cmd"], timeout=10)
            for line in text.splitlines()[-5:]:
                self._log.add(f"Jetson: {line}")
            if code != 0:
                raise RuntimeError(f"the start command exited with code {code}")
            for _ in range(8):                           # give the script a few seconds to appear
                time.sleep(1)
                with self._lock:
                    if gen != self._generation:
                        return
                if self._settings["check_cmd"] and self._run(self._settings["check_cmd"], timeout=6)[0] == 0:
                    with self._lock:
                        self._cameras = "running"
                    self._log.add("Cameras: the stream script is running")
                    return
            raise RuntimeError("the script is not running 8 seconds after the start command")
        except Exception as e:
            with self._lock:
                if gen != self._generation:
                    return
                self._cameras = "failed"
            self._log.add(f"Cameras: {type(e).__name__}: {e}")

    def stop_cameras(self):
        with self._lock:
            if self._ssh != "connected" or self._cameras in ("stopped", "stopping", "starting"):
                return False
            if not self._settings["stop_cmd"]:
                self._log.add("Cameras: no stop_cmd in app_settings.json")
                return False
            self._cameras, gen = "stopping", self._generation
        self._log.add("Cameras: stop command sent")
        threading.Thread(target=self._stop_thread, args=(gen,), name="jetson-stop", daemon=True).start()
        return True

    def _stop_thread(self, gen):
        try:
            code, text = self._run(self._settings["stop_cmd"], timeout=10)
            if code != 0:
                raise RuntimeError(f"the stop command exited with code {code}: {text}")
            for line in text.splitlines()[-5:]:
                self._log.add(f"Jetson: {line}")
            for _ in range(5):
                time.sleep(1)
                with self._lock:
                    if gen != self._generation:
                        return
                if not self._settings["check_cmd"] or self._run(self._settings["check_cmd"], timeout=6)[0] != 0:
                    with self._lock:
                        self._cameras = "stopped"
                    self._log.add("Cameras: the stream script has stopped")
                    return
            raise RuntimeError("the script is still running 5 seconds after the stop command")
        except Exception as e:
            with self._lock:
                if gen != self._generation:
                    return
                self._cameras = "failed"
            self._log.add(f"Cameras: {type(e).__name__}: {e}")

    def refresh_temperatures(self):
        with self._lock:
            if self._ssh != "connected":
                return False
            gen = self._generation
        threading.Thread(
            target=self._refresh_temperatures_thread,
            args=(gen,),
            name="jetson-temperature-refresh",
            daemon=True,
        ).start()
        return True

    def _refresh_temperatures_thread(self, gen):
        try:
            self._read_temps()
        except Exception as e:
            self._lost(gen, f"{type(e).__name__}: {e}")

    def set_fan_speed(self, percent):
        if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
            self._log.add("Fan: speed must be an integer from 0 to 100")
            return False
        with self._lock:
            if self._ssh != "connected":
                self._log.add("Fan: connect to the Jetson first")
                return False
            if self._fan_mode != "manual":
                self._log.add("Fan: select Manual mode before setting a speed")
                return False
            if not self._settings.get("fan_cmd"):
                self._log.add("Fan: configure fan_cmd in app_settings.json first")
                return False
            gen = self._generation
        pwm = round(percent * 255 / 100)
        command = self._settings["fan_cmd"].replace("{pwm}", str(pwm)).replace(
            "{percent}", str(percent)
        )
        threading.Thread(
            target=self._set_fan_speed_thread,
            args=(gen, percent, command),
            name="jetson-fan-control",
            daemon=True,
        ).start()
        return True

    def set_fan_mode(self, mode, percent=50):
        if mode not in ("auto", "manual"):
            self._log.add("Fan: mode must be auto or manual")
            return False
        if mode == "manual" and (
            isinstance(percent, bool)
            or not isinstance(percent, int)
            or not 0 <= percent <= 100
        ):
            self._log.add("Fan: speed must be an integer from 0 to 100")
            return False
        with self._lock:
            if self._ssh != "connected":
                self._log.add("Fan: connect to the Jetson first")
                return False
            if self._fan_mode.startswith("setting_"):
                return False
            command = self._settings.get(f"fan_{mode}_cmd", "")
            if not command:
                self._log.add(f"Fan: configure fan_{mode}_cmd in app_settings.json first")
                return False
            if mode == "manual":
                pwm = round(percent * 255 / 100)
                command = command.replace("{pwm}", str(pwm)).replace(
                    "{percent}", str(percent)
                )
            gen = self._generation
            self._fan_mode = f"setting_{mode}"
        threading.Thread(
            target=self._set_fan_mode_thread,
            args=(gen, mode, command),
            name="jetson-fan-mode",
            daemon=True,
        ).start()
        return True

    def _run_sudo_command(self, script, timeout=10):
        with self._lock:
            client = self._client
        if client is None:
            raise ConnectionError("not connected")
        command = f"sudo -S -p '' sh -c {shlex.quote(script)}"
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        stdin.write((self._settings["password"] or "") + "\n")
        stdin.flush()
        stdin.close()
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        return stdout.channel.recv_exit_status(), out, err

    def _set_fan_mode_thread(self, gen, mode, script):
        try:
            code, out, err = self._run_sudo_command(script)
            if code:
                raise RuntimeError(f"command failed (exit {code}): {err or out}")
            with self._lock:
                if gen != self._generation:
                    return
                self._fan_mode = mode
            self._log.add(out or f"Fan: switched to {mode} mode")
        except Exception as e:
            with self._lock:
                if gen != self._generation:
                    return
                self._fan_mode = f"failed_{mode}"
            self._log.add(f"Fan: could not switch to {mode} mode: {type(e).__name__}: {e}")

    def _set_fan_speed_thread(self, gen, percent, script):
        try:
            code, out, err = self._run_sudo_command(script)
            with self._lock:
                if gen != self._generation:
                    return
            if code:
                self._log.add(f"Fan: command failed (exit {code}): {err or out}")
            else:
                self._log.add(out or f"Fan: set to {percent}%")
        except Exception as e:
            self._log.add(f"Fan: {type(e).__name__}: {e}")


# ======================================================================================
# PART 4: the Cube connection and the joystick (the same two loops as in joy_cube_test.py)
# ======================================================================================
class Vehicle:
    def __init__(self, settings, cube_log):
        from cube import Cube
        from joy_main import Joystick
        self._settings = settings
        self.log = cube_log
        self.cube = Cube()
        self.joy = None                          # works with no joystick plugged in
        self.joystick_enabled = True
        self.joystick_connected = None                   # None until the first reading
        self.sent = [0.0] * 5                            # what is really being sent: surge, sway, heave, yaw, roll
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._connection_loop, name="connection", daemon=True),
                         threading.Thread(target=self._joystick_loop, name="joystick", daemon=True),
                         threading.Thread(target=self._events_loop, name="events", daemon=True)]

    def start(self):
        for t in self._threads:
            t.start()

    def stop(self):
        """Shutdown in the right order: no more sticks, then neutral + disarm while the link is still open, then close it."""
        self._stop.set()
        self._threads[1].join(timeout=1.5)
        self.cube.close()
        self._threads[0].join(timeout=2.0)

    def set_joystick_enabled(self, on):
        on = bool(on)
        if on != self.joystick_enabled:
            self.joystick_enabled = on
            if on:
                self.joy.js = None                       # look for the joystick again from scratch
            self.log.add("Joystick enabled" if on else "Joystick disabled: zeros are sent, buttons are ignored")
        return True

    # ------------------------------------------------------------ thread: keep the Cube connection alive
    def _connection_loop(self):
        from pymavlink import mavutil
        c = self._settings["cube"]
        master, not_connected_since, last_error = None, None, 0.0
        try:
            while not self._stop.is_set():
                if master is None:
                    try:
                        master = mavutil.mavlink_connection(c["connection"], baud=c["baud"])
                        self.cube.attach(master)
                        not_connected_since = time.monotonic()
                        self.log.add(f"Port {c['connection']} opened, waiting for the Cube ...")
                    except Exception as e:
                        master = None
                        if time.monotonic() - last_error > 30:          # say it now and then, not every 2 seconds
                            last_error = time.monotonic()
                            self.log.add(f"Cannot open {c['connection']}: {type(e).__name__}: {e}")
                        self._stop.wait(c["retry_s"])
                        continue
                if self.cube.get_data()["connected"]:
                    not_connected_since = None
                else:
                    if not_connected_since is None:
                        not_connected_since = time.monotonic()
                    if time.monotonic() - not_connected_since > c["lost_s"]:
                        self.cube.detach()                              # dead for too long: close and search again
                        self._close_port(master)
                        master = None
                        self.log.add(f"No Cube on {c['connection']}: searching again")
                        self._stop.wait(1.0)
                self._stop.wait(0.5)
        finally:
            if master is not None:
                try:
                    master.close()
                except Exception:
                    pass
    def _close_port(self, master, timeout=2.0):
        """Close a port that may be dead. Runs in its own thread, so a close that hangs cannot freeze the reconnect loop."""
        def work():
            try:
                master.close()
            except Exception:
                pass
            try:
                master.port.close()          # belt and braces: pymavlink keeps the pyserial object here
            except Exception:
                pass
        t = threading.Thread(target=work, name="port-close", daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            self.log.add("Closing the old port is taking too long, continuing anyway")
    # ------------------------------------------------------------ thread: joystick -> Cube (+ the safety rule)
    def _read_joystick(self):
        """(connected, message or None). While disabled nothing but 'is it plugged in' is read, so buttons do nothing."""
        import pygame
        if self.joystick_enabled:
            m = self.joy.update()
            return self.joy.js is not None, m
        pygame.event.pump()
        pygame.event.clear()
        return pygame.joystick.get_count() > self.joy.settings.get("joystick_index", 0), None

    def _joystick_loop(self):
        from joy_main import Joystick
        self.joy = Joystick()
        period = 1.0 / self._settings["joystick"]["rate_hz"]
        was_connected, last_disarm, last_note = None, 0.0, 0.0
        disarm_rule = self._settings["joystick"]["disarm_without_joystick"]
        while not self._stop.is_set():
            try:
                connected, m = self._read_joystick()
                live = connected and m is not None                      # sticks and buttons count
                values = ([m["surge"], m["sway"], m["heave"], m["yaw"], m["roll"]] if live else [0.0] * 5)
                self.cube.set_manual(*values)                           # also zeros: keeps the control stream alive
                self.sent = [float(v) for v in values]
                if live:
                    for i in range(4):
                        self.cube.set_relay(i, m[f"Relay_{i + 1}"] == 1)
                    if m["Arm_toggle"] == 1:
                        self.cube.toggle_arm()

                # ---- joystick plugged / unplugged
                if connected != was_connected:
                    if connected:
                        self.log.add("Joystick connected")
                    elif was_connected:
                        self.log.add("Joystick DISCONNECTED: zeros are sent" + (", disarming" if disarm_rule else ""))
                        self.joystick_connected == False
                    else:
                        self.log.add("No joystick found")
                    was_connected = connected
                self.joystick_connected = connected
                now = time.monotonic()
                if disarm_rule and not connected and now - last_disarm > 1.0 and self.cube.get_data()["armed"] and self.cube.get_data()["mode"] == "MANUAL":
                    last_disarm = now                                   # repeated every second until the Cube really is disarmed
                    self.cube.disarm()
                    if now - last_note > 10 and was_connected is False:
                        last_note = now
                        self.log.add("Armed without a joystick: disarming")
            except Exception as e:
                self.log.add(f"Joystick thread error: {type(e).__name__}: {e}")
                self.sent = [0.0] * 5
                self.joystick_connected == False
                try:
                    self.cube.set_manual(0, 0, 0, 0, 0)
                except Exception:
                    pass
                self._stop.wait(1.0)
            self._stop.wait(period)

    # ------------------------------------------------------------ thread: Cube messages -> the shared log
    def _events_loop(self):
        while not self._stop.is_set():
            for text in self.cube.pop_events():
                self.log.add(text)
            self._stop.wait(0.2)


# ======================================================================================
# PART 5: the web server
# ======================================================================================
def create_app(vehicle, jetson, cube_log, jetson_log):
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")

    @app.route("/")
    def index():
        return app.send_static_file("index.html")

    def snapshot():
        """The same keys as MockBackend.get_data() in static/backend.js."""
        d = vehicle.cube.get_data()
        d["motors"] = d["motors"][:6] if d["motors"] else None          # the page shows M1..M6
        d["jetson"], d["jetson_cpu_c"], d["jetson_gpu_c"] = jetson.state()
        d["jetson_camera_urls"] = jetson.camera_urls()
        d["joystick_enabled"] = vehicle.joystick_enabled
        d["joystick_connected"] = vehicle.joystick_connected
        d["sent"] = list(vehicle.sent)
        return d

    @app.route("/api/stream")
    def stream():
        def generate():
            seen_cube, seen_jetson = cube_log.last_id(), jetson_log.last_id()   # a new page starts with an empty log
            yield "retry: 1000\n\n"
            while True:
                ev, seen_cube = cube_log.since(seen_cube)
                jev, seen_jetson = jetson_log.since(seen_jetson)
                yield "data: " + json.dumps({"d": snapshot(), "ev": ev, "jev": jev}) + "\n\n"
                time.sleep(0.1)
        return Response(generate(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def answer(ok):
        return jsonify(ok=bool(ok))

    @app.post("/api/arm")
    def arm():
        return answer(vehicle.cube.arm())

    @app.post("/api/disarm")
    def disarm():
        return answer(vehicle.cube.disarm())

    @app.post("/api/mode")
    def mode():
        return answer(vehicle.cube.set_mode((request.get_json(silent=True) or {}).get("name", "")))

    @app.post("/api/joystick")
    def joystick():
        return answer(vehicle.set_joystick_enabled((request.get_json(silent=True) or {}).get("enabled", True)))

    @app.post("/api/jetson/<action>")
    def jetson_action(action):
        actions = {"connect": jetson.connect, "disconnect": jetson.disconnect,
                   "start_cameras": jetson.start_cameras, "stop_cameras": jetson.stop_cameras,
                   "refresh_temperatures": jetson.refresh_temperatures}
        if action not in actions:
            return answer(False), 404
        return answer(actions[action]())

    @app.post("/api/jetson/fan")
    def jetson_fan():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(ok=False, error="JSON object required"), 400
        percent = payload.get("percent")
        if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
            return jsonify(ok=False, error="percent must be an integer from 0 to 100"), 400
        if not jetson.set_fan_speed(percent):
            return jsonify(ok=False), 409
        return answer(True)

    @app.post("/api/jetson/fan/mode")
    def jetson_fan_mode():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("mode") not in ("auto", "manual"):
            return jsonify(ok=False, error="mode must be auto or manual"), 400
        percent = payload.get("percent", 50)
        if payload["mode"] == "manual" and (
            isinstance(percent, bool)
            or not isinstance(percent, int)
            or not 0 <= percent <= 100
        ):
            return jsonify(ok=False, error="percent must be an integer from 0 to 100"), 400
        if not jetson.set_fan_mode(payload["mode"], percent):
            return jsonify(ok=False), 409
        return answer(True)

    return app


# ======================================================================================
# MAIN
# ======================================================================================
def main():
    settings = load_app_settings()
    cube_log, jetson_log = EventLog(), EventLog()
    vehicle = Vehicle(settings, cube_log)
    jetson = JetsonLink(settings["jetson"], jetson_log)
    app = create_app(vehicle, jetson, cube_log, jetson_log)

    def on_terminate(*_):
        raise KeyboardInterrupt                           # closing the window / kill: shut down the same way as Ctrl+C
    signal.signal(signal.SIGTERM, on_terminate)
    vehicle.start()
    jetson.start_auto_connect()
    web = settings["web"]
    print(f"Dashboard: http://localhost:{web['port']}   (Ctrl+C to stop)")
    try:
        app.run(host=web["host"], port=web["port"], threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nShutting down ...")
        jetson.stop_auto_connect()
        jetson.disconnect(manual=False)
        vehicle.stop()
        print("Done.")


if __name__ == "__main__":
    main()
