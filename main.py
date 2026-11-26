import atexit
import base64
import ctypes
import json
import os
import hashlib
import queue
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import types
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter import font as tkfont

import cv2
import mss
import numpy as np

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


class _MacKeyboard:
    """The few functions of the `keyboard` library this macro uses, on macOS (built on pynput).
    `keyboard` itself needs root there; this only needs the Accessibility / Input Monitoring
    permission. Key names are given in the keyboard library's style ('left shift', 'f8', ...)."""
    NAMES = {"shift": "left shift", "shift_l": "left shift", "shift_r": "right shift",
             "ctrl": "left ctrl", "ctrl_l": "left ctrl", "ctrl_r": "right ctrl",
             "alt": "left alt", "alt_l": "left alt", "alt_r": "right alt", "alt_gr": "right alt",
             "cmd": "left windows", "cmd_l": "left windows", "cmd_r": "right windows",
             "caps_lock": "caps lock", "page_up": "page up", "page_down": "page down",
             "print_screen": "print screen", "num_lock": "num lock", "scroll_lock": "scroll lock",
             "esc": "esc", "enter": "enter", "space": "space", "tab": "tab"}

    def __init__(self):
        self._listener = None
        self._pk = None
        self._entries = {}          # id -> (kind, spec, handler)
        self._next = 1
        self._down = set()          # names of the keys that are down now (modifiers without side)
        self._lock = threading.Lock()

    @staticmethod
    def _base(name):
        for side in ("left ", "right "):
            if name.startswith(side):
                return name[len(side):]
        return name

    def _name(self, key):
        if isinstance(key, self._pk.Key):
            return self.NAMES.get(key.name, key.name.replace("_", " "))
        ch = getattr(key, "char", None)
        return ch.lower() if ch else None

    def _ensure(self):
        if self._listener is None:
            from pynput import keyboard as pk      # imported late: only needed on macOS
            self._pk = pk
            self._listener = pk.Listener(on_press=self._on_press, on_release=self._on_release)
            self._listener.daemon = True
            self._listener.start()

    def _on_press(self, key):
        name = self._name(key)
        if not name:
            return
        base = self._base(name)
        self._down.add(base)
        event = types.SimpleNamespace(name=name, is_keypad=False)
        with self._lock:
            entries = list(self._entries.values())
        for kind, spec, handler in entries:
            try:
                if kind == "key" and spec == name:
                    handler(event)
                elif kind == "combo" and base in spec and spec <= self._down:
                    handler()
                elif kind == "any":
                    handler(event)
            except Exception:  # noqa: BLE001  (never let a callback kill the listener thread)
                pass

    def _on_release(self, key):
        name = self._name(key)
        if name:
            self._down.discard(self._base(name))
            event = types.SimpleNamespace(name=name, is_keypad=False)
            with self._lock:
                entries = list(self._entries.values())
            for kind, _spec, handler in entries:
                if kind == "anyup":
                    try:
                        handler(event)
                    except Exception:  # noqa: BLE001
                        pass

    def _add(self, kind, spec, handler):
        self._ensure()
        with self._lock:
            hid = self._next
            self._next += 1
            self._entries[hid] = (kind, spec, handler)
        return hid

    def add_hotkey(self, combo, handler):
        return self._add("combo", {self._base(k.strip().lower()) for k in combo.split("+")}, handler)

    def on_press_key(self, key, handler):
        return self._add("key", str(key).lower(), handler)

    def on_press(self, handler):
        return self._add("any", None, handler)

    def on_release(self, handler):
        return self._add("anyup", None, handler)

    def remove_hotkey(self, hid):
        self.unhook(hid)

    def unhook(self, hid):
        with self._lock:
            self._entries.pop(hid, None)

    def unhook_all(self):
        with self._lock:
            self._entries.clear()
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


if IS_WIN:
    import keyboard
    import pydirectinput
else:
    import pyautogui as pydirectinput       # same keyDown / keyUp / moveTo / moveRel / click calls
    keyboard = _MacKeyboard()

if IS_WIN:
    _orig_moveTo = pydirectinput.moveTo

    def _moveTo(x=None, y=None, *args, **kwargs):
        """pydirectinput only knows the PRIMARY monitor. A spot that is outside of it (a second
        monitor) is reached with SetCursorPos; spots on the primary monitor behave exactly as before."""
        try:
            u = ctypes.windll.user32
            pw, ph = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
            if x is not None and y is not None and not (0 <= x < pw and 0 <= y < ph):
                u.SetCursorPos(int(x), int(y))
                return
        except Exception:  # noqa: BLE001
            pass
        return _orig_moveTo(x, y, *args, **kwargs)
    pydirectinput.moveTo = _moveTo

# ----------------------------- SETTINGS ---------------------------------
VERSION = "v1.5.8"        # release version tag
                          # (shown in the footer, next to "Always on top")

# --- Update checker (footer > Updates) ---
UPDATE_REPO = "SvdXenos/Ymir"   # <-- your GitHub "owner/repo" (public). Releases are read from it
UPDATE_CHECK_ON_START = True       # look for a newer release a moment after the window opens
UPDATE_TIMEOUT = 15                # seconds before a GitHub request is given up

# --- Window logo (keep next to this script; falls back to the text "ymir" if missing) ---
LOGO_FILE = "ymir_logo_app.png"
ICON_FILE = "ymir.ico"   # window / taskbar icon (the lambda); skipped if missing
FILE_ICON_FILE = "ymir_file.ico"   # icon of the .ymir files in Explorer (only the lambda: white, black outline,
                                   # transparent). Lives in the image folder next to ymir.ico; skipped if missing

# --- Retry button detection ---
TEMPLATE_FILE = "retry_text.png"     # just the word RETRY (keep next to this script)
THRESHOLD = 0.90          # strict on purpose, so the Leave button can't match
ALT_RETRY_FILE = "alt_retry_button.png"   # optional: a picture of the WHOLE retry button, "RETRY (0/1)".
                                          # Only used as an extra check when retry_text.png finds nothing.
ALT_RETRY_THRESHOLD = 0.85   # the alt picture has the button colour around the word, so it is a bit looser
ALT_RETRY_WORD_COLS = (24, 100)   # the RETRY word only (x range inside the alt picture), because the
                                  # "(0/1)" counter changes; it is tried as well as the whole picture
SCAN_INTERVAL = 0.5       # seconds between screen checks for the retry button
FAST_SCAN_INTERVAL = 0.1  # the same in fast mode (Edit configs)
FAST_START_POLL = 0.05    # fast mode: seconds between looks for the mission-start icon (normally 0.2)
MONITOR_INDEX = 1         # 1 = primary monitor, 2 = second monitor, ...
SCALES = [1.0]            # extra sizes to try on top of the automatic resolution scaling (leave as is)
RETRY_SCALES = [0.80, 0.85, 0.90, 0.95, 1.0, 1.05, 1.10, 1.15, 1.20, 1.30]
                          # extra sizes tried ONLY for the retry button on screens that are not 1080p.
                          # Roblox buttons / text do not always grow exactly with the screen height, so
                          # the 1080p picture of the screen can show the button a bit bigger or smaller
                          # than retry_text.png. Whichever size fits best wins.
RETRY_DEBUG = False       # True = write the best retry score to the log every few seconds
                          # (to see how close a screen is to being detected)
BASE_HEIGHT = 1080        # all template images were taken at this screen height (1080p)
FORCE_SCALE = None        # None = automatic (screen height / 1080). Set e.g. 1.33 to force a scale
SCALE_THRESHOLD_RELAX = 0.05   # on a non-1080p screen every match threshold is lowered by this much
                               # (scaling the picture blurs it a little, so scores come out lower)
AFTER_CLICK_DELAY = 7     # seconds of doing NOTHING right after clicking retry
RETRY_CLICK_TIME = 0.8     # the retry button is clicked over and over this long (not just once)
RETRY_CLICK_INTERVAL = 0.1  # pause between those clicks
RETRY_CONFIRM_DELAY = 0.4 # re-check this long after first sighting (buttons fade in)
SAVE_CLICK_DEBUG = False   # saves last_retry_click.png showing exactly where it clicked

# --- Mission result detection (the gold COMPLETED / FAILED text on the end screen) ---
RESULT_FILES = {"completed": "result_completed.png", "failed": "result_failed.png"}
RESULT_THRESHOLD = 0.70   # lower it if results aren't detected

# --- Loot detection (looked for on the end screen, right before clicking retry) ---
LOOT_ENABLED = True
LOOT_FILES = {                       # kind -> template file (the icon only, no number)
    "scroll": "loot_scroll.png",
    "body": "loot_body.png",         # yellow fragment
    "defence": "loot_defence.png",   # blue fragment
    "offence": "loot_offence.png",   # red fragment
    "support": "loot_support.png",   # green fragment
    "vessel": "loot_vessel.png",     # Female Raid only (missions look for the scroll instead)
    "attack_serum": "loot_attack_serum.png",       # green
    "armor_serum": "loot_armor_serum.png",         # orange
    "female_serum": "loot_female_serum.png",       # blue
    "beast_serum": "loot_beast_serum.png",         # brown
    "colossal_serum": "loot_colossal_serum.png",   # red
}
FRAGMENT_KINDS = ("body", "defence", "offence", "support")
SERUM_KINDS = ("attack_serum", "armor_serum", "female_serum", "beast_serum", "colossal_serum")


def active_loot_kinds():
    """The loot kinds looked for with the config that is picked now: raids have vessels instead of
    scrolls (fragments and serums are looked for in both)."""
    raid = raid_counting()
    return [k for k in LOOT_FILES if k != ("scroll" if raid else "vessel")]
LOOT_THRESHOLD = 0.75     # lower it if loot is missed, raise it if you get false hits
LOOT_SCAN_TIME = 1.0      # seconds spent looking (items can fade in) before clicking retry
LOOT_SCAN_STEP = 0.25     # seconds between looks during that time
LOOT_FIX_SCALES = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15,
                   1.2, 1.3, 1.4, 1.5, 1.65, 1.8, 2.0]   # "Fix loot tracking": icon sizes tried
LOOT_FIX_RELAX = 0.05     # "Fix loot tracking": loot threshold is this much lower (resized icons are blurrier)
LOW_LOOT_SCAN_TIME = 3.0  # low quality only: the loot is looked for ONLY during this many seconds
                          # right after the retry click (nothing is looked for before the click)
SAVE_LOOT_DEBUG = False    # saves last_end_scan.png with boxes around the result + loot found

# --- Grabbed detection (the red YOUR STREAK IS IN DANGER text) ---
GRAB_ENABLED = True
GRAB_FILE = "grabbed_text.png"   # screenshot of just that text (keep next to this script)
GRAB_THRESHOLD = 0.70            # lower it if the grab isn't noticed, raise it for false hits
GRAB_REPEAT = 1.0                # while the text stays, press the key again after this many seconds

# --- Mission start detection (ability 2 icon) ---
START_TEMPLATE_FILE = "mission_start.png"
START_THRESHOLD = 0.80    # lower it if the icon isn't being detected
START_SETTLE_DELAY = 1.0  # extra wait after the icon appears, before ability 2
ICON_GONE_TIMEOUT = 20    # after retry, max seconds to wait for the old HUD to vanish

# --- Ability 4 cooldown (it can't be used until this long after ability 3) ---
ABILITY4_COOLDOWN = 15.0           # seconds of buying time after ability 3
ABILITY4_COOLDOWN_MARGIN = 0.3     # tiny extra, so ability 4 is surely off cooldown

# --- Stuck-mission rescue (a very rare obscure bug after the shift can leave the macro stuck) ---
MISSION_TIMEOUT = 120.0     # DEFAULT failsafe time (2 min); the live value is in Settings > Configs > Failsafe
MIN_FAILSAFE_SECONDS = 90   # the failsafe time can't be set lower than this
MAX_FAILSAFE_MINUTES = 60
RESCUE_CLICK_DELAY = 0.5    # seconds between the failsafe clicks
# Before clicking, the character is forced to look at the ground: press ctrl, move the mouse down
# for LOOK_DOWN_TIME seconds, press ctrl again.
# Before that look-down, ability 2 is ALWAYS spammed for STILL_SHIFTED_SPAM_TIME (it ejects the
# player if shifted; no icon check). The wait before looking down (STILL_SHIFTED_WAIT) starts
# STILL_SHIFTED_WAIT_START seconds after the spam started, so it runs partly during the spam.
# Then the macro looks down and tries to kill the player.
STILL_SHIFTED_SPAM_TIME = 5.0        # spam ability 2 this long
STILL_SHIFTED_SPAM_INTERVAL = 0.1    # seconds between those ability 2 presses
STILL_SHIFTED_WAIT_START = 2.0       # the wait below starts this many seconds after the spam started
STILL_SHIFTED_WAIT = 15.0            # wait this long before looking down
DEBUG_RESCUE_KEY = "f3+k"            # DEBUG: F3+K triggers the stuck-mission rescue by hand (remove later)
LOOK_UP_TIME = 0.5          # missions: the look up at the very start (ctrl, mouse up, ctrl) takes this long in total
LOOK_UP_PIXELS = 150        # ... and moves the mouse up this many pixels in total
LOOK_DOWN_KEY = "ctrl"
LOOK_DOWN_TIME = 2.0        # seconds the mouse is moved down
LOOK_DOWN_STEP = 0.01       # seconds between two small downward mouse movements
LOOK_DOWN_PIXELS = 15       # pixels moved down per step (relative movement, works in a locked mouse)

# --- Saved stats (kept between runs; the Reset button in the window erases them) ---
STATS_FILE = "aotr_stats.json"   # saved in Documents\Ymir Macro
CONFIG_FILE = "config.json"      # same folder: ONLY the timings changed in Edit configs (the overrides)
STATS_FOLDER = "Ymir Macro"      # folder created inside your Documents folder
OVERRIDE_FOLDER = "asset_override"   # inside Documents\Ymir Macro: the user's own pictures win over image/
IMAGE_FOLDER = "image"           # all images (templates, logo, icon) are loaded from this folder,
                                 # next to this script / .exe
LOW_FOLDER = "low quality fix"   # inside the image folder: the low graphics looks of the retry button,
                                 # the ability 2 icon and the loot (same file names as the normal ones)

# --- Mission routine timings ---
AFTER_ABILITY_DELAY = 0.6        # pause after ability 3 / 4
AFTER_STRAFE_DELAY = 0.3         # Shiki: pause after the strafe, before ability 3
BLOOD_STRAFE_LEFT_TIME = 8.0     # Blood Awakening: A is held this long after the movement, before the shift
AFTER_STRAFE_DELAY_FAST = 0.0    # Universal / Blood Awakening: go straight to ability 3 / 5 after the strafe
AFTER_ABILITY2_DELAY = 1.5       # pause after ability 2, before the dash sequence starts
DOUBLE_TAP_GAP = (0.06, 0.12)    # seconds between the two presses of ONE dash/strafe
STRAFE_DELAY = (0.15, 0.25)      # seconds between A strafes while buying time (~0.2)
SHIFT_SPAM_TIME = 5.0            # after ability 4: spam ability 5 for this long, so the shift is used for sure
SHIFT_SPAM_INTERVAL = 0.2        # seconds between those ability 5 presses
SHIFT_SPAM_EXTRA = 4.0           # keeps spamming ability 5 this much longer, but INSIDE the wait below:
                                 # the macro already counts the shift as used after SHIFT_SPAM_TIME and
                                 # starts the wait below, while still pressing 5 in the background
                                 # (5s + 4s = 9s of real spamming)
SHIFTED4_SPAM_AT = 17.0          # the shift takes ~22s; this many seconds after the 5 spam, start spamming
                                 # the shifted ability 4 (it runs past the 22s mark, that's intended)
SHIFTED4_SPAM_TIME = 8.0         # spam the shifted ability 4 for this long
SHIFTED4_SPAM_INTERVAL = 0.2     # seconds between those ability 4 presses
SHIFTED_4_TO_5_DELAY = 3.0       # after the shifted ability 4 spam, wait this long, then ability 5
ABILITY_5_CHECK_INTERVAL = 1.0   # how often to press 5 (set to its cooldown
                                 # if pressing early causes problems)

# --- Default keys (change them in the Settings window; your choice is saved in STATS_FILE) ---
KEY_ABILITY_1ST = "2"
KEY_ABILITY_2ND = "3"
KEY_ABILITY_3RD = "4"
KEY_ABILITY_LAST = "5"
KEY_DASH = "space"
KEY_STRAFE = "a"
KEY_STRAFE_D = "d"    # the other strafe direction (Universal family)
KEY_GRAB = "1"       # pressed when you get grabbed
KEY_BACK = "s"
# moves rotated (each one double-tapped) while waiting for ability 4: just A, spammed
BUY_TIME_MOVES = [KEY_STRAFE]

# --- Default hotkeys (also changeable in the Settings window) ---
START_KEY = "f8"
PAUSE_KEY = "f6"
QUIT_KEY = "f7"   # stops the macro (the window stays open)

# --- Family "Shiki": ability 4 icon during the buying time ---
# While strafing A after ability 3, the ability 4 icon (shiki_ability4.png, same two folders) is
# watched. The moment it shows up, ability 4 is spammed for SHIKI_ABILITY4_SPAM_TIME and the buying
# time is over (the plain ABILITY4_COOLDOWN timer stays as a fallback if the icon is never seen).
SHIKI_ABILITY4_FILE = "shiki_ability4.png"
SHIKI_ABILITY4_THRESHOLD = 0.80      # lower it if the icon isn't being detected
SHIKI_ABILITY4_CHECK_INTERVAL = 0.25 # seconds between two looks for the icon
SHIKI_ABILITY4_CHECK_AFTER = 3.0     # start looking for the icon this many seconds after ability 3
SHIKI_ABILITY4_SPAM_TIME = 2.0       # spam ability 4 this long once the icon is seen
SHIKI_ABILITY4_SPAM_INTERVAL = 0.1   # seconds between those ability 4 presses

# --- Movement (every config): dashing copied from a recorded macro, Space spammed non-stop ---
UNIVERSAL_DASH_TIME = 8.2         # seconds of spamming
SHIFT_LOCK_FIX_HOLD = 0.06        # Movement > Fix shift lock at start: how long ctrl is held
SHIFT_LOCK_FIX_SETTLE = 0.15      # ... and the pause after it, before anything else happens
UNIVERSAL_DASH_HOLD = (0.065, 0.09)  # how long each Space press is held
UNIVERSAL_DASH_PRESS_GAP = (0.05, 0.08)  # pause between two presses
UNIVERSAL_ABILITY4_SPAM_TIME = 3.5      # Universal: spam ability 4 this long after looking down
UNIVERSAL_ABILITY4_SPAM_INTERVAL = 0.1  # seconds between those ability 4 presses
# Universal family: right after ability 3: ctrl, mouse dragged down (LOOK_DOWN_TIME), ctrl again (forces the player to look down),
# then ability 4 spammed for UNIVERSAL_ABILITY4_SPAM_TIME, then the shifting sequence goes on as usual (no buying time, no icon check)

# --- Settings window ---
MINI_POS_OPTIONS = ("Left", "Middle", "Right")      # where the mini stats bar sits on Roblox
DEFAULT_MINI_POS = "Middle"
MINI_POS_BADGES = {
    "Left":   ("L", "#3b82f6", "#ffffff"),
    "Middle": ("M", "#a855f7", "#ffffff"),
    "Right":  ("R", "#14b8a6", "#ffffff"),
}
MOVE_SHIFTED_OPTIONS = ("Left", "Right", "Stand")   # "Move while shifted" (step 9)
DEFAULT_MOVE_SHIFTED = "Right"
MOVE_SHIFTED_BADGES = {
    "Left":  ("L", "#3b82f6", "#ffffff"),
    "Right": ("R", "#14b8a6", "#ffffff"),
    "Stand": ("S", "#7a8398", "#ffffff"),
}
FAMILIES = ("Shiki", "Universal", "Blood Awakening", "Female Raid")
DEFAULT_FAMILY = "Shiki"
FAMILY_LABELS = {f: f for f in FAMILIES}   # what the dropdown shows
FAMILY_TAGS = {}   # entry -> tag text in the dropdown ("WIP" yellow, "Beta" blue), e.g. {"Universal": "Beta"}
# icon next to each entry: (letter, badge colour, letter colour)
FAMILY_BADGES = {
    "Shiki":           ("S", "#3b82f6", "#ffffff"),   # blue S
    "Universal":       ("U", "#facc15", "#1a1500"),   # yellow U
    "Blood Awakening": ("B", "#ef4444", "#ffffff"),   # red B
    "Female Raid":     ("F", "#ef4444", "#ffffff"),   # red F
}

# --- Family "Female Raid": pictures it needs (same two folders / asset_override rules as the others) ---
RAID_SKIP_FILE = "skip.png"          # the skip button
RAID_START_FILE = "raid_start.png"   # shows up when the raid starts: starts the raid and the buffs (ability 1, 3, 5)
RAID_START_THRESHOLD = 0.80
RAID_FINISH_FILE = "finish_button.png"   # the finish button
RAID_HEATWAVE_FILE = "heatwave.png"      # seen after the Nth skip -> ability 1 is spammed
RAID_HEATWAVE_THRESHOLD = 0.80
KEY_RAID_SKIP = "e"              # pressed over and over while the skip button is on screen
RAID_SKIP_THRESHOLD = 0.67       # (skip button lowered from 0.80 - it was detected late) lower them if the buttons aren't being detected
RAID_FINISH_THRESHOLD = 0.80
RAID_SKIP_FIRST_SPAM_TIME = 0.5  # the FIRST skip button: spam E this long (fixed)
RAID_SKIP_SPAM_TIME = 2.0        # every skip button after the first: spam E this long (runs in the background, alongside other actions)
RAID_SKIP_SPAM_INTERVAL = 0.1    # seconds between those E presses
RAID_SKIP_NUMBER = 3             # after this skip button (the first one = no. 1) the macro waits for heatwave.png
RAID_HEATWAVE_TIME = 6.0         # after the Nth skip the macro waits for heatwave.png, then spams ability 1 this long
RAID_HEATWAVE_INTERVAL = 0.1
RAID_A3_TIME = 2.5               # opening: after ability 1, spam ability 3 this long
RAID_A3_INTERVAL = 0.1
RAID_A5_TIME = 3.0               # then ability 5 this long
RAID_A5_INTERVAL = 0.1
RAID_A5_EXTRA = 3.0               # ability 5 keeps being spammed this long INSIDE the wait below (the shift is still starting)
RAID_WAIT_TIME = 17.0            # then wait this long
RAID_OPEN_A1_TIME = 2.0          # opening: ability 1 is spammed this long first
RAID_A1_TIME = 4.0               # then spam ability 1 this long
RAID_A1_INTERVAL = 0.1
RAID_RANDOM_GAP = 2.0            # then ability 4 -> 3 -> 2 (over and over), one every this many seconds, until the raid ends
RAID_HEATWAVE_REPEAT = 16.0      # after a heatwave ability 1 spam: seeing heatwave.png again only counts after this many seconds
RAID_CHEST_CLICK_TIME = 2.0      # a chest is clicked over and over this long ...
RAID_CHEST_CLICK_INTERVAL = 0.1
RAID_FINISH_WAIT = 30.0          # ... then the macro waits (at most) this long for the finish button to show again ("Opening chest")
RAID_FINISH_CLICK_TIME = 2.0     # the finish button is clicked over and over this long
RAID_FINISH_CLICK_INTERVAL = 0.1
RAID_FAILSAFE_AFTER_SEQUENCE = 180.0   # raid failsafe (fixed, not the Failsafe timer): this long after the shifting + waiting sequence ...
RAID_FAILSAFE_MAX_TIME = 180.0         # ... or when the raid has lasted this long (3 min), whichever comes first
RAID_FINISH_LOOK = 0.1          # seconds between two looks for the finish button while a chest opens (not editable)
RAID_LOOP_DELAY = 0.02           # seconds between two checks while waiting during a raid (not editable)
RAID_LOOK_INTERVAL = 0.05        # seconds between two looks for the skip / finish button (not editable)


# --- Edit configs (Settings > Configs > Abilities > Edit configs) ---
# Every timing of the routine can be changed there, per part: Movement (shared by all) and the three
# ability configs. The constants above are the DEFAULTS; only values the user changed are saved
# (settings["timings"]). field = (key, label, kind, default, low, high, group, hint)
#   kind: "sec" = seconds | "range" = (min, max) seconds, a random value in between each time
#         "pix" = whole pixels | "num" = plain number | "bool" = on/off switch (low / high unused)
#         "spot" = a screen coordinate (x, y) picked with the picker, default None = not set
#                  (low / high = allowed pixel range)
def _f(key, label, kind, default, lo, hi, group, hint=""):
    return (key, label, kind, default, lo, hi, group, hint)


def _skip_ability2_field():
    return _f("skip_ability2", "Skip ability 2", "bool", False, 0, 1, "Start",
              "On: the mission starts without ability 2 (and without the wait after it). The ability 2 "
              "of the stuck-mission rescue is not affected.")


def _fast_mode_field(on=False):
    return _f("fast_mode", "Fast mode", "bool", on, 0, 1, "Fast mode",
              "On: retry is clicked the moment it is seen (checked every 0.1s, no confirmation look), the "
              "win/loss and loot are logged right after from the screenshot taken then, and the next "
              "mission starts without the extra wait after its start icon shows. Loot that only fades in "
              "after the retry button can be missed.")


def _shift_fields():
    """The shifting sequence (ability 5 shift, wait, shifted ability 4, ability 5) - every config has its own copy."""
    g = "Shift sequence"
    return [
        _f("shift_spam_time", "Ability 5 spam time", "sec", SHIFT_SPAM_TIME, 0, 60, g,
           "How long ability 5 is spammed so the shift is used."),
        _f("shift_spam_interval", "Ability 5 spam interval", "sec", SHIFT_SPAM_INTERVAL, 0.02, 5, g,
           "Pause between those presses."),
        _f("shift_spam_extra", "Extra spam inside the wait", "sec", SHIFT_SPAM_EXTRA, 0, 60, g,
           "Ability 5 keeps being pressed quietly this long, inside the wait below."),
        _f("shifted4_at", "Wait for the shift", "sec", SHIFTED4_SPAM_AT, 0, 120, g,
           "Seconds after the ability 5 spam before the shifted ability 4 starts."),
        _f("shifted4_time", "Shifted ability 4 spam time", "sec", SHIFTED4_SPAM_TIME, 0, 60, g,
           "How long the shifted ability 4 is spammed."),
        _f("shifted4_interval", "Shifted ability 4 interval", "sec", SHIFTED4_SPAM_INTERVAL, 0.02, 5, g,
           "Pause between those presses."),
        _f("shifted4_to5", "Wait before ability 5", "sec", SHIFTED_4_TO_5_DELAY, 0, 60, g,
           "Pause after the shifted ability 4, before the last ability 5 loop."),
        _f("ability5_interval", "Ability 5 press interval", "sec", ABILITY_5_CHECK_INTERVAL, 0.05, 30,
           "Last step", "How often ability 5 is pressed (while holding the move key) until the retry button shows up."),
    ]


CONFIG_SECTIONS = ("Movement",) + FAMILIES
CONFIG_FIELDS = {
    "Movement": [
        _f("look_up", "Look up at the start", "bool", False, 0, 1, "Look up",
           "Missions: the very first thing - ctrl, the mouse moves up a bit, ctrl again."),
        _f("look_up_time", "Look up total time", "sec", LOOK_UP_TIME, 0.2, 5, "Look up",
           "The whole look up (both ctrl presses and the mouse move) takes this long."),
        _f("look_up_pixels", "Look up amount", "pix", LOOK_UP_PIXELS, 1, 2000, "Look up",
           "How far the mouse moves up, in pixels (relative movement, works in a locked mouse)."),
        _f("after_ability2", "Wait after ability 2", "sec", AFTER_ABILITY2_DELAY, 0, 30, "Start",
           "Nothing else happens during this wait, then the dashing starts."),
        _f("dash_time", "Dash spam time", "sec", UNIVERSAL_DASH_TIME, 0, 60, "Dashing",
           "How long Space is spammed."),
        _f("dash_hold", "Hold per press", "range", UNIVERSAL_DASH_HOLD, 0.01, 1, "Dashing",
           "How long each Space press is held (min - max, random in between)."),
        _f("dash_gap", "Gap between presses", "range", UNIVERSAL_DASH_PRESS_GAP, 0, 1, "Dashing",
           "Pause between two presses (min - max, random in between)."),
        _f("tap_gap", "Strafe double-tap gap", "range", DOUBLE_TAP_GAP, 0, 1, "Strafe",
           "Pause between the two presses of one strafe, like A-A (min - max)."),
        _f("retry_click_time", "Retry click spam time", "sec", RETRY_CLICK_TIME, 0.05, 10, "Retry button",
           "The retry button is clicked over and over this long instead of once."),
        _f("retry_click_interval", "Retry click interval", "sec", RETRY_CLICK_INTERVAL, 0.02, 2,
           "Retry button", "Pause between those clicks."),
        _f("fix_shift_lock", "Fix shift lock at start", "bool", False, 0, 1, "Shift lock",
           "On: ctrl is pressed once the moment the mission starts (before anything else)."),
    ],
    "Shiki": [
        _skip_ability2_field(),
        _fast_mode_field(),
        _f("after_strafe", "Wait after strafe", "sec", AFTER_STRAFE_DELAY, 0, 10, "Ability 3",
           "Pause between the strafe and ability 3."),
        _f("cooldown", "Buying time", "sec", ABILITY4_COOLDOWN, 0, 120, "Buying time",
           "Seconds of strafing after ability 3 (the ability 4 cooldown)."),
        _f("cooldown_margin", "Cooldown margin", "sec", ABILITY4_COOLDOWN_MARGIN, 0, 10, "Buying time",
           "Tiny extra so ability 4 is surely off cooldown."),
        _f("strafe_gap", "Gap between strafes", "range", STRAFE_DELAY, 0, 5, "Buying time",
           "Pause between the strafes while buying time (min - max)."),
        _f("icon_after", "Look for the icon after", "sec", SHIKI_ABILITY4_CHECK_AFTER, 0, 120,
           "Ability 4 icon", "Start looking for the ability 4 icon this long after ability 3."),
        _f("icon_interval", "Icon check interval", "sec", SHIKI_ABILITY4_CHECK_INTERVAL, 0.05, 10,
           "Ability 4 icon", "Seconds between two looks for the icon."),
        _f("icon_threshold", "Icon match threshold", "num", SHIKI_ABILITY4_THRESHOLD, 0.3, 1,
           "Ability 4 icon", "Lower it if the icon isn't being detected."),
        _f("ability4_time", "Ability 4 spam time", "sec", SHIKI_ABILITY4_SPAM_TIME, 0, 30, "Ability 4",
           "How long ability 4 is spammed once the icon is seen."),
        _f("ability4_interval", "Ability 4 spam interval", "sec", SHIKI_ABILITY4_SPAM_INTERVAL, 0.02, 5,
           "Ability 4", "Pause between those presses."),
        _f("after_ability", "Wait after ability 4", "sec", AFTER_ABILITY_DELAY, 0, 10, "Ability 4",
           "Pause before the shift sequence starts."),
    ] + _shift_fields(),
    "Universal": [
        _skip_ability2_field(),
        _fast_mode_field(),
        _f("after_strafe", "Wait after strafe", "sec", AFTER_STRAFE_DELAY_FAST, 0, 10, "Ability 3",
           "Pause between the strafe and ability 3."),
        _f("look_time", "Look down time", "sec", LOOK_DOWN_TIME, 0, 20, "Look down",
           "Seconds the mouse is dragged down (ctrl, mouse down, ctrl)."),
        _f("look_pixels", "Pixels per step", "pix", LOOK_DOWN_PIXELS, 1, 200, "Look down",
           "Pixels the mouse moves down every 0.01 s."),
        _f("ability4_time", "Ability 4 spam time", "sec", UNIVERSAL_ABILITY4_SPAM_TIME, 0, 30, "Ability 4",
           "How long ability 4 is spammed after looking down."),
        _f("ability4_interval", "Ability 4 spam interval", "sec", UNIVERSAL_ABILITY4_SPAM_INTERVAL, 0.02, 5,
           "Ability 4", "Pause between those presses."),
        _f("after_ability", "Wait after ability 4", "sec", AFTER_ABILITY_DELAY, 0, 10, "Ability 4",
           "Pause before the shift sequence starts."),
    ] + _shift_fields(),
    "Blood Awakening": [
        _skip_ability2_field(),
        _fast_mode_field(),
        _f("strafe_left_time", "Strafe left time", "sec", BLOOD_STRAFE_LEFT_TIME, 0, 60, "Start of the shift",
           "After the movement, A is strafed (double-tapped over and over, like Shiki's buying time) "
           "this long, then the ability 5 shift starts. 0 = off."),
        _f("strafe_gap", "Gap between strafes", "range", STRAFE_DELAY, 0, 5, "Start of the shift",
           "Random pause between two strafes while strafing left."),
    ] + _shift_fields(),
    "Female Raid": [
        _f("chest1", "Chest 1", "spot", None, 0, 20000, "Chests",
           "Screen coordinate of chest 1. Pick it with the Pick button (click on the chest) - the raid "
           "needs it."),
        _f("chest2", "Chest 2", "spot", None, 0, 20000, "Chests",
           "Screen coordinate of chest 2."),
        _f("open_chest2", "Open chest 2", "bool", True, 0, 1, "Chests",
           "On: chest 2 is opened too. Off: only chest 1 is opened."),
        _f("chest_click_time", "Click spam time", "sec", RAID_CHEST_CLICK_TIME, 0, 30, "Chests",
           "A chest is clicked over and over this long."),
        _f("chest_click_interval", "Click spam interval", "sec", RAID_CHEST_CLICK_INTERVAL, 0.02, 5,
           "Chests", "Pause between those clicks."),
        _f("finish_wait", "Max wait for finish button", "sec", RAID_FINISH_WAIT, 1, 300, "Chests",
           "After clicking a chest the macro looks for finish_button.png - when it shows up the "
           "chest is open. This is the longest it waits for it (the status card says Opening chest)."),
        _f("finish_click_time", "Click spam time", "sec", RAID_FINISH_CLICK_TIME, 0, 30,
           "Finish button", "The finish button is clicked over and over this long."),
        _f("finish_click_interval", "Click spam interval", "sec", RAID_FINISH_CLICK_INTERVAL, 0.02, 5,
           "Finish button", "Pause between those clicks."),
        _fast_mode_field(True),     # on by default for the raid
        _f("skip_spam_time", "E spam time", "sec", RAID_SKIP_SPAM_TIME, 0, 30, "Skip button",
           "How long E is spammed every time the skip button is seen."),
        _f("skip_spam_interval", "E spam interval", "sec", RAID_SKIP_SPAM_INTERVAL, 0.02, 5, "Skip button",
           "Pause between those E presses."),
        _f("skip_number", "Wait for heatwave after skip no.", "num", RAID_SKIP_NUMBER, 2, 50,
           "Heatwave",
           "After this skip button (the first one, that marks the raid's start, is no. 1) the macro "
           "does ability 3 once and looks for heatwave.png; once it is seen ability 1 is spammed."),
        _f("heatwave_time", "Ability 1 spam time", "sec", RAID_HEATWAVE_TIME, 0, 60, "Heatwave",
           "How long ability 1 is spammed once heatwave.png is seen."),
        _f("heatwave_interval", "Ability 1 spam interval", "sec", RAID_HEATWAVE_INTERVAL, 0.02, 5,
           "Heatwave", "Pause between those presses."),
        _f("heatwave_repeat", "Pause before it can repeat", "sec", RAID_HEATWAVE_REPEAT, 0, 300,
           "Heatwave",
           "After the 3rd skip, every time heatwave.png is seen the ability 1 spam is done (ability 4 "
           "once first). This many seconds must have passed since the last one ended. 0 = no pause."),
        _f("open_a1_time", "First ability 1 spam time", "sec", RAID_OPEN_A1_TIME, 0, 60, "Buffing / pre-shift",
           "After the first skip: ability 1 is spammed this long first (uses the ability 1 spam interval below)."),
        _f("a3_time", "Ability 3 spam time", "sec", RAID_A3_TIME, 0, 60, "Buffing / pre-shift",
           "Then ability 3 is spammed this long."),
        _f("a3_interval", "Ability 3 spam interval", "sec", RAID_A3_INTERVAL, 0.02, 5, "Buffing / pre-shift",
           "Pause between those presses."),
        _f("a5_time", "Ability 5 spam time", "sec", RAID_A5_TIME, 0, 60, "Buffing / pre-shift",
           "Then ability 5 is spammed this long."),
        _f("a5_interval", "Ability 5 spam interval", "sec", RAID_A5_INTERVAL, 0.02, 5, "Buffing / pre-shift",
           "Pause between those presses."),
        _f("a5_extra", "Extra ability 5 spam while shifting", "sec", RAID_A5_EXTRA, 0, 60, "Buffing / pre-shift",
           "Ability 5 keeps being pressed this long inside the wait below (the shift is still starting)."),
        _f("wait_time", "Wait before ability 1", "sec", RAID_WAIT_TIME, 0, 120, "Opening",
           "Pause after the ability 5 spam (the extra ability 5 spam above runs inside it)."),
        _f("a1_time", "Ability 1 spam time", "sec", RAID_A1_TIME, 0, 60, "Opening",
           "Then ability 1 is spammed this long."),
        _f("a1_interval", "Ability 1 spam interval", "sec", RAID_A1_INTERVAL, 0.02, 5, "Opening",
           "Pause between those presses."),
        _f("random_gap", "Gap between abilities", "sec", RAID_RANDOM_GAP, 0.05, 60,
           "Ability cycle",
           "After the opening ability 4, 3, 2 are pressed one after the other (then again from 4), "
           "one this often, until the raid ends."),
    ],
}
CONFIG_INDEX = {sec: {f[0]: f for f in fields} for sec, fields in CONFIG_FIELDS.items()}
# fields that live in Settings > Configs instead of the Edit configs window (still saved in the same place)
CONFIG_HIDDEN = {"Female Raid": ("chest1", "chest2")}
CONFIG_BADGES = dict(FAMILY_BADGES, Movement=("M", "#a78bfa", "#1a1033"))   # purple M
CONFIG_INFO = {
    "Movement": "Used by every config: the wait after ability 2, the dashing and the strafe.",
    "Shiki": "Used while the Shiki abilities are selected.",
    "Universal": "Used while the Universal abilities are selected.",
    "Blood Awakening": "Used while the Blood Awakening abilities are selected.",
    "Female Raid": "Used while Female Raid is selected. (The chest locations are set in Settings > Configs.)",
}


def clean_cfg_value(spec, raw):
    """A saved / typed value checked against its field's limits. Returns the value (a float, an
    int for pixels, a (min, max) tuple for ranges) or None when it is not usable."""
    kind, lo, hi = spec[2], spec[4], spec[5]
    if kind == "bool":
        return raw if isinstance(raw, bool) else None
    if kind == "spot":
        if (isinstance(raw, (list, tuple)) and len(raw) == 2
                and all(isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi for v in raw)):
            return (int(raw[0]), int(raw[1]))
        return None

    def num(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:   # v != v: NaN
            return None
        if kind == "pix":
            return int(v) if float(v).is_integer() and lo <= v <= hi else None
        return float(v) if lo <= v <= hi else None

    if kind == "range":
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            a, b = num(raw[0]), num(raw[1])
            if a is not None and b is not None and a <= b:
                return (a, b)
        return None
    return num(raw)


def parse_cfg_text(spec, texts):
    """The text(s) of the entry box(es) -> a checked value, or None when it is not valid."""
    nums = []
    for t in texts:
        try:
            nums.append(float(t.strip().replace(",", ".")))
        except ValueError:
            return None
    return clean_cfg_value(spec, nums if spec[2] == "range" else nums[0])


def parse_spot_text(spec, texts):
    """The X / Y boxes of a coordinate row -> (valid, value). Both empty = not set = (True, None)."""
    if not any(t.strip() for t in texts):
        return True, None
    try:
        nums = [int(t.strip()) for t in texts]
    except ValueError:
        return False, None
    val = clean_cfg_value(spec, nums)
    return val is not None, val


def tv(section, key):
    """The live value of one editable timing: the user's saved value, else its default.
    A key the config doesn't have (e.g. fast_mode / skip_ability2 on Female Raid) reads as off."""
    spec = CONFIG_INDEX.get(section, {}).get(key)     # a custom macro has no timings: reads as off
    if spec is None:
        return False
    val = settings["timings"].get(section, {}).get(key)
    if val is None:
        val = spec[3]
    if spec[2] in ("range", "spot"):
        return tuple(val) if val is not None else None
    return val


# --- Themes (Settings > Themes). Every theme = 5 base colours (the rest is blended from
# them) + optional hand-picked overrides + style (font, corner radius, card outline).
DEFAULT_THEME = "Default"
_BASE_STYLE = dict(FONT="Segoe UI", MONO="Consolas",
                   R_TILE=10,      # corner radius of the buttons / tiles
                   R_PILL=None,    # corner radius of the status pill (None = fully round)
                   R_SEG=3,        # corner radius of the step bar
                   R_SWITCH=10,    # roundness of the on/off switches (10 = fully round)
                   CARD_BW=1)      # card outline: 1 = thin line, 0 = borderless
THEMES = {
    "Default": dict(
        colors=("#0b0e14", "#11151e", "#e6e9f2", "#7a8398", "#3b82f6"),   # bg, card, text, sub, accent
        over={
            "BORDER": "#1a2030", "LOGBG": "#0f121a", "FIELD": "#1a1e29", "FIELD_EDGE": "#2a3042",
            "FIELD_HOVER": "#212637", "FIELD_EDGE_HOVER": "#3a4156", "ACCENT_BG": "#16254a",
            "ACCENT_BG_HOVER": "#1d3266", "ACCENT_LINE": "#3b6fe0", "ACCENT_BADGE": "#3b5cb0",
            "ACCENT_TEXT": "#9db8ff", "ACCENT_SOFT": "#7aa7ff", "STEP_DONE": "#2f55b8",
            "STEP_PENDING": "#232a3b", "SCROLL": "#262c3d", "SCROLL_HOVER": "#323a52",
            "DIS_FILL": "#13161e", "DIS_EDGE": "#1b202c", "DIS_TEXT": "#4b5264",
            "DIS_BADGE": "#232837", "DIS_ICON": "#3c4254", "BADGE_HOVER": "#4a5270",
            "BADGE_FG": "#9aa3b8", "BADGE_FG_HOVER": "#b4bcd0", "LOG_TS": "#586073",
            "SWITCH_OFF": "#272e40",
            "PILLS": {"idle": ("Idle", "#1d2230", "#9aa3b8"),
                      "waiting": ("Waiting", "#2e2410", "#fbbf24"),
                      "running": ("Running", "#0f2a22", "#34d399"),
                      "retrying": ("Retrying", "#14243f", "#60a5fa"),
                      "paused": ("Paused", "#251d3f", "#a78bfa")},
        },
        style={}),
    "Violet": dict(
        colors=("#0e0b17", "#161225", "#ece8f8", "#8a82a8", "#8b5cf6"),
        over={}, style=dict(R_TILE=12)),
    "Rose": dict(
        colors=("#140c10", "#1f1319", "#f7e9ee", "#a88994", "#f472b6"),
        over={}, style=dict(R_TILE=16, CARD_BW=0)),
    "Ember": dict(
        colors=("#12100d", "#1b1814", "#f1ebe2", "#9a8f80", "#f97316"),
        over={}, style=dict(FONT="Bahnschrift", R_TILE=6, R_SEG=1, R_SWITCH=6)),
    "Terminal": dict(
        colors=("#050806", "#0a110c", "#c7f9d4", "#5f8f6f", "#22c55e"),
        over={"BORDER": lambda P: _mix(P["CARD"], P["ACCENT"], 0.35),
              "FIELD_EDGE": lambda P: _mix(P["CARD"], P["ACCENT"], 0.3)},
        style=dict(FONT="Consolas", R_TILE=2, R_PILL=2, R_SEG=0, R_SWITCH=2)),
    "Ocean": dict(
        colors=("#08111a", "#0d1a27", "#e3f1f7", "#7e97a8", "#06b6d4"),
        over={}, style=dict(R_TILE=8, CARD_BW=0)),
    "Crimson": dict(
        colors=("#110a0b", "#1a1012", "#f5e8e9", "#a38588", "#ef4444"),
        over={}, style=dict(R_TILE=8, R_SEG=2)),
    "Gold": dict(
        colors=("#110e08", "#1a150c", "#f6efdc", "#a39777", "#eab308"),
        over={"BORDER": lambda P: _mix(P["CARD"], "#ffffff", 0.8),          # white outlines
              "FIELD_EDGE": lambda P: _mix(P["CARD"], "#ffffff", 0.7),
              "FIELD_EDGE_HOVER": "#ffffff"},
        style=dict(R_TILE=10, R_SEG=3, CARD_BW=1)),
    "Midnight": dict(
        colors=("#070a1a", "#0d1229", "#e4e8fb", "#7f89b5", "#818cf8"),
        over={}, style=dict(R_TILE=14, CARD_BW=0, R_SEG=4)),
    "Mono": dict(
        colors=("#0a0a0a", "#141414", "#ededed", "#8a8a8a", "#d4d4d4"),
        over={}, style=dict(FONT="Bahnschrift", R_TILE=4, R_SEG=1, R_SWITCH=4)),
}


def _mix(a, b, t):
    """Blend colour a towards colour b by t (0..1). Colours are '#rrggbb'."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ca, cb))


_font_names = None


def _font_or(family, fallback="Segoe UI"):
    """The font if this PC has it, otherwise the fallback (so a theme never looks broken)."""
    global _font_names
    if _font_names is None:
        try:
            from tkinter import font as tkfont
            _font_names = {f.lower() for f in tkfont.families()}
        except Exception:
            _font_names = set()
    return family if (not _font_names or family.lower() in _font_names) else fallback


_palette_cache = {}


def build_palette(name):
    """All colours + style values of one theme, as a dict (keys become App.<KEY>). Built once per
    theme, then reused (the theme cards ask for every theme each time the UI tab is built)."""
    hit = _palette_cache.get(name)
    if hit is not None:
        return hit
    spec = THEMES.get(name) or THEMES[DEFAULT_THEME]
    bg, card, text, sub, accent = spec["colors"]
    m = _mix
    field = m(card, text, 0.05)
    acc_bg = m(card, accent, 0.22)
    soft = m(accent, "#ffffff", 0.3)
    P = {
        "BG": bg, "CARD": card, "TEXT": text, "SUB": sub, "ACCENT": accent,
        "BORDER": m(card, text, 0.07), "LOGBG": m(bg, card, 0.65),
        "FIELD": field, "FIELD_EDGE": m(card, text, 0.12), "FIELD_HOVER": m(card, text, 0.09),
        "FIELD_EDGE_HOVER": m(card, text, 0.18),
        "ACCENT_BG": acc_bg, "ACCENT_BG_HOVER": m(card, accent, 0.32),
        "ACCENT_LINE": m(accent, "#000000", 0.1), "ACCENT_BADGE": m(card, accent, 0.65),
        "ACCENT_TEXT": m(accent, "#ffffff", 0.45), "ACCENT_SOFT": soft,
        "STEP_DONE": m(card, accent, 0.75), "STEP_PENDING": m(card, text, 0.09),
        "SCROLL": m(card, text, 0.12), "SCROLL_HOVER": m(card, text, 0.2),
        "DIS_FILL": m(card, bg, 0.3), "DIS_EDGE": m(card, text, 0.08),
        "DIS_TEXT": m(card, text, 0.28), "DIS_BADGE": m(card, text, 0.1),
        "DIS_ICON": m(card, text, 0.22), "BADGE_HOVER": m(card, text, 0.25),
        "BADGE_FG": m(card, text, 0.63), "BADGE_FG_HOVER": m(card, text, 0.76),
        "LOG_TS": m(card, text, 0.35), "SWITCH_OFF": m(card, text, 0.12), "KNOB": "#ffffff",
        "PILLS": {"idle": ("Idle", field, m(card, text, 0.63)),
                  "waiting": ("Waiting", m(card, "#fbbf24", 0.16), "#fbbf24"),
                  "running": ("Running", m(card, "#34d399", 0.16), "#34d399"),
                  "retrying": ("Retrying", acc_bg, soft),
                  "paused": ("Paused", m(card, "#a78bfa", 0.16), "#a78bfa")},
    }
    for key, value in spec["over"].items():
        P[key] = value(P) if callable(value) else value
    P.update(_BASE_STYLE)
    P.update(spec["style"])
    P["FONT"] = _font_or(P["FONT"])
    P["MONO"] = _font_or(P["MONO"], "Courier New")
    _palette_cache[name] = P
    return P

MAX_GRAPH_HOURS = 24             # the stats graph interval can be 1 .. this many hours
WEBHOOK_TIMEOUT = 15             # seconds before a Discord request is given up
# -------------------------------------------------------------------------

if IS_WIN:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

pydirectinput.PAUSE = 0.0
pydirectinput.FAILSAFE = True


def app_dir():
    """Folder of the .exe (when compiled) or of this script. Templates, stats and
    debug screenshots live here, so they survive between runs."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CHANGELOG_FILE = "change.md"     # shown by footer > Changelog; add it to the build: --add-data "change.md;."


def changelog_text():
    """The text of change.md: bundled inside the .exe (PyInstaller --add-data), or next to the script."""
    for base in (getattr(sys, "_MEIPASS", None), app_dir()):
        if not base:
            continue
        path = os.path.join(base, CHANGELOG_FILE)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            continue
    return f"# Changelog\n\n{CHANGELOG_FILE} was not found next to the macro."


def asset_path(filename):
    """Images are loaded from the "image" folder. One next to the .exe / script wins
    (so you can swap an image without recompiling); otherwise the copy bundled
    inside the .exe is used."""
    local = os.path.join(app_dir(), IMAGE_FOLDER, filename)
    if os.path.exists(local):
        return local
    return os.path.join(getattr(sys, "_MEIPASS", app_dir()), IMAGE_FOLDER, filename)


GUIDE_FOLDER = "guides"     # the Guide window reads build_<name>.md from here
GUIDE_NAMES = {"Shiki": "shiki", "Universal": "universal", "Blood Awakening": "blood",
               "Female Raid": "female"}      # family -> the <name> part of the md file names


def guide_path(filename):
    """guides/<filename>: one next to the .exe / script wins, else the copy bundled in the .exe."""
    local = os.path.join(app_dir(), GUIDE_FOLDER, filename)
    if os.path.exists(local):
        return local
    return os.path.join(getattr(sys, "_MEIPASS", app_dir()), GUIDE_FOLDER, filename)


def documents_dir():
    """The user's real Documents folder (follows OneDrive redirection on Windows)."""
    if sys.platform == "win32":
        try:
            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_PERSONAL = 5 (My Documents), SHGFP_TYPE_CURRENT = 0
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
                return buf.value
        except Exception:  # noqa: BLE001
            pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def data_dir():
    """Documents\\Ymir Macro - created if it doesn't exist yet. The json lives here."""
    path = os.path.join(documents_dir(), STATS_FOLDER)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return app_dir()   # can't create it: fall back to next to the script
    return path

def override_dir():
    """Documents\\Ymir Macro\\asset_override - the user's own pictures (not created here)."""
    return os.path.join(data_dir(), OVERRIDE_FOLDER)


def ensure_override_dir():
    """Create the asset_override folder if it doesn't exist yet and return its path."""
    path = override_dir()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def override_path(filename):
    """The user's own copy of an image (asset_override/<filename>) or None if they didn't add one.
    `filename` is relative to the image folder, e.g. "retry_text.png" or
    "low quality fix/retry_text.png"."""
    path = os.path.join(override_dir(), filename)
    return path if os.path.isfile(path) else None


def open_override_folder():
    """Open the asset_override folder in the file explorer."""
    try:
        folder = ensure_override_dir()
        if IS_WIN:
            os.startfile(folder)
        else:
            subprocess.Popen(["open" if IS_MAC else "xdg-open", folder])
    except Exception:  # noqa: BLE001
        pass


STEP_NAMES = ["Ability 2", "Dashing", "Strafe", "Ability 3",
              "Buying time", "Ability 4", "Shift", "Shifted 4", "Ability 5"]

RAID_STEP_NAMES = ["Using buffs", "Shifting", "Killing Eren", "Killing Female titan",
                   "Opening chests", "Clicking retry"]


def step_names():
    """The steps shown on the status card for the config that is picked now."""
    if settings["family"] == "Female Raid":
        return RAID_STEP_NAMES
    if is_custom(settings["family"]):
        return CUSTOM_STEP_NAMES
    return STEP_NAMES


# ------------------------------ settings --------------------------------
# bind id -> (label shown in the Settings window, default key)
HOTKEY_BINDS = {
    "start": ("Start / manual start", START_KEY),
    "pause": ("Pause / resume", PAUSE_KEY),
    "stop": ("Stop", QUIT_KEY),
}
GAME_BINDS = {
    "ability2": ("Ability 2", KEY_ABILITY_1ST),
    "ability3": ("Ability 3", KEY_ABILITY_2ND),
    "ability4": ("Ability 4", KEY_ABILITY_3RD),
    "ability5": ("Ability 5", KEY_ABILITY_LAST),
    "dash": ("Dash", KEY_DASH),
    "strafe": ("Strafe A", KEY_STRAFE),
    "strafe_d": ("Strafe D", KEY_STRAFE_D),
    "grab": ("Escape grab", KEY_GRAB),
}
BIND_LABELS = {k: v[0] for k, v in {**HOTKEY_BINDS, **GAME_BINDS}.items()}
DEFAULT_KEYBINDS = {k: v[1] for k, v in {**HOTKEY_BINDS, **GAME_BINDS}.items()}
DEFAULT_WEBHOOK = {
    "url": "",              # empty = Discord is off
    "mention": False,       # ping the user in error messages
    "user_id": "",          # the Discord user ID that gets pinged
    "hourly_graph": False,  # send a stats graph every graph_hours hours
    "graph_hours": 1,       # that interval, in hours
    "notify_mission": True, # post a stats embed after every finished mission
    "notify_error": True,   # message when the macro hits an error
    "use_bot": False,       # True = send with a Discord bot (and obey its !commands) instead of the webhook
    "bot_token": "",        # the bot's token
    "channel_id": "",       # the channel the bot posts in and listens to
}

DEFAULT_FAILSAFE = {
    "minutes": 2,       # failsafe triggers when a mission lasts longer than minutes + seconds
    "seconds": 0,
    "click_x": None,    # where the failsafe clicks (screen pixels). None = X centre, 83% down the Y axis;
    "click_y": None,    # a picked coordinate overrides the middle
    "look_down": False, # before clicking: ctrl, mouse moved down, ctrl (off by default)
    "look_time": LOOK_DOWN_TIME,      # Configure window: seconds the mouse is moved down
    "look_pixels": LOOK_DOWN_PIXELS,  # ... pixels per step
    "look_step": LOOK_DOWN_STEP,      # ... seconds between two steps
}
RECONNECT_IMAGE_FILE = "disconnect.png"      # the Disconnect Image (saved in asset_override)
ROBLOX_GAME_ID = 13379208636                 # Reconnect rejoins this game (roblox://placeId=...)
RECONNECT_THRESHOLD = 0.75                   # how well the Disconnect Image must match
RECONNECT_CHECK_INTERVAL = 1.0               # look for it this often (seconds)
RECONNECT_CLOSE_WAIT = 3.0                   # after closing Roblox, before launching it again
RECONNECT_LOAD_WAIT = 25.0                   # after launching: hands off while the game loads
RECONNECT_COOLDOWN = 60.0                    # after a rejoin the image is ignored this long
RECONNECT_CLICK_DELAY = 2.5                  # then: seconds before EACH of the other coordinates is clicked (default;
                                             # tweakable in Recovery > Reconnect > Configure > Click delay)
RECONNECT_CLICK_DELAY_RANGE = (0.0, 60.0)    # what the Click delay boxes accept (seconds)
RECONNECT_PLAY_DELAY = 0.5                   # the wait between Create and Play (default; tweakable like the click delay)
RECONNECT_PLAY_KEYS = ("m_play", "r_play")   # the Play spots of the Missions / Raids tab (they use that wait)
DEFAULT_RECONNECT = {"enabled": False, "spots": {}, "click_delay": RECONNECT_CLICK_DELAY,
                     "play_delay": RECONNECT_PLAY_DELAY}   # Recovery > Reconnect > Configure: off until it is set up
RECONNECT_FIRST_DELAY = 2.0                  # the first click happens this long after the Select picture is seen
RECONNECT_RESCUE_LIMIT = 120.0               # a failsafe rescue still running after this long forces a rejoin
# The two pictures under the coordinates (saved in asset_override, like the Disconnect Image). They are the
# same for both tabs: the first click (Pick a slot) waits until "Select" is on screen, and
# Click play (lobby) waits until "Play" is on screen.
RECONNECT_SELECT_FILE = "reconnect_select.png"
RECONNECT_PLAY_FILE = "reconnect_play.png"
RECONNECT_IMAGE_FILES = {"select": RECONNECT_SELECT_FILE, "play": RECONNECT_PLAY_FILE}
RECONNECT_IMAGE_NAMES = {"disconnect": "Disconnect", "select": "Select", "play": "Play"}
# every picture of the Images section of the window, in that order (the Disconnect Image is the one the
# macro watches for; Select / Play gate two of the clicks)
RECONNECT_ALL_IMAGES = {"disconnect": RECONNECT_IMAGE_FILE, **RECONNECT_IMAGE_FILES}
RECONNECT_STEP_GATES = {"pick_slot": "select", "click_play_lobby": "play"}   # spot key -> picture it waits for
RECONNECT_IMAGE_TIMEOUT = 120.0              # a picture that has not shown after this long: rejoin again
RECONNECT_MAX_ATTEMPTS = 3                   # ... at most this many rejoins in a row
# Reconnect > Add modifiers: on the FIRST retry window after a rejoin the macro turns on the game's UI
# navigation (\\), opens the Modifiers button with the arrow keys, presses Enter on every modifier that is
# switched on, and turns the UI navigation off again.
MODIFIER_OPEN_KEY = "\\"                       # turns UI navigation on
MODIFIER_CLOSE_KEY = "\\"                      # turns UI navigation off again
MODIFIER_KEY_HOLD = 0.04                      # how long every key is held down
MODIFIER_KEY_GAP = 0.12                       # short pause between two key presses
MODIFIER_RIGHT_PRESSES = 4                    # right arrow, pressed this many times after UI nav is on
MODIFIER_MENU_WAIT = 0.4                      # wait after the right presses / after opening the Modifiers button
MODIFIER_UP_PRESSES = 7                       # the up arrow burst ...
MODIFIER_UP_TIME = 1.0                        # ... spread over this long (seconds)
MODIFIER_DOWN_GAP = 0.5                       # delay between all the down arrow presses
MODIFIER_PRE_DOWNS = 4                        # down presses before Oddball is reached (missions)
MODIFIER_RAID_PRE_DOWNS = 3                   # ... the same, when the macro is a raid (Oddball is not offered there)
MODIFIER_END_DOWNS = 4                        # down presses after Time trial, then Enter
MODIFIER_TEST_COUNTDOWN = 3                   # Test button: seconds to click into Roblox first
# (key, name) in the order the menu is walked: one down press per row, Enter only when switched on.
# The row between Fog and Time trial is skipped (None): down, but no Enter.
MODIFIER_LIST = (("oddball", "Oddball"), ("injury_prone", "Injury prone"),
                 ("chronic", "Chronic injuries"), ("fog", "Fog"),
                 ("time_trial", "Time trial"))
MODIFIER_ROWS = (("oddball", "Oddball"), ("injury_prone", "Injury prone"),
                 ("chronic", "Chronic injuries"), ("fog", "Fog"), (None, "Skipped option"),
                 ("time_trial", "Time trial"))
DEFAULT_MODIFIERS = {"enabled": False, "oddball": False, "injury_prone": False,
                     "chronic": False, "fog": False, "time_trial": False}   # Reconnect > Add modifiers (off by default)
# the coordinates picked in the Reconnect window (Recovery > Reconnect > Configure), 2 sub tabs.
# Both tabs start with the same 4 shared coordinates (one set of values, shown in both), then:
#   Missions - Click missions, then Pick outskirts > Create > Play   (mission macros)
#   Raids    - Click Raids, then Pick female raid > Create > Play    (Female Raid / raid macros)
# (key, name). The whole tab of the macro in use has to be filled in or Reconnect stays off.
RECONNECT_SHARED_SPOTS = (("pick_slot", "Pick a slot"), ("click_play", "Click play"),
                          ("choose_lobby", "Choose lobby"), ("click_play_lobby", "Click play (lobby)"))
RECONNECT_MISSION_SPOTS = (("click_missions", "Click missions"), ("m_pick_outskirts", "Pick outskirts"),
                           ("m_create", "Create"), ("m_play", "Play"))
RECONNECT_RAID_SPOTS = (("click_raids", "Click Raids"), ("r_pick_female_raid", "Pick female raid"),
                        ("r_create", "Create"), ("r_play", "Play"))
# Reconnect > Join VIP server (off by default): instead of the "Click play" click, the sequence clicks
# Click Codes, then VIP server (a Click delay before each), then a Click delay later pastes the code
# and presses Enter. The rest of the sequence (Choose lobby, ...) goes on as usual.
RECONNECT_VIP_SPOTS = (("vip_click_codes", "Click Codes"), ("vip_server", "VIP server"))
RECONNECT_VIP_REPLACES = "click_play"            # the shared spot that is skipped while Join VIP server is on
RECONNECT_VIP_CODE_KEY = "vip_code"              # step key of the "paste the code + Enter" step
DEFAULT_VIP = {"enabled": False, "code": "", "server_owner": False}   # Reconnect > Join VIP server
                                                  # (server_owner on = the code is not pasted, so no code is needed)
RECONNECT_VIP_PASTE_WAIT = 0.3                   # after the paste, before Enter
RECONNECT_TABS = ("Missions", "Raids")
RECONNECT_TAB_SPOTS = {"Missions": RECONNECT_SHARED_SPOTS + RECONNECT_MISSION_SPOTS,
                       "Raids": RECONNECT_SHARED_SPOTS + RECONNECT_RAID_SPOTS}
RECONNECT_SPOTS = (RECONNECT_SHARED_SPOTS + RECONNECT_MISSION_SPOTS + RECONNECT_RAID_SPOTS
                   + RECONNECT_VIP_SPOTS)   # every saved key
FAILSAFE_LOOK_LIMITS = {"look_time": (0.1, 20.0), "look_pixels": (1, 200), "look_step": (0.005, 1.0)}

# the live settings (saved in STATS_FILE under "settings")
settings = {
    "keybinds": dict(DEFAULT_KEYBINDS),
    "webhook": dict(DEFAULT_WEBHOOK),
    "family": DEFAULT_FAMILY,
    "theme": DEFAULT_THEME,
    "move_shifted": DEFAULT_MOVE_SHIFTED,   # direction held at step 9: Left (A) / Right (D) / Stand
    "timings": {},          # Edit configs: {section: {key: value}} - only what the user changed
    "failsafe": dict(DEFAULT_FAILSAFE),     # stuck-mission rescue: time before it triggers + click spot
    "reconnect": {"enabled": False, "spots": {}, "click_delay": RECONNECT_CLICK_DELAY,
                  "play_delay": RECONNECT_PLAY_DELAY,
                  "modifiers": dict(DEFAULT_MODIFIERS),
                  "vip": dict(DEFAULT_VIP)},   # Recovery tab: the reconnect feature (off by default)
    "retry_scale": None,    # template size that last found the retry button on a non-1080p screen
                            # (None = not found yet). Tried first; updated when another size matches
    "config_section": CONFIG_SECTIONS[0],   # Edit configs: the part that was open last (kept between runs)
    "always_on_top": False, # footer switch "Always on top" (kept between runs)
    "compact_ui": False,    # Settings > UI > "Compact mode": while the macro runs, hide the main
                            # window and show the small stats bar instead (off = keep the window)
    "mini_pos": DEFAULT_MINI_POS,   # Settings > UI: Left / Middle / Right edge of Roblox
    "animations": False,    # Settings > UI > "GUI animations": slide / fade effects (off = the plain look)
    "less_windows": True,   # Settings > UI > "Less windows": a window opened from Settings replaces it
                            # (with a Back button + path) instead of opening on top (off = old way)
    "fix_loot": False,      # Settings > Configs > Fix image detection > "Fix loot tracking" (Beta): look for
                            # the loot icons at many sizes, for UIs where the drops are resized
    "monitor": 1,           # Settings > Keybinds > Monitor: 1 = first monitor, 2 = second, ...
    "window": None,         # {"x", "y", "w", "h"} of the main window (None = never moved/resized)
}


def apply_settings():
    """Copy the saved keybinds into the key variables the macro uses."""
    global START_KEY, PAUSE_KEY, QUIT_KEY
    global KEY_ABILITY_1ST, KEY_ABILITY_2ND, KEY_ABILITY_3RD, KEY_ABILITY_LAST
    global KEY_DASH, KEY_STRAFE, KEY_STRAFE_D, KEY_GRAB, BUY_TIME_MOVES, MONITOR_INDEX
    kb = settings["keybinds"]
    START_KEY, PAUSE_KEY, QUIT_KEY = kb["start"], kb["pause"], kb["stop"]
    KEY_ABILITY_1ST, KEY_ABILITY_2ND = kb["ability2"], kb["ability3"]
    KEY_ABILITY_3RD, KEY_ABILITY_LAST = kb["ability4"], kb["ability5"]
    KEY_DASH, KEY_STRAFE, KEY_GRAB = kb["dash"], kb["strafe"], kb["grab"]
    KEY_STRAFE_D = kb["strafe_d"]
    BUY_TIME_MOVES = [KEY_STRAFE]
    MONITOR_INDEX = max(1, int(settings["monitor"]))


def load_settings(saved):
    """Merge the "settings" part of the json into the defaults (bad values are ignored)."""
    if not isinstance(saved, dict):
        return
    kb = saved.get("keybinds")
    if isinstance(kb, dict):
        merged = dict(DEFAULT_KEYBINDS)
        for bind_id in merged:
            value = kb.get(bind_id)
            if isinstance(value, str) and value.strip():
                merged[bind_id] = value.strip().lower()
        if len(set(merged.values())) == len(merged):   # a hand-edited file with clashes -> defaults
            settings["keybinds"] = merged
    wh = saved.get("webhook")
    if isinstance(wh, dict):
        for key, default in DEFAULT_WEBHOOK.items():
            value = wh.get(key, default)
            settings["webhook"][key] = value if isinstance(value, type(default)) else default
        hours = settings["webhook"]["graph_hours"]
        settings["webhook"]["graph_hours"] = min(MAX_GRAPH_HOURS, max(1, hours))
    if family_valid(saved.get("family")):
        settings["family"] = saved["family"]
    if saved.get("theme") in THEMES:
        settings["theme"] = saved["theme"]
    if saved.get("move_shifted") in MOVE_SHIFTED_OPTIONS:
        settings["move_shifted"] = saved["move_shifted"]
    apply_timings(saved.get("timings"))     # only present in older stats files (now config.json)
    fs = saved.get("failsafe")
    if isinstance(fs, dict):
        def _int(v):
            return v if isinstance(v, int) and not isinstance(v, bool) else None
        mins, secs = _int(fs.get("minutes")), _int(fs.get("seconds"))
        if mins is not None and secs is not None:
            settings["failsafe"]["minutes"] = min(MAX_FAILSAFE_MINUTES, max(0, mins))
            settings["failsafe"]["seconds"] = min(59, max(0, secs))
        cx, cy = _int(fs.get("click_x")), _int(fs.get("click_y"))
        if cx is not None and cy is not None and cx >= 0 and cy >= 0:
            settings["failsafe"]["click_x"], settings["failsafe"]["click_y"] = cx, cy
        if isinstance(fs.get("look_down"), bool):
            settings["failsafe"]["look_down"] = fs["look_down"]
        for key, (lo, hi) in FAILSAFE_LOOK_LIMITS.items():
            v = fs.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                settings["failsafe"][key] = (int(min(hi, max(lo, v))) if key == "look_pixels"
                                             else float(min(hi, max(lo, v))))
    rc = saved.get("reconnect")
    if isinstance(rc, dict):
        spots = rc.get("spots")
        if isinstance(spots, dict):
            for key, _name in RECONNECT_SPOTS:
                v = spots.get(key)
                if (isinstance(v, (list, tuple)) and len(v) == 2
                        and all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in v)):
                    settings["reconnect"]["spots"][key] = [v[0], v[1]]
        cd = rc.get("click_delay")
        if isinstance(cd, (int, float)) and not isinstance(cd, bool):
            lo, hi = RECONNECT_CLICK_DELAY_RANGE
            settings["reconnect"]["click_delay"] = float(min(hi, max(lo, cd)))
        pd = rc.get("play_delay")
        if isinstance(pd, (int, float)) and not isinstance(pd, bool):
            lo, hi = RECONNECT_CLICK_DELAY_RANGE
            settings["reconnect"]["play_delay"] = float(min(hi, max(lo, pd)))
        mods = rc.get("modifiers")
        if isinstance(mods, dict):
            for key in DEFAULT_MODIFIERS:
                if isinstance(mods.get(key), bool):
                    settings["reconnect"]["modifiers"][key] = mods[key]
        vip = rc.get("vip")
        if isinstance(vip, dict):
            if isinstance(vip.get("code"), str):
                settings["reconnect"]["vip"]["code"] = vip["code"].strip()
            if isinstance(vip.get("server_owner"), bool):
                settings["reconnect"]["vip"]["server_owner"] = vip["server_owner"]
            if vip.get("enabled") is True and not reconnect_vip_missing():
                settings["reconnect"]["vip"]["enabled"] = True   # only stays on while it is filled in
        if rc.get("enabled") is True and not reconnect_missing():
            settings["reconnect"]["enabled"] = True    # only stays on while everything it needs is set
    if saved.get("config_section") in CONFIG_SECTIONS:
        settings["config_section"] = saved["config_section"]
    if isinstance(saved.get("always_on_top"), bool):
        settings["always_on_top"] = saved["always_on_top"]
    if saved.get("mini_pos") in MINI_POS_OPTIONS:
        settings["mini_pos"] = saved["mini_pos"]
    if isinstance(saved.get("compact_ui"), bool):
        settings["compact_ui"] = saved["compact_ui"]
    elif isinstance(saved.get("legacy_ui"), bool):       # the first name of this switch (inverted)
        settings["compact_ui"] = not saved["legacy_ui"]
    if isinstance(saved.get("fix_loot"), bool):
        settings["fix_loot"] = saved["fix_loot"]
    if isinstance(saved.get("animations"), bool):
        settings["animations"] = saved["animations"]
    if isinstance(saved.get("less_windows"), bool):
        settings["less_windows"] = saved["less_windows"]
    mon = saved.get("monitor")
    if isinstance(mon, int) and not isinstance(mon, bool) and 1 <= mon <= 16:
        settings["monitor"] = mon
    rs = saved.get("retry_scale")
    if isinstance(rs, (int, float)) and not isinstance(rs, bool) and 0.3 <= rs <= 3.0:
        settings["retry_scale"] = float(rs)
    win = saved.get("window")
    if (isinstance(win, dict) and all(
            isinstance(win.get(k), int) and not isinstance(win.get(k), bool)
            for k in ("x", "y", "w", "h"))):
        settings["window"] = {k: win[k] for k in ("x", "y", "w", "h")}


def apply_timings(tm):
    """Merge saved Edit configs overrides {section: {key: value}} into settings["timings"].
    Unknown sections / keys and bad or out-of-range values are ignored."""
    if not isinstance(tm, dict):
        return
    for section, fields in CONFIG_FIELDS.items():
        sv = tm.get(section)
        if not isinstance(sv, dict):
            continue
        for spec in fields:
            val = clean_cfg_value(spec, sv.get(spec[0]))
            if val is not None:
                settings["timings"].setdefault(section, {})[spec[0]] = val


def mission_timeout():
    """Seconds a mission may last before the failsafe (stuck-mission rescue) triggers."""
    fs = settings["failsafe"]
    return float(max(MIN_FAILSAFE_SECONDS, fs["minutes"] * 60 + fs["seconds"]))


def list_monitors():
    """The real monitors as mss sees them (left / top / width / height), first monitor first."""
    try:
        with mss.mss() as sct:
            return [dict(m) for m in sct.monitors[1:]]
    except Exception:  # noqa: BLE001
        return []


def pick_monitor(monitors):
    """`monitors` = an mss .monitors list (entry 0 = all screens together). The monitor chosen in
    Settings > Keybinds > Monitor, or the first one when that monitor isn't connected any more."""
    idx = MONITOR_INDEX if 1 <= MONITOR_INDEX < len(monitors) else 1
    return monitors[idx] if idx < len(monitors) else monitors[0]


def macro_monitor_rect(owner=None):
    """(left, top, width, height) of the chosen monitor, in screen pixels."""
    try:
        with mss.mss() as sct:
            m = pick_monitor(sct.monitors)
            return m["left"], m["top"], m["width"], m["height"]
    except Exception:  # noqa: BLE001
        if owner is not None:
            return 0, 0, owner.winfo_screenwidth(), owner.winfo_screenheight()
        return 0, 0, 1920, 1080


FAILSAFE_DEFAULT_Y = 0.83    # default failsafe click: middle of the X axis, this fraction down the Y axis


def reconnect_macro_tab():
    """Which of the two macro tabs the selected macro needs: Raids for a raid macro, else Missions."""
    return "Raids" if family_is_raid(settings["family"]) else "Missions"


def reconnect_vip_settings():
    """The Join VIP server settings (always a full dict, even for an old settings file)."""
    vip = dict(DEFAULT_VIP)
    try:
        saved = settings["reconnect"].get("vip", {})
        vip["enabled"] = bool(saved.get("enabled", False))
        vip["code"] = str(saved.get("code", "") or "").strip()
        vip["server_owner"] = bool(saved.get("server_owner", False))
    except Exception:  # noqa: BLE001
        pass
    return vip


def reconnect_vip_enabled():
    return bool(reconnect_vip_settings()["enabled"])


def reconnect_vip_missing():
    """What Join VIP server still needs (names; empty = ready), whether or not it is switched on."""
    spots = settings["reconnect"]["spots"]
    missing = [f"Join VIP server > {name}" for key, name in RECONNECT_VIP_SPOTS if spots.get(key) is None]
    vip = reconnect_vip_settings()
    if not vip["code"] and not vip["server_owner"]:     # the server owner has no code to paste
        missing.append("Join VIP server > code")
    return missing


def reconnect_tab_spots(tab):
    """The coordinates a tab needs: Click play is not needed while Join VIP server is on."""
    spots = RECONNECT_TAB_SPOTS[tab]
    if reconnect_vip_enabled():
        spots = tuple(sp for sp in spots if sp[0] != RECONNECT_VIP_REPLACES)
    return spots


def reconnect_missing():
    """What Reconnect still needs before it can be turned on (a list of names, empty = ready).
    Every required setting of the Reconnect window is checked here: the Disconnect Image, the Select
    and Play pictures and all 8 coordinates of the tab (Missions or Raids) of the macro that is selected - the 4 shared ones
    plus that tab's own 4."""
    missing = []
    if not override_path(RECONNECT_IMAGE_FILE):
        missing.append("Disconnect Image")
    for kind, filename in RECONNECT_IMAGE_FILES.items():
        if not override_path(filename):
            missing.append(RECONNECT_IMAGE_NAMES[kind] + " Image")
    spots = settings["reconnect"]["spots"]
    tab = reconnect_macro_tab()
    shared = {k for k, _n in RECONNECT_SHARED_SPOTS}
    for key, name in reconnect_tab_spots(tab):
        if spots.get(key) is None:
            missing.append(name if key in shared else f"{tab} > {name}")
    if reconnect_vip_enabled():
        missing.extend(reconnect_vip_missing())
    return missing


def reconnect_click_delay():
    """Seconds between the Reconnect clicks (Click delay in the Reconnect window)."""
    try:
        return float(settings["reconnect"].get("click_delay", RECONNECT_CLICK_DELAY))
    except (TypeError, ValueError):
        return float(RECONNECT_CLICK_DELAY)


def reconnect_play_delay():
    """Seconds between Create and Play (Play delay in the Reconnect window)."""
    try:
        return float(settings["reconnect"].get("play_delay", RECONNECT_PLAY_DELAY))
    except (TypeError, ValueError):
        return float(RECONNECT_PLAY_DELAY)


def reconnect_steps():
    """The clicks after a rejoin, in order: [(label, (x, y), picture, spot key), ...] - the 4 shared
    coordinates, then the tab's own: Click missions / Click Raids, then 3 more. `picture` is
    "select" / "play" when the click waits for that picture to show, else None."""
    spots = settings["reconnect"]["spots"]
    tab = reconnect_macro_tab()
    shared = {k for k, _n in RECONNECT_SHARED_SPOTS}
    steps = []
    vip = reconnect_vip_enabled()
    for key, name in RECONNECT_TAB_SPOTS[tab]:
        if vip and key == RECONNECT_VIP_REPLACES:
            # Join VIP server replaces the Click play click: Click Codes, VIP server, then the code
            for vkey, vname in RECONNECT_VIP_SPOTS:
                vxy = spots.get(vkey)
                if vxy:
                    steps.append((vname if vkey != "vip_click_codes" else "Click Codes",
                                  (vxy[0], vxy[1]), None, vkey))
            if not reconnect_vip_settings()["server_owner"]:      # Server owner: no code is pasted
                steps.append(("Paste VIP code + Enter", (0, 0), None, RECONNECT_VIP_CODE_KEY))
            continue
        xy = spots.get(key)
        if xy:
            steps.append((name if key in shared else f"{tab} > {name}", (xy[0], xy[1]),
                          RECONNECT_STEP_GATES.get(key), key))
    return steps


def failsafe_default_point():
    """The failsafe click when no spot is picked: centre of the X axis, 83% down the Y axis."""
    return (_monitor["left"] + _monitor["width"] // 2,
            _monitor["top"] + int(_monitor["height"] * FAILSAFE_DEFAULT_Y))


def failsafe_click_point(cx, cy):
    """Where the failsafe clicks: the picked coordinate if there is one, else (cx, cy)."""
    fs = settings["failsafe"]
    if fs["click_x"] is not None and fs["click_y"] is not None:
        return fs["click_x"], fs["click_y"]
    return cx, cy


# ----------------------------- hotkeys ----------------------------------
_hotkey_handles = []


HOTKEY_REPEAT_GAP = 0.6   # a hotkey can't fire again within this long (also ignores a held key's repeats)


def unregister_hotkeys():
    for kind, handle in _hotkey_handles:
        try:
            if kind == "combo":
                keyboard.remove_hotkey(handle)
            else:
                keyboard.unhook(handle)
        except Exception:  # noqa: BLE001
            pass
    _hotkey_handles.clear()


def _hotkey_handler(name, fn):
    """Wrap a hotkey action: never raises (an exception inside a keyboard callback can kill
    the library's listener thread, and then NO hotkey works any more) and ignores key repeats."""
    last = [0.0]

    def handler(_event=None):
        now = time.time()
        gap = now - last[0]
        last[0] = now        # a held key keeps refreshing this, so its repeats never re-fire
        if gap < HOTKEY_REPEAT_GAP:
            return
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log(f"Hotkey '{name}' error: {type(e).__name__}: {e}")
    return handler


def register_hotkeys():
    """(Re)bind F8 / F6 / F7 - or whatever they were changed to in Settings.
    A single key is bound with on_press_key, so it fires whatever other keys are (or seem to
    be) held down - add_hotkey stops working when the library thinks some key is stuck."""
    unregister_hotkeys()
    for key, fn in ((PAUSE_KEY, toggle_pause), (QUIT_KEY, request_stop),
                    (START_KEY, request_start),
                    (DEBUG_RESCUE_KEY, debug_trigger_rescue)):   # DEBUG: F3+K
        handler = _hotkey_handler(key, fn)
        try:
            if "+" in key and key != "+":
                _hotkey_handles.append(("combo", keyboard.add_hotkey(key, handler)))
            else:
                _hotkey_handles.append(("key", keyboard.on_press_key(key, handler)))
        except (ValueError, KeyError) as e:
            log(f"Error: could not bind hotkey '{key}': {e}")


def to_game_key(name, keypad=False):
    """Turn a name from the keyboard library ('left shift') into one pydirectinput
    understands ('shiftleft'). Returns None if pydirectinput can't press that key."""
    n = name.lower().strip()
    if keypad and n.isdigit():
        n = "num" + n
    for side in ("left", "right"):
        if n.startswith(side + " "):
            n = n[len(side) + 1:] + side
    n = n.replace(" ", "").replace("windows", "win")
    known = getattr(pydirectinput, "KEYBOARD_MAPPING", None) or getattr(pydirectinput, "KEYBOARD_KEYS", None)
    if known is not None and n not in known:
        return None
    return n


# --------------------------- custom macros (.ymir) -------------------------
# Settings > Configs > "+" : your own macro, written as a pseudo-script or recorded. Every macro is
# ONE plain text file in Documents\Ymir Macro\Custom-Macros, so sharing it = sending the file
# (drop a received .ymir into that folder and it shows up in the dropdown).
CUSTOM_FOLDER = "Custom-Macros"
MACRO_EXT = ".ymir"
CUSTOM_PREFIX = "custom:"          # settings["family"] of a custom macro = "custom:<file name>"
CUSTOM_STEP_NAMES = ["Running macro", "Waiting for retry"]
CUSTOM_BADGE_COLORS = (("#14b8a6", "#04201d"), ("#a855f7", "#ffffff"), ("#f97316", "#1c0a00"),
                       ("#ec4899", "#ffffff"), ("#22c55e", "#052e12"), ("#0ea5e9", "#021824"))
MACRO_HEADER_RE = re.compile(r"^(name|author|description|version|type)\s*:\s*(.*)$", re.I)
# "type: ..." tells the macro what a run of this script IS, so the counters stay honest:
#   mission  - every run is one mission: counted in Missions, Completed / Failed, Discord embeds
#   raid     - every run is one raid: counted in Raids / Vessels / the raid's Completed / Failed (like
#              the Female Raid config), and the start is the skip button / raid_start.png
MACRO_TYPES = {
    "mission": "counts every run as a mission (default)",
    "raid": "counts every run as a raid (Raids, Vessels, raid Completed / Failed)",
}
# "utility" was the old name of "raid" (it and its aliases still work in older macros)
MACRO_TYPE_ALIASES = {"missions": "mission", "raids": "raid", "utility": "raid", "none": "raid",
                      "other": "raid", "loop": "raid", "afk": "raid", "farm": "raid"}
# functions a macro can call (with or without the brackets):  retry()  trackloot()  openchest() ...
MACRO_FUNCS = ("retry", "trackloot", "openchest", "addwin", "addlose", "addmission", "addraid")
MACRO_CALL_RE = re.compile(r"^([A-Za-z_]+)\s*\(\s*\)$")
DEFAULT_MACRO_TYPE = "mission"             # a macro without a type: line behaves like it always did
MACRO_MAX_SECONDS = 3600.0
MACRO_MAX_DEPTH = 6                 # repeat / if inside repeat / if inside ...
MACRO_IMG_EXTS = (".png", ".jpg", ".jpeg", ".bmp")   # pictures a macro can look for (find / imgclick)
MACRO_INDENT = "    "                # one level of indentation (optional in a script, ignored when running)
CUSTOM_IMG_THRESHOLD = 0.85         # how well a macro picture has to match to count as seen
_custom_held = set()                # keys a macro has pressed with "down" and not released yet


def custom_dir():
    """Documents\\Ymir Macro\\Custom-Macros - created if it doesn't exist yet."""
    path = os.path.join(data_dir(), CUSTOM_FOLDER)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


FILE_PROGID = "Ymir.Macro"      # the Windows file type .ymir is attached to


def register_ymir_file_type():
    """Windows: give every .ymir file its own icon in Explorer (the lambda, FILE_ICON_FILE).
    Written under HKEY_CURRENT_USER, so no admin rights are needed, and only when something is
    different, so a normal start touches nothing. The icon is copied into Documents\\Ymir Macro first:
    a one-file .exe unpacks itself into a temp folder that is deleted again, and the registry has to
    point at a file that stays. Double-clicking a .ymir is not changed (no open command is set).
    Never raises - a missing icon or a locked registry just means the files keep the plain icon."""
    if not IS_WIN:
        return
    try:
        import filecmp
        import winreg
        src = asset_path(FILE_ICON_FILE)
        if not os.path.isfile(src):
            return
        icon = os.path.join(data_dir(), FILE_ICON_FILE)
        try:
            if not os.path.isfile(icon) or not filecmp.cmp(src, icon, shallow=False):
                shutil.copyfile(src, icon)
        except OSError:
            icon = src                          # can't copy it: point at the bundled one
        changed = False

        def put(path, name, value):
            nonlocal changed
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0,
                                    winreg.KEY_READ | winreg.KEY_WRITE) as key:
                try:
                    if winreg.QueryValueEx(key, name)[0] == value:
                        return
                except OSError:
                    pass
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
                changed = True

        classes = "Software\\Classes\\"
        put(classes + MACRO_EXT, "", FILE_PROGID)
        put(classes + MACRO_EXT, "Content Type", "text/plain")      # a .ymir is plain text
        put(classes + MACRO_EXT, "PerceivedType", "text")
        put(classes + FILE_PROGID, "", "Ymir macro")                # the "Type" column in Explorer
        put(classes + FILE_PROGID + "\\DefaultIcon", "", icon + ",0")
        if changed:                             # tell Explorer, so the icon shows without a restart
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)   # SHCNE_ASSOCCHANGED
    except Exception:  # noqa: BLE001
        pass


def is_custom(key):
    return isinstance(key, str) and key.startswith(CUSTOM_PREFIX)


def custom_filename(key):
    return key[len(CUSTOM_PREFIX):] if is_custom(key) else str(key)


def custom_path(key):
    """Full path of a macro; basename() so a hand-edited setting can never leave the folder."""
    return os.path.join(custom_dir(), os.path.basename(custom_filename(key)))


def family_valid(key):
    """A built-in config, or a custom macro whose file is still there."""
    if key in FAMILIES:
        return True
    return is_custom(key) and os.path.isfile(custom_path(key))


def safe_macro_filename(name):
    base = re.sub(r"[^\w\- ]+", "", name, flags=re.UNICODE).strip().replace(" ", "_")[:60]
    return (base or "macro") + MACRO_EXT


def custom_badge(name):
    letter = next((ch for ch in name if ch.isalnum()), "?").upper()
    bg, fg = CUSTOM_BADGE_COLORS[sum(name.encode("utf-8")) % len(CUSTOM_BADGE_COLORS)]
    return (letter, bg, fg)


_custom_list_cache = {"sig": None, "found": []}


def list_custom_macros():
    """[(key, display name, file name)] of every .ymir in the Custom-Macros folder. The headers are
    only read again when a file was added, removed or changed."""
    try:
        folder = custom_dir()
        files = sorted(os.listdir(folder), key=str.lower)
        sig = []
        for fname in files:
            if fname.lower().endswith(MACRO_EXT):
                try:
                    sig.append((fname, os.path.getmtime(os.path.join(folder, fname))))
                except OSError:
                    sig.append((fname, 0))
        sig = tuple(sig)
    except OSError:
        return []
    if _custom_list_cache["sig"] == sig:
        return list(_custom_list_cache["found"])
    found = _read_custom_macros(files)
    _custom_list_cache["sig"], _custom_list_cache["found"] = sig, found
    return list(found)


def _read_custom_macros(files):
    found = []
    for fname in files:
        if not fname.lower().endswith(MACRO_EXT):
            continue
        name = os.path.splitext(fname)[0]
        try:
            with open(os.path.join(custom_dir(), fname), "r", encoding="utf-8-sig") as fh:
                for i, line in enumerate(fh):
                    m = MACRO_HEADER_RE.match(line.strip())
                    if m and m.group(1).lower() == "name" and m.group(2).strip():
                        name = m.group(2).strip()
                        break
                    if i > 40:
                        break
        except (OSError, UnicodeDecodeError):
            continue
        found.append((CUSTOM_PREFIX + fname, name, fname))
    return found


def normalize_macro_type(word):
    """'Mission' / 'raid' ... -> 'mission' | 'raid'. None when it is not a known type."""
    w = (word or "").strip().lower()
    w = MACRO_TYPE_ALIASES.get(w, w)
    return w if w in MACRO_TYPES else None


_macro_type_cache = {}      # file name -> (mtime, type)


def custom_macro_type(key):
    """The type: of a custom macro ('mission' when the line is missing or unknown)."""
    path = custom_path(key)
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return DEFAULT_MACRO_TYPE
    hit = _macro_type_cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    kind = DEFAULT_MACRO_TYPE
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            for i, line in enumerate(fh):
                m = MACRO_HEADER_RE.match(_strip_macro_comment(line))
                if m and m.group(1).lower() == "type":
                    kind = normalize_macro_type(m.group(2)) or DEFAULT_MACRO_TYPE
                    break
                if i > 40:
                    break
    except (OSError, UnicodeDecodeError):
        pass
    _macro_type_cache[path] = (stamp, kind)
    return kind


def family_is_raid(fam):
    """Female Raid, or a custom macro of type: raid - they count raids / vessels, not missions."""
    return fam == "Female Raid" or (is_custom(fam) and custom_macro_type(fam) == "raid")


def raid_counting():
    """family_is_raid() of the config that is picked now."""
    return family_is_raid(settings["family"])


_macro_chest_cache = {}      # path -> (mtime, uses openchest)


def custom_uses_chest(key):
    """Does that custom macro call openchest()? (Then Settings > Configs shows the chest spots.)"""
    path = custom_path(key)
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return False
    hit = _macro_chest_cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    uses = False
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            uses = any(re.match(r"openchest\b", _strip_macro_comment(line), re.I) for line in fh)
    except (OSError, UnicodeDecodeError):
        pass
    _macro_chest_cache[path] = (stamp, uses)
    return uses


def family_items():
    """Everything the config dropdown lists: the built-in configs, then the custom macros."""
    items = [(f, FAMILY_LABELS[f], FAMILY_BADGES[f], FAMILY_TAGS.get(f)) for f in FAMILIES]
    for key, name, _fname in list_custom_macros():
        items.append((key, name, custom_badge(name), "Raid" if custom_macro_type(key) == "raid" else None))
    return items


# ----- the pseudo-script: parsing -----
class MacroSyntaxError(Exception):
    pass


def resolve_macro_key(word):
    """A key word of a script -> what pydirectinput presses (None = unknown key).
    ability2..ability5 / dash / strafe / strafe_d / grab are YOUR keybinds from Settings, so a
    shared macro uses the keys of whoever runs it."""
    n = word.strip().lower()
    if n in GAME_BINDS:
        return settings["keybinds"].get(n)
    key = to_game_key(n)
    if key is None and "_" in n:
        key = to_game_key(n.replace("_", " "))
    return key


def normalize_image_name(word):
    """What a script wrote after find / imgclick -> the file name in asset_override ('retry' and
    'retry.png' both give 'retry.png'). None when it can't be a picture there (folders, odd names)."""
    w = (word or "").strip()
    if (not w or w.startswith(".") or "/" in w or "\\" in w
            or not re.fullmatch(r"[\w\-.]+", w, re.UNICODE)):
        return None
    if not w.lower().endswith(MACRO_IMG_EXTS):
        w += ".png"
    return w


def safe_image_name(name):
    """A name typed in a dialog -> a file name a script can use (no spaces / folders)."""
    base = os.path.basename((name or "").replace("\\", "/"))
    stem, ext = os.path.splitext(base)
    stem = re.sub(r"[^\w\-]+", "_", stem, flags=re.UNICODE).strip("_")[:50]
    if not stem:
        return None
    ext = ext.lower()
    return stem + (ext if ext in MACRO_IMG_EXTS else ".png")


def image_exists(name):
    """Is there a picture of that name - the user's own (asset_override) or a built-in one?"""
    return bool(override_path(name) or os.path.isfile(asset_path(name)))


def _parse_condition(words):
    """The words after 'if' -> a condition tree:
    ('find', picture) | ('not', cond) | ('and', a, b) | ('or', a, b)   (and binds tighter than or)"""
    usage = "if find IMAGE.png   (also: not find ..., and, or)"
    pos = [0]

    def peek():
        return words[pos[0]].lower() if pos[0] < len(words) else None

    def take():
        pos[0] += 1
        return words[pos[0] - 1]

    def atom():
        w = peek()
        if w is None:
            raise MacroSyntaxError(f"the condition is not finished - use: {usage}")
        if w == "not":
            take()
            return ("not", atom())
        if w == "find":
            take()
            if peek() is None:
                raise MacroSyntaxError("'find' needs a picture, like: find retry.png")
            raw = take()
            img = normalize_image_name(raw)
            if img is None:
                raise MacroSyntaxError(f"'{raw}' is not a picture file name (use a name like "
                                       "retry.png - no spaces, no folders)")
            return ("find", img)
        raise MacroSyntaxError(f"unknown condition '{words[pos[0]]}' - use: {usage}")

    def conj():
        node = atom()
        while peek() == "and":
            take()
            node = ("and", node, atom())
        return node

    def disj():
        node = conj()
        while peek() == "or":
            take()
            node = ("or", node, conj())
        return node

    node = disj()
    if pos[0] < len(words):
        raise MacroSyntaxError(f"unexpected '{words[pos[0]]}' in the condition - use: {usage}")
    return node


def macro_node_images(nodes):
    """Every picture name a parsed script looks for (find / imgclick), sorted."""
    out = set()

    def conds(c):
        if c is None:
            return
        if c[0] == "find":
            out.add(c[1])
        elif c[0] == "not":
            conds(c[1])
        else:
            conds(c[1])
            conds(c[2])

    def walk(ns):
        for n in ns:
            if n[0] == "imgclick":
                out.add(n[1])
            elif n[0] == "if":
                conds(n[1])
                walk(n[2])
                walk(n[3])
            elif n[0] == "repeat":
                walk(n[2])
    walk(nodes)
    return sorted(out, key=str.lower)


def macro_image_usage():
    """{picture name: [names of the macros that use it]} over every .ymir in Custom-Macros. Anyone
    who gets a macro sees its pictures listed in Fix image detection > Macros (their own copy of
    each picture goes in asset_override, with the same name)."""
    used = {}
    for key, name, _fname in list_custom_macros():
        try:
            with open(custom_path(key), "r", encoding="utf-8-sig") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        for raw in text.splitlines():
            words = _strip_macro_comment(raw).split()
            if not words:
                continue
            found = []
            if words[0].lower() == "imgclick" and len(words) > 1:
                found.append(words[1])
            found += [words[i + 1] for i, w in enumerate(words[:-1]) if w.lower() == "find"]
            for w in found:
                img = normalize_image_name(w)
                if img and name not in used.setdefault(img, []):
                    used[img].append(name)
    return used


def macro_picture_list():
    """[(file name, [macros that use it])] for the Macros tab of Fix image detection: every picture
    a macro looks for, plus every picture the user dropped into asset_override that isn't one of
    the macro's own (those are on the UI / Loot tabs)."""
    builtin = {f for _label, f in ASSET_LIST}
    used = macro_image_usage()
    names = set(used)
    try:
        for f in os.listdir(override_dir()):
            if f.lower().endswith(MACRO_IMG_EXTS) and os.path.isfile(os.path.join(override_dir(), f)):
                names.add(f)
    except OSError:
        pass
    names -= builtin
    return [(n, used.get(n, [])) for n in sorted(names, key=str.lower)]


def _macro_num(word, what, lo=0.0, hi=MACRO_MAX_SECONDS):
    try:
        v = float(word)
    except ValueError:
        raise MacroSyntaxError(f"'{word}' is not a number ({what})")
    if not (lo <= v <= hi):
        raise MacroSyntaxError(f"{what} must be between {lo:g} and {hi:g}")
    return v


def _macro_int(word, what, lo=-20000, hi=20000):
    try:
        v = int(word)
    except ValueError:
        raise MacroSyntaxError(f"'{word}' is not a whole number ({what})")
    if not (lo <= v <= hi):
        raise MacroSyntaxError(f"{what} must be between {lo} and {hi}")
    return v


def _macro_key(word):
    key = resolve_macro_key(word)
    if not key:
        raise MacroSyntaxError(f"unknown key '{word}'")
    return key


STRAFE_WORDS = {"left": "strafe", "l": "strafe", "right": "strafe_d", "r": "strafe_d"}


def _strafe_key(word):
    """'left' / 'right' -> your Strafe A / Strafe D keybind. Anything else is read as a key (a, d ...)."""
    bind = STRAFE_WORDS.get(word.strip().lower())
    return resolve_macro_key(bind) if bind else resolve_macro_key(word)


def _macro_args(cmd, args, lo, hi, usage):
    if not (lo <= len(args) <= hi):
        raise MacroSyntaxError(f"'{cmd}' needs: {usage}")


def _strip_macro_comment(s):
    s = s.strip()
    if s.startswith("#") or s.startswith("//"):
        return ""
    return re.split(r"\s+(?:#|//)", s, maxsplit=1)[0].strip()


def _parse_macro_command(cmd, args, line):
    """One line -> a node tuple (op, ...)."""
    if cmd in ("press", "tap"):
        _macro_args(cmd, args, 1, 1, "press KEY")
        return ("press", _macro_key(args[0]))
    if cmd in ("doubletap", "dtap"):
        _macro_args(cmd, args, 1, 1, "doubletap KEY")
        return ("doubletap", _macro_key(args[0]))
    if cmd == "down":
        _macro_args(cmd, args, 1, 1, "down KEY")
        return ("down", _macro_key(args[0]))
    if cmd == "up":
        _macro_args(cmd, args, 1, 1, "up KEY")
        return ("up", _macro_key(args[0]))
    if cmd == "hold":
        _macro_args(cmd, args, 2, 2, "hold KEY SECONDS")
        return ("hold", _macro_key(args[0]), _macro_num(args[1], "seconds"))
    if cmd in ("wait", "sleep"):
        _macro_args(cmd, args, 1, 1, "wait SECONDS   (or a random one: wait 0.5-1.2)")
        word = args[0]
        if "-" in word[1:]:
            a, b = word.split("-", 1)
            lo, hi = _macro_num(a, "seconds"), _macro_num(b, "seconds")
            if hi < lo:
                raise MacroSyntaxError("in 'wait A-B' the second number must not be smaller")
            return ("wait", lo, hi)
        v = _macro_num(word, "seconds")
        return ("wait", v, v)
    if cmd == "strafe":
        usage = "strafe left|right|KEY [SECONDS] [GAP]   (no SECONDS = one strafe)"
        _macro_args(cmd, args, 1, 3, usage)
        key = _strafe_key(args[0])
        if not key:
            raise MacroSyntaxError(f"'{args[0]}' is not a strafe direction - use left, right or a key like a / d")
        secs = _macro_num(args[1], "seconds") if len(args) >= 2 else 0.0
        gap = _macro_num(args[2], "gap", 0.0, 60) if len(args) == 3 else None
        return ("strafe", key, secs, gap, args[0].lower())
    if cmd == "strafefor":
        usage = "strafefor SECONDS [left|right|KEY] [GAP]   (default direction: left = your Strafe A keybind)"
        _macro_args(cmd, args, 1, 3, usage)
        word = args[1] if len(args) >= 2 else "left"
        key = _strafe_key(word)
        if not key:
            raise MacroSyntaxError(f"'{word}' is not a strafe direction - use left, right or a key like a / d")
        gap = _macro_num(args[2], "gap", 0.0, 60) if len(args) == 3 else None
        return ("strafefor", key, _macro_num(args[0], "seconds"), gap, word.lower())
    if cmd == "lookdown":
        _macro_args(cmd, args, 0, 2, "lookdown [SECONDS] [PIXELS]   (default: 2 seconds, 15 pixels per step)")
        secs = _macro_num(args[0], "seconds", 0.0, 20.0) if args else None
        pix = _macro_int(args[1], "pixels per step", 1, 200) if len(args) == 2 else None
        return ("lookdown", secs, pix)
    if cmd == "dashfor":
        _macro_args(cmd, args, 1, 2, "dashfor SECONDS [KEY]   (KEY defaults to your Dash keybind)")
        key = _macro_key(args[1] if len(args) == 2 else "dash")
        return ("dashfor", key, _macro_num(args[0], "seconds"))
    if cmd == "spam":
        _macro_args(cmd, args, 2, 3, "spam KEY SECONDS [GAP]")
        gap = _macro_num(args[2], "gap", 0.02, 60) if len(args) == 3 else 0.1
        return ("spam", _macro_key(args[0]), _macro_num(args[1], "seconds"), gap, args[0].lower())
    if cmd == "click":
        button = "left"
        rest = list(args)
        if rest and rest[-1].lower() in ("left", "right"):
            button = rest.pop().lower()
        if len(rest) not in (0, 2):
            raise MacroSyntaxError("'click' needs: click   or   click X Y   (+ left / right)")
        if rest:
            return ("click", _macro_int(rest[0], "X", 0), _macro_int(rest[1], "Y", 0), button)
        return ("click", None, None, button)
    if cmd == "move":
        _macro_args(cmd, args, 2, 2, "move X Y")
        return ("move", _macro_int(args[0], "X", 0), _macro_int(args[1], "Y", 0))
    if cmd == "moverel":
        _macro_args(cmd, args, 2, 2, "moverel DX DY")
        return ("moverel", _macro_int(args[0], "DX"), _macro_int(args[1], "DY"))
    if cmd == "imgclick":
        usage = "imgclick IMAGE.png [SECONDS] [right]   (SECONDS = keep looking for it that long)"
        rest = list(args)
        button = "left"
        if rest and rest[-1].lower() in ("left", "right"):
            button = rest.pop().lower()
        if not 1 <= len(rest) <= 2:
            raise MacroSyntaxError(f"'imgclick' needs: {usage}")
        img = normalize_image_name(rest[0])
        if img is None:
            raise MacroSyntaxError(f"'{rest[0]}' is not a picture file name (use a name like "
                                   "retry.png - no spaces, no folders)")
        secs = _macro_num(rest[1], "seconds") if len(rest) == 2 else 0.0
        return ("imgclick", img, secs, button)
    if cmd in MACRO_FUNCS:
        _macro_args(cmd, args, 0, 0, f"{cmd}()")
        return (cmd,)
    if cmd == "find":
        raise MacroSyntaxError("'find' only works inside a condition, like:  if find retry.png")
    if cmd in ("log", "status"):
        text = line.split(None, 1)[1] if len(args) else ""
        return (cmd, text)
    raise MacroSyntaxError(f"unknown command '{cmd}'")


def parse_macro(text):
    """Pseudo-script text -> (meta dict, program, errors).
    program = list of nodes. A repeat node is ("repeat", count or None, [nodes], line); an if node is
    ("if", condition, [nodes when true], [nodes when false], line).
    errors = [(line number, message)] - empty when the script is fine.
    Indentation is optional: only the words on a line matter, never the spaces before them."""
    meta, errors = {}, []
    root = []
    stack = [(root, 0, "root", None)]   # (list the next node goes into, line that opened it, kind, node)
    for no, raw in enumerate(text.splitlines(), 1):
        line = _strip_macro_comment(raw)
        if not line:
            continue
        m = MACRO_HEADER_RE.match(line)
        if m:
            field, value = m.group(1).lower(), m.group(2).strip()
            if field == "type":
                kind = normalize_macro_type(value)
                if kind is None:
                    errors.append((no, f"unknown type '{value}' - use: " + ", ".join(MACRO_TYPES)))
                else:
                    value = kind
            meta[field] = value
            continue
        parts = line.split()
        call = MACRO_CALL_RE.match(line)            # retry()  /  addwin ( )
        if call:
            cmd, args = call.group(1).lower(), []
        else:
            cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd == "repeat":
                _macro_args(cmd, args, 0, 1, "repeat [TIMES]  ...  end")
                times = _macro_int(args[0], "times", 1, 100000) if args else None
                if len(stack) > MACRO_MAX_DEPTH:
                    raise MacroSyntaxError(f"blocks are nested more than {MACRO_MAX_DEPTH} deep")
                node = ("repeat", times, [], no)
                stack[-1][0].append(node)
                stack.append((node[2], no, "repeat", node))
            elif cmd == "if":
                err = None
                try:
                    cond = _parse_condition(args)
                except MacroSyntaxError as e:
                    cond, err = None, e         # still opens a block, so its 'end' isn't a second error
                if len(stack) > MACRO_MAX_DEPTH and err is None:
                    err = MacroSyntaxError(f"blocks are nested more than {MACRO_MAX_DEPTH} deep")
                node = ("if", cond, [], [], no)
                stack[-1][0].append(node)
                stack.append((node[2], no, "if", node))
                if err is not None:
                    raise err
            elif cmd == "else":
                _macro_args(cmd, args, 0, 0, "else   (on its own line)")
                if stack[-1][2] == "else":
                    raise MacroSyntaxError("this 'if' already has an 'else'")
                if stack[-1][2] != "if":
                    raise MacroSyntaxError("'else' without an 'if' above it")
                _body, opened, _kind, node = stack[-1]
                stack[-1] = (node[3], opened, "else", node)
            elif cmd == "end":
                _macro_args(cmd, args, 0, 0, "end   (on its own line)")
                if len(stack) == 1:
                    raise MacroSyntaxError("'end' without an 'if' or 'repeat' above it")
                stack.pop()
            else:
                stack[-1][0].append(_parse_macro_command(cmd, args, line) + (no,))
        except MacroSyntaxError as e:
            errors.append((no, str(e)))
    for _body, opened, kind, _node in stack[1:]:
        errors.append((opened, f"'{'if' if kind == 'else' else kind}' is never closed with 'end'"))
    return meta, root, sorted(errors)


def count_macro_nodes(nodes):
    total = 0
    for n in nodes:
        total += 1
        if n[0] == "repeat":
            total += count_macro_nodes(n[2])
        elif n[0] == "if":
            total += count_macro_nodes(n[2]) + count_macro_nodes(n[3])
    return total


# ----- the pseudo-script: running (inside the macro thread, so stop / pause / retry just work) -----
def _macro_mouse_rel(dx, dy):
    if IS_WIN:
        ctypes.windll.user32.mouse_event(0x0001, int(dx), int(dy), 0, 0)   # MOUSEEVENTF_MOVE
    else:
        pydirectinput.moveRel(int(dx), int(dy), _pause=False)


def _macro_click(x, y, button):
    if x is not None:
        pydirectinput.moveTo(x, y)
        wait(0.08, scan=False)
        pydirectinput.moveRel(1, 0)         # a real mouse move, same as the retry click
        wait(0.05, scan=False)
    pydirectinput.click(button=button)


_macro_img_cache = {}      # picture name -> (stamp, template or None)
_macro_img_warned = set()  # pictures already reported as missing (one log line each)


def _macro_image(name):
    """The picture a script looks for: the user's own copy in asset_override wins, else a built-in
    one of that name in the image folder. Their picture is scaled to 1080p size like every other
    (see load_template). Returns None - and says so once in the log - when there is none."""
    mine = override_path(name)
    try:
        stamp = ("mine", os.path.getmtime(mine)) if mine else ("built", os.path.isfile(asset_path(name)))
    except OSError:
        stamp = ("mine", 0)
    stamp += (round(_scale, 3),)
    hit = _macro_img_cache.get(name)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        tpl = load_template(name)
    except FileNotFoundError:
        tpl = None
    _macro_img_cache[name] = (stamp, tpl)
    if tpl is None and name not in _macro_img_warned:
        _macro_img_warned.add(name)
        log(f"Macro picture '{name}' not found - add it in Settings > Fix image detection > Macros "
            "(until then 'find' says not seen)")
    return tpl


def _macro_find(name, screen):
    """Match (cx, cy, score, w, h) of the picture on that screenshot, or None."""
    tpl = _macro_image(name)
    if tpl is None:
        return None
    return find_template(screen, tpl, CUSTOM_IMG_THRESHOLD)


def _macro_eval(cond, shot):
    """True / False for a condition tree. `shot` = [screenshot or None]: taken the first time a
    'find' needs it, so 'a and b' / 'a or b' look at one and the same picture of the screen."""
    kind = cond[0]
    if kind == "find":
        if shot[0] is None:
            shot[0] = grab_screen()
        return _macro_find(cond[1], shot[0]) is not None
    if kind == "not":
        return not _macro_eval(cond[1], shot)
    if kind == "and":
        return _macro_eval(cond[1], shot) and _macro_eval(cond[2], shot)
    return _macro_eval(cond[1], shot) or _macro_eval(cond[2], shot)


def _macro_imgclick(name, seconds, button):
    """Find the picture on screen and click its middle. With seconds > 0 keep looking that long
    (stop / pause / the retry button keep working meanwhile); not found = skipped, the macro goes on."""
    end = time.time() + seconds
    while True:
        tick()
        hit = _macro_find(name, grab_screen())
        if hit:
            cx, cy, _score, tw, th = hit
            jit = max(1, int(min(tw, th) * _scale)) // 6
            x = _monitor["left"] + int(cx * _scale) + random.randint(-jit, jit)
            y = _monitor["top"] + int(cy * _scale) + random.randint(-jit, jit)
            detail(f"Clicking {name}")
            _macro_click(x, y, button)
            return True
        left = end - time.time()
        if left <= 0:
            if seconds > 0:
                log(f"imgclick: {name} not seen after {seconds:g}s - skipped")
            else:
                detail(f"{name} not on screen - skipped")
            return False
        detail(f"Looking for {name}... {left:.0f}s")
        wait(min(0.15, left))


def _macro_strafe(key, seconds, gap, label):
    """One strafe = the key double-tapped (like a-a). With SECONDS: strafe again and again that long,
    with the same random pause between strafes as the built-in configs (or your own GAP)."""
    if seconds <= 0:
        double_tap(key)
        return
    _macro_strafefor(key, seconds, gap, label)


def _macro_strafefor(key, seconds, gap, label):
    """The built-in strafe (the key double-tapped, like a-a) over and over for SECONDS, with the same
    random pause between strafes as the built-in configs (Edit configs > Gap between strafes), or
    your own GAP. It returns right after the last strafe: no pause is waited out at the end, so the
    next line of the macro starts at once."""
    end = time.time() + seconds
    while True:
        left = end - time.time()
        if left <= 0:
            return
        detail(f"Strafing {label}... {left:.0f}s left")
        double_tap(key)
        pause = gap if gap is not None else random.uniform(*tv("Shiki", "strafe_gap"))
        if end - time.time() <= pause:      # the next strafe would not fit: finish now
            return
        wait(pause)


def macro_retry():
    """retry(): do what every config does at the end of a mission - wait for the retry button. When
    it shows up the normal retry handling takes over (result + loot, click, hands off, next
    mission), so the lines below a retry() never run."""
    set_step(1)
    show("Waiting for retry", "retry() - waiting for the retry button")
    log("Macro: retry() - waiting for the retry button")
    while True:
        wait(0.1)


def macro_trackloot():
    """trackloot(): look for the loot pictures on screen now and add what is seen to the loot
    counters. The retry handling then doesn't count the same loot a second time."""
    if not any(_loot_templates.values()):
        log("trackloot(): no loot pictures loaded - nothing to look for (Settings > Fix image detection)")
        return
    show("Tracking loot", "Looking for the loot pictures")
    found = {}
    for quality, scan_time in (("normal", None), ("low", LOW_LOOT_SCAN_TIME)):
        if not _loot_templates.get(quality):
            continue
        got, _result = scan_end_screen(quality, want_loot=True, want_result=False,
                                       scan_time=scan_time)
        found.update(got)
        if found:
            break
    record_loot(found)
    if found:
        state["macro_loot"] = {**(state.get("macro_loot") or {}), **found}


def macro_openchest():
    """openchest(): the raid's chest part. Waits for the finish button, then opens chest 1 (and chest
    2 when it is on) and clicks finish - the spots and click times are the Female Raid ones
    (Settings > Configs > Chest locations / Edit configs)."""
    if not _raid_templates.get("finish"):
        log("openchest(): finish_button.png not found - add it in Settings > Configs > "
            "Fix image detection")
        return
    log("openchest(): waiting for the finish button")
    hit = None
    while hit is None:
        show("Opening chests", "Waiting for the finish button...")
        hit = raid_wait_finish_button(30)
    raid_finish(hit, phase=False)


def macro_add_result(kind):
    """addwin() / addlose(): one more Completed / Failed (the raid's for a raid macro). The
    retry handling then doesn't read the result a second time."""
    state["macro_result"] = kind
    counters()[1][kind] += 1
    log(f"Result: {kind.capitalize()} (added by the macro)")
    push_stats()


def macro_add_count(raid):
    """addmission() / addraid(): one more in the Missions / Raids counter."""
    (raid_stats if raid else stats)["missions"] += 1
    log(f"{'Raid' if raid else 'Mission'} added by the macro")
    push_stats()


def run_macro_nodes(nodes):
    for node in nodes:
        op = node[0]
        if op == "press":
            tap(node[1])
        elif op == "doubletap":
            double_tap(node[1])
        elif op == "down":
            pydirectinput.keyDown(node[1])
            _custom_held.add(node[1])
        elif op == "up":
            pydirectinput.keyUp(node[1])
            _custom_held.discard(node[1])
        elif op == "hold":
            pydirectinput.keyDown(node[1])
            try:
                wait(node[2])
            finally:
                pydirectinput.keyUp(node[1])
        elif op == "wait":
            secs = random.uniform(node[1], node[2]) if node[2] > node[1] else node[1]
            detail(f"Waiting {secs:.1f}s")
            wait(secs)
        elif op == "lookdown":
            secs = LOOK_DOWN_TIME if node[1] is None else node[1]
            pix = LOOK_DOWN_PIXELS if node[2] is None else node[2]
            show("Looking down", "ctrl, mouse down, ctrl")
            log(f"Macro: looking down ({secs:g}s, {pix}px per step)")
            look_down(lambda: tick(scan_retry=False), secs, pix, settle=0)
        elif op == "dashfor":
            log(f"Macro: dashing for {node[2]:g}s")
            spam_recorded_dash(node[1], node[2], tight=True)    # same held presses as the built-in configs
        elif op == "spam":
            spam_key_for(node[1], node[2], node[3], node[4])
        elif op == "strafe":
            _macro_strafe(node[1], node[2], node[3], node[4])
        elif op == "strafefor":
            log(f"Macro: strafing {node[4]} for {node[2]:g}s")
            _macro_strafefor(node[1], node[2], node[3], node[4])
        elif op == "click":
            _macro_click(node[1], node[2], node[3])
        elif op == "imgclick":
            _macro_imgclick(node[1], node[2], node[3])
        elif op == "if":
            tick()                              # stop / pause / retry work even in a tight loop of ifs
            run_macro_nodes(node[2] if _macro_eval(node[1], [None]) else node[3])
        elif op == "retry":
            macro_retry()
        elif op == "trackloot":
            macro_trackloot()
        elif op == "openchest":
            macro_openchest()
        elif op == "addwin":
            macro_add_result("completed")
        elif op == "addlose":
            macro_add_result("failed")
        elif op == "addmission":
            macro_add_count(False)
        elif op == "addraid":
            macro_add_count(True)
        elif op == "move":
            pydirectinput.moveTo(node[1], node[2])
        elif op == "moverel":
            _macro_mouse_rel(node[1], node[2])
        elif op == "log":
            log(node[1])
        elif op == "status":
            show(node[1])
        elif op == "repeat":
            if node[1] is None:             # no number: until the mission ends (the retry button)
                while True:
                    run_macro_nodes(node[2])
                    wait(0.02)              # never a hot loop, even with an empty body
            else:
                for _ in range(node[1]):
                    run_macro_nodes(node[2])


def custom_routine():
    """The mission of a custom macro: run the script once, then wait for the retry button like every
    other config. Never returns normally (ends with RetryFound / stop)."""
    key = settings["family"]
    try:
        with open(custom_path(key), "r", encoding="utf-8-sig") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as e:
        raise RuntimeError(f"custom macro '{custom_filename(key)}' could not be read: {e}")
    meta, program, errors = parse_macro(text)
    title = meta.get("name") or os.path.splitext(custom_filename(key))[0]
    if errors:
        for no, msg in errors[:8]:
            log(f"{title}: line {no}: {msg}")
        raise RuntimeError(f"custom macro '{title}' has {len(errors)} error(s) - fix it in "
                           "Settings > Configs (the Edit button next to the dropdown)")
    _macro_img_cache.clear()
    _macro_img_warned.clear()
    set_step(0)
    show(title, "Running your macro")
    kind = normalize_macro_type(meta.get("type")) or DEFAULT_MACRO_TYPE
    log(f"Custom macro '{title}': running  (type: {kind}"
        + ("" if kind == "mission" else " - not counted as a mission") + ")")
    try:
        run_macro_nodes(program)
        set_step(1)
        show("Waiting for retry", f"'{title}' is finished")
        log(f"Custom macro '{title}': finished - waiting for the retry button")
        while True:
            wait(0.1)
    finally:
        for k in list(_custom_held):        # a macro that pressed "down" and never "up"
            try:
                pydirectinput.keyUp(k)
            except Exception:  # noqa: BLE001
                pass
        _custom_held.clear()


# ----- recording -----
def start_mouse_watch(on_click):
    """Call on_click(x, y, 'left' | 'right') for every click anywhere on the screen.
    Returns a function that stops watching, or None if clicks can't be watched on this system."""
    if IS_WIN:
        from ctypes import wintypes
        stop = threading.Event()

        def poll():
            user32 = ctypes.windll.user32
            pt = wintypes.POINT()
            was = {0x01: False, 0x02: False}
            while not stop.is_set():
                for vk, name in ((0x01, "left"), (0x02, "right")):
                    down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                    if down and not was[vk]:
                        user32.GetCursorPos(ctypes.byref(pt))
                        try:
                            on_click(pt.x, pt.y, name)
                        except Exception:  # noqa: BLE001
                            pass
                    was[vk] = down
                time.sleep(0.004)
        threading.Thread(target=poll, daemon=True).start()
        return stop.set
    try:
        from pynput import mouse as pm
    except Exception:  # noqa: BLE001
        return None

    def on_c(x, y, button, pressed):
        if pressed and button in (pm.Button.left, pm.Button.right):
            on_click(int(x), int(y), "left" if button == pm.Button.left else "right")
    lis = pm.Listener(on_click=on_c)
    lis.daemon = True
    lis.start()
    return lis.stop


class MacroRecorder:
    """Records key presses and mouse clicks and turns them into pseudo-script lines.
    Not recorded: mouse movement (camera turns) - add those with 'moverel'."""
    MIN_WAIT = 0.02         # shorter gaps are not written
    PRESS_MAX = 0.30        # a key held shorter than this becomes 'press', longer becomes 'hold'
    PRESS_COST = 0.05       # what a 'press' takes while it runs (taken off the next 'wait')
    CLICK_COST = 0.15

    def __init__(self, use_binds=True):
        self.use_binds = use_binds
        self.events = []                # (seconds, "down" | "up" | "click", data)
        self.t0 = None                  # None = not recording yet (countdown)
        self.stop_requested = False
        self.skipped = set()
        self.ignore = None              # (x1, y1, x2, y2): clicks in here (the indicator) are not recorded
        self.mouse_ok = False
        self._down = set()
        self._handles = []
        self._mouse_stop = None

    def start(self):
        unregister_hotkeys()            # F8 / F6 / F7 must not run the macro while the player plays
        self._handles = [keyboard.on_press(self._on_down), keyboard.on_release(self._on_up)]
        self._mouse_stop = start_mouse_watch(self._on_click)
        self.mouse_ok = self._mouse_stop is not None

    def begin(self):
        self.t0 = time.perf_counter()

    def stop(self):
        for h in self._handles:
            try:
                keyboard.unhook(h)
            except (KeyError, ValueError):
                pass
        self._handles = []
        if self._mouse_stop is not None:
            try:
                self._mouse_stop()
            except Exception:  # noqa: BLE001
                pass
            self._mouse_stop = None
        register_hotkeys()

    def _now(self):
        return time.perf_counter() - self.t0

    def _on_down(self, e):
        if self.t0 is None:
            return
        name = (e.name or "").lower()
        if name == QUIT_KEY.lower():
            self.stop_requested = True
            return
        if name in (START_KEY.lower(), PAUSE_KEY.lower()):
            return
        key = to_game_key(name, bool(getattr(e, "is_keypad", False)))
        if key is None:
            self.skipped.add(name)
            return
        if key in self._down:           # auto-repeat of a key that is held
            return
        self._down.add(key)
        self.events.append((self._now(), "down", key))

    def _on_up(self, e):
        if self.t0 is None:
            return
        key = to_game_key((e.name or "").lower(), bool(getattr(e, "is_keypad", False)))
        if key is None or key not in self._down:
            return
        self._down.discard(key)
        self.events.append((self._now(), "up", key))

    def _on_click(self, x, y, button):
        if self.t0 is None:
            return
        r = self.ignore
        if r and r[0] <= x <= r[2] and r[1] <= y <= r[3]:
            return
        self.events.append((self._now(), "click", (int(x), int(y), button)))

    def _name(self, key):
        """With 'use my keybind names' on, the key you bound to ability 2 is written as ability2."""
        if self.use_binds:
            for bind_id in GAME_BINDS:
                if settings["keybinds"].get(bind_id) == key:
                    return bind_id
        return key

    def to_lines(self):
        evs = sorted(self.events, key=lambda e: e[0])
        lines, held = [], set()
        cursor, owed, i = 0.0, 0.0, 0
        while i < len(evs):
            t, kind, data = evs[i]
            span = t - cursor
            if span - owed >= self.MIN_WAIT:
                lines.append(f"wait {span - owed:.2f}")
                owed = 0.0
            else:
                owed = max(0.0, owed - span)
            cursor = t
            if kind == "click":
                x, y, button = data
                lines.append(f"click {x} {y}" + (" right" if button == "right" else ""))
                owed += self.CLICK_COST
            elif kind == "down":
                nxt = evs[i + 1] if i + 1 < len(evs) else None
                if nxt and nxt[1] == "up" and nxt[2] == data:
                    dur = nxt[0] - t
                    if dur < self.PRESS_MAX:
                        lines.append(f"press {self._name(data)}")
                        owed += self.PRESS_COST
                    else:
                        lines.append(f"hold {self._name(data)} {dur:.2f}")
                    cursor = nxt[0]
                    i += 1
                else:
                    lines.append(f"down {self._name(data)}")
                    held.add(data)
            else:
                lines.append(f"up {self._name(data)}")
                held.discard(data)
            i += 1
        for k in sorted(held):          # still held when recording stopped
            lines.append(f"up {self._name(k)}")
        return lines


# --------------------------- Discord webhook ----------------------------
DISCORD_API = "https://discord.com/api/v10"


def bot_ready():
    """True when bot mode is on and the token / channel ID look usable."""
    wh = settings["webhook"]
    return bool(wh["use_bot"] and len(wh["bot_token"].strip()) >= 30
                and wh["channel_id"].strip().isdigit())


def webhook_url():
    """Where messages are sent: the bot's channel (bot mode) or the webhook URL. '' = Discord is off.
    (The name is kept: every "is Discord on?" check in the macro uses it.)"""
    wh = settings["webhook"]
    if wh["use_bot"]:
        return f"{DISCORD_API}/channels/{wh['channel_id'].strip()}/messages" if bot_ready() else ""
    url = wh["url"].strip()
    return url if url.startswith("https://") and "/api/webhooks/" in url else ""


def mention_id():
    wh = settings["webhook"]
    uid = wh["user_id"].strip()
    return uid if wh["mention"] and uid.isdigit() else ""


def post_webhook(text, png=None, ping=True, title="ymir", embed=None):
    """Send one message (optionally with a PNG picture). Blocks; returns (ok, message)."""
    url = webhook_url()
    if not url:
        if settings["webhook"]["use_bot"]:
            return False, "The bot token or the channel ID is missing / not valid"
        return False, "No valid webhook URL (it starts with https://discord.com/api/webhooks/)"
    uid = mention_id() if ping else ""
    payload = {"content": f"<@{uid}> {text}" if uid else text,
               "allowed_mentions": {"users": [uid]} if uid else {"parse": []}}
    if embed is not None:
        payload["embeds"] = [embed]
    if png is None:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    else:
        base = embed if embed is not None else {"title": title, "color": 0x3B82F6}
        payload["embeds"] = [dict(base, image={"url": "attachment://stats.png"})]
        boundary = uuid.uuid4().hex
        body = b"".join([
            (f"--{boundary}\r\n"
             'Content-Disposition: form-data; name="payload_json"\r\n'
             "Content-Type: application/json\r\n\r\n").encode(),
            json.dumps(payload).encode("utf-8"), b"\r\n",
            (f"--{boundary}\r\n"
             'Content-Disposition: form-data; name="files[0]"; filename="stats.png"\r\n'
             "Content-Type: image/png\r\n\r\n").encode(),
            png, b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    headers["User-Agent"] = "ymir-macro/1.0"   # Discord rejects the default python user agent
    if settings["webhook"]["use_bot"]:
        headers["Authorization"] = "Bot " + settings["webhook"]["bot_token"].strip()
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT):
            return True, "Sent"
    except urllib.error.HTTPError as e:
        if settings["webhook"]["use_bot"] and e.code in (401, 403, 404):
            return False, {401: "Discord rejected the bot token (401)",
                           403: "The bot can't write in that channel (403): give it View Channel, "
                                "Send Messages and Attach Files",
                           404: "Channel not found (404): check the channel ID"}[e.code]
        return False, f"Discord answered {e.code} {e.reason}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"Could not reach Discord: {e}"


def notify_async(text):
    """Send a webhook message in the background (never slows the macro down)."""
    if not webhook_url():
        return

    def work():
        ok, msg = post_webhook(text)
        if not ok:
            log(f"Discord: {msg}")
    threading.Thread(target=work, daemon=True).start()


# ------------------------- Discord bot: !commands -------------------------
# The bot reads the channel with plain HTTPS requests (no extra library): every BOT_POLL_SECONDS it
# asks for the messages that are newer than the last one it has seen. Needs "Message Content Intent"
# turned on for the bot in the Discord developer portal. Only the user IDs typed in Settings may use it.
BOT_PREFIX = "!"
BOT_POLL_SECONDS = 2.0
BOT_COMMANDS_TEXT = "!start  !pause  !resume  !stop  !reconnect  !screenshot  !stats  !status  !help"
_bot = {"stop": None, "sig": None}


def bot_allowed_ids():
    """The Discord user IDs that may give commands (the User ID box; several separated by commas)."""
    raw = settings["webhook"]["user_id"]
    return {p for p in re.split(r"[,\s]+", raw.strip()) if p.isdigit()}


def _bot_api_get(path, token):
    req = urllib.request.Request(DISCORD_API + path, method="GET", headers={
        "Authorization": "Bot " + token, "User-Agent": "ymir-macro/1.0"})
    with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8") or "[]")


def _bot_say(text, png=None, title="ymir"):
    ok, msg = post_webhook(text, png=png, ping=False, title=title)
    if not ok:
        log(f"Discord bot: {msg}")


def _bot_status_text():
    st, res, _loot = counters()
    if not running():
        phase = "Not running"
    elif state["quit"]:
        phase = "Stopping"
    elif state["paused"]:
        phase = "Paused"
    else:
        phase = str(state["phase_status"]).capitalize()
    return (f"{phase}  -  missions: {st['missions']}, completed: {res['completed']}, "
            f"failed: {res['failed']}")


def _bot_run(cmd):
    """Run one command (in its own thread, so a slow screenshot never blocks the listener)."""
    try:
        if cmd == "start":
            if running() and not state["quit"]:
                request_start()
                _bot_say("The macro is already running. " + _bot_status_text())
            else:
                request_start()
                _bot_say("Starting the macro...")
        elif cmd == "pause":
            if not running():
                _bot_say("The macro isn't running.")
            else:
                toggle_pause()
                _bot_say("Paused." if state["paused"] else "Resumed.")
        elif cmd == "resume":
            if running() and state["paused"]:
                toggle_pause()
                _bot_say("Resumed.")
            else:
                _bot_say("The macro isn't paused.")
        elif cmd == "stop":
            if running():
                request_stop()
                _bot_say("Stopping the macro...")
            else:
                _bot_say("The macro isn't running.")
        elif cmd in ("screenshot", "ss", "screen"):
            png = cv2.imencode(".png", grab_roblox())[1].tobytes()
            _bot_say("", png=png, title="Screenshot")
        elif cmd in ("stats", "graph"):
            ok, msg = send_stats_graph("Stats", partial=True)
            if not ok:
                _bot_say(f"Could not send the stats: {msg}")
        elif cmd == "status":
            _bot_say(_bot_status_text())
        elif cmd == "reconnect":
            if not running():
                _bot_say("The macro isn't running.")
            elif _disconnect_template is None:
                missing = reconnect_missing()
                _bot_say("Reconnect is off. " + ("Still needed: " + ", ".join(missing) + "."
                                                 if missing else "Turn it on in Recovery > Reconnect."))
            elif state.get("reconnecting"):
                _bot_say("Already reconnecting.")
            else:
                state["rc_request"] = True
                _bot_say("Reconnecting: closing Roblox, rejoining and clicking through the lobby...")
        elif cmd in ("help", "commands"):
            _bot_say("Commands: " + BOT_COMMANDS_TEXT)
    except Exception as e:  # noqa: BLE001
        log(f"Discord bot: command '{cmd}' failed: {type(e).__name__}: {e}")


def _bot_handle(msg, warned):
    author = msg.get("author") or {}
    if author.get("bot"):
        return
    text = (msg.get("content") or "").strip()
    if not text:
        if not warned.get("empty") and not msg.get("attachments") and not msg.get("embeds"):
            warned["empty"] = True
            log("Discord bot: the messages arrive empty - turn on 'Message Content Intent' "
                "for the bot in the Discord developer portal")
        return
    if not text.startswith(BOT_PREFIX):
        return
    parts = text[len(BOT_PREFIX):].split()
    if not parts:
        return
    cmd = parts[0].lower()
    allowed = bot_allowed_ids()
    if str(author.get("id")) not in allowed:
        if not allowed and not warned.get("noid"):
            warned["noid"] = True
            _bot_say("Commands are off: type your Discord user ID in ymir > Settings > Discord first.")
        return
    threading.Thread(target=_bot_run, args=(cmd,), daemon=True).start()


def _bot_loop(stop, token, channel):
    last, warned, down = None, {}, False
    while not stop.is_set():
        wait_s = BOT_POLL_SECONDS
        try:
            if last is None:     # first look: remember the newest message, don't run old commands
                first = _bot_api_get(f"/channels/{channel}/messages?limit=1", token)
                last = max((int(m["id"]) for m in first), default=0)
                log(f"Discord bot: listening for commands ({BOT_COMMANDS_TEXT})")
            else:
                msgs = _bot_api_get(f"/channels/{channel}/messages?after={last}&limit=20", token)
                for m in sorted(msgs, key=lambda m: int(m["id"])):
                    last = max(last, int(m["id"]))
                    _bot_handle(m, warned)
            down = False
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                why = {401: "the token was rejected (401)",
                       403: "the bot can't read that channel (403): give it View Channel + Read Message History",
                       404: "channel not found (404)"}[e.code]
                log(f"Discord bot: {why} - check Settings > Discord")
                return
            if e.code == 429:
                try:
                    wait_s = min(30.0, float(json.loads(e.read().decode("utf-8")).get("retry_after", 5)))
                except Exception:  # noqa: BLE001
                    wait_s = 5.0
            elif not down:
                down = True
                log(f"Discord bot: Discord answered {e.code} {e.reason}")
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
            if not down:
                down = True
                log(f"Discord bot: could not reach Discord ({e})")
        stop.wait(wait_s)


def bot_restart():
    """Start / stop / restart the command listener so it matches the Discord settings."""
    wh = settings["webhook"]
    sig = (bool(wh["use_bot"]), wh["bot_token"].strip(), wh["channel_id"].strip())
    if sig == _bot["sig"]:
        return
    _bot["sig"] = sig
    if _bot["stop"] is not None:
        _bot["stop"].set()
        _bot["stop"] = None
    if bot_ready():
        stop = threading.Event()
        _bot["stop"] = stop
        threading.Thread(target=_bot_loop, args=(stop, sig[1], sig[2]), daemon=True).start()


def loot_parts(found):
    """['1 scroll', '2 fragments (body, defence)', '1 serum (attack)'] for the kinds in `found`."""
    parts = []
    raid = raid_counting()
    if "scroll" in found and not raid:
        parts.append("1 scroll")
    if "vessel" in found and raid:
        parts.append("1 vessel")
    frags = [k for k in FRAGMENT_KINDS if k in found]
    if frags:
        parts.append(f"{len(frags)} fragment{'s' if len(frags) != 1 else ''} ({', '.join(frags)})")
    serums = [k.replace("_serum", "") for k in SERUM_KINDS if k in found]
    if serums:
        parts.append(f"{len(serums)} serum{'s' if len(serums) != 1 else ''} ({', '.join(serums)})")
    return parts


LOOT_PINK = 0xF472B6      # the embed colour when any tracked loot dropped


# Custom Discord emojis (from another server: needs "Use External Emojis" for @everyone in the channel).
LOOT_EMOJI = {"scroll": "<:scroll:1502824781207310346>", "body": "<:body:1566849647677407352>",
              "defence": "<:defence:1566849590752186480>",
              "offence": "<:offence:1566849540211089428>",
              "support": "<:support:1566849487173849188>",
              "fragments": "<:fragments:1566862673713438851>",
              "vessel": "<:ScarletBlossomVessel:1566848731134034172>",
              "attack_serum": "<:AttackSerum:1566849853684715661>",
              "armor_serum": "<:ArmoredSerum:1566849901071962163>",
              "female_serum": "<:FemaleSerum:1566849755076759693>",
              "beast_serum": "<:BeastSerum:1566849940641292499>",
              "colossal_serum": "<:ColossalSerum:1566849808508002354>",
              "serums": "<:SerumBag:1566875481523884305>"}


# Bot mode: a bot can only show another server's emojis when it is in that server AND has "Use External
# Emojis", which is why they showed as plain :name: text. So the bot gets its own copies instead
# ("application emojis", owned by the bot, usable in every server it is in): the pictures are fetched from
# Discord's CDN by the ids above and uploaded once; later runs find them by name and reuse them.
_app_emoji = {"token": None, "map": {}, "retry": 0.0, "done": False}
_app_emoji_lock = threading.Lock()
EMOJI_PREFIX = "ymir_"


def _discord_call(method, path, token, payload=None):
    """One authenticated JSON call to the Discord API (waits out a 429 once or twice)."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": "Bot " + token, "User-Agent": "ymir-macro/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for _ in range(3):
        try:
            req = urllib.request.Request(DISCORD_API + path, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            try:
                delay = float(json.loads(e.read().decode("utf-8")).get("retry_after", 1))
            except (ValueError, OSError):
                delay = 1.0
            time.sleep(min(10.0, max(0.2, delay)))
    raise OSError("Discord rate limit")


def bot_emoji_map():
    """{loot key: '<:ymir_key:id>'} for the bot's own emojis (created on first use). Keys that could
    not be made are missing; a failed try is repeated after 5 minutes."""
    token = settings["webhook"]["bot_token"].strip()
    with _app_emoji_lock:
        c = _app_emoji
        if c["token"] != token:
            c.update(token=token, map={}, retry=0.0, done=False)
        if c["done"] or time.time() < c["retry"] or not token:
            return dict(c["map"])
        c["retry"] = time.time() + 300
        try:
            app_id = _discord_call("GET", "/applications/@me", token)["id"]
            have = _discord_call("GET", f"/applications/{app_id}/emojis", token).get("items", [])
            ids = {e["name"]: e["id"] for e in have}
        except Exception as exc:  # noqa: BLE001
            log(f"Discord bot emojis: could not read the bot's emojis ({type(exc).__name__}: {exc})")
            return dict(c["map"])
        for key, markup in LOOT_EMOJI.items():
            name = EMOJI_PREFIX + key
            try:
                if name not in ids:
                    src = re.match(r"<a?:\w+:(\d+)>", markup).group(1)
                    req = urllib.request.Request(
                        f"https://cdn.discordapp.com/emojis/{src}.png?size=128",
                        headers={"User-Agent": "ymir-macro/1.0"})
                    with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT) as r:
                        img = r.read()
                    made = _discord_call("POST", f"/applications/{app_id}/emojis", token, {
                        "name": name, "image": "data:image/png;base64," + base64.b64encode(img).decode()})
                    ids[name] = made["id"]
                c["map"][key] = f"<:{name}:{ids[name]}>"
            except Exception as exc:  # noqa: BLE001
                log(f"Discord bot emojis: {key} not made ({type(exc).__name__}: {exc})")
        c["done"] = len(c["map"]) == len(LOOT_EMOJI)
        return dict(c["map"])


def loot_emoji(key):
    """The emoji text for a loot kind: the original custom emoji for a webhook, the bot's own copy in bot
    mode ('' when that copy is not available, so no broken :name: text shows up)."""
    if settings["webhook"]["use_bot"]:
        return bot_emoji_map().get(key, "")
    return LOOT_EMOJI.get(key, "")


def loot_name(kind):
    """'attack_serum' -> 'Attack serum', 'body' -> 'Body'."""
    return kind.replace("_", " ").capitalize()


def mission_embed(result, found, mission_seconds=None):
    """The stats shown in Discord after every mission / raid, laid out like the app's stat cards
    (a screenshot is attached below them). Only the loot that can drop is shown:
    missions -> fragments, scrolls, serums totals, then each fragment and serum;
    Female Raid -> vessels, female serum, each fragment."""
    kind = result[0] if result else None
    stats, _results, loot = counters()
    raid = raid_counting()
    word = "Raid" if raid else "Mission"
    main_item = "vessel" if raid else "scroll"
    serum_kinds = ("female_serum",) if raid else SERUM_KINDS
    snap = snapshot()
    done = snap["completed"] + snap["failed"]
    rate = f"{100 * snap['completed'] // done}%" if done else "-"
    got = [f"{loot_emoji(k)} {loot_name(k)}".strip()
           for k in (((main_item,) + serum_kinds + FRAGMENT_KINDS) if raid
                     else ((main_item,) + FRAGMENT_KINDS + serum_kinds)) if k in found]
    run = fmt_time(time.time() - report["t0"]) if report["t0"] else "--:--"
    length = fmt_time(mission_seconds) if mission_seconds else "--:--"
    head = {"completed": "Completed", "failed": "Failed"}.get(kind, "Result not detected")

    def field(name, value=""):
        return {"name": name, "value": value or "​", "inline": True}

    def emo(k, name=None):   # a loot count with its emoji, like the app's coloured dots
        return field(name or k.upper(), f"{loot_emoji(k)} **{loot.get(k, 0)}**".strip())
    if raid:   # vessels, female serum, then the 4 fragments
        loot_fields = [emo("vessel", ui_word("scrolls").upper()), emo("female_serum", "FEMALE SERUM"),
                       *(emo(k) for k in FRAGMENT_KINDS)]
    else:
        loot_fields = [field("FRAGMENTS", f"{loot_emoji('fragments')} **{snap['fragments']}**"),
                       emo("scroll", ui_word("scrolls").upper()),
                       field("SERUMS", f"{loot_emoji('serums')} **{snap['serums']}**"),
                       # below the totals: each fragment, then each serum (9 = 3 full rows)
                       *(emo(k) for k in FRAGMENT_KINDS),
                       *(emo(k, k.split("_")[0].upper()) for k in SERUM_KINDS)]
    return {
        "author": {"name": f"ymir {VERSION}"},
        "title": f"{word} #{stats['missions']}  ·  {head}",
        # purple completed, red failed, grey otherwise
        "color": {"completed": 0xA855F7, "failed": 0xF87171}.get(kind, 0x7A8398),
        "description": "**LOOT**  " + ("   ".join(got) if got else f"nothing this {word.lower()}"),
        "fields": [field(ui_word("missions").upper(), f"**{stats['missions']}**"),
                   field("COMPLETED", f"🟣 **{snap['completed']}**"),
                   field("FAILED", f"🔴 **{snap['failed']}**"),
                   field("SUCCESS RATE", f"**{rate}**"),
                   field(ui_word("mission_time").upper(), f"`{length}`"),
                   field("RUN TIME", f"`{run}`"),
                   *loot_fields],
        "footer": {"text": "ymir  ·  by soul.py"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def roblox_area():
    """Screen area inside the Roblox window (below its title bar) as an mss-style dict,
    or None if Roblox isn't open or is minimized."""
    u = ctypes.windll.user32
    hwnd = u.FindWindowW(None, "Roblox")
    rect = (ctypes.c_long * 4)()            # left, top, right, bottom
    if not (hwnd and not u.IsIconic(hwnd) and u.GetClientRect(hwnd, rect) and rect[2] and rect[3]):
        return None
    w, h = rect[2], rect[3]
    u.ClientToScreen(hwnd, rect)           # rect[0:2] becomes the client's top-left on screen
    return {"left": rect[0], "top": rect[1], "width": w, "height": h}


def grab_roblox():
    """BGR screenshot of the inside of the Roblox window (no title bar, no taskbar); the whole
    monitor if Roblox isn't open or is minimized. At most 1080 tall (Discord upload limit).
    Uses its own mss instance, so it works from any thread and when the macro isn't running."""
    with mss.mss() as sct:
        area = roblox_area() or pick_monitor(sct.monitors)
        shot = cv2.cvtColor(np.array(sct.grab(area)), cv2.COLOR_BGRA2BGR)
    if shot.shape[0] > 1080:
        f = 1080 / shot.shape[0]
        shot = cv2.resize(shot, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    return shot


def notify_mission_async(result, found, mission_seconds=None):
    if not webhook_url() or not settings["webhook"]["notify_mission"]:
        return
    embed = mission_embed(result, found, mission_seconds)
    try:
        shot = grab_roblox()   # grab now (before the retry click); encode + send in background
    except Exception as e:  # noqa: BLE001
        log(f"Discord: no screenshot ({type(e).__name__}: {e})")
        shot = None

    def work():
        png = None if shot is None else cv2.imencode(".png", shot)[1].tobytes()
        ok, msg = post_webhook("", png=png, ping=False, embed=embed)
        if not ok:
            log(f"Discord: {msg}")
    threading.Thread(target=work, daemon=True).start()


def rescue_embed(eliminated, mission_no, mission_seconds=None):
    """Discord embed for the stuck-mission rescue: it started / it worked."""
    timeout = mission_timeout()
    limit = f"{int(timeout // 60)}m {int(timeout % 60):02d}s"
    word = "Raid" if raid_counting() else "Mission"
    low = word.lower()
    if eliminated:
        embed = {
            "title": f"{word} #{mission_no} - Player eliminated",
            "color": 0xA855F7,
            "description": f"The retry button showed up, so the stuck {low} is over: "
                           "the player was **successfully eliminated**.",
        }
        if mission_seconds is not None:
            embed["footer"] = {"text": f"{word} length {fmt_time(mission_seconds)}"}
    else:
        embed = {
            "title": f"{word} #{mission_no} - Stuck {low}",
            "color": 0xFBBF24,
            "description": f"This {low} ran longer than {limit}, so the macro is now "
                           "**trying to eliminate the player** (looking at the ground, then "
                           "clicking until retry shows up).",
        }
    embed["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return embed


def notify_rescue_async(eliminated, mission_no, mission_seconds=None):
    """Send the rescue embed in the background (only if a webhook is set). Mentions you when
    mentions are on and a user ID is set."""
    if not webhook_url():
        return
    embed = rescue_embed(eliminated, mission_no, mission_seconds)

    def work():
        ok, msg = post_webhook("", ping=True, embed=embed)
        if not ok:
            log(f"Discord: {msg}")
    threading.Thread(target=work, daemon=True).start()


# ------------------------- hourly stats graph ---------------------------
report = {"run": 0, "base": None, "hourly": [], "t0": 0.0}


def snapshot():
    _st, results, loot = counters()
    return {"completed": results["completed"], "failed": results["failed"],
            "scrolls": loot.get("vessel" if raid_counting() else "scroll", 0),
            "fragments": sum(loot.get(k, 0) for k in FRAGMENT_KINDS),
            "serums": sum(loot.get(k, 0) for k in SERUM_KINDS)}


def report_rebase():
    """Call after the stats were reset, so the next hour doesn't count negative."""
    report["base"] = snapshot()


def render_graph(hourly, totals, run_seconds, hours=1):
    """Draw the report picture (bars per hour + totals) and return it as PNG bytes."""
    W, H = 960, 830
    BG, CARD, GRID = (23, 16, 13), (34, 23, 19), (56, 40, 34)       # BGR colours
    TEXT, SUB = (242, 233, 230), (152, 131, 122)
    GREEN, RED = (153, 211, 52), (113, 113, 248)
    GOLD, PINK, CYAN = (36, 191, 251), (182, 114, 244), (238, 211, 34)      # scrolls/vessels, fragments, serums
    font = cv2.FONT_HERSHEY_SIMPLEX
    img = np.full((H, W, 3), BG, np.uint8)

    def text(s, x, y, scale=0.55, color=TEXT, thick=1, anchor="l"):
        (tw, _), _ = cv2.getTextSize(s, font, scale, thick)
        if anchor == "c":
            x -= tw // 2
        elif anchor == "r":
            x -= tw
        cv2.putText(img, s, (int(x), int(y)), font, scale, color, thick, cv2.LINE_AA)

    text("ymir  |  stats report", 30, 48, 0.9, TEXT, 2)
    text(time.strftime("%Y-%m-%d %H:%M"), W - 30, 48, 0.55, SUB, 1, "r")
    cv2.rectangle(img, (660, 70), (940, 810), CARD, -1)

    def bar_chart(y1, y2, title, series, empty_text):
        """One panel of bars per interval. series = [(label, key, colour)], one bar each per slot."""
        cv2.rectangle(img, (20, y1), (640, y2), CARD, -1)
        text(title, 40, y1 + 32, 0.6, SUB)
        x = 620                                     # legend, right aligned
        for label, _key, color in reversed(series):
            (tw, _), _ = cv2.getTextSize(label, font, 0.45, 1)
            x -= tw
            text(label, x, y1 + 32, 0.45)
            x -= 18
            cv2.rectangle(img, (x, y1 + 20), (x + 12, y1 + 32), color, -1)
            x -= 16
        bars = hourly[-12:]
        left, right, top_y, bottom_y = 70, 620, y1 + 65, y2 - 50
        peak = max([1] + [h.get(key, 0) for h in bars for _l, key, _c in series])
        step = max(1, -(-peak // 5))
        top = step * -(-peak // step)
        for v in range(0, top + 1, step):
            y = bottom_y - (bottom_y - top_y) * v // top
            cv2.line(img, (left, y), (right, y), GRID, 1)
            text(str(v), left - 10, y + 5, 0.45, SUB, 1, "r")
        if not bars:
            text(empty_text, (left + right) // 2, (top_y + bottom_y) // 2, 0.6, SUB, 1, "c")
            return
        n = len(series)
        slot = (right - left) / len(bars)
        bar_w = int(min(36, slot * 0.8 / n))
        total_w = n * bar_w + (n - 1) * 2
        for i, h in enumerate(bars):
            cx = int(left + slot * (i + 0.5))
            for j, (_label, key, color) in enumerate(series):
                v = h.get(key, 0)
                if v:
                    x1 = cx - total_w // 2 + j * (bar_w + 2)
                    y1b = bottom_y - (bottom_y - top_y) * v // top
                    cv2.rectangle(img, (x1, y1b), (x1 + bar_w, bottom_y), color, -1)
                    text(str(v), x1 + bar_w // 2, y1b - 6, 0.45, TEXT, 1, "c")
            text(time.strftime("%H:%M", time.localtime(h["t"])), cx, bottom_y + 24, 0.45, SUB, 1, "c")

    per = "per hour" if hours == 1 else f"per {hours} hours"
    bar_chart(70, 435, f"{ui_word('missions')} {per}",
              [("Completed", "completed", GREEN), ("Failed", "failed", RED)],
              "No full hour recorded yet")
    bar_chart(445, 810, f"Loot {per}",
              [(ui_word("scrolls"), "scrolls", GOLD), ("Fragments", "fragments", PINK),
               ("Serums", "serums", CYAN)],
              "No full hour recorded yet")

    # --- totals ---
    text("Totals (since reset)", 680, 102, 0.6, SUB)
    done = totals["completed"] + totals["failed"]
    rate = f"{100 * totals['completed'] // done}%" if done else "-"
    rows = ((ui_word("missions"), totals["missions"], TEXT), ("Completed", totals["completed"], GREEN),
            ("Failed", totals["failed"], RED), ("Success rate", rate, TEXT),
            (ui_word("scrolls"), totals["scrolls"], TEXT), ("Fragments", totals["fragments"], TEXT),
            ("Serums", totals["serums"], TEXT),
            ("Run time", fmt_time(run_seconds), TEXT))
    y = 144
    for label, value, color in rows:
        text(label, 680, y, 0.55, SUB)
        text(str(value), 920, y, 0.7, color, 2, "r")
        y += 40
    ok, buf = cv2.imencode(".png", img)
    return buf.tobytes()


def current_entry():
    """What happened since the last report (or all-time numbers if nothing is running yet)."""
    now, base = snapshot(), report["base"]
    entry = {"t": time.time()}
    entry.update(now if base is None else {k: max(0, now[k] - base[k]) for k in now})
    return entry


def send_stats_graph(label, partial=False):
    """Draw the graph and post it. partial=True adds the interval that is still running
    (used by the test button). Blocks; returns (ok, message)."""
    hours = settings["webhook"]["graph_hours"]
    entries = list(report["hourly"])
    if partial:
        entries.append(current_entry())
    totals = dict(snapshot(), missions=counters()[0]["missions"])   # all-time numbers (since reset)
    run = time.time() - report["t0"] if report["t0"] and running() else 0
    png = render_graph(entries, totals, run, hours)
    return post_webhook(label, png=png, ping=False, title="Stats report")


def reporter_main(run_id):
    """Background timer: every N hours (Settings) while the macro runs, record that interval
    and (if turned on) send the graph to Discord."""
    last = time.time()
    while report["run"] == run_id and running():
        time.sleep(1)
        if time.time() - last < 3600 * settings["webhook"]["graph_hours"]:
            continue
        last = time.time()
        entry = current_entry()
        report["base"] = snapshot()
        report["hourly"].append(entry)
        del report["hourly"][:-24]
        if settings["webhook"]["hourly_graph"] and webhook_url():
            try:
                ok, msg = send_stats_graph("Stats report")
                log("Discord: stats graph sent" if ok else f"Discord: {msg}")
            except Exception as e:  # noqa: BLE001 - never kill the timer
                log(f"Discord: could not build the graph: {type(e).__name__}: {e}")


def start_reporter():
    report["run"] += 1
    report.update(base=snapshot(), hourly=[], t0=time.time())
    threading.Thread(target=reporter_main, args=(report["run"],), daemon=True).start()


# ------------------- shared state + events to the window ----------------
ev = queue.Queue()  # macro thread -> window
state = {"paused": False, "quit": False, "manual_start": False,
         "phase_status": "waiting",
         "mission_t0": None,        # when the current mission began (None = no mission running)
         "rescuing": False,         # True while the stuck-mission rescue is clicking
         "rescued": False,          # True once the rescue started in the current mission
         "debug_rescue": False}     # DEBUG: set by F3+K, makes tick() start the rescue right now
stats = {"missions": 0, "retries": 0}
loot = {kind: 0 for kind in LOOT_FILES}
results = {"completed": 0, "failed": 0}
# Female Raid keeps its own mission / retry / result counters ("missions" here = raids), saved
# separately in the json. The LOOT counter is shared: raids and missions add to the same one.
raid_stats = {"missions": 0, "retries": 0}
raid_loot = loot
raid_results = {"completed": 0, "failed": 0}
worker = None


def counters():
    """(stats, results, loot) of the config that is picked now: the raid's own counts while Female
    Raid is selected, the mission ones for every other config. The loot is the same for both."""
    if raid_counting():
        return raid_stats, raid_results, raid_loot
    return stats, results, loot

_sct = None
_sct_lock = threading.Lock()   # guards _sct re-creation when the monitor turns off/on
_monitor = None
_scale = 1.0     # real screen height / BASE_HEIGHT (set when the macro starts)
_retry_template = None
_alt_retry_templates = []        # extra looks of the retry button: [(templates, dx)] - see load_alt_retry()
_start_template = None
_shiki_ability4_icon = None      # Shiki: ability 4 icon, seen = ability 4 is ready
_raid_templates = {}             # Female Raid: {"skip": {...}, "finish": {...}} (see load_quality_templates)
_loot_templates = {}
_result_templates = {}
_grab_template = None
_disconnect_template = None       # Reconnect: the Disconnect Image (None = reconnect off)
_reconnect_images = {}            # Reconnect: {"select": templates, "play": templates}
_last_scan = 0.0
_last_grab = 0.0


class RetryFound(Exception):
    def __init__(self, match, quality="normal", screen=None):
        super().__init__("retry button found")
        self.match = match
        self.quality = quality    # which look matched: "normal" or "low" (low graphics)
        self.screen = screen      # fast mode only: the screenshot the button was found on


class Disconnected(Exception):
    """Rejoin the game (see reconnect_rejoin). `reason`: the Disconnect Image was seen, the failsafe
    rescue ran longer than RECONNECT_RESCUE_LIMIT, or the bot command !reconnect was used."""

    def __init__(self, reason="the Disconnect Image was seen"):
        super().__init__(reason)
        self.reason = reason


def log(msg):
    ev.put(("log", msg))


def show(title, detail_text=""):
    ev.put(("now", title, detail_text))


def detail(text):
    ev.put(("detail", text))


def set_step(i):
    ev.put(("step", i))


def stats_path():
    return os.path.join(data_dir(), STATS_FILE)


def migrate_old_stats():
    """One time: if the json used to live next to the script, copy it into the Documents/Ymir Macro folder."""
    old = os.path.join(app_dir(), STATS_FILE)
    new = stats_path()
    if os.path.exists(old) and not os.path.exists(new) and os.path.abspath(old) != os.path.abspath(new):
        try:
            shutil.copy2(old, new)
        except OSError:
            pass


_save_lock = threading.Lock()


def save_stats():
    """Writes stats AND settings (same json). Resetting stats never touches settings."""
    data = {"missions": stats["missions"], "retries": stats["retries"],
            "completed": results["completed"], "failed": results["failed"],
            "loot": dict(loot),
            "raid": {"raids": raid_stats["missions"], "retries": raid_stats["retries"],
                     "completed": raid_results["completed"], "failed": raid_results["failed"]},
            "settings": {k: v for k, v in settings.items() if k != "timings"}}
    try:
        with _save_lock:   # the macro thread and the Settings window both save
            tmp = stats_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, stats_path())  # never leaves a half-written file behind
    except OSError as e:
        log(f"Could not save stats: {e}")


def config_path():
    return os.path.join(data_dir(), CONFIG_FILE)


def save_timings():
    """config.json (Documents/Ymir Macro): ONLY the timings changed in Edit configs."""
    try:
        with _save_lock:
            tmp = config_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(settings["timings"], f, indent=2)
            os.replace(tmp, config_path())   # never leaves a half-written file behind
    except OSError as e:
        log(f"Could not save {CONFIG_FILE}: {e}")


def load_timings():
    """Read config.json into settings["timings"]. First run after the update: timings that an
    older version kept in aotr_stats.json are moved over into config.json."""
    try:
        with open(config_path(), encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        if settings["timings"]:
            save_timings()
            save_stats()        # the stats file does not keep them any more
        return
    except (OSError, ValueError):
        return                  # unreadable: keep what there is, the next change rewrites it
    settings["timings"] = {}
    apply_timings(data)


def load_stats():
    migrate_old_stats()
    try:
        with open(stats_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return  # no file yet (or unreadable): start from zero
    load_settings(data.get("settings"))
    try:
        stats["missions"] = int(data.get("missions", 0))
        stats["retries"] = int(data.get("retries", 0))
        results["completed"] = int(data.get("completed", 0))
        results["failed"] = int(data.get("failed", 0))
        saved_loot = data.get("loot", {})
        for kind in loot:
            loot[kind] = int(saved_loot.get(kind, 0))
    except (TypeError, ValueError):
        pass
    raid = data.get("raid")
    if isinstance(raid, dict):
        try:
            raid_stats["missions"] = int(raid.get("raids", 0))
            raid_stats["retries"] = int(raid.get("retries", 0))
            raid_results["completed"] = int(raid.get("completed", 0))
            raid_results["failed"] = int(raid.get("failed", 0))
            saved_raid_loot = raid.get("loot", {})    # older json: the raid had its own loot - merge it in
            if isinstance(saved_raid_loot, dict):
                for kind in loot:
                    loot[kind] += int(saved_raid_loot.get(kind, 0))
        except (TypeError, ValueError):
            pass


def push_stats(save=True):
    st, res, lt = counters()       # the window shows the counters of the picked config
    ev.put(("stats", st["missions"], st["retries"], dict(lt), dict(res)))
    if save:
        save_stats()


def reset_stats():
    """Erase the counters of the picked config (missions, or raids for Female Raid) back to 0.
    The other one's counts (and every setting) stay; the loot is shared, so it is cleared for both."""
    st, res, lt = counters()
    st.update(missions=0, retries=0)
    res.update(completed=0, failed=0)
    for kind in lt:
        lt[kind] = 0
    report_rebase()
    push_stats()
    log("Raid stats reset to 0" if raid_counting() else "Mission stats reset to 0")


def set_status(key):
    if key in ("waiting", "running", "retrying"):
        state["phase_status"] = key
    if not state["paused"] or key in ("idle", "paused"):
        ev.put(("status", key))


# ----------------------------- controls ---------------------------------
def running():
    return worker is not None and worker.is_alive()


def toggle_pause():
    if not running():
        return
    state["paused"] = not state["paused"]
    if state["paused"]:
        ev.put(("status", "paused"))
        log("Paused")
    else:
        ev.put(("status", state["phase_status"]))
        log("Resumed")


def request_stop():
    if running():
        state["quit"] = True
        state["paused"] = False
        ev.put(("stopping",))   # hide the mini bar now, not when the worker has finished


def debug_trigger_rescue():
    """DEBUG (F3+K): start the stuck-mission rescue right now, as if 2m had passed.
    Remove this function, DEBUG_RESCUE_KEY and the lines marked DEBUG when it is not needed."""
    if not running():
        log("DEBUG F3+K: start the macro first")
        return
    if state["rescuing"]:
        return
    state["debug_rescue"] = True
    log("DEBUG F3+K: rescue requested")


def start_macro(manual=False):
    global worker
    if running():
        return
    state.update(paused=False, quit=False, manual_start=manual,
                 phase_status="waiting", mission_t0=None, rescuing=False,
                 debug_rescue=False)
    worker = threading.Thread(target=macro_main, daemon=True)
    worker.start()
    start_reporter()
    ev.put(("started",))


def request_start():
    if running():
        if state["quit"]:
            # still shutting down after a stop: start again as soon as it has finished
            old = worker

            def restart():
                old.join(3.0)
                if not running():
                    start_macro()
            threading.Thread(target=restart, daemon=True).start()
        elif state["phase_status"] == "waiting" and not state["paused"]:
            state["manual_start"] = True      # F8 while waiting = start the mission now
            log("Manual start requested")
        # during a mission F8 does nothing (it must not leave a stale manual start behind)
    else:
        start_macro()    # like the Start button: waits for the ability 2 icon


# ------------------------- screen detection -----------------------------
def _read_image(path):
    """cv2.imread that also works with non-ASCII folder names (Documents of a user called "José")."""
    try:
        data = np.fromfile(path, dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        return None


def load_template(filename):
    """Load one picture. The user's own copy in asset_override wins over the built-in one.
    Their picture is taken on THEIR screen, so it is scaled to the 1080p size the screen is
    matched at (see grab_screen); the built-in pictures already are 1080p."""
    mine = override_path(filename)
    if mine:
        img = _read_image(mine)
        if img is not None:
            if abs(_scale - 1.0) > 0.01:
                interp = cv2.INTER_AREA if _scale > 1 else cv2.INTER_CUBIC
                img = cv2.resize(img, None, fx=1 / _scale, fy=1 / _scale, interpolation=interp)
            return img
        log(f"asset_override: could not read {filename} - using the built-in picture")
    path = asset_path(filename)
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(f"Could not load template image: {path}")
    return img


def load_quality_templates(filename):
    """{"normal": img, "low": img} for one template. The normal one is image/<filename>, the low
    graphics one is image/low quality fix/<filename>. At least one of them has to exist.
    If the user put their own copy of it in asset_override, ONLY their copy is used (the built-in
    looks of that picture are skipped, they are what didn't match on their setup)."""
    names = (("normal", filename), ("low", os.path.join(LOW_FOLDER, filename)))
    mine = {q for q, name in names
            if override_path(name)}
    found = {}
    for quality, name in names:
        if mine and quality not in mine:
            continue
        try:
            found[quality] = load_template(name)
        except FileNotFoundError:
            pass
    if not found:
        raise FileNotFoundError(f"Could not load template image: {asset_path(filename)}")
    return found


def find_quality_template(screen_bgr, templates, threshold, scales=None):
    """Look for every look of a template. Returns (match, quality) of the best one that
    passes the threshold, or None. The quality says which look it was ("normal" / "low")."""
    best = None
    for quality, tpl in templates.items():
        m = find_template(screen_bgr, tpl, threshold, scales)
        if m and (best is None or m[2] > best[0][2]):
            best = (m, quality)
    return best


_small_cache = (None, None)
_scale_hint = {}      # id(template) -> (best size, scans left before sweeping again)


def _coarse_best_scale(screen_bgr, template, scales):
    """Pick the template size that fits best using a half-resolution picture (4x cheaper than
    matching every size at full resolution)."""
    global _small_cache
    hint = _scale_hint.get(id(template))
    if hint and hint[1] > 0:                  # the best size is the same every scan: reuse it
        _scale_hint[id(template)] = (hint[0], hint[1] - 1)
        return hint[0]
    if _small_cache[0] is not screen_bgr:
        _small_cache = (screen_bgr, cv2.resize(screen_bgr, None, fx=0.5, fy=0.5,
                                               interpolation=cv2.INTER_AREA))
    small = _small_cache[1]
    best_scale, best_score = 1.0, -1.0
    for scale in scales:
        t = cv2.resize(template, None, fx=scale * 0.5, fy=scale * 0.5,
                       interpolation=cv2.INTER_AREA)
        th, tw = t.shape[:2]
        if th < 4 or tw < 4 or th > small.shape[0] or tw > small.shape[1]:
            continue
        score = cv2.minMaxLoc(cv2.matchTemplate(small, t, cv2.TM_CCOEFF_NORMED))[1]
        if score > best_score:
            best_scale, best_score = scale, score
    if best_score >= 0.6:       # only trust it when the button really is on screen
        _scale_hint[id(template)] = (best_scale, 20)     # sweep again after 20 scans
    return best_scale


def best_match(screen_bgr, template, scales=None):
    """Return (center_x, center_y, score, w, h) of the best match, or None."""
    best = None
    scales = scales or SCALES
    if len(scales) > 1:
        scales = [_coarse_best_scale(screen_bgr, template, scales)]   # one full-size match only
    for scale in scales:
        t = template if scale == 1.0 else cv2.resize(
            template, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        th, tw = t.shape[:2]
        if th > screen_bgr.shape[0] or tw > screen_bgr.shape[1]:
            continue
        result = cv2.matchTemplate(screen_bgr, t, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(result)
        if best is None or score > best[2]:
            best = (loc[0] + tw // 2, loc[1] + th // 2, score, tw, th)
    return best


def _any_size_match(screen_bgr, small, template, scales, top=3):
    """The work of best_match_any_size. `small` = the screen at half resolution. Touches no shared
    state, so the asset test window can use it while the macro runs.
    Returns ((center_x, center_y, score, w, h), size) or (None, 1.0)."""
    coarse = []
    for scale in scales:
        t = cv2.resize(template, None, fx=scale * 0.5, fy=scale * 0.5, interpolation=cv2.INTER_AREA)
        th, tw = t.shape[:2]
        if th < 4 or tw < 4 or th > small.shape[0] or tw > small.shape[1]:
            continue
        coarse.append((cv2.minMaxLoc(cv2.matchTemplate(small, t, cv2.TM_CCOEFF_NORMED))[1], scale))
    coarse.sort(reverse=True)
    best, best_scale = None, 1.0
    for _score, scale in coarse[:top]:
        t = template if scale == 1.0 else cv2.resize(
            template, None, fx=scale, fy=scale,
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
        th, tw = t.shape[:2]
        if th > screen_bgr.shape[0] or tw > screen_bgr.shape[1]:
            continue
        _, score, _, loc = cv2.minMaxLoc(cv2.matchTemplate(screen_bgr, t, cv2.TM_CCOEFF_NORMED))
        if best is None or score > best[2]:
            best = (loc[0] + tw // 2, loc[1] + th // 2, score, tw, th)
            best_scale = scale
    return best, best_scale


def best_match_any_size(screen_bgr, template, scales, top=3):
    """Like best_match, but for a picture that can be shown at ANY size (resized loot drops).
    Every size in `scales` is tried on a half-resolution copy of the screen (cheap), then the
    `top` best sizes are checked again at full resolution.
    Returns ((center_x, center_y, score, w, h), size) or (None, 1.0)."""
    global _small_cache
    if _small_cache[0] is not screen_bgr:
        _small_cache = (screen_bgr, cv2.resize(screen_bgr, None, fx=0.5, fy=0.5,
                                               interpolation=cv2.INTER_AREA))
    return _any_size_match(screen_bgr, _small_cache[1], template, scales, top)


def relax(threshold):
    """Match thresholds are a bit lower on screens that are not 1080p (the scaled picture is blurrier)."""
    if abs(_scale - 1.0) > 0.01:
        return threshold - SCALE_THRESHOLD_RELAX
    return threshold


def find_template(screen_bgr, template, threshold, scales=None):
    best = best_match(screen_bgr, template, scales)
    if best and best[2] >= relax(threshold):
        return best
    return None


# --- Monitor turned off (DisplayPort drops the display when the monitor is switched off) ---
_MONITOR_OFF_RETRY_INTERVAL = 2.0   # seconds between reinit attempts while the monitor is off
_monitor_was_off = False            # used to log "monitor back" once


def _reinit_mss():
    """Create a fresh mss instance (called when a grab fails or returns a black frame).
    Returns True when the new instance works, False when the monitor is still unreachable."""
    global _sct, _monitor, _scale
    with _sct_lock:
        # Close the stale instance cleanly (best-effort).
        try:
            if _sct is not None:
                _sct.close()
        except Exception:  # noqa: BLE001
            pass
        new_sct = None
        try:
            new_sct = mss.mss()
            mon = pick_monitor(new_sct.monitors)
            # Quick sanity-grab to confirm the monitor is really back.
            test = np.array(new_sct.grab(mon))
            if test.max() == 0:
                new_sct.close()
                return False
            _sct = new_sct
            _monitor = mon
            _scale = FORCE_SCALE if FORCE_SCALE else _monitor["height"] / BASE_HEIGHT
            return True
        except Exception:  # noqa: BLE001
            try:
                if new_sct is not None:
                    new_sct.close()
            except Exception:  # noqa: BLE001
                pass
            return False


def _to_points(img):
    """macOS Retina: mss returns the screenshot in real pixels (2x) while the monitor size and the
    mouse use points. Bring it to the monitor size so every position / scale below stays right."""
    if IS_MAC and _monitor and img.shape[1] != _monitor["width"]:
        img = cv2.resize(img, (_monitor["width"], _monitor["height"]), interpolation=cv2.INTER_AREA)
    return img


def grab_screen():
    """The screen as a BGR image, always scaled to 1080p size so the template images (which are
    1080p) match on any resolution. Positions in this image are 1080p positions: multiply by
    _scale to get real screen pixels.

    If the monitor is turned off (physical button), mss raises or returns a black frame.
    The function then waits in a loop, logging once, until the monitor comes back, and
    continues transparently so the macro never crashes or stops. Stop (F7) still works
    while it waits."""
    global _monitor_was_off
    while True:
        try:
            shot = np.array(_sct.grab(_monitor))
            img = _to_points(cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR))
            # An all-black frame means the monitor is off (or going to sleep).
            if img.max() == 0:
                raise OSError("All-black frame - monitor appears to be off")
            if _monitor_was_off:
                _monitor_was_off = False
                log("Monitor is back - resuming screen detection")
            if abs(_scale - 1.0) > 0.01:
                interp = cv2.INTER_AREA if _scale > 1 else cv2.INTER_CUBIC
                img = cv2.resize(img, None, fx=1 / _scale, fy=1 / _scale, interpolation=interp)
            return img
        except SystemExit:
            raise
        except Exception:  # noqa: BLE001
            if not _monitor_was_off:
                _monitor_was_off = True
                log("Monitor appears to be off - waiting for it to come back (screen detection paused)")
            if state["quit"]:
                raise SystemExit
            # Try to reconnect mss; if it fails, wait and try again (in short steps, so Stop works).
            if not _reinit_mss():
                end = time.time() + _MONITOR_OFF_RETRY_INTERVAL
                while time.time() < end:
                    if state["quit"]:
                        raise SystemExit
                    time.sleep(0.1)


def red_mask(bgr):
    """Black/white image of the red text pixels (so the changing background doesn't matter)."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return (cv2.inRange(hsv, (0, 120, 90), (10, 255, 255))
            | cv2.inRange(hsv, (170, 120, 90), (180, 255, 255)))


def check_grabbed(screen):
    """If the YOUR STREAK IS IN DANGER text is on screen, press the escape key."""
    global _last_grab
    if _grab_template is None or time.time() - _last_grab < GRAB_REPEAT:
        return
    if settings["family"] in ("Shiki", "Female Raid"):
        return   # Shiki: ability 1 is a perk, no need to press it; the raid has no grab detection
    if find_template(red_mask(screen), _grab_template, GRAB_THRESHOLD):
        _last_grab = time.time()
        log(f"Grabbed! Pressing {KEY_GRAB.upper()}")
        tap(KEY_GRAB)


def icon_visible():
    return find_quality_template(grab_screen(), _start_template, START_THRESHOLD) is not None


# --------------------- tick / wait (stay responsive) --------------------
def load_alt_retry():
    """alt_retry_button.png (and image/low quality fix/alt_retry_button.png): the whole button.
    Two looks are made of it: the whole picture and the RETRY word only (the "(0/1)" counter
    changes). dx = how far the picture's centre is from the whole button's centre, so a match
    can be turned back into the button centre for the click."""
    _alt_retry_templates.clear()
    try:
        whole = load_quality_templates(ALT_RETRY_FILE)
    except FileNotFoundError:
        return
    _alt_retry_templates.append((whole, 0))
    x0, x1 = ALT_RETRY_WORD_COLS
    word, dx = {}, 0
    for quality, img in whole.items():
        name = ALT_RETRY_FILE if quality == "normal" else os.path.join(LOW_FOLDER, ALT_RETRY_FILE)
        if override_path(name):
            continue     # the user's own picture: the word columns are the built-in one's, skip the crop
        w = img.shape[1]
        if 0 <= x0 < x1 <= w:
            word[quality] = img[:, x0:x1]
            dx = w / 2 - (x0 + x1) / 2
    if word:
        _alt_retry_templates.append((word, dx))
    log(f"{ALT_RETRY_FILE} loaded - used as an extra check for the retry button")


_last_debug_log = 0.0


def _log_retry_score(screen, scales):
    """RETRY_DEBUG: log the best retry score (even when it is below the threshold)."""
    global _last_debug_log
    if time.time() - _last_debug_log < 5:
        return
    _last_debug_log = time.time()
    for quality, tpl in _retry_template.items():
        best = best_match(screen, tpl, scales)
        if best:
            log(f"[debug] retry best score ({quality}): {best[2]:.3f} "
                f"(needs {relax(THRESHOLD):.2f}), template size {best[3]}x{best[4]}")


_retry_scans = 0


def _match_retry(screen, scales):
    """retry_text.png first, then alt_retry_button.png, at the given template sizes.
    Returns (match, quality, size) or None. size = how much bigger/smaller than the picture."""
    found = find_quality_template(screen, _retry_template, THRESHOLD, scales)
    if found:
        match, quality = found
        return match, quality, match[3] / _retry_template[quality].shape[1]
    for templates, dx in _alt_retry_templates:
        alt = find_quality_template(screen, templates, ALT_RETRY_THRESHOLD, scales)
        if alt:
            (cx, cy, score, w, h), quality = alt
            size = w / templates[quality].shape[1]
            # centre of the whole button (dx is in picture pixels, so it grows with the size)
            return (cx + dx * size, cy, score, w, h), quality, size
    return None


def find_retry(screen):
    """The retry button: retry_text.png first, then (extra check) alt_retry_button.png.
    Returns (match, quality) like find_quality_template, or None.

    Not 1080p: the size that matched last time (settings "retry_scale", saved in the json) is
    tried first. Only when it misses are the other sizes (RETRY_SCALES) tried - and if one of
    those matches, it becomes the saved size from then on."""
    global _retry_scans
    if abs(_scale - 1.0) <= 0.01:
        found = _match_retry(screen, None)
        return found[:2] if found else None
    if RETRY_DEBUG:
        _log_retry_score(screen, RETRY_SCALES)
    saved = settings.get("retry_scale")
    found = _match_retry(screen, [saved]) if saved else None
    if not found:
        # the other sizes cost more: with a saved size they are tried every 4th scan only
        _retry_scans += 1
        if not saved or _retry_scans % 4 == 0:
            found = _match_retry(screen, RETRY_SCALES)
            if found:
                size = min(RETRY_SCALES, key=lambda sc: abs(sc - found[2]))
                if saved is None or abs(size - saved) > 0.001:
                    settings["retry_scale"] = size
                    save_stats()
                    log(f"Retry button matched at size x{size:.2f} (score {found[0][2]:.2f}) "
                        "- saved, it is tried first from now on")
    return found[:2] if found else None


def scan_for_retry():
    """Look for the end of the mission once (and for a grab). Raises RetryFound if it is there.
    The end of the mission is the retry button."""
    global _last_scan
    _last_scan = time.time()
    screen = grab_screen()
    check_grabbed(screen)
    found = find_retry(screen)
    if found:
        match = found[0]
        if tv(settings["family"], "fast_mode"):
            raise RetryFound(match, found[1], screen)   # fast mode: click at once, no confirmation look
        # screens often fade/animate in: look again and click only if it's
        # still there, at the same spot
        time.sleep(RETRY_CONFIRM_DELAY)
        again = find_retry(grab_screen())
        if again and abs(again[0][0] - match[0]) <= 15 and abs(again[0][1] - match[1]) <= 15:
            raise RetryFound(again[0], again[1])


def _mouse_move_down(pixels):
    """Relative mouse movement (what a locked mouse in Roblox needs to turn the camera)."""
    if IS_WIN:
        ctypes.windll.user32.mouse_event(0x0001, 0, int(pixels), 0, 0)   # MOUSEEVENTF_MOVE
    else:
        pydirectinput.moveRel(0, int(pixels), _pause=False)


def look_down(check, seconds=None, pixels=None, settle=0.15, step=None):
    """Press ctrl, move the mouse down for `seconds` (default LOOK_DOWN_TIME), press ctrl again.
    `settle` = pause after the second ctrl (0 = return the moment the look down has ended).
    `check` is called all the time (stop / pause handling, may raise SystemExit)."""
    seconds = LOOK_DOWN_TIME if seconds is None else seconds
    pixels = LOOK_DOWN_PIXELS if pixels is None else pixels
    step = LOOK_DOWN_STEP if step is None else step
    def press_ctrl():
        pydirectinput.keyDown(LOOK_DOWN_KEY)
        try:
            end = time.time() + 0.06
            while time.time() < end:
                check()
                time.sleep(0.01)
        finally:
            pydirectinput.keyUp(LOOK_DOWN_KEY)

    press_ctrl()
    end = time.time() + 0.15                 # let the cursor lock before moving
    while time.time() < end:
        check()
        time.sleep(0.01)
    end = time.time() + seconds
    while time.time() < end:
        check()
        _mouse_move_down(pixels)
        time.sleep(step)
    press_ctrl()
    end = time.time() + settle               # let the cursor unlock before clicking
    while time.time() < end:
        check()
        time.sleep(0.01)


def fix_shift_lock(check):
    """Movement > Fix shift lock at start: press ctrl once (the very start of a mission)."""
    pydirectinput.keyDown(LOOK_DOWN_KEY)
    try:
        end = time.time() + SHIFT_LOCK_FIX_HOLD
        while time.time() < end:
            check()
            time.sleep(0.005)
    finally:
        pydirectinput.keyUp(LOOK_DOWN_KEY)
    end = time.time() + SHIFT_LOCK_FIX_SETTLE
    while time.time() < end:
        check()
        time.sleep(0.005)


def look_up(check, total=None, pixels=None):
    """The look up at the start of a mission: press ctrl, move the mouse up `pixels` in total, press
    ctrl again - the whole thing takes `total` seconds (default LOOK_UP_TIME).
    `check` is called all the time (stop / pause handling, may raise SystemExit)."""
    total = LOOK_UP_TIME if total is None else total
    pixels = LOOK_UP_PIXELS if pixels is None else pixels
    press, lock, unlock = 0.06, 0.08, 0.10         # ctrl held / cursor locks / cursor unlocks
    move = max(0.05, total - 2 * press - lock - unlock)

    def pause(seconds):
        end = time.time() + seconds
        while time.time() < end:
            check()
            time.sleep(0.005)

    def press_ctrl():
        pydirectinput.keyDown(LOOK_DOWN_KEY)
        try:
            pause(press)
        finally:
            pydirectinput.keyUp(LOOK_DOWN_KEY)

    press_ctrl()
    pause(lock)
    steps = max(1, int(move / LOOK_DOWN_STEP))
    done = 0
    for i in range(1, steps + 1):                  # spread the pixels evenly, no rounding loss
        check()
        target = round(pixels * i / steps)
        if target > done:
            _mouse_move_down(-(target - done))     # negative = up
            done = target
        time.sleep(move / steps)
    press_ctrl()
    pause(unlock)


def click_middle(cx, cy):
    pydirectinput.moveTo(cx, cy)
    time.sleep(0.05)
    pydirectinput.moveRel(1, 0)
    time.sleep(0.05)
    pydirectinput.click()


def rescue_clicks(debug=False, reason=None):
    """Stuck mission (longer than the failsafe time): spam ability 2, wait, look at the ground
    (ctrl, mouse down for 2s, ctrl), then click (X centre / 83% Y, or the coordinate picked in
    Settings) every RESCUE_CLICK_DELAY seconds until the retry button can be seen. The retry button being found raises RetryFound, so the normal
    end-of-mission handling takes over."""
    state["rescuing"] = True
    state["rescue_t0"] = None if debug else time.time()   # tick(): a rescue past RECONNECT_RESCUE_LIMIT rejoins
    if not debug:
        state["rescued"] = True
    for held in (KEY_STRAFE_D, KEY_STRAFE):
        try:
            pydirectinput.keyUp(held)          # step 9 may be holding D / A: let go before rescuing
        except Exception:  # noqa: BLE001
            pass
    if not debug:
        notify_rescue_async(False, counters()[0]["missions"])  # Discord: stuck, trying to eliminate
    cx, cy = failsafe_click_point(*failsafe_default_point())
    where = "the picked spot" if (cx, cy) != failsafe_default_point() else "bottom-centre (50% X, 83% Y)"
    look = bool(settings["failsafe"]["look_down"])      # Settings > Configs > Failsafe > Look down
    ground = "looking at the ground, " if look else ""
    if reason:      # raid failsafe
        log(f"Raid failsafe ({reason}) - ejecting, {ground}"
            f"then clicking {where} ({cx}, {cy}) until retry shows up")
        show("Stuck raid", f"Ejecting, {ground}then clicking {where}")
    else:
        log(f"Mission is taking longer than {mission_timeout():g}s - ejecting, {ground}"
            f"then clicking {where} ({cx}, {cy}) until retry shows up")
        show("Stuck mission", f"Ejecting, {ground}then clicking {where}")

    def click_phase():
        """Click every RESCUE_CLICK_DELAY seconds until retry shows up."""
        while True:
            tick(scan_retry=False)                      # stop / pause
            loop_start = time.time()
            scan_for_retry()                            # raises RetryFound when it is visible
            if reason and "finish" in _raid_templates:  # raid failsafe: stop at the finish button too
                fin = raid_find("finish", grab_screen())
                if fin:
                    log("Raid failsafe: finish button seen - failsafe stopped")
                    state["rescuing"] = False
                    raid_finish(fin)
                    raise RaidFinished
            click_middle(cx, cy)
            end = loop_start + RESCUE_CLICK_DELAY      # click-to-click gap = exactly the delay
            while time.time() < end:
                tick(scan_retry=False)
                time.sleep(0.05)

    try:
        if debug:
            log("DEBUG F3+K: running the rescue now")
        # always use ability 2 (ejects the player if shifted), then wait before looking down
        show("Stuck mission", f"Spamming ability 2 for {STILL_SHIFTED_SPAM_TIME:g}s")
        log(f"Rescue: spamming ability 2 for {STILL_SHIFTED_SPAM_TIME:g}s, the {STILL_SHIFTED_WAIT:g}s "
            f"wait starts {STILL_SHIFTED_WAIT_START:g}s after the spam started")
        spam_start = time.time()
        wait_end = spam_start + STILL_SHIFTED_WAIT_START + STILL_SHIFTED_WAIT
        spam_key_for(KEY_ABILITY_1ST, STILL_SHIFTED_SPAM_TIME, STILL_SHIFTED_SPAM_INTERVAL,
                     "ability 2")
        while True:     # the rest of the wait (it started during the spam)
            remaining = wait_end - time.time()
            if remaining <= 0:
                break
            show("Stuck mission", f"Waiting {remaining:.0f}s before "
                                  f"{'looking down' if look else 'clicking'}")
            wait(min(0.5, remaining))
        if look:        # ctrl, mouse down, ctrl - off by default (on some setups the camera goes UP)
            fsl = settings["failsafe"]      # Failsafe > Configure: time / pixels / step delay
            look_down(lambda: tick(scan_retry=False), seconds=fsl["look_time"],
                      pixels=fsl["look_pixels"], step=fsl["look_step"])
        click_phase()
    finally:
        state["rescuing"] = False
        state["rescue_t0"] = None


_fs_test = {"cancel": threading.Event(), "thread": None, "msgs": queue.Queue()}


def failsafe_test_running():
    th = _fs_test["thread"]
    return th is not None and th.is_alive()


def failsafe_test_stop():
    _fs_test["cancel"].set()


def failsafe_test_start(countdown=3.0, clicks=5):
    """Failsafe > Test: only the look down (if it is on) and the clicking part of the rescue, once
    - no ability 2, no waiting, no retry detection. Messages go to _fs_test["msgs"] as
    (text, kind) with kind None / "good" / "error" / "done"."""
    cancel = _fs_test["cancel"]
    cancel.clear()
    msgs = _fs_test["msgs"]

    def check():
        if cancel.is_set() or state["quit"]:
            raise SystemExit

    def nap(seconds):
        end = time.time() + seconds
        while time.time() < end:
            check()
            time.sleep(0.02)

    def run():
        try:
            fs = dict(settings["failsafe"])
            left = countdown
            while left > 0:
                msgs.put((f"Test: click into Roblox - starting in {left:.0f}s", None))
                nap(min(1.0, left))
                left -= 1.0
            with mss.mss() as sct:
                m = pick_monitor(sct.monitors)
            dx, dy = m["left"] + m["width"] // 2, m["top"] + int(m["height"] * FAILSAFE_DEFAULT_Y)
            cx, cy = (fs["click_x"], fs["click_y"]) if (
                fs["click_x"] is not None and fs["click_y"] is not None) else (dx, dy)
            if fs["look_down"]:
                msgs.put((f"Test: looking down ({fs['look_time']:g}s, {fs['look_pixels']}px "
                          f"every {fs['look_step']:g}s)", None))
                look_down(check, seconds=fs["look_time"], pixels=fs["look_pixels"],
                          step=fs["look_step"])
            else:
                msgs.put(("Test: look down is off - clicking only", None))
            for i in range(1, clicks + 1):
                check()
                msgs.put((f"Test: click {i}/{clicks} at {cx}, {cy}", None))
                click_middle(cx, cy)
                nap(RESCUE_CLICK_DELAY)
            msgs.put(("Test finished", "good"))
        except SystemExit:
            msgs.put(("Test stopped", None))
        except Exception as exc:  # noqa: BLE001
            msgs.put((f"Test failed: {exc}", "error"))
        finally:
            try:
                pydirectinput.keyUp(LOOK_DOWN_KEY)      # never leave ctrl held
            except Exception:  # noqa: BLE001
                pass
            msgs.put(("", "done"))

    th = threading.Thread(target=run, daemon=True)
    _fs_test["thread"] = th
    th.start()


def modifiers_settings():
    """The Add modifiers settings (always a full dict, even for an old settings file)."""
    mods = dict(DEFAULT_MODIFIERS)
    try:
        mods.update({k: bool(v) for k, v in settings["reconnect"].get("modifiers", {}).items()
                     if k in DEFAULT_MODIFIERS})
    except Exception:  # noqa: BLE001
        pass
    return mods


def modifiers_enabled():
    return bool(modifiers_settings()["enabled"])


def run_modifier_sequence(mods=None, check=None, say=None, raid=None):
    """The modifier menu: \\ (UI navigation on), right x4, wait 0.4s, left once, Enter (opens the
    Modifiers button), down once, up x7 in 1s, down x4 (x3 for a raid, 0.5s apart), then one down per
    row - Oddball, Injury prone, Chronic injuries, Fog, a skipped option, Time trial - with Enter on
    the rows that are switched on (0.5s between all down presses), then down x4 and Enter once, and
    \\ turns the UI navigation off again. For a raid Oddball is never switched on (it is greyed out
    in the Reconnect window). `raid` None = the macro that is picked now. `check` (Test button) is
    called often and may raise SystemExit; without it the macro's own wait() is used so stop / pause
    keep working."""
    mods = dict(mods) if mods is not None else modifiers_settings()
    if raid is None:
        raid = raid_counting()
    if raid:
        mods["oddball"] = False                         # no Oddball in raids, whatever was saved
    pre_downs = MODIFIER_RAID_PRE_DOWNS if raid else MODIFIER_PRE_DOWNS

    def nap(seconds):
        if seconds <= 0:
            return
        if check is None:
            wait(seconds, scan=False)
            return
        end = time.time() + seconds
        while True:
            check()
            left = end - time.time()
            if left <= 0:
                break
            time.sleep(min(0.02, left))

    def press(key):
        pydirectinput.keyDown(key)
        try:
            nap(MODIFIER_KEY_HOLD)
        finally:
            pydirectinput.keyUp(key)

    def down(enter=False):
        """One down press; Enter after a short pause when asked; the whole step lasts DOWN_GAP."""
        t0 = time.time()
        press("down")
        if enter:
            nap(MODIFIER_KEY_GAP)
            press("enter")
        nap(MODIFIER_DOWN_GAP - (time.time() - t0))

    def note(text):
        if say:
            say(text)

    try:
        note("Modifiers: UI navigation on")
        press(MODIFIER_OPEN_KEY)
        nap(MODIFIER_KEY_GAP)
        for _ in range(MODIFIER_RIGHT_PRESSES):
            press("right")
            nap(MODIFIER_KEY_GAP)
        nap(MODIFIER_MENU_WAIT)
        press("left")
        nap(MODIFIER_KEY_GAP)
        press("enter")                                  # the Modifiers button is open
        nap(MODIFIER_MENU_WAIT)
        press("down")
        nap(MODIFIER_KEY_GAP)
        gap = max(0.0, MODIFIER_UP_TIME / MODIFIER_UP_PRESSES - MODIFIER_KEY_HOLD)
        for _ in range(MODIFIER_UP_PRESSES):
            press("up")
            nap(gap)
        nap(MODIFIER_KEY_GAP)
        for _ in range(pre_downs):
            down()
        note("Modifiers: " + (", ".join(name for key, name in MODIFIER_LIST if mods.get(key))
                              or "none") + " will be set")
        for key, _name in MODIFIER_ROWS:
            down(enter=bool(key and mods.get(key)))
        for _ in range(MODIFIER_END_DOWNS):
            down()
        press("enter")
        nap(MODIFIER_KEY_GAP)
        press(MODIFIER_CLOSE_KEY)                       # UI navigation off again
        nap(MODIFIER_KEY_GAP)
    finally:
        for key in ("left", "right", "up", "down", "enter", MODIFIER_OPEN_KEY, MODIFIER_CLOSE_KEY):
            try:
                pydirectinput.keyUp(key)
            except Exception:  # noqa: BLE001
                pass


_mod_test = {"cancel": threading.Event(), "thread": None, "msgs": queue.Queue()}


def modifier_test_running():
    th = _mod_test["thread"]
    return th is not None and th.is_alive()


def modifier_test_stop():
    _mod_test["cancel"].set()


def modifier_test_start(countdown=MODIFIER_TEST_COUNTDOWN):
    """Reconnect > Add modifiers > Test: runs the modifier menu once with the current toggles (even
    when Add modifiers is off). Messages go to _mod_test["msgs"] as (text, kind) with kind None /
    "good" / "error" / "done"."""
    cancel = _mod_test["cancel"]
    cancel.clear()
    msgs = _mod_test["msgs"]
    mods = modifiers_settings()
    raid = raid_counting()          # read here, not in the thread

    def check():
        if cancel.is_set() or state["quit"]:
            raise SystemExit

    def run():
        try:
            left = float(countdown)
            while left > 0:
                msgs.put((f"Test: click into Roblox - starting in {left:.0f}s", None))
                end = time.time() + min(1.0, left)
                while time.time() < end:
                    check()
                    time.sleep(0.02)
                left -= 1.0
            run_modifier_sequence(mods, check=check, say=lambda t: msgs.put((t, None)), raid=raid)
            msgs.put(("Test finished", "good"))
        except SystemExit:
            msgs.put(("Test stopped", None))
        except Exception as exc:  # noqa: BLE001
            msgs.put((f"Test failed: {exc}", "error"))
        finally:
            msgs.put(("", "done"))

    th = threading.Thread(target=run, daemon=True)
    _mod_test["thread"] = th
    th.start()


def set_clipboard_text(text):
    """Put text on the clipboard (Windows: ctypes, macOS: pbcopy). True when it worked."""
    try:
        if IS_WIN:
            import ctypes.wintypes as wt
            u32 = ctypes.WinDLL("user32")        # own copies, so the argtypes below affect nothing else
            k32 = ctypes.WinDLL("kernel32")
            u32.OpenClipboard.argtypes = [wt.HWND]
            u32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
            k32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
            k32.GlobalAlloc.restype = ctypes.c_void_p
            k32.GlobalLock.argtypes = [ctypes.c_void_p]
            k32.GlobalLock.restype = ctypes.c_void_p
            k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            data = (text + "\0").encode("utf-16-le")
            for _ in range(10):                  # the clipboard may be busy for a moment
                if u32.OpenClipboard(None):
                    break
                time.sleep(0.05)
            else:
                return False
            try:
                u32.EmptyClipboard()
                handle = k32.GlobalAlloc(0x0002, len(data))        # GMEM_MOVEABLE
                if not handle:
                    return False
                ptr = k32.GlobalLock(handle)
                if not ptr:
                    return False
                ctypes.memmove(ptr, data, len(data))
                k32.GlobalUnlock(handle)
                return bool(u32.SetClipboardData(13, handle))      # CF_UNICODETEXT
            finally:
                u32.CloseClipboard()
        if IS_MAC:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=5, check=True)
            return True
    except Exception as exc:  # noqa: BLE001
        log(f"Could not use the clipboard: {type(exc).__name__}: {exc}")
    return False


def _tap_key(key):
    pydirectinput.keyDown(key)
    try:
        time.sleep(MODIFIER_KEY_HOLD)
    finally:
        pydirectinput.keyUp(key)


def reconnect_paste_vip_code():
    """Paste the VIP code into the box that is focused and press Enter. The code goes on the clipboard
    and Ctrl+V (Cmd+V on a Mac) pastes it; when the clipboard can't be used it is typed key by key."""
    code = reconnect_vip_settings()["code"]
    if set_clipboard_text(code):
        mod = "command" if IS_MAC else "ctrl"
        pydirectinput.keyDown(mod)
        try:
            time.sleep(MODIFIER_KEY_HOLD)
            _tap_key("v")
            time.sleep(MODIFIER_KEY_HOLD)
        finally:
            pydirectinput.keyUp(mod)
    else:
        log("Reconnect: clipboard not available - typing the VIP code instead")
        for ch in code:
            if ch.isupper():
                pydirectinput.keyDown("shift")
            try:
                _tap_key(ch.lower())
            finally:
                if ch.isupper():
                    pydirectinput.keyUp("shift")
            time.sleep(MODIFIER_KEY_GAP / 2)
    wait(RECONNECT_VIP_PASTE_WAIT, scan=False)
    _tap_key("enter")


def reconnect_seen(kind):
    """True when the Select / Play picture of Reconnect is on the screen right now."""
    tpl = _reconnect_images.get(kind)
    return bool(tpl) and bool(find_quality_template(grab_screen(), tpl, RECONNECT_THRESHOLD))


def reconnect_wait_for(gate, i, total, name):
    """Wait until the Select / Play picture is on screen. False when it has not shown within
    RECONNECT_IMAGE_TIMEOUT."""
    label = RECONNECT_IMAGE_NAMES[gate]
    give_up = time.time() + RECONNECT_IMAGE_TIMEOUT
    while not reconnect_seen(gate):
        left = give_up - time.time()
        if left <= 0:
            log(f'Reconnect: the "{label}" picture did not show within '
                f"{RECONNECT_IMAGE_TIMEOUT:g}s - {name} was not clicked")
            return False
        show("Reconnecting", f'Step {i}/{total}: waiting for "{label}" to show ({left:.0f}s)')
        wait(0.5, scan=False)
    log(f'Reconnect: "{label}" seen')
    return True


def reconnect_countdown(i, total, name, seconds):
    end = time.time() + seconds
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            break
        show("Reconnecting", f"Step {i}/{total}: {name} in {remaining:.0f}s")
        wait(min(0.5, remaining), scan=False)


def reconnect_clicks():
    """After the rejoin: click the picked coordinates one by one (the 4 shared ones, then the 4 of
    the Missions / Raids tab). The first click (Pick a slot) waits until the Select picture is on
    screen and happens RECONNECT_FIRST_DELAY seconds later - no other wait before it. Every other
    click waits the Click delay (reconnect_click_delay(), 2.5s by default; the Play click after Create waits the Play delay, 0.5s by default), and Click play (lobby) then also waits until the Play
    picture is on screen. Returns False when a picture did not show within RECONNECT_IMAGE_TIMEOUT
    (nothing more is clicked then), True when every click was made."""
    steps = reconnect_steps()
    total = len(steps)
    for i, (name, (x, y), gate, key) in enumerate(steps, 1):
        if gate == "select":
            if not reconnect_wait_for(gate, i, total, name):
                return False
            if not state.get("rc_notified"):      # Discord: the game is back (Select seen) - once per rejoin
                state["rc_notified"] = True
                notify_async("Reconnected successfully - the game has loaded, clicking through the lobby now.")
            reconnect_countdown(i, total, name, RECONNECT_FIRST_DELAY)
        else:
            reconnect_countdown(i, total, name, reconnect_play_delay() if key in RECONNECT_PLAY_KEYS
                                else reconnect_click_delay())
            if gate and not reconnect_wait_for(gate, i, total, name):
                return False
        if key == RECONNECT_VIP_CODE_KEY:
            show("Reconnecting", f"Step {i}/{total}: pasting the VIP code")
            log(f"Reconnect step {i}/{total}: pasting the VIP code and pressing Enter")
            reconnect_paste_vip_code()
            continue
        show("Reconnecting", f"Step {i}/{total}: clicking {name}")
        log(f"Reconnect step {i}/{total}: clicking {name} ({x}, {y})")
        click_middle(x, y)
    return True


def reconnect_launch(reason):
    """One rejoin: close Roblox, launch the game again (roblox://placeId=...), stay hands off while
    it loads - for at most RECONNECT_LOAD_WAIT seconds: the wait ends as soon as the Select picture
    is on screen."""
    state["rescuing"] = False
    state["rescue_t0"] = None
    state["mission_t0"] = None
    _raid["active"] = False
    _skip_spam["until"] = 0.0
    release_all_keys()
    set_status("waiting")
    show("Reconnecting", "Closing Roblox")
    log(f"Reconnect: {reason} - closing Roblox and rejoining the game")
    if IS_WIN:
        for exe in ("RobloxPlayerBeta.exe", "Windows10Universal.exe"):
            try:
                subprocess.run(["taskkill", "/F", "/IM", exe], capture_output=True,
                               timeout=10, creationflags=0x08000000)
            except Exception:  # noqa: BLE001
                pass
    wait(RECONNECT_CLOSE_WAIT, scan=False)
    link = f"roblox://placeId={ROBLOX_GAME_ID}"
    show("Reconnecting", f"Launching game {ROBLOX_GAME_ID}")
    log(f"Launching {link}")
    try:
        if IS_WIN:
            os.startfile(link)
        else:
            webbrowser.open(link)
    except Exception as exc:  # noqa: BLE001
        log(f"Could not launch Roblox: {type(exc).__name__}: {exc}")
    end = time.time() + RECONNECT_LOAD_WAIT
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            break
        if reconnect_seen("select"):          # the lobby is up: no need to wait out the rest
            log('Reconnect: "Select" seen - skipping the rest of the load wait')
            break
        show("Reconnecting", f"Waiting for the game to load... {remaining:.0f}s")
        wait(min(0.5, remaining), scan=False)


def reconnect_rejoin(reason="the Disconnect Image was seen"):
    """Force a rejoin: reconnect_launch(), then click the picked coordinates step by step
    (reconnect_clicks). If the Select / Play picture never shows, the whole rejoin is tried again
    (up to RECONNECT_MAX_ATTEMPTS). The macro goes back to waiting for the next mission afterwards."""
    state["reconnecting"] = True
    state["rc_notified"] = False
    # Discord: tell the user (with a mention when "Mention me" is on) that the rejoin started
    if str(reason).startswith("the Disconnect Image"):
        notify_async("Disconnected from the game - closing Roblox and rejoining...")
    else:
        notify_async(f"Rejoining the game ({reason}) - closing Roblox and rejoining...")
    try:
        for attempt in range(1, RECONNECT_MAX_ATTEMPTS + 1):
            reconnect_launch(reason)
            log(f"Game loaded - waiting for the Select picture, then clicking "
                f"{len(reconnect_steps())} spots ({reconnect_click_delay():g}s apart)")
            if reconnect_clicks():
                if modifiers_enabled():
                    state["rc_mods_pending"] = True     # the first retry window after this rejoin adds the modifiers
                log("Rejoin done - waiting for the next mission")
                return
            reason = "a Reconnect picture did not show"
            if attempt < RECONNECT_MAX_ATTEMPTS:
                log(f"Rejoining again (attempt {attempt + 1}/{RECONNECT_MAX_ATTEMPTS})")
        log(f"Reconnect gave up after {RECONNECT_MAX_ATTEMPTS} attempts - waiting for the next mission")
        notify_async(f"Reconnect FAILED after {RECONNECT_MAX_ATTEMPTS} attempts - please check the game.")
    finally:
        state["reconnecting"] = False
        state["rc_next"] = time.time() + RECONNECT_COOLDOWN


def tick(scan_retry=True):
    """Called constantly: handles stop, pause, retry-button detection and the stuck-mission rescue."""
    if state["quit"]:
        raise SystemExit
    while state["paused"]:
        if state["quit"]:
            raise SystemExit
        time.sleep(0.1)
    if _disconnect_template is not None and not state.get("reconnecting"):
        # Reconnect is on: three ways to rejoin - the bot command, a failsafe rescue that has not
        # ended after RECONNECT_RESCUE_LIMIT (2 min), or the Disconnect Image
        if state.pop("rc_request", False):
            raise Disconnected("the bot command !reconnect was used")
        t0 = state.get("rescue_t0")
        if state["rescuing"] and t0 and time.time() - t0 > RECONNECT_RESCUE_LIMIT:
            raise Disconnected(f"the failsafe has been rescuing for {RECONNECT_RESCUE_LIMIT / 60:g} minutes")
        if time.time() >= state.get("rc_next", 0.0):
            state["rc_next"] = time.time() + RECONNECT_CHECK_INTERVAL
            if find_quality_template(grab_screen(), _disconnect_template, RECONNECT_THRESHOLD):
                raise Disconnected
    if scan_retry:
        t0 = state["mission_t0"]
        if state["debug_rescue"] and not state["rescuing"]:     # DEBUG: F3+K
            state["debug_rescue"] = False
            rescue_clicks(debug=True)
        elif (not state["rescuing"] and t0 is not None
                and settings["family"] != "Female Raid"      # the raid has its own failsafe (raid_failsafe_*)
                and time.time() - t0 > mission_timeout()):
            rescue_clicks()
        if _raid["active"] and not _raid["in_raid_wait"] and not state["rescuing"]:
            raid_skip_poll()        # the skip button is watched during every other raid action too
        interval = FAST_SCAN_INTERVAL if tv(settings["family"], "fast_mode") else SCAN_INTERVAL
        if time.time() - _last_scan >= interval:
            scan_for_retry()


def wait(seconds, scan=True):
    end = time.time() + seconds
    while True:
        tick(scan)
        remaining = end - time.time()
        if remaining <= 0:
            break
        time.sleep(min(0.05, remaining))


# ----------------------------- inputs -----------------------------------
def tap(key):
    pydirectinput.keyDown(key)
    try:
        wait(random.uniform(0.03, 0.07), scan=False)   # raises SystemExit when stopped
    finally:
        pydirectinput.keyUp(key)   # never leave the key held down (it breaks the hotkeys)


def release_all_keys():
    """Best effort: let go of every key the macro presses (called when it stops)."""
    for key in {KEY_ABILITY_1ST, KEY_ABILITY_2ND, KEY_ABILITY_3RD, KEY_ABILITY_LAST,
                KEY_DASH, KEY_STRAFE, KEY_STRAFE_D, KEY_GRAB, KEY_BACK, KEY_RAID_SKIP}:
        try:
            pydirectinput.keyUp(key)
        except Exception:  # noqa: BLE001
            pass


def double_tap(key):
    """One dash/strafe = the key pressed twice (like a-a)."""
    tap(key)
    wait(random.uniform(*tv("Movement", "tap_gap")), scan=False)
    tap(key)


def click_at(x, y, tw, th):
    x += random.randint(-tw // 6, tw // 6)
    y += random.randint(-th // 6, th // 6)
    pydirectinput.moveTo(x, y)
    time.sleep(random.uniform(0.08, 0.2))
    pydirectinput.moveRel(1, 0)
    time.sleep(0.05)
    # spam the button for a moment instead of one click (stop still works)
    end = time.time() + tv("Movement", "retry_click_time")
    interval = tv("Movement", "retry_click_interval")
    while True:
        if state["quit"]:
            raise SystemExit
        pydirectinput.click()
        remaining = end - time.time()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))


def save_click_debug(cx, cy, tw, th):
    """Save a screenshot with a box where the click will land (for troubleshooting)."""
    if not SAVE_CLICK_DEBUG:
        return
    img = grab_screen()
    cv2.rectangle(img, (cx - tw // 2, cy - th // 2), (cx + tw // 2, cy + th // 2),
                  (0, 0, 255), 2)
    cv2.circle(img, (cx, cy), 6, (0, 0, 255), -1)
    out = os.path.join(app_dir(), "last_retry_click.png")
    cv2.imwrite(out, img)


# ----------------------------- loot tracking ----------------------------
def gold_mask(bgr):
    """Black/white image of the gold text pixels (so the background doesn't matter)."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (12, 70, 150), (40, 255, 255))


_loot_fix_log = {"size": None}


def scan_end_screen(quality="normal", want_loot=True, want_result=True, scan_time=None, frame=None):
    """
    Look at the end screen for a moment. Only the loot icons of the same graphics quality as
    the retry button are looked for ("low" icons for a low graphics screen), so the two looks
    never get mixed up.
    Returns (loot, result):
      loot   = {kind: (cx, cy, score, w, h)} for every loot icon seen
      result = ("completed" | "failed", (cx, cy, score, w, h)) or None
    want_loot / want_result turn one of the two scans off.
    frame: fast mode - read this one screenshot (taken when the retry button was found) instead of
    looking at the live screen for scan_time seconds.
    """
    loot_found = {}
    loot_sizes = {}
    result_best = {}
    fix = bool(settings.get("fix_loot"))      # "Fix loot tracking" (Beta): loot icons at any size
    loot_thr = relax(LOOT_THRESHOLD) - (LOOT_FIX_RELAX if fix else 0.0)
    wanted = active_loot_kinds()
    loot_tpls = ({k: t for k, t in _loot_templates.get(quality, {}).items() if k in wanted}
                 if want_loot else {})
    end = time.time() + (LOOT_SCAN_TIME if scan_time is None else scan_time)
    while True:
        screen = frame if frame is not None else grab_screen()
        for kind, tpl in loot_tpls.items():
            if fix:
                best, size = best_match_any_size(screen, tpl, LOOT_FIX_SCALES)
            else:
                best, size = best_match(screen, tpl), 1.0
            if best and best[2] >= loot_thr:
                if kind not in loot_found or best[2] > loot_found[kind][2]:
                    loot_found[kind] = best
                    loot_sizes[kind] = size
        if _result_templates and want_result:
            mask = gold_mask(screen)
            for kind, tpl in _result_templates.items():
                th, tw = tpl.shape[:2]
                if th > mask.shape[0] or tw > mask.shape[1]:
                    continue
                res = cv2.matchTemplate(mask, tpl, cv2.TM_CCOEFF_NORMED)
                _, score, _, loc = cv2.minMaxLoc(res)
                if kind not in result_best or score > result_best[kind][2]:
                    result_best[kind] = (loc[0] + tw // 2, loc[1] + th // 2, score, tw, th)
        if frame is not None or time.time() >= end:
            break
        wait(LOOT_SCAN_STEP, scan=False)

    if fix and loot_found:
        size = loot_sizes[max(loot_found, key=lambda k: loot_found[k][2])]
        if _loot_fix_log["size"] != size:         # log the size only when it changes
            _loot_fix_log["size"] = size
            log(f"Fix loot tracking: loot icons matched at size x{size:.2f}")

    result = None
    for kind, m in result_best.items():
        if m[2] >= relax(RESULT_THRESHOLD) and (result is None or m[2] > result[1][2]):
            result = (kind, m)

    if SAVE_LOOT_DEBUG and (loot_found or result):
        img = frame.copy() if frame is not None else grab_screen()
        marks = {k: (v, (0, 255, 0)) for k, v in loot_found.items()}
        if result:
            marks["result " + result[0]] = (result[1], (0, 200, 255))
        for label, ((cx, cy, score, tw, th), color) in marks.items():
            cv2.rectangle(img, (cx - tw // 2, cy - th // 2), (cx + tw // 2, cy + th // 2),
                          color, 2)
            cv2.putText(img, f"{label} {score:.2f}", (cx - tw // 2, cy - th // 2 - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        out = os.path.join(app_dir(), "last_end_scan.png")
        cv2.imwrite(out, img)
    return loot_found, result


def record_result(result, counted=False):
    """Count the mission as completed or failed (if the text was seen). counted=True: the macro
    already added it itself (addwin / addlose)."""
    if counted:
        push_stats()
        return
    if result is None:
        log("Result: not detected")
    else:
        kind = result[0]
        counters()[1][kind] += 1
        log(f"Result: {kind.capitalize()}")
    push_stats()


def record_loot(found):
    """Add one to each kind that was seen, log it, update the window."""
    lt = counters()[2]
    for kind in found:
        lt[kind] += 1
    if not found:
        log("Loot: nothing found")
    else:
        log("Loot: " + ", ".join(loot_parts(found)))
    push_stats()


# ------------------- mission start / end handling -----------------------
def wait_for_mission_start(scan_retry=True):
    """Block until the ability-2 icon appears (or F8 is pressed).

    scan_retry=False means hands-off: no retry-button scanning and no clicks.
    It is used right after clicking retry, so we never click twice.
    """
    set_status("waiting")
    set_step(-1)
    raid = raid_counting()
    show("Waiting for the raid" if raid else "Waiting for the mission",
         "Looking for the skip button / raid_start.png" if raid else "Looking for the ability 2 icon")
    log("Waiting for the raid to start (skip button / raid_start.png)" if raid else "Waiting for the mission to start")
    while True:
        tick(scan_retry=scan_retry)
        if state["manual_start"]:
            state["manual_start"] = False
            return
        fast = tv(settings["family"], "fast_mode")
        if start_marker_visible():
            if raid:
                log("Raid started (skip button or raid_start.png seen)")
                return
            log("Mission started (icon detected)")
            if not fast:
                wait(START_SETTLE_DELAY, scan=False)    # fast mode: not a moment is wasted
            return
        time.sleep(FAST_START_POLL if fast else 0.2)


def hands_off_wait(seconds):
    """Do nothing (no scanning, no inputs) for a while, showing a countdown."""
    end = time.time() + seconds
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            break
        show("Hands off", f"Waiting after the retry click... {remaining:.0f}s")
        wait(min(0.5, remaining), scan=False)


def wait_for_icon_gone():
    """After retry, wait for the old mission HUD to disappear (loading)."""
    show("Hands off", "Waiting for the next mission to load")
    deadline = time.time() + ICON_GONE_TIMEOUT
    while time.time() < deadline:
        tick(scan_retry=False)
        if not start_marker_visible():
            return
        time.sleep(0.2)


def buy_time_until_ability4_ready():
    """Strafe A for the length of ability 4's cooldown. Call right after ability 3.
    Returns True when the Shiki ability 4 icon was seen (buying time over early), else False."""
    start = time.time()
    total = tv("Shiki", "cooldown") + tv("Shiki", "cooldown_margin")
    keys = BUY_TIME_MOVES
    i = 0
    next_strafe = start
    next_look = start + tv("Shiki", "icon_after")
    while True:
        now = time.time()
        remaining = total - (now - start)
        if remaining <= 0:
            return False
        if _shiki_ability4_icon and now >= next_look:
            next_look = now + tv("Shiki", "icon_interval")
            if find_quality_template(grab_screen(), _shiki_ability4_icon,
                                     tv("Shiki", "icon_threshold")) is not None:
                log("Shiki ability 4 icon seen")
                return True
        detail(f"Strafing {KEY_STRAFE.upper()}... ability 4 ready in {remaining:.0f}s")
        if now >= next_strafe:
            double_tap(keys[i % len(keys)])
            i += 1
            next_strafe = time.time() + random.uniform(*tv("Shiki", "strafe_gap"))
        wait(0.05)


def spam_key_for(key, seconds, interval, label):
    """Press one key over and over for a while."""
    end = time.time() + seconds
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return
        detail(f"Spamming {label}... {remaining:.0f}s left")
        tap(key)
        wait(min(interval, max(remaining, 0.01)))


def spam_recorded_dash(key, seconds, tight=False):
    """The movement (every config): Space pressed over and over with the timings of a recorded macro
    (each press held ~65-90 ms, ~50-80 ms between presses)."""
    end = time.time() + seconds
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return
        show("Dashing", f"Spamming dash... {remaining:.0f}s left")
        pydirectinput.keyDown(key)
        try:
            wait(random.uniform(*tv("Movement", "dash_hold")), scan=False)
        finally:
            pydirectinput.keyUp(key)
        if time.time() >= end:      # last press done: no trailing gap, move on at once
            return
        gap = random.uniform(*tv("Movement", "dash_gap"))
        if tight and end - time.time() <= gap:   # custom macros: the next press would not fit, so
            return                               # finish now instead of waiting the gap out
        wait(gap, scan=False)


def shift_sequence(fam=None):
    """The shift: ability 5 spam, the wait, shifted ability 4, then ability 5 until the retry
    button shows up."""
    fam = fam or settings["family"]     # this config's own shift timings (Edit configs)
    shift_spam_time = tv(fam, "shift_spam_time")
    shift_spam_interval = tv(fam, "shift_spam_interval")
    shift_spam_extra = tv(fam, "shift_spam_extra")
    shifted4_at = tv(fam, "shifted4_at")
    shifted4_time = tv(fam, "shifted4_time")
    shifted4_interval = tv(fam, "shifted4_interval")
    shifted4_to5 = tv(fam, "shifted4_to5")
    ability5_interval = tv(fam, "ability5_interval")
    set_step(6)
    show("Shift", "Spamming ability 5 so the shift is used")
    log(f"Ability 5 spam for {shift_spam_time:g}s (shift)")
    spam_end = time.time() + shift_spam_time
    while time.time() < spam_end:
        tap(KEY_ABILITY_LAST)
        wait(shift_spam_interval)
    log(f"Waiting {shifted4_at:g}s for the shift (then shifted ability 4)")
    shift_end = time.time() + shifted4_at
    extra_end = time.time() + shift_spam_extra   # still pressing 5 quietly during the first seconds of the wait
    while True:
        remaining = shift_end - time.time()
        if remaining <= 0:
            break
        show("Shift", f"Waiting for the shift... {remaining:.0f}s")
        if time.time() < extra_end:
            tap(KEY_ABILITY_LAST)
            wait(min(shift_spam_interval, remaining))
        else:
            wait(min(0.5, remaining))

    set_step(7)
    log(f"Shifted ability 4 spam for {shifted4_time:g}s")
    spam_key_for(KEY_ABILITY_3RD, shifted4_time, shifted4_interval,
                 "shifted ability 4")
    show("Shifted ability 4", f"Waiting {shifted4_to5:g}s before ability 5")
    wait(shifted4_to5)

    set_step(8)
    move = settings["move_shifted"]
    hold_key = KEY_STRAFE_D if move == "Right" else KEY_STRAFE if move == "Left" else None
    hold_txt = f"holding {hold_key.upper()}" if hold_key else "standing still"
    log(f"Ability 5 over and over + {hold_txt} (until the mission ends "
        f"or the mission passes {mission_timeout():g}s)")
    presses = 0
    next_5 = time.time()
    d_held = False
    try:
        while True:
            now = time.time()
            t0 = state["mission_t0"]
            timed_out = t0 is not None and now - t0 > mission_timeout()
            if timed_out:
                if d_held:                       # mission too long: let go of the key
                    pydirectinput.keyUp(hold_key)
                    d_held = False
                    log(f"Mission over {mission_timeout():g}s - released {hold_key.upper()}")
            elif hold_key and not d_held:
                pydirectinput.keyDown(hold_key)   # hold A / D (the end of the mission ends this)
                d_held = True
            if now >= next_5:
                presses += 1
                show("Ability 5", f"Pressed x{presses} + {hold_txt} "
                                  "- until the mission ends")
                tap(KEY_ABILITY_LAST)
                next_5 = time.time() + ability5_interval
            wait(0.05)
    finally:
        if hold_key:
            pydirectinput.keyUp(hold_key)    # retry found / stop / error: never leave it held


# ---------------------------- the routine -------------------------------
def raid_find(kind, screen):
    """Look for the skip / finish button. Returns the match (cx, cy, score, w, h) or None."""
    tpl = _raid_templates.get(kind)
    if not tpl:
        return None
    thr = {"skip": RAID_SKIP_THRESHOLD, "finish": RAID_FINISH_THRESHOLD,
           "heatwave": RAID_HEATWAVE_THRESHOLD, "start": RAID_START_THRESHOLD}[kind]
    hit = find_quality_template(screen, tpl, thr)
    return hit[0] if hit else None


def raid_skip_visible():
    return raid_find("skip", grab_screen()) is not None


def start_marker_visible():
    """The thing that tells a new mission / raid has started: the ability 2 icon, or for Female Raid
    the skip button or raid_start.png (whichever shows up first marks the raid's start)."""
    if raid_counting():
        screen = grab_screen()
        return raid_find("skip", screen) is not None or raid_find("start", screen) is not None
    return icon_visible()


def raid_click_spam(x, y, seconds, interval, label, jitter=2):
    """Move onto a screen spot and click it over and over for `seconds` (stop / pause still work)."""
    x += random.randint(-jitter, jitter)
    y += random.randint(-jitter, jitter)
    pydirectinput.moveTo(int(x), int(y))
    time.sleep(random.uniform(0.08, 0.2))
    pydirectinput.moveRel(1, 0)
    time.sleep(0.05)
    end = time.time() + seconds
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return
        detail(f"Clicking {label}... {remaining:.0f}s left")
        pydirectinput.click()
        wait(min(interval, max(remaining, 0.01)), scan=False)


class RaidFinished(Exception):
    """The finish button was handled: nothing is left to do but the retry button."""


_raid = {"skips": 0, "skip_visible": False, "last_look": 0.0, "heatwave": False,
         "next_a1": None, "start_seen": False, "step": -1, "title": "Raid", "detail": "",
         "seq_done_t": None,     # when the shifting + waiting sequence ended (raid failsafe timer)
         "active": False, "in_raid_wait": False, "last_skip_look": 0.0}
_skip_spam = {"until": 0.0, "thread": None}


def _skip_spam_worker():
    """Background E presses, so the skip spam overlaps whatever the macro is doing meanwhile."""
    while time.time() < _skip_spam["until"] and not state["quit"]:
        if state["paused"] or state["rescuing"]:
            time.sleep(0.05)
            continue
        try:
            pydirectinput.keyDown(KEY_RAID_SKIP)
            time.sleep(random.uniform(0.03, 0.06))
            pydirectinput.keyUp(KEY_RAID_SKIP)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(max(0.02, tv("Female Raid", "skip_spam_interval")))
    _skip_spam["thread"] = None


def raid_skip_e_spam(seconds):
    """Spam E for `seconds` from now (a new sighting restarts / extends the time)."""
    _skip_spam["until"] = time.time() + seconds
    th = _skip_spam["thread"]
    if th is None or not th.is_alive():
        th = threading.Thread(target=_skip_spam_worker, daemon=True)
        _skip_spam["thread"] = th
        th.start()


def raid_skip_handle(screen):
    """The skip button check (used by raid_watch and, during other actions, by tick). Returns True
    while the button is on screen. E is spammed in the background for the E spam time (3s); a skip
    button counts once per time it shows up."""
    if not raid_find("skip", screen):
        _raid["skip_visible"] = False
        return False
    new = not _raid["skip_visible"]
    _raid["skip_visible"] = True
    if new:
        _raid["skips"] += 1
        _raid["start_seen"] = True          # the first skip starts the buffs
        n = _raid["skips"]
        e_time = RAID_SKIP_FIRST_SPAM_TIME if n == 1 else tv("Female Raid", "skip_spam_time")
        raid_skip_e_spam(e_time)            # once per sighting (the first skip: 0.5s, the rest: 2s)
        log(f"Skip button seen (no. {n}) - spamming {KEY_RAID_SKIP.upper()} for {e_time:g}s")
        if n == 2:
            raid_phase(2, "Killing Eren", "Skip no. 2")
        if n == int(tv("Female Raid", "skip_number")):
            show("Ability 3", f"Skip no. {n}: ability 3 once")
            log(f"Skip no. {n}: ability 3 once")
            tap(KEY_ABILITY_2ND)
            _raid["heatwave"] = True
            raid_phase(3, "Killing Female titan", f"Skip no. {n}: waiting for heatwave")
            log(f"Skip no. {n}: waiting for heatwave.png (then ability 1)")
    return True


def raid_skip_poll():
    """tick() calls this during every raid action, so the skip button is noticed in time."""
    now = time.time()
    if now - max(_raid["last_skip_look"], _raid["last_look"]) < RAID_LOOK_INTERVAL:
        return
    _raid["last_skip_look"] = now
    raid_skip_handle(grab_screen())


def raid_trigger_failsafe(reason):
    """Raid failsafe: the same rescue as the mission failsafe (ability 2, look at the ground, click
    the spot picked in Settings > Failsafe - or the middle - until retry shows up). Only the trigger
    differs. Never returns: it ends with RetryFound / stop."""
    rescue_clicks(reason=reason)


def raid_failsafe_time_check():
    """5 min since the raid began, or 3 min since the shifting + waiting sequence ended."""
    t0 = state["mission_t0"]
    if t0 is None or state["rescuing"]:
        return
    now = time.time()
    if now - t0 > RAID_FAILSAFE_MAX_TIME:
        raid_trigger_failsafe(f"raid longer than {RAID_FAILSAFE_MAX_TIME / 60:g} min")
    seq = _raid["seq_done_t"]
    if seq is not None and now - seq > RAID_FAILSAFE_AFTER_SEQUENCE:
        raid_trigger_failsafe(f"{RAID_FAILSAFE_AFTER_SEQUENCE / 60:g} min after the "
                              "shifting + waiting sequence")


def raid_phase(step, title, detail_text=""):
    """Move the status card to a raid step (0-5). It never goes back to an earlier step; the same
    step may get a new title."""
    if step < _raid["step"]:
        return
    _raid.update(step=step, title=title, detail=detail_text)
    set_step(step)
    show(title, detail_text)


def raid_back_to_phase():
    """Show the current raid step's title again (after a temporary 'Skip' / 'Ability 1' title)."""
    show(_raid["title"], _raid["detail"] or "Waiting for the skip / finish / retry button")


def raid_wait_finish_button(timeout):
    """After a chest was clicked: wait with 'Opening chest' on the status card until the finish
    button (finish_button.png) shows up - that is how we know the chest is open. Returns its match,
    or None after `timeout` seconds (stop / pause / the retry button still work)."""
    end = time.time() + timeout
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return None
        show("Opening chests", f"Waiting for the finish button... {remaining:.0f}s")
        hit = raid_find("finish", grab_screen())
        if hit:
            return hit
        wait(RAID_FINISH_LOOK, scan=True)


def raid_finish(match, phase=True):
    """The finish button is on screen: chest 1 (clicked over and over), wait until the finish button
    shows up again (= the chest is open), chest 2 (if on) the same, then the finish button is
    clicked over and over. The retry button that shows up afterwards is
    clicked by the normal retry handling."""
    cx, cy, _score, tw, th = match
    t = lambda key: tv("Female Raid", key)    # noqa: E731
    log("Finish button seen")
    if phase:                       # (openchest() in a custom macro has no raid steps)
        raid_phase(4, "Opening chests", "Finish button seen")
    for n, key in ((1, "chest1"), (2, "chest2")):
        if n == 2 and not t("open_chest2"):
            continue
        spot = t(key)
        if spot is None:
            log(f"WARNING: Female Raid - Chest {n} coordinate is not set, not opening it")
            continue
        log(f"Opening chest {n} at {spot[0]}, {spot[1]}")
        show("Opening chests", f"Clicking chest {n}")
        raid_click_spam(spot[0], spot[1], t("chest_click_time"), t("chest_click_interval"),
                        f"chest {n}")
        hit = raid_wait_finish_button(t("finish_wait"))
        if hit:
            log(f"Chest {n} open (finish button seen)")
            cx, cy, _score, tw, th = hit
        else:
            log(f"WARNING: finish button not seen {t('finish_wait'):g}s after chest {n}")
    show("Opening chests", "Clicking the finish button")
    log(f"Clicking finish for {t('finish_click_time'):g}s")
    raid_click_spam(_monitor["left"] + int(cx * _scale), _monitor["top"] + int(cy * _scale),
                    t("finish_click_time"), t("finish_click_interval"), "finish",
                    jitter=max(1, int(min(tw, th) * _scale) // 6))


def raid_watch(force=False):
    """One look at the screen (every RAID_LOOK_INTERVAL, or at once with force). The skip button is
    checked first so its E presses start right away:
      skip button     -> E spam; after the Nth skip (Edit configs, default 3) ability 3 is pressed once
                         and the macro starts waiting for heatwave.png
      finish button   -> chests + finish click, then RaidFinished is raised
      heatwave.png    -> (only while waiting for it) ability 1 spam
    A skip button counts once per time it shows up (while it stays on screen it is the same one)."""
    now = time.time()
    if not force and now - _raid["last_look"] < RAID_LOOK_INTERVAL:
        return
    _raid["last_look"] = now
    screen = grab_screen()
    _raid["last_skip_look"] = now
    if raid_skip_handle(screen):
        raid_back_to_phase()
    fin = raid_find("finish", screen)
    if fin:
        raid_finish(fin)
        raise RaidFinished
    if (_raid["heatwave"]                      # armed by the 3rd skip, stays armed
            and raid_find("heatwave", screen)):
        log("Heatwave seen")
        raid_ability1_burst()


def raid_ability1_burst():
    """Ability 4 once, then spam ability 1 for as long as heatwave.png stays on screen. Nothing
    else is done meanwhile (no skip handling, no ability cycle) - only stop / pause / the retry
    button and the raid failsafe keep working. When the heatwave is gone the raid goes on."""
    _skip_spam["until"] = 0.0                       # drop any E spam still running
    interval = tv("Female Raid", "heatwave_interval")
    show("Ability 1", "Ability 4 once, then ability 1")
    log("Ability 4 once")
    tap(KEY_ABILITY_3RD)
    log("Spamming ability 1 while the heatwave is visible")
    _raid["in_raid_wait"] = True                    # tick() must not look for the skip button now
    t_start = time.time()
    misses = 0
    try:
        while True:
            show("Ability 1", f"Spamming ability 1 while the heatwave is visible "
                              f"({time.time() - t_start:.0f}s)")
            if raid_find("heatwave", grab_screen()):
                misses = 0
                tap(KEY_GRAB)
            else:
                misses += 1
                if misses >= 2:                     # gone on two looks in a row
                    break
            wait(max(0.02, interval))
            raid_failsafe_time_check()
    finally:
        _raid["in_raid_wait"] = False
    log(f"Heatwave gone after {time.time() - t_start:.1f}s - ability 1 spam stopped")
    _raid["next_a1"] = None
    raid_back_to_phase()


def raid_wait(seconds):
    """wait() that also keeps an eye on the skip / finish button."""
    end = time.time() + seconds
    while True:
        _raid["in_raid_wait"] = True        # raid_watch() below looks for the skip button itself
        try:
            tick()
        finally:
            _raid["in_raid_wait"] = False
        raid_watch()
        raid_failsafe_time_check()
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(RAID_LOOP_DELAY, remaining))


def raid_spam(key, seconds, interval, label):
    """spam_key_for() that also keeps an eye on the skip / finish button."""
    end = time.time() + seconds
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return
        detail(f"Spamming {label}... {remaining:.0f}s left")
        tap(key)
        raid_wait(min(interval, max(remaining, 0.01)))


def raid_sequence():
    """After the first skip: ability 1, spam ability 3, spam ability 5, wait, spam ability 1, then
    ability 4 -> 3 -> 2 (over and over) every few seconds until the raid ends. (Ability 1 is the
    "escape grab" key in Settings > Keybinds - the same key in the game.) Every number is in Edit configs."""
    t = lambda key: tv("Female Raid", key)    # noqa: E731
    raid_phase(0, "Using buffs", "Ability 1")
    log(f"Ability 1 spam for {t('open_a1_time'):g}s")
    raid_spam(KEY_GRAB, t("open_a1_time"), t("a1_interval"), "ability 1")
    log(f"Ability 3 spam for {t('a3_time'):g}s")
    raid_spam(KEY_ABILITY_2ND, t("a3_time"), t("a3_interval"), "ability 3")
    log(f"Ability 5 spam for {t('a5_time'):g}s")
    raid_spam(KEY_ABILITY_LAST, t("a5_time"), t("a5_interval"), "ability 5")
    log(f"Waiting {t('wait_time'):g}s (ability 5 keeps spamming for the first {t('a5_extra'):g}s)")
    end = time.time() + t("wait_time")
    extra_end = time.time() + t("a5_extra")   # still pressing 5 during the first seconds of the wait
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            break
        raid_phase(1, "Shifting", f"Waiting... {remaining:.0f}s")
        if time.time() < extra_end:
            tap(KEY_ABILITY_LAST)
            raid_wait(min(t("a5_interval"), remaining))
        else:
            raid_wait(min(0.5, remaining))
    _raid["seq_done_t"] = time.time()     # shifting + waiting is done: the raid failsafe timer starts
    log(f"Shifting + waiting done - raid failsafe in {RAID_FAILSAFE_AFTER_SEQUENCE / 60:g} min")
    raid_phase(1, "Killing Female titan", "Ability 1 spam")
    log(f"Ability 1 spam for {t('a1_time'):g}s")
    raid_spam(KEY_GRAB, t("a1_time"), t("a1_interval"), "ability 1")
    log("Abilities 4 -> 3 -> 2 (over and over) until the raid ends")
    cycle = ((4, KEY_ABILITY_3RD), (3, KEY_ABILITY_2ND), (2, KEY_ABILITY_1ST))
    n = 0
    while True:
        num, key = cycle[n % 3]
        n += 1
        show(_raid["title"], f"Ability {num}")
        tap(key)
        raid_wait(t("random_gap"))


def raid_routine():
    """Female Raid. The raid starts when the skip button or raid_start.png is seen (see
    wait_for_mission_start). The buffs (ability 1, 3, 5 ...) start when raid_start.png is seen (if
    that picture is missing: at the first skip). Skip button = E spam (and ability 3 on the Nth one),
    finish button = chests + finish, retry button = the end (tick() raises RetryFound, handled as
    usual)."""
    _raid.update(skips=0, skip_visible=False, last_look=0.0, heatwave=False, next_a1=None,
                 start_seen=False, step=-1, seq_done_t=None, title="Raid started",
                 detail="Waiting for the first skip")
    log("Female Raid: raid started")
    show(_raid["title"], _raid["detail"])
    _raid["active"] = True
    _skip_spam["until"] = 0.0
    try:
        try:
            raid_watch(force=True)          # E if the skip button is what started the raid
            while not _raid["start_seen"]:  # the buffs wait for the first skip
                raid_wait(0.05)
            log("First skip seen - using buffs")
            raid_sequence()                 # never returns by itself
        except RaidFinished:
            pass
        _raid["active"] = False             # finished: no more skip polling
        _skip_spam["until"] = 0.0
        show("Opening chests", "Finish clicked - waiting for the retry button")
        while True:
            wait(0.2)
    finally:
        _raid["active"] = False             # retry / stop: the raid is over
        _skip_spam["until"] = 0.0


def warn_raid_config():
    """Console warnings when Female Raid is the picked config and a chest spot is missing."""
    if settings["family"] != "Female Raid":
        return
    if tv("Female Raid", "chest1") is None:
        log("WARNING: Female Raid - Chest 1 coordinate is not set! "
            "Set it in Settings > Configs > Chest locations.")
    if tv("Female Raid", "open_chest2") and tv("Female Raid", "chest2") is None:
        log("WARNING: Female Raid - 'Open chest 2' is on but the Chest 2 coordinate is not set.")


def run_mission():
    state["manual_start"] = False   # a mission is running now: forget any leftover F8 press
    state["mission_t0"] = time.time()   # for the stuck-mission rescue (failsafe time)
    state["rescued"] = False
    state.pop("macro_loot", None)   # what trackloot() / addwin() / addlose() of a custom macro
    state.pop("macro_result", None)  # counted in this run (so the retry handling skips it)
    counters()[0]["missions"] += 1  # a mission - or a raid (Female Raid / custom type: raid)
    push_stats()
    ev.put(("mission_started",))
    set_status("running")

    # read once per mission: the movement is the same for every config,
    # the family (abilities) decides what happens from ability 3 on
    fam = settings["family"]

    if tv("Movement", "fix_shift_lock"):
        show("Fixing shift lock", "ctrl once")
        log("Fix shift lock: ctrl pressed once")
        fix_shift_lock(lambda: tick(scan_retry=False))

    if fam == "Female Raid":
        raid_routine()          # never returns: ends with RetryFound / stop

    if is_custom(fam):
        custom_routine()        # your own .ymir macro; never returns: ends with RetryFound / stop

    if tv("Movement", "look_up"):
        show("Looking up", "ctrl, mouse up, ctrl")
        log(f"Looking up ({tv('Movement', 'look_up_time'):g}s: ctrl, mouse up {tv('Movement', 'look_up_pixels')}px, ctrl)")
        look_up(lambda: tick(scan_retry=False), tv("Movement", "look_up_time"),
                tv("Movement", "look_up_pixels"))

    set_step(0)
    if tv(fam, "skip_ability2"):
        show("Ability 2", "Skipped (Skip ability 2 is on)")
        log("Ability 2 skipped")
    else:
        show("Ability 2", "Only ability 2 for now - no dashing yet")
        log("Ability 2")
        tap(KEY_ABILITY_1ST)
        wait(tv("Movement", "after_ability2"))  # nothing else happens until the dash sequence

    universal = fam == "Universal"   # ability 3, wait, look down, ability 4, shift
    blood = fam == "Blood Awakening"

    set_step(1)
    dash_time = tv("Movement", "dash_time")
    log(f"Movement: spamming dash for {dash_time:g}s")
    spam_recorded_dash(KEY_DASH, dash_time)

    set_step(2)
    if not blood:       # Blood Awakening goes from the dash straight into its strafing (no delay)
        show("Strafe", f"Double-tap {KEY_STRAFE.upper()}")
        double_tap(KEY_STRAFE)
        wait(tv(fam, "after_strafe"), scan=False)

    if blood:
        # Blood Awakening: no ability 3 / buying time / ability 4 - strafe left, then the ability 5 shift
        left_time = tv("Blood Awakening", "strafe_left_time")
        if left_time > 0:
            show("Strafe left", f"Strafing {KEY_STRAFE.upper()} for {left_time:g}s")
            log(f"Blood Awakening: strafing left ({KEY_STRAFE.upper()}) for {left_time:g}s")
            end = time.time() + left_time
            next_strafe = time.time()
            while True:                              # same strafing as Shiki's buying time
                now = time.time()
                remaining = end - now
                if remaining <= 0:
                    break
                detail(f"Strafing left... {remaining:.0f}s left")
                if now >= next_strafe:
                    double_tap(KEY_STRAFE)
                    next_strafe = time.time() + random.uniform(*tv("Blood Awakening", "strafe_gap"))
                wait(0.05, scan=False)
        log("Blood Awakening: going to the ability 5 shift")
    else:
        set_step(3)
        show("Ability 3")
        log("Ability 3")
        tap(KEY_ABILITY_2ND)  # no pause: buying time starts right away

        if universal:
            # Universal: ability 3 -> look down at once (ctrl, drag mouse down, ctrl) -> ability 4
            set_step(4)
            show("Looking down", "ctrl, mouse down, ctrl")
            log("Universal: looking down (ctrl, mouse down, ctrl)")
            look_down(lambda: tick(scan_retry=False),
                      tv("Universal", "look_time"), tv("Universal", "look_pixels"),
                      settle=0)      # ability 4 right when the look down ends, no delay
            set_step(5)
            u4_time = tv("Universal", "ability4_time")
            show("Ability 4", f"Spamming ability 4 for {u4_time:g}s")
            log(f"Ability 4 spam for {u4_time:g}s")
            spam_key_for(KEY_ABILITY_3RD, u4_time,
                         tv("Universal", "ability4_interval"), "ability 4")
            wait(tv("Universal", "after_ability"))
        else:
            set_step(4)
            show("Buying time", f"Strafing {KEY_STRAFE.upper()} until ability 4 is off cooldown")
            log(f"Buying time for {tv('Shiki', 'cooldown'):.0f}s (ability 4 cooldown)")
            icon_seen = buy_time_until_ability4_ready()

            set_step(5)
            if icon_seen:
                s4_time = tv("Shiki", "ability4_time")
                show("Ability 4", f"Icon seen - spamming ability 4 for {s4_time:g}s")
                log(f"Ability 4 spam for {s4_time:g}s (icon seen)")
                spam_key_for(KEY_ABILITY_3RD, s4_time,
                             tv("Shiki", "ability4_interval"), "ability 4")
            else:
                show("Ability 4")
                log("Ability 4")
                tap(KEY_ABILITY_3RD)
            wait(tv("Shiki", "after_ability"))

    shift_sequence(fam)


# --------------------------- macro thread -------------------------------
def _screen_scale():
    """real screen height / BASE_HEIGHT, worked out before the pictures are loaded (the user's own
    pictures have to be scaled to 1080p size when they are loaded)."""
    if FORCE_SCALE:
        return FORCE_SCALE
    try:
        with mss.mss() as tmp:
            return pick_monitor(tmp.monitors)["height"] / BASE_HEIGHT
    except Exception:  # noqa: BLE001
        return 1.0


def _log_overrides():
    """Tell the user which of their own pictures from asset_override are in use."""
    folder = ensure_override_dir()
    mine = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
                mine.append(os.path.relpath(os.path.join(root, f), folder).replace("\\", "/"))
    if mine:
        log(f"asset_override: using your own picture(s) instead of the built-in ones: "
            f"{', '.join(sorted(mine))}")


def macro_main():
    global _sct, _monitor, _scale, _retry_template, _start_template, _grab_template
    global _shiki_ability4_icon, _disconnect_template
    try:
        _scale = _screen_scale()     # the user's own pictures (asset_override) are scaled with it
        _log_overrides()
        _retry_template = load_quality_templates(TEMPLATE_FILE)
        load_alt_retry()
        _start_template = load_quality_templates(START_TEMPLATE_FILE)
        try:
            _shiki_ability4_icon = load_quality_templates(SHIKI_ABILITY4_FILE)
        except FileNotFoundError:
            _shiki_ability4_icon = None
            log(f"Shiki: {SHIKI_ABILITY4_FILE} not found - buying time uses the {tv('Shiki', 'cooldown'):.0f}s timer")
        _raid_templates.clear()
        for kind, filename in (("skip", RAID_SKIP_FILE), ("finish", RAID_FINISH_FILE),
                               ("heatwave", RAID_HEATWAVE_FILE), ("start", RAID_START_FILE)):
            try:
                _raid_templates[kind] = load_quality_templates(filename)
            except FileNotFoundError:
                pass            # a missing picture is not logged
        _disconnect_template = None
        _reconnect_images.clear()
        state["rc_next"] = 0.0
        state["rc_request"] = False
        state["rc_mods_pending"] = False
        state["rescue_t0"] = None
        if settings["reconnect"]["enabled"] and reconnect_missing():
            log("Reconnect is switched on but not complete for this macro (still needed: "
                + ", ".join(reconnect_missing()) + ") - it stays off")
        elif settings["reconnect"]["enabled"]:
            try:
                for kind, filename in RECONNECT_IMAGE_FILES.items():
                    _reconnect_images[kind] = load_quality_templates(filename)
                _disconnect_template = load_quality_templates(RECONNECT_IMAGE_FILE)
                log(f"Reconnect is ON ({reconnect_macro_tab()} coordinates): rejoining game "
                    f"{ROBLOX_GAME_ID} on a disconnect, a failsafe longer than "
                    f"{RECONNECT_RESCUE_LIMIT / 60:g} min, or !reconnect")
            except FileNotFoundError:
                _reconnect_images.clear()
                log("Reconnect: one of its pictures could not be loaded - it stays off")
        _grab_template = None
        if GRAB_ENABLED:
            try:
                _grab_template = red_mask(load_template(GRAB_FILE))
            except FileNotFoundError:
                pass            # a missing picture is not logged
        _result_templates.clear()
        for kind, filename in RESULT_FILES.items():
            try:
                _result_templates[kind] = gold_mask(load_template(filename))
            except FileNotFoundError:
                pass            # a missing picture is not logged
        _loot_templates.clear()
        if LOOT_ENABLED:
            for quality, folder in (("normal", ""), ("low", LOW_FOLDER)):
                _loot_templates[quality] = {}
                for kind, filename in LOOT_FILES.items():
                    try:
                        _loot_templates[quality][kind] = load_template(
                            os.path.join(folder, filename))
                    except FileNotFoundError:
                        if quality == "normal" or any(
                                os.path.exists(asset_path(os.path.join(LOW_FOLDER, f)))
                                for f in LOOT_FILES.values()):
                            tag = "" if quality == "normal" else " (low quality)"
                            pass            # a missing picture is not logged
        if LOOT_ENABLED and settings.get("fix_loot"):
            log("Fix loot tracking (Beta) is ON: loot icons are looked for at any size")
        log("Started. Click into Roblox; the routine starts when the mission begins.")
        warn_raid_config()

        # Initialise mss manually so _reinit_mss() can replace it while running.
        try:
            _sct = mss.mss()
            _monitor = pick_monitor(_sct.monitors)
            _scale = FORCE_SCALE if FORCE_SCALE else _monitor["height"] / BASE_HEIGHT
            if abs(_scale - 1.0) > 0.01:
                log(f"Screen is {_monitor['width']}x{_monitor['height']}: scaling the picture "
                    f"to 1080p size (x{1 / _scale:.2f}) so the images match")

            need_start = True
            after_retry = False
            while True:
                try:
                    try:
                        if need_start:
                            # right after a retry click: do nothing until the next
                            # mission starts (no retry scanning, no clicks, no keys)
                            wait_for_mission_start(scan_retry=not after_retry)
                            need_start = False
                            after_retry = False
                        run_mission()
                    except RetryFound as r:
                        mission_t0 = state["mission_t0"]
                        state["mission_t0"] = None     # the mission is over (no rescue needed)
                        mission_len = time.time() - mission_t0 if mission_t0 else None
                        was_rescued, state["rescued"] = state["rescued"], False
                        cx, cy, score, tw, th = r.match
                        # cx, cy are 1080p-picture positions: back to real screen pixels
                        abs_x = _monitor["left"] + int(cx * _scale)
                        abs_y = _monitor["top"] + int(cy * _scale)
                        ev.put(("mission_ended",))
                        set_status("retrying")
                        raid_mode = settings["family"] == "Female Raid"
                        retry_title = "Clicking retry" if raid_mode else "Retry found"
                        set_step(5 if raid_mode else -1)
                        log(f"Retry found ({r.quality} quality, score {score:.2f})")
                        if was_rescued:
                            log("Stuck mission is over - player eliminated")
                            notify_rescue_async(True, counters()[0]["missions"],
                                                time.time() - mission_t0 if mission_t0 else None)
                        low = r.quality == "low"
                        q = r.quality
                        loot_found, result = {}, None
                        # a custom macro that called trackloot() / addwin() / addlose() has counted that
                        # itself: it is not counted a second time here
                        tracked = state.pop("macro_loot", None)
                        macro_result = state.pop("macro_result", None)
                        macro_result_tuple = (macro_result, None) if macro_result else None
                        loot_on = bool(_loot_templates.get(q)) and tracked is None   # icons of THIS quality
                        # fast mode: click first, read result + loot afterwards from the screenshot
                        # taken when the button was found
                        fast = bool(tv(settings["family"], "fast_mode")) and r.screen is not None
                        # normal quality: result + loot are read before the click.
                        # low quality: only the result now, the loot after the click (see below)
                        if not fast and (_result_templates or (loot_on and not low)):
                            show(retry_title, "Checking the result and loot..."
                                 if not low else "Checking the result...")
                            loot_found, result = scan_end_screen(
                                q, want_loot=loot_on and not low, want_result=True)
                            if _result_templates:
                                record_result(result, counted=bool(macro_result))
                            if loot_on and not low:
                                record_loot(loot_found)
                        if not low and not fast:
                            notify_mission_async(result or macro_result_tuple, loot_found or tracked or {}, mission_len)   # Discord stats embed (if on)
                        if state.pop("rc_mods_pending", False) and modifiers_enabled():
                            # first retry window after a reconnect: set the modifiers, then retry as usual
                            show(retry_title, "Adding modifiers...")
                            log("Adding modifiers (first retry window after the reconnect)")
                            run_modifier_sequence(say=lambda t: log(t))
                        show(retry_title, "Clicking...")
                        log("Clicking retry")
                        save_click_debug(cx, cy, tw, th)
                        click_at(abs_x, abs_y, int(tw * _scale), int(th * _scale))
                        clicked_at = time.time()
                        counters()[0]["retries"] += 1
                        push_stats()
                        if fast:
                            if _result_templates or (loot_on and not low):
                                show("Retry clicked", "Fast mode: reading the result and loot")
                                loot_found, result = scan_end_screen(
                                    q, want_loot=loot_on and not low, want_result=True, frame=r.screen)
                                if _result_templates:
                                    record_result(result, counted=bool(macro_result))
                                if loot_on and not low:
                                    record_loot(loot_found)
                            if not low:
                                notify_mission_async(result or macro_result_tuple, loot_found or tracked or {}, mission_len)
                        if low:
                            if loot_on:
                                show("Checking loot", f"Low quality: looking for the loot for "
                                                      f"{LOW_LOOT_SCAN_TIME:g}s after the click")
                                log(f"Low quality: checking the loot for {LOW_LOOT_SCAN_TIME:g}s "
                                    "after the retry click")
                                loot_found, _ = scan_end_screen(
                                    q, want_loot=True, want_result=False,
                                    scan_time=LOW_LOOT_SCAN_TIME)
                                record_loot(loot_found)
                            notify_mission_async(result or macro_result_tuple, loot_found or tracked or {}, mission_len)
                        # the loot scan above counts as part of the hands-off time
                        left = max(0.0, AFTER_CLICK_DELAY - (time.time() - clicked_at))
                        log(f"Hands off for {left:.0f}s more, then waiting for the next mission")
                        hands_off_wait(left)
                        wait_for_icon_gone()
                        after_retry = True
                        need_start = True
                except Disconnected as dc:
                    reconnect_rejoin(dc.reason)
                    need_start = True
                    after_retry = False
        finally:
            # Clean up the mss instance (whether we're stopping or _reinit_mss() replaced it):
            # close whatever is current now.
            with _sct_lock:
                try:
                    if _sct is not None:
                        _sct.close()
                except Exception:  # noqa: BLE001
                    pass
    except SystemExit:
        log("Stopped")
    except Exception as e:  # noqa: BLE001 - show any error in the window
        log(f"Error: {type(e).__name__}: {e}")
        if settings["webhook"]["notify_error"]:
            notify_async(f"ymir stopped because of an error: {type(e).__name__}: {e}")
    finally:
        state["mission_t0"] = None
        release_all_keys()
        ev.put(("done",))


# ------------------------------- window ---------------------------------
def fmt_time(sec):
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


GITHUB_URL = "https://github.com/SvdXenos/Ymir"
DISCORD_URL = "https://discord.gg/dFfFXEtRr9"     # Settings footer > Discord (invite link)
PATREON_URL = "https://www.patreon.com/"          # Settings footer > Patreon  <-- put your Patreon page here
# 64x64 alpha mask of the GitHub mark; it is tinted + resized at runtime (any theme colour, any DPI)
GITHUB_MASK_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAAAAACPAi4CAAADt0lEQVR42pVXW2yMQRT+dnbpUvRCU3Vp1a0Xl7RItEE9IAgSkVQkLiVUCNEiooQEaUKppCX60gfx5E5F4vZC0jTufRGqoi5NpFRbLVva0j0edv+d88/+u//seZoz53zfP//MnMs4EEJScrPTUhNj3OjpbP3QWP+kGRGIyDvTRIq8r5zn0IQPL/lIlvKhZLgOvNxDIcVzMt4G7ir+QWGlY5czHH7Kc7KVpxmh8Vt+k4Z0bwq1/CrSlLOWvzHoNmnLLbcF/gFFIPeDGFy3KSKpUf+iiiKUSmX/KWLZwPGZgfNbNmPthZ+hML8vFWRndRu3Mo1tQOD+vAaAocUtVvAfh+MB4KahPxYA4AKAnbMMqocA8KuiumRfFLoamr62ERCblJo5At7qQ20AgEcr/b45288Z8SPv/3aDKaMwjUdv8rq5xnBBwLndiKxTcpmr7MN1mvQ+7l+AJyKCKdL7ZxwggK3R0ppgT5Aoh0O3AAKikFmn2xNwl60OAHn8qF7ZE1zj/rkAzjDdu1xjBd0McBoAz79VOkm3iAHeAuOY2qWTdeFqZJDRYjYzXWjXIfjHAzFXzGDaRb3CcaVfjrNFulQ8z/QI2thZpYsUqTT0a9au13I4ToyUyjfd2tkihyNFjFT6dAmYY4yIkspAXQLmOEiw+VhdAuZIolcqSboEo+WwR3SypiRKk2CyHHYKtvWuLM0OZDw7EPGZWRbqESxg2fKTaGCW1XoE+WzcgDU8P8zTwSf3EU+iKZygVqcRO88Ro4D3XN9pj1/iZf5vAFRygt5FdvipHdy/HMB8c/3MD4+f02pyzwEgzG2ltzpMXosu/WtyfusAgANERNR45FitPzOWTbSGjzr4VSnYe1hpuyWAxcb6XpatTBUm8NilR+v+qQW/y58LThIR0b1kIKNdWutYZDhuWDYcpcbd9m3slxRgRcDYN5kvIMGqA/4eCOtd/ibWxW7JZfP/l1sQbAtYnU99M7sB9x2/db2ZIC8YX8d2Kd1X7zqGAc6iZiLyXFWyS2xw62/6x42+ySIAcEyamTEg6Ax7VQJlib4a3RK6wehW8BWK3VlDRETPQuZF5SVwI6hhd9/zdV6HJgAABquB/ceEv2uRPt1GB/m9/kX9F+9gxdzD8dct06+zgvtEh9nEilDvpg2s4RuiljP55CkIk/DrAm7DFFMgjp+khX2zbmvz+6l1zh+K7TucdnWvtIuIiNS00k9E9OtEnEbWjdn7jqhZ/dAboqb9cZq1z5G7eYw6l7R5vu7jOzL5D54ffEhC1T+eAAAAAElFTkSuQmCC"
)


DISCORD_MASK_B64 = (     # 64x64 alpha mask of the Discord mark (same use as the GitHub one)
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAAAAACPAi4CAAADKUlEQVRYCaXBf2iUdRzA8ffnca5Mc3rZEVuxCik2c3Yz7YcQ5sjZls2MsCEUShmSsfAH6JwLF2tROiSJ3NZqMBhLZH/UUqozzITM0igXcYxWWxbr0DvB9sNt19PdPc+z+z43Cu/5vl6CJkGToEnQJGgSNAmaBE3CFLPK1pTMrGrFrbTDONnVfYl0gpv/yTUrsoGxB75HldszFxg/c6T7FxOVoCgsf2qJgeX33WdGcWQtqluA7XzXJ9+SIjiMzS8VcG0GmhtHsAmOt1/m2n20NoZFsK08ZpCBF5uxCJbrfryLTFwqDJMkWPbUkZm2DSQJSQXnricz/5QGSRCSgiVkKlQ0RpyQUNlB5mrqiRPifD/7ydxIIAQIcU2b8CL4KCDAg19Nw5PKThDI/roYb/4oiiCw4028OrQZ4caBOXg1cXefsK4T76obhL21ePfhM8KBKtKEPg/fXnYLbpePhnKWF5PmWJlw+Glchra0x2BGdbWBonVHFOSxD/y4nL1P+PhxVFdXnSBpy0FSGrebJBSc8qH69U7hu8Wo6l7F1l2O43zxBJbn2lBFfULvfBSjuVFsy07heLYdW1bfbShMnxCZi+KLEhxZgzdhieWGcbQ8jypfciIGipZNTDpbjCXij+HY2YBqmeRdQPXeC0w6F8AS8cdw7Hod1Xq5/zSqE4/gmD7owxK7dRBH60ZU9bKuE9VoXgTbw1/i2NCGLeu3PFS9srcWl/oaLHJ0FY6fAuNYNrbiJj0LcBkrD5K0dT8pB6tMEhaenIObmKQZeeX9CZhVu81A0b71IhgVzfNIIyZT9AXDd6y8Gbcrn4VmL7+HKcREj5joERM9YqJHTPSIiR4ZnoEWuaFi10K8Gmg8ImBU1N6LF3+9dWgIIU5W715KSl/vEh9TDX0zc6kw6cK+lmFASJLSNxZh66ocmx4oLQnMJmW45/inp6+yswHbxdeaR0kQbMbaPUUkDBZGScr1zcvLzp/WP95/OdqP5fgKEsL73v0bizDJeKJmMbC+g/+W/0MO/Lm/aQiHoJCybQ+9s93kf6xuu9J0YIQUQZOgSdAkaBI0CZoETYImQdO/36PgccbCGAwAAAAASUVORK5CYII="
)


_icon_cache = {}      # (kind, ...) -> tk.PhotoImage, kept alive here


def tinted_icon(color_hex, px, mask_b64=None):
    """The GitHub mark (or the mask given) in one colour, px x px, as a tk.PhotoImage (None if it
    can't be made). Cached: a theme change rebuilds the windows, the icons stay the same."""
    key = ("tint", color_hex, px, mask_b64)
    if key in _icon_cache:
        return _icon_cache[key]
    try:
        raw = base64.b64decode(mask_b64 or GITHUB_MASK_B64)
        mask = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
        mask = cv2.resize(mask, (px, px), interpolation=cv2.INTER_AREA)
        r, g, b = (int(color_hex[i:i + 2], 16) for i in (1, 3, 5))
        img = np.zeros((px, px, 4), np.uint8)
        img[..., 0], img[..., 1], img[..., 2], img[..., 3] = b, g, r, mask
        ok, buf = cv2.imencode(".png", img)
        photo = tk.PhotoImage(data=base64.b64encode(buf.tobytes())) if ok else None
        if photo is not None:
            _icon_cache[key] = photo
        return photo
    except Exception:
        return None


def book_icon(color_hex, px):
    """An open book in one colour, px x px, as a tk.PhotoImage (None if it can't be made). Drawn
    here because the book emoji doesn't render in tkinter labels."""
    key = ("book", color_hex, px)
    if key in _icon_cache:
        return _icon_cache[key]
    try:
        s = 8                                   # draw big, shrink = smooth edges
        n = px * s
        mask = np.zeros((n, n), np.uint8)
        u = n / 16.0
        def pt(x, y):
            return [int(x * u), int(y * u)]
        # two pages (slightly curved tops meeting at the spine) ...
        left = np.array([pt(1, 3.2), pt(7.5, 4.4), pt(7.5, 13.6), pt(1, 12.4)], np.int32)
        right = np.array([pt(15, 3.2), pt(8.5, 4.4), pt(8.5, 13.6), pt(15, 12.4)], np.int32)
        cv2.fillPoly(mask, [left, right], 255)
        # ... and a few text lines cut out of the pages
        for y in (6.6, 8.6, 10.6):
            cv2.line(mask, tuple(pt(2.6, y)), tuple(pt(6.2, y + 0.5)), 0, max(1, int(0.7 * u)))
            cv2.line(mask, tuple(pt(9.8, y + 0.5)), tuple(pt(13.4, y)), 0, max(1, int(0.7 * u)))
        mask = cv2.resize(mask, (px, px), interpolation=cv2.INTER_AREA)
        r, g, b = (int(color_hex[i:i + 2], 16) for i in (1, 3, 5))
        img = np.zeros((px, px, 4), np.uint8)
        img[..., 0], img[..., 1], img[..., 2], img[..., 3] = b, g, r, mask
        ok, buf = cv2.imencode(".png", img)
        photo = tk.PhotoImage(data=base64.b64encode(buf.tobytes())) if ok else None
        if photo is not None:
            _icon_cache[key] = photo
        return photo
    except Exception:
        return None


_icon_warned = set()


def emoji_icon(filename, px):
    """A Discord emoji PNG from the image folder, px x px, as a tk.PhotoImage (None if missing).
    The mini bar and the main window's loot counters both use this, so they show the same pictures.
    Cached per file (and its change time), so a theme change does not read / convert them again."""
    try:
        path = asset_path(filename)
        key = ("emoji", path, px, os.path.getmtime(path))
        if key in _icon_cache:
            return _icon_cache[key]
        img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_UNCHANGED)
        img = cv2.resize(img, (px, px), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", img)
        photo = tk.PhotoImage(data=base64.b64encode(buf.tobytes())) if ok else None
        if photo is not None:
            _icon_cache[key] = photo
        return photo
    except Exception:
        if filename not in _icon_warned:        # tell once, so a missing picture is easy to find
            _icon_warned.add(filename)
            log(f"Icon not loaded: {filename} (looked in {asset_path(filename)})")
        return None


def rounded_rect(c, x1, y1, x2, y2, r, **kw):
    """Draw a rounded rectangle on a canvas (a plain rectangle when the corners are ~square)."""
    if r < 2.5:
        return c.create_rectangle(x1, y1, x2, y2, **kw)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
           x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


class ActionTile(tk.Canvas):
    """
    Control tile: icon top-left, hotkey badge top-right, label bottom-left.

    States:  normal  -> clickable
             active  -> blue border + tinted background (the current mode)
             disabled-> dimmed, not clickable
    """
    # (fill, outline, outline_width, label_fg, badge_outline, badge_fg)
    LOOK = {
        "normal":   ("#1a1e29", "#2a3042", 1, "#e6e9f2", "#3a4156", "#9aa3b8"),
        "hover":    ("#212637", "#3a4156", 1, "#ffffff", "#4a5270", "#b4bcd0"),
        "active":   ("#16254a", "#3b6fe0", 2, "#ffffff", "#3b5cb0", "#9db8ff"),
        "disabled": ("#13161e", "#1b202c", 1, "#4b5264", "#232837", "#4b5264"),
    }
    OFF_ICON = "#3c4254"

    def __init__(self, parent, label, key_text, icon, icon_color, on_click,
                 parent_bg, height=66):
        super().__init__(parent, width=120, height=height, bg=parent_bg,
                         highlightthickness=0, bd=0)
        self.th = height
        self.label_text = label
        self.key_text = key_text
        self.icon = icon
        self.icon_color = icon_color
        self.on_click = on_click
        self.enabled = True
        self.active = False
        self.hovering = False
        self.pressed = False
        self.bind("<Configure>", lambda _e: self.draw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    # ----- drawing -----
    def draw(self):
        self.delete("all")
        w, h = max(self.winfo_width(), 60), self.th
        if not self.enabled:
            look = self.LOOK["disabled"]
        elif self.active:
            look = self.LOOK["active"]
        elif self.hovering:
            look = self.LOOK["hover"]
        else:
            look = self.LOOK["normal"]
        fill, outline, ow, label_fg, badge_line, badge_fg = look

        inset = ow / 2 + 0.5
        rounded_rect(self, inset, inset, w - inset, h - inset, App.R_TILE,
                     fill=fill, outline=outline, width=ow)

        self._draw_icon(24, 24, self.icon_color if self.enabled else self.OFF_ICON)

        # hotkey badge
        bx2, by1, bx2_h = w - 12, 12, 18
        bx1 = bx2 - max(30, 8 * len(self.key_text) + 14)
        rounded_rect(self, bx1, by1, bx2, by1 + bx2_h, min(5, App.R_TILE),
                     fill="", outline=badge_line, width=1)
        self.create_text((bx1 + bx2) / 2, by1 + bx2_h / 2, text=self.key_text,
                         fill=badge_fg, font=(App.FONT, 8, "bold"))

        self.create_text(16, h - 15, text=self.label_text, fill=label_fg,
                         anchor="w", font=(App.FONT, 10, "bold"))

    def _draw_icon(self, cx, cy, color):
        if self.icon == "play":
            self.create_polygon(cx - 5, cy - 8, cx - 5, cy + 8, cx + 8, cy,
                                fill=color, outline=color, width=2, joinstyle="round")
        elif self.icon == "pause":
            for dx in (-6, 2):
                rounded_rect(self, cx + dx, cy - 8, cx + dx + 5, cy + 8, 1.5,
                             fill=color, outline="")
        elif self.icon == "stop":
            rounded_rect(self, cx - 7, cy - 7, cx + 7, cy + 7, min(3, App.R_TILE),
                         fill=color, outline="")

    # ----- state -----
    def update_tile(self, label=None, enabled=None, active=None, key_text=None):
        if label is not None:
            self.label_text = label
        if key_text is not None:
            self.key_text = key_text
        if enabled is not None:
            self.enabled = enabled
            self.configure(cursor="hand2" if enabled else "arrow")
        if active is not None:
            self.active = active
        self.draw()

    # ----- mouse -----
    def _on_enter(self, _e):
        self.hovering = True
        self.draw()

    def _on_leave(self, _e):
        self.hovering = False
        self.pressed = False
        self.draw()

    def _on_press(self, _e):
        self.pressed = True

    def _on_release(self, e):
        was_pressed, self.pressed = self.pressed, False
        if (was_pressed and self.enabled and self.on_click
                and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.th):
            self.on_click()


class Pill(tk.Canvas):
    """Small rounded status badge with a coloured dot."""

    def __init__(self, parent, parent_bg, width=94, height=26):
        super().__init__(parent, width=width, height=height, bg=parent_bg,
                         highlightthickness=0, bd=0)
        self.pw, self.ph = width, height

    def update_pill(self, text, bg, fg):
        self.delete("all")
        rounded_rect(self, 1, 1, self.pw - 1, self.ph - 1,
                     self.ph // 2 if App.R_PILL is None else App.R_PILL,
                     fill=bg, outline="")
        r = 3.5
        self.create_oval(14 - r, self.ph / 2 - r, 14 + r, self.ph / 2 + r,
                         fill=fg, outline="")
        self.create_text(26, self.ph / 2, text=text, fill=fg, anchor="w",
                         font=(App.FONT, 9, "bold"))


class ToggleSwitch(tk.Canvas):
    """Modern on/off switch bound to a BooleanVar (replaces the old tick boxes)."""
    ON, OFF, KNOB = "#3b82f6", "#272e40", "#ffffff"

    def __init__(self, parent, variable, command=None, parent_bg=None):
        super().__init__(parent, width=38, height=22, bg=parent_bg or App.CARD,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.var = variable
        self.command = command
        self.locked = False
        self.var.trace_add("write", lambda *_: self.draw())
        self.bind("<Button-1>", self.flip)
        self.draw()

    def lock(self):
        """Greyed out: shown off, does not react to clicks."""
        self.locked = True
        self.configure(cursor="arrow")
        self.draw()

    def flip(self, _e=None):
        if self.locked:
            return
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def draw(self):
        try:
            self.delete("all")
            if self.locked:
                rounded_rect(self, 1, 1, 37, 21, App.R_SWITCH, fill=App.DIS_FILL,
                             outline=App.DIS_EDGE)
                rounded_rect(self, 10 - 7, 11 - 7, 10 + 7, 11 + 7, max(0, App.R_SWITCH - 3),
                             fill=App.DIS_ICON, outline="")
                return
            on = bool(self.var.get())
            rounded_rect(self, 1, 1, 37, 21, App.R_SWITCH, fill=self.ON if on else self.OFF,
                         outline="")
            x = 28 if on else 10
            rounded_rect(self, x - 7, 11 - 7, x + 7, 11 + 7, max(0, App.R_SWITCH - 3),
                         fill=self.KNOB, outline="")
        except tk.TclError:
            pass    # the window was closed


def switch_row(parent, text, var, command, bg, fg, sub_font=None, pady=4, locked=False):
    """Label on the left, switch on the right. Clicking the label flips it too.
    locked=True: greyed out and off (cannot be switched)."""
    row = tk.Frame(parent, bg=bg)
    row.pack(fill="x", pady=pady)
    sw = ToggleSwitch(row, var, command, parent_bg=bg)
    sw.pack(side="right")
    if locked:
        var.set(False)
        sw.lock()
        tk.Label(row, text=text, font=sub_font or (App.FONT, 10), fg=App.DIS_TEXT, bg=bg,
                 anchor="w").pack(side="left", fill="x", expand=True)
        return row
    lbl = tk.Label(row, text=text, font=sub_font or (App.FONT, 10), fg=fg, bg=bg, anchor="w",
                   cursor="hand2")
    lbl.pack(side="left", fill="x", expand=True)
    lbl.bind("<Button-1>", sw.flip)
    return row


class Stepper(tk.Canvas):
    """A row of small segments showing which mission step is running."""

    def __init__(self, parent, n_segments, parent_bg, colors):
        super().__init__(parent, height=6, bg=parent_bg, highlightthickness=0, bd=0)
        self.n_segments = n_segments
        self.current = -1
        self.seg_colors = colors  # (done, current, pending)
        self.bind("<Configure>", lambda _e: self.draw())

    def set_index(self, idx):
        self.current = idx
        self.draw()

    def set_count(self, n):
        self.n_segments = n
        self.draw()

    def draw(self):
        self.delete("all")
        width = max(int(self.winfo_width()), 20)
        gap = 5
        seg = (width - gap * (self.n_segments - 1)) / self.n_segments
        done, cur, pending = self.seg_colors
        for i in range(self.n_segments):
            x1 = i * (seg + gap)
            if self.current < 0 or i > self.current:
                col = pending
            elif i < self.current:
                col = done
            else:
                col = cur
            rounded_rect(self, x1, 0, x1 + seg, 6, App.R_SEG, fill=col, outline="")


def prepare_window(win):
    """First line of a window's __init__: invisible AND hidden while it is being built, so nothing
    of it can show (the default white window background, a half-built layout, the light title
    bar) before it is finished."""
    try:
        win.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    win.withdraw()


def _ease_out(t):
    return 1 - (1 - t) ** 3


def animations_on():
    """Settings > UI > GUI animations (off by default)."""
    return bool(settings.get("animations"))


def mix_color(c1, c2, t):
    """Colour t (0..1) of the way from c1 to c2 (both '#rrggbb')."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _win_pos(win):
    """(x, y) of the window's top-left corner as wm geometry reports it, or None."""
    m = re.search(r"([+-]-?\d+)([+-]-?\d+)$", win.geometry())
    if not m:
        return None
    return int(m.group(1).replace("+-", "-")), int(m.group(2).replace("+-", "-"))


def reveal_window(win, steps=9, delay=14, rise=14):
    """Show a window that is completely built. It is mapped while still fully transparent, Tk lays
    it out and paints it (all the <Configure> re-wrapping happens now, unseen), and only then it
    fades in - so it appears finished instead of flashing white / jumping into place.
    Sub windows also glide up `rise` pixels while they fade in (the main window only fades)."""
    try:
        win.attributes("-alpha", 0.0)
        win.deiconify()
        win.update()
    except tk.TclError:
        return
    if win.__dict__.get("_tb_bound"):
        retitle_window(win)              # the frame exists now: dark title bar before it fades in
    anim = animations_on()
    if not anim:                         # animations off: the plain quick fade, no gliding
        steps, delay, rise = 6, 12, 0
    pos = _win_pos(win) if isinstance(win, tk.Toplevel) and rise else None

    def step(i=1):
        try:
            if not win.winfo_exists():
                return
            k = _ease_out(min(1.0, i / steps)) if anim else min(1.0, i / steps)
            win.attributes("-alpha", k)
            if pos:
                win.geometry(f"+{pos[0]}+{pos[1] + round(rise * (1 - k))}")
        except tk.TclError:
            return
        if i < steps:
            win.after(delay, step, i + 1)
    step()


def fade_close(win, steps=5, delay=12):
    """Fade a window out, then destroy it (use instead of win.destroy() for a close button)."""
    if getattr(win, "_closing", False):
        return
    win._closing = True
    if not animations_on():              # animations off: close right away
        try:
            win.destroy()
        except tk.TclError:
            pass
        return

    def step(i=1):
        try:
            if not win.winfo_exists():
                return
            win.attributes("-alpha", max(0.0, 1 - i / steps))
        except tk.TclError:
            return
        if i < steps:
            win.after(delay, step, i + 1)
        else:
            try:
                win.destroy()
            except tk.TclError:
                pass
    step()


# ---- "Less windows": sub windows replace the one that opened them ---------------------------
_nav = {"stack": [], "app": None}      # [(window, label)]: settings > reconnect > images ...


def less_windows_on():
    """Settings > UI > Less windows (on by default)."""
    return bool(settings.get("less_windows", True))


def _nav_alive(win):
    try:
        return bool(win.winfo_exists())
    except tk.TclError:
        return False


def _nav_request_close(win):
    """Close a window the way its own X button does (so unsaved-changes prompts etc. still run)."""
    try:
        win._nav_xy = _win_pos(win) or getattr(win, "_nav_xy", None)
        cmd = win.protocol("WM_DELETE_WINDOW")
        if cmd:
            win.tk.call(cmd)
        else:
            fade_close(win)
    except tk.TclError:
        pass


def nav_back(win):
    _nav_request_close(win)


def nav_jump(win, idx):
    """Breadcrumb click: go back up to level idx, closing everything above it."""
    st = _nav["stack"]
    above = [w for w, _l in st[idx + 1:]]
    for w in above[:-1]:
        w._nav_quiet = True                 # these are closed too: do not flash them on the way
    for w in reversed(above):
        _nav_request_close(w)
    app = _nav.get("app")
    if app is not None:
        def clear():
            for w in above:
                if _nav_alive(w):
                    w._nav_quiet = False
        app.root.after(700, clear)


def _nav_pop(win):
    """A sub window is gone: show the one underneath again, where this one was."""
    st = _nav["stack"]
    idx = next((i for i, e in enumerate(st) if e[0] is win), -1)
    if idx < 0:
        return
    was_top = idx == len(st) - 1
    st.pop(idx)
    if not was_top or not st:
        return
    parent = st[-1][0]
    if getattr(parent, "_nav_quiet", False) or not _nav_alive(parent):
        return
    xy = getattr(win, "_nav_xy", None)
    try:
        if xy:
            parent.geometry(f"+{xy[0]}+{xy[1]}")
        parent.deiconify()
        retitle_window(parent)
        parent.attributes("-topmost", True)
        parent.lift()
        parent.focus_force()
    except tk.TclError:
        pass
    app = _nav.get("app")
    if app is not None:
        app.keep_settings_on_top()


class NavBackButton(tk.Canvas):
    """The big rounded "< Back" button: accent coloured, the arrow slides left on hover."""
    H = 34

    def __init__(self, parent, command):
        self.font = tkfont.Font(family=App.FONT, size=10, weight="bold")
        self.tw = self.font.measure("Back")
        self.w = 18 + 14 + 8 + self.tw + 18
        super().__init__(parent, width=self.w, height=self.H, bg=App.BG, highlightthickness=0,
                         bd=0, cursor="hand2")
        self.command = command
        self.state = "normal"
        self.draw()
        self.bind("<Enter>", lambda _e: self.set_state("hover"))
        self.bind("<Leave>", lambda _e: self.set_state("normal"))
        self.bind("<ButtonPress-1>", lambda _e: self.set_state("down"))
        self.bind("<ButtonRelease-1>", self.on_release)

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self.draw()

    def on_release(self, e):
        inside = 0 <= e.x <= self.w and 0 <= e.y <= self.H
        self.set_state("hover" if inside else "normal")
        if inside:
            self.command()

    def draw(self):
        self.delete("all")
        look = {"normal": (App.ACCENT_BG, App.ACCENT_LINE, App.ACCENT_TEXT),
                "hover": (App.ACCENT_BG_HOVER, App.ACCENT, App.TEXT),
                "down": (App.ACCENT, App.ACCENT, "#ffffff")}[self.state]
        fill, line, fg = look
        rounded_rect(self, 1, 1, self.w - 1, self.H - 1,
                     min(App.R_TILE, self.H // 2) if App.R_TILE else 8,
                     fill=fill, outline=line, width=1)
        cy = self.H / 2
        ax = 17 - (3 if self.state != "normal" else 0)          # the arrow glides left on hover
        self.create_line(ax + 7, cy - 6, ax, cy, ax + 7, cy + 6, fill=fg, width=2.4,
                         capstyle="round", joinstyle="round")
        self.create_line(ax + 1, cy, ax + 14, cy, fill=fg, width=2.4, capstyle="round")
        self.create_text(18 + 14 + 8, cy, text="Back", anchor="w", fill=fg, font=self.font)


class NavCrumbs(tk.Canvas):
    """The path next to the Back button: settings > reconnect > images. Earlier steps are
    clickable (they jump back there), the current one is bold. Long paths shorten with an ellipsis."""
    H = 34

    def __init__(self, parent, labels, on_jump):
        super().__init__(parent, height=self.H, bg=App.BG, highlightthickness=0, bd=0)
        self.labels, self.on_jump = labels, on_jump
        self.norm = tkfont.Font(family=App.FONT, size=10)
        self.bold = tkfont.Font(family=App.FONT, size=10, weight="bold")
        self.under = tkfont.Font(family=App.FONT, size=10, underline=True)
        self.bind("<Configure>", lambda e: self.draw(e.width))

    def draw(self, width):
        self.delete("all")
        rounded_rect(self, 1, 1, width - 1, self.H - 1, min(App.R_TILE, 12) if App.R_TILE else 8,
                     fill=App.CARD, outline=App.BORDER, width=1)
        n = len(self.labels)
        shown = list(range(n))
        pad, sep = 14, 22

        def total(idx):
            t = 2 * pad
            for k, i in enumerate(idx):
                t += (self.bold if i == n - 1 else self.norm).measure(
                    "\u2026" if i < 0 else self.labels[i]) + (sep if k else 0)
            return t
        while total(shown) > width and len([i for i in shown if i >= 0]) > 2:
            first_real = next(k for k, i in enumerate(shown) if i > 0)
            shown[first_real] = -1                      # becomes the "..." step
            while len(shown) > first_real + 1 and shown[first_real + 1] == -1:
                shown.pop(first_real + 1)
            if total(shown) <= width:
                break
            real = [k for k, i in enumerate(shown) if i > 0 and i < n - 1]
            if not real:
                break
        x, cy = pad, self.H / 2
        for k, i in enumerate(shown):
            if k:
                self.create_text(x + sep / 2, cy, text="\u203a", fill=App.SUB, font=self.bold)
                x += sep
            last = i == n - 1
            text = "\u2026" if i < 0 else self.labels[i]
            fnt = self.bold if last else self.norm
            if last:
                self.create_oval(x - 1, cy - 3, x + 5, cy + 3, fill=App.ACCENT, outline="")
                x += 11
            tid = self.create_text(x, cy, text=text, anchor="w", fill=App.TEXT if last else App.SUB,
                                   font=fnt)
            x += fnt.measure(text)
            if not last and i >= 0:
                self.tag_bind(tid, "<Enter>", lambda _e, t=tid: (
                    self.itemconfigure(t, fill=App.TEXT, font=self.under),
                    self.configure(cursor="hand2")))
                self.tag_bind(tid, "<Leave>", lambda _e, t=tid: (
                    self.itemconfigure(t, fill=App.SUB, font=self.norm),
                    self.configure(cursor="")))
                self.tag_bind(tid, "<Button-1>", lambda _e, j=i: self.on_jump(j))


def nav_attach(child, label, app):
    """Call at the end of a sub window's __init__, right before reveal_window(). With Less windows
    on: the window that opened it is hidden, this one takes its place and gets a
    "< Back   settings / reconnect / images" bar. Off: nothing happens (the old way)."""
    if not less_windows_on():
        return
    st = _nav["stack"]
    st[:] = [e for e in st if _nav_alive(e[0])]
    sw = app.settings_win
    if not st:
        if sw is None or not _nav_alive(sw) or child is sw:
            return                          # not opened from Settings (e.g. changelog from the main window)
        st.append((sw, "settings"))
    _nav["app"] = app
    parent = st[-1][0]
    pos = _win_pos(parent)
    st.append((child, label))
    child._nav_xy = pos
    try:
        child.transient(app.root)           # a hidden owner would hide a transient window with it
        if pos:
            x = pos[0]
            y = max(0, min(pos[1], child.winfo_screenheight() - 160))
            child.geometry(f"+{x}+{y}")
    except tk.TclError:
        pass

    head = tk.Frame(child, bg=App.BG)
    slaves = child.pack_slaves()
    if slaves:
        head.pack(side="top", fill="x", before=slaves[0])
    else:
        head.pack(side="top", fill="x")
    row = tk.Frame(head, bg=App.BG)
    row.pack(fill="x", padx=18, pady=(12, 10))
    NavBackButton(row, lambda: nav_back(child)).pack(side="left")
    crumbs = NavCrumbs(row, [lab for _w, lab in st],
                       lambda k: nav_jump(child, k))
    crumbs.pack(side="left", fill="x", expand=True, padx=(10, 0))
    tk.Frame(head, bg=App.BORDER, height=1).pack(fill="x", padx=18)   # thin divider under the bar
    child.bind("<Alt-Left>", lambda _e: nav_back(child), add="+")

    def moved(e):
        if e.widget is child:
            child._nav_xy = _win_pos(child) or child._nav_xy
    child.bind("<Configure>", moved, add="+")
    child.bind("<Destroy>", lambda e: _nav_pop(child) if e.widget is child else None, add="+")
    try:
        parent.withdraw()
    except tk.TclError:
        pass


# ---- collapsing a window to its title bar ------------------------------------------------
COLLAPSED_H = 46          # height of the strip that stays visible when a window is collapsed


def _animate_height(win, h0, h1, done=None, steps=10, delay=14):
    """Smoothly change the window height (width and place stay) from h0 to h1."""
    try:
        w = win.winfo_width()
        if not animations_on():          # animations off: jump straight to the end size
            win.geometry(f"{w}x{max(1, h1)}")
            win.update_idletasks()
            if done:
                done()
            return
    except tk.TclError:
        return

    def step(i=1):
        try:
            if not win.winfo_exists():
                return
            k = _ease_out(min(1.0, i / steps))
            win.geometry(f"{w}x{max(1, round(h0 + (h1 - h0) * k))}")
        except tk.TclError:
            return
        if i < steps:
            win.after(delay, step, i + 1)
        elif done:
            done()
    step()


def toggle_collapse(win):
    """Minimize <-> restore: the window rolls up to a slim bar under its title bar (with the
    title in big text) and rolls back down to the size it had. Never raises: if anything goes
    wrong the window is simply left as it is."""
    try:
        if not win.winfo_exists():
            return
        st = win.__dict__.setdefault("_col", {"on": False, "busy": False})
        if st["busy"]:
            return
        st["busy"] = True
        if not st["on"]:
            st.update(h=win.winfo_height(), min=win.minsize(), max=win.maxsize(),
                      title=win.title())
            win.minsize(st["min"][0], 1)             # lift the minimum height while collapsed

            def collapsed():
                try:
                    name = st["title"].split(" - ", 1)[-1]
                    name = name[:1].upper() + name[1:]
                    bg = getattr(App, "CARD", "#11151e")
                    strip = tk.Frame(win, bg=bg, cursor="hand2")
                    strip.place(x=0, y=0, relwidth=1, relheight=1)
                    tk.Frame(strip, bg=getattr(App, "ACCENT", "#3b82f6"), width=4).pack(
                        side="left", fill="y")
                    lbl = tk.Label(strip, text=name, font=(App.FONT, 16, "bold"), fg=App.TEXT,
                                   bg=bg, anchor="w", cursor="hand2")
                    lbl.pack(side="left", padx=(14, 8), fill="y")
                    chev = tk.Label(strip, text="\u25be", font=(App.FONT, 16), fg=App.SUB,
                                    bg=bg, cursor="hand2")
                    chev.pack(side="right", padx=16, fill="y")
                    for wdg in (strip, lbl, chev):
                        wdg.bind("<Button-1>", lambda _e: toggle_collapse(win))
                    win.title("ymir")                # the big text says which window it is
                    win.maxsize(st["max"][0], COLLAPSED_H)    # can't be dragged taller
                    st["strip"] = strip
                    st["on"] = True
                except Exception as e:  # noqa: BLE001
                    log(f"Error: collapse - {type(e).__name__}: {e}")
                finally:
                    st["busy"] = False
            _animate_height(win, st["h"], COLLAPSED_H, collapsed)
        else:
            try:
                st["strip"].destroy()
            except Exception:  # noqa: BLE001
                pass
            win.title(st["title"])
            win.maxsize(*st["max"])

            def expanded():
                try:
                    win.minsize(*st["min"])
                except Exception:  # noqa: BLE001
                    pass
                st["on"] = False
                st["busy"] = False
            _animate_height(win, win.winfo_height(), st["h"], expanded)
    except Exception as e:  # noqa: BLE001
        try:
            win.__dict__["_col"]["busy"] = False
        except Exception:  # noqa: BLE001
            pass
        log(f"Error: collapse - {type(e).__name__}: {e}")


def _unminimize(win):
    """The minimize button was pressed: bring the window straight back and collapse it."""
    try:
        if win.winfo_exists() and win.state() == "iconic":
            win.deiconify()
            toggle_collapse(win)
    except Exception:  # noqa: BLE001
        pass


if IS_WIN:
    from ctypes import wintypes
    _U32 = ctypes.WinDLL("user32")
    _U32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _U32.GetWindowLongW.restype = ctypes.c_long
    _U32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    _U32.SetWindowLongW.restype = ctypes.c_long
    _U32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]


def _enable_collapse(win):
    """Give a sub window a minimize button (Windows) - pressing it collapses / restores the
    window instead of sending it to the taskbar. No window-procedure hooks are used: the button
    is switched on with a style flag and the resulting minimize is turned into a collapse.
    macOS: Cmd+M does the same."""
    try:
        if IS_MAC:
            win.bind("<Command-m>", lambda _e: (toggle_collapse(win), "break")[1])
            return
        if not IS_WIN:
            return
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if hwnd:
            style = _U32.GetWindowLongW(hwnd, -16)
            if not style & 0x20000:                   # WS_MINIMIZEBOX
                _U32.SetWindowLongW(hwnd, -16, style | 0x20000)
                _U32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x37)   # redraw the frame
        if not win.__dict__.get("_min_bound"):
            win.__dict__["_min_bound"] = True

            def on_unmap(e):
                try:
                    if e.widget is win and win.state() == "iconic":
                        win.after(0, _unminimize, win)
                except Exception:  # noqa: BLE001
                    pass
            win.bind("<Unmap>", on_unmap, add="+")
    except Exception:  # noqa: BLE001  (no minimize button - everything else still works)
        pass


def _dwm_title_colors(win, bg_hex):
    """Paint the title bar of one window dark (Windows 10/11 DWM attributes). Raises on failure."""
    hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
    if not hwnd:
        return
    dwm = ctypes.windll.dwmapi
    on = ctypes.c_int(1)
    for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / old id)
        dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on))
    r, g, b = (int(bg_hex[i:i + 2], 16) for i in (1, 3, 5))
    color = ctypes.c_int(b << 16 | g << 8 | r)  # COLORREF 0x00BBGGRR
    dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), ctypes.sizeof(color))   # caption
    dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(color), ctypes.sizeof(color))   # border (Win 11)
    txt = App.TEXT
    tr, tg, tb = (int(txt[i:i + 2], 16) for i in (1, 3, 5))
    tcolor = ctypes.c_int(tb << 16 | tg << 8 | tr)
    dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(tcolor), ctypes.sizeof(tcolor))  # caption text


def retitle_window(win):
    """Paint the title bar again (colours of the live theme). Windows forgets it when a window is
    hidden and shown again, or when Tk re-creates the frame (transient / nav bar) - that was the
    white strip on top of a sub window."""
    if not IS_WIN:
        return
    try:
        _dwm_title_colors(win, App.BG)
    except Exception:  # noqa: BLE001
        pass


def _on_title_map(event, win):
    if event.widget is win:
        retitle_window(win)                         # right away, before the first paint ...
        try:
            win.after_idle(retitle_window, win)     # ... and once more when Tk has settled
        except tk.TclError:
            pass


def style_title_bar(root, bg_hex):
    """Dark title bar on Windows 10/11 (silently skipped if unsupported / on macOS). The window
    repaints its title bar every time it is shown again."""
    if not IS_WIN:
        if isinstance(root, tk.Toplevel):
            _enable_collapse(root)
        return
    try:
        root.update()
        _dwm_title_colors(root, bg_hex)
    except Exception:
        pass
    try:
        if not root.__dict__.get("_tb_bound"):
            root.__dict__["_tb_bound"] = True
            root.bind("<Map>", lambda e, w=root: _on_title_map(e, w), add="+")
    except Exception:  # noqa: BLE001
        pass
    if isinstance(root, tk.Toplevel):
        _enable_collapse(root)      # minimize button = collapse to the title bar


# ------------------------------- updater --------------------------------
_update = {"busy": False}


def parse_version(text):
    """'v1.1.2' -> (1, 1, 2, 0) so versions can be compared."""
    nums = [int(n) for n in re.findall(r"\d+", str(text))][:4]
    return tuple(nums + [0] * (4 - len(nums)))


def update_repo_ok():
    return bool(re.fullmatch(r"[\w.-]+/[\w.-]+", UPDATE_REPO)) and "REPO_NAME" not in UPDATE_REPO


def fetch_latest_release():
    """Ask GitHub for the latest (non-draft, non-prerelease) release of UPDATE_REPO."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
        headers={"User-Agent": "ymir-macro-updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as r:
        data = json.loads(r.read().decode("utf-8"))
    info = {"tag": str(data.get("tag_name", "")),
            "page": data.get("html_url") or f"https://github.com/{UPDATE_REPO}/releases",
            "url": None, "size": None, "digest": None}
    prefix = f"https://github.com/{UPDATE_REPO}/releases/download/"
    for a in data.get("assets", []):
        url = a.get("browser_download_url", "")
        if IS_WIN and a.get("name", "").lower().endswith(".exe") and url.startswith(prefix):
            info.update(url=url, size=a.get("size"), digest=a.get("digest"))
            break
    return info


def _update_check_worker(manual):
    info, error = None, None
    try:
        info = fetch_latest_release()
    except urllib.error.HTTPError as e:
        error = "no release found on GitHub" if e.code == 404 else f"GitHub answered {e.code}"
    except Exception as e:  # noqa: BLE001  (no internet, timeout, bad json ...)
        error = f"{type(e).__name__}: {e}"
    ev.put(("update_result", manual, info, error))


def start_update_check(manual=False):
    if _update["busy"]:
        return
    if not update_repo_ok():
        if manual:
            log("Erreur: vérificateur de mises à jour - met UPDATE_REPO (owner/repo) en haut du script")
        return
    _update["busy"] = True
    if manual:
        log("Je check les updates en vif...")
    threading.Thread(target=_update_check_worker, args=(manual,), daemon=True).start()


def _install_update_worker(info):
    """Download the new .exe next to the running one, check it, then hand over to a small .bat
    that waits for this program to close, swaps the files and starts the new version."""
    exe = sys.executable
    new_path = exe + ".new"
    try:
        log(f"Téléchargement de la {info['tag']}...")
        req = urllib.request.Request(info["url"], headers={"User-Agent": "ymir-macro-updater"})
        sha = hashlib.sha256()
        done, last_pct = 0, -1
        with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as r, open(new_path, "wb") as f:
            total = int(r.headers.get("Content-Length") or info.get("size") or 0)
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total // 20 * 20
                    if pct != last_pct:
                        last_pct = pct
                        log(f"Downloading update... {pct}%")
        # make sure it is the whole, real file
        if info.get("size") and done != info["size"]:
            raise RuntimeError(f"incomplete download ({done} of {info['size']} bytes)")
        with open(new_path, "rb") as f:
            if f.read(2) != b"MZ":
                raise RuntimeError("the downloaded file is not a Windows program")
        digest = str(info.get("digest") or "")
        if digest.startswith("sha256:") and digest[7:].lower() != sha.hexdigest():
            raise RuntimeError("the download does not match GitHub's checksum")

        q = lambda path: path.replace("%", "%%")      # % is special inside a .bat
        bat = os.path.join(tempfile.gettempdir(), "ymir_update.bat")
        with open(bat, "w", encoding="mbcs", errors="replace", newline="\r\n") as f:
            f.write("\n".join([
                "@echo off",
                "set _MEIPASS2=",
                "set PYINSTALLER_RESET_ENVIRONMENT=1",
                f'cd /d "{q(os.path.dirname(exe))}"',
                "set /a n=0",
                ":wait",
                "set /a n+=1",
                f'move /y "{q(new_path)}" "{q(exe)}" >nul 2>&1',
                "if not errorlevel 1 goto done",
                "if %n% geq 60 goto end",
                "ping -n 2 127.0.0.1 >nul",
                "goto wait",
                ":done",
                f'start "" "{q(exe)}"',
                ":end",
                '(goto) 2>nul & del "%~f0"',
            ]) + "\n")
        env = {k: v for k, v in os.environ.items() if k != "_MEIPASS2"}
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        subprocess.Popen(["cmd", "/c", bat], env=env, close_fds=True,
                         creationflags=0x08000000 | 0x00000008)   # no window, detached
        log("Mise à jour installée - merci pour le pc, je rigole...")
        ev.put(("update_ready",))
    except Exception as e:  # noqa: BLE001
        log(f"Erreur: mise à jour échouée - {type(e).__name__}: {e}")
        try:
            os.remove(new_path)
        except OSError:
            pass
        _update["busy"] = False


def install_update(info):
    threading.Thread(target=_install_update_worker, args=(info,), daemon=True).start()


# main window words: (not Female Raid, Female Raid)
UI_WORDS = {
    "missions": ("Missions", "Raids"),
    "mission_time": ("Mission time", "Raid time"),
    "scrolls": ("Scrolls", "Vessels"),
}


def ui_word(key):
    """The caption of a main window counter for the config that is picked now."""
    return UI_WORDS[key][raid_counting()]


class App:
    frag_open = False    # the "Details" section under the loot row (kept while the window lives)
    last_loot = None     # the loot counts last received (so a config change can swap scrolls <-> vessels)
    BG = "#0b0e14"
    CARD = "#11151e"
    BORDER = "#1a2030"
    LOGBG = "#0f121a"
    TEXT = "#e6e9f2"
    SUB = "#7a8398"
    ACCENT = "#3b82f6"
    # label, pill background, pill text/dot colour
    PILLS = {
        "idle":     ("Idle",     "#1d2230", "#9aa3b8"),
        "waiting":  ("Waiting",  "#2e2410", "#fbbf24"),
        "running":  ("Running",  "#0f2a22", "#34d399"),
        "retrying": ("Retrying", "#14243f", "#60a5fa"),
        "paused":   ("Paused",   "#251d3f", "#a78bfa"),
    }
    def idle_hint(self):
        return (f"Press Start or {START_KEY.upper()}. "
                "Keep this window off the game area.")

    def __init__(self, root):
        self.root = root
        self.run_t0 = None
        self.mission_t0 = None
        self.mission_last = None
        self.reset_armed = False
        self.reset_job = None
        self.settings_win = None
        self.pill_key = "idle"
        self.step_idx = -1
        self.geo_job = None
        self.mini = None    # small stats bar shown instead of the window while the macro runs

        prepare_window(root)    # hidden + transparent while building, so the window never flickers on open
        apply_theme(settings["theme"])
        root.title("ymir")
        try:
            root.iconbitmap(asset_path(ICON_FILE))
        except (tk.TclError, OSError):
            pass  # no icon file: keep the default window icon
        root.configure(bg=self.BG)
        root.geometry("430x720+20+20")  # top-left, away from the game's HUD/buttons
        root.minsize(400, 660)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        try:
            ttk.Style(root).theme_use("clam")
        except tk.TclError:
            pass

        self.build_ui()
        self.set_pill("idle")
        self.set_step(-1)
        # size the window to what the content really needs (fonts get bigger with
        # Windows display scaling), so nothing is cut off when it opens
        root.update_idletasks()
        w = max(430, root.winfo_reqwidth())
        h = max(720, root.winfo_reqheight())
        min_h = max(560, h - 160)
        w, h, x, y = self.restore_geometry(w, h, min_h)   # last size / spot (default: top-left)
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.minsize(400, min_h)
        style_title_bar(root, self.BG)
        reveal_window(root)
        root.bind("<Configure>", self.on_configure)       # remember moves / resizes
        root.bind("<FocusIn>", self.keep_settings_on_top, add="+")   # settings stays above this window
        root.bind("<ButtonPress>", self.keep_settings_on_top, add="+")
        self.pump()
        if UPDATE_CHECK_ON_START:
            root.after(1500, start_update_check)

    # ----- (re)building the window, themes -----
    def apply_styles(self):
        style = ttk.Style(self.root)
        style.configure("Vertical.TScrollbar", troughcolor=self.LOGBG,
                        background=self.SCROLL, bordercolor=self.LOGBG,
                        arrowcolor=self.SUB, lightcolor=self.SCROLL,
                        darkcolor=self.SCROLL, gripcount=0)
        style.map("Vertical.TScrollbar", background=[("active", self.SCROLL_HOVER)])

    def build_ui(self):
        self.apply_styles()
        self.build_header()
        self.build_now()
        self.build_stats()
        self.build_loot()
        self.build_footer()    # bottom-packed first, so these two never get cut off
        self.build_controls()
        self.build_log()       # the log takes whatever space is left

    def set_theme(self, name):
        apply_theme(name)
        self.rebuild_ui()
        save_stats()           # the theme lives in the settings part of the json

    def finish_theme(self):
        """The theme was already applied (the settings window followed first): rebuild this window."""
        self.rebuild_ui()
        save_stats()

    def rebuild_ui(self):
        """Rebuild every widget in the new theme and put back what was on screen."""
        r = self.root
        self.disarm_reset()
        geo = r.geometry()      # size + position now: the theme change must not move / resize the window
        try:
            r.attributes("-alpha", 0.0)     # rebuilt unseen, then shown at once (no half-drawn flicker)
        except tk.TclError:
            pass
        keep = {
            "title": self.now_title.cget("text"),
            "detail": self.now_detail.cget("text"),
            "stats": {k: w.cget("text") for k, w in self.stat_labels.items()},
            "loot": {k: w.cget("text") for k, w in self.loot_labels.items()},
            "topmost": bool(self.topmost.get()),
            "log": self.log_text.dump("1.0", "end-1c", text=True, tag=True),
        }
        for child in r.winfo_children():
            if child is not self.settings_win:      # the settings window is rebuilt by itself
                child.destroy()
        had_mini, self.mini = self.mini is not None, None   # (destroyed above with the rest)
        r.configure(bg=self.BG)
        self.build_ui()
        if had_mini:
            self.show_mini()
        self.now_title.configure(text=keep["title"])
        self.now_detail.configure(text=keep["detail"])
        for k, text in keep["stats"].items():
            self.stat_labels[k].configure(text=text)
        for k, text in keep["loot"].items():
            self.loot_labels[k].configure(text=text)
        self.topmost.set(keep["topmost"])
        self.set_pill(self.pill_key)
        self.set_step(self.step_idx)
        t = self.log_text
        t.configure(state="normal")
        active = set()
        runs = []                   # neighbouring pieces with the same tags go in as one insert
        for kind, value, _index in keep["log"]:
            if kind == "tagon":
                active.add(value)
            elif kind == "tagoff":
                active.discard(value)
            elif kind == "text":
                tags = tuple(sorted(tag for tag in active if tag in ("ts", "err", "good", "info")))
                if runs and runs[-1][1] == tags:
                    runs[-1][0] += value
                else:
                    runs.append([value, tags])
        for value, tags in runs:
            t.insert("end", value, tags)
        t.see("end")
        t.configure(state="disabled")
        r.update_idletasks()
        r.geometry(geo)         # same size and place as before (the log takes up any difference)
        style_title_bar(r, self.BG)     # (this also lets Tk paint everything once, still unseen)
        try:
            r.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
        self.keep_settings_on_top()      # (debounced: one single lift, no flicker)

    # ----- layout helpers -----
    def card(self, title=None, expand=False, pad=(18, 14)):
        outer = tk.Frame(self.root, bg=self.CARD, highlightbackground=self.BORDER,
                         highlightthickness=self.CARD_BW)
        outer.pack(fill="both" if expand else "x", expand=expand, padx=16, pady=6)
        inner = tk.Frame(outer, bg=self.CARD)
        inner.pack(fill="both", expand=True, padx=pad[0], pady=pad[1])
        if title:
            tk.Label(inner, text=title, font=(App.FONT, 8, "bold"), fg=self.SUB,
                     bg=self.CARD, anchor="w").pack(fill="x", pady=(0, 10))
        return inner

    def build_header(self):
        head = tk.Frame(self.root, bg=self.BG)
        head.pack(fill="x", padx=18, pady=(16, 8))
        self.logo_img = None  # keep a reference, or tkinter drops the image
        try:
            logo_path = asset_path(LOGO_FILE)
            stamp = (logo_path, os.path.getmtime(logo_path))
            if _icon_cache.get("logo_stamp") != stamp or "logo" not in _icon_cache:
                _icon_cache["logo"] = tk.PhotoImage(file=logo_path)     # decoded once, not on every rebuild
                _icon_cache["logo_stamp"] = stamp
            self.logo_img = _icon_cache["logo"]
        except (tk.TclError, OSError):
            pass
        if self.logo_img:
            tk.Label(head, image=self.logo_img, bg=self.BG, bd=0).pack(side="left")
        else:
            tk.Label(head, text="ymir", font=(App.FONT, 16, "bold"),
                     fg=self.TEXT, bg=self.BG).pack(side="left")
        self.pill = Pill(head, self.BG)
        self.pill.pack(side="right")

    def build_controls(self):
        row = tk.Frame(self.root, bg=self.BG)
        row.pack(side="bottom", fill="x", padx=16, pady=(5, 5))
        for col in range(3):
            row.columnconfigure(col, weight=1, uniform="tiles")

        self.btn_start = ActionTile(row, "Start", START_KEY.upper(), "play",
                                    self.ACCENT, lambda: start_macro(), self.BG)
        self.btn_pause = ActionTile(row, "Pause", PAUSE_KEY.upper(), "pause",
                                    "#f5b942", toggle_pause, self.BG)
        self.btn_stop = ActionTile(row, "Stop", QUIT_KEY.upper(), "stop",
                                   "#ef5350", request_stop, self.BG)
        for col, tile in enumerate((self.btn_start, self.btn_pause, self.btn_stop)):
            tile.grid(row=0, column=col, sticky="ew",
                      padx=(0 if col == 0 else 4, 0 if col == 2 else 4))

    def build_now(self):
        c = self.card()
        top = tk.Frame(c, bg=self.CARD)
        top.pack(fill="x")
        self.now_title = tk.Label(top, text="Not running", font=(App.FONT, 16, "bold"),
                                  fg=self.TEXT, bg=self.CARD, anchor="w")
        self.now_title.pack(side="left")
        self.step_count = tk.Label(top, text="", font=(App.FONT, 9),
                                   fg=self.SUB, bg=self.CARD)
        self.step_count.pack(side="right")
        self.now_detail = tk.Label(c, text=self.idle_hint(), font=(App.FONT, 10),
                                   fg=self.SUB, bg=self.CARD, anchor="w",
                                   justify="left", wraplength=360)
        self.now_detail.pack(fill="x", pady=(2, 12))
        self.stepper = Stepper(c, len(step_names()), self.CARD,
                               (self.STEP_DONE, self.ACCENT_SOFT, self.STEP_PENDING))
        self.stepper.pack(fill="x")

    def build_stats(self):
        c = self.card()
        row = tk.Frame(c, bg=self.CARD)
        row.pack(fill="x")
        self.stat_labels = {}
        self.stat_captions = {}
        items = (("missions", ui_word("missions")), ("completed", "Completed"),
                 ("failed", "Failed"), ("mission_time", ui_word("mission_time")),
                 ("run_time", "Run time"))
        colors = {"completed": "#34d399", "failed": "#f87171"}
        for i, (key, caption) in enumerate(items):
            if i:
                tk.Frame(row, width=1, bg=self.BORDER).pack(side="left", fill="y", pady=2)
            box = tk.Frame(row, bg=self.CARD)
            box.pack(side="left", expand=True, fill="x")
            val = tk.Label(box, text="--:--" if key.endswith("_time") else "0",
                           font=(App.FONT, 16, "bold"),
                           fg=colors.get(key, self.TEXT), bg=self.CARD)
            val.pack()
            cap = tk.Label(box, text=caption.upper(), font=(App.FONT, 7, "bold"), fg=self.SUB,
                           bg=self.CARD)
            cap.pack(pady=(1, 0))
            self.stat_captions[key] = cap
            self.stat_labels[key] = val

    LOOT_CHIPS = (("body", "Body", "#fbbf24"), ("defence", "Defence", "#60a5fa"),
                  ("offence", "Offence", "#f87171"), ("support", "Support", "#34d399"))
    SERUM_CHIPS = (("attack_serum", "Attack", "#34d399"), ("armor_serum", "Armor", "#fb923c"),
                   ("female_serum", "Female", "#60a5fa"), ("beast_serum", "Beast", "#a16c3a"),
                   ("colossal_serum", "Colossal", "#f87171"))

    def build_loot(self):
        c = self.card(pad=(10, 10))
        self.loot_labels = {}
        self.loot_captions = {}
        self.loot_icon_labels = {}      # key -> the icon label (so Scrolls <-> Vessels can swap it)
        # the Discord emoji pictures (image/emoji_*.png), same ones as the mini bar; a picture
        # that is missing is just left out
        px = max(18, round(20 * max(1.0, self.root.winfo_fpixels("1i") / 96)))
        self.loot_imgs = {name: emoji_icon(f"emoji_{name}.png", px)
                          for name in ("scroll", "vessel", "fragments", "serums")}
        raid = raid_counting()
        top_icons = {"scrolls": "vessel" if raid else "scroll", "fragments": "fragments",
                     "serums": "serums"}

        def counter(parent, col, key, caption, color=None, size=15):
            parent.columnconfigure(col, weight=1, uniform=f"loot_{id(parent)}")
            box = tk.Frame(parent, bg=self.CARD)
            box.grid(row=0, column=col, sticky="ew")
            line = tk.Frame(box, bg=self.CARD)
            line.pack()
            img = self.loot_imgs.get(top_icons.get(key))
            if img is not None:
                ic = tk.Label(line, image=img, bg=self.CARD, bd=0)
                ic.pack(side="left", padx=(0, 5))
                self.loot_icon_labels[key] = ic
            val = tk.Label(line, text="0", font=(App.FONT, size, "bold"),
                           fg=self.TEXT, bg=self.CARD)
            val.pack(side="left")
            cap = tk.Frame(box, bg=self.CARD)
            cap.pack()
            if color:
                tk.Label(cap, text="\u25cf", font=(App.FONT, 7), fg=color,
                         bg=self.CARD).pack(side="left", padx=(0, 4))
            name = tk.Label(cap, text=caption.upper(), font=(App.FONT, 7, "bold"), fg=self.SUB,
                            bg=self.CARD)
            name.pack(side="left")
            self.loot_labels[key] = val
            self.loot_captions[key] = name

        def ridge(parent, col):
            """A thin vertical divider between two counters."""
            tk.Frame(parent, bg=self.BORDER, width=1).grid(row=0, column=col, sticky="ns", pady=2)

        # Scrolls (Vessels for Female Raid) | Fragments (all four kinds) | Serums (all five kinds)
        top = tk.Frame(c, bg=self.CARD)
        top.pack(fill="x")
        counter(top, 0, "scrolls", ui_word("scrolls"))
        ridge(top, 1)
        counter(top, 2, "fragments", "Fragments")
        ridge(top, 3)
        counter(top, 4, "serums", "Serums")

        # "Details" section: the fragment types, a ridge, then the serum types; opened / closed
        # with the line above it
        self.frag_toggle = tk.Label(c, text="", font=(App.FONT, 8), fg=self.SUB, bg=self.CARD,
                                    cursor="hand2")
        self.frag_toggle.pack(pady=(6, 0))
        self.frag_toggle.bind("<Button-1>", self.toggle_fragments)
        self.frag_toggle.bind("<Enter>", lambda _e: self.frag_toggle.configure(fg=self.TEXT))
        self.frag_toggle.bind("<Leave>", lambda _e: self.frag_toggle.configure(fg=self.SUB))
        self.frag_frame = tk.Frame(c, bg=self.CARD)
        frag_row = tk.Frame(self.frag_frame, bg=self.CARD)
        frag_row.pack(fill="x")
        for n, (key, caption, color) in enumerate(self.LOOT_CHIPS):
            counter(frag_row, n, key, caption, color, size=13)
        tk.Frame(self.frag_frame, bg=self.BORDER, height=1).pack(fill="x", pady=6)
        serum_row = tk.Frame(self.frag_frame, bg=self.CARD)
        serum_row.pack(fill="x")
        for n, (key, caption, color) in enumerate(self.SERUM_CHIPS):
            counter(serum_row, n, key, caption, color, size=13)
        self.apply_fragments()

    def toggle_fragments(self, _e=None):
        App.frag_open = not App.frag_open
        self.apply_fragments()

    def apply_fragments(self):
        if App.frag_open:
            self.frag_frame.pack(fill="x", pady=(6, 0))
        else:
            self.frag_frame.pack_forget()
        self.frag_toggle.configure(
            text=("\u25be" if App.frag_open else "\u25b8") + "  Details")

    def refresh_labels(self):
        """The config was changed in Settings: Missions / Mission time / Scrolls <-> Raids / Raid time / Vessels."""
        try:
            for key in ("missions", "mission_time"):
                self.stat_captions[key].configure(text=ui_word(key).upper())
            self.loot_captions["scrolls"].configure(text=ui_word("scrolls").upper())
            ic = self.loot_icon_labels.get("scrolls")
            img = self.loot_imgs.get("vessel" if raid_counting() else "scroll")
            if ic is not None and img is not None:
                ic.configure(image=img)
            self.stepper.set_count(len(step_names()))
            if self.mini is not None:
                self.mini_stepper.set_count(len(step_names()))
            self.set_step(-1 if self.step_idx >= len(step_names()) else self.step_idx)
            if self.last_loot:
                self.show_loot(self.last_loot)
        except (tk.TclError, AttributeError, KeyError):
            pass

    def show_loot(self, counts):
        """Put the loot counts on the labels (vessels for Female Raid, scrolls otherwise)."""
        App.last_loot = dict(counts)
        raid = raid_counting()
        self.loot_labels["scrolls"].configure(text=str(counts.get("vessel" if raid else "scroll", 0)))
        self.loot_labels["fragments"].configure(
            text=str(sum(counts.get(k, 0) for k in FRAGMENT_KINDS)))
        self.loot_labels["serums"].configure(
            text=str(sum(counts.get(k, 0) for k in SERUM_KINDS)))
        for kind in FRAGMENT_KINDS + SERUM_KINDS:
            self.loot_labels[kind].configure(text=str(counts.get(kind, 0)))

    def on_reset_click(self, _e=None):
        """First click arms the button, a second click within 3s erases everything."""
        if self.reset_armed:
            self.disarm_reset()
            reset_stats()
        else:
            self.reset_armed = True
            self.reset_btn.configure(text="Click again to erase these stats", fg="#f87171")
            self.reset_job = self.root.after(3000, self.disarm_reset)

    def disarm_reset(self):
        if self.reset_job:
            try:
                self.root.after_cancel(self.reset_job)
            except tk.TclError:
                pass
            self.reset_job = None
        self.reset_armed = False
        self.reset_btn.configure(text="Reset stats", fg=self.SUB)

    def build_log(self):
        c = self.card("ACTIVITY", expand=True)
        wrap = tk.Frame(c, bg=self.LOGBG)
        wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(wrap, height=8, bg=self.LOGBG, fg=self.TEXT, bd=0,
                                highlightthickness=0, font=(App.MONO, 9),
                                wrap="word", state="disabled", padx=10, pady=8,
                                spacing1=1, spacing3=1)
        sb = ttk.Scrollbar(wrap, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("ts", foreground=self.LOG_TS)
        self.log_text.tag_configure("err", foreground="#f87171")
        self.log_text.tag_configure("good", foreground="#34d399")
        self.log_text.tag_configure("info", foreground=self.ACCENT_SOFT)

    def keep_settings_on_top(self, _event=None):
        """The settings window stays above the main window, even when that one is always on top."""
        if self.settings_win is None or getattr(self, "_lift_job", None):
            return                       # nothing to do, or a lift is already scheduled
        self._lift_job = self.root.after(60, self._lift_settings)

    def _lift_settings(self):
        """One single lift, 60 ms after the last request (many events -> no flicker)."""
        self._lift_job = None
        win = self.settings_win
        if win is None:
            return
        try:
            if win.state() == "withdrawn":
                return                   # hidden behind a sub window (Less windows)
            win.lift()                   # it is owned by the main window and topmost already
        except tk.TclError:
            pass

    def on_topmost_toggle(self):
        on = bool(self.topmost.get())
        self.root.attributes("-topmost", on)
        self.keep_settings_on_top()
        settings["always_on_top"] = on
        save_stats()

    def build_footer(self):
        foot = tk.Frame(self.root, bg=self.BG)
        foot.pack(side="bottom", fill="x", padx=18, pady=(4, 12))
        self.topmost = tk.BooleanVar(value=bool(settings.get("always_on_top")))
        self.root.attributes("-topmost", self.topmost.get())     # restore the saved state
        left = tk.Frame(foot, bg=self.BG)
        left.pack(side="left")
        sw = ToggleSwitch(left, self.topmost, self.on_topmost_toggle, parent_bg=self.BG)
        sw.pack(side="left")
        lbl = tk.Label(left, text="Always on top", font=(App.FONT, 9), fg=self.SUB,
                       bg=self.BG, cursor="hand2")
        lbl.pack(side="left", padx=(8, 0))
        lbl.bind("<Button-1>", sw.flip)
        tk.Label(left, text=VERSION, font=(App.FONT, 9), fg=self.SUB,
                 bg=self.BG).pack(side="left", padx=(14, 0))
        upd = tk.Label(left, text="\u21bb  Updates", font=(App.FONT, 9), fg=self.SUB,
                       bg=self.BG, cursor="hand2")
        upd.pack(side="left", padx=(14, 0))
        upd.bind("<Button-1>", lambda _e: start_update_check(manual=True))
        upd.bind("<Enter>", lambda _e: upd.configure(fg=self.TEXT))
        upd.bind("<Leave>", lambda _e: upd.configure(fg=self.SUB))

        self.reset_btn = tk.Label(foot, text="Reset stats", font=(App.FONT, 9),
                                  fg=self.SUB, bg=self.BG, cursor="hand2")
        self.reset_btn.pack(side="right")
        self.reset_btn.bind("<Button-1>", self.on_reset_click)
        self.reset_btn.bind("<Enter>", lambda _e: self.reset_btn.configure(
            fg="#f87171" if self.reset_armed else self.TEXT))
        self.reset_btn.bind("<Leave>", lambda _e: self.reset_btn.configure(
            fg="#f87171" if self.reset_armed else self.SUB))

        self.settings_btn = tk.Label(foot, text="\u2699  Settings", font=(App.FONT, 9),
                                     fg=self.SUB, bg=self.BG, cursor="hand2")
        self.settings_btn.pack(side="right", padx=(0, 16))
        self.settings_btn.bind("<Button-1>", lambda _e: self.open_settings())
        self.settings_btn.bind("<Enter>", lambda _e: self.settings_btn.configure(fg=self.TEXT))
        self.settings_btn.bind("<Leave>", lambda _e: self.settings_btn.configure(fg=self.SUB))

    # ----- state updates -----
    def set_pill(self, key):
        self.pill_key = key
        text, bg, fg = self.PILLS[key]
        self.pill.update_pill(text, bg, fg)
        self.set_mini_paused(key == "paused")
        if key == "idle":
            self.btn_start.update_tile(enabled=True, active=False)
            self.btn_pause.update_tile(label="Pause", enabled=False, active=False)
            self.btn_stop.update_tile(enabled=False, active=False)
        elif key == "paused":
            self.btn_start.update_tile(enabled=False, active=False)
            self.btn_pause.update_tile(label="Resume", enabled=True, active=True)
            self.btn_stop.update_tile(enabled=True, active=False)
        else:  # waiting / running / retrying
            self.btn_start.update_tile(enabled=True, active=True)
            self.btn_pause.update_tile(label="Pause", enabled=True, active=False)
            self.btn_stop.update_tile(enabled=True, active=False)

    def set_step(self, idx):
        self.step_idx = idx
        self.stepper.set_index(idx)
        if self.mini is not None:
            self.mini_stepper.set_index(idx)
        self.step_count.configure(
            text="" if idx < 0 else f"Step {idx + 1} of {len(step_names())}")

    def add_log(self, msg):
        t = self.log_text
        t.configure(state="normal")
        t.insert("end", time.strftime("%H:%M:%S") + "   ", "ts")
        low = msg.lower()
        if low.startswith("error"):
            tag = "err"
        elif low.startswith(("mission started", "started")):
            tag = "good"
        elif low.startswith("result: completed"):
            tag = "good"
        elif low.startswith("result: failed"):
            tag = "err"
        elif low.startswith("loot:") and "nothing" not in low:
            tag = "good"
        elif low.startswith(("retry found", "hands off", "waiting")):
            tag = "info"
        else:
            tag = ""
        t.insert("end", msg + "\n", tag)
        lines = int(t.index("end-1c").split(".")[0])
        if lines > 300:
            t.delete("1.0", f"{lines - 300}.0")
        t.see("end")
        t.configure(state="disabled")

    def handle(self, e):
        kind = e[0]
        if kind == "log":
            self.add_log(e[1])
        elif kind == "now":
            self.now_title.configure(text=e[1])
            self.now_detail.configure(text=e[2])
        elif kind == "detail":
            self.now_detail.configure(text=e[1])
        elif kind == "step":
            self.set_step(e[1])
        elif kind == "status":
            self.set_pill(e[1])
        elif kind == "stats":
            self.stat_labels["missions"].configure(text=str(e[1]))
            self.stat_labels["completed"].configure(text=str(e[4]["completed"]))
            self.stat_labels["failed"].configure(text=str(e[4]["failed"]))
            self.show_loot(e[3])
        elif kind == "captured":
            if self.settings_win:
                self.settings_win.on_captured(e[1], e[2])
        elif kind == "settings_msg":
            if self.settings_win:
                self.settings_win.say(e[1], error=e[2], good=e[3])
        elif kind == "update_result":
            self.on_update_result(e[1], e[2], e[3])
        elif kind == "update_ready":
            self.on_close()          # the .bat swaps the files and starts the new version
        elif kind == "mission_started":
            self.mission_t0 = time.time()
        elif kind == "mission_ended":
            if self.mission_t0:
                self.mission_last = time.time() - self.mission_t0
            self.mission_t0 = None
        elif kind == "started":
            self.run_t0 = time.time()
            self.mission_t0 = None
            self.mission_last = None
            self.set_pill("waiting")
            self.show_mini()
        elif kind == "stopping":
            self.hide_mini()
        elif kind == "done":
            self.hide_mini()
            self.run_t0 = None
            self.mission_t0 = None
            self.set_pill("idle")
            self.set_step(-1)
            self.now_title.configure(text="Stopped")
            self.now_detail.configure(text=self.idle_hint())

    def pump(self):
        try:
            while True:
                self.handle(ev.get_nowait())
        except queue.Empty:
            pass
        if self.run_t0:
            self.stat_labels["run_time"].configure(text=fmt_time(time.time() - self.run_t0))
        if self.mission_t0:
            self.stat_labels["mission_time"].configure(
                text=fmt_time(time.time() - self.mission_t0))
        elif self.mission_last is not None:
            self.stat_labels["mission_time"].configure(text=fmt_time(self.mission_last))
        if self.mini is not None:      # the mini bar mirrors the window's numbers
            for key, lbl in self.mini_labels.items():
                src = self.stat_labels.get(key) or self.loot_labels[key]
                lbl.configure(text=src.cget("text"))
        self.root.after(100, self.pump)

    # ----- mini stats bar (instead of the window while the macro runs) -----
    MINI_ITEMS = (("missions", "Missions"), ("completed", "Completed"), ("failed", "Failed"),
                  ("run_time", "Run time"), ("serums", "Serums"), ("scrolls", "Scrolls"),
                  ("fragments", "Fragments"))
    MINI_ICONS = {"serums": "emoji_serums.png", "scrolls": "emoji_scroll.png",
                  "fragments": "emoji_fragments.png"}  # Discord emojis
    MINI_RAID_ICONS = {"scrolls": "emoji_vessel.png"}     # Female Raid: vessels instead of scrolls

    def show_mini(self):
        """Hide the window and show a small always-on-top stats bar in the top-right corner.
        It goes away (and the window comes back) when the macro stops."""
        if self.mini is not None or not settings.get("compact_ui", False):
            return
        m = self.mini = tk.Toplevel(self.root, bg=self.CARD, highlightbackground=self.BORDER,
                                    highlightthickness=self.CARD_BW)
        m.withdraw()                        # built + placed unseen, shown at the end
        m.overrideredirect(True)            # no title bar / taskbar button
        m.attributes("-topmost", True)
        self.mini_labels = {}
        self.mini_imgs = []                 # keep references, or tkinter drops the images
        px = max(14, round(16 * max(1.0, self.root.winfo_fpixels("1i") / 96)))
        colors = {"completed": "#34d399", "failed": "#f87171"}   # same as the stats card
        top = tk.Frame(m, bg=self.CARD)     # the stats row; the step bar goes below it
        top.pack(fill="x")
        # "paused" box at the left end, only packed while paused (see set_mini_paused)
        pz_color = self.PILLS["paused"][2]
        self.mini_pause = pz = tk.Frame(top, bg=self.CARD)
        pbox = tk.Frame(pz, bg=self.CARD)
        pbox.pack(side="left", padx=9, pady=(5, 6))
        icon = tk.Canvas(pbox, width=px, height=px, bg=self.CARD, highlightthickness=0)
        icon.pack()
        for x in (0.18, 0.58):     # the two bars of the pause icon, like the Pause tile
            rounded_rect(icon, px * x, px * 0.12, px * (x + 0.24), px * 0.88, 1.5,
                         fill=pz_color, outline="")
        tk.Label(pbox, text="PAUSED", font=(App.FONT, 6, "bold"), fg=pz_color,
                 bg=self.CARD).pack()
        tk.Frame(pz, width=1, bg=self.BORDER).pack(side="left", fill="y", pady=7)
        for i, (key, caption) in enumerate(self.MINI_ITEMS):
            if i:
                tk.Frame(top, width=1, bg=self.BORDER).pack(side="left", fill="y", pady=7)
            box = tk.Frame(top, bg=self.CARD)
            box.pack(side="left", padx=9, pady=(5, 6))
            row = tk.Frame(box, bg=self.CARD)
            row.pack()
            icons = dict(self.MINI_ICONS, **(self.MINI_RAID_ICONS
                                             if raid_counting() else {}))
            icon = emoji_icon(icons[key], px) if key in icons else None
            if key in UI_WORDS:        # Missions / Scrolls -> Raids / Vessels, like the window
                caption = ui_word(key)
            if icon is not None:
                self.mini_imgs.append(icon)
                ic = tk.Label(row, image=icon, bg=self.CARD, bd=0)
                ic.pack(side="left", padx=(0, 4))
            val = tk.Label(row, text="0", font=(App.FONT, 11, "bold"),
                           fg=colors.get(key, self.TEXT), bg=self.CARD)
            val.pack(side="left")
            cap = tk.Label(box, text=caption.upper(), font=(App.FONT, 6, "bold"),
                           fg=self.SUB, bg=self.CARD)
            cap.pack()
            self.mini_labels[key] = val
            if not i:
                self.mini_first = box
        # the mission step bar, same as the one in the window (kept in sync by set_step)
        self.mini_stepper = Stepper(m, len(step_names()), self.CARD,
                                    (self.STEP_DONE, self.ACCENT_SOFT, self.STEP_PENDING))
        self.mini_stepper.configure(width=1)   # just fill the bar, don't widen it
        self.mini_stepper.pack(fill="x", padx=10, pady=(0, 8))
        self.mini_stepper.set_index(self.step_idx)
        self.set_mini_paused(self.pill_key == "paused")   # also places the bar
        m.deiconify()
        self.root.withdraw()

    def set_mini_paused(self, on):
        """Show / hide the PAUSED box and keep the bar at the top of Roblox (just under its
        title bar), left / middle / right as picked in Settings > UI, or of the screen if Roblox
        isn't found."""
        if self.mini is None:
            return
        if on:
            self.mini_pause.pack(side="left", fill="y", before=self.mini_first)
        else:
            self.mini_pause.pack_forget()
        m = self.mini
        m.update_idletasks()
        a = roblox_area() or {"left": 0, "top": 0, "width": m.winfo_screenwidth()}
        free = a["width"] - m.winfo_reqwidth()           # room left over beside the bar
        pos = settings.get("mini_pos", DEFAULT_MINI_POS)
        x = a["left"] + (0 if pos == "Left" else free if pos == "Right" else free // 2)
        m.geometry(f"+{x}+{a['top']}")

    def hide_mini(self):
        """Remove the stats bar and bring the window back."""
        if self.mini is None:
            return
        self.mini.destroy()
        self.mini = None
        reveal_window(self.root)

    def on_update_result(self, manual, info, error):
        _update["busy"] = False
        if error:
            if manual:
                log(f"Erreur: update check n'a pas fonctionné - {error}")
            return
        if not info or parse_version(info["tag"]) <= parse_version(VERSION):
            if manual:
                log(f"Tu as la dernière version ({VERSION})")
            return
        log(f"mise à jour disponible: {info['tag']} (tu as {VERSION})")
        if running():
            log("Arrête la macro avant, puis fait Updates pour l'installer")
            return
        if not getattr(sys, "frozen", False) or not info["url"]:
            # plain .py (or a release without an .exe): just open the release page
            if messagebox.askyesno("Mise à jour disponible",
                                   f"{info['tag']} est disponible (tu as la {VERSION}).\n\n"
                                   "Tu veux ouvrir la page de la release?", parent=self.root):
                webbrowser.open(info["page"])
            return
        if messagebox.askyesno("Mise à jour disponible",
                               f"{info['tag']} est disponible (tu as la {VERSION}).\n\n"
                               "Télécharger et redémarrer la macro maintenant?", parent=self.root):
            _update["busy"] = True
            install_update(info)

    def open_changelog(self):
        """Footer > Changelog: a window showing change.md."""
        win = getattr(self, "changelog_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.lift()
                win.focus_force()
                return
        except tk.TclError:
            pass
        self.changelog_win = ChangelogWindow(self)

    def open_settings(self):
        if self.settings_win is not None:
            self.settings_win.deiconify()
            self.settings_win.attributes("-topmost", True)
            self.settings_win.lift()
            self.settings_win.focus_force()
            return
        self.settings_win = SettingsWindow(self)

    def refresh_hotkeys(self):
        """Hotkeys were changed in Settings: update the tile badges and the hint."""
        self.btn_start.update_tile(key_text=START_KEY.upper())
        self.btn_pause.update_tile(key_text=PAUSE_KEY.upper())
        self.btn_stop.update_tile(key_text=QUIT_KEY.upper())
        if not running():
            self.now_detail.configure(text=self.idle_hint())

    # ----- remembering the window size / position -----
    def restore_geometry(self, w, h, min_h):
        """Saved size + position, kept inside the screen. Returns (w, h, x, y)."""
        win = settings.get("window")
        if not win:
            return w, h, 20, 20
        r = self.root
        vx, vy = r.winfo_vrootx(), r.winfo_vrooty()
        vw = max(r.winfo_vrootwidth(), r.winfo_screenwidth())
        vh = max(r.winfo_vrootheight(), r.winfo_screenheight())
        w = min(max(win["w"], 400), vw)
        h = min(max(win["h"], min_h), vh)
        # keep a good part of the window (and its title bar) on a screen, e.g. after a
        # monitor was unplugged
        x = min(max(win["x"], vx - w + 120), vx + vw - 120)
        y = min(max(win["y"], vy), vy + vh - 80)
        return w, h, x, y

    def on_configure(self, event):
        if event.widget is not self.root:      # ignore the child widgets
            return
        if self.geo_job:
            self.root.after_cancel(self.geo_job)
        self.geo_job = self.root.after(800, self.save_window)   # once it stops moving

    def save_window(self):
        self.geo_job = None
        r = self.root
        try:
            if r.state() != "normal":          # minimized / maximized: keep the last good one
                return
            m = re.match(r"(\d+)x(\d+)([+-])(-?\d+)([+-])(-?\d+)$", r.geometry())
        except tk.TclError:
            return
        if not m:
            return
        w, h = int(m.group(1)), int(m.group(2))
        x = int(m.group(4)) * (1 if m.group(3) == "+" else -1)
        y = int(m.group(6)) * (1 if m.group(5) == "+" else -1)
        new = {"x": x, "y": y, "w": w, "h": h}
        if new != settings.get("window"):
            settings["window"] = new
            save_stats()

    def on_close(self):
        if self.geo_job:
            self.root.after_cancel(self.geo_job)
        self.save_window()
        if self.settings_win is not None:
            self.settings_win.close()
        request_stop()
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        self.root.destroy()


class KeyButton(tk.Label):
    """Clickable key badge: shows the current key, or 'Press a key...' while capturing."""
    NORMAL, HOVER, CAPTURE = "#1a1e29", "#212637", "#16254a"

    def __init__(self, parent, key, on_click):
        super().__init__(parent, width=14, font=(App.FONT, 9, "bold"), bd=0, pady=5,
                         cursor="hand2", highlightthickness=1)
        self.capturing = False
        self.show_key(key)
        self.bind("<Button-1>", lambda _e: on_click())
        self.bind("<Enter>", lambda _e: None if self.capturing else self.configure(bg=self.HOVER))
        self.bind("<Leave>", lambda _e: None if self.capturing else self.configure(bg=self.NORMAL))

    def show_key(self, key):
        self.capturing = False
        self.configure(text=key.upper(), fg=App.TEXT, bg=self.NORMAL,
                       highlightbackground=App.FIELD_EDGE, highlightcolor=App.FIELD_EDGE)

    def show_capturing(self):
        self.capturing = True
        self.configure(text="Press a key...", fg=App.ACCENT_TEXT, bg=self.CAPTURE,
                       highlightbackground=App.ACCENT_LINE, highlightcolor=App.ACCENT_LINE)


class ModernDropdown(tk.Frame):
    """Flat dark dropdown. Every entry shows a round letter badge, its name and an
    optional tag (WIP / Beta). items = [(key, label, (letter, badge_bg, letter_fg), tag), ...]
    where tag is None, "WIP" or "Beta"."""
    BG, HOVER, EDGE = "#1a1e29", "#232a3d", "#2a3042"
    TEXT, SUB, ACCENT = "#e6e9f2", "#7a8398", "#3b82f6"
    WIP_BG, WIP_FG = "#2e2410", "#fbbf24"
    BETA_BG, BETA_FG = "#10223d", "#60a5fa"

    def __init__(self, parent, items, value, on_change, provider=None):
        super().__init__(parent, bg=self.BG, cursor="hand2", highlightthickness=1,
                         highlightbackground=self.EDGE, highlightcolor=self.EDGE)
        self.items = {it[0]: it for it in items}
        self.order = [it[0] for it in items]
        self.value = value if value in self.items else self.order[0]
        self.on_change = on_change
        self.provider = provider    # optional: called each time the list opens (new / deleted entries)
        self.popup = None
        self.row = None
        self.render_trigger()
        self.bind("<Destroy>", lambda e: self.close_popup() if e.widget is self else None)

    # ----- pieces -----
    def make_badge(self, parent, spec, bg):
        letter, color, fg = spec
        c = tk.Canvas(parent, width=22, height=22, bg=bg, highlightthickness=0, bd=0)
        c.create_oval(1, 1, 21, 21, fill=color, outline="")
        c.create_text(11, 11, text=letter, fill=fg, font=(App.FONT, 9, "bold"))
        return c

    def make_row(self, parent, key, bg, trailing):
        """One entry laid out as: badge  name  [WIP]  ........  trailing symbol."""
        _key, label, badge, tag_text = self.items[key]
        row = tk.Frame(parent, bg=bg, cursor="hand2")
        parts = [self.make_badge(row, badge, bg)]
        parts[0].pack(side="left", padx=(12, 10), pady=9)
        name = tk.Label(row, text=label, font=(App.FONT, 10), fg=self.TEXT, bg=bg)
        name.pack(side="left")
        parts.append(name)
        if tag_text:
            beta = tag_text == "Beta"
            tag = tk.Label(row, text=tag_text, font=(App.FONT, 7, "bold"),
                           fg=self.BETA_FG if beta else self.WIP_FG,
                           bg=self.BETA_BG if beta else self.WIP_BG, padx=6, pady=1)
            tag.keep_bg = True
            tag.pack(side="left", padx=(8, 0))
            parts.append(tag)
        sym = tk.Label(row, text=trailing, font=(App.FONT, 9), bg=bg,
                       fg=self.ACCENT if trailing == "\u2713" else self.SUB)
        sym.pack(side="right", padx=(0, 14))
        parts.append(sym)
        return row, parts

    @staticmethod
    def paint(row, bg):
        row.configure(bg=bg)
        for w in row.winfo_children():
            if not getattr(w, "keep_bg", False):
                w.configure(bg=bg)

    # ----- closed state -----
    def render_trigger(self):
        if self.row is not None:
            self.row.destroy()
        self.row, parts = self.make_row(self, self.value, self.BG, "\u25be")
        self.row.pack(fill="x")
        for w in [self, self.row] + parts:
            w.bind("<Button-1>", lambda _e: self.toggle())
            w.bind("<Enter>", lambda _e: self.hover(True))
            w.bind("<Leave>", lambda _e: self.hover(False))

    def hover(self, on):
        edge = self.ACCENT if (on or self.popup) else self.EDGE
        self.configure(highlightbackground=edge, highlightcolor=edge)

    def set_items(self, items, value=None, notify=False):
        """Swap the entries. value=None keeps the pick (falls back to the first entry if it is gone)."""
        old = self.items
        self.items = {it[0]: it for it in items}
        self.order = [it[0] for it in items]
        want = self.value if value is None else value
        new = want if want in self.items else self.order[0]
        changed = new != self.value
        self.value = new
        if changed or value is not None or old.get(new) != self.items.get(new):
            self.render_trigger()
        if changed and notify:
            self.on_change(new)

    # ----- open state -----
    def toggle(self):
        if self.popup is not None:
            self.close_popup()
            return
        if self.provider is not None:
            self.set_items(self.provider(), notify=True)
        self.update_idletasks()
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height() + 4
        pop = tk.Toplevel(self)
        pop.withdraw()                         # hidden until it is sized + placed (no flicker)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)      # stays above the always-on-top settings window
        pop.attributes("-alpha", 0.0)
        pop.configure(bg=self.EDGE)
        inner = tk.Frame(pop, bg=self.BG)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        for key in self.order:
            selected = key == self.value
            row, parts = self.make_row(inner, key, self.BG, "\u2713" if selected else "")
            row.pack(fill="x")
            for w in [row] + parts:
                w.bind("<Button-1>", lambda _e, k=key: self.pick(k))
                w.bind("<Enter>", lambda _e, r=row: self.paint(r, self.HOVER))
                w.bind("<Leave>", lambda _e, r=row: self.paint(r, self.BG))
        pop.update_idletasks()
        pop.geometry(f"{self.winfo_width()}x{pop.winfo_reqheight()}+{x}+{y}")
        pop.bind("<Escape>", lambda _e: self.close_popup())
        pop.bind("<FocusOut>", lambda _e: self.after(60, self.check_focus))
        self.popup = pop
        self.hover(True)
        pop.deiconify()
        pop.update_idletasks()
        pop.attributes("-alpha", 1.0)
        pop.lift()
        pop.focus_force()

    def check_focus(self):
        """Close when the user clicks anywhere outside the list."""
        pop = self.popup
        if pop is None:
            return
        try:
            f = pop.focus_get()
        except KeyError:
            f = None
        if f is None or not str(f).startswith(str(pop)):
            self.close_popup()

    def close_popup(self):
        pop, self.popup = self.popup, None
        if pop is not None:
            try:
                pop.destroy()
            except tk.TclError:
                pass
        try:
            self.hover(False)
        except tk.TclError:
            pass

    def pick(self, key):
        self.close_popup()
        if key != self.value:
            self.value = key
            self.render_trigger()
            self.on_change(key)


class ThemeCard(tk.Canvas):
    """Clickable preview of one theme: a tiny mock of the window in that theme's look."""
    W, H = 170, 96

    def __init__(self, parent, name, selected, on_click, parent_bg):
        super().__init__(parent, width=self.W, height=self.H, bg=parent_bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.name, self.selected, self.on_click = name, selected, on_click
        self.P = build_palette(name)
        self.hover = False
        self.bind("<Enter>", lambda _e: self.set_hover(True))
        self.bind("<Leave>", lambda _e: self.set_hover(False))
        self.bind("<Button-1>", lambda _e: self.on_click(self.name))
        self.draw()

    def set_hover(self, on):
        self.hover = on
        self.draw()

    def draw(self):
        P, W, H = self.P, self.W, self.H
        self.delete("all")
        if self.selected:
            line, lw = P["ACCENT"], 2
        elif self.hover:
            line, lw = P["FIELD_EDGE_HOVER"], 2
        else:
            line, lw = P["FIELD_EDGE"], 1
        rounded_rect(self, 1, 1, W - 1, H - 1, min(P["R_TILE"] + 2, 14),
                     fill=P["BG"], outline=line, width=lw)
        cr = min(P["R_TILE"] // 2, 6)
        # mini card: title bars + a status pill
        rounded_rect(self, 10, 10, W - 10, 40, cr, fill=P["CARD"],
                     outline=P["BORDER"] if P["CARD_BW"] else "")
        rounded_rect(self, 18, 18, 66, 24, 2, fill=P["TEXT"], outline="")
        rounded_rect(self, 18, 28, 48, 32, 2, fill=P["SUB"], outline="")
        pill_r = 7 if P["R_PILL"] is None else min(P["R_PILL"], 7)
        rounded_rect(self, W - 54, 18, W - 18, 32, pill_r, fill=P["ACCENT_BG"], outline="")
        self.create_oval(W - 48, 22, W - 42, 28, fill=P["ACCENT"], outline="")
        # three mini tiles, the first one "active"
        tw = (W - 20 - 12) / 3
        for i in range(3):
            x1 = 10 + i * (tw + 6)
            if i == 0:
                rounded_rect(self, x1, 48, x1 + tw, 64, cr, fill=P["ACCENT_BG"],
                             outline=P["ACCENT_LINE"], width=1)
            else:
                rounded_rect(self, x1, 48, x1 + tw, 64, cr, fill=P["FIELD"],
                             outline=P["FIELD_EDGE"], width=1)
        # name (in the theme's own font) + a tick when selected
        self.create_text(12, H - 16, text=self.name, fill=P["TEXT"], anchor="w",
                         font=(P["FONT"], 9, "bold"))
        if self.selected:
            self.create_oval(W - 25, H - 23, W - 11, H - 9, fill=P["ACCENT"], outline="")
            self.create_text(W - 18, H - 16, text="\u2713", fill="#ffffff",
                             font=("Segoe UI", 8, "bold"))
        else:
            self.create_oval(W - 20, H - 20, W - 12, H - 12, fill=P["ACCENT"], outline="")


class SettingsWindow(tk.Toplevel):
    """Settings: Keybinds / Discord / Configs / UI. Every change is saved right away
    (into the same json as the stats)."""
    TABS = (("keys", "Keybinds"), ("family", "Configs"), ("recovery", "Recovery"),
            ("discord", "Discord"), ("themes", "UI"))
    UID_HELP = ("Discord > Settings > Advanced > Developer Mode on, then right-click "
                "your name > Copy User ID.")

    def __init__(self, app):
        super().__init__(app.root)
        prepare_window(self)    # build everything hidden, then show it once - no flicker
        self.app = app
        self.capturing = None       # bind id waiting for a key press
        self.capture_hook = None
        self.save_job = None
        self.say_job = None
        self.key_buttons = {}
        self.pages = {}
        self.tab_widgets = {}

        self.title("ymir - settings")
        try:
            self.iconbitmap(asset_path(ICON_FILE))
        except (tk.TclError, OSError):
            pass
        self.resizable(True, True)
        self.minsize(420, 380)
        self.wrap_w = 353                    # wraplength of the notes / hints (follows the width)
        self.transient(app.root)             # owned by the main window = always drawn above it
        self.attributes("-topmost", True)    # settings always stays above everything
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.build_ui("keys")
        self.update_idletasks()
        x, y = app.root.winfo_x() + 40, app.root.winfo_y() + 40
        h = min(self.winfo_reqheight(), max(380, self.winfo_screenheight() - 120))
        self.geometry(f"460x{h}+{x}+{max(0, min(y, self.winfo_screenheight() - h - 60))}")
        style_title_bar(self, App.BG)
        reveal_window(self)
        self.lift()
        self.attributes("-topmost", True)
        self.focus_force()

    def build_ui(self, tab):
        """(Re)build every widget - on open, and again when the theme changes."""
        self.cancel_capture()
        for child in self.winfo_children():
            if isinstance(child, (AssetOverrideWindow, ConfigEditorWindow, GuideWindow,
                                  FailsafeConfigWindow, ReconnectConfigWindow)):
                continue
            child.destroy()
        self.key_buttons, self.pages, self.tab_widgets = {}, {}, {}
        self.configure(bg=App.BG)
        self.build_tab_bar()
        # footer first, so it always stays visible when the window is made small
        foot = tk.Frame(self, bg=App.BG)
        foot.pack(side="bottom", fill="x", padx=22, pady=(4, 14))
        # the pages sit in a scrollable area: the window can be resized, a scrollbar shows when
        # the content no longer fits
        body = tk.Frame(self, bg=App.BG)
        body.pack(side="top", fill="both", expand=True, padx=(18, 4))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(body, bg=App.BG, highlightthickness=0, bd=0)
        self.vscroll = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        # the scrollbar's column keeps its width even while the bar is hidden: the pages then
        # keep the same width on every tab, so switching tabs never re-wraps / jumps the text
        body.columnconfigure(1, minsize=max(12, self.vscroll.winfo_reqwidth()))
        self.canvas.configure(yscrollcommand=self.vscroll.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vscroll.grid(row=0, column=1, sticky="ns")
        inner = tk.Frame(self.canvas, bg=App.BG)
        self.inner_id = self.canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.columnconfigure(0, weight=1)
        inner.rowconfigure(0, weight=1)
        inner.bind("<Configure>", lambda _e: self.on_size())
        self.canvas.bind("<Configure>", lambda e: self.on_size(e.width))
        self.bind("<MouseWheel>", self.on_wheel)
        for key, _text in self.TABS:    # all pages sit on top of each other, one is raised
            page = tk.Frame(inner, bg=App.BG)
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = page
        self.build_keys(self.pages["keys"])
        self.build_discord(self.pages["discord"])
        self.build_family(self.pages["family"])
        self.build_recovery(self.pages["recovery"])
        self.build_themes(self.pages["themes"])
        self.update_idletasks()
        # opens tall enough to show the whole Configs tab (capped to the screen in __init__);
        # a taller page on another tab is reached by scrolling
        try:
            shown = self.pages["family"].winfo_reqheight()
        except (KeyError, tk.TclError):
            shown = inner.winfo_reqheight()
        self.canvas.configure(height=shown)

        # footer: the status line on the left, Changelog + GitHub on the right
        self.status = tk.Label(foot, text="", font=(App.FONT, 9), fg=App.SUB, bg=App.BG,
                               anchor="w", wraplength=170, justify="left")
        self.status.pack(side="left", fill="x", expand=True)
        links = tk.Frame(foot, bg=App.BG)
        links.pack(side="right")
        px = max(14, round(16 * max(1.0, self.winfo_fpixels("1i") / 96)))
        pat = tk.Label(links, text="\u2665 Patreon", font=(App.FONT, 9, "bold"), fg="#ff7a66",
                       bg=App.BG, cursor="hand2")
        pat.pack(side="left", padx=(0, 16))
        pat.bind("<Button-1>", lambda _e: webbrowser.open(PATREON_URL))
        pat.bind("<Enter>", lambda _e: pat.configure(fg="#ffb3a6"))
        pat.bind("<Leave>", lambda _e: pat.configure(fg="#ff7a66"))
        self.dc_dim = tinted_icon(App.SUB, px, DISCORD_MASK_B64)
        self.dc_hi = tinted_icon(App.TEXT, px, DISCORD_MASK_B64)
        if self.dc_dim is not None and self.dc_hi is not None:
            dc = tk.Label(links, image=self.dc_dim, bg=App.BG, bd=0, cursor="hand2")
            dc.bind("<Enter>", lambda _e: dc.configure(image=self.dc_hi))
            dc.bind("<Leave>", lambda _e: dc.configure(image=self.dc_dim))
        else:
            dc = tk.Label(links, text="Discord", font=(App.FONT, 9), fg=App.SUB,
                          bg=App.BG, cursor="hand2")
            dc.bind("<Enter>", lambda _e: dc.configure(fg=App.TEXT))
            dc.bind("<Leave>", lambda _e: dc.configure(fg=App.SUB))
        dc.pack(side="left", padx=(0, 16))
        dc.bind("<Button-1>", lambda _e: webbrowser.open(DISCORD_URL))
        chg = tk.Label(links, text="\u2630  Changelog", font=(App.FONT, 9), fg=App.SUB,
                       bg=App.BG, cursor="hand2")
        chg.pack(side="left")
        chg.bind("<Button-1>", lambda _e: self.app.open_changelog())
        chg.bind("<Enter>", lambda _e: chg.configure(fg=App.TEXT))
        chg.bind("<Leave>", lambda _e: chg.configure(fg=App.SUB))
        # GitHub icon (opens the author's page); dim normally, bright on hover
        self.gh_dim, self.gh_hi = tinted_icon(App.SUB, px), tinted_icon(App.TEXT, px)
        if self.gh_dim is not None and self.gh_hi is not None:
            gh = tk.Label(links, image=self.gh_dim, bg=App.BG, bd=0, cursor="hand2")
            gh.bind("<Enter>", lambda _e: gh.configure(image=self.gh_hi))
            gh.bind("<Leave>", lambda _e: gh.configure(image=self.gh_dim))
        else:
            gh = tk.Label(links, text="GitHub", font=(App.FONT, 9), fg=App.SUB, bg=App.BG,
                          cursor="hand2")
            gh.bind("<Enter>", lambda _e: gh.configure(fg=App.TEXT))
            gh.bind("<Leave>", lambda _e: gh.configure(fg=App.SUB))
        gh.pack(side="left", padx=(16, 0))
        gh.bind("<Button-1>", lambda _e: webbrowser.open(GITHUB_URL))
        self.show_tab(tab)

    def on_size(self, width=None):
        """Keep the pages as wide as the window, wrap the notes to it, show the scrollbar only if needed."""
        try:
            inner = self.canvas.nametowidget(self.canvas.itemcget(self.inner_id, "window"))
            if width is not None:
                self.canvas.itemconfigure(self.inner_id, width=width)
                wrap = max(200, width - 34)
                if wrap != self.wrap_w:
                    self.wrap_w = wrap
                    self.rewrap(inner)
            page = self.pages.get(getattr(self, "cur_tab", None))
            need = page.winfo_reqheight() if page is not None else inner.winfo_reqheight()
            self.canvas.configure(scrollregion=(0, 0, 1, need))
            if need > self.canvas.winfo_height() > 1:
                self.vscroll.grid()
            else:
                self.vscroll.grid_remove()
                self.canvas.yview_moveto(0)
        except tk.TclError:
            pass

    def rewrap(self, widget):
        for w in widget.winfo_children():
            if isinstance(w, tk.Label) and int(w.cget("wraplength") or 0) > 0:
                w.configure(wraplength=self.wrap_w)
            self.rewrap(w)

    def on_wheel(self, e):
        if self.vscroll.winfo_ismapped():
            self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    def retheme(self, tab):
        """The theme changed: rebuild this window in the new look (same place, same size)."""
        self.commit()
        old_w, old_h = self.winfo_width(), self.winfo_height()
        try:
            self.attributes("-alpha", 0.0)      # rebuilt unseen
        except tk.TclError:
            pass
        self.build_ui(tab)
        self.update_idletasks()
        # keep the size it had; it only grows if the new look really needs more room
        self.geometry(f"{max(old_w, self.winfo_reqwidth())}x{max(old_h, self.winfo_reqheight())}")
        style_title_bar(self, App.BG)   # (this also lets Tk paint everything once, still unseen)
        try:
            self.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
        self.app.keep_settings_on_top()

    # ----- look -----
    def setup_styles(self):
        pass    # nothing to style: the dropdown and theme cards are custom widgets

    def card(self, parent, title, note=None, hot=False):
        """hot=True: a brighter, bigger title and note (for a card that must not be overlooked)."""
        outer = tk.Frame(parent, bg=App.CARD, highlightbackground=App.BORDER,
                         highlightthickness=App.CARD_BW)
        outer.pack(fill="x", pady=6)
        inner = tk.Frame(outer, bg=App.CARD)
        inner.pack(fill="x", padx=16, pady=14)
        tk.Label(inner, text=title, font=(App.FONT, 10 if hot else 8, "bold"),
                 fg=App.TEXT if hot else App.SUB, bg=App.CARD,
                 anchor="w").pack(fill="x")
        if note:
            tk.Label(inner, text=note, font=(App.FONT, 9), fg=App.TEXT if hot else App.SUB,
                     bg=App.CARD,
                     anchor="w", justify="left",
                     wraplength=self.wrap_w).pack(fill="x", pady=(2, 0))
        tk.Frame(inner, height=8, bg=App.CARD).pack()
        return inner

    def check(self, parent, text, var, command):
        return switch_row(parent, text, var, command, App.CARD, App.TEXT)

    def entry(self, parent, var, show=None):
        e = tk.Entry(parent, textvariable=var, show=show, font=(App.FONT, 10),
                     bg=App.LOGBG, fg=App.TEXT, insertbackground=App.TEXT, relief="flat",
                     disabledbackground=App.DIS_FILL, disabledforeground=App.DIS_TEXT,
                     highlightthickness=1, highlightbackground=App.BORDER,
                     highlightcolor=App.ACCENT)
        e.pack(fill="x", ipady=5)
        return e

    def flat_button(self, parent, text, command):
        b = tk.Label(parent, text=text, font=(App.FONT, 9, "bold"), fg=App.ACCENT_TEXT,
                     bg=App.ACCENT_BG, padx=14, pady=6, cursor="hand2",
                     highlightthickness=1, highlightbackground=App.ACCENT_BG,
                     highlightcolor=App.ACCENT_BG)
        b.bind("<Button-1>", lambda _e: command())
        b.bind("<Enter>", lambda _e: b.configure(bg=App.ACCENT_BG_HOVER,
                                                 highlightbackground=App.ACCENT_LINE))
        b.bind("<Leave>", lambda _e: b.configure(bg=App.ACCENT_BG,
                                                 highlightbackground=App.ACCENT_BG))
        return b

    def hint(self, parent, text="", color=None):
        lbl = tk.Label(parent, text=text, font=(App.FONT, 8), fg=color or App.SUB,
                       bg=App.CARD, anchor="w", justify="left", wraplength=self.wrap_w)
        lbl.pack(fill="x", pady=(4, 0))
        return lbl

    # ----- tabs -----
    def build_tab_bar(self):
        bar = tk.Frame(self, bg=App.BG)
        bar.pack(fill="x", padx=22, pady=(16, 10))
        for i, (key, text) in enumerate(self.TABS):
            bar.columnconfigure(i, weight=1, uniform="tabs")    # equal widths, fill the window
            cell = tk.Frame(bar, bg=App.BG, cursor="hand2")
            cell.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0))
            lbl = tk.Label(cell, text=text, font=(App.FONT, 11, "bold"), fg=App.SUB,
                           bg=App.BG, cursor="hand2")
            lbl.pack(fill="x")
            line = tk.Frame(cell, height=3, bg=App.BG)
            line.pack(fill="x", pady=(5, 0))
            for w in (cell, lbl):
                w.bind("<Button-1>", lambda _e, k=key: self.show_tab(k))
            self.tab_widgets[key] = (lbl, line)

    def show_tab(self, key):
        self.cancel_capture()
        prev = getattr(self, "cur_tab", None)
        job = getattr(self, "tab_job", None)
        if job:                              # a switch still animating: finish it at once
            try:
                self.after_cancel(job)
                self.canvas.coords(self.inner_id, 0, 0)
            except tk.TclError:
                pass
            self.tab_job = None
        animate = animations_on() and prev in self.pages and prev != key
        if not animate:
            for k, (lbl, line) in self.tab_widgets.items():
                on = k == key
                lbl.configure(fg=App.TEXT if on else App.SUB)
                line.configure(bg=App.ACCENT if on else App.BG)
        self.pages[key].tkraise()
        self.cur_tab = key
        self.canvas.yview_moveto(0)          # every page starts at its top
        self.on_size()                       # scroll extent / scrollbar follow this page
        self.say("")
        if animate:
            self.animate_tab(prev, key)

    def animate_tab(self, old, new, steps=9, delay=14, dx=18):
        """Tab switch: the new page slides in a few pixels while the tab names / underlines
        fade between their two colours."""
        def paint(k):
            for name, (lbl, line) in self.tab_widgets.items():
                if name == new:
                    lbl.configure(fg=mix_color(App.SUB, App.TEXT, k))
                    line.configure(bg=mix_color(App.BG, App.ACCENT, k))
                elif name == old:
                    lbl.configure(fg=mix_color(App.TEXT, App.SUB, k))
                    line.configure(bg=mix_color(App.ACCENT, App.BG, k))
                else:
                    lbl.configure(fg=App.SUB)
                    line.configure(bg=App.BG)
            self.canvas.coords(self.inner_id, round(dx * (1 - k)), 0)

        def step(i=1):
            try:
                paint(_ease_out(min(1.0, i / steps)))
            except (tk.TclError, ValueError):
                self.tab_job = None
                return
            self.tab_job = self.after(delay, step, i + 1) if i < steps else None
        try:
            paint(0)
        except (tk.TclError, ValueError):
            return
        step()

    # ----- keybinds tab -----
    def build_keys(self, page):
        c = self.card(page, "HOTKEYS", "Work even while Roblox is focused.")
        for bind_id, (label, _default) in HOTKEY_BINDS.items():
            self.key_row(c, bind_id, label)
        c = self.card(page, "IN-GAME KEYS",
                      "Set these to match your keybinds in Roblox.")
        for bind_id, (label, _default) in GAME_BINDS.items():
            self.key_row(c, bind_id, label)
        reset = tk.Label(c, text="Reset all keybinds to default", font=(App.FONT, 9),
                         fg=App.SUB, bg=App.CARD, cursor="hand2")
        reset.pack(anchor="e", pady=(8, 0))
        reset.bind("<Button-1>", lambda _e: self.reset_keybinds())
        reset.bind("<Enter>", lambda _e: reset.configure(fg=App.TEXT))
        reset.bind("<Leave>", lambda _e: reset.configure(fg=App.SUB))
        self.build_monitor_card(page)

    def build_monitor_card(self, page):
        """More than one screen: pick the monitor the macro looks at and clicks on."""
        mons = list_monitors()
        c = self.card(page, "MONITOR",
                      "Using more than one screen? Pick the monitor Roblox is on - the macro "
                      "only looks at and clicks on that one.")
        if not mons:
            self.hint(c, "Could not read your monitors.", "#f87171")
            return
        items = []
        for i, m in enumerate(mons, 1):
            label = f"Monitor {i}  \u00b7  {m['width']}\u00d7{m['height']}"
            if m["left"] == 0 and m["top"] == 0:
                label += "  \u00b7  main"
            items.append((i, label, (str(i), "#10223d", "#60a5fa"), None))
        cur = settings["monitor"] if settings["monitor"] <= len(mons) else 1
        self.monitor_dd = ModernDropdown(c, items, cur, self.on_monitor_pick)
        self.monitor_dd.pack(fill="x")
        self.hint(c, "Change it before pressing Start. After switching monitors, pick the "
                     "failsafe spot again (Configs > Failsafe).")

    def on_monitor_pick(self, key):
        if running():
            self.say("Stop the macro before changing the monitor.", error=True)
            self.monitor_dd.value = min(settings["monitor"], len(self.monitor_dd.order))
            self.monitor_dd.render_trigger()
            return
        settings["monitor"] = int(key)
        apply_settings()
        save_stats()
        self.say(f"Monitor {key} selected", good=True)

    def key_row(self, parent, bind_id, label):
        row = tk.Frame(parent, bg=App.CARD)
        row.pack(fill="x", pady=4)
        tk.Label(row, text=label, font=(App.FONT, 10), fg=App.TEXT,
                 bg=App.CARD).pack(side="left")
        btn = KeyButton(row, settings["keybinds"][bind_id],
                        lambda b=bind_id: self.start_capture(b))
        btn.pack(side="right")
        self.key_buttons[bind_id] = btn

    def refresh_key_buttons(self):
        for bind_id, btn in self.key_buttons.items():
            btn.show_key(settings["keybinds"][bind_id])

    def start_capture(self, bind_id):
        if running():
            self.say("Stop the macro before changing keybinds.", error=True)
            return
        self.cancel_capture()
        self.capturing = bind_id
        self.key_buttons[bind_id].show_capturing()
        unregister_hotkeys()    # so pressing e.g. F8 here doesn't start the macro
        self.capture_hook = keyboard.on_press(
            lambda e: ev.put(("captured", e.name, bool(getattr(e, "is_keypad", False)))))
        self.say(f"Press the new key for {BIND_LABELS[bind_id]} (Esc to cancel)")

    def cancel_capture(self):
        if self.capturing is None:
            return
        if self.capture_hook is not None:
            try:
                keyboard.unhook(self.capture_hook)
            except (KeyError, ValueError):
                pass
            self.capture_hook = None
        self.capturing = None
        self.refresh_key_buttons()
        register_hotkeys()

    def on_captured(self, name, keypad):
        """A key was pressed while a button was waiting for one."""
        bind_id = self.capturing
        if bind_id is None:
            return
        self.cancel_capture()
        name = name.lower()
        if name == "esc":
            self.say("Cancelled")
            return
        if bind_id in GAME_BINDS:
            key = to_game_key(name, keypad)
            if key is None:
                self.say(f"'{name}' can't be pressed by the macro - try another key.",
                         error=True)
                return
        else:
            key = name
        for other, value in settings["keybinds"].items():
            if other != bind_id and value == key:
                self.say(f"{key.upper()} is already used by {BIND_LABELS[other]}.", error=True)
                return
        settings["keybinds"][bind_id] = key
        self.keybinds_changed()
        self.say(f"{BIND_LABELS[bind_id]} = {key.upper()}", good=True)

    def reset_keybinds(self):
        if running():
            self.say("Stop the macro before changing keybinds.", error=True)
            return
        self.cancel_capture()
        settings["keybinds"] = dict(DEFAULT_KEYBINDS)
        self.keybinds_changed()
        self.say("Keybinds reset to default", good=True)

    def keybinds_changed(self):
        apply_settings()
        register_hotkeys()
        save_stats()
        self.refresh_key_buttons()
        self.app.refresh_hotkeys()

    # ----- discord tab -----
    def build_discord(self, page):
        wh = settings["webhook"]
        self.v_url = tk.StringVar(value=wh["url"])
        self.v_mention = tk.BooleanVar(value=wh["mention"])
        self.v_uid = tk.StringVar(value=wh["user_id"])
        self.v_hourly = tk.BooleanVar(value=wh["hourly_graph"])
        self.v_hours = tk.StringVar(value=str(wh["graph_hours"]))
        self.v_mission = tk.BooleanVar(value=wh["notify_mission"])
        self.v_error = tk.BooleanVar(value=wh["notify_error"])
        self.v_bot = tk.BooleanVar(value=wh["use_bot"])
        self.v_token = tk.StringVar(value=wh["bot_token"])
        self.v_channel = tk.StringVar(value=wh["channel_id"])
        self.v_show_token = tk.BooleanVar(value=False)

        c = self.card(page, "CONNECTION",
                      "Send the Discord messages with a webhook, or with a bot - a bot can also be "
                      "controlled from Discord with commands like !start, !pause and !stop.")
        self.check(c, "Use a bot instead of a webhook", self.v_bot, self.on_bot_toggle)

        c = self.card(page, "WEBHOOK", "Optional. Leave the URL empty to keep Discord off.")
        self.wh_outer = c.master
        self.url_entry = self.entry(c, self.v_url, show="\u2022")
        self.hint_url = self.hint(c, "", "#f87171")
        row = tk.Frame(c, bg=App.CARD)
        row.pack(fill="x", pady=(6, 0))
        self.v_show = tk.BooleanVar(value=False)
        show = tk.Frame(row, bg=App.CARD)
        show.pack(side="left")
        sw = ToggleSwitch(show, self.v_show, self.toggle_show, parent_bg=App.CARD)
        sw.pack(side="left")
        lbl = tk.Label(show, text="Show URL", font=(App.FONT, 9), fg=App.SUB, bg=App.CARD,
                       cursor="hand2")
        lbl.pack(side="left", padx=(8, 0))
        lbl.bind("<Button-1>", sw.flip)
        self.flat_button(row, "Send test message", self.test_webhook).pack(side="right")
        self.hint(c, "By Hemo")

        c = self.card(page, "BOT", "Your bot posts the messages and listens for commands in one channel.")
        self.bot_outer = c.master
        for label, var, secret in (("Bot token", self.v_token, True),
                                   ("Channel ID", self.v_channel, False),
                                   ("Your user ID  (only you can use the commands)", self.v_uid, False)):
            tk.Label(c, text=label, font=(App.FONT, 9), fg=App.SUB, bg=App.CARD,
                     anchor="w").pack(fill="x", pady=(8, 2))
            e = self.entry(c, var, show="\u2022" if secret else None)
            if secret:
                self.token_entry = e
        self.hint_bot = self.hint(c, "", "#f87171")
        row = tk.Frame(c, bg=App.CARD)
        row.pack(fill="x", pady=(6, 0))
        show = tk.Frame(row, bg=App.CARD)
        show.pack(side="left")
        sw = ToggleSwitch(show, self.v_show_token, self.toggle_show_token, parent_bg=App.CARD)
        sw.pack(side="left")
        lbl = tk.Label(show, text="Show token", font=(App.FONT, 9), fg=App.SUB, bg=App.CARD,
                       cursor="hand2")
        lbl.pack(side="left", padx=(8, 0))
        lbl.bind("<Button-1>", sw.flip)
        self.flat_button(row, "Send test message", self.test_webhook).pack(side="right")
        self.hint(c, "Commands:  " + BOT_COMMANDS_TEXT + "\n"
                     "Setup: discord.com/developers > New Application > Bot. Turn on Message Content "
                     "Intent, copy the token, then invite the bot to your server with View Channel, "
                     "Send Messages, Attach Files and Read Message History. Channel ID: Developer "
                     "Mode on, right-click the channel > Copy Channel ID.")

        c = self.card(page, "MESSAGES")
        self.msgs_outer = c.master
        self.check(c, "Post my stats + a screenshot after every mission / raid", self.v_mission,
                   self.on_toggle)
        self.check(c, "Message me when the macro hits an error", self.v_error, self.on_toggle)
        self.check(c, "Mention me in error messages", self.v_mention, self.on_toggle)
        row = tk.Frame(c, bg=App.CARD)
        row.pack(fill="x", pady=(4, 0))
        tk.Label(row, text="User ID", font=(App.FONT, 10), fg=App.TEXT,
                 bg=App.CARD).pack(side="left", padx=(0, 10))
        inner = tk.Frame(row, bg=App.CARD)
        inner.pack(side="left", fill="x", expand=True)
        self.uid_entry = self.entry(inner, self.v_uid)
        self.hint_uid = self.hint(c, self.UID_HELP)

        c = self.card(page, "STATS GRAPH")
        self.check(c, "Send a stats graph on an interval", self.v_hourly, self.on_toggle)
        row = tk.Frame(c, bg=App.CARD)
        row.pack(fill="x", pady=(4, 0))
        tk.Label(row, text="Every", font=(App.FONT, 10), fg=App.TEXT,
                 bg=App.CARD).pack(side="left", padx=(0, 8))
        self.hours_spin = tk.Spinbox(row, from_=1, to=MAX_GRAPH_HOURS, width=4,
                                     textvariable=self.v_hours, command=self.save_soon,
                                     font=(App.FONT, 10), justify="center", bg=App.LOGBG,
                                     fg=App.TEXT, insertbackground=App.TEXT,
                                     buttonbackground=App.FIELD, relief="flat",
                                     highlightthickness=1, highlightbackground=App.BORDER,
                                     highlightcolor=App.ACCENT)
        self.hours_spin.pack(side="left", ipady=3)
        tk.Label(row, text="hour(s)", font=(App.FONT, 10), fg=App.TEXT,
                 bg=App.CARD).pack(side="left", padx=(8, 0))
        self.flat_button(row, "Send test graph", self.test_graph).pack(side="right")
        self.hint(c, "Sent while the macro is running. It never pings you.")

        for var in (self.v_url, self.v_uid, self.v_hours, self.v_token, self.v_channel):
            var.trace_add("write", lambda *_: self.save_soon())
        self.update_dependents()
        self.show_bot_cards(redraw=False)

    def toggle_show(self):
        self.url_entry.configure(show="" if self.v_show.get() else "\u2022")

    def toggle_show_token(self):
        self.token_entry.configure(show="" if self.v_show_token.get() else "\u2022")

    def show_bot_cards(self, redraw=True):
        """Bot switch on: the BOT card replaces the WEBHOOK card."""
        on = bool(self.v_bot.get())
        self.wh_outer.pack_forget()
        self.bot_outer.pack_forget()
        (self.bot_outer if on else self.wh_outer).pack(fill="x", pady=6, before=self.msgs_outer)
        if redraw:
            self.on_size()

    def on_bot_toggle(self):
        self.show_bot_cards()
        self.commit()

    def update_dependents(self):
        self.uid_entry.configure(state="normal" if self.v_mention.get() else "disabled")

    def on_toggle(self):
        self.update_dependents()
        self.commit()

    def test_webhook(self):
        self.commit()
        if not webhook_url():
            self.say("Fill in the bot token and the channel ID first." if self.v_bot.get()
                     else "Paste a valid webhook URL first.", error=True)
            return
        self.say("Sending test message...")

        def work():
            try:
                png = cv2.imencode(".png", grab_roblox())[1].tobytes()
                embed = dict(mission_embed(("completed",), set()),
                             title="Test message  ·  the webhook works")
                ok, msg = post_webhook("Test message from ymir - the webhook works.",
                                       png=png, embed=embed)
            except Exception as e:  # noqa: BLE001
                ok, msg = False, f"Could not take the screenshot: {type(e).__name__}: {e}"
            ev.put(("settings_msg", "Test message sent" if ok else msg, not ok, ok))
        threading.Thread(target=work, daemon=True).start()

    def test_graph(self):
        self.commit()
        if not webhook_url():
            self.say("Fill in the bot token and the channel ID first." if self.v_bot.get()
                     else "Paste a valid webhook URL first.", error=True)
            return
        self.say("Building the graph...")

        def work():
            try:
                ok, msg = send_stats_graph("Test graph", partial=True)
            except Exception as e:  # noqa: BLE001
                ok, msg = False, f"Could not build the graph: {type(e).__name__}: {e}"
            ev.put(("settings_msg", "Test graph sent" if ok else msg, not ok, ok))
        threading.Thread(target=work, daemon=True).start()

    # ----- configs tab -----
    def build_family(self, page):
        # section 1: abilities (the family)
        self.v_family = tk.StringVar(value=settings["family"])
        c = self.card(page, "ABILITIES")
        dd_row = tk.Frame(c, bg=App.CARD)
        dd_row.pack(fill="x")
        self.family_dd = ModernDropdown(dd_row, family_items(), settings["family"],
                                        self.on_family_pick, provider=family_items)
        self.family_dd.pack(side="left", fill="x", expand=True)
        self.add_macro_btn = self.square_button(dd_row, "+", self.open_macro_editor)
        self.add_macro_btn.pack(side="left", fill="y", padx=(8, 0))
        self.edit_macro_btn = self.square_button(dd_row, "Edit", self.edit_current_macro)
        self.update_edit_btn()
        btn_row = tk.Frame(c, bg=App.CARD)
        btn_row.pack(anchor="w", pady=(10, 0))
        self.flat_button(btn_row, "Edit configs", self.open_config_window).pack(side="left")
        self.book_img = book_icon(App.ACCENT_TEXT, max(14, round(15 * max(1.0, self.winfo_fpixels("1i") / 96))))
        guide_btn = self.flat_button(btn_row, " Guide" if self.book_img else "Guide",
                                     self.open_guide_window)
        if self.book_img is not None:
            guide_btn.configure(image=self.book_img, compound="left")
        guide_btn.pack(side="left", padx=(8, 0))
        self.hint(c, "Every timing of the macro: movement and the three ability configs.")
        self.hint(c, "+ makes your own macro: write it as a pseudo-script or record it. It is saved as "
                     "a .ymir file in Documents/Ymir Macro/Custom-Macros - send that file to share it.")

        # section 2: movement while shifted (step 9)
        self.v_move_shifted = tk.StringVar(value=settings["move_shifted"])
        ms = self.card(page, "MOVE WHILE SHIFTED",
                       "The direction you move in after shifting (while spamming ability 5). "
                       f"Left holds {KEY_STRAFE.upper()}, Right holds {KEY_STRAFE_D.upper()}, "
                       "Stand holds nothing.")
        ms_items = [(o, o, MOVE_SHIFTED_BADGES[o], None) for o in MOVE_SHIFTED_OPTIONS]
        self.move_shifted_dd = ModernDropdown(ms, ms_items, settings["move_shifted"],
                                              self.on_move_shifted_pick)
        self.move_shifted_dd.pack(fill="x")

        # section 3: failsafe (stuck-mission rescue) - or, while Female Raid is picked, the chest
        # spots. One slot that is rebuilt when the config changes (the variables live on).
        fs = settings["failsafe"]
        self.v_fs_min = tk.StringVar(value=str(fs["minutes"]))
        self.v_fs_sec = tk.StringVar(value=str(fs["seconds"]))
        self.v_fs_x = tk.StringVar(value="" if fs["click_x"] is None else str(fs["click_x"]))
        self.v_fs_y = tk.StringVar(value="" if fs["click_y"] is None else str(fs["click_y"]))
        self.v_fs_look = tk.BooleanVar(value=bool(fs["look_down"]))
        for var in (self.v_fs_min, self.v_fs_sec, self.v_fs_x, self.v_fs_y):
            var.trace_add("write", lambda *_: self.save_soon())

        self.v_chest = {}
        for key in ("chest1", "chest2"):
            cur = tv("Female Raid", key)
            vx = tk.StringVar(value="" if cur is None else str(cur[0]))
            vy = tk.StringVar(value="" if cur is None else str(cur[1]))
            for v in (vx, vy):
                v.trace_add("write", lambda *_: self.save_soon())
            self.v_chest[key] = (vx, vy)
        self._chest_saved = {k: tv("Female Raid", k) for k in self.v_chest}
        self.fs_slot = tk.Frame(page, bg=page.cget("bg"))
        self.fs_slot.pack(fill="x")
        self.build_fs_slot()

        # section 4: fix image detection (replace the macro's pictures with your own)
        a = self.card(page, "FIX IMAGE DETECTION",
                      "Detection not working? Replace the macro's pictures with your own screenshots.",
                      hot=True)
        self.asset_open_btn = self.flat_button(a, "Open", self.open_asset_window)
        self.asset_open_btn.pack(anchor="w")
        tk.Frame(a, height=10, bg=App.CARD).pack()
        self.v_fix_loot = tk.BooleanVar(value=bool(settings.get("fix_loot")))
        row = tk.Frame(a, bg=App.CARD)
        row.pack(fill="x", pady=(0, 2))
        sw = ToggleSwitch(row, self.v_fix_loot, self.on_fix_loot, parent_bg=App.CARD)
        sw.pack(side="right")
        lbl = tk.Label(row, text="Fix loot tracking", font=(App.FONT, 10), fg=App.TEXT,
                       bg=App.CARD, anchor="w", cursor="hand2")
        lbl.pack(side="left")
        lbl.bind("<Button-1>", sw.flip)
        tk.Label(row, text="Beta", font=(App.FONT, 7, "bold"), fg=ModernDropdown.BETA_FG,
                 bg=ModernDropdown.BETA_BG, padx=6, pady=1).pack(side="left", padx=(8, 0))
        self.hint(a, "Some UIs let you resize the loot drops, so the loot pictures no longer match. "
                     "When on, every loot icon is looked for at many sizes. Costs a little extra time "
                     "when the mission ends. Off by default.", App.TEXT)

    def on_fix_loot(self):
        settings["fix_loot"] = bool(self.v_fix_loot.get())
        save_stats()

    # ----- custom macros (the "+" next to the config dropdown) -----
    def square_button(self, parent, text, command):
        """A small button that looks like the dropdown next to it."""
        one = len(text) == 1
        b = tk.Label(parent, text=text, font=(App.FONT, 15 if one else 9, "bold"),
                     fg=ModernDropdown.ACCENT, bg=ModernDropdown.BG, width=3 if one else 5,
                     cursor="hand2", highlightthickness=1,
                     highlightbackground=ModernDropdown.EDGE, highlightcolor=ModernDropdown.EDGE)
        b.bind("<Button-1>", lambda _e: command())
        b.bind("<Enter>", lambda _e: b.configure(highlightbackground=ModernDropdown.ACCENT))
        b.bind("<Leave>", lambda _e: b.configure(highlightbackground=ModernDropdown.EDGE))
        return b

    def update_edit_btn(self):
        """The Edit button only exists while one of your own macros is picked."""
        if is_custom(self.v_family.get()):
            self.edit_macro_btn.pack(side="left", fill="y", padx=(8, 0))
        else:
            self.edit_macro_btn.pack_forget()

    def open_macro_editor(self, key=None):
        win = getattr(self, "macro_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.deiconify()
                win.lift()
                win.focus_force()
                return
        except tk.TclError:
            pass
        self.macro_win = MacroEditorWindow(self, key if is_custom(key) else None)

    def edit_current_macro(self):
        self.open_macro_editor(self.v_family.get())

    def macro_saved(self, key):
        """The editor saved a macro: list it and pick it."""
        self.v_family.set(key)
        self.family_dd.set_items(family_items(), key)
        self.commit()
        self.update_edit_btn()
        self.build_fs_slot()

    def macro_deleted(self, key):
        if self.v_family.get() == key:
            self.v_family.set(DEFAULT_FAMILY)
        self.family_dd.set_items(family_items(), self.v_family.get())
        self.commit()
        self.update_edit_btn()
        self.build_fs_slot()

    def build_fs_slot(self):
        """The chest spots card (Female Raid, or a custom macro that opens chests). The failsafe
        itself lives in the Recovery tab."""
        for w in self.fs_slot.winfo_children():
            w.destroy()
        fam = settings["family"]
        if fam == "Female Raid" or (is_custom(fam) and (
                custom_macro_type(fam) == "raid" or custom_uses_chest(fam))):
            self.build_chest_card(self.fs_slot)         # openchest() clicks these spots
        else:
            # Tk keeps the old size of a frame whose last child was destroyed - that left an empty
            # gap where the chest card was. Ask for a 1 px frame again.
            self.fs_slot.configure(height=1)
        try:
            self.after_idle(self.on_size)               # the page / scroll height follows
        except tk.TclError:
            pass

    def build_recovery(self, page):
        """Recovery tab: what happens when something goes wrong - the failsafe and Reconnect."""
        self.build_failsafe_card(page)
        self.build_reconnect_card(page)

    def build_reconnect_card(self, parent):
        f = self.card(parent, "RECONNECT",
                      "Rejoins the game and clicks back into a mission / raid when you disconnect, "
                      "when the failsafe has been rescuing for 2 minutes, or on the bot command "
                      "!reconnect. Off by default - Configure it first.")
        row = tk.Frame(f, bg=App.CARD)
        row.pack(fill="x", pady=(10, 0))
        self.reconnect_status = tk.Label(row, text="", font=(App.FONT, 10), anchor="w",
                                         bg=App.CARD)
        self.reconnect_status.pack(side="left")
        self.flat_button(row, "Configure", self.open_reconnect_config).pack(side="right")
        self.update_reconnect_status()

    def update_reconnect_status(self):
        lbl = getattr(self, "reconnect_status", None)
        if lbl is None:
            return
        on = settings["reconnect"]["enabled"]
        incomplete = on and bool(reconnect_missing())     # e.g. the macro was switched to Raids
        try:
            if incomplete:
                lbl.configure(text="Reconnect: Off - fill in the " + reconnect_macro_tab()
                                   + " tab for this macro", fg="#fbbf24")
            else:
                lbl.configure(text="Reconnect: On" if on else "Reconnect: Off",
                              fg="#34d399" if on else App.SUB)
        except tk.TclError:
            pass

    def open_reconnect_config(self):
        win = getattr(self, "rc_cfg_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.lift()
                win.focus_force()
                return
        except tk.TclError:
            pass
        self.rc_cfg_win = ReconnectConfigWindow(self)

    def build_failsafe_card(self, parent):
        f = self.card(parent, "FAILSAFE",
                      "If a mission lasts longer than the time below, the macro spams ability 2, "
                      "waits 15s and clicks until the retry button shows up. Female Raid has its "
                      "own 3 min raid failsafe (the wait time below isn't used there).")

        def spin(parent, var, hi, unit):
            sp = tk.Spinbox(parent, from_=0, to=hi, width=4, textvariable=var,
                            command=self.save_soon, font=(App.FONT, 10), justify="center",
                            bg=App.LOGBG, fg=App.TEXT, insertbackground=App.TEXT,
                            buttonbackground=App.FIELD, relief="flat", highlightthickness=1,
                            highlightbackground=App.BORDER, highlightcolor=App.ACCENT)
            sp.pack(side="left", ipady=3)
            tk.Label(parent, text=unit, font=(App.FONT, 10), fg=App.TEXT,
                     bg=App.CARD).pack(side="left", padx=(6, 14))

        row = tk.Frame(f, bg=App.CARD)
        row.pack(fill="x", pady=(0, 2))
        tk.Label(row, text="Wait time", font=(App.FONT, 10), fg=App.TEXT,
                 bg=App.CARD).pack(side="left", padx=(0, 10))
        spin(row, self.v_fs_min, MAX_FAILSAFE_MINUTES, "min")
        spin(row, self.v_fs_sec, 59, "s")
        self.hint(f, f"How long a mission can run before the failsafe triggers "
                     f"(default 2 min 0 s, minimum 1 min 30 s).")

        row = tk.Frame(f, bg=App.CARD)
        row.pack(fill="x", pady=(10, 2))
        tk.Label(row, text="Click at", font=(App.FONT, 10), fg=App.TEXT,
                 bg=App.CARD).pack(side="left", padx=(0, 10))
        for label, var in (("X", self.v_fs_x), ("Y", self.v_fs_y)):
            tk.Label(row, text=label, font=(App.FONT, 9), fg=App.SUB,
                     bg=App.CARD).pack(side="left", padx=(0, 4))
            e = tk.Entry(row, textvariable=var, width=6, font=(App.FONT, 10), justify="center",
                         bg=App.LOGBG, fg=App.TEXT, insertbackground=App.TEXT, relief="flat",
                         highlightthickness=1, highlightbackground=App.BORDER,
                         highlightcolor=App.ACCENT)
            e.pack(side="left", ipady=3, padx=(0, 10))
        row2 = tk.Frame(f, bg=App.CARD)
        row2.pack(fill="x", pady=(6, 0))
        self.flat_button(row2, "Pick spot", self.pick_failsafe_spot).pack(side="left")
        reset = tk.Label(row2, text="Use default (bottom center)", font=(App.FONT, 9), fg=App.SUB,
                         bg=App.CARD, cursor="hand2")
        reset.pack(side="left", padx=(14, 0))
        reset.bind("<Button-1>", lambda _e: self.clear_failsafe_spot())
        reset.bind("<Enter>", lambda _e: reset.configure(fg=App.TEXT))
        reset.bind("<Leave>", lambda _e: reset.configure(fg=App.SUB))
        self.hint(f, "Where the failsafe clicks. Empty = middle of the X axis, 83% down the Y axis; "
                     "a picked coordinate overrides it. Tip: pick a spot near the HP bar.")

        row = tk.Frame(f, bg=App.CARD)
        row.pack(fill="x", pady=(14, 2))
        tk.Label(row, text="Look down", font=(App.FONT, 10), fg=App.TEXT, bg=App.CARD,
                 anchor="w").pack(side="left")
        self.flat_button(row, "Configure", self.open_failsafe_config).pack(side="right")
        tb = self.flat_button(row, "Test", self.toggle_failsafe_test)
        tb.pack(side="right", padx=(0, 8))
        self.fs_test_btns = [tb]
        self.hint(f, "Before clicking: press ctrl, move the mouse down, press ctrl again (off by "
                     "default). Configure turns it on and sets the time / pixels. Test runs only the "
                     "look down + clicking part, 3s after you press it - click into Roblox first.")
        self.hint(f, "Adaptation not recommended", color=App.SUB).configure(font=(App.FONT, 7))

    def build_chest_card(self, parent):
        """Female Raid: where the chests are on the screen (the same two coordinates as
        the Female Raid timings)."""
        c = self.card(parent, "CHEST LOCATIONS",
                      "Where the raid clicks to open the chests. Click Pick spot, then click on the "
                      "chest on your screen.")
        title = c.winfo_children()[0]               # the card title: put a yellow ! next to it
        head = tk.Frame(c, bg=App.CARD)
        head.pack(fill="x", before=title)
        title.pack_forget()
        title.pack(in_=head, side="left")
        self.chest_warn = tk.Label(head, text="!", font=(App.FONT, 10, "bold"), fg="#facc15",
                                   bg=App.CARD)
        self.chest_warn.pack(side="left", padx=(6, 0))
        self.update_chest_warning()
        for key, label in (("chest1", "Chest 1"), ("chest2", "Chest 2")):
            vx, vy = self.v_chest[key]
            row = tk.Frame(c, bg=App.CARD)
            row.pack(fill="x", pady=(8, 2))
            tk.Label(row, text=label, font=(App.FONT, 10), fg=App.TEXT,
                     bg=App.CARD).pack(side="left", padx=(0, 10))
            for axis, var in (("X", vx), ("Y", vy)):
                tk.Label(row, text=axis, font=(App.FONT, 9), fg=App.SUB,
                         bg=App.CARD).pack(side="left", padx=(0, 4))
                e = tk.Entry(row, textvariable=var, width=6, font=(App.FONT, 10), justify="center",
                             bg=App.LOGBG, fg=App.TEXT, insertbackground=App.TEXT, relief="flat",
                             highlightthickness=1, highlightbackground=App.BORDER,
                             highlightcolor=App.ACCENT)
                e.pack(side="left", ipady=3, padx=(0, 10))
            row2 = tk.Frame(c, bg=App.CARD)
            row2.pack(fill="x", pady=(4, 0))
            self.flat_button(row2, "Pick spot",
                             lambda k=key, l=label: self.pick_chest_spot(k, l)).pack(side="left")
            clear = tk.Label(row2, text="Clear", font=(App.FONT, 9), fg=App.SUB, bg=App.CARD,
                             cursor="hand2")
            clear.pack(side="left", padx=(14, 0))
            clear.bind("<Button-1>", lambda _e, k=key, l=label: self.clear_chest_spot(k, l))
            clear.bind("<Enter>", lambda _e, w=clear: w.configure(fg=App.TEXT))
            clear.bind("<Leave>", lambda _e, w=clear: w.configure(fg=App.SUB))
        self.hint(c, "Chest 1 is needed for the raid. Whether chest 2 is opened too is set in "
                     "Edit configs > Female Raid.")

    def update_chest_warning(self):
        """Yellow ! next to CHEST LOCATIONS while the Chest 1 coordinate is not provided."""
        lbl = getattr(self, "chest_warn", None)
        if lbl is None:
            return
        try:
            vx, vy = self.v_chest["chest1"]
            valid, val = parse_spot_text(CONFIG_INDEX["Female Raid"]["chest1"], [vx.get(), vy.get()])
            if valid and val is not None:
                lbl.configure(text="")
            else:
                lbl.configure(text="!")
        except tk.TclError:
            pass

    def pick_chest_spot(self, key, label):
        def picked(x, y):
            self.v_chest[key][0].set(str(x))
            self.v_chest[key][1].set(str(y))
            self.commit()
            self.say(f"{label} set to {x}, {y}", good=True)
        pick_screen_spot(self, f"Click on {label}   (Esc to cancel)", picked)

    def clear_chest_spot(self, key, label):
        self.v_chest[key][0].set("")
        self.v_chest[key][1].set("")
        self.commit()
        self.say(f"{label} cleared", good=True)

    def commit_chests(self):
        """Save the chest spots into the Female Raid timings (same place as Edit configs). Only a
        changed spot is written, so an older value in these boxes never overwrites a newer one
        that was set in Edit configs."""
        store = settings["timings"].setdefault("Female Raid", {})
        changed = False
        for key, (vx, vy) in self.v_chest.items():
            spec = CONFIG_INDEX["Female Raid"][key]
            valid, val = parse_spot_text(spec, [vx.get(), vy.get()])
            if not valid or val == self._chest_saved[key]:
                continue
            self._chest_saved[key] = val
            if val == spec[3]:
                store.pop(key, None)
            else:
                store[key] = val
            changed = True
        if not store:
            settings["timings"].pop("Female Raid", None)
        self.update_chest_warning()
        if not changed:
            return
        save_timings()

    def open_asset_window(self, tab=None):
        """tab = "UI" / "Loot" / "Macros": open straight on that tab (the macro editor's Images button)."""
        win = getattr(self, "asset_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.deiconify()
                win.lift()
                win.focus_force()
                if tab:
                    win.set_tab(tab, force=True)
                return
        except tk.TclError:
            pass
        self.asset_win = AssetOverrideWindow(self)
        if tab:
            self.asset_win.set_tab(tab, force=True)

    def open_guide_window(self):
        fam = self.v_family.get()
        if is_custom(fam):              # the "guide" of your own macro is its script
            self.open_macro_editor(fam)
            return
        win = getattr(self, "guide_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.set_family(fam)
                win.deiconify()
                win.lift()
                win.focus_force()
                return
        except tk.TclError:
            pass
        self.guide_win = GuideWindow(self, fam)

    def open_config_window(self):
        win = getattr(self, "config_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.deiconify()
                win.lift()
                win.focus_force()
                return
        except tk.TclError:
            pass
        self.config_win = ConfigEditorWindow(self)

    def pick_failsafe_spot(self):
        """Tint the screen, show the live coordinates next to the cursor, and store the spot of
        the next click as the failsafe click."""
        def picked(x, y):
            self.v_fs_x.set(str(x))
            self.v_fs_y.set(str(y))
            self.commit()
            self.say(f"Failsafe click spot set to {x}, {y}", good=True)
        pick_screen_spot(self, "Click where the failsafe should click   (Esc to cancel)", picked)

    def open_failsafe_config(self):
        win = getattr(self, "fs_cfg_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.lift()
                win.focus_force()
                return
        except tk.TclError:
            pass
        self.fs_cfg_win = FailsafeConfigWindow(self)

    def fs_say(self, msg, error=False, good=False):
        self.say(msg, error=error, good=good)
        win = getattr(self, "fs_cfg_win", None)
        try:
            if win is not None and win.winfo_exists():
                win.say(msg, error=error, good=good)
        except tk.TclError:
            pass

    def set_fs_test_buttons(self, running):
        btns = list(getattr(self, "fs_test_btns", []))
        win = getattr(self, "fs_cfg_win", None)
        try:
            if win is not None and win.winfo_exists():
                btns.append(win.test_btn)
        except tk.TclError:
            pass
        for b in btns:
            try:
                b.configure(text="Stop test" if running else "Test")
            except tk.TclError:
                pass

    def toggle_failsafe_test(self):
        """Test the failsafe's look down + clicking part only (press again to stop it)."""
        if failsafe_test_running():
            failsafe_test_stop()
            return
        if worker is not None and worker.is_alive():
            self.fs_say("Stop the macro first, then test the failsafe", error=True)
            return
        self.commit()
        failsafe_test_start()
        self.set_fs_test_buttons(True)
        self.after(100, self.poll_failsafe_test)

    def poll_failsafe_test(self):
        try:
            while True:
                msg, kind = _fs_test["msgs"].get_nowait()
                if kind == "done":
                    self.set_fs_test_buttons(False)
                    return
                self.fs_say(msg, error=(kind == "error"), good=(kind == "good"))
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if failsafe_test_running() or not _fs_test["msgs"].empty():
            self.after(100, self.poll_failsafe_test)
        else:
            self.set_fs_test_buttons(False)

    def clear_failsafe_spot(self):
        self.v_fs_x.set("")
        self.v_fs_y.set("")
        self.commit()
        self.say("Failsafe will click the default spot (X centre, 83% Y)", good=True)

    def on_move_shifted_pick(self, key):
        self.v_move_shifted.set(key)
        self.commit()

    def on_family_pick(self, key):
        self.v_family.set(key)
        self.commit()
        self.build_fs_slot()
        self.update_edit_btn()
        win = getattr(self, "guide_win", None)      # an open Guide window follows the pick
        try:
            if win is not None and win.winfo_exists() and not is_custom(key):
                win.set_family(key)
        except tk.TclError:
            pass

    # ----- themes tab -----
    def build_themes(self, page):
        c = self.card(page, "THEME", "Pick a look - the whole app updates instantly.")
        grid = tk.Frame(c, bg=App.CARD)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        for i, name in enumerate(THEMES):
            card = ThemeCard(grid, name, name == settings["theme"], self.on_theme_pick,
                             App.CARD)
            card.grid(row=i // 2, column=i % 2, padx=4, pady=4)

        c = self.card(page, "LAYOUT")
        self.v_compact = tk.BooleanVar(value=bool(settings.get("compact_ui", False)))
        self.check(c, "Compact mode", self.v_compact, self.on_compact_toggle)
        self.hint(c, "On: while the macro runs, the window is replaced by a small stats bar "
                     "over Roblox. Off: the main window stays open.")
        c = self.card(page, "WINDOWS")
        self.v_less_win = tk.BooleanVar(value=less_windows_on())
        self.check(c, "Less windows", self.v_less_win, self.on_less_windows_toggle)
        self.hint(c, "On (default): a window opened from Settings replaces it, with a Back button "
                     "and a path like settings / reconnect / images. Off: every window opens on "
                     "top of the previous one. Applies to windows opened from now on.")
        c = self.card(page, "ANIMATIONS")
        self.v_anim = tk.BooleanVar(value=bool(settings.get("animations", False)))
        self.check(c, "GUI animations", self.v_anim, self.on_animations_toggle)
        self.hint(c, "On: windows glide and fade in and out, tabs slide, and collapsing a window "
                     "is animated. Off (default): everything appears instantly.")
        c = self.card(page, "MINI BAR POSITION",
                      "Where the small stats bar sits along the top of Roblox (when Compact mode is on).")
        items = [(o, o, MINI_POS_BADGES[o], None) for o in MINI_POS_OPTIONS]
        self.mini_pos_dd = ModernDropdown(c, items, settings.get("mini_pos", DEFAULT_MINI_POS),
                                          self.on_mini_pos_pick)
        self.mini_pos_dd.pack(fill="x")

    def on_mini_pos_pick(self, key):
        if key not in MINI_POS_OPTIONS:
            return
        settings["mini_pos"] = key
        save_stats()
        self.app.set_mini_paused(self.app.pill_key == "paused")   # move the bar right away

    def on_less_windows_toggle(self):
        settings["less_windows"] = bool(self.v_less_win.get())
        save_stats()

    def on_animations_toggle(self):
        settings["animations"] = bool(self.v_anim.get())
        save_stats()

    def on_compact_toggle(self):
        settings["compact_ui"] = bool(self.v_compact.get())
        save_stats()
        if not settings["compact_ui"]:
            self.app.hide_mini()                 # bar goes away, window comes back
        elif running():
            self.app.show_mini()

    def on_theme_pick(self, name):
        if name == settings["theme"]:
            return
        apply_theme(name)                                  # every widget built from now on uses it
        self.after_idle(self.theme_follow)                 # (not inside the card's own click)

    def theme_follow(self):
        """This window changes first - it is the one being looked at - then the main window."""
        self.retheme("themes")
        self.after_idle(self.app.finish_theme)

    # ----- saving -----
    def save_soon(self):
        """Typing in a text box saves a moment after the last key."""
        if self.save_job:
            self.after_cancel(self.save_job)
        self.save_job = self.after(500, self.commit)

    def commit(self):
        if self.save_job:
            try:
                self.after_cancel(self.save_job)
            except tk.TclError:
                pass
            self.save_job = None
        wh = settings["webhook"]
        wh["url"] = self.v_url.get().strip()
        wh["mention"] = bool(self.v_mention.get())
        wh["user_id"] = self.v_uid.get().strip()
        wh["hourly_graph"] = bool(self.v_hourly.get())
        wh["notify_mission"] = bool(self.v_mission.get())
        try:
            hours = int(self.v_hours.get())
        except ValueError:
            hours = wh["graph_hours"]      # box is empty / being typed: keep the old value
        wh["graph_hours"] = min(MAX_GRAPH_HOURS, max(1, hours))
        wh["notify_error"] = bool(self.v_error.get())
        wh["use_bot"] = bool(self.v_bot.get())
        wh["bot_token"] = self.v_token.get().strip()
        wh["channel_id"] = self.v_channel.get().strip()
        fam = self.v_family.get()
        old_fam = settings["family"]
        settings["family"] = fam if family_valid(fam) else DEFAULT_FAMILY
        if family_is_raid(old_fam) != raid_counting():
            report_rebase()
            push_stats(save=False)      # mission <-> raid counters on the main window
        self.app.refresh_labels()
        ms = self.v_move_shifted.get()
        settings["move_shifted"] = ms if ms in MOVE_SHIFTED_OPTIONS else DEFAULT_MOVE_SHIFTED
        fs = settings["failsafe"]
        try:
            fs["minutes"] = min(MAX_FAILSAFE_MINUTES, max(0, int(self.v_fs_min.get())))
        except ValueError:
            pass                            # being typed / empty: keep the old value
        try:
            fs["seconds"] = min(59, max(0, int(self.v_fs_sec.get())))
        except ValueError:
            pass
        try:
            fx, fy = int(self.v_fs_x.get()), int(self.v_fs_y.get())
            if fx >= 0 and fy >= 0:
                fs["click_x"], fs["click_y"] = fx, fy
        except ValueError:
            if not self.v_fs_x.get().strip() and not self.v_fs_y.get().strip():
                fs["click_x"] = fs["click_y"] = None     # both empty = default spot
        fs["look_down"] = bool(self.v_fs_look.get())
        self.commit_chests()
        save_stats()
        bot_restart()                       # start / stop the Discord command listener if needed
        # gentle warnings (it still saves what was typed)
        bad_url = (not wh["use_bot"]) and bool(wh["url"]) and not webhook_url()
        self.hint_url.configure(
            text="That doesn't look like a Discord webhook URL." if bad_url else "")
        bad_uid = wh["mention"] and not wh["user_id"].isdigit()
        self.hint_uid.configure(
            text="The user ID must be numbers only." if bad_uid else
                 self.UID_HELP,
            fg="#f87171" if bad_uid else App.SUB)
        bad_bot = wh["use_bot"] and bool(wh["bot_token"] or wh["channel_id"]) and not bot_ready()
        self.hint_bot.configure(
            text="Check the bot token and the channel ID (numbers only)." if bad_bot else "")

    def say(self, msg, error=False, good=False):
        self.status.configure(text=msg,
                              fg="#f87171" if error else "#34d399" if good else App.SUB)
        if self.say_job:
            self.after_cancel(self.say_job)
            self.say_job = None
        if msg and not msg.startswith("Press the new key"):
            self.say_job = self.after(5000, lambda: self.status.configure(text=""))

    def close(self):
        failsafe_test_stop()
        self.cancel_capture()
        self.commit()
        self.app.settings_win = None
        fade_close(self)


_spot_picker = {"win": None}   # the open picker overlay (only one at a time)


def pick_screen_spot(owner, prompt, on_pick):
    """Tint the screen, show the live coordinates next to the cursor and call on_pick(x, y) with
    the spot of the next click (Esc / right click cancels). Used by the failsafe and the Female
    Raid chests."""
    if _spot_picker["win"] is not None:
        return
    ml, mt, sw, sh = macro_monitor_rect(owner)     # the monitor the macro runs on
    pk = tk.Toplevel(owner)
    _spot_picker["win"] = pk
    pk.overrideredirect(True)
    pk.attributes("-topmost", True)
    pk.attributes("-alpha", 0.55)                    # the tint
    pk.configure(bg="black", cursor="fleur")         # "fleur" = the four-arrow move cursor
    pk.geometry(f"{sw}x{sh}{ml:+d}{mt:+d}")
    tk.Label(pk, text=prompt, font=(App.FONT, 16, "bold"), fg="white", bg="black",
             cursor="fleur").pack(pady=40)

    # the coordinates live in their own opaque little window that follows the cursor
    tip = tk.Toplevel(pk)
    tip.overrideredirect(True)
    tip.attributes("-topmost", True)
    tip.configure(bg="#111111")
    lbl = tk.Label(tip, text="", font=("Consolas", 11, "bold"), fg="white", bg="#111111",
                   padx=8, pady=3)
    lbl.pack()

    def follow(x, y):
        lbl.configure(text=f"{x}, {y}")
        tip.update_idletasks()
        w, h = tip.winfo_reqwidth(), tip.winfo_reqheight()
        tx, ty = x + 18, y + 18                      # bottom right of the cursor
        if tx + w > ml + sw:                         # flip near the screen edges
            tx = x - 18 - w
        if ty + h > mt + sh:
            ty = y - 18 - h
        tip.geometry(f"+{tx}+{ty}")

    def close():
        _spot_picker["win"] = None
        try:
            tip.destroy()
            pk.destroy()
        except tk.TclError:
            pass
        try:
            owner.lift()
        except tk.TclError:
            pass

    def done(event):
        x, y = int(event.x_root), int(event.y_root)
        close()
        on_pick(x, y)

    pk.bind("<Motion>", lambda e: follow(int(e.x_root), int(e.y_root)))
    pk.bind("<Button-1>", done)
    pk.bind("<Escape>", lambda _e: close())
    pk.bind("<Button-3>", lambda _e: close())
    follow(pk.winfo_pointerx(), pk.winfo_pointery())
    pk.focus_force()


class MacroEditorWindow(tk.Toplevel):
    """The "+" window: write a macro as a pseudo-script (or record one), check it and save it as a
    .ymir file in Documents/Ymir Macro/Custom-Macros."""
    # the Syntax box: ("title", text) / ("row", usage, description) / ("code", lines) / ("note", text)
    SYNTAX_DOC = (
        ("title", "COMMANDS   (one per line, everything after a # is a comment)"),
        ("row", "press KEY", "tap a key once"),
        ("row", "doubletap KEY", "tap it twice (a dash)"),
        ("row", "hold KEY SECONDS", "hold a key down for a while"),
        ("row", "down KEY  /  up KEY", "press / release (don't forget the 'up')"),
        ("row", "strafe left|right|KEY", "one strafe (the key tapped twice): left = Strafe A, right = Strafe D"),
        ("row", "strafe left|right|KEY SECONDS [GAP]", "keep strafing that long (GAP = pause between strafes)"),
        ("row", "strafefor SECONDS [left|right|KEY] [GAP]", "strafe like the built-in configs for that long "
                                                               "(default left; gap: Edit configs > Shiki)"),
        ("row", "spam KEY SECONDS [GAP]", "tap over and over (GAP = seconds between taps, default 0.1)"),
        ("row", "lookdown [SECONDS] [PIXELS]", "force the camera down: ctrl, mouse dragged down, ctrl "
                                               "(default 2 s, 15 px per step)"),
        ("row", "dashfor SECONDS [KEY]", "dash like the built-in configs: Dash held and released over and over "
                                         "(hold / gap: Edit configs > Movement)"),
        ("row", "wait SECONDS", "pause   (wait 0.5-1.2 = a random pause)"),
        ("row", "click [X Y] [right]", "click where the mouse is, or at a screen spot"),
        ("row", "imgclick IMAGE [SECONDS] [right]",
         "find the picture on screen and click it (SECONDS = keep looking that long, "
         "not found = skipped)"),
        ("row", "move X Y", "move the mouse to a screen spot"),
        ("row", "moverel DX DY", "move the mouse by pixels (turns the camera)"),
        ("row", "status TEXT", "show TEXT on the status card"),
        ("row", "log TEXT", "write TEXT in the log"),
        ("row", "name: / author: / description:",
         "info about the macro (name and type come from the boxes at the top)"),
        ("row", "type: mission", "every run counts as a mission (Missions, Completed / Failed, Discord)"),
        ("row", "type: raid", "every run counts as a raid (Raids, Vessels, raid Completed / Failed)"),
        ("title", "FUNCTIONS   (the () is optional)"),
        ("row", "retry()", "wait for the retry button and retry, like every config (nothing below it runs)"),
        ("row", "trackloot()", "look for the loot pictures now and add what is seen to the loot"),
        ("row", "openchest()", "the raid's chests: wait for the finish button, open the chests, click "
                               "finish (spots: Settings > Configs)"),
        ("row", "addwin()  /  addlose()", "add one to Completed / Failed"),
        ("row", "addmission()  /  addraid()", "add one to the Missions / Raids counter"),
        ("title", "LOGIC"),
        ("row", "if CONDITION", "run the lines below only when the condition is true"),
        ("row", "else", "(optional) run the lines below when it is not"),
        ("row", "end", "closes an if / repeat"),
        ("row", "repeat [N] ... end", "run the lines inside N times (no N = until the mission ends)"),
        ("row", "CONDITION", "find IMAGE   -   picture is on screen now"),
        ("row", "", "not find IMAGE   -   it is not"),
        ("row", "", "a and b   /   a or b   (and goes first)"),
        ("title", "EXAMPLE"),
        ("code", ("type: mission",
                  "strafe left 2",
                  "if find boss.png",
                  "    spam ability3 3",
                  "else",
                  "    press ability2",
                  "    wait 0.5-1.2",
                  "end",
                  "imgclick retry.png 5")),
        ("note", "Indenting is optional: Tab adds 4 spaces, Enter keeps the indent, 'Tidy' fixes the "
                 "whole script. The macro only reads the words."),
        ("title", "PICTURES   (IMAGE = a file name like retry.png)"),
        ("note", "Add them with the Images button (or drop the files into asset_override). Every "
                 "picture a macro uses is listed in Settings > Fix image detection > Macros, so "
                 "whoever gets your .ymir sees what it looks for. Send the picture files along with "
                 "it - they go into their asset_override folder. A picture that is missing shows "
                 "red in the editor."),
        ("title", "KEYS"),
        ("note", "KEY = ability2 ability3 ability4 ability5 dash strafe strafe_d grab\n"
                 "(those follow Settings > Keybinds, so a shared macro uses the keys of whoever "
                 "runs it) or any key: a  2  space  f1  shiftleft  ctrlleft  enter ...\n"
                 "When the lines run out the macro waits for the retry button, like every other "
                 "config."),
    )
    STARTER = (
        "author: you\n"
        "# A starter - change it, or press Record and play it once yourself.\n"
        "press ability2\n"
        "wait 1.5\n"
        "spam dash 8 0.07\n"
        "strafe left\n"
        "press ability3\n"
        "wait 3\n"
        "spam ability4 3 0.1\n")

    def __init__(self, settings_win, key=None):
        super().__init__(settings_win)
        prepare_window(self)
        self.sw = settings_win
        self.key = key                          # None = a new macro
        self.orig_file = custom_filename(key) if key else None
        self.say_job = None
        self.rec = None
        self.indicator = None
        self.title("ymir - edit macro" if key else "ymir - new macro")
        try:
            self.iconbitmap(asset_path(ICON_FILE))
        except (tk.TclError, OSError):
            pass
        self.configure(bg=App.BG)
        self.transient(settings_win)
        self.attributes("-topmost", True)
        self.minsize(520, 460)
        self.protocol("WM_DELETE_WINDOW", self.close)

        # bottom bar first, so it always stays visible
        bottom = tk.Frame(self, bg=App.BG)
        bottom.pack(side="bottom", fill="x", padx=18, pady=(8, 14))
        self.sw.flat_button(bottom, "Save", self.save).pack(side="right")
        if key:
            self.sw.flat_button(bottom, "Delete", self.delete).pack(side="right", padx=(0, 8))
        self.sw.flat_button(bottom, "Open folder", self.open_folder).pack(side="left")
        self.note = tk.Label(self, text="", font=(App.FONT, 9), fg=App.SUB, bg=App.BG,
                             anchor="w", justify="left", wraplength=480)
        self.note.pack(side="bottom", fill="x", padx=18)

        top = tk.Frame(self, bg=App.BG)
        top.pack(side="top", fill="x", padx=18, pady=(16, 0))
        top.columnconfigure(0, weight=1)
        tk.Label(top, text="NAME", font=(App.FONT, 8, "bold"), fg=App.SUB, bg=App.BG,
                 anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(top, text="TYPE", font=(App.FONT, 8, "bold"), fg=App.SUB, bg=App.BG,
                 anchor="w").grid(row=0, column=1, sticky="w", padx=(14, 0))
        self.v_name = tk.StringVar()
        tk.Entry(top, textvariable=self.v_name, font=(App.FONT, 10), bg=App.LOGBG, fg=App.TEXT,
                 insertbackground=App.TEXT, relief="flat", highlightthickness=1,
                 highlightbackground=App.BORDER, highlightcolor=App.ACCENT).grid(
            row=1, column=0, sticky="ew", ipady=6, pady=(4, 0))
        self.v_type = tk.StringVar(value=DEFAULT_MACRO_TYPE)
        self.type_btns = {}
        seg = tk.Frame(top, bg=App.BORDER, padx=1, pady=1)
        seg.grid(row=1, column=1, sticky="ns", padx=(14, 0), pady=(4, 0))
        for kind in MACRO_TYPES:
            b = tk.Label(seg, text=kind.capitalize(), font=(App.FONT, 9, "bold"), padx=12,
                         cursor="hand2", bg=App.CARD, fg=App.SUB)
            b.pack(side="left", fill="y")
            b.bind("<Button-1>", lambda _e, k=kind: self.set_type(k))
            b.bind("<Enter>", lambda _e, k=kind: self.paint_type(hover=k))
            b.bind("<Leave>", lambda _e: self.paint_type())
            self.type_btns[kind] = b
        self.paint_type()
        self.type_hint = tk.Label(self, text="", font=(App.FONT, 8), fg=App.SUB, bg=App.BG,
                                  anchor="w", justify="left")
        self.type_hint.pack(side="top", fill="x", padx=18, pady=(4, 0))
        self.paint_type()

        bar = tk.Frame(self, bg=App.BG)
        bar.pack(side="top", fill="x", padx=18, pady=(10, 0))
        self.rec_btn = self.sw.flat_button(bar, "Record", self.start_record)
        self.rec_btn.pack(side="left")
        self.sw.flat_button(bar, "Pick click spot", self.pick_click).pack(side="left", padx=(8, 0))
        self.sw.flat_button(bar, "Check", self.check).pack(side="left", padx=(8, 0))
        self.sw.flat_button(bar, "Syntax", self.toggle_syntax).pack(side="left", padx=(8, 0))
        self.sw.flat_button(bar, "Tidy", self.tidy).pack(side="left", padx=(8, 0))
        self.sw.flat_button(bar, "Images", lambda: self.sw.open_asset_window("Macros")).pack(
            side="left", padx=(8, 0))
        self.v_binds = tk.BooleanVar(value=True)
        switch_row(self, "Record keys as my keybind names (ability2, dash ...)", self.v_binds,
                   lambda: None, App.BG, App.SUB, sub_font=(App.FONT, 9), pady=6).pack_configure(
            padx=18, side="top")

        self.syntax_wrap = tk.Frame(self, bg=App.BG)
        self.doc_col = tkfont.Font(font=("Consolas", 9)).measure("0") * 36     # description column
        self.syntax_box = tk.Text(self.syntax_wrap, height=16, font=("Consolas", 9), bg=App.CARD,
                                  fg=App.TEXT, relief="flat", wrap="word", highlightthickness=1,
                                  highlightbackground=App.BORDER, padx=8, pady=6,
                                  tabs=(self.doc_col,), cursor="arrow")
        doc_sb = ttk.Scrollbar(self.syntax_wrap, orient="vertical", command=self.syntax_box.yview)
        self.syntax_box.configure(yscrollcommand=doc_sb.set)
        doc_sb.pack(side="right", fill="y")
        self.syntax_box.pack(side="left", fill="both", expand=True)
        for tag, opts in self.HL_STYLES.items():
            self.syntax_box.tag_configure(tag, **opts)
        self.fill_syntax()
        self.syntax_shown = False

        wrap = tk.Frame(self, bg=App.BG)
        wrap.pack(side="top", fill="both", expand=True, padx=18, pady=(4, 6))
        self.text = tk.Text(wrap, font=("Consolas", 10), bg=App.LOGBG, fg=App.TEXT,
                            insertbackground=App.TEXT, relief="flat", undo=True, wrap="none",
                            highlightthickness=1, highlightbackground=App.BORDER,
                            highlightcolor=App.ACCENT, padx=8, pady=6)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        for tag, opts in self.HL_STYLES.items():
            self.text.tag_configure(tag, **opts)
        self.text.tag_configure("bad", background="#3b1219")
        self.text.tag_raise("bad")                  # error lines stay visible under the colours
        self.hl_job = None
        self.text.bind("<KeyRelease>", self.on_key_release)
        self.text.bind("<Return>", self.on_return)
        self.text.bind("<Tab>", self.on_tab)
        for seq in ("<Shift-Tab>", "<ISO_Left_Tab>"):
            try:
                self.text.bind(seq, self.on_shift_tab)
            except tk.TclError:
                pass
        self.text.bind("<FocusIn>", self.hl_soon)     # (a picture may have been added meanwhile)
        for seq in ("<<Paste>>", "<<Cut>>", "<<Undo>>", "<<Redo>>"):
            self.text.bind(seq, self.hl_soon, add="+")

        self.load()
        self.highlight()
        self.update_idletasks()
        x, y = settings_win.winfo_x() + 30, settings_win.winfo_y() + 30
        self.geometry(f"600x660+{x}+{y}")
        style_title_bar(self, App.BG)
        nav_attach(self, "edit macro", settings_win.app)
        reveal_window(self)
        self.lift()
        self.focus_force()

    # ----- the Type selector -----
    HEAD_LINES = 2          # name: and type: are written on top of the file by Save / Check

    def set_type(self, kind):
        self.v_type.set(kind)
        self.paint_type()
        self.text.edit_modified(True)

    def paint_type(self, hover=None):
        cur = self.v_type.get()
        for kind, b in self.type_btns.items():
            on = kind == cur
            b.configure(bg=App.ACCENT_BG if on else (App.FIELD_HOVER if kind == hover else App.CARD),
                        fg=App.ACCENT_TEXT if on else App.SUB)
        if hasattr(self, "type_hint"):
            self.type_hint.configure(text=f"type: {cur}  -  {MACRO_TYPES.get(cur, '')}")

    # ----- syntax highlighting -----
    HL_STYLES = {
        "hl_comment": {"foreground": "#6b7694"},
        "hl_header": {"foreground": "#fbbf24"},
        "hl_cmd": {"foreground": "#60a5fa"},
        "hl_flow": {"foreground": "#c084fc"},
        "hl_key": {"foreground": "#34d399"},
        "hl_num": {"foreground": "#fb923c"},
        "hl_text": {"foreground": "#a5b4cf"},
        "hl_img": {"foreground": "#2dd4bf"},                  # a picture that exists
        "hl_imgbad": {"foreground": "#f87171", "underline": True},   # a picture that is missing
        "hl_arg": {"foreground": "#fda4af"},                  # KEY / SECONDS ... in the docs
        "hl_desc": {"foreground": "#8b97b5"},                 # the docs' descriptions
        "hl_title": {"foreground": "#e2e8f0", "font": ("Consolas", 9, "bold"), "spacing1": 8,
                     "spacing3": 2},
    }
    HL_KEY_CMDS = ("press", "tap", "doubletap", "dtap", "down", "up", "hold", "spam")
    HL_TEXT_CMDS = ("log", "status")
    HL_FLOW = ("repeat", "end", "if", "else")
    HL_COND = ("not", "and", "or")
    HL_CMDS = (HL_KEY_CMDS + HL_TEXT_CMDS
               + ("strafe", "strafefor", "dashfor", "lookdown", "wait", "sleep", "click", "imgclick", "move", "moverel", "find")
               + MACRO_FUNCS)
    HL_NUM_RE = re.compile(r"^-?\d+(\.\d+)?(-\d+(\.\d+)?)?$")

    def hl_soon(self, _event=None):
        """Colour the text a moment after the last change (one pass, however fast you type)."""
        if self.hl_job:
            self.after_cancel(self.hl_job)
        self.hl_job = self.after(60, self.highlight)

    def _img_tag(self, word, cache):
        """hl_img when the picture exists, hl_imgbad when it doesn't (None: not a picture name)."""
        name = normalize_image_name(word)
        if name is None:
            return None
        if name not in cache:
            cache[name] = image_exists(name)
        return "hl_img" if cache[name] else "hl_imgbad"

    def _hl_line(self, t, ln, line, cache):
        """Colour one line of code (indentation is just spaces and is skipped)."""
        cut = len(line)
        m = re.search(r"(^|\s)(#|//)", line)        # a comment starts at # or // (after a space)
        if m:
            cut = m.start(2)
            t.tag_add("hl_comment", f"{ln}.{cut}", f"{ln}.{len(line)}")
        code = line[:cut]

        def mark(tag, a, b):
            t.tag_add(tag, f"{ln}.{a}", f"{ln}.{b}")

        h = MACRO_HEADER_RE.match(code.strip())
        if h:
            a = code.index(h.group(1))
            mark("hl_header", a, a + len(h.group(1)) + 1)       # "name:"
            if h.group(1).lower() == "type":
                v = code.index(h.group(2), a + len(h.group(1))) if h.group(2) else -1
                if v >= 0:
                    mark("hl_key" if normalize_macro_type(h.group(2)) else "hl_imgbad", v, v + len(h.group(2)))
            return
        tokens = list(re.finditer(r"\S+", code))
        if not tokens:
            return
        first = tokens[0]
        cmd = first.group().lower()
        if cmd.endswith("()"):
            cmd = cmd[:-2]                              # retry()  ->  retry
        if cmd in self.HL_FLOW:
            mark("hl_flow", first.start(), first.end())
        elif cmd in self.HL_CMDS and cmd != "find":
            mark("hl_cmd", first.start(), first.end())
        else:
            return                                      # unknown command: left plain (Check flags it)
        if cmd in self.HL_TEXT_CMDS:
            if len(tokens) > 1:
                mark("hl_text", tokens[1].start(), len(code.rstrip()))
            return
        prev = ""
        for i, tok in enumerate(tokens[1:]):
            word = tok.group().lower()
            if cmd == "if":
                if word == "find":
                    mark("hl_cmd", tok.start(), tok.end())
                elif prev == "find":
                    tag = self._img_tag(tok.group(), cache)
                    if tag:
                        mark(tag, tok.start(), tok.end())
                elif word in self.HL_COND:
                    mark("hl_flow", tok.start(), tok.end())
            elif i == 0 and cmd == "imgclick":
                tag = self._img_tag(tok.group(), cache)
                if tag:
                    mark(tag, tok.start(), tok.end())
            elif i == 0 and cmd == "strafe":
                if _strafe_key(word):
                    mark("hl_key", tok.start(), tok.end())
            elif i == 1 and cmd == "strafefor":
                if _strafe_key(word):
                    mark("hl_key", tok.start(), tok.end())
            elif i == 0 and cmd in self.HL_KEY_CMDS:
                if resolve_macro_key(word):
                    mark("hl_key", tok.start(), tok.end())
            elif word in ("left", "right") and cmd in ("click", "imgclick"):
                mark("hl_key", tok.start(), tok.end())
            elif self.HL_NUM_RE.match(word):
                mark("hl_num", tok.start(), tok.end())
            prev = word

    def highlight(self):
        self.hl_job = None
        t = self.text
        for tag in self.HL_STYLES:
            t.tag_remove(tag, "1.0", "end")
        cache = {}
        for ln, line in enumerate(t.get("1.0", "end-1c").split("\n"), 1):
            self._hl_line(t, ln, line, cache)

    # ----- the Syntax box (highlighted like the editor) -----
    def fill_syntax(self):
        box = self.syntax_box
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.tag_configure("doc_row", lmargin1=0, lmargin2=self.doc_col)
        cache = {}
        first = True
        for item in self.SYNTAX_DOC:
            kind = item[0]
            if kind == "title":
                box.insert("end", ("" if first else "\n") + item[1] + "\n", "hl_title")
            elif kind == "row":
                start = box.index("end-1c")
                self._doc_usage(box, item[1])
                box.insert("end", "\t" + item[2] + "\n", "hl_desc")
                box.tag_add("doc_row", start, "end-1c")
            elif kind == "code":
                for line in item[1]:
                    ln = int(box.index("end-1c").split(".")[0])
                    box.insert("end", line + "\n")
                    self._hl_line(box, ln, line, cache)
            else:
                box.insert("end", item[1] + "\n", "hl_desc")
            first = False
        box.configure(state="disabled")

    def _doc_usage(self, box, usage):
        """A usage line like 'spam KEY SECONDS [GAP]': the command coloured, the placeholders too."""
        for m in re.finditer(r"\S+|\s+", usage):
            w = m.group()
            core, low = w.strip("[]"), w.strip("[]").lower()
            if w.isspace() or not core:
                tag = None
            elif core.endswith(":") and MACRO_HEADER_RE.match(core + " x"):
                tag = "hl_header"
            elif low in self.HL_FLOW or low in self.HL_COND:
                tag = "hl_flow"
            elif low in self.HL_CMDS:
                tag = "hl_cmd"
            elif low in ("left", "right"):
                tag = "hl_key"
            elif core.isalpha() and core.isupper():
                tag = "hl_arg"
            else:
                tag = None
            box.insert("end", w, tag) if tag else box.insert("end", w)

    # ----- indentation (optional) -----
    @staticmethod
    def _first_word(line):
        words = _strip_macro_comment(line).lower().split()
        return words[0] if words else ""

    def on_return(self, _event=None):
        """Enter keeps the indentation of the line above (one level deeper after if / else / repeat)."""
        t = self.text
        before = t.get("insert linestart", "insert")
        indent = re.match(r"[ \t]*", before).group()
        if self._first_word(before) in ("if", "else", "repeat"):
            indent += MACRO_INDENT
        t.insert("insert", "\n" + indent)
        t.see("insert")
        self.hl_soon()
        return "break"

    def on_tab(self, _event=None):
        self.text.insert("insert", MACRO_INDENT)
        self.hl_soon()
        return "break"

    def on_shift_tab(self, _event=None):
        t = self.text
        ln = t.index("insert").split(".")[0]
        line = t.get(f"{ln}.0", f"{ln}.end")
        cut = len(line) - len(line.lstrip(" "))
        cut = min(cut, len(MACRO_INDENT))
        if cut:
            t.delete(f"{ln}.0", f"{ln}.{cut}")
        self.hl_soon()
        return "break"

    def on_key_release(self, event=None):
        """Typing 'end' / 'else' at the start of an indented line pulls it back one level."""
        t = self.text
        if event is not None and event.keysym in ("d", "D", "e", "E"):
            ln = t.index("insert").split(".")[0]
            line = t.get(f"{ln}.0", f"{ln}.end")
            if (line.strip().lower() in ("end", "else") and line.startswith(MACRO_INDENT)
                    and t.compare("insert", "==", f"{ln}.end")):
                t.delete(f"{ln}.0", f"{ln}.{len(MACRO_INDENT)}")
        self.hl_soon()

    def tidy(self):
        """Indent the whole script by its if / else / repeat blocks."""
        t = self.text
        pos = t.index("insert")
        level, out = 0, []
        for raw in t.get("1.0", "end-1c").split("\n"):
            word = self._first_word(raw)
            if word in ("end", "else"):
                level = max(0, level - 1)
            out.append(MACRO_INDENT * level + raw.strip() if raw.strip() else "")
            if word in ("if", "else", "repeat"):
                level += 1
        new = "\n".join(out)
        if new != t.get("1.0", "end-1c"):
            t.edit_separator()
            t.delete("1.0", "end")
            t.insert("1.0", new)
            t.edit_separator()
        try:
            t.mark_set("insert", pos)
        except tk.TclError:
            pass
        self.highlight()
        self.say("Indentation fixed.", good=True)

    # ----- file -----
    def load(self):
        body = self.STARTER
        if self.key:
            try:
                with open(custom_path(self.key), "r", encoding="utf-8-sig") as fh:
                    raw = fh.read()
            except (OSError, UnicodeDecodeError) as e:
                raw = ""
                self.say(f"Could not read the file: {e}", error=True)
            meta, _prog, _err = parse_macro(raw)
            self.v_name.set(meta.get("name") or os.path.splitext(self.orig_file)[0])
            self.v_type.set(normalize_macro_type(meta.get("type")) or DEFAULT_MACRO_TYPE)
            self.paint_type()
            kept, dropped, dropped_t = [], False, False
            for line in raw.splitlines():
                m = MACRO_HEADER_RE.match(line.strip())
                if m and m.group(1).lower() == "name" and not dropped:
                    dropped = True          # the Name box owns this line
                    continue
                if m and m.group(1).lower() == "type" and not dropped_t:
                    dropped_t = True        # ... and the Type selector this one
                    continue
                kept.append(line)
            body = "\n".join(kept).strip("\n") + "\n"
        self.text.insert("1.0", body)
        self.text.edit_reset()
        self.text.edit_modified(False)

    def build_text(self):
        name = self.v_name.get().strip()
        lines = []
        for line in self.text.get("1.0", "end-1c").splitlines():
            m = MACRO_HEADER_RE.match(line.strip())
            if m and m.group(1).lower() in ("name", "type"):
                continue                    # only the Name box / Type selector set these
            lines.append(line)
        return name, f"name: {name}\ntype: {self.v_type.get()}\n" + "\n".join(lines).rstrip() + "\n"

    def save(self):
        name, text = self.build_text()
        if not name:
            self.say("Give the macro a name first.", error=True)
            return
        _meta, _prog, errors = parse_macro(text)
        if errors:
            self.show_errors(errors)
            return
        fname = safe_macro_filename(name)
        path = os.path.join(custom_dir(), fname)
        if os.path.exists(path) and fname != self.orig_file:
            if not messagebox.askyesno("ymir", f"{fname} already exists. Replace it?", parent=self):
                return
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)           # never leaves a half-written file behind
            if self.orig_file and self.orig_file != fname:      # renamed: remove the old file
                old = os.path.join(custom_dir(), os.path.basename(self.orig_file))
                if os.path.isfile(old):
                    os.remove(old)
        except OSError as e:
            self.say(f"Could not save: {e}", error=True)
            return
        self.text.edit_modified(False)
        self.sw.macro_saved(CUSTOM_PREFIX + fname)
        self.sw.say(f"Saved {fname} - it is the picked config now", good=True)
        self.destroy()

    def delete(self):
        name = self.v_name.get().strip() or self.orig_file
        if not messagebox.askyesno("ymir", f"Delete '{name}'?\nThe .ymir file is removed for good.",
                                   parent=self):
            return
        try:
            os.remove(custom_path(self.key))
        except OSError as e:
            self.say(f"Could not delete: {e}", error=True)
            return
        self.sw.macro_deleted(self.key)
        self.sw.say(f"Deleted {name}", good=True)
        self.destroy()

    def open_folder(self):
        folder = custom_dir()
        try:
            if IS_WIN:
                os.startfile(folder)    # noqa
            else:
                subprocess.Popen(["open" if IS_MAC else "xdg-open", folder])
        except Exception:  # noqa: BLE001
            self.say(folder)

    def close(self):
        if self.rec is not None:
            return                          # recording: finish that first (the stop key)
        if self.text.edit_modified() and not messagebox.askyesno(
                "ymir", "Close without saving your changes?", parent=self):
            return
        fade_close(self)

    # ----- checking -----
    def show_errors(self, errors):
        self.text.tag_remove("bad", "1.0", "end")
        # line numbers of the file = line numbers here + HEAD_LINES (name: / type: are added on top)
        for no, _msg in errors:
            ln = no - self.HEAD_LINES
            if ln >= 1:
                self.text.tag_add("bad", f"{ln}.0", f"{ln}.end+1c")
        no, msg = errors[0]
        more = f"  (+{len(errors) - 1} more)" if len(errors) > 1 else ""
        self.say(f"Line {max(1, no - self.HEAD_LINES)}: {msg}{more}", error=True)
        self.text.see(f"{max(1, no - self.HEAD_LINES)}.0")

    def check(self):
        self.text.tag_remove("bad", "1.0", "end")
        _name, text = self.build_text()
        _meta, prog, errors = parse_macro(text)
        if errors:
            self.show_errors(errors)
        else:
            msg = f"Looks good - {count_macro_nodes(prog)} command(s)."
            missing = [n for n in macro_node_images(prog) if not image_exists(n)]
            if missing:
                self.say(f"{msg} Missing picture(s): {', '.join(missing)} - add them with the "
                         "Images button.", warn=True)
            else:
                self.say(msg, good=True)

    def toggle_syntax(self):
        if self.syntax_shown:
            self.syntax_wrap.pack_forget()
        else:
            self.syntax_wrap.pack(side="top", fill="x", padx=18, pady=(6, 0), before=self.text.master)
        self.syntax_shown = not self.syntax_shown

    # ----- helpers for the text box -----
    def insert_lines(self, lines):
        if self.text.index("insert").split(".")[1] != "0":
            self.text.insert("insert", "\n")
        self.text.insert("insert", "\n".join(lines) + "\n")
        self.text.see("insert")
        self.highlight()

    def pick_click(self):
        def picked(x, y):
            self.insert_lines([f"click {x} {y}"])
            self.say(f"Added: click {x} {y}", good=True)
            self.deiconify()
            self.lift()
        pick_screen_spot(self, "Click the spot the macro should click   (Esc to cancel)", picked)

    # ----- recording -----
    def start_record(self):
        if running():
            self.say("Stop the macro before recording.", error=True)
            return
        if self.rec is not None:
            return
        self.rec = MacroRecorder(use_binds=bool(self.v_binds.get()))
        try:
            self.rec.start()
        except Exception as e:  # noqa: BLE001
            self.rec = None
            self.say(f"Could not start recording: {type(e).__name__}: {e}", error=True)
            return
        self.withdraw()
        self._sw_was_shown = self.sw.state() != "withdrawn"
        self.sw.withdraw()                      # nothing of ymir in the way of the game
        ind = tk.Toplevel(self.sw.app.root)
        self.indicator = ind
        ind.overrideredirect(True)
        ind.attributes("-topmost", True)
        ind.configure(bg="#111111", highlightthickness=1, highlightbackground="#ef4444")
        self.ind_label = tk.Label(ind, text="", font=(App.FONT, 11, "bold"), fg="#ffffff",
                                  bg="#111111", padx=14, pady=8)
        self.ind_label.pack(side="left")
        stop = tk.Label(ind, text="Stop", font=(App.FONT, 10, "bold"), fg="#ffffff", bg="#ef4444",
                        padx=12, pady=8, cursor="hand2")
        stop.pack(side="left")
        stop.bind("<Button-1>", lambda _e: setattr(self.rec, "stop_requested", True))
        self.ind_label.configure(text="Recording starts in 3 ... switch to the game")
        ind.update_idletasks()
        ml, mt, mw, _mh = macro_monitor_rect(self)
        w, h = ind.winfo_reqwidth() + 60, ind.winfo_reqheight()
        x, y = ml + (mw - w) // 2, mt + 8
        ind.geometry(f"{w}x{h}{x:+d}{y:+d}")
        self.rec.ignore = (x, y, x + w, y + h)
        self.countdown(3)

    def countdown(self, n):
        if self.rec is None:
            return
        if n > 0:
            self.ind_label.configure(text=f"Recording starts in {n} ... switch to the game")
            self.after(1000, lambda: self.countdown(n - 1))
            return
        self.ind_label.configure(text=f"\u25cf REC   press {QUIT_KEY.upper()} to stop")
        self.rec.begin()
        self.poll_record()

    def poll_record(self):
        if self.rec is None:
            return
        if self.rec.stop_requested:
            self.finish_record()
            return
        self.after(80, self.poll_record)

    def finish_record(self):
        rec, self.rec = self.rec, None
        try:
            lines = rec.to_lines()
        finally:
            rec.stop()                          # also binds F8 / F6 / F7 again
        if self.indicator is not None:
            self.indicator.destroy()
            self.indicator = None
        if getattr(self, "_sw_was_shown", True):
            self.sw.deiconify()                 # (with Less windows it stays hidden under the editor)
        self.deiconify()
        self.lift()
        self.focus_force()
        self.sw.app.keep_settings_on_top()
        if not lines:
            self.say("Nothing was recorded - press some keys while it counts as recording.",
                     error=True)
            return
        self.insert_lines(["# recorded " + time.strftime("%Y-%m-%d %H:%M")] + lines)
        extra = ""
        if rec.skipped:
            extra += f" Skipped keys the macro can't press: {', '.join(sorted(rec.skipped))}."
        if not rec.mouse_ok:
            extra += " Mouse clicks can't be recorded on this system."
        self.say(f"Recorded {len(lines)} line(s). Mouse movement is not recorded - "
                 f"use moverel for camera turns.{extra}", good=True)

    def say(self, msg, error=False, good=False, warn=False):
        self.note.configure(text=msg, fg="#f87171" if error else "#34d399" if good
                            else "#fbbf24" if warn else App.SUB)
        if self.say_job:
            self.after_cancel(self.say_job)
            self.say_job = None
        if msg and not error:
            self.say_job = self.after(8000 if not warn else 14000,
                                      lambda: self.note.configure(text=""))


class FailsafeConfigWindow(tk.Toplevel):
    """Failsafe > Configure: the look down switch and its time / pixels / step delay, plus a Test
    button (look down + clicking only). Every change is saved right away."""
    FIELDS = (
        ("look_time", "Time", "sec", "How long the mouse is moved down."),
        ("look_pixels", "Pixels per step", "px", "How far the mouse moves down in one step "
                                                 "(relative movement, works in a locked mouse)."),
        ("look_step", "Step delay", "sec", "Pause between two steps."),
    )

    def __init__(self, sw):
        super().__init__(sw)
        prepare_window(self)
        self.sw = sw
        self.save_job = None
        self.say_job = None
        self.vars = {}
        self.title("ymir - failsafe look down")
        try:
            self.iconbitmap(asset_path(ICON_FILE))
        except (tk.TclError, OSError):
            pass
        self.configure(bg=App.BG)
        self.transient(sw)
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.close)

        card = tk.Frame(self, bg=App.CARD, highlightthickness=1, highlightbackground=App.BORDER)
        card.pack(fill="both", expand=True, padx=18, pady=(16, 8))
        f = tk.Frame(card, bg=App.CARD)
        f.pack(fill="both", expand=True, padx=16, pady=14)
        tk.Label(f, text="LOOK DOWN", font=(App.FONT, 8, "bold"), fg=App.SUB, bg=App.CARD,
                 anchor="w").pack(fill="x")

        row = tk.Frame(f, bg=App.CARD)
        row.pack(fill="x", pady=(10, 2))
        sw_btn = ToggleSwitch(row, sw.v_fs_look, sw.commit, parent_bg=App.CARD)
        sw_btn.pack(side="right")
        lbl = tk.Label(row, text="Look down before clicking", font=(App.FONT, 10), fg=App.TEXT,
                       bg=App.CARD, anchor="w", cursor="hand2")
        lbl.pack(side="left")
        lbl.bind("<Button-1>", sw_btn.flip)
        self.hint(f, "Press ctrl, move the mouse down, press ctrl again - before the failsafe "
                     "clicks. Some setups look up instead, so leave it off if that happens to you.")

        fs = settings["failsafe"]
        for key, label, unit, hint in self.FIELDS:
            row = tk.Frame(f, bg=App.CARD)
            row.pack(fill="x", pady=(12, 0))
            tk.Label(row, text=label, font=(App.FONT, 10), fg=App.TEXT, bg=App.CARD,
                     width=14, anchor="w").pack(side="left")
            var = tk.StringVar(value=f"{fs[key]:g}")
            var.trace_add("write", lambda *_: self.save_soon())
            self.vars[key] = var
            e = tk.Entry(row, textvariable=var, width=8, font=(App.FONT, 10), justify="center",
                         bg=App.LOGBG, fg=App.TEXT, insertbackground=App.TEXT, relief="flat",
                         highlightthickness=1, highlightbackground=App.BORDER,
                         highlightcolor=App.ACCENT)
            e.pack(side="left", ipady=3)
            e.bind("<FocusOut>", lambda _e: self.commit())
            tk.Label(row, text=unit, font=(App.FONT, 9), fg=App.SUB,
                     bg=App.CARD).pack(side="left", padx=(6, 0))
            self.hint(f, hint)
        self.total = tk.Label(f, text="", font=(App.FONT, 9), fg=App.SUB, bg=App.CARD, anchor="w")
        self.total.pack(fill="x", pady=(12, 0))

        btns = tk.Frame(f, bg=App.CARD)
        btns.pack(fill="x", pady=(14, 0))
        self.test_btn = sw.flat_button(btns, "Stop test" if failsafe_test_running() else "Test",
                                       sw.toggle_failsafe_test)
        self.test_btn.pack(side="left")
        sw.flat_button(btns, "Reset", self.reset).pack(side="left", padx=(8, 0))
        self.hint(f, "Test runs only the look down + clicking part (5 clicks at the failsafe spot), "
                     "3s after you press it - click into Roblox first. Press it again to stop.")

        self.note = tk.Label(self, text="", font=(App.FONT, 9), fg=App.SUB, bg=App.BG,
                             anchor="w", justify="left", wraplength=400)
        self.note.pack(fill="x", padx=20, pady=(0, 12))

        self.update_total()
        self.update_idletasks()
        self.geometry(f"+{sw.winfo_x() + 30}+{sw.winfo_y() + 30}")
        style_title_bar(self, App.BG)
        nav_attach(self, "look down", sw.app)
        reveal_window(self)
        self.lift()
        self.focus_force()

    def hint(self, parent, text):
        lbl = tk.Label(parent, text=text, font=(App.FONT, 8), fg=App.SUB, bg=App.CARD,
                       anchor="w", justify="left", wraplength=400)
        lbl.pack(fill="x", pady=(4, 0))
        return lbl

    def say(self, msg, error=False, good=False):
        self.note.configure(text=msg, fg="#f87171" if error else "#34d399" if good else App.SUB)
        if self.say_job:
            self.after_cancel(self.say_job)
            self.say_job = None
        if msg and not error:
            self.say_job = self.after(6000, lambda: self.note.configure(text=""))

    def update_total(self):
        fs = settings["failsafe"]
        steps = max(1, int(fs["look_time"] / fs["look_step"]))
        self.total.configure(text=f"About {steps} steps x {fs['look_pixels']}px = "
                                  f"{steps * fs['look_pixels']}px of mouse movement in total")

    def save_soon(self):
        if self.save_job:
            self.after_cancel(self.save_job)
        self.save_job = self.after(500, self.commit)

    def commit(self):
        if self.save_job:
            try:
                self.after_cancel(self.save_job)
            except tk.TclError:
                pass
            self.save_job = None
        fs = settings["failsafe"]
        for key, var in self.vars.items():
            lo, hi = FAILSAFE_LOOK_LIMITS[key]
            try:
                v = float(var.get().replace(",", "."))
            except ValueError:
                continue                        # being typed / empty: keep the old value
            v = min(hi, max(lo, v))
            fs[key] = int(round(v)) if key == "look_pixels" else v
        save_stats()
        self.update_total()

    def reset(self):
        fs = settings["failsafe"]
        defaults = {"look_time": LOOK_DOWN_TIME, "look_pixels": LOOK_DOWN_PIXELS,
                    "look_step": LOOK_DOWN_STEP}
        for key, val in defaults.items():
            fs[key] = val
            self.vars[key].set(f"{val:g}")
        self.commit()
        self.say("Look down settings reset to the defaults", good=True)

    def close(self):
        self.commit()
        self.destroy()


class ReconnectConfigWindow(tk.Toplevel):
    """Recovery > Reconnect > Configure. The Reconnect switch is off by default and can only be
    turned on once every setting below it is filled in (reconnect_missing())."""
    THUMB_W, THUMB_H = 200, 70
    SMALL_W, SMALL_H = 110, 36          # the Select / Play thumbnails

    def __init__(self, sw):
        super().__init__(sw)
        prepare_window(self)
        self.sw = sw
        self.say_job = None
        self.save_job = None
        self.spot_vars = {}                 # key -> (X var, Y var)
        for key, _name in RECONNECT_SPOTS:
            cur = settings["reconnect"]["spots"].get(key)
            vx = tk.StringVar(value="" if cur is None else str(cur[0]))
            vy = tk.StringVar(value="" if cur is None else str(cur[1]))
            for v in (vx, vy):
                v.trace_add("write", lambda *_: self.save_soon())
            self.spot_vars[key] = (vx, vy)
        self.title("ymir - reconnect")
        try:
            self.iconbitmap(asset_path(ICON_FILE))
        except (tk.TclError, OSError):
            pass
        self.configure(bg=App.BG)
        self.transient(sw)
        self.attributes("-topmost", True)
        self.resizable(False, True)          # taller / shorter by dragging; the content scrolls
        self.minsize(300, 260)
        self.protocol("WM_DELETE_WINDOW", self.close)

        # everything scrolls (mouse wheel or the bar) when the window is shorter than its content
        holder = tk.Frame(self, bg=App.BG)
        self.canvas = tk.Canvas(holder, bg=App.BG, highlightthickness=0, bd=0)
        self.scroll = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=App.BG)
        self.inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self.inner_id, width=e.width))
        self.bind("<MouseWheel>", self._wheel)

        card = tk.Frame(self.inner, bg=App.CARD, highlightthickness=1, highlightbackground=App.BORDER)
        card.pack(fill="both", expand=True, padx=(0, 6))
        f = tk.Frame(card, bg=App.CARD)
        f.pack(fill="both", expand=True, padx=16, pady=14)

        # the switch
        self.v_on = tk.BooleanVar(value=bool(settings["reconnect"]["enabled"]))
        row = tk.Frame(f, bg=App.CARD)
        row.pack(fill="x")
        self.toggle = ToggleSwitch(row, self.v_on, self.on_toggle, parent_bg=App.CARD)
        self.toggle.pack(side="right")
        lbl = tk.Label(row, text="Reconnect", font=(App.FONT, 11, "bold"), fg=App.TEXT,
                       bg=App.CARD, anchor="w", cursor="hand2")
        lbl.pack(side="left")
        lbl.bind("<Button-1>", self.toggle.flip)
        self.req = tk.Label(f, text="", font=(App.FONT, 8), anchor="w", justify="left",
                            bg=App.CARD, wraplength=400)
        self.req.pack(fill="x", pady=(4, 0))

        tk.Label(f, text=f"When the Disconnect Image shows, the failsafe has been rescuing for "
                         f"{RECONNECT_RESCUE_LIMIT / 60:g} minutes or !reconnect is sent, Roblox is "
                         f"closed and game {ROBLOX_GAME_ID} is launched again.",
                 font=(App.FONT, 8), fg=App.SUB,
                 bg=App.CARD, anchor="w", justify="left", wraplength=400).pack(fill="x", pady=(4, 0))

        tk.Frame(f, height=1, bg=App.BORDER).pack(fill="x", pady=(12, 10))

        # setting 1: the Images (the Disconnect Image + the two that gate the clicks)
        tk.Label(f, text="Images", font=(App.FONT, 10, "bold"), fg=App.TEXT, bg=App.CARD,
                 anchor="w").pack(fill="x")
        tk.Label(f, text=f'A screenshot of just that text or button. Disconnect: the message Roblox '
                         f'shows when you are disconnected (the macro watches for it). Select: the '
                         f'first click happens {RECONNECT_FIRST_DELAY:g}s after it shows. Play: '
                         f'Click play (lobby) waits until it shows. Same for both tabs.',
                 font=(App.FONT, 8), fg=App.SUB, bg=App.CARD, anchor="w", justify="left",
                 wraplength=400).pack(fill="x", pady=(2, 0))
        self.img_lbls = {}
        self.img_thumbs = {}
        for kind in RECONNECT_ALL_IMAGES:
            self._image_row(f, kind)

        tk.Frame(f, height=1, bg=App.BORDER).pack(fill="x", pady=(14, 10))

        # setting: the delay between the clicks
        tk.Label(f, text="Click delay", font=(App.FONT, 10, "bold"), fg=App.TEXT, bg=App.CARD,
                 anchor="w").pack(fill="x")
        tk.Label(f, text=f"Seconds the macro waits before each click after the first one "
                         f"(default {RECONNECT_CLICK_DELAY:g}s), except Play after Create. The first click happens "
                         f"{RECONNECT_FIRST_DELAY:g}s after \"Select\" shows.",
                 font=(App.FONT, 8), fg=App.SUB, bg=App.CARD, anchor="w", justify="left",
                 wraplength=400).pack(fill="x", pady=(2, 0))
        drow = tk.Frame(f, bg=App.CARD)
        drow.pack(fill="x", pady=(8, 0))
        tk.Label(drow, text="Delay between clicks", font=(App.FONT, 10), fg=App.TEXT, bg=App.CARD,
                 width=17, anchor="w").pack(side="left")
        self.delay_var = tk.StringVar(value=f"{reconnect_click_delay():g}")
        self.delay_var.trace_add("write", lambda *_: self.save_soon())
        de = tk.Entry(drow, textvariable=self.delay_var, width=6, font=(App.FONT, 10),
                      justify="center", bg=App.LOGBG, fg=App.TEXT, insertbackground=App.TEXT,
                      relief="flat", highlightthickness=1, highlightbackground=App.BORDER,
                      highlightcolor=App.ACCENT)
        de.pack(side="left", ipady=3, padx=(0, 6))
        de.bind("<FocusOut>", lambda _e: self.commit_spots())
        tk.Label(drow, text="sec", font=(App.FONT, 9), fg=App.SUB, bg=App.CARD).pack(side="left")
        prow = tk.Frame(f, bg=App.CARD)
        prow.pack(fill="x", pady=(6, 0))
        tk.Label(prow, text="Create > Play delay", font=(App.FONT, 10), fg=App.TEXT, bg=App.CARD,
                 width=17, anchor="w").pack(side="left")
        self.play_var = tk.StringVar(value=f"{reconnect_play_delay():g}")
        self.play_var.trace_add("write", lambda *_: self.save_soon())
        pe = tk.Entry(prow, textvariable=self.play_var, width=6, font=(App.FONT, 10),
                      justify="center", bg=App.LOGBG, fg=App.TEXT, insertbackground=App.TEXT,
                      relief="flat", highlightthickness=1, highlightbackground=App.BORDER,
                      highlightcolor=App.ACCENT)
        pe.pack(side="left", ipady=3, padx=(0, 6))
        pe.bind("<FocusOut>", lambda _e: self.commit_spots())
        tk.Label(prow, text="sec", font=(App.FONT, 9), fg=App.SUB, bg=App.CARD).pack(side="left")
        tk.Label(f, text=f"The wait before the Play click that follows Create (default "
                         f"{RECONNECT_PLAY_DELAY:g}s).", font=(App.FONT, 8), fg=App.SUB, bg=App.CARD,
                 anchor="w", justify="left", wraplength=400).pack(fill="x", pady=(4, 0))

        tk.Frame(f, height=1, bg=App.BORDER).pack(fill="x", pady=(14, 10))

        # settings 2-11: the coordinates, in three sub tabs
        tk.Label(f, text="Coordinates", font=(App.FONT, 10, "bold"), fg=App.TEXT, bg=App.CARD,
                 anchor="w").pack(fill="x")
        tk.Label(f, text=f"Press Pick, then click on that spot on your screen. After a rejoin the "
                         f"macro clicks them one by one, top to bottom: the first {RECONNECT_FIRST_DELAY:g}s "
                         f"after \"Select\" shows, the rest one Click delay apart. "
                         f"The first 4 are the same in both tabs. Every coordinate of the tab "
                         f"your macro uses (Missions or Raids) has to be filled in before "
                         f"Reconnect can be turned on.",
                 font=(App.FONT, 8), fg=App.SUB, bg=App.CARD, anchor="w", justify="left",
                 wraplength=400).pack(fill="x", pady=(2, 8))
        self.sub = reconnect_macro_tab()     # opens on the tab the selected macro uses
        tabs = tk.Frame(f, bg=App.CARD)
        tabs.pack(fill="x")
        self.sub_btns = {}
        for tab in RECONNECT_TABS:
            lb = tk.Label(tabs, text=tab, font=(App.FONT, 10, "bold"), padx=16, pady=5,
                          cursor="hand2")
            lb.pack(side="left", padx=(0, 6))
            lb.bind("<Button-1>", lambda _e, n=tab: self.show_sub(n))
            self.sub_btns[tab] = lb
        self.sub_note = tk.Label(f, text="", font=(App.FONT, 8), fg=App.SUB, bg=App.CARD,
                                 anchor="w", justify="left", wraplength=400)
        self.sub_note.pack(fill="x", pady=(8, 0))
        self.sub_host = tk.Frame(f, bg=App.CARD)
        self.sub_host.pack(fill="x")
        self.sub_frames = {}
        for tab in RECONNECT_TABS:
            fr = tk.Frame(self.sub_host, bg=App.CARD)
            for key, name in RECONNECT_TAB_SPOTS[tab]:
                self._spot_row(fr, key, name)
            self.sub_frames[tab] = fr

        tk.Frame(f, height=1, bg=App.BORDER).pack(fill="x", pady=(14, 10))

        # join VIP server (off by default)
        vip = reconnect_vip_settings()
        vrow = tk.Frame(f, bg=App.CARD)
        vrow.pack(fill="x")
        self.vip_on = tk.BooleanVar(value=vip["enabled"])
        ToggleSwitch(vrow, self.vip_on, self.on_vip_toggle, parent_bg=App.CARD).pack(side="right")
        vlbl = tk.Label(vrow, text="Join VIP server", font=(App.FONT, 10, "bold"), fg=App.TEXT,
                        bg=App.CARD, anchor="w", cursor="hand2")
        vlbl.pack(side="left")
        vlbl.bind("<Button-1>", lambda _e: self.vip_on.set(not self.vip_on.get()) or self.on_vip_toggle())
        tk.Label(f, text="Off by default. When on, the reconnect sequence does not click Click play: it clicks "
                         "Click Codes, then VIP server (one Click delay before each), then one Click delay "
                         "later pastes the code below and presses Enter. The rest of the sequence goes on "
                         "as usual. Click play is not needed while this is on. Server owner: the two "
                         "clicks still happen, but the code is not pasted and no code is needed.",
                 font=(App.FONT, 8), fg=App.SUB, bg=App.CARD, anchor="w", justify="left",
                 wraplength=400).pack(fill="x", pady=(4, 0))
        for key, name in RECONNECT_VIP_SPOTS:
            self._spot_row(f, key, name)
        crow = tk.Frame(f, bg=App.CARD)
        crow.pack(fill="x", pady=(6, 0))
        tk.Label(crow, text="Code", font=(App.FONT, 10), fg=App.TEXT, bg=App.CARD,
                 width=17, anchor="w").pack(side="left")
        self.vip_code_var = tk.StringVar(value=vip["code"])
        self.vip_code_var.trace_add("write", lambda *_: self.save_soon())
        ce = tk.Entry(crow, textvariable=self.vip_code_var, width=24, font=(App.FONT, 10),
                      bg=App.LOGBG, fg=App.TEXT, insertbackground=App.TEXT, relief="flat",
                      highlightthickness=1, highlightbackground=App.BORDER, highlightcolor=App.ACCENT)
        ce.pack(side="left", ipady=3, padx=(0, 8))
        ce.bind("<FocusOut>", lambda _e: self.commit_spots())
        sw.flat_button(crow, "Paste", self.vip_code_paste).pack(side="left")
        self.vip_owner = tk.BooleanVar(value=vip["server_owner"])
        switch_row(f, "Server owner (skip the code)", self.vip_owner, self.on_vip_owner_toggle,
                   App.CARD, App.TEXT)

        tk.Frame(f, height=1, bg=App.BORDER).pack(fill="x", pady=(14, 10))

        # add modifiers (off by default)
        self.mod_vars = {}
        mods = modifiers_settings()
        mrow = tk.Frame(f, bg=App.CARD)
        mrow.pack(fill="x")
        self.mod_vars["enabled"] = tk.BooleanVar(value=mods["enabled"])
        ToggleSwitch(mrow, self.mod_vars["enabled"], self.on_mod_toggle,
                     parent_bg=App.CARD).pack(side="right")
        self.mod_test_btn = sw.flat_button(mrow, "Test", self.toggle_mod_test)
        self.mod_test_btn.pack(side="right", padx=(0, 10))
        mlbl = tk.Label(mrow, text="Add modifiers", font=(App.FONT, 10, "bold"), fg=App.TEXT,
                        bg=App.CARD, anchor="w", cursor="hand2")
        mlbl.pack(side="left")
        mlbl.bind("<Button-1>", lambda _e: self.mod_vars["enabled"].set(
            not self.mod_vars["enabled"].get()) or self.on_mod_toggle())
        tk.Label(f, text="Off by default. Only on the first retry window after a reconnect: presses \\ "
                         "(UI navigation), opens Modifiers and presses Enter on the ones switched on "
                         "below, then presses \\ again and the retry goes on as usual. Test runs it once, "
                         f"{MODIFIER_TEST_COUNTDOWN}s after you press it - click into Roblox first.",
                 font=(App.FONT, 8), fg=App.SUB, bg=App.CARD, anchor="w", justify="left",
                 wraplength=400).pack(fill="x", pady=(4, 4))
        is_raid = raid_counting()       # Oddball does not exist in raids: greyed out and always off
        for key, name in MODIFIER_LIST:
            lock = is_raid and key == "oddball"
            self.mod_vars[key] = tk.BooleanVar(value=False if lock else mods[key])
            switch_row(f, name + ("  (not in raids)" if lock else ""), self.mod_vars[key],
                       self.on_mod_toggle, App.CARD, App.TEXT, locked=lock)

        self.note = tk.Label(self, text="", font=(App.FONT, 9), fg=App.SUB, bg=App.BG,
                             anchor="w", justify="left", wraplength=400)
        self.note.pack(side="bottom", fill="x", padx=20, pady=(0, 12))   # always visible under the scroll area
        holder.pack(side="top", fill="both", expand=True, padx=(18, 8), pady=(16, 8))

        self.show_sub(self.sub)
        self.refresh()
        self.update_idletasks()
        # as tall as the content, but never taller than the screen allows
        maxh = max(260, int(self.winfo_screenheight() * 0.80) - self.note.winfo_reqheight() - 40)
        self.canvas.configure(width=self.inner.winfo_reqwidth(),
                              height=min(self.inner.winfo_reqheight(), maxh))
        self.update_idletasks()
        x = sw.winfo_x() + 30
        y = max(0, min(sw.winfo_y() + 30, self.winfo_screenheight() - self.winfo_reqheight() - 60))
        self.geometry(f"+{x}+{y}")
        style_title_bar(self, App.BG)
        nav_attach(self, "reconnect", sw.app)
        reveal_window(self)
        self.lift()
        self.focus_force()

    def _wheel(self, e):
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    # ----- sub tabs -----
    def _spot_row(self, parent, key, name):
        sw = self.sw
        vx, vy = self.spot_vars[key]
        row = tk.Frame(parent, bg=App.CARD)
        row.pack(fill="x", pady=(6, 0))
        tk.Label(row, text=name, font=(App.FONT, 10), fg=App.TEXT, bg=App.CARD, width=17,
                 anchor="w").pack(side="left")
        for axis, var in (("X", vx), ("Y", vy)):
            tk.Label(row, text=axis, font=(App.FONT, 9), fg=App.SUB,
                     bg=App.CARD).pack(side="left", padx=(0, 3))
            e = tk.Entry(row, textvariable=var, width=5, font=(App.FONT, 10),
                         justify="center", bg=App.LOGBG, fg=App.TEXT,
                         insertbackground=App.TEXT, relief="flat", highlightthickness=1,
                         highlightbackground=App.BORDER, highlightcolor=App.ACCENT)
            e.pack(side="left", ipady=3, padx=(0, 8))
            e.bind("<FocusOut>", lambda _e: self.commit_spots())
        sw.flat_button(row, "Pick",
                       lambda k=key, n=name: self.pick_spot(k, n)).pack(side="left")
        clear = tk.Label(row, text="Clear", font=(App.FONT, 8), fg=App.SUB, bg=App.CARD,
                         cursor="hand2")
        clear.pack(side="left", padx=(10, 0))
        clear.bind("<Button-1>", lambda _e, k=key, n=name: self.clear_spot(k, n))
        clear.bind("<Enter>", lambda _e, w=clear: w.configure(fg=App.TEXT))
        clear.bind("<Leave>", lambda _e, w=clear: w.configure(fg=App.SUB))

    def _image_row(self, parent, kind):
        sw = self.sw
        row = tk.Frame(parent, bg=App.CARD)
        row.pack(fill="x", pady=(8, 0))
        tk.Label(row, text=RECONNECT_IMAGE_NAMES[kind], font=(App.FONT, 10), fg=App.TEXT,
                 bg=App.CARD, width=10, anchor="w").pack(side="left")
        box = tk.Frame(row, bg=App.LOGBG, highlightthickness=1, highlightbackground=App.BORDER,
                       width=self.SMALL_W + 10, height=self.SMALL_H + 10)
        box.pack(side="left")
        box.pack_propagate(False)
        lbl = tk.Label(box, text="No picture", font=(App.FONT, 8), fg=App.SUB, bg=App.LOGBG)
        lbl.pack(expand=True)
        self.img_lbls[kind] = lbl
        sw.flat_button(row, "Change", lambda k=kind: self.img_change(k)).pack(side="left", padx=(8, 0))
        sw.flat_button(row, "Paste", lambda k=kind: self.img_paste(k)).pack(side="left", padx=(4, 0))
        rm = tk.Label(row, text="Remove", font=(App.FONT, 8), fg=App.SUB, bg=App.CARD,
                      cursor="hand2")
        rm.pack(side="left", padx=(10, 0))
        rm.bind("<Button-1>", lambda _e, k=kind: self.img_remove(k))
        rm.bind("<Enter>", lambda _e, w=rm: w.configure(fg=App.TEXT))
        rm.bind("<Leave>", lambda _e, w=rm: w.configure(fg=App.SUB))

    def _make_thumb(self, filename, w, h):
        path = override_path(filename)
        img = _read_image(path) if path else None
        if img is None:
            return None
        ih, iw = img.shape[:2]
        k = min(w / iw, h / ih, 3.0)
        img = cv2.resize(img, (max(1, int(iw * k)), max(1, int(ih * k))),
                         interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)
        ok, buf = cv2.imencode(".png", img)
        return tk.PhotoImage(data=base64.b64encode(buf.tobytes())) if ok else None

    def refresh_images(self):
        for kind, filename in RECONNECT_ALL_IMAGES.items():
            photo = self._make_thumb(filename, self.SMALL_W, self.SMALL_H)
            self.img_thumbs[kind] = photo            # keep the PhotoImage alive
            lbl = self.img_lbls.get(kind)
            if lbl is not None:
                if photo is not None:
                    lbl.configure(image=photo, text="")
                else:
                    lbl.configure(image="", text="No picture")

    def img_change(self, kind):
        name = RECONNECT_IMAGE_NAMES[kind]
        path = filedialog.askopenfilename(
            parent=self, title=f'Pick your "{name}" picture',
            filetypes=[("Pictures", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        self.lift()
        if not path:
            return
        if import_override_picture(path, RECONNECT_ALL_IMAGES[kind]):
            self.refresh()
            self.say(f'{name}: your picture is saved.', good=True)
        else:
            self.say("Could not read that picture.", error=True)

    def img_paste(self, kind):
        name = RECONNECT_IMAGE_NAMES[kind]
        img = clipboard_image()
        if img is None:
            self.say("No screenshot on the clipboard. Take one first (Win+Shift+S), then press "
                     "Paste.", error=True)
        elif save_override_image(img, RECONNECT_ALL_IMAGES[kind]):
            self.refresh()
            self.say(f"{name}: pasted from the clipboard.", good=True)
        else:
            self.say("Could not save the picture.", error=True)

    def img_remove(self, kind):
        name = RECONNECT_IMAGE_NAMES[kind]
        if remove_override_picture(RECONNECT_ALL_IMAGES[kind]):
            self.refresh()
            self.say(f"{name} picture removed.", good=True)
        else:
            self.say("Could not remove your picture.", error=True)

    def show_sub(self, name):
        self.sub = name
        for tab, fr in self.sub_frames.items():
            if tab == name:
                fr.pack(fill="x")
            else:
                fr.pack_forget()
        self.style_subtabs()

    def style_subtabs(self):
        """Tab colours + a '!' on the tab of the selected macro while it has an empty coordinate."""
        if not hasattr(self, "sub_frames"):
            return
        used = reconnect_macro_tab()
        spots = settings["reconnect"]["spots"]
        for tab, lb in self.sub_btns.items():
            on = tab == self.sub
            incomplete = (tab == used
                          and any(spots.get(k) is None for k, _n in reconnect_tab_spots(tab)))
            lb.configure(text=tab + ("  !" if incomplete else ""),
                         bg=App.ACCENT_BG if on else App.LOGBG,
                         fg="#ffffff" if on else ("#fbbf24" if incomplete else App.SUB))
        if self.sub == used:
            txt = (f"Your macro ({settings['family']}) uses this tab: all {len(reconnect_tab_spots(tab))} "
                   f"coordinates have to be filled in. The shared ones are the same in the other tab.")
        else:
            txt = (f"Not used by your macro ({settings['family']}) - it uses the {used} tab. "
                   f"The first 4 are shared with that tab.")
        self.sub_note.configure(text=txt)

    # ----- state -----
    def say(self, msg, error=False, good=False):
        self.note.configure(text=msg, fg="#f87171" if error else "#34d399" if good else App.SUB)
        if self.say_job:
            self.after_cancel(self.say_job)
            self.say_job = None
        if msg and not error:
            self.say_job = self.after(6000, lambda: self.note.configure(text=""))

    def refresh(self):
        """The thumbnails, the 'still needed' line and the switch follow what is set right now."""
        self.refresh_images()
        self.update_requirements()

    def update_requirements(self):
        """The 'still needed' line and the switch follow what is set right now."""
        if hasattr(self, "vip_on") and self.vip_on.get() and reconnect_vip_missing():
            self.vip_on.set(False)              # something Join VIP server needs is gone: switch it off
            settings["reconnect"]["vip"]["enabled"] = False
            save_stats()
        missing = reconnect_missing()
        if missing:
            self.req.configure(text="Fill in before Reconnect can be turned on: "
                                    + ", ".join(missing), fg="#fbbf24")
            if self.v_on.get():                 # something it needs is gone: switch it off
                self.v_on.set(False)
                settings["reconnect"]["enabled"] = False
                save_stats()
        else:
            self.req.configure(text="Everything Reconnect needs is set.", fg="#34d399")
        self.style_subtabs()
        self.sw.update_reconnect_status()

    def on_toggle(self):
        if self.v_on.get() and reconnect_missing():
            self.v_on.set(False)                # can't be turned on yet
            self.say("Fill in " + ", ".join(reconnect_missing()) + " first", error=True)
            return
        settings["reconnect"]["enabled"] = bool(self.v_on.get())
        save_stats()
        self.sw.update_reconnect_status()
        self.say("Reconnect is on" if self.v_on.get() else "Reconnect is off", good=self.v_on.get())

    # ----- join VIP server -----
    def vip_code_paste(self):
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            text = ""
        if not text:
            self.say("Nothing to paste: copy the VIP code first", error=True)
            return
        self.vip_code_var.set(text)
        self.commit_spots()
        self.say("VIP code pasted", good=True)

    def on_vip_owner_toggle(self):
        settings["reconnect"]["vip"]["server_owner"] = bool(self.vip_owner.get())
        save_stats()
        self.update_requirements()          # no code needed any more (or needed again)
        self.say("Server owner is on: the code will not be pasted" if self.vip_owner.get()
                 else "Server owner is off: the code is pasted again", good=self.vip_owner.get())

    def on_vip_toggle(self):
        want = bool(self.vip_on.get())
        self.commit_spots()                     # what was just typed / picked counts
        if want and reconnect_vip_missing():
            self.vip_on.set(False)              # can't be turned on yet
            self.say("Fill in " + ", ".join(reconnect_vip_missing()) + " first", error=True)
            return
        settings["reconnect"]["vip"]["enabled"] = bool(self.vip_on.get())
        save_stats()
        self.update_requirements()
        self.say("Join VIP server is on" if self.vip_on.get() else "Join VIP server is off",
                 good=self.vip_on.get())

    # ----- add modifiers -----
    def on_mod_toggle(self):
        new = {k: bool(v.get()) for k, v in self.mod_vars.items()}
        if raid_counting():             # Oddball is locked off in raids: keep what a mission macro saved
            new["oddball"] = bool(settings["reconnect"].get("modifiers", {}).get("oddball", False))
        settings["reconnect"]["modifiers"] = new
        save_stats()
        if self.mod_vars["enabled"].get():
            on = [name for key, name in MODIFIER_LIST if self.mod_vars[key].get()]
            self.say("Add modifiers is on (" + (", ".join(on) or "no modifier picked") + ")", good=True)
        else:
            self.say("Add modifiers is off")

    def set_mod_test_button(self, running):
        try:
            self.mod_test_btn.configure(text="Stop test" if running else "Test")
        except tk.TclError:
            pass

    def toggle_mod_test(self):
        if modifier_test_running():
            modifier_test_stop()
            return
        if worker is not None and worker.is_alive():
            self.say("Stop the macro first, then test the modifiers", error=True)
            return
        self.on_mod_toggle()
        modifier_test_start()
        self.set_mod_test_button(True)
        self.after(100, self.poll_mod_test)

    def poll_mod_test(self):
        try:
            while True:
                msg, kind = _mod_test["msgs"].get_nowait()
                if kind == "done":
                    self.set_mod_test_button(False)
                    return
                self.say(msg, error=(kind == "error"), good=(kind == "good"))
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if modifier_test_running() or not _mod_test["msgs"].empty():
            self.after(100, self.poll_mod_test)
        else:
            self.set_mod_test_button(False)

    # ----- the coordinates -----
    def pick_spot(self, key, name):
        def picked(x, y):
            self.spot_vars[key][0].set(str(x))
            self.spot_vars[key][1].set(str(y))
            self.commit_spots()
            self.say(f"{name} set to {x}, {y}", good=True)
        pick_screen_spot(self, f"Click on: {name}   (Esc to cancel)", picked)

    def clear_spot(self, key, name):
        self.spot_vars[key][0].set("")
        self.spot_vars[key][1].set("")
        self.commit_spots()
        self.say(f"{name} cleared", good=True)

    def save_soon(self):
        if self.save_job:
            self.after_cancel(self.save_job)
        self.save_job = self.after(500, self.commit_spots)

    def commit_spots(self):
        if self.save_job:
            try:
                self.after_cancel(self.save_job)
            except tk.TclError:
                pass
            self.save_job = None
        spots = settings["reconnect"]["spots"]
        if hasattr(self, "vip_code_var"):
            settings["reconnect"]["vip"]["code"] = self.vip_code_var.get().strip()
        if hasattr(self, "delay_var"):
            try:
                d = float(self.delay_var.get().strip().replace(",", "."))
                lo, hi = RECONNECT_CLICK_DELAY_RANGE
                settings["reconnect"]["click_delay"] = float(min(hi, max(lo, d)))
            except ValueError:
                pass                            # empty / half typed: keep the last good value
        if hasattr(self, "play_var"):
            try:
                d = float(self.play_var.get().strip().replace(",", "."))
                lo, hi = RECONNECT_CLICK_DELAY_RANGE
                settings["reconnect"]["play_delay"] = float(min(hi, max(lo, d)))
            except ValueError:
                pass
        for key, (vx, vy) in self.spot_vars.items():
            try:
                x, y = int(vx.get()), int(vy.get())
                if x >= 0 and y >= 0:
                    spots[key] = [x, y]
            except ValueError:
                if not vx.get().strip() and not vy.get().strip():
                    spots[key] = None           # both empty = not set
        save_stats()
        self.update_requirements()

    def close(self):
        if modifier_test_running():
            modifier_test_stop()
        self.commit_spots()
        self.destroy()


class ConfigEditorWindow(tk.Toplevel):
    """Every timing of the routine, one part at a time: Movement + the three ability configs.
    Pick the part in the dropdown at the top and change a value - it is checked and saved at once
    (settings["timings"]); the macro reads it the next time it needs it."""

    def __init__(self, settings_win):
        super().__init__(settings_win)
        prepare_window(self)
        self.sw = settings_win
        self.section = (settings["config_section"] if settings.get("config_section") in CONFIG_SECTIONS
                        else CONFIG_SECTIONS[0])      # opens on the part picked last time
        self.rows_section = self.section   # the part the boxes on screen belong to
        self.rows = {}              # key -> (spec, [StringVar], [Entry], name label)
        self.save_job = None
        self.say_job = None
        self.title("ymir - edit configs")
        try:
            self.iconbitmap(asset_path(ICON_FILE))
        except (tk.TclError, OSError):
            pass
        self.configure(bg=App.BG)
        self.transient(settings_win)
        self.attributes("-topmost", True)
        self.minsize(480, 380)
        self.protocol("WM_DELETE_WINDOW", self.close)

        # bottom bar first, so it always stays visible
        bottom = tk.Frame(self, bg=App.BG)
        bottom.pack(side="bottom", fill="x", padx=18, pady=(8, 14))
        self.sw.flat_button(bottom, "Reset this config", self.reset_section).pack(side="left")
        self.note = tk.Label(bottom, text="", font=(App.FONT, 9), fg=App.SUB, bg=App.BG,
                             anchor="w", justify="left", wraplength=280)
        self.note.pack(side="left", padx=(12, 0), fill="x", expand=True)

        top = tk.Frame(self, bg=App.BG)
        top.pack(side="top", fill="x", padx=18, pady=(14, 0))
        tk.Label(top, text="CONFIG", font=(App.FONT, 8, "bold"), fg=App.SUB, bg=App.BG,
                 anchor="w").pack(fill="x")
        items = [(sec, sec, CONFIG_BADGES[sec], None) for sec in CONFIG_SECTIONS]
        self.dd = ModernDropdown(top, items, self.section, self.on_pick)
        self.dd.pack(fill="x", pady=(4, 0))
        self.info = tk.Label(self, text="", font=(App.FONT, 9), fg=App.SUB, bg=App.BG,
                             anchor="w", justify="left", wraplength=440)
        self.info.pack(side="top", fill="x", padx=18, pady=(6, 6))

        wrap = tk.Frame(self, bg=App.BG)
        wrap.pack(side="top", fill="both", expand=True, padx=(18, 8))
        self.canvas = tk.Canvas(wrap, bg=App.BG, highlightthickness=0, bd=0)
        self.scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=App.BG)
        self.inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self.inner_id, width=e.width))
        self.bind("<MouseWheel>", self._wheel)

        self.build_rows()
        self.update_idletasks()
        x, y = settings_win.winfo_x() + 30, settings_win.winfo_y() + 30
        self.geometry(f"520x640+{x}+{y}")
        style_title_bar(self, App.BG)
        nav_attach(self, "edit configs", settings_win.app)
        reveal_window(self)
        self.lift()
        self.focus_force()

    def _wheel(self, e):
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    # ----- rows -----
    def build_rows(self):
        """Show the fields of the picked part (the caller has already committed the old ones)."""
        self.rows = {}      # first: a focus-out while the old boxes are destroyed must save nothing
        for child in self.inner.winfo_children():
            child.destroy()
        self.rows_section = self.section
        active = " (active now)" if self.section == settings["family"] else ""
        self.info.configure(text=CONFIG_INFO[self.section] + active)
        group, card = None, None
        hidden = CONFIG_HIDDEN.get(self.section, ())
        for spec in CONFIG_FIELDS[self.section]:
            if spec[0] in hidden:
                continue
            if spec[6] != group:
                group = spec[6]
                card = self._card(group)
            self._row(card, spec)
        self.update_idletasks()
        self.canvas.yview_moveto(0)

    def _card(self, title):
        outer = tk.Frame(self.inner, bg=App.CARD, highlightbackground=App.BORDER,
                         highlightthickness=App.CARD_BW)
        outer.pack(fill="x", pady=4)
        inner = tk.Frame(outer, bg=App.CARD)
        inner.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(inner, text=title.upper(), font=(App.FONT, 8, "bold"), fg=App.SUB, bg=App.CARD,
                 anchor="w").pack(fill="x", pady=(0, 8))
        for w in (outer, inner):
            w.bind("<MouseWheel>", self._wheel)
        return inner

    def _bool_row(self, parent, spec):
        key, label, _kind, _default, _lo, _hi, _grp, hint = spec
        row = tk.Frame(parent, bg=App.CARD)
        row.pack(fill="x", pady=(0, 10))
        line = tk.Frame(row, bg=App.CARD)
        line.pack(fill="x")
        edited = key in settings["timings"].get(self.section, {})
        name = tk.Label(line, text=label, font=(App.FONT, 10), fg=App.ACCENT if edited else App.TEXT,
                        bg=App.CARD, anchor="w", cursor="hand2")
        name.pack(side="left")
        var = tk.BooleanVar(value=bool(tv(self.section, key)))
        sw = ToggleSwitch(line, var, self.commit, parent_bg=App.CARD)   # saves right away
        sw.pack(side="right")
        name.bind("<Button-1>", sw.flip)
        tk.Label(row, text=hint, font=(App.FONT, 8), fg=App.SUB, bg=App.CARD, anchor="w",
                 justify="left", wraplength=400).pack(fill="x", pady=(3, 0))
        for w in (row, line, name, sw):
            w.bind("<MouseWheel>", self._wheel)
        self.rows[key] = (spec, [var], [], name)

    def _spot_row(self, parent, spec):
        """A coordinate: X / Y boxes + a Pick button (click on the screen) + Clear."""
        key, label, _kind, _default, lo, hi, _grp, hint = spec
        cur = tv(self.section, key)
        row = tk.Frame(parent, bg=App.CARD)
        row.pack(fill="x", pady=(0, 10))
        line = tk.Frame(row, bg=App.CARD)
        line.pack(fill="x")
        edited = key in settings["timings"].get(self.section, {})
        name = tk.Label(line, text=label, font=(App.FONT, 10), fg=App.ACCENT if edited else App.TEXT,
                        bg=App.CARD, anchor="w")
        name.pack(side="left")
        box = tk.Frame(line, bg=App.CARD)
        box.pack(side="right")
        vars_, entries = [], []
        for i, axis in enumerate(("X", "Y")):
            tk.Label(box, text=axis, font=(App.FONT, 9), fg=App.SUB,
                     bg=App.CARD).pack(side="left", padx=(0 if i == 0 else 8, 4))
            var = tk.StringVar(value="" if cur is None else str(cur[i]))
            e = tk.Entry(box, textvariable=var, width=6, font=(App.FONT, 10), justify="center",
                         bg=App.LOGBG, fg=App.TEXT, insertbackground=App.TEXT, relief="flat",
                         highlightthickness=1, highlightbackground=App.BORDER,
                         highlightcolor=App.ACCENT)
            e.pack(side="left", ipady=3)
            e.bind("<Return>", lambda _e: self.commit())
            e.bind("<FocusOut>", lambda _e: self.commit())
            var.trace_add("write", lambda *_: self.save_soon())
            vars_.append(var)
            entries.append(e)
        btns = tk.Frame(row, bg=App.CARD)
        btns.pack(fill="x", pady=(6, 0))
        self.sw.flat_button(btns, "Pick spot",
                            lambda: self.pick_spot(key, label)).pack(side="left")
        clear = tk.Label(btns, text="Clear", font=(App.FONT, 9), fg=App.SUB, bg=App.CARD,
                         cursor="hand2")
        clear.pack(side="left", padx=(14, 0))
        clear.bind("<Button-1>", lambda _e: self.clear_spot(key, label))
        clear.bind("<Enter>", lambda _e: clear.configure(fg=App.TEXT))
        clear.bind("<Leave>", lambda _e: clear.configure(fg=App.SUB))
        tk.Label(row, text=hint, font=(App.FONT, 8), fg=App.SUB, bg=App.CARD, anchor="w",
                 justify="left", wraplength=400).pack(fill="x", pady=(3, 0))
        for w in (row, line, name, btns):
            w.bind("<MouseWheel>", self._wheel)
        self.rows[key] = (spec, vars_, entries, name)

    def pick_spot(self, key, label):
        row = self.rows.get(key)
        if row is None:
            return

        def picked(x, y):
            if key not in self.rows:      # the part was switched while the picker was open
                return
            row[1][0].set(str(x))
            row[1][1].set(str(y))
            self.commit()
            self.say(f"{label} set to {x}, {y}", good=True)
        pick_screen_spot(self, f"Click on {label}   (Esc to cancel)", picked)

    def clear_spot(self, key, label):
        row = self.rows.get(key)
        if row is None:
            return
        row[1][0].set("")
        row[1][1].set("")
        self.commit()
        self.say(f"{label} cleared", good=True)

    def _row(self, parent, spec):
        key, label, kind, _default, lo, hi, _grp, hint = spec
        if kind == "bool":
            self._bool_row(parent, spec)
            return
        if kind == "spot":
            self._spot_row(parent, spec)
            return
        unit = "px" if kind == "pix" else "" if kind == "num" else "s"
        cur = tv(self.section, key)
        values = list(cur) if kind == "range" else [cur]
        row = tk.Frame(parent, bg=App.CARD)
        row.pack(fill="x", pady=(0, 10))
        line = tk.Frame(row, bg=App.CARD)
        line.pack(fill="x")
        edited = key in settings["timings"].get(self.section, {})
        name = tk.Label(line, text=label, font=(App.FONT, 10), fg=App.ACCENT if edited else App.TEXT,
                        bg=App.CARD, anchor="w")
        name.pack(side="left")
        box = tk.Frame(line, bg=App.CARD)
        box.pack(side="right")
        vars_, entries = [], []
        for i, v in enumerate(values):
            if i:
                tk.Label(box, text="-", font=(App.FONT, 10), fg=App.SUB,
                         bg=App.CARD).pack(side="left", padx=4)
            var = tk.StringVar(value=f"{v:g}")
            e = tk.Entry(box, textvariable=var, width=7, font=(App.FONT, 10), justify="center",
                         bg=App.LOGBG, fg=App.TEXT, insertbackground=App.TEXT, relief="flat",
                         highlightthickness=1, highlightbackground=App.BORDER,
                         highlightcolor=App.ACCENT)
            e.pack(side="left", ipady=3)
            e.bind("<Return>", lambda _e: self.commit())
            e.bind("<FocusOut>", lambda _e: self.commit())
            var.trace_add("write", lambda *_: self.save_soon())
            vars_.append(var)
            entries.append(e)
        tk.Label(box, text=unit, font=(App.FONT, 10), fg=App.TEXT, bg=App.CARD,
                 width=2, anchor="w").pack(side="left", padx=(6, 0))
        limits = f"Allowed: {lo:g} to {hi:g}{unit}."
        tk.Label(row, text=f"{hint} {limits}" if hint else limits, font=(App.FONT, 8), fg=App.SUB,
                 bg=App.CARD, anchor="w", justify="left", wraplength=400).pack(fill="x", pady=(3, 0))
        for w in (row, line, name):
            w.bind("<MouseWheel>", self._wheel)
        self.rows[key] = (spec, vars_, entries, name)

    # ----- saving -----
    def save_soon(self):
        if self.save_job:
            self.after_cancel(self.save_job)
        self.save_job = self.after(400, self.commit)

    def commit(self):
        """Check every box of the shown part and save it (bad boxes turn red and are not saved)."""
        if self.save_job:
            try:
                self.after_cancel(self.save_job)
            except tk.TclError:
                pass
            self.save_job = None
        if not self.rows:
            return
        section = self.rows_section
        store = settings["timings"].setdefault(section, {})
        bad = 0
        try:
            for key, (spec, vars_, entries, name) in self.rows.items():
                if spec[2] == "bool":
                    val, valid = bool(vars_[0].get()), True
                elif spec[2] == "spot":
                    valid, val = parse_spot_text(spec, [v.get() for v in vars_])
                else:
                    val = parse_cfg_text(spec, [v.get() for v in vars_])
                    valid = val is not None
                if not valid:
                    bad += 1
                    for e in entries:
                        e.configure(highlightbackground="#f87171", highlightcolor="#f87171")
                    continue
                for e in entries:
                    e.configure(highlightbackground=App.BORDER, highlightcolor=App.ACCENT)
                default = spec[3]
                same = (tuple(val) == tuple(default)) if spec[2] == "range" else val == default   # spot: None = not set
                if same:
                    store.pop(key, None)
                else:
                    store[key] = val
                name.configure(fg=App.ACCENT if key in store else App.TEXT)
        except tk.TclError:
            return                       # the window is closing
        if not store:
            settings["timings"].pop(section, None)
        save_timings()
        if bad:
            self.say("Some values are not valid numbers (or out of range) - they are marked red and not saved.",
                     error=True)

    def on_pick(self, section):
        if section == self.section:
            return
        self.commit()
        self.section = section
        settings["config_section"] = section
        save_stats()                    # remembered for the next time (also after a restart)
        self.build_rows()

    def reset_section(self):
        if self.save_job:
            self.after_cancel(self.save_job)
            self.save_job = None
        keep = {k: v for k, v in settings["timings"].get(self.section, {}).items()
                if k in CONFIG_HIDDEN.get(self.section, ())}    # chest spots are not reset here
        if keep:
            settings["timings"][self.section] = keep
        else:
            settings["timings"].pop(self.section, None)
        save_timings()
        self.build_rows()
        self.say(f"{self.section}: back to the defaults.", good=True)

    def say(self, msg, error=False, good=False):
        self.note.configure(text=msg, fg="#f87171" if error else "#34d399" if good else App.SUB)
        if self.say_job:
            self.after_cancel(self.say_job)
        self.say_job = self.after(5000, lambda: self.note.configure(text=""))

    def close(self):
        self.commit()
        fade_close(self)


class GuideWindow(tk.Toplevel):
    """Guide: shows guides/build_<name>.md of the picked config (Blood Awakening -> build_blood.md,
    see GUIDE_NAMES). No tabs, just the one page.
    Markdown: # headings, **bold**, *italic*, `code`, ``` blocks, - / 1. lists, > quotes, ---, [links](url),
    and pictures on their own line: ![text](images/pic.png) - the path is inside the guides folder
    (png / jpg / webp / bmp / gif), big pictures are shrunk to fit the window width."""

    def __init__(self, settings_win, family):
        super().__init__(settings_win)
        prepare_window(self)
        self.sw = settings_win
        self.family = family
        self.title(f"ymir - guide - {family}")
        try:
            self.iconbitmap(asset_path(ICON_FILE))
        except (tk.TclError, OSError):
            pass
        self.configure(bg=App.BG)
        self.transient(settings_win)
        self.attributes("-topmost", True)
        self.minsize(460, 400)
        self.protocol("WM_DELETE_WINDOW", lambda: fade_close(self))

        wrap = tk.Frame(self, bg=App.CARD, highlightbackground=App.BORDER,
                        highlightthickness=App.CARD_BW)
        wrap.pack(side="top", fill="both", expand=True, padx=18, pady=16)
        self.text = tk.Text(wrap, bg=App.CARD, fg=App.TEXT, font=(App.FONT, 10), wrap="word",
                            relief="flat", bd=0, padx=16, pady=12, cursor="arrow",
                            highlightthickness=0, spacing1=2, spacing3=2)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.setup_tags()
        self.dividers = []          # ridge lines (# / ##) embedded in the text, stretched to the window width
        self.div_end = "1.0"
        self.photos = []            # keeps the shown pictures alive (tk drops them otherwise)
        self.has_images = False
        self.img_w = 0              # window width the pictures were scaled for
        self.refit_job = None
        self.text.bind("<Configure>", lambda _e: self.on_resize(), add="+")
        self.text.bind("<Key>", lambda e: "break" if e.state & 4 == 0 else None)   # read-only
        self.bind("<MouseWheel>", lambda e: self.text.yview_scroll(-1 if e.delta > 0 else 1, "units"))

        self.geometry(f"560x640+{settings_win.winfo_x() + 40}+{settings_win.winfo_y() + 40}")
        self.load()
        style_title_bar(self, App.BG)
        nav_attach(self, "guide", settings_win.app)
        reveal_window(self)
        self.lift()
        self.focus_force()

    def setup_tags(self):
        t, f = self.text, App.FONT
        t.tag_configure("h1", font=(f, 16, "bold"), spacing1=10, spacing3=6)
        t.tag_configure("h2", font=(f, 13, "bold"), foreground=App.ACCENT_TEXT, spacing1=10, spacing3=4)
        t.tag_configure("h3", font=(f, 11, "bold"), spacing1=8, spacing3=2)
        t.tag_configure("bold", font=(f, 10, "bold"))
        t.tag_configure("italic", font=(f, 10, "italic"))
        t.tag_configure("bolditalic", font=(f, 10, "bold italic"))
        t.tag_configure("code", font=(App.MONO, 9), background=App.LOGBG, foreground=App.ACCENT_SOFT)
        t.tag_configure("codeblock", font=(App.MONO, 9), background=App.LOGBG, lmargin1=12,
                        lmargin2=12, spacing1=0, spacing3=0)
        t.tag_configure("quote", foreground=App.SUB, lmargin1=14, lmargin2=14)
        t.tag_configure("bullet", lmargin1=14, lmargin2=30)
        t.tag_configure("rule", font=(f, 4), foreground=App.BORDER)
        t.tag_configure("dim", foreground=App.SUB)
        t.tag_configure("link", foreground=App.ACCENT_SOFT, underline=True)
        t.tag_bind("link", "<Enter>", lambda _e: t.configure(cursor="hand2"))
        t.tag_bind("link", "<Leave>", lambda _e: t.configure(cursor="arrow"))

    def set_family(self, family):
        self.family = family
        self.title(f"ymir - guide - {family}")
        self.load()

    def load(self):
        name = GUIDE_NAMES.get(self.family, re.sub(r"\W+", "_", self.family.lower()))
        fname = f"build_{name}.md"
        try:
            with open(guide_path(fname), "r", encoding="utf-8-sig") as fh:
                md = fh.read()
        except (OSError, UnicodeDecodeError):
            md = None
        t = self.text
        t.configure(state="normal")
        for d in self.dividers:
            d.destroy()
        self.dividers = []
        self.photos = []
        self.has_images = False
        t.delete("1.0", "end")
        self.div_end = "1.0"
        if md is None:
            t.insert("end", f"Nothing here yet.\n", "h3")
            t.insert("end", f"Add {GUIDE_FOLDER}/{fname} next to the macro and it shows up here.", "dim")
        else:
            self.render(md)
        t.configure(state="disabled")
        t.yview_moveto(0)

    # ----- pictures -----
    def on_resize(self):
        self.fit_dividers()
        w = self.text.winfo_width()
        if self.has_images and abs(w - self.img_w) > 24:      # re-scale the pictures to the new width
            if self.refit_job:
                self.after_cancel(self.refit_job)
            self.refit_job = self.after(200, self.refit_images)

    def refit_images(self):
        self.refit_job = None
        if not self.winfo_exists():
            return
        frac = self.text.yview()[0]
        self.load()
        self.text.yview_moveto(frac)

    def load_image(self, rel):
        """guides/<rel> -> tk.PhotoImage scaled to the window width (no PIL needed: cv2 decodes it)."""
        rel = urllib.parse.unquote(rel.strip()).replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        try:
            data = np.fromfile(guide_path(rel), dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
        except (OSError, ValueError):
            return None
        if img is None:
            return None
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        if img.shape[2] == 4:                       # transparent picture: blend onto the card colour
            try:
                c = App.CARD.lstrip("#")
                bg = np.array([int(c[4:6], 16), int(c[2:4], 16), int(c[0:2], 16)], dtype=np.float32)
            except (ValueError, IndexError, AttributeError):
                bg = np.zeros(3, dtype=np.float32)
            a = img[:, :, 3:4].astype(np.float32) / 255.0
            img = (img[:, :, :3].astype(np.float32) * a + bg * (1 - a)).astype(np.uint8)
        elif img.dtype != np.uint8:
            img = cv2.convertScaleAbs(img, alpha=255.0 / max(1, float(img.max())))
        tw = self.text.winfo_width()
        self.img_w = tw
        max_w = max(120, (tw if tw > 100 else 520) - 56)
        h, w = img.shape[:2]
        if w > max_w:
            img = cv2.resize(img, (max_w, max(1, int(h * max_w / w))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return None
        return tk.PhotoImage(data=base64.b64encode(buf.tobytes()))

    # ----- ridges between sections -----
    def fit_dividers(self):
        w = max(40, self.text.winfo_width() - 40)
        for d in self.dividers:
            d.configure(width=w)

    def divider(self, kind):
        """kind 'h1': thick accent bar under a # title. 'h2': double ridge line above a ## section."""
        t = self.text
        if kind == "h1":
            box = tk.Frame(t, height=3, bg=App.ACCENT)
        else:
            box = tk.Frame(t, height=5, bg=App.CARD)
            box.pack_propagate(False)
            tk.Frame(box, height=1, bg=App.BORDER).pack(side="top", fill="x")
            tk.Frame(box, height=1, bg=App.CARD).pack(side="top", fill="x")
            tk.Frame(box, height=2, bg=App.BORDER).pack(side="top", fill="x")
        box.configure(width=max(40, t.winfo_width() - 40))
        self.dividers.append(box)
        t.window_create("end", window=box, align="center")
        t.insert("end", "\n", "rule")
        self.div_end = t.index("end-1c")

    # ----- tiny markdown renderer -----
    INLINE = re.compile(r"(\*\*\*.+?\*\*\*|\*\*.+?\*\*|(?<![\w*])\*[^*\s][^*]*?\*(?![\w*])|`[^`]+`|\[[^\]]+\]\([^)]+\))")

    def inline(self, text, *tags):
        t = self.text
        pos = 0
        for m in self.INLINE.finditer(text):
            if m.start() > pos:
                t.insert("end", text[pos:m.start()], tags)
            tok = m.group(0)
            if tok.startswith("***"):
                t.insert("end", tok[3:-3], tags + ("bolditalic",))
            elif tok.startswith("**"):
                t.insert("end", tok[2:-2], tags + ("bold",))
            elif tok.startswith("`"):
                t.insert("end", tok[1:-1], tags + ("code",))
            elif tok.startswith("["):
                label, url = re.match(r"\[([^\]]+)\]\(([^)]+)\)", tok).groups()
                tag = f"link{len(t.tag_names())}"
                t.tag_configure(tag, foreground=App.ACCENT_SOFT, underline=True)
                t.tag_bind(tag, "<Button-1>", lambda _e, u=url: webbrowser.open(u))
                t.tag_bind(tag, "<Enter>", lambda _e: t.configure(cursor="hand2"))
                t.tag_bind(tag, "<Leave>", lambda _e: t.configure(cursor="arrow"))
                t.insert("end", label, tags + (tag,))
            else:
                t.insert("end", tok[1:-1], tags + ("italic",))
            pos = m.end()
        if pos < len(text):
            t.insert("end", text[pos:], tags)

    def render(self, md):
        t = self.text
        in_code = False
        for raw in md.replace("\r\n", "\n").split("\n"):
            line = raw.rstrip()
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                t.insert("end", (line or " ") + "\n", "codeblock")
                continue
            stripped = line.strip()
            if not stripped:
                t.insert("end", "\n", "dim")
                continue
            m = re.fullmatch(r'!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+"[^"]*")?\s*\)', stripped)
            if m:
                self.has_images = True
                photo = self.load_image(m.group(2))
                if photo is None:
                    t.insert("end", f"[picture not found: {m.group(2)}]\n", "dim")
                else:
                    self.photos.append(photo)
                    t.image_create("end", image=photo, padx=0, pady=6)
                    t.insert("end", "\n")
                continue
            m = re.match(r"(#{1,3})\s+(.*)", stripped)
            if m:
                level = len(m.group(1))
                if level == 2 and t.index("end-1c") not in ("1.0", self.div_end):
                    self.divider("h2")      # ridge marks where a new ## section starts
                self.inline(m.group(2), f"h{level}")
                t.insert("end", "\n")
                if level == 1:
                    self.divider("h1")      # bar under the # title
                continue
            if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", stripped):
                t.insert("end", "\u2500" * 48 + "\n", "rule")
                continue
            m = re.match(r"[-*+]\s+(.*)", stripped)
            if m:
                indent = (len(line) - len(line.lstrip())) // 2
                t.insert("end", "\u2022 ", "bullet")
                self.inline(m.group(1), "bullet")
                t.insert("end", "\n")
                continue
            m = re.match(r"(\d+)[.)]\s+(.*)", stripped)
            if m:
                t.insert("end", f"{m.group(1)}. ", "bullet")
                self.inline(m.group(2), "bullet")
                t.insert("end", "\n")
                continue
            if stripped.startswith(">"):
                self.inline(stripped.lstrip("> ").strip(), "quote")
                t.insert("end", "\n")
                continue
            self.inline(stripped, )
            t.insert("end", "\n")


ASSET_LIST = (
    # (label, file name relative to the image folder) - normal quality pictures only
    ("Retry button text", TEMPLATE_FILE),
    ("Retry button (whole)", ALT_RETRY_FILE),
    ("Mission start (ability 2 icon)", START_TEMPLATE_FILE),
    ("Shiki ability 4 icon", SHIKI_ABILITY4_FILE),
    ("Grabbed text", GRAB_FILE),
    ("Female Raid: skip button", RAID_SKIP_FILE),
    ("Female Raid: raid start", RAID_START_FILE),
    ("Female Raid: finish button", RAID_FINISH_FILE),
    ("Female Raid: heatwave", RAID_HEATWAVE_FILE),
    ("Result: Completed", RESULT_FILES["completed"]),
    ("Result: Failed", RESULT_FILES["failed"]),
    ("Loot: Scroll", LOOT_FILES["scroll"]),
    ("Loot: Body fragment", LOOT_FILES["body"]),
    ("Loot: Defence fragment", LOOT_FILES["defence"]),
    ("Loot: Offence fragment", LOOT_FILES["offence"]),
    ("Loot: Support fragment", LOOT_FILES["support"]),
    ("Loot: Vessel (Female Raid)", LOOT_FILES["vessel"]),
    ("Loot: Attack serum", LOOT_FILES["attack_serum"]),
    ("Loot: Armor serum", LOOT_FILES["armor_serum"]),
    ("Loot: Female serum", LOOT_FILES["female_serum"]),
    ("Loot: Beast serum", LOOT_FILES["beast_serum"]),
    ("Loot: Colossal serum", LOOT_FILES["colossal_serum"]),
)


def clipboard_image():
    """The screenshot currently on the clipboard (Win+Shift+S, Print Screen, a copied picture) as
    a BGR array, or None. Windows only, no extra package needed."""
    if sys.platform != "win32":
        return None
    import ctypes.wintypes as wt
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.OpenClipboard.argtypes = [wt.HWND]
    u32.GetClipboardData.argtypes = [wt.UINT]
    u32.GetClipboardData.restype = wt.HANDLE
    k32.GlobalLock.argtypes = [wt.HANDLE]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [wt.HANDLE]
    k32.GlobalSize.argtypes = [wt.HANDLE]
    k32.GlobalSize.restype = ctypes.c_size_t
    CF_DIB = 8
    opened = False
    for _ in range(10):                       # the clipboard may be busy for a moment
        if u32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.05)
    if not opened:
        return None
    data = None
    try:
        handle = u32.GetClipboardData(CF_DIB)
        if handle:
            ptr = k32.GlobalLock(handle)
            if ptr:
                try:
                    data = ctypes.string_at(ptr, k32.GlobalSize(handle))
                finally:
                    k32.GlobalUnlock(handle)
    finally:
        u32.CloseClipboard()
    if not data or len(data) < 40:
        return None
    try:
        import struct
        hdr, = struct.unpack_from("<I", data, 0)
        bpp, = struct.unpack_from("<H", data, 14)
        comp, = struct.unpack_from("<I", data, 16)
        used, = struct.unpack_from("<I", data, 32)
        masks = 12 if (hdr == 40 and comp == 3) else 16 if (hdr == 40 and comp == 6) else 0
        palette = used * 4 if used else ((1 << bpp) * 4 if bpp <= 8 else 0)
        offset = 14 + hdr + masks + palette
        bmp = b"BM" + struct.pack("<IHHI", 14 + len(data), 0, 0, offset) + data
        return cv2.imdecode(np.frombuffer(bmp, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        return None


def save_override_image(img, filename):
    """Write a picture (BGR array) into asset_override under the macro's file name, as a PNG."""
    dest = os.path.join(ensure_override_dir(), filename)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return False
        buf.tofile(dest)
    except Exception:  # noqa: BLE001
        return False
    return True


def import_override_picture(src_path, filename):
    """Copy a picture the user picked into asset_override under the macro's file name (always a
    PNG, so jpg / bmp screenshots work too). Returns True when it worked."""
    img = _read_image(src_path)
    return img is not None and save_override_image(img, filename)


def remove_override_picture(filename):
    path = override_path(filename)
    if not path:
        return True
    try:
        os.remove(path)
        return True
    except OSError:
        return False


# ------------- asset test (Asset override window > Test) -------------
def asset_test_entries():
    """[(file name, {look: picture}, threshold, mask function, size kind)] for every picture in
    ASSET_LIST, loaded the way the macro loads it (your own pictures from asset_override win).
    A picture that can't be loaded gets an empty dict."""
    rules = {TEMPLATE_FILE: (THRESHOLD, None, "retry"),
             ALT_RETRY_FILE: (ALT_RETRY_THRESHOLD, None, "retry"),
             START_TEMPLATE_FILE: (START_THRESHOLD, None, None),
             SHIKI_ABILITY4_FILE: (SHIKI_ABILITY4_THRESHOLD, None, None),
             GRAB_FILE: (GRAB_THRESHOLD, red_mask, None),
             RAID_SKIP_FILE: (RAID_SKIP_THRESHOLD, None, None),
             RAID_START_FILE: (RAID_START_THRESHOLD, None, None),
             RAID_FINISH_FILE: (RAID_FINISH_THRESHOLD, None, None),
             RAID_HEATWAVE_FILE: (RAID_HEATWAVE_THRESHOLD, None, None)}
    for f in RESULT_FILES.values():
        rules[f] = (RESULT_THRESHOLD, gold_mask, None)
    for f in LOOT_FILES.values():
        rules[f] = (LOOT_THRESHOLD, None, "loot")
    entries = []
    for _label, filename in ASSET_LIST:
        thr, mask, kind = rules.get(filename, (0.8, None, None))
        try:
            if mask:                                   # text pictures: matched as a colour mask
                tpls = {"normal": mask(load_template(filename))}
            else:
                tpls = load_quality_templates(filename)
        except FileNotFoundError:
            tpls = {}
        if filename == ALT_RETRY_FILE and tpls:        # the RETRY word alone, like load_alt_retry
            x0, x1 = ALT_RETRY_WORD_COLS
            for quality in list(tpls):
                name = filename if quality == "normal" else os.path.join(LOW_FOLDER, filename)
                if not override_path(name) and 0 <= x0 < x1 <= tpls[quality].shape[1]:
                    tpls[quality + " word"] = tpls[quality][:, x0:x1]
        entries.append((filename, tpls, thr, mask, kind))
    for name, _users in macro_picture_list():          # the pictures of custom macros (Macros tab)
        try:
            tpls = {"normal": load_template(name)}
        except FileNotFoundError:
            tpls = {}
        entries.append((name, tpls, CUSTOM_IMG_THRESHOLD, None, None))
    return entries


def asset_test_scan(screen, entries, scale):
    """Look for every picture on one screenshot (already scaled to 1080p size).
    Returns {file name: True (seen) / False (not seen) / None (no picture)}."""
    relax_by = SCALE_THRESHOLD_RELAX if abs(scale - 1.0) > 0.01 else 0.0
    fix = bool(settings.get("fix_loot"))
    sources, smalls, out = {}, {}, {}
    for filename, tpls, thr, mask, kind in entries:
        if not tpls:
            out[filename] = None
            continue
        thr -= relax_by
        sizes = None
        if kind == "retry" and relax_by:
            sizes = RETRY_SCALES
        elif kind == "loot" and fix:
            sizes = LOOT_FIX_SCALES
            thr -= LOOT_FIX_RELAX
        if mask not in sources:
            sources[mask] = mask(screen) if mask else screen
        src = sources[mask]
        seen = False
        for tpl in tpls.values():
            if sizes:
                if mask not in smalls:
                    smalls[mask] = cv2.resize(src, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
                m, _size = _any_size_match(src, smalls[mask], tpl, sizes)
                score = m[2] if m else -1.0
            else:
                th, tw = tpl.shape[:2]
                if th > src.shape[0] or tw > src.shape[1]:
                    continue
                score = cv2.minMaxLoc(cv2.matchTemplate(src, tpl, cv2.TM_CCOEFF_NORMED))[1]
            if score >= thr:
                seen = True
                break
        out[filename] = seen
    return out


class AssetOverrideWindow(tk.Toplevel):
    """Every picture the macro looks for, with a thumbnail and a Change button. A picture chosen
    here is saved into asset_override with the right name, so the macro uses it instead of the
    built-in one (see load_template)."""
    THUMB_W, THUMB_H = 120, 44

    def __init__(self, settings_win):
        super().__init__(settings_win)
        prepare_window(self)
        self.sw = settings_win
        self.thumbs = []                  # keep the PhotoImages alive
        self.testing = False              # the Test button: look for the pictures on screen, live
        self.test_seen = {}               # file name -> True / False / None, written by the test thread
        self.test_rect = None             # where this window is (it is blacked out of the screenshot)
        self.test_dirty = False           # a picture changed: the test thread loads them again
        self.test_stop = None
        self.test_job = None
        self.indicators = {}              # file name -> the little "Seen / Not seen" label
        self.title("ymir - fix image detection")
        try:
            self.iconbitmap(asset_path(ICON_FILE))
        except (tk.TclError, OSError):
            pass
        self.configure(bg=App.BG)
        self.transient(settings_win)
        self.attributes("-topmost", True)
        self.minsize(480, 320)

        # bottom bar first, so it always stays visible
        bottom = tk.Frame(self, bg=App.BG)
        bottom.pack(side="bottom", fill="x", padx=18, pady=(8, 14))
        self.sw.flat_button(bottom, "Open folder", open_override_folder).pack(side="left")
        self.test_btn = self.sw.flat_button(bottom, "Test", self.toggle_test)
        self.test_btn.pack(side="left", padx=(8, 0))
        self.note = tk.Label(bottom, text="", font=(App.FONT, 9), fg=App.SUB, bg=App.BG,
                             anchor="w", justify="left", wraplength=250)
        self.note.pack(side="left", padx=(12, 0), fill="x", expand=True)

        tk.Label(self, text="Pictures the macro looks for", font=(App.FONT, 11, "bold"),
                 fg=App.TEXT, bg=App.BG, anchor="w").pack(side="top", fill="x", padx=18, pady=(14, 0))
        tk.Label(self, text="Change = pick a file.  Paste = use the screenshot on your clipboard.",
                 font=(App.FONT, 9), fg=App.SUB, bg=App.BG,
                 anchor="w").pack(side="top", fill="x", padx=18, pady=(0, 8))

        # tabs: UI (everything but the loot) | Loot (the loot pictures) | Macros (pictures of custom macros)
        self.tab = "UI"
        tabs = tk.Frame(self, bg=App.BG)
        tabs.pack(side="top", fill="x", padx=18, pady=(0, 8))
        self.tab_btns = {}
        for name in ("UI", "Loot", "Macros"):
            lb = tk.Label(tabs, text=name, font=(App.FONT, 10, "bold"), padx=18, pady=5,
                          cursor="hand2")
            lb.pack(side="left", padx=(0, 6))
            lb.bind("<Button-1>", lambda _e, n=name: self.set_tab(n))
            self.tab_btns[name] = lb
        self._style_tabs()

        wrap = tk.Frame(self, bg=App.BG)
        wrap.pack(side="top", fill="both", expand=True, padx=(18, 8))
        self.canvas = tk.Canvas(wrap, bg=App.BG, highlightthickness=0, bd=0)
        self.scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=App.BG)
        self.inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self.inner_id, width=e.width))
        self.bind("<MouseWheel>", self._wheel)

        self.refresh()
        self.update_idletasks()
        x, y = settings_win.winfo_x() + 30, settings_win.winfo_y() + 30
        self.geometry(f"560x560+{x}+{y}")
        style_title_bar(self, App.BG)
        self.protocol("WM_DELETE_WINDOW", lambda: fade_close(self))
        nav_attach(self, "images", settings_win.app)
        reveal_window(self)
        self.lift()
        self.focus_force()

    def _wheel(self, e):
        self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

    def _style_tabs(self):
        for name, lb in self.tab_btns.items():
            on = name == self.tab
            lb.configure(bg=App.ACCENT_BG if on else App.CARD,
                         fg="#ffffff" if on else App.SUB)

    def set_tab(self, name, force=False):
        if name == self.tab and not force:
            return
        self.tab = name
        self._style_tabs()
        self.refresh()
        self.canvas.yview_moveto(0)

    def _thumb(self, filename):
        """(PhotoImage or None, is_custom). Shows the user's picture if there is one."""
        mine = override_path(filename)
        path = mine
        if not path:
            built_in = asset_path(filename)
            path = built_in if os.path.isfile(built_in) else None
        img = _read_image(path) if path else None
        if img is None:
            return None, bool(mine)
        h, w = img.shape[:2]
        k = min(self.THUMB_W / w, self.THUMB_H / h, 3.0)
        img = cv2.resize(img, (max(1, int(w * k)), max(1, int(h * k))),
                         interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)
        ok, buf = cv2.imencode(".png", img)
        if not ok:
            return None, bool(mine)
        return tk.PhotoImage(data=base64.b64encode(buf.tobytes())), bool(mine)

    def refresh(self):
        top = self.canvas.yview()[0]
        for child in self.inner.winfo_children():
            child.destroy()
        self.thumbs = []
        self.indicators = {}
        self.test_dirty = True            # (a picture may have changed: the test loads them again)
        if self.tab == "Macros":
            self._macro_head()
            pics = macro_picture_list()
            for name, users in pics:
                self._row(name, name, users=users)
            if not pics:
                tk.Label(self.inner, text="No macro pictures yet. Use 'find' / 'imgclick' in a custom "
                                          "macro, or press Add picture.", font=(App.FONT, 9),
                         fg=App.SUB, bg=App.BG, anchor="w", justify="left",
                         wraplength=440).pack(fill="x", pady=(8, 0))
        else:
            for label, filename in ASSET_LIST:
                if label.startswith("Loot:") == (self.tab == "Loot"):
                    self._row(label, filename)
        self.update_idletasks()
        self.canvas.yview_moveto(top)

    def _macro_head(self):
        """Top of the Macros tab: what it is for + the buttons that add a new picture."""
        head = tk.Frame(self.inner, bg=App.BG)
        head.pack(fill="x", pady=(0, 4))
        tk.Label(head, text="Pictures your custom macros look for (find / imgclick). They live in "
                            "asset_override: send them along with the .ymir file, and whoever "
                            "runs the macro sees them here.",
                 font=(App.FONT, 9), fg=App.SUB, bg=App.BG, anchor="w", justify="left",
                 wraplength=440).pack(fill="x", pady=(0, 6))
        btns = tk.Frame(head, bg=App.BG)
        btns.pack(fill="x")
        self.sw.flat_button(btns, "Add picture", self.add_image).pack(side="left")
        self.sw.flat_button(btns, "Paste as new", self.paste_new_image).pack(side="left", padx=(6, 0))

    def _row(self, label, filename, users=None):
        """users = None for the macro's own pictures; a list (maybe empty) of macro names for a row
        of the Macros tab."""
        photo, custom = self._thumb(filename)
        in_macro_tab = users is not None
        has_builtin = os.path.isfile(asset_path(filename))
        row = tk.Frame(self.inner, bg=App.CARD, highlightbackground=App.BORDER,
                       highlightthickness=App.CARD_BW)
        row.pack(fill="x", pady=3)
        box = tk.Frame(row, bg=App.LOGBG, width=self.THUMB_W + 8, height=self.THUMB_H + 8)
        box.pack(side="left", padx=8, pady=8)
        box.pack_propagate(False)
        if photo is not None:
            self.thumbs.append(photo)
            tk.Label(box, image=photo, bg=App.LOGBG, bd=0).pack(expand=True)
        else:
            tk.Label(box, text="no picture", font=(App.FONT, 8), fg=App.SUB,
                     bg=App.LOGBG).pack(expand=True)
        info = tk.Frame(row, bg=App.CARD)
        info.pack(side="left", fill="x", expand=True, padx=(0, 6))
        title = tk.Frame(info, bg=App.CARD)
        title.pack(fill="x")
        tk.Label(title, text=label, font=(App.FONT, 10), fg=App.TEXT, bg=App.CARD,
                 anchor="w").pack(side="left")
        if self.testing:                  # the live "Seen / Not seen" light, only while Test is on
            ind = tk.Label(title, text="", font=(App.FONT, 8, "bold"), bg=App.CARD, anchor="w")
            ind.pack(side="left", padx=(10, 0))
            self.indicators[filename] = ind
            self._paint(filename, ind)
        if in_macro_tab:
            state_txt = ("Your picture" if custom else "Built-in" if has_builtin
                         else "Missing - add your picture")
            if users:
                state_txt += "  -  used by: " + ", ".join(users)
            bad = not custom and not has_builtin
            tk.Label(info, text=state_txt, font=(App.FONT, 8), wraplength=230, justify="left",
                     fg="#f87171" if bad else App.ACCENT if custom else App.SUB,
                     bg=App.CARD, anchor="w").pack(fill="x")
        else:
            tk.Label(info, text="Your picture" if custom else "Built-in", font=(App.FONT, 8),
                     fg=App.ACCENT if custom else App.SUB, bg=App.CARD, anchor="w").pack(fill="x")
        btns = tk.Frame(row, bg=App.CARD)
        btns.pack(side="right", padx=10)
        top_btns = tk.Frame(btns, bg=App.CARD)
        top_btns.pack()
        self.sw.flat_button(top_btns, "Change",
                            lambda f=filename, l=label: self.change(f, l)).pack(side="left")
        self.sw.flat_button(top_btns, "Paste",
                            lambda f=filename, l=label: self.paste(f, l)).pack(side="left",
                                                                               padx=(4, 0))
        if custom:
            reset = tk.Label(btns, text="Reset" if (has_builtin or not in_macro_tab) else "Remove",
                             font=(App.FONT, 8), fg=App.SUB, bg=App.CARD, cursor="hand2")
            reset.pack(pady=(4, 0))
            reset.bind("<Button-1>", lambda _e, f=filename, l=label: self.reset(f, l))
            reset.bind("<Enter>", lambda _e, w=reset: w.configure(fg=App.TEXT))
            reset.bind("<Leave>", lambda _e, w=reset: w.configure(fg=App.SUB))
        for w in (row, info, box):
            w.bind("<MouseWheel>", self._wheel)

    # ----- Test: is the picture on the screen right now? -----
    def _paint(self, filename, lbl):
        v = self.test_seen.get(filename, "wait")
        if v == "wait":
            lbl.configure(text="\u2026 checking", fg=App.SUB)
        elif v is None:
            lbl.configure(text="no picture", fg=App.SUB)
        elif v:
            lbl.configure(text="\u25cf Seen", fg="#34d399")
        else:
            lbl.configure(text="\u25cb Not seen", fg=App.SUB)

    def toggle_test(self):
        self.stop_test() if self.testing else self.start_test()

    def start_test(self):
        global _scale
        _scale = _screen_scale()          # your own pictures are scaled with it when they are loaded
        self.testing = True
        self.test_seen = {}
        self.test_dirty = True
        self.test_rect = (self.winfo_rootx(), self.winfo_rooty(), self.winfo_width(), self.winfo_height())
        self.test_stop = threading.Event()
        threading.Thread(target=self._test_worker, args=(self.test_stop,), daemon=True).start()
        self.test_btn.configure(text="Stop test", fg="#34d399")
        self.note.configure(text="Test is on: open the game and the lights show what the macro "
                                 "sees. Seen pictures are also printed in the log.", fg=App.SUB)
        log("Asset test: ON - looking for the pictures on your screen")
        self.refresh()                    # adds the indicators
        self.test_job = self.after(250, self._poll_test)

    def stop_test(self):
        if self.test_stop:
            self.test_stop.set()
            self.test_stop = None
        if self.test_job:
            try:
                self.after_cancel(self.test_job)
            except tk.TclError:
                pass
            self.test_job = None
        if not self.testing:
            return
        self.testing = False
        try:
            self.test_btn.configure(text="Test", fg=App.ACCENT_TEXT)
            self.note.configure(text="Test stopped.", fg=App.SUB)
            self.refresh()                # removes the indicators
        except tk.TclError:
            pass                          # the window is closing
        log("Asset test: OFF")

    def _poll_test(self):
        self.test_job = None
        if not self.testing:
            return
        try:
            self.test_rect = (self.winfo_rootx(), self.winfo_rooty(),
                              self.winfo_width(), self.winfo_height())
            for filename, lbl in list(self.indicators.items()):
                self._paint(filename, lbl)
        except tk.TclError:
            return
        self.test_job = self.after(250, self._poll_test)

    def _test_worker(self, stop):
        """Own thread + own screen grabber, so it never gets in the way of the macro. Looks at the
        screen about 3 times a second and logs \"<file> is seen\" when a picture shows up."""
        try:
            sct = mss.mss()
        except Exception as e:  # noqa: BLE001
            log(f"Asset test: could not read the screen ({e})")
            return
        try:
            mon = pick_monitor(sct.monitors)
            scale = FORCE_SCALE if FORCE_SCALE else mon["height"] / BASE_HEIGHT
            entries, was_seen = None, set()
            while not stop.is_set():
                if entries is None or self.test_dirty:
                    self.test_dirty = False
                    entries = asset_test_entries()
                img = cv2.cvtColor(np.array(sct.grab(mon)), cv2.COLOR_BGRA2BGR)
                if IS_MAC and img.shape[1] != mon["width"]:
                    img = cv2.resize(img, (mon["width"], mon["height"]), interpolation=cv2.INTER_AREA)
                rect = self.test_rect
                if rect:                  # black out this window: its thumbnails would match themselves
                    x0, y0 = max(0, rect[0] - mon["left"]), max(0, rect[1] - mon["top"])
                    x1, y1 = x0 + rect[2] + 8, y0 + rect[3] + 8
                    img[y0:y1, x0:x1] = 0
                if abs(scale - 1.0) > 0.01:
                    interp = cv2.INTER_AREA if scale > 1 else cv2.INTER_CUBIC
                    img = cv2.resize(img, None, fx=1 / scale, fy=1 / scale, interpolation=interp)
                result = asset_test_scan(img, entries, scale)
                for filename, v in result.items():
                    if v and filename not in was_seen:
                        log(f"{filename} is seen")
                was_seen = {f for f, v in result.items() if v}
                self.test_seen = result
                stop.wait(0.3)
        except Exception as e:  # noqa: BLE001
            log(f"Asset test stopped: {e}")
        finally:
            try:
                sct.close()
            except Exception:  # noqa: BLE001
                pass

    def destroy(self):
        if self.test_stop:
            self.test_stop.set()
            self.test_stop = None
        if self.test_job:
            try:
                self.after_cancel(self.test_job)
            except tk.TclError:
                pass
            self.test_job = None
        super().destroy()

    def change(self, filename, label):
        # the dialog is owned by this (always-on-top) window, so it opens above every macro window
        path = filedialog.askopenfilename(
            parent=self, title=f"Pick your picture for: {label}",
            filetypes=[("Pictures", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        self.lift()
        if not path:
            return
        if import_override_picture(path, filename):
            self.note.configure(text=f"{label}: your picture is saved.", fg="#34d399")
            self.refresh()
        else:
            self.note.configure(text="Could not read that picture.", fg="#f87171")

    def paste(self, filename, label):
        """Use the screenshot on the clipboard (Win+Shift+S, then Paste)."""
        img = clipboard_image()
        if img is None:
            self.note.configure(text="No screenshot on the clipboard. Take one first "
                                     "(Win+Shift+S), then press Paste.", fg="#f87171")
        elif save_override_image(img, filename):
            self.note.configure(text=f"{label}: pasted from the clipboard.", fg="#34d399")
            self.refresh()
        else:
            self.note.configure(text="Could not save the picture.", fg="#f87171")

    def _ask_image_name(self, default):
        """Ask what a new macro picture is called (the name a script uses: find NAME). None = cancelled."""
        raw = simpledialog.askstring("ymir", "Name of this picture (your macro uses it like: "
                                             "find NAME).", initialvalue=default, parent=self)
        self.lift()
        if not raw:
            return None
        name = safe_image_name(raw)
        if not name:
            self.note.configure(text="That is not a usable name.", fg="#f87171")
            return None
        if name in {f for _l, f in ASSET_LIST}:
            self.note.configure(text=f"{name} is one of the macro's own pictures - pick another name.",
                                fg="#f87171")
            return None
        if override_path(name) and not messagebox.askyesno(
                "ymir", f"{name} already exists. Replace it?", parent=self):
            self.lift()
            return None
        self.lift()
        return name

    def add_image(self):
        """Macros tab > Add picture: pick a file, name it, it is saved into asset_override."""
        path = filedialog.askopenfilename(
            parent=self, title="Pick a picture for your macro",
            filetypes=[("Pictures", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        self.lift()
        if not path:
            return
        name = self._ask_image_name(safe_image_name(os.path.basename(path)) or "picture.png")
        if not name:
            return
        if import_override_picture(path, name):
            self.note.configure(text=f"{name} added - use it as: find {name}", fg="#34d399")
            self.refresh()
        else:
            self.note.configure(text="Could not read that picture.", fg="#f87171")

    def paste_new_image(self):
        """Macros tab > Paste as new: the screenshot on the clipboard becomes a new macro picture."""
        img = clipboard_image()
        if img is None:
            self.note.configure(text="No screenshot on the clipboard. Take one first "
                                     "(Win+Shift+S), then press Paste as new.", fg="#f87171")
            return
        name = self._ask_image_name("picture.png")
        if not name:
            return
        if save_override_image(img, name):
            self.note.configure(text=f"{name} added - use it as: find {name}", fg="#34d399")
            self.refresh()
        else:
            self.note.configure(text="Could not save the picture.", fg="#f87171")

    def reset(self, filename, label):
        if remove_override_picture(filename):
            if os.path.isfile(asset_path(filename)) or not self.tab == "Macros":
                self.note.configure(text=f"{label}: back to the built-in picture.", fg="#34d399")
            else:
                self.note.configure(text=f"{label}: picture removed.", fg="#34d399")
            self.refresh()
        else:
            self.note.configure(text="Could not remove your picture.", fg="#f87171")


class ChangelogWindow(tk.Toplevel):
    """Settings footer > Changelog: change.md in a scrollable, read-only window (# / ## / ###
    headings and - bullets are styled, # and ## get a ridge line under them, **bold** and `code`
    marks are dropped)."""

    def __init__(self, app):
        super().__init__(app.root)
        prepare_window(self)
        self.title("ymir - changelog")
        try:
            self.iconbitmap(asset_path(ICON_FILE))
        except (tk.TclError, OSError):
            pass
        self.configure(bg=App.BG)
        self.transient(app.root)
        self.attributes("-topmost", True)
        self.minsize(380, 300)
        self.protocol("WM_DELETE_WINDOW", lambda: fade_close(self))

        tk.Label(self, text="Changelog", font=(App.FONT, 13, "bold"), fg=App.TEXT, bg=App.BG,
                 anchor="w").pack(side="top", fill="x", padx=18, pady=(14, 8))
        wrap = tk.Frame(self, bg=App.BG)
        wrap.pack(side="top", fill="both", expand=True, padx=(18, 8), pady=(0, 14))
        self.text = tk.Text(wrap, wrap="word", bg=App.LOGBG, fg=App.TEXT, relief="flat",
                            font=(App.FONT, 10), padx=14, pady=10, bd=0, highlightthickness=1,
                            highlightbackground=App.BORDER, cursor="arrow", spacing3=2)
        sb = ttk.Scrollbar(wrap, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("h1", font=(App.FONT, 15, "bold"), foreground=App.TEXT,
                                spacing1=4, spacing3=6)
        self.text.tag_configure("h2", font=(App.FONT, 12, "bold"), foreground=App.ACCENT_TEXT,
                                spacing1=12, spacing3=4)
        self.text.tag_configure("h3", font=(App.FONT, 10, "bold"), foreground=App.TEXT,
                                spacing1=8, spacing3=2)
        self.text.tag_configure("bullet", lmargin1=14, lmargin2=30)
        self.text.tag_configure("rule", font=(App.FONT, 2), spacing1=0, spacing3=6)
        self.rules = []          # the ridges under # and ## headings (resized with the window)
        self.text.bind("<Configure>", self._fit_rules)
        self.render(changelog_text())
        self.text.configure(state="disabled")

        x, y = app.root.winfo_x() + 30, app.root.winfo_y() + 30
        self.geometry(f"520x560+{x}+{y}")
        style_title_bar(self, App.BG)
        nav_attach(self, "changelog", app)
        reveal_window(self)
        self.lift()
        self.focus_force()

    def _fit_rules(self, _e=None):
        width = max(40, self.text.winfo_width() - 2 * 14 - 8)
        for rule in self.rules:
            rule.configure(width=width)

    def _add_rule(self, color):
        """A thin line under a heading (like on GitHub)."""
        rule = tk.Frame(self.text, bg=color, height=1, width=400)
        start = self.text.index("end-1c")
        self.text.window_create("end-1c", window=rule)
        self.text.insert("end-1c", "\n")
        self.text.tag_add("rule", start, "end-1c")
        self.rules.append(rule)

    def render(self, md):
        for raw in md.replace("\r\n", "\n").split("\n"):
            line = raw.rstrip()
            tag = ""
            if line.startswith("### "):
                tag, line = "h3", line[4:]
            elif line.startswith("## "):
                tag, line = "h2", line[3:]
            elif line.startswith("# "):
                tag, line = "h1", line[2:]
            elif line.lstrip().startswith(("- ", "* ")):
                tag, line = "bullet", "\u2022  " + line.lstrip()[2:]
            line = line.replace("**", "").replace("`", "")
            self.text.insert("end", line + "\n", tag)
            if tag == "h1":
                self._add_rule(App.FIELD_EDGE)
            elif tag == "h2":
                self._add_rule(App.BORDER)


def apply_theme(name):
    """Make `name` the live theme: sets the colours / radii / font every widget reads.
    (Widgets built afterwards use it; App.rebuild_ui() rebuilds the visible ones.)"""
    if name not in THEMES:
        name = DEFAULT_THEME
    P = build_palette(name)
    settings["theme"] = name
    for key, value in P.items():
        setattr(App, key, value)
    ActionTile.LOOK = {   # (fill, outline, outline_width, label_fg, badge_outline, badge_fg)
        "normal":   (P["FIELD"], P["FIELD_EDGE"], 1, P["TEXT"], P["FIELD_EDGE_HOVER"], P["BADGE_FG"]),
        "hover":    (P["FIELD_HOVER"], P["FIELD_EDGE_HOVER"], 1, "#ffffff", P["BADGE_HOVER"],
                     P["BADGE_FG_HOVER"]),
        "active":   (P["ACCENT_BG"], P["ACCENT_LINE"], 2, "#ffffff", P["ACCENT_BADGE"],
                     P["ACCENT_TEXT"]),
        "disabled": (P["DIS_FILL"], P["DIS_EDGE"], 1, P["DIS_TEXT"], P["DIS_BADGE"], P["DIS_TEXT"]),
    }
    ActionTile.OFF_ICON = P["DIS_ICON"]
    ToggleSwitch.ON, ToggleSwitch.OFF, ToggleSwitch.KNOB = P["ACCENT"], P["SWITCH_OFF"], P["KNOB"]
    KeyButton.NORMAL, KeyButton.HOVER, KeyButton.CAPTURE = (
        P["FIELD"], P["FIELD_HOVER"], P["ACCENT_BG"])
    ModernDropdown.BG, ModernDropdown.HOVER, ModernDropdown.EDGE = (
        P["FIELD"], P["FIELD_HOVER"], P["FIELD_EDGE"])
    ModernDropdown.TEXT, ModernDropdown.SUB, ModernDropdown.ACCENT = (
        P["TEXT"], P["SUB"], P["ACCENT"])
    ModernDropdown.WIP_BG, ModernDropdown.WIP_FG = P["PILLS"]["waiting"][1:]
    return name


def main():
    load_stats()          # stats + settings (saved keybinds, webhook, family)
    load_timings()        # config.json: the timings changed in Edit configs
    ensure_override_dir() # Documents\\Ymir Macro\\asset_override: users drop their own pictures here
    register_ymir_file_type()   # Windows: the lambda icon on .ymir files (no admin needed)
    apply_settings()
    register_hotkeys()
    bot_restart()         # Discord bot mode: start listening for !commands
    root = tk.Tk()
    App(root)
    push_stats(save=False)  # show the saved numbers right away
    root.mainloop()


if __name__ == "__main__":
    main()