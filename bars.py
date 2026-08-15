#!/usr/bin/env python3
"""NTSC composite test pattern generator for Raspberry Pi.

Run as `bars` at the console. Screen updates are written directly into
/dev/fb0's memory rather than through SDL/DRM: the vc4-fkms-v3d driver
rejects the async page flips SDL issues on every flip() call, and
whatever fallback that triggers is disruptive enough to blank the
picture for about a second on every single update, on both composite
and HDMI. Direct framebuffer writes avoid that entirely -- but taking
a real KMS display (as SDL's kmsdrm driver does, to read the keyboard)
would mean *our own* buffer is what's scanned out, hiding the fb0
writes completely. So pygame here runs fully headless (SDL_VIDEODRIVER
=dummy, no display.set_mode on real hardware) purely to build surfaces
and render fonts, and the keyboard is read directly via evdev instead.
"""
import array
import configparser
import fcntl
import math
import mmap
import os
import selectors
import signal
import socket
import subprocess
import sys
from pathlib import Path

import evdev
import numpy as np
from evdev import ecodes

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "alsa")

import pygame  # noqa: E402  (must come after SDL env vars are set)

VERSION = "1.1"

BASE_DIR = Path(__file__).resolve().parent
PATTERN_DIR = BASE_DIR / "patterns"
FONT_PATH = BASE_DIR / "VCR_OSD_MONO_1.001.ttf"
SETTINGS_PATH = BASE_DIR / "settings.ini"

FRAME_W, FRAME_H = 720, 480
DEFAULT_PATTERN = "BARS_0013_SMPTE-Bars.png"
DEFAULT_CUSTOM_TEXT = "CUSTOM TEXT"

ORANGE = (0xFF, 0xA5, 0x00)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

OVERLAY_CYCLE = [None, "ip", "hostname", "custom"]
POWER_OPTIONS = ["NO", "YES", "RESTART"]
MOUSE_MOVE_THRESHOLD = 12  # cumulative REL_X/REL_Y units before it counts as one direction press

# See menu.py -- Home exits with this so menu.py's launch_app() knows to
# hand off to Health Monitor instead of redrawing its own app list.
EXIT_GOTO_HOME = 42

TONE_HZ = 1000
TONE_SAMPLE_RATE = 48000
TONE_AMPLITUDE = 32767  # full-scale 16-bit signed == 0 dBFS

KDSETMODE = 0x4B3A
KD_TEXT = 0x00
KD_GRAPHICS = 0x01

MENU_LINES = [
    ("NTSC TEST PATTERN GENERATOR", 32),
    ("", 0),
    ("LEFT/RIGHT - CHANGE PATTERN", 32),
    ("UP/DOWN - CYCLE OVERLAY", 32),
    ("I - IP ADDRESS", 32),
    ("H - HOSTNAME", 32),
    ("C - CUSTOM TEXT", 32),
    ("T - TONE", 32),
    ("Q, <ESC>, or REMOTE BACK - APP MENU", 32),
    ("REMOTE HOME - HEALTH MONITOR", 32),
]


def load_pattern_paths():
    paths = sorted(PATTERN_DIR.glob("BARS_*.png"))
    if not paths:
        sys.exit(f"No patterns found in {PATTERN_DIR}")
    return paths


def get_ip_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "NO NETWORK"
    finally:
        s.close()


def get_hostname():
    return socket.gethostname()


def get_custom_text():
    # Re-read on every call (rather than caching at startup) so edits to
    # settings.ini show up next time 'C' is pressed, no restart needed.
    parser = configparser.ConfigParser()
    parser.read(SETTINGS_PATH)
    return parser.get("bars", "custom_text", fallback=DEFAULT_CUSTOM_TEXT)


