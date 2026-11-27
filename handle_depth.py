from pymavlink import mavutil
from cube import Cube
import time

cube = Cube()
cube.attach(mavutil.mavlink_connection("COM8", baud=115200))
while True:
    d = cube.get_data()
    print(" | ".join(f"P{n}: {d[f'press{n}_hpa']} hPa {d[f'press{n}_temp_c']} C" for n in (1, 2, 3)),
          f"| depth_vfr {d['depth_vfr']}")
    time.sleep(0.5)