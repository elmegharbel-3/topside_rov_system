# ROV dashboard: how to run

Folder layout (all in ONE folder):
    app.py  app_settings.json  cube.py  cube_settings.json  joy_main.py  joystick_settings.json  static/

Install once:    pip install -r requirements.txt
Run:             python app.py         then open http://localhost:5000
Stop:            Ctrl+C                (neutral + disarm are sent on the way out)

Try it with no vehicle:
    1. in app_settings.json set  "connection": "udpin:0.0.0.0:14550"  and  "disarm_without_joystick": false  (if you have no joystick)
    2. python fake_cube.py          (own window)
    3. python app.py

Open static/index.html by double-clicking (no app.py) and you get the MOCK backend with fake data and the "Mock controls" panel.
Add ?mock to the address of a page served by app.py to get the mock there too.

Jetson controls are on the dashboard: SSH status/connect, camera start/stop, CPU/GPU temperatures, fan speed, and the
four live camera streams. Automatic SSH connection retries while the configured Jetson SSH port is reachable; cameras
start after connecting if the configured check command reports them stopped.
CPU/GPU temperatures refresh every second. Fan control defaults to the Jetson's automatic mode; choose Manual to enable
the speed slider and Set Fan Speed button.

Jetson host, username and password are set by `JETSON_IP`, `JETSON_USER` and `JETSON_PASS` constants near the top of
app.py. You can override them in app_settings.json. `JETSON_PASSWORD` can override the password; the same SSH password
is used for sudo. Fan mode and speed commands use sudo; adjust the fan commands if your Jetson's fan interface differs.
The browser connects to the configured camera ports directly, so the computer running the browser must be able to reach
the Jetson on those ports.

Camera ports and stream path, along with start/stop/check and temperature/fan commands, can be changed in app_settings.json.