def find_keyboard_devices():
    # A "keyboard" here means any device with an EV_KEY capability, not just
    # full keyboards (KEY_A). Remote controls and other HID composite
    # devices split themselves across several /dev/input nodes -- e.g. the
    # 2.4G remote exposes separate Consumer Control (volume, home) and
    # System Control (power) interfaces alongside its base keyboard node --
    # and all of them need to be read for every button to work. Device node
    # numbers aren't stable across reboots, so this filters by capability
    # rather than hardcoding paths.
    devices = []
    for path in evdev.list_devices():
        dev = evdev.InputDevice(path)
        if dev.capabilities().get(ecodes.EV_KEY):
            devices.append(dev)
    if not devices:
        sys.exit("No keyboard input device found (looked for devices with an EV_KEY capability)")
    return devices


class FrameBuffer:
    """Direct writer for /dev/fb0, bypassing DRM page-flips entirely.

    Geometry is read from sysfs at open time since it depends on
    whichever output (composite/HDMI) is currently active.
    """

    def __init__(self, dev="/dev/fb0"):
        sys_dir = Path("/sys/class/graphics") / Path(dev).name
        self.width, self.height = (int(x) for x in (sys_dir / "virtual_size").read_text().split(","))
        self.bpp = int((sys_dir / "bits_per_pixel").read_text())
        self.stride = int((sys_dir / "stride").read_text())
        self.bypp = self.bpp // 8
        self.row_bytes = self.width * self.bypp
        size = self.stride * self.height
        self.fd = os.open(dev, os.O_RDWR)
        self.mm = mmap.mmap(self.fd, size, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ)
        if self.bpp not in (16, 32):
            raise RuntimeError(f"Unsupported framebuffer depth: {self.bpp}bpp")

    def write_surface(self, surface):
        if surface.get_size() != (self.width, self.height):
            surface = pygame.transform.scale(surface, (self.width, self.height))
        arr = pygame.surfarray.pixels3d(surface).transpose(1, 0, 2)  # (H, W, RGB) uint8
        if self.bpp == 16:
            r = arr[:, :, 0].astype(np.uint16) >> 3
            g = arr[:, :, 1].astype(np.uint16) >> 2
            b = arr[:, :, 2].astype(np.uint16) >> 3
            raw = ((r << 11) | (g << 5) | b).astype("<u2").tobytes()
        else:
            alpha = np.zeros((self.height, self.width, 1), dtype=np.uint8)
            raw = np.concatenate([arr[:, :, ::-1], alpha], axis=2).astype(np.uint8).tobytes()

        if self.stride == self.row_bytes:
            self.mm.seek(0)
            self.mm.write(raw)
        else:
            for y in range(self.height):
                self.mm.seek(y * self.stride)
                self.mm.write(raw[y * self.row_bytes : (y + 1) * self.row_bytes])

    def close(self):
        self.mm.close()
        os.close(self.fd)


def make_tone_sound():
    n_samples = TONE_SAMPLE_RATE  # exactly 1 second -> loops with no click at 1kHz
    buf = array.array("h")
    for i in range(n_samples):
        sample = int(TONE_AMPLITUDE * math.sin(2 * math.pi * TONE_HZ * i / TONE_SAMPLE_RATE))
        buf.append(sample)
        buf.append(sample)  # stereo
    return pygame.mixer.Sound(buffer=buf.tobytes())


