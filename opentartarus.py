#!/usr/bin/env python3
"""
Tartarus Pro Manager
A Synapse-like tray application for the Razer Tartarus Pro / V2 Pro.
Runs the remap daemon in the background and provides a GUI for configuration.

===============================================================================
IMPORTANT — Synapse setup required (do this once on Windows)
===============================================================================
1. Joystick / Analog Stick
   In Razer Synapse, set the analog stick to “Full Analog” (or “Analog” mode).
   Do NOT leave it in Digital / Hat / D-pad mode.
   Save the profile to the device’s onboard memory.

2. Three Thumb Buttons (Btn1 / Btn2 / Btn3)
   Set Btn1, Btn2, and Btn3 to the keyboard keys F13, F14, and F15.
   Do NOT leave them on Default, Hypershift, Profile Cycle, or No Function.
   Save the profile to the device’s onboard memory.

After saving, unplug the Tartarus, wait a few seconds, and plug it back in.
If you skip these steps the stick will only act as a digital hat and the three
thumb buttons will not appear in OpenTartarus. The program checks for the
thumb buttons at startup and will warn you (tray + console) if they are missing.
===============================================================================
"""

import sys
import json
import os
import time
import selectors
import copy
import threading
from evdev import InputDevice, UInput, ecodes, categorize, KeyEvent, AbsInfo

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QComboBox, QGroupBox, QGridLayout,
    QSlider, QColorDialog, QTabWidget, QMessageBox, QInputDialog,
    QSystemTrayIcon, QMenu, QStackedWidget
)
from PyQt6.QtCore import Qt, QObject, QEvent, pyqtSignal, QThread
from PyQt6.QtGui import QColor, QIcon, QPixmap, QPainter

try:
    from openrazer.client import DeviceManager
    OPENRAZER_AVAILABLE = True
except ImportError:
    OPENRAZER_AVAILABLE = False

CONFIG_FILE         = os.path.expanduser("~/.tartarus_profiles.json")
ACTIVE_PROFILE_FILE = os.path.expanduser("~/.tartarus_active_profile")
AUTOSTART_DIR       = os.path.expanduser("~/.config/autostart")
AUTOSTART_FILE      = os.path.join(AUTOSTART_DIR, "opentartarus.desktop")

TARTARUS_VENDOR_ID = 0x1532
TARTARUS_PRODUCT_IDS = {
    0x02EB,  # Tartarus V2 Pro only
}

def find_tartarus_devices():
    import evdev as _evdev
    candidates = []
    xbox_dev = None

    for path in _evdev.list_devices():
        try:
            dev = _evdev.InputDevice(path)
            info = dev.info
            name = dev.name.lower()

            if "x-box" in name or "xbox" in name:
                xbox_dev = dev
                continue

            if info.vendor == TARTARUS_VENDOR_ID and info.product in TARTARUS_PRODUCT_IDS:
                candidates.append(dev)
            else:
                dev.close()
        except Exception:
            pass

    if not candidates and not xbox_dev:
        raise RuntimeError("Razer Tartarus not found. Is it plugged in?")

    keys_dev   = None
    mouse_dev  = None
    analog_dev = xbox_dev

    for dev in candidates:
        caps = dev.capabilities()
        keys = caps.get(ecodes.EV_KEY, [])
        rels = caps.get(ecodes.EV_REL, [])
        abs_axes = caps.get(ecodes.EV_ABS, [])

        has_space   = ecodes.KEY_SPACE  in keys
        has_grave   = ecodes.KEY_GRAVE  in keys
        has_b       = ecodes.KEY_B      in keys
        has_middle  = ecodes.BTN_MIDDLE in keys
        has_wheel   = ecodes.REL_WHEEL  in rels
        has_hat     = (ecodes.ABS_HAT0X in abs_axes) or (ecodes.ABS_HAT0Y in abs_axes)
        name_l      = dev.name.lower()

        # Prefer the non-"Keyboard"-named interface when two claim the same key set
        if has_grave and has_space and has_b and not has_middle:
            if keys_dev is None or "keyboard" not in name_l:
                keys_dev = dev
        elif has_middle and has_wheel:
            mouse_dev = dev
        elif has_hat or "generic" in name_l:
            analog_dev = dev
        else:
            dev.close()

    remaining = [d for d in candidates if d not in (keys_dev, mouse_dev, analog_dev)]
    if not keys_dev and remaining:
        keys_dev = remaining.pop(0)
    if not mouse_dev and remaining:
        mouse_dev = remaining.pop(0)
    if analog_dev is None and remaining:
        analog_dev = remaining.pop(0)

    return keys_dev, mouse_dev, analog_dev

TARTARUS_KEY_MAP = {}

KEYS_DEVICE_MAP = {
    "KEY_GRAVE":     "01",
    "KEY_1":         "02",
    "KEY_2":         "03",
    "KEY_3":         "04",
    "KEY_4":         "05",
    "KEY_5":         "06",
    "KEY_TAB":       "07",
    "KEY_Q":         "08",
    "KEY_W":         "09",
    "KEY_E":         "10",
    "KEY_R":         "11",
    "KEY_T":         "12",
    "KEY_CAPSLOCK":  "13",
    "KEY_A":         "14",
    "KEY_S":         "15",
    "KEY_D":         "16",
    "KEY_F":         "17",
    "KEY_G":         "18",
    "KEY_LEFTSHIFT": "19",
    "KEY_Z":         "20",
    "KEY_X":         "21",
    "KEY_C":         "22",
    "KEY_V":         "23",
    "KEY_LEFTCTRL":  "24",
    "KEY_B":         "25",
    "KEY_I":         "26",
    "KEY_SPACE":     "27",
    "KEY_M":         "28",
    "KEY_F13":       "btn1",
    "KEY_F14":       "btn2",
    "KEY_F15":       "btn3",
}

MOUSE_DEVICE_MAP = {
    # V2 Pro has no scroll wheel
}

ANALOG_DEVICE_MAP = {
    "ABS_HAT0X": "analog_x",
    "ABS_HAT0Y": "analog_y",
    "BTN_THUMBL": "analog_click",
}

