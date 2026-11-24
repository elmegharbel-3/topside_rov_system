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

Settings to fill in (marked CHECK in app_settings.json): Cube port, Jetson address / login / start-stop-check commands.
Camera stream addresses: SOURCES in static/common.js.