class BarsApp:
    def __init__(self):
        # pygame/SDL installs its own SIGINT/SIGTERM handler that turns the
        # signal into an SDL_QUIT event; since input is read via evdev and
        # nothing drains pygame's event queue, that would silently swallow
        # both signals. Install plain handlers so the process still
        # terminates normally.
        self._quit_requested = False
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        pygame.init()
        pygame.display.set_mode((FRAME_W, FRAME_H))  # headless (dummy driver); needed for .convert()

        try:
            pygame.mixer.init(frequency=TONE_SAMPLE_RATE, size=-16, channels=2)
            self.audio_ok = True
        except pygame.error as exc:
            print(f"Audio init failed, tone disabled: {exc}", file=sys.stderr)
            self.audio_ok = False

        self.fb = FrameBuffer()

        self.kbd_devices = find_keyboard_devices()
        self.selector = selectors.DefaultSelector()
        for dev in self.kbd_devices:
            self.selector.register(dev, selectors.EVENT_READ)

        self.pattern_paths = load_pattern_paths()
        names = [p.name for p in self.pattern_paths]
        self.index = names.index(DEFAULT_PATTERN) if DEFAULT_PATTERN in names else 0
        self.image_cache = {}

        self.menu_fonts = {size: pygame.font.Font(str(FONT_PATH), size) for _, size in MENU_LINES if size}
        self.osd_font = pygame.font.Font(str(FONT_PATH), 36)

        self.overlay = None  # None, "ip", "hostname", or "custom"
        self.tone_playing = False
        self.tone_sound = None
        self.power_dialog_active = False
        self.power_dialog_selection = 0  # index into POWER_OPTIONS, defaults to NO
        self.pending_power_action = None  # None, "shutdown", or "restart"
        self.pending_exit_code = 0  # 0 or EXIT_GOTO_HOME
        self._rel_accum = {"x": 0, "y": 0}

        # Tell the console driver to stop drawing its own cursor/text over
        # our framebuffer writes. Only possible on a real VT (not over SSH,
        # and not without any controlling terminal at all), set last since
        # it must be paired with the KD_TEXT restore in run()'s finally
        # block. Restored there.
        self.tty_fd = None
        self.console_graphics_mode = False
        try:
            self.tty_fd = os.open("/dev/tty", os.O_RDWR)
            fcntl.ioctl(self.tty_fd, KDSETMODE, KD_GRAPHICS)
            self.console_graphics_mode = True
        except OSError as exc:
            print(f"Console graphics mode not available: {exc}", file=sys.stderr)

    def _handle_signal(self, signum, frame):
        self._quit_requested = True

    def current_image(self):
        path = self.pattern_paths[self.index]
        img = self.image_cache.get(path)
        if img is None:
            img = pygame.image.load(str(path)).convert()
            self.image_cache[path] = img
        return img

    def build_pattern_canvas(self):
        canvas = pygame.Surface((FRAME_W, FRAME_H))
        canvas.blit(self.current_image(), (0, 0))
        return canvas

    def draw_osd_box(self, canvas, text):
        text_surf = self.osd_font.render(text, True, WHITE)
        pad_x, pad_y = 20, 12
        box = pygame.Surface((text_surf.get_width() + pad_x * 2, text_surf.get_height() + pad_y * 2))
        box.fill(BLACK)
        box.blit(text_surf, (pad_x, pad_y))
        canvas.blit(box, ((FRAME_W - box.get_width()) // 2, (FRAME_H - box.get_height()) // 2))

    def build_menu_canvas(self):
        canvas = pygame.Surface((FRAME_W, FRAME_H))
        canvas.fill(BLACK)
        rendered = []
        for text, size in MENU_LINES:
            if size == 0:
                rendered.append(None)
            else:
                rendered.append(self.menu_fonts[size].render(text, True, ORANGE))
        gap = 10
        total_h = sum((s.get_height() if s else 22) for s in rendered) + gap * (len(rendered) - 1)
        y = (FRAME_H - total_h) // 2
        for s in rendered:
            if s:
                canvas.blit(s, ((FRAME_W - s.get_width()) // 2, y))
                y += s.get_height() + gap
            else:
                y += 22 + gap
        return canvas

    def is_menu(self):
        return self.index == len(self.pattern_paths)

    def render(self):
        if self.is_menu():
            canvas = self.build_menu_canvas()
        else:
            canvas = self.build_pattern_canvas()
            if self.overlay == "ip":
                self.draw_osd_box(canvas, get_ip_address())
            elif self.overlay == "hostname":
                self.draw_osd_box(canvas, get_hostname())
            elif self.overlay == "custom":
                self.draw_osd_box(canvas, get_custom_text())

        if self.power_dialog_active:
            self.draw_power_dialog(canvas)

        self.fb.write_surface(canvas)

    def draw_power_dialog(self, canvas):
        lines = ["ARE YOU SURE YOU WANT", "TO SHUT DOWN?"]
        line_surfs = [self.osd_font.render(line, True, WHITE) for line in lines]
        option_font = self.menu_fonts[32]
        option_surfs = [
            option_font.render(opt, True, BLACK if i == self.power_dialog_selection else ORANGE)
            for i, opt in enumerate(POWER_OPTIONS)
        ]

        pad_x, pad_y, gap = 40, 24, 40
        options_w = sum(s.get_width() for s in option_surfs) + gap * (len(option_surfs) - 1)
        content_w = max(max(s.get_width() for s in line_surfs), options_w)
        content_h = (sum(s.get_height() for s in line_surfs) + 10 * (len(line_surfs) - 1)
                     + 30 + option_surfs[0].get_height())

        box = pygame.Surface((content_w + pad_x * 2, content_h + pad_y * 2))
        box.fill(BLACK)
        pygame.draw.rect(box, ORANGE, box.get_rect(), 3)

        y = pad_y
        for surf in line_surfs:
            box.blit(surf, ((box.get_width() - surf.get_width()) // 2, y))
            y += surf.get_height() + 10
        y += 20

        x = (box.get_width() - options_w) // 2
        for i, surf in enumerate(option_surfs):
            if i == self.power_dialog_selection:
                highlight = pygame.Rect(x - 10, y - 6, surf.get_width() + 20, surf.get_height() + 12)
                pygame.draw.rect(box, ORANGE, highlight)
            box.blit(surf, (x, y))
            x += surf.get_width() + gap

        canvas.blit(box, ((FRAME_W - box.get_width()) // 2, (FRAME_H - box.get_height()) // 2))

    def set_tone(self, on):
        if not self.audio_ok or on == self.tone_playing:
            return
        if on:
            if self.tone_sound is None:
                self.tone_sound = make_tone_sound()
            self.tone_sound.play(loops=-1)
            self.tone_playing = True
        else:
            self.tone_sound.stop()
            self.tone_playing = False

    def toggle_tone(self):
        self.set_tone(not self.tone_playing)

    def next_pattern(self, step):
        # +1 slot for the menu screen, which now sits in the rotation
        # alongside the image patterns instead of behind its own key.
        self.index = (self.index + step) % (len(self.pattern_paths) + 1)

    def cycle_overlay(self, step):
        self.overlay = OVERLAY_CYCLE[(OVERLAY_CYCLE.index(self.overlay) + step) % len(OVERLAY_CYCLE)]

    def handle_keycode(self, code):
        """Returns True if the app should redraw after this key."""
        # KEY_HOME/KEY_HOMEPAGE/KEY_BACK/KEY_COMPOSE/KEY_POWER/KEY_VOLUME{UP,DOWN}/KEY_F2/KEY_F3
        # below are the remote control's Home/Back/Menu/Power/Volume buttons (or their keyboard
        # equivalents) -- see the evdev capture in the README for how those were identified.
        # Home is distinct from Back/Menu/Q/Esc: it exits with EXIT_GOTO_HOME
        # to jump straight to Health Monitor, skipping the App Menu --
        # Back/Menu/Q/Esc exit normally, which just returns to the App Menu
        # (their immediate parent). See menu.py's launch_app() for the
        # sentinel check. KEY_COMPOSE (the remote's hamburger/Menu button)
        # used to jump to this app's own internal help screen -- repurposed
        # for the new system-wide "open App Menu" meaning; the help screen
        # is still reachable via Left/Right (it's in the pattern rotation).
        if self.power_dialog_active:
            return self.handle_power_dialog_keycode(code)
        if code in (ecodes.KEY_HOMEPAGE, ecodes.KEY_HOME):
            return "quit_home"
        elif code in (ecodes.KEY_Q, ecodes.KEY_ESC, ecodes.KEY_BACK, ecodes.KEY_COMPOSE):
            return "quit"
        elif code == ecodes.KEY_LEFT:
            self.next_pattern(-1)
        elif code == ecodes.KEY_RIGHT:
            self.next_pattern(1)
        elif code == ecodes.KEY_UP:
            self.cycle_overlay(1)
        elif code == ecodes.KEY_DOWN:
            self.cycle_overlay(-1)
        elif code == ecodes.KEY_I:
            self.overlay = None if self.overlay == "ip" else "ip"
        elif code == ecodes.KEY_H:
            self.overlay = None if self.overlay == "hostname" else "hostname"
        elif code == ecodes.KEY_C:
            self.overlay = None if self.overlay == "custom" else "custom"
        elif code == ecodes.KEY_T:
            self.toggle_tone()
            return False
        elif code in (ecodes.KEY_VOLUMEDOWN, ecodes.KEY_F3):
            self.set_tone(False)
            return False
        elif code in (ecodes.KEY_VOLUMEUP, ecodes.KEY_F2):
            self.set_tone(True)
            return False
        elif code == ecodes.KEY_POWER:
            self.power_dialog_active = True
            self.power_dialog_selection = 0
        else:
            return False
        return True

    def handle_power_dialog_keycode(self, code):
        """Returns True if the app should redraw after this key."""
        if code in (ecodes.KEY_LEFT, ecodes.KEY_UP):
            self.power_dialog_selection = (self.power_dialog_selection - 1) % len(POWER_OPTIONS)
        elif code in (ecodes.KEY_RIGHT, ecodes.KEY_DOWN):
            self.power_dialog_selection = (self.power_dialog_selection + 1) % len(POWER_OPTIONS)
        elif code in (ecodes.KEY_ENTER, ecodes.KEY_KPENTER, ecodes.BTN_LEFT, ecodes.BTN_MOUSE):
            choice = POWER_OPTIONS[self.power_dialog_selection]
            if choice == "NO":
                self.power_dialog_active = False
            else:
                return "shutdown" if choice == "YES" else "restart"
        elif code in (ecodes.KEY_ESC, ecodes.KEY_BACK, ecodes.KEY_POWER):
            self.power_dialog_active = False
        else:
            return False
        return True

    def handle_rel_event(self, code, value):
        """Translates the remote's air-mouse-mode movement into the same
        discrete direction presses its keyboard-mode D-pad sends, so
        navigation works no matter which mode the remote happens to be in."""
        if code == ecodes.REL_X:
            axis = "x"
        elif code == ecodes.REL_Y:
            axis = "y"
        else:
            return False
        self._rel_accum[axis] += value
        accum = self._rel_accum[axis]
        if abs(accum) < MOUSE_MOVE_THRESHOLD:
            return False
        self._rel_accum[axis] = 0
        if axis == "x":
            synthetic = ecodes.KEY_RIGHT if accum > 0 else ecodes.KEY_LEFT
        else:
            synthetic = ecodes.KEY_DOWN if accum > 0 else ecodes.KEY_UP
        return self.handle_keycode(synthetic)

    def run(self):
        try:
            self.render()
            running = True
            while running and not self._quit_requested:
                for key, _ in self.selector.select(timeout=1):
                    device = key.fileobj
                    for event in device.read():
                        if event.type == ecodes.EV_KEY and event.value == 1:  # 1 == key down (skip up/repeat)
                            result = self.handle_keycode(event.code)
                        elif event.type == ecodes.EV_REL:
                            result = self.handle_rel_event(event.code, event.value)
                        else:
                            continue
                        if result == "quit":
                            running = False
                        elif result == "quit_home":
                            running = False
                            self.pending_exit_code = EXIT_GOTO_HOME
                        elif result in ("shutdown", "restart"):
                            self.pending_power_action = result
                            running = False
                        elif result:
                            self.render()
                    if not running:
                        break
        finally:
            if self.tone_playing:
                self.tone_sound.stop()
            self.fb.close()
            if self.console_graphics_mode:
                fcntl.ioctl(self.tty_fd, KDSETMODE, KD_TEXT)
                os.write(self.tty_fd, b"\033[2J\033[H")  # clear screen, home cursor
            if self.tty_fd is not None:
                os.close(self.tty_fd)
            pygame.quit()

        if self.pending_power_action == "shutdown":
            subprocess.run(["sudo", "shutdown", "-h", "now"])
        elif self.pending_power_action == "restart":
            subprocess.run(["sudo", "shutdown", "-r", "now"])

        sys.exit(self.pending_exit_code)


def main():
    BarsApp().run()


if __name__ == "__main__":
    main()