def key_name_to_code(name):
    name = name.strip().lower()
    mapping = {
        "ctrl": ecodes.KEY_LEFTCTRL, "shift": ecodes.KEY_LEFTSHIFT,
        "alt": ecodes.KEY_LEFTALT, "super": ecodes.KEY_LEFTMETA,
        "tab": ecodes.KEY_TAB, "space": ecodes.KEY_SPACE,
        "return": ecodes.KEY_ENTER, "enter": ecodes.KEY_ENTER,
        "backspace": ecodes.KEY_BACKSPACE, "delete": ecodes.KEY_DELETE,
        "escape": ecodes.KEY_ESC, "esc": ecodes.KEY_ESC,
        "up": ecodes.KEY_UP, "down": ecodes.KEY_DOWN,
        "left": ecodes.KEY_LEFT, "right": ecodes.KEY_RIGHT,
        "home": ecodes.KEY_HOME, "end": ecodes.KEY_END,
        "prior": ecodes.KEY_PAGEUP, "next": ecodes.KEY_PAGEDOWN,
        "pageup": ecodes.KEY_PAGEUP, "pagedown": ecodes.KEY_PAGEDOWN,
        "insert": ecodes.KEY_INSERT, "caps_lock": ecodes.KEY_CAPSLOCK,
        "num_lock": ecodes.KEY_NUMLOCK, "scroll_lock": ecodes.KEY_SCROLLLOCK,
        "f1": ecodes.KEY_F1,   "f2": ecodes.KEY_F2,   "f3": ecodes.KEY_F3,
        "f4": ecodes.KEY_F4,   "f5": ecodes.KEY_F5,   "f6": ecodes.KEY_F6,
        "f7": ecodes.KEY_F7,   "f8": ecodes.KEY_F8,   "f9": ecodes.KEY_F9,
        "f10": ecodes.KEY_F10, "f11": ecodes.KEY_F11, "f12": ecodes.KEY_F12,
        "f13": ecodes.KEY_F13, "f14": ecodes.KEY_F14, "f15": ecodes.KEY_F15,
        "1": ecodes.KEY_1, "2": ecodes.KEY_2, "3": ecodes.KEY_3,
        "4": ecodes.KEY_4, "5": ecodes.KEY_5, "6": ecodes.KEY_6,
        "7": ecodes.KEY_7, "8": ecodes.KEY_8, "9": ecodes.KEY_9, "0": ecodes.KEY_0,
        "a": ecodes.KEY_A, "b": ecodes.KEY_B, "c": ecodes.KEY_C,
        "d": ecodes.KEY_D, "e": ecodes.KEY_E, "f": ecodes.KEY_F,
        "g": ecodes.KEY_G, "h": ecodes.KEY_H, "i": ecodes.KEY_I,
        "j": ecodes.KEY_J, "k": ecodes.KEY_K, "l": ecodes.KEY_L,
        "m": ecodes.KEY_M, "n": ecodes.KEY_N, "o": ecodes.KEY_O,
        "p": ecodes.KEY_P, "q": ecodes.KEY_Q, "r": ecodes.KEY_R,
        "s": ecodes.KEY_S, "t": ecodes.KEY_T, "u": ecodes.KEY_U,
        "v": ecodes.KEY_V, "w": ecodes.KEY_W, "x": ecodes.KEY_X,
        "y": ecodes.KEY_Y, "z": ecodes.KEY_Z,
        "-": ecodes.KEY_MINUS, "=": ecodes.KEY_EQUAL,
        "[": ecodes.KEY_LEFTBRACE, "]": ecodes.KEY_RIGHTBRACE,
        ";": ecodes.KEY_SEMICOLON, "'": ecodes.KEY_APOSTROPHE,
        ",": ecodes.KEY_COMMA, ".": ecodes.KEY_DOT, "/": ecodes.KEY_SLASH,
        "`": ecodes.KEY_GRAVE, "\\": ecodes.KEY_BACKSLASH,
    }
    return mapping.get(name)

def parse_combo(combo_str):
    parts = [p.strip() for p in combo_str.lower().split("+")]
    codes = []
    for part in parts:
        code = key_name_to_code(part)
        if code is not None:
            codes.append(code)
    return codes

def load_profiles():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {
        "Default": {"keys": {}, "lighting": {"effect": "spectrum", "color": "#00FF00", "brightness": 80}, "analog_mode": "left", "analog_keymap": "off"},
        "WoW":     {"keys": {}, "lighting": {"effect": "static",   "color": "#FF8800", "brightness": 80}, "analog_mode": "left", "analog_keymap": "off"},
        "Valheim": {"keys": {}, "lighting": {"effect": "wave",     "color": "#0088FF", "brightness": 80}, "analog_mode": "left", "analog_keymap": "off"},
    }

def save_profiles(profiles):
    with open(CONFIG_FILE, "w") as f:
        json.dump(profiles, f, indent=2)

def get_active_profile_name(profiles):
    if os.path.exists(ACTIVE_PROFILE_FILE):
        with open(ACTIVE_PROFILE_FILE) as f:
            name = f.read().strip()
            if name in profiles:
                return name
    return list(profiles.keys())[0]

def set_active_profile_name(name):
    with open(ACTIVE_PROFILE_FILE, "w") as f:
        f.write(name)

KEY_SELECTOR_OPTIONS = [
    ("Escape", "esc"),
    ("Tab", "tab"),
    ("Enter / Return", "return"),
    ("Backspace", "backspace"),
    ("Delete", "delete"),
    ("Space", "space"),
    ("Insert", "insert"),
    ("Home", "home"),
    ("End", "end"),
    ("Page Up", "pageup"),
    ("Page Down", "pagedown"),
    ("Up Arrow", "up"),
    ("Down Arrow", "down"),
    ("Left Arrow", "left"),
    ("Right Arrow", "right"),
    ("Caps Lock", "caps_lock"),
    ("Num Lock", "num_lock"),
    ("Scroll Lock", "scroll_lock"),
    ("F1", "F1"), ("F2", "F2"), ("F3", "F3"), ("F4", "F4"),
    ("F5", "F5"), ("F6", "F6"), ("F7", "F7"), ("F8", "F8"),
    ("F9", "F9"), ("F10", "F10"), ("F11", "F11"), ("F12", "F12"),
    ("Ctrl (modifier)", "ctrl"),
    ("Shift (modifier)", "shift"),
    ("Alt (modifier)", "alt"),
    ("Super / Meta (modifier)", "super"),
]

