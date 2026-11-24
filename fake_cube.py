"""fake_cube.py - a pretend ArduSub vehicle, so you can try app.py with NO hardware.

Run it first, in its own window:     python fake_cube.py
then set  "connection": "udpin:0.0.0.0:14550"  in app_settings.json and run  python app.py.

It sends a heartbeat, attitude, depth, water temperature and motor outputs, answers arm / disarm / mode / relay
commands, and moves the motors when it receives sticks while armed.
Options:   --refuse-arm   (answers every arm with DENIED, to see the refusal in the log)
           --leak         (sends a "Leak detected" message after 10 seconds)
"""
import math
import os
import sys
import time

os.environ.setdefault("MAVLINK20", "1")
from pymavlink import mavutil

mavlink = mavutil.mavlink
TARGET = "udpout:127.0.0.1:14550"
REFUSE_ARM = "--refuse-arm" in sys.argv
LEAK = "--leak" in sys.argv

master = mavutil.mavlink_connection(TARGET, source_system=1, source_component=1)
armed, mode = False, 19                                   # 19 = MANUAL
sticks = [0.0, 0.0, 0.0, 0.0]
relays = [0, 0, 0, 0]
t0 = time.monotonic()
last = {"hb": 0, "att": 0, "slow": 0, "fast": 0}
leak_sent = False
print(f"Fake Cube sending to {TARGET}. Ctrl+C to stop.")


def say(text):
    master.mav.statustext_send(mavlink.MAV_SEVERITY_INFO, text.encode())


def handle(msg):
    global armed, mode, sticks
    kind = msg.get_type()
    if kind == "COMMAND_LONG":
        result = mavlink.MAV_RESULT_ACCEPTED
        if msg.command == mavlink.MAV_CMD_COMPONENT_ARM_DISARM:
            if msg.param1 == 1 and REFUSE_ARM:
                result = mavlink.MAV_RESULT_DENIED
                say("PreArm: fake refusal")
            else:
                armed = msg.param1 == 1
                say("Arming motors" if armed else "Disarming motors")
        elif msg.command == mavlink.MAV_CMD_DO_SET_MODE:
            mode = int(msg.param2)
        elif msg.command == mavlink.MAV_CMD_DO_SET_RELAY:
            relays[int(msg.param1)] = int(msg.param2)
        master.mav.command_ack_send(msg.command, result)
    elif kind == "MANUAL_CONTROL":
        sticks = [msg.x / 1000, msg.y / 1000, (msg.z - 500) / 500, msg.r / 1000]


while True:
    now = time.monotonic()
    t = now - t0
    while True:
        msg = master.recv_match(blocking=False)
        if msg is None:
            break
        handle(msg)
    if now - last["hb"] >= 1.0:
        base = mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED | (mavlink.MAV_MODE_FLAG_SAFETY_ARMED if armed else 0)
        master.mav.heartbeat_send(mavlink.MAV_TYPE_SUBMARINE, mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA, base, mode, 4)
        last["hb"] = now
    if now - last["att"] >= 0.1:
        master.mav.attitude_send(int(t * 1000), 0.3 * math.sin(t * 0.8), 0.2 * math.sin(t * 0.55), (t * 0.25) % 6.28 - 3.14, 0, 0, 0)
        last["att"] = now
    if now - last["slow"] >= 0.2:
        depth = max(0.0, 3.0 + 2.0 * math.sin(t * 0.3))
        master.mav.vfr_hud_send(0, 0, int(math.degrees(t * 0.25) % 360), 0, -depth, 0)
        master.mav.scaled_pressure2_send(int(t * 1000), 1013 + depth * 98.0, 0, 1840)       # temperature in 0.01 C
        thrust = [sticks[0] + sticks[3], sticks[0] - sticks[3], sticks[1], sticks[1], sticks[2], sticks[2]]
        pwm = [int(1500 + max(-1, min(1, v)) * 400) if armed else 1500 for v in thrust]
        master.mav.servo_output_raw_send(int(t * 1000), 0, *(pwm + [1500, 1500]))
        master.mav.sys_status_send(0, 0, 0, 0, 15800, -1, 80, 0, 0, 0, 0, 0, 0)
        last["slow"] = now
    if LEAK and not leak_sent and t > 10:
        say("Leak detected")
        leak_sent = True
    time.sleep(0.01)
