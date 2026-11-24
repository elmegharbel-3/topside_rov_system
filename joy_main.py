import json
import os
import time
import pygame
settings_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),"joystick_settings.json")
axis_names = ("surge","sway","heave","yaw")

def load_settings(path=settings_file):
    try:
        with open(path, encoding="utf-8") as f:
            settings = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Settings file not found: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"The settings file is not valid JSON: {e} (check for a missing comma or quote)")
    for name in axis_names:
        if name not in settings.get("axes", {}):
            raise ValueError(f"The settings file has no entry for axes -> {name}")
    return settings

class Joystick:
    def __init__(self,settings_file=settings_file):
        self.settings = load_settings(settings_file) # settings to dictionary
        self.relay_buttons_indexes = list(self.settings.get("relay_buttons") or [])
        self.arm_btn_down_since = None
        self.relay_buttons_states = [0] * 4
        self.relay_buttons_prev = [0] * 4
        self.relay_states = [0] * 4      # the one sent to the cube
        self.arm_button = self.settings["arm_button"]
        self.arm_btn_wait = self.settings["arm_hold_seconds"]
        self.prev_value_arm = 0
        self.start_time = None
        self.arm_signal_sent = False

        pygame.init()
        pygame.joystick.init()
        self.js = None
        self.connect()
        self.message = {
            "surge":0.0,
            "sway":0.0,
            "heave":0.0,
            "yaw":0.0,
            "roll":0,
            "Relay_1":0,
            "Relay_2":0,
            "Relay_3":0,
            "Relay_4":0,
            "Arm_toggle":0
        }
    def connect(self):
        idx = self.settings.get("joystick_index", 0)
        if pygame.joystick.get_count() > idx:
            self.js = pygame.joystick.Joystick(idx)   # pygame 2 inits it automatically
            self.last_error = None

    def update(self):
        for e in pygame.event.get():                  # pumps AND reads events (don't clear them first)
            if e.type == pygame.JOYDEVICEREMOVED:
                if self.js is not None and getattr(e, "instance_id", None) == self.js.get_instance_id():
                    self.disconnect()
            elif e.type == pygame.JOYDEVICEADDED and self.js is None:
                self.connect()
        if self.js is None:
            self.connect()                            # cheap fallback poll, also covers startup
        if self.js is None:
            self.set_neutral()
            return self.message
        try:
            for n in axis_names:
                self.message[n] = self.read_axis(axis_name=n)
            self.message["roll"] = self.read_roll()
            self.relay_toggle(self.relay_buttons_indexes)
            self.message["Arm_toggle"] = self.arm_toggle()
        except pygame.error as e:
            self.last_error = str(e)                  # shows WHY, e.g. "Invalid joystick hat"
            self.set_neutral()
        return self.message

    def read_axis(self,axis_name):
        axis_index = self.settings["axes"][axis_name]["axis"]
        axis_inverted = self.settings["axes"][axis_name]["invert"]
        axis_deadzone = self.settings["axes"][axis_name]["deadzone"]
        axis_scale = self.settings["axes"][axis_name]["scale"]
        value = self.js.get_axis(axis_index)
        if abs(value) < axis_deadzone:
            return 0.0
        if axis_inverted:
            value = -value
        return max(-1,min(1,value * axis_scale)) 
    def read_roll(self):
        sub_index = self.settings["roll"]["hat_subindex"]
        hat_inverted = self.settings["roll"]["invert"]
        hat_read = self.js.get_hat(0)
        value = hat_read[sub_index]
        if hat_inverted:
            value = -value
        return value
    def relay_toggle(self,relay_buttons):
        for i,relay_index in enumerate(relay_buttons):
            value = self.js.get_button(relay_index)
            self.relay_buttons_states[i] = value
            if self.relay_buttons_states[i] == 0 and self.relay_buttons_prev[i] == 1:
                self.relay_states[i] = 1 - self.relay_states[i]      # flips the state
            self.message[f"Relay_{i+1}"] = self.relay_states[i]      # instead of pulses the states are save and sent to mavlink
            self.relay_buttons_prev[i] = value
            
    def arm_toggle(self):
        if self.arm_button is None:             # if arm button not set
            return 0
        value = self.js.get_button(self.arm_button)
        if self.arm_signal_sent and value == 1:
            return 0
        if value == 1 and self.prev_value_arm == 0:
            self.start_time = time.monotonic()
            self.prev_value_arm = value
            return 0
        elif value == 1 and self.prev_value_arm == 1:
            elapsed = time.monotonic() - self.start_time
            if elapsed >= self.arm_btn_wait:
                elapsed = 0
                self.arm_signal_sent = True
                return 1
            else:return 0
        elif value == 0:
            self.prev_value_arm = 0
            self.arm_signal_sent = False
            return 0
    def set_neutral(self):                       # zero everything
        for n in axis_names:
            self.message[n] = 0.0
        self.message["roll"] = 0.0
        self.message["Arm_toggle"] = 0

if __name__ == "__main__":
    joy = Joystick()
    print("Move sticks, press buttons. Ctrl+C to stop.\n")
    try:
        while True:
            m = joy.update()
            print(f"\rsurge:{m['surge']:+.2f} sway:{m['sway']:+.2f} heave:{m['heave']:+.2f} "
                  f"yaw:{m['yaw']:+.2f} roll:{m['roll']:+.1f} | relays:{m['Relay_1']}{m['Relay_2']}"
                  f"{m['Relay_3']}{m['Relay_4']} | arm:{m['Arm_toggle']} | "
                  f"{'joystick OK' if joy.js else 'NO JOYSTICK'}   ", end="", flush=True)
            if m["Arm_toggle"]:
                print("\n>>> ARM / DISARM requested")
            time.sleep(0.033)
    except KeyboardInterrupt:
        print("\nStopped.")
        pygame.quit()
        

        

        

    