class DaemonThread(QThread):
    status_changed = pyqtSignal(str)
    warning_needed = pyqtSignal(str)
    device_found = pyqtSignal(bool)
    config_status = pyqtSignal(str)

    MODIFIER_CODES = {
        ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL,
        ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT,
        ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT,
        ecodes.KEY_LEFTMETA, ecodes.KEY_RIGHTMETA,
    }

    def __init__(self):
        super().__init__()
        self.mapping = {}
        self.ui = None
        self.gamepad = None
        self.devices = []
        self.running = False
        self.sel = selectors.DefaultSelector()
        self.config_mtime = 0
        self.profiles = {}
        self.held_modifiers = set()
        self.held_keys = set()
        self._analog_held = []
        self._hat_x = 0
        self._hat_y = 0
        self._abs_x = 0
        self._abs_y = 0
        self.analog_thread = None

    def setup_uinput(self):
        cap = {
            ecodes.EV_KEY: list(ecodes.keys.keys()) + [
                ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE
            ],
        }
        self.ui = UInput(cap, name="Tartarus Pro Manager")
        self.status_changed.emit("uinput ready")

    def setup_gamepad(self):
        cap = {
            ecodes.EV_KEY: [
                ecodes.BTN_SOUTH, ecodes.BTN_EAST, ecodes.BTN_NORTH, ecodes.BTN_WEST,
                ecodes.BTN_TL, ecodes.BTN_TR, ecodes.BTN_SELECT, ecodes.BTN_START,
                ecodes.BTN_MODE, ecodes.BTN_THUMBL, ecodes.BTN_THUMBR,
            ],
            ecodes.EV_ABS: [
                (ecodes.ABS_X,  AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                (ecodes.ABS_Y,  AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                (ecodes.ABS_RX, AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                (ecodes.ABS_RY, AbsInfo(value=0, min=-32768, max=32767, fuzz=16, flat=128, resolution=0)),
                (ecodes.ABS_HAT0X, AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)),
                (ecodes.ABS_HAT0Y, AbsInfo(value=0, min=-1, max=1, fuzz=0, flat=0, resolution=0)),
            ],
        }
        self.gamepad = UInput(cap, name="Tartarus Analog Stick")
        self.status_changed.emit("virtual gamepad ready")

    def setup_devices(self):
        global TARTARUS_KEY_MAP
        try:
            keys_dev, mouse_dev, analog_dev = find_tartarus_devices()
        except RuntimeError as e:
            self.status_changed.emit(str(e))
            return

        has_f13 = has_f14 = has_f15 = False
        for dev in [keys_dev, mouse_dev, analog_dev]:
            if not dev:
                continue
            try:
                keys = dev.capabilities().get(ecodes.EV_KEY, [])
                if ecodes.KEY_F13 in keys:
                    has_f13 = True
                if ecodes.KEY_F14 in keys:
                    has_f14 = True
                if ecodes.KEY_F15 in keys:
                    has_f15 = True
            except Exception:
                pass

        if not (has_f13 and has_f14 and has_f15):
            warning = (
                "Thumb buttons not configured.\n\n"
                "In Razer Synapse set Btn1/Btn2/Btn3 to F13/F14/F15, "
                "save the profile to the device's onboard memory, then unplug and replug the Tartarus."
            )
            self.status_changed.emit("WARNING: Thumb buttons (Btn1/Btn2/Btn3) are not emitting F13/F14/F15.")
            self.warning_needed.emit(warning)
            btn_status = "Buttons: not F13/F14/F15"
        else:
            btn_status = "Buttons: F13/F14/F15 OK"

        # Check stick mode — prefer the device we actually use for analog
        stick_status = "Stick: no axes"
        try:
            # First check the analog device we already identified
            if analog_dev:
                try:
                    abs_axes = analog_dev.capabilities().get(ecodes.EV_ABS, [])
                    has_abs_xy = ecodes.ABS_X in abs_axes and ecodes.ABS_Y in abs_axes
                    has_hat = ecodes.ABS_HAT0X in abs_axes or ecodes.ABS_HAT0Y in abs_axes
                    if has_abs_xy and not has_hat:
                        stick_status = "Stick: full analog"
                    elif has_abs_xy and has_hat:
                        stick_status = "Stick: analog + hat"
                    elif has_hat:
                        stick_status = "Stick: digital hat only"
                    else:
                        stick_status = f"Stick: {analog_dev.name} (no abs)"
                except Exception:
                    stick_status = "Stick: analog dev check failed"

            # If still unknown, scan for Xbox-named devices
            if stick_status == "Stick: no axes" or "no abs" in stick_status:
                import evdev as _evdev
                for path in _evdev.list_devices():
                    try:
                        dev = _evdev.InputDevice(path)
                        name = dev.name.lower()
                        if "x-box" in name or "xbox" in name:
                            abs_axes = dev.capabilities().get(ecodes.EV_ABS, [])
                            has_abs_xy = ecodes.ABS_X in abs_axes and ecodes.ABS_Y in abs_axes
                            has_hat = ecodes.ABS_HAT0X in abs_axes or ecodes.ABS_HAT0Y in abs_axes
                            if has_abs_xy and not has_hat:
                                stick_status = "Stick: full analog"
                                dev.close()
                                break
                            elif has_abs_xy and has_hat:
                                stick_status = "Stick: analog + hat"
                                dev.close()
                                break
                            elif has_hat:
                                stick_status = "Stick: digital hat only"
                        dev.close()
                    except Exception:
                        pass
        except Exception:
            stick_status = "Stick: check failed"

        self.config_status.emit(f"{btn_status}  |  {stick_status}")

        TARTARUS_KEY_MAP = {}

        if keys_dev:
            for keycode, tid in KEYS_DEVICE_MAP.items():
                TARTARUS_KEY_MAP[(keys_dev.path, keycode)] = tid
            self.status_changed.emit(f"Keys device: {keys_dev.path}")

        if mouse_dev:
            for keycode, tid in MOUSE_DEVICE_MAP.items():
                TARTARUS_KEY_MAP[(mouse_dev.path, keycode)] = tid
            self.status_changed.emit(f"Mouse device: {mouse_dev.path}")

        if analog_dev:
            for keycode, tid in ANALOG_DEVICE_MAP.items():
                TARTARUS_KEY_MAP[(analog_dev.path, keycode)] = tid
            self.status_changed.emit(f"Analog device: {analog_dev.path}")

        found = bool(keys_dev or mouse_dev or analog_dev)
        self.device_found.emit(found)

        for dev in [keys_dev, mouse_dev]:
            if dev:
                try:
                    saved_path = dev.path
                    dev.close()
                    dev = InputDevice(saved_path)
                    dev.grab()
                    self.devices.append(dev)
                    self.sel.register(dev, selectors.EVENT_READ)
                    self.status_changed.emit(f"Grabbed {dev.path} ({dev.name})")
                except Exception as e:
                    self.status_changed.emit(f"Could not grab {dev.path}: {e}")

        if analog_dev:
            try:
                saved_path = analog_dev.path
                analog_dev.close()
                analog_dev = InputDevice(saved_path)
                analog_dev.grab()
                self.devices.append(analog_dev)
                self.status_changed.emit(f"Grabbed {analog_dev.path} ({analog_dev.name})")

                def analog_reader():
                    try:
                        for event in analog_dev.read_loop():
                            if not self.running:
                                break
                            self.handle_event(analog_dev, event)
                    except Exception:
                        pass

                self.analog_thread = threading.Thread(target=analog_reader, daemon=True)
                self.analog_thread.start()
            except Exception as e:
                self.status_changed.emit(f"Could not grab analog: {e}")

    def reload_mapping(self):
        try:
            mtime = os.path.getmtime(CONFIG_FILE)
            if mtime != self.config_mtime:
                self.profiles = load_profiles()
                active = get_active_profile_name(self.profiles)
                self.mapping = self.profiles[active].get("keys", {})
                self.config_mtime = mtime
                self.status_changed.emit(f"Profile '{active}' loaded — {len(self.mapping)} keys")
        except Exception as e:
            self.status_changed.emit(f"Config error: {e}")

    def press_combo(self, combo_str):
        codes = parse_combo(combo_str)
        if not codes:
            return
        held = set(self.held_modifiers)
        extra = [c for c in held if c not in codes]
        all_codes = extra + codes
        for code in all_codes:
            self.ui.write(ecodes.EV_KEY, code, 1)
        self.ui.syn()
        for code in reversed(all_codes):
            self.ui.write(ecodes.EV_KEY, code, 0)
        self.ui.syn()
        for code in held:
            self.ui.write(ecodes.EV_KEY, code, 1)
        self.ui.syn()

    def press_macro(self, macro_str):
        for combo in macro_str.strip().split():
            self.press_combo(combo)
            time.sleep(0.05)

    def _flush_keys(self, repeat=False):
        value = 2 if repeat else 1
        for code in self.held_keys:
            self.ui.write(ecodes.EV_KEY, code, value)
        self.ui.syn()

    def _emit_analog(self, dir_id):
        assignment = self.mapping.get(dir_id, {})
        mapped = assignment.get("key", "").strip()
        if not mapped:
            return
        codes = parse_combo(mapped)
        if not codes:
            return
        held_extra = [c for c in self.held_modifiers if c not in codes]
        for code in held_extra + codes:
            self.ui.write(ecodes.EV_KEY, code, 1)
        self.ui.syn()
        for c in codes:
            if c not in self._analog_held:
                self._analog_held.append(c)

    def _release_analog(self):
        if not self._analog_held:
            return
        for code in reversed(self._analog_held):
            self.ui.write(ecodes.EV_KEY, code, 0)
        self.ui.syn()
        self._analog_held = []
        for code in self.held_modifiers:
            self.ui.write(ecodes.EV_KEY, code, 1)
        self.ui.syn()

    def handle_event(self, device, event):
        if event.type == ecodes.EV_ABS:
            if event.code == ecodes.ABS_X:
                self._abs_x = event.value
            elif event.code == ecodes.ABS_Y:
                self._abs_y = event.value
            elif event.code == ecodes.ABS_HAT0X:
                self._hat_x = event.value
            elif event.code == ecodes.ABS_HAT0Y:
                self._hat_y = event.value

            mode = "left"
            keymap_mode = "off"
            try:
                active = get_active_profile_name(self.profiles)
                mode = self.profiles.get(active, {}).get("analog_mode", "left")
                keymap_mode = self.profiles.get(active, {}).get("analog_keymap", "off")
            except Exception:
                pass

            # When key bindings are enabled, suppress analog passthrough
            # and turn continuous axes into digital directions.
            if keymap_mode in ("4way", "8way"):
                # Derive digital directions from continuous axes if present
                threshold = 16384
                if abs(self._abs_x) > threshold or abs(self._abs_y) > threshold:
                    hx = 1 if self._abs_x > threshold else (-1 if self._abs_x < -threshold else 0)
                    hy = 1 if self._abs_y > threshold else (-1 if self._abs_y < -threshold else 0)
                else:
                    hx, hy = self._hat_x, self._hat_y

                self._release_analog()
                if keymap_mode == "8way" and hx and hy:
                    if hx == -1 and hy == -1:
                        self._emit_analog("analog_up_left")
                    elif hx == 1 and hy == -1:
                        self._emit_analog("analog_up_right")
                    elif hx == -1 and hy == 1:
                        self._emit_analog("analog_down_left")
                    elif hx == 1 and hy == 1:
                        self._emit_analog("analog_down_right")
                else:
                    if hx == 1:
                        self._emit_analog("analog_right")
                    elif hx == -1:
                        self._emit_analog("analog_left")
                    if hy == -1:
                        self._emit_analog("analog_up")
                    elif hy == 1:
                        self._emit_analog("analog_down")
                return

            # Normal analog / dpad passthrough
            if self.gamepad:
                if mode == "right":
                    self.gamepad.write(ecodes.EV_ABS, ecodes.ABS_RX, self._abs_x)
                    self.gamepad.write(ecodes.EV_ABS, ecodes.ABS_RY, self._abs_y)
                elif mode == "dpad":
                    hx = 1 if self._abs_x > 16384 else (-1 if self._abs_x < -16384 else 0)
                    hy = 1 if self._abs_y > 16384 else (-1 if self._abs_y < -16384 else 0)
                    self.gamepad.write(ecodes.EV_ABS, ecodes.ABS_HAT0X, hx)
                    self.gamepad.write(ecodes.EV_ABS, ecodes.ABS_HAT0Y, hy)
                else:
                    self.gamepad.write(ecodes.EV_ABS, ecodes.ABS_X, self._abs_x)
                    self.gamepad.write(ecodes.EV_ABS, ecodes.ABS_Y, self._abs_y)
                self.gamepad.syn()
            return

        if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_THUMBL and self.gamepad:
            self.gamepad.write(ecodes.EV_KEY, ecodes.BTN_THUMBL, event.value)
            self.gamepad.syn()

        if event.type != ecodes.EV_KEY:
            return

        key_event = categorize(event)
        keycode = key_event.keycode
        if isinstance(keycode, list):
            keycode = keycode[0]

        tartarus_id = TARTARUS_KEY_MAP.get((device.path, keycode))

        assignment = self.mapping.get(tartarus_id, {}) if tartarus_id else {}
        mapped_key = assignment.get("key", "").strip()
        mapped_macro = assignment.get("macro", "").strip()

        if mapped_key:
            emit_codes = parse_combo(mapped_key)
        else:
            orig = getattr(ecodes, keycode, None)
            emit_codes = [orig] if orig is not None else []

        if not emit_codes:
            return

        is_modifier = all(c in self.MODIFIER_CODES for c in emit_codes)

        if key_event.keystate == KeyEvent.key_up:
            for code in emit_codes:
                self.held_keys.discard(code)
                self.ui.write(ecodes.EV_KEY, code, 0)
            self.ui.syn()
            self._flush_keys()
            return

        if key_event.keystate == KeyEvent.key_hold:
            if not is_modifier:
                self._flush_keys(repeat=True)
            return

        if mapped_macro:
            self.press_macro(mapped_macro)
            return

        for code in emit_codes:
            self.held_keys.add(code)
        self._flush_keys()

    def run(self):
        self.running = True
        self.setup_uinput()
        self.setup_gamepad()
        self.setup_devices()
        self.reload_mapping()
        last_reload = time.time()

        while self.running:
            try:
                events = self.sel.select(timeout=0.5)
                for key, mask in events:
                    device = key.fileobj
                    try:
                        for event in device.read():
                            self.handle_event(device, event)
                    except Exception:
                        pass

                if time.time() - last_reload > 2.0:
                    self.reload_mapping()
                    last_reload = time.time()
            except Exception:
                time.sleep(0.1)
        self.cleanup()

    def stop(self):
        self.running = False

    def cleanup(self):
        for dev in self.devices:
            try:
                dev.ungrab()
                dev.close()
            except Exception:
                pass
        if self.ui:
            self.ui.close()
        if self.gamepad:
            self.gamepad.close()
        self.sel.close()

def make_tray_icon(color="#00FF00"):
    px = QPixmap(22, 22)
    px.fill(Qt.GlobalColor.transparent)
    painter = QPainter(px)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(QColor("#333333"))
    painter.drawEllipse(2, 2, 18, 18)
    painter.end()
    return QIcon(px)

class KeyButton(QPushButton):
    def __init__(self, key_id, label, parent=None):
        super().__init__(parent)
        self.key_id = key_id
        self.label = label
        self.assignment = ""
        self.setFixedSize(68, 52)
        self.setCheckable(True)
        self.update_display()

    def update_display(self):
        short = self.assignment[:8] + "…" if len(self.assignment) > 8 else self.assignment
        text = f"{self.label}\n{short}" if short else self.label
        self.setText(text)
        self.setStyleSheet("""
            QPushButton {
                background: #2a2a2a;
                color: #ccc;
                border: 1px solid #444;
                border-radius: 6px;
                font-size: 11px;
            }
            QPushButton:checked {
                background: #3a3060;
                color: #b09fff;
                border: 1.5px solid #7F77DD;
            }
            QPushButton:hover {
                border-color: #7F77DD;
            }
        """)

class AnalogButton(QPushButton):
    def __init__(self, key_id, parent=None):
        super().__init__(parent)
        self.key_id = key_id
        self.assignment = ""
        self.setFixedSize(68, 68)
        self.setCheckable(True)
        self.setText("Analog")
        self.setStyleSheet("""
            QPushButton {
                background: #2a2a2a;
                color: #ccc;
                border: 1px solid #444;
                border-radius: 34px;
                font-size: 11px;
            }
            QPushButton:checked {
                background: #3a3060;
                color: #b09fff;
                border: 1.5px solid #7F77DD;
            }
            QPushButton:hover {
                border-color: #7F77DD;
            }
        """)

class KeyRecorder(QObject):
    def __init__(self, app_window):
        super().__init__()
        self.app_window = app_window

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress and self.app_window.recording:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.app_window.stop_recording()
                return True
            mods = []
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                mods.append("ctrl")
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                mods.append("shift")
            if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                mods.append("alt")
            if event.modifiers() & Qt.KeyboardModifier.MetaModifier:
                mods.append("super")
            name = event.text().lower() if event.text() else ""
            if not name:
                name = {
                    Qt.Key.Key_Return: "return",
                    Qt.Key.Key_Enter: "return",
                    Qt.Key.Key_Backspace: "backspace",
                    Qt.Key.Key_Delete: "delete",
                    Qt.Key.Key_Tab: "tab",
                    Qt.Key.Key_Space: "space",
                    Qt.Key.Key_Up: "up",
                    Qt.Key.Key_Down: "down",
                    Qt.Key.Key_Left: "left",
                    Qt.Key.Key_Right: "right",
                }.get(key, "")
            if name:
                combo = "+".join(mods + [name]) if mods else name
                self.app_window.key_input.setText(combo)
                self.app_window.stop_recording()
            return True
        return False

class TartarusWindow(QMainWindow):
    def __init__(self, daemon):
        super().__init__()
        self.daemon = daemon
        self.profiles = load_profiles()
        self.current_profile = get_active_profile_name(self.profiles)
        self.selected_key = None
        self.pending_changes = {}
        self.pending_analog_mode = None
        self.pending_analog_keymap = None
        self.recording = False
        self.recorder = KeyRecorder(self)
        self.tartarus_device = None
        self.color = QColor("#00FF00")
        self.all_key_buttons = {}

        self.setWindowTitle("Tartarus V2 Pro Manager")
        self.setMinimumSize(900, 700)
        self.init_ui()
        self.load_profile_to_ui()
        self.init_openrazer()

        # Update status from daemon
        self.daemon.device_found.connect(self.on_device_found)
        self.daemon.config_status.connect(self.on_config_status)

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def on_config_status(self, text):
        self.config_status_label.setText(text)
        if "not F13" in text or "digital hat" in text or "no axes" in text or "check failed" in text:
            self.config_status_label.setStyleSheet("color:#ffaa66;font-size:11px;")
        else:
            self.config_status_label.setStyleSheet("color:#80ff80;font-size:11px;")

    def on_device_found(self, found):
        if found:
            self.device_status.setText("● Device connected")
            self.device_status.setStyleSheet("color:#80ff80;")
        else:
            self.device_status.setText("● No device")
            self.device_status.setStyleSheet("color:#ff6666;")

    def init_openrazer(self):
        if not OPENRAZER_AVAILABLE:
            return
        try:
            dm = DeviceManager()
            for d in dm.devices:
                if "Tartarus" in d.name:
                    self.tartarus_device = d
                    break
        except Exception:
            pass

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        self.setStyleSheet("""
            QMainWindow, QWidget { background:#1a1a1a; color:#ddd; }
            QTabWidget::pane { border:1px solid #333; border-radius:8px; }
            QTabBar::tab { background:#2a2a2a; color:#888; padding:8px 20px; border-radius:4px; margin-right:4px; }
            QTabBar::tab:selected { background:#3a3060; color:#b09fff; }
            QGroupBox { border:1px solid #333; border-radius:8px; margin-top:8px; padding-top:8px; color:#888; font-size:11px; }
            QLineEdit { background:#2a2a2a; border:1px solid #444; border-radius:6px; padding:6px 10px; color:#ddd; }
            QLineEdit:focus { border-color:#7F77DD; }
            QComboBox { background:#2a2a2a; border:1px solid #444; border-radius:6px; padding:6px 10px; color:#ddd; }
            QComboBox QAbstractItemView { background:#2a2a2a; color:#ddd; selection-background-color:#3a3060; }
            QPushButton#primary { background:#3a3060; color:#b09fff; border:1px solid #7F77DD; border-radius:6px; padding:8px 16px; }
            QPushButton#primary:hover { background:#4a40a0; }
            QPushButton#saveall { background:#1a3a1a; color:#80ff80; border:1px solid #40a040; border-radius:6px; padding:8px 16px; }
            QPushButton#saveall:hover { background:#2a4a2a; }
            QPushButton#record { background:#3a1a1a; color:#ff8080; border:1px solid #aa3333; border-radius:6px; padding:8px 12px; }
            QPushButton#record:checked { background:#5a2a2a; }
        """)

        # Profile bar
        pb = QHBoxLayout()
        pb.addWidget(QLabel("Profile:"))
        self.profile_combo = QComboBox()
        for name in self.profiles.keys():
            self.profile_combo.addItem(name)
        self.profile_combo.setCurrentText(self.current_profile)
        self.profile_combo.currentTextChanged.connect(self.on_profile_change)
        pb.addWidget(self.profile_combo)

        new_btn = QPushButton("+ New"); new_btn.setObjectName("primary"); new_btn.clicked.connect(self.new_profile)
        clone_btn = QPushButton("Clone"); clone_btn.setObjectName("primary"); clone_btn.clicked.connect(self.clone_profile)
        del_btn = QPushButton("Delete"); del_btn.setStyleSheet("background:#4a1a1a;color:#ff8080;border:1px solid #aa3333;border-radius:6px;padding:8px 16px;")
        del_btn.clicked.connect(self.delete_profile)
        pb.addWidget(new_btn); pb.addWidget(clone_btn); pb.addWidget(del_btn)
        pb.addStretch()
        self.device_status = QLabel("● No device"); self.device_status.setStyleSheet("color:#ff6666;")
        self.config_status_label = QLabel("Buttons: checking…  |  Stick: checking…")
        self.config_status_label.setStyleSheet("color:#888;font-size:11px;")
        status_col = QVBoxLayout()
        status_col.setSpacing(2)
        status_col.addWidget(self.device_status)
        status_col.addWidget(self.config_status_label)
        pb.addLayout(status_col)
        main_layout.addLayout(pb)

        tabs = QTabWidget()
        main_layout.addWidget(tabs)

        # Remap tab
        rw = QWidget()
        rl = QVBoxLayout(rw)

        # Keypad layout
        dg = QGroupBox("Keypad layout — click a key to remap it")
        dl = QHBoxLayout(dg)
        dl.setSpacing(10)

        def mk(kid, lbl):
            b = KeyButton(kid, lbl)
            b.clicked.connect(lambda: self.select_key(kid))
            self.all_key_buttons[kid] = b
            return b

        c_left = QVBoxLayout()
        c_left.setSpacing(8)
        c_left.setAlignment(Qt.AlignmentFlag.AlignTop)
        for kid, lbl in [("01","01"), ("07","07"), ("13","13"), ("19","19"), ("24","24")]:
            c_left.addWidget(mk(kid, lbl))
        dl.addLayout(c_left)

        columns = [
            [("02","02"), ("08","08"), ("14","14"), ("20","20"), ("25","25")],
            [("03","03"), ("09","09"), ("15","15"), ("21","21"), ("26","26")],
            [("04","04"), ("10","10"), ("16","16"), ("22","22"), ("27","27")],
            [("05","05"), ("11","11"), ("17","17"), ("23","23")],
            [("06","06"), ("12","12"), ("18","18")],
        ]
        for col in columns:
            c = QVBoxLayout()
            c.setSpacing(8)
            c.setAlignment(Qt.AlignmentFlag.AlignTop)
            for kid, lbl in col:
                c.addWidget(mk(kid, lbl))
            dl.addLayout(c)

        c_right = QVBoxLayout()
        c_right.setSpacing(8)
        c_right.setAlignment(Qt.AlignmentFlag.AlignTop)
        c_right.addSpacing(40)

        ab = AnalogButton("analog")
        ab.clicked.connect(lambda: self.select_key("analog"))
        self.all_key_buttons["analog"] = ab
        c_right.addWidget(ab)

        ac = KeyButton("analog_click", "Click")
        ac.clicked.connect(lambda: self.select_key("analog_click"))
        self.all_key_buttons["analog_click"] = ac
        c_right.addWidget(ac)

        for kid, lbl in [("btn1","Btn1"), ("btn2","Btn2"), ("btn3","Btn3")]:
            c_right.addWidget(mk(kid, lbl))

        dl.addLayout(c_right)
        rl.addWidget(dg)

        # Assignment area
        self.assign_group = QGroupBox("Select a key to remap it")
        al = QVBoxLayout(self.assign_group)
        self.assign_stack = QStackedWidget()

        # Normal key page
        normal_page = QWidget()
        npl = QVBoxLayout(normal_page)
        npl.setContentsMargins(0,0,0,0)
        r1 = QHBoxLayout()
        kl = QLabel("Key / combo:"); kl.setStyleSheet("color:#888;font-size:13px;")
        self.key_input = QLineEdit(); self.key_input.setPlaceholderText("e.g. ctrl+c, F5, shift+1")
        self.key_input.textChanged.connect(self.auto_stage)
        self.record_btn = QPushButton("⏺ Record"); self.record_btn.setObjectName("record")
        self.record_btn.setCheckable(True); self.record_btn.setFixedWidth(100)
        self.record_btn.clicked.connect(self.toggle_record)
        r1.addWidget(kl); r1.addWidget(self.key_input); r1.addWidget(self.record_btn)
        npl.addLayout(r1)
        self.record_hint = QLabel("Press Record then hit a key or combo on your keyboard")
        self.record_hint.setStyleSheet("color:#666;font-size:11px;"); npl.addWidget(self.record_hint)

        r1b = QHBoxLayout()
        ksl = QLabel("Or pick a key:"); ksl.setStyleSheet("color:#888;font-size:13px;")
        self.key_selector = QComboBox()
        self.key_selector.addItem("Select a key…", "")
        for label, value in KEY_SELECTOR_OPTIONS:
            self.key_selector.addItem(label, value)
        self.key_selector.currentIndexChanged.connect(self.on_key_selector_changed)
        r1b.addWidget(ksl); r1b.addWidget(self.key_selector)
        npl.addLayout(r1b)
        ks_hint = QLabel("Use this for keys that are hard to record directly, like Escape")
        ks_hint.setStyleSheet("color:#666;font-size:11px;"); npl.addWidget(ks_hint)

        r2 = QHBoxLayout()
        ml = QLabel("Macro:"); ml.setStyleSheet("color:#888;font-size:13px;")
        self.macro_input = QLineEdit(); self.macro_input.setPlaceholderText("e.g. ctrl+a ctrl+c ctrl+v")
        self.macro_input.textChanged.connect(self.auto_stage)
        r2.addWidget(ml); r2.addWidget(self.macro_input); npl.addLayout(r2)

        self.assign_stack.addWidget(normal_page)

        # Analog page
        analog_page = QWidget()
        apl = QVBoxLayout(analog_page)
        apl.setContentsMargins(0,0,0,0)

        warn = QLabel(
            "⚠ Full analog requires the stick set to Analog mode in Razer Synapse "
            "(Windows or a VM). 4-way / 8-way modes only emit digital hat events."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color:#cc8800;font-size:11px;padding:4px;")
        apl.addWidget(warn)

        mode_row = QHBoxLayout()
        mode_lbl = QLabel("Stick mode:"); mode_lbl.setStyleSheet("color:#888;font-size:13px;")
        self.analog_mode = QComboBox()
        self.analog_mode.addItem("Left analog", "left")
        self.analog_mode.addItem("Right analog", "right")
        self.analog_mode.addItem("D-pad", "dpad")
        self.analog_mode.currentIndexChanged.connect(self.on_analog_mode_changed)
        mode_row.addWidget(mode_lbl)
        mode_row.addWidget(self.analog_mode)
        apl.addLayout(mode_row)

        self.analog_keymap_check = QPushButton("Enable key bindings")
        self.analog_keymap_check.setCheckable(True)
        self.analog_keymap_check.setStyleSheet(
            "QPushButton{background:#2a2a2a;color:#888;border:1px solid #444;border-radius:6px;padding:6px 12px;}"
            "QPushButton:checked{background:#3a3060;color:#b09fff;border:1px solid #7F77DD;}"
        )
        self.analog_keymap_check.clicked.connect(self.on_analog_keymap_toggled)
        apl.addWidget(self.analog_keymap_check)

        keymap_row = QHBoxLayout()
        keymap_lbl = QLabel("Mode:"); keymap_lbl.setStyleSheet("color:#888;font-size:13px;")
        self.analog_keymap = QComboBox()
        self.analog_keymap.addItem("4-way", "4way")
        self.analog_keymap.addItem("8-way", "8way")
        self.analog_keymap.currentIndexChanged.connect(self.on_analog_keymap_changed)
        keymap_row.addWidget(keymap_lbl)
        keymap_row.addWidget(self.analog_keymap)
        self.analog_keymap_widget = QWidget()
        self.analog_keymap_widget.setLayout(keymap_row)
        self.analog_keymap_widget.setVisible(False)
        apl.addWidget(self.analog_keymap_widget)

        self.analog_inputs_widget = QWidget()
        inputs_layout = QVBoxLayout(self.analog_inputs_widget)
        inputs_layout.setContentsMargins(0,0,0,0)
        self.analog_inputs = {}
        self.analog_dir_rows = {}
        dir_label = {
            "analog_up":         "↑  Up",
            "analog_down":       "↓  Down",
            "analog_left":       "←  Left",
            "analog_right":      "→  Right",
            "analog_up_left":    "↖  Up-Left",
            "analog_up_right":   "↗  Up-Right",
            "analog_down_left":  "↙  Down-Left",
            "analog_down_right": "↘  Down-Right",
        }
        for dir_id, dir_lbl in dir_label.items():
            row_widget = QWidget()
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0,0,0,0)
            lbl = QLabel(dir_lbl); lbl.setStyleSheet("color:#888;font-size:13px;"); lbl.setFixedWidth(110)
            inp = QLineEdit(); inp.setPlaceholderText("e.g. w, a, s, d or ctrl+w")
            inp.textChanged.connect(lambda text, d=dir_id: self.auto_stage_analog(d, text))
            row.addWidget(lbl); row.addWidget(inp)
            inputs_layout.addWidget(row_widget)
            self.analog_inputs[dir_id] = inp
            self.analog_dir_rows[dir_id] = row_widget
        self.analog_inputs_widget.setVisible(False)
        apl.addWidget(self.analog_inputs_widget)
        self.assign_stack.addWidget(analog_page)

        al.addWidget(self.assign_stack)

        cb = QPushButton("Clear"); cb.setFixedWidth(80)
        cb.setStyleSheet("background:#2a2a2a;border:1px solid #444;border-radius:6px;padding:8px 12px;color:#888;")
        cb.clicked.connect(self.clear_assignment)
        self.save_all_btn = QPushButton("💾 Save all"); self.save_all_btn.setObjectName("saveall")
        self.save_all_btn.setFixedWidth(120); self.save_all_btn.clicked.connect(self.save_all)
        br = QHBoxLayout(); br.addWidget(cb); br.addStretch(); br.addWidget(self.save_all_btn)
        al.addLayout(br)
        self.pending_label = QLabel(""); self.pending_label.setStyleSheet("color:#aa8800;font-size:11px;")
        al.addWidget(self.pending_label)
        rl.addWidget(self.assign_group)
        tabs.addTab(rw, "Remap")

        # Lighting tab
        lw = QWidget(); ll = QVBoxLayout(lw); ll.setSpacing(12)
        eg = QGroupBox("Effect"); el = QGridLayout(eg); self.effect_buttons = {}
        efx = [("Static","static"),("Spectrum","spectrum"),("Breath","breath_single"),
               ("Breath random","breath_random"),("Reactive","reactive"),("Wave","wave"),
               ("Starlight","starlight_single"),("Starlight random","starlight_random"),
               ("Ripple","ripple"),("None","none")]
        for i,(lbl,eff) in enumerate(efx):
            b = QPushButton(lbl); b.setCheckable(True)
            b.setStyleSheet("QPushButton{background:#2a2a2a;color:#888;border:1px solid #444;border-radius:6px;padding:8px;} QPushButton:checked{background:#3a3060;color:#b09fff;border:1.5px solid #7F77DD;} QPushButton:hover{border-color:#7F77DD;}")
            b.clicked.connect(lambda _,e=eff: self.select_effect(e))
            el.addWidget(b, i//5, i%5); self.effect_buttons[eff] = b
        ll.addWidget(eg)
        cg = QGroupBox("Color"); cl = QHBoxLayout(cg)
        for hc in ["#00FF00","#FF0000","#0088FF","#FF00FF","#FF8800","#FFFFFF"]:
            sw = QPushButton(); sw.setFixedSize(28,28)
            sw.setStyleSheet(f"background:{hc};border-radius:14px;border:none;")
            sw.clicked.connect(lambda _,c=hc: self.set_color(c)); cl.addWidget(sw)
        self.color_preview = QPushButton(); self.color_preview.setFixedSize(36,36)
        self.color_preview.setStyleSheet("background:#00FF00;border-radius:18px;border:2px solid #555;")
        self.color_preview.clicked.connect(self.pick_color)
        self.color_hex = QLineEdit("#00FF00"); self.color_hex.setFixedWidth(100)
        self.color_hex.editingFinished.connect(self.hex_color_changed)
        cl.addWidget(self.color_preview); cl.addWidget(QLabel("Hex:")); cl.addWidget(self.color_hex); cl.addStretch()
        ll.addWidget(cg)
        bg = QGroupBox("Brightness"); bl = QHBoxLayout(bg)
        self.bright_slider = QSlider(Qt.Orientation.Horizontal); self.bright_slider.setRange(0,100); self.bright_slider.setValue(80)
        self.bright_label = QLabel("80%"); self.bright_label.setFixedWidth(40)
        self.bright_slider.valueChanged.connect(lambda v: self.bright_label.setText(f"{v}%"))
        bl.addWidget(self.bright_slider); bl.addWidget(self.bright_label); ll.addWidget(bg)
        apb = QPushButton("Apply lighting"); apb.setObjectName("primary"); apb.clicked.connect(self.apply_lighting)
        ll.addWidget(apb); ll.addStretch()
        tabs.addTab(lw, "Lighting")

        # Settings tab
        sw2 = QWidget(); sl2 = QVBoxLayout(sw2); sl2.setSpacing(12)
        sg = QGroupBox("Startup"); sgl = QVBoxLayout(sg)
        self.autostart_btn = QPushButton()
        self.autostart_btn.setObjectName("primary")
        self.autostart_btn.clicked.connect(self.toggle_autostart)
        self.update_autostart_btn()
        sgl.addWidget(self.autostart_btn)
        sgl.addWidget(QLabel("Enables/disables automatic launch on login via KDE autostart."))
        sl2.addWidget(sg)
        dg2 = QGroupBox("Daemon status"); dgl = QVBoxLayout(dg2)
        self.daemon_label = QLabel("🟢 Daemon running")
        self.daemon_label.setStyleSheet("color:#80ff80;font-size:13px;")
        dgl.addWidget(self.daemon_label)
        sl2.addWidget(dg2)
        sl2.addStretch()
        tabs.addTab(sw2, "Settings")

    def on_analog_mode_changed(self, idx):
        mode = self.analog_mode.itemData(idx)
        self.pending_analog_mode = mode
        self._update_pending_label()

    def on_analog_keymap_toggled(self, checked):
        self.analog_keymap_widget.setVisible(checked)
        self.analog_inputs_widget.setVisible(checked)
        if not checked:
            self.pending_analog_keymap = "off"
        else:
            mode = self.analog_keymap.itemData(self.analog_keymap.currentIndex())
            self.pending_analog_keymap = mode
            self._update_analog_dir_visibility(mode)
        self._update_pending_label()

    def on_analog_keymap_changed(self, idx):
        if not self.analog_keymap_check.isChecked():
            return
        mode = self.analog_keymap.itemData(idx)
        self.pending_analog_keymap = mode
        self._update_analog_dir_visibility(mode)
        self._update_pending_label()

    def _update_analog_dir_visibility(self, mode):
        show_diag = (mode == "8way")
        for dir_id, widget in self.analog_dir_rows.items():
            if dir_id in ("analog_up", "analog_down", "analog_left", "analog_right"):
                widget.setVisible(True)
            else:
                widget.setVisible(show_diag)

    def on_key_selector_changed(self, idx):
        value = self.key_selector.itemData(idx)
        if value:
            self.key_input.setText(value)
            self.auto_stage()
        self.key_selector.blockSignals(True)
        self.key_selector.setCurrentIndex(0)
        self.key_selector.blockSignals(False)

    def auto_stage(self):
        if not self.selected_key:
            return
        self.pending_changes[self.selected_key] = {
            "key": self.key_input.text(),
            "macro": self.macro_input.text(),
        }
        btn = self.all_key_buttons.get(self.selected_key)
        if btn and hasattr(btn, 'assignment'):
            btn.assignment = self.key_input.text() or self.macro_input.text()
            if hasattr(btn, 'update_display'):
                btn.update_display()
        self._update_pending_label()

    def auto_stage_analog(self, dir_id, text):
        self.pending_changes[dir_id] = {"key": text, "macro": ""}
        self._update_pending_label()

    def _update_pending_label(self):
        count = len(self.pending_changes)
        if getattr(self, "pending_analog_mode", None) is not None:
            count += 1
        if getattr(self, "pending_analog_keymap", None) is not None:
            count += 1
        self.pending_label.setText(f"⏳ {count} unsaved change{'s' if count != 1 else ''} — click Save all when done")

    def select_key(self, key_id):
        for btn in self.all_key_buttons.values():
            btn.setChecked(False)
        self.all_key_buttons[key_id].setChecked(True)
        self.selected_key = key_id
        self.stop_recording()

        self.key_input.setVisible(True)
        self.macro_input.setVisible(True)
        self.record_btn.setVisible(True)
        self.key_selector.setVisible(True)

        if key_id == "analog":
            self.assign_stack.setCurrentIndex(1)
            self.assign_group.setTitle("Analog stick directions")
            keys = self.profiles[self.current_profile]["keys"]
            for dir_id, inp in self.analog_inputs.items():
                inp.blockSignals(True)
                staged = self.pending_changes.get(dir_id)
                if staged:
                    inp.setText(staged.get("key",""))
                else:
                    inp.setText(keys.get(dir_id, {}).get("key",""))
                inp.blockSignals(False)
        else:
            self.assign_stack.setCurrentIndex(0)
            self.key_input.blockSignals(True); self.macro_input.blockSignals(True)
            staged = self.pending_changes.get(key_id)
            if staged:
                self.key_input.setText(staged.get("key",""))
                self.macro_input.setText(staged.get("macro",""))
            else:
                saved = self.profiles[self.current_profile]["keys"].get(key_id, {})
                self.key_input.setText(saved.get("key",""))
                self.macro_input.setText(saved.get("macro",""))
            self.key_input.blockSignals(False); self.macro_input.blockSignals(False)
            self.assign_group.setTitle(f"Key {key_id}")

    def save_all(self):
        # Always stage the currently selected key if its fields are visible and have content
        if self.selected_key and self.selected_key != "analog" and self.key_input.isVisible():
            self.auto_stage()
        # Apply staged analog mode / keymap
        changed = 0
        if getattr(self, "pending_analog_mode", None) is not None:
            self.profiles[self.current_profile]["analog_mode"] = self.pending_analog_mode
            self.pending_analog_mode = None
            changed += 1
        if getattr(self, "pending_analog_keymap", None) is not None:
            self.profiles[self.current_profile]["analog_keymap"] = self.pending_analog_keymap
            self.pending_analog_keymap = None
            changed += 1
        if not self.pending_changes and changed == 0:
            QMessageBox.information(self, "Nothing to save", "No pending changes.")
            return
        for kid, asgn in self.pending_changes.items():
            self.profiles[self.current_profile]["keys"][kid] = asgn
        save_profiles(self.profiles)
        try:
            os.utime(CONFIG_FILE, None)
        except Exception:
            pass
        total = len(self.pending_changes) + changed
        self.pending_changes.clear()
        self.pending_label.setText("")
        QMessageBox.information(self, "Saved", f"Saved {total} change{'s' if total != 1 else ''} to '{self.current_profile}'!")

    def clear_assignment(self):
        if self.selected_key == "analog":
            for inp in self.analog_inputs.values():
                inp.clear()
        else:
            self.key_input.clear(); self.macro_input.clear()

    def toggle_record(self):
        if self.recording: self.stop_recording()
        else: self.start_recording()

    def start_recording(self):
        self.recording = True
        self.key_input.blockSignals(True); self.key_input.clear(); self.key_input.blockSignals(False)
        self.key_input.setPlaceholderText("Press a key or combo...")
        self.key_input.setStyleSheet("background:#3a1a1a;border:1px solid #aa3333;border-radius:6px;padding:6px 10px;color:#ff8080;")
        self.record_hint.setText("Listening… press Escape to cancel")
        self.record_btn.setChecked(True)

    def stop_recording(self):
        self.recording = False; self.record_btn.setChecked(False)
        self.key_input.setPlaceholderText("e.g. ctrl+c, F5, shift+1")
        self.key_input.setStyleSheet("")
        self.record_hint.setText("Press Record then hit a key or combo on your keyboard")

    def on_profile_change(self, name):
        self.pending_changes.clear(); self.pending_label.setText("")
        self.current_profile = name
        set_active_profile_name(name)
        self.load_profile_to_ui()

    def load_profile_to_ui(self):
        profile = self.profiles[self.current_profile]
        keys = profile.get("keys", {})
        for kid, btn in self.all_key_buttons.items():
            asgn = keys.get(kid, {})
            if hasattr(btn, 'assignment'):
                btn.assignment = asgn.get("key","") or asgn.get("macro","")
            if hasattr(btn, 'update_display'):
                btn.update_display()
        lighting = profile.get("lighting", {})
        for e, btn in self.effect_buttons.items():
            btn.setChecked(e == lighting.get("effect","spectrum"))
        self.set_color(lighting.get("color","#00FF00"))
        self.bright_slider.setValue(lighting.get("brightness", 80))

        mode = self.profiles[self.current_profile].get("analog_mode", "left")
        idx = self.analog_mode.findData(mode)
        if idx >= 0:
            self.analog_mode.blockSignals(True)
            self.analog_mode.setCurrentIndex(idx)
            self.analog_mode.blockSignals(False)

        kmode = self.profiles[self.current_profile].get("analog_keymap", "off")
        enabled = kmode != "off"
        self.analog_keymap_check.blockSignals(True)
        self.analog_keymap_check.setChecked(enabled)
        self.analog_keymap_check.blockSignals(False)
        self.analog_keymap_widget.setVisible(enabled)
        self.analog_inputs_widget.setVisible(enabled)
        if enabled:
            kidx = self.analog_keymap.findData(kmode)
            if kidx >= 0:
                self.analog_keymap.blockSignals(True)
                self.analog_keymap.setCurrentIndex(kidx)
                self.analog_keymap.blockSignals(False)
            self._update_analog_dir_visibility(kmode)

    def new_profile(self):
        name, ok = QInputDialog.getText(self, "New Profile", "Profile name:")
        if ok and name and name not in self.profiles:
            self.profiles[name] = {"keys": {}, "lighting": {"effect": "spectrum", "color": "#00FF00", "brightness": 80}, "analog_mode": "left", "analog_keymap": "off"}
            self.profile_combo.addItem(name)
            self.profile_combo.setCurrentText(name)
            save_profiles(self.profiles)

    def clone_profile(self):
        name, ok = QInputDialog.getText(self, "Clone Profile", "New profile name:")
        if ok and name and name not in self.profiles:
            self.profiles[name] = copy.deepcopy(self.profiles[self.current_profile])
            self.profile_combo.addItem(name)
            self.profile_combo.setCurrentText(name)
            save_profiles(self.profiles)

    def delete_profile(self):
        if len(self.profiles) <= 1:
            QMessageBox.warning(self, "Error", "Cannot delete the last profile.")
            return
        del self.profiles[self.current_profile]
        self.profile_combo.removeItem(self.profile_combo.currentIndex())
        save_profiles(self.profiles)

    def select_effect(self, effect):
        for e, btn in self.effect_buttons.items(): btn.setChecked(e == effect)
        self.profiles[self.current_profile]["lighting"]["effect"] = effect
        save_profiles(self.profiles)

    def pick_color(self):
        color = QColorDialog.getColor(self.color, self)
        if color.isValid(): self.set_color(color.name())

    def hex_color_changed(self): self.set_color(self.color_hex.text())

    def set_color(self, hex_color):
        try:
            self.color = QColor(hex_color)
            self.color_preview.setStyleSheet(f"background:{hex_color};border-radius:18px;border:2px solid #555;")
            self.color_hex.setText(hex_color)
            self.profiles[self.current_profile]["lighting"]["color"] = hex_color
            save_profiles(self.profiles)
        except Exception: pass

    def apply_lighting(self):
        if not self.tartarus_device:
            QMessageBox.warning(self, "No device", "Tartarus not connected via OpenRazer.")
            return
        lighting = self.profiles[self.current_profile].get("lighting", {})
        effect = lighting.get("effect", "spectrum")
        color = QColor(lighting.get("color", "#00FF00"))
        r, g, b = color.red(), color.green(), color.blue()
        try:
            self.tartarus_device.brightness = lighting.get("brightness", 80)
            fx = self.tartarus_device.fx
            if effect == "static":             fx.static(r, g, b)
            elif effect == "spectrum":         fx.spectrum()
            elif effect == "breath_single":    fx.breath_single(r, g, b)
            elif effect == "breath_random":    fx.breath_random()
            elif effect == "reactive":         fx.reactive(r, g, b, speed=1)
            elif effect == "wave":             fx.wave(1)
            elif effect == "starlight_single": fx.starlight_single(r, g, b, speed=1)
            elif effect == "starlight_random": fx.starlight_random(speed=1)
            elif effect == "ripple":           fx.ripple(r, g, b)
            elif effect == "none":             fx.none()
            QMessageBox.information(self, "Done", f"Applied {effect}!")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def is_autostart_enabled(self):
        return os.path.exists(AUTOSTART_FILE)

    def update_autostart_btn(self):
        if self.is_autostart_enabled():
            self.autostart_btn.setText("✅ Launch on login — click to disable")
        else:
            self.autostart_btn.setText("🚀 Enable launch on login")

    def toggle_autostart(self):
        if self.is_autostart_enabled():
            os.remove(AUTOSTART_FILE)
            QMessageBox.information(self, "Autostart", "Autostart disabled.")
        else:
            os.makedirs(AUTOSTART_DIR, exist_ok=True)
            script_path = os.path.abspath(sys.argv[0])
            desktop = f"""[Desktop Entry]
Type=Application
Name=OpenTartarus
Exec=python3 {script_path}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
X-KDE-Autostart-enabled=true
"""
            with open(AUTOSTART_FILE, "w") as f:
                f.write(desktop)
            QMessageBox.information(self, "Autostart", "OpenTartarus will launch on login.")
        self.update_autostart_btn()

class TartarusApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setStyle("Fusion")

        self.daemon = DaemonThread()
        self.daemon.status_changed.connect(self.on_daemon_status)
        self.daemon.warning_needed.connect(self.on_warning)
        self.daemon.start()

        self.window = TartarusWindow(self.daemon)

        self.tray = QSystemTrayIcon()
        self.tray.setIcon(make_tray_icon("#00CC00"))
        self.tray.setToolTip("Tartarus Pro Manager")
        self.tray.activated.connect(self.on_tray_activated)

        menu = QMenu()
        open_action = menu.addAction("⚙️  Open Tartarus Manager")
        open_action.triggered.connect(self.show_window)
        menu.addSeparator()

        self.profile_menu = menu.addMenu("🎮 Switch profile")
        self.rebuild_profile_menu()
        menu.addSeparator()

        quit_action = menu.addAction("❌ Quit")
        quit_action.triggered.connect(self.quit)

        self.tray.setContextMenu(menu)
        self.tray.show()
        self.tray.showMessage("Tartarus Pro Manager", "Running in the background.", QSystemTrayIcon.MessageIcon.Information, 2000)

    def on_warning(self, message):
        self.tray.showMessage(
            "OpenTartarus — Action Required",
            message,
            QSystemTrayIcon.MessageIcon.Warning,
            15000
        )
        print(f"[daemon] WARNING: {message}", flush=True)

    def rebuild_profile_menu(self):
        self.profile_menu.clear()
        profiles = load_profiles()
        active = get_active_profile_name(profiles)
        for name in profiles.keys():
            action = self.profile_menu.addAction(f"{'✓ ' if name == active else '   '}{name}")
            action.triggered.connect(lambda _, n=name: self.switch_profile(n))

    def switch_profile(self, name):
        set_active_profile_name(name)
        self.window.profile_combo.setCurrentText(name)
        self.rebuild_profile_menu()
        self.tray.showMessage("Tartarus Pro Manager", f"Profile switched to '{name}'.", QSystemTrayIcon.MessageIcon.Information, 1500)

    def on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window()

    def show_window(self):
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def on_daemon_status(self, msg):
        print(f"[daemon] {msg}", flush=True)

    def quit(self):
        self.daemon.stop()
        self.daemon.wait(3000)
        self.tray.hide()
        self.app.quit()

    def run(self):
        sys.exit(self.app.exec())

if __name__ == "__main__":
    TartarusApp().run()
