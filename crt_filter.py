"""CRT Filter - live green-phosphor filter over any area of the screen (Windows 10 2004+).

The filter window is excluded from screen capture, so it can sit directly on top of
the area it captures: it always shows a filtered version of whatever is underneath it.
"""
import ctypes
import sys
from ctypes import wintypes
from datetime import datetime
from functools import reduce
from operator import or_
from pathlib import Path

import mss
import numpy as np
from PySide6.QtCore import QElapsedTimer, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QImage, QPainter, QPen
from PySide6.QtWidgets import QApplication, QMenu, QWidget

# --- Win32 -----------------------------------------------------------------------------
user32 = ctypes.windll.user32
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

WDA_EXCLUDEFROMCAPTURE = 0x11
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x20
WS_EX_LAYERED = 0x80000
LWA_ALPHA = 0x2

VK_CONTROL, VK_MENU, VK_ESCAPE = 0x11, 0x12, 0x1B
VK_OEM_PLUS, VK_OEM_MINUS = 0xBB, 0xBD


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def window_rect(hwnd):
    """Window rect in physical screen pixels."""
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


# Phosphor colors, selected with keys 1-9, 0 (in this order). Each is the brightest
# shade; the palette ramps from black (index 0) up to it (index 255).
COLORS = [
    ("green", (38, 255, 63)),
    ("blue", (40, 110, 255)),
    ("red", (255, 40, 40)),
    ("purple", (170, 60, 255)),
    ("yellow", (255, 240, 40)),
    ("cyan", (40, 255, 255)),
    ("amber", (255, 176, 0)),
    ("pink", (255, 60, 200)),
    ("orange", (255, 110, 20)),
    ("white", (235, 245, 255)),
]
COLOR_KEYS = [ord(str(n)) for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 0)]


def make_palette(peak):
    r, g, b = peak
    return [QColor(r * i // 255, g * i // 255, b * i // 255).rgb() for i in range(256)]


# Phosphor glow levels cycled with G: (name, brightness of the halo added on top).
GLOW_LEVELS = [("off", 0.0), ("soft", 0.7), ("strong", 1.4)]
GLOW_SCALE = 4    # the halo is computed at 1/4 resolution, then smoothly upscaled
GLOW_RADIUS = 2   # box blur radius at that resolution (two passes ~ gaussian)


def box_blur(a, r):
    """Separable box blur of a 2D float array with edge padding (rows, then columns)."""
    k = 2 * r + 1
    for _ in range(2):
        c = np.cumsum(np.pad(a, ((0, 0), (r + 1, r)), mode="edge"), axis=1)
        a = ((c[:, k:] - c[:, :-k]) / k).T   # transpose so the second pass blurs columns
    return a

HELP = """CRT FILTER
drag            move window
drag edges      resize
double-click    toggle fullscreen
right-click     menu
wheel / Up/Dn   threshold
Ctrl+wheel      contrast (soft mode)
M  soft/binary   I  invert
S  scanlines     A  auto threshold
G  glow off/soft/strong
H  this help     Esc  quit
P  screenshot (Pictures\\CRT_Filter)
1-9, 0  color: green blue red purple yellow
        cyan amber pink orange white

GLOBAL (work anytime)
Ctrl+Alt+F  fullscreen on/off
Ctrl+Alt+R  reselect area
Ctrl+Alt+C  click-through on/off
Ctrl+Alt+ + / -  threshold
Ctrl+Alt+I  invert
Ctrl+Alt+1-9, 0  color
Ctrl+Alt+P  screenshot
Ctrl+Alt+Q  quit
Esc (fullscreen)  exit fullscreen"""


class Selector(QWidget):
    """Full-desktop dimmed overlay: drag a rectangle to choose the area to filter."""

    def __init__(self, on_select, on_cancel, on_fullscreen):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.on_select, self.on_cancel, self.on_fullscreen = on_select, on_cancel, on_fullscreen
        self.origin = self.current = None

    def start(self):
        self.origin = self.current = None
        self.setGeometry(QGuiApplication.primaryScreen().virtualGeometry())
        self.show()
        self.raise_()
        self.activateWindow()

    def selection(self):
        return QRect(self.origin, self.current).normalized() if self.origin else QRect()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 120))
        sel = self.selection()
        if not sel.isEmpty():
            p.setCompositionMode(QPainter.CompositionMode_Clear)
            p.fillRect(sel, Qt.transparent)
            p.setCompositionMode(QPainter.CompositionMode_SourceOver)
            p.setPen(QPen(QColor(0, 255, 70), 2))
            p.drawRect(sel)
            p.drawText(sel.topLeft() + QPoint(4, -6), f"{sel.width()} x {sel.height()}")
        # Instructions centred on the primary screen.
        screen = QGuiApplication.primaryScreen().geometry().translated(-self.geometry().topLeft())
        p.setFont(QFont("Consolas", 14))
        p.setPen(QColor(0, 255, 70))
        p.drawText(screen.adjusted(0, 40, 0, 0), Qt.AlignHCenter | Qt.AlignTop,
                   "Drag to select an area     F = fullscreen     Esc = cancel")

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.origin = self.current = e.position().toPoint()
            self.update()
        elif e.button() == Qt.RightButton:
            self._cancel()

    def mouseMoveEvent(self, e):
        if self.origin:
            self.current = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or not self.origin:
            return
        sel = self.selection().translated(self.geometry().topLeft())
        self.hide()
        if sel.width() < 20 or sel.height() < 20:
            self.on_cancel()
        else:
            self.on_select(sel)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self._cancel()
        elif e.key() == Qt.Key_F:
            self.hide()
            self.on_fullscreen()

    def _cancel(self):
        self.hide()
        self.on_cancel()


class FilterWindow(QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("CRT Filter")
        self.setMouseTracking(True)
        self.setMinimumSize(60, 40)
        self.setAttribute(Qt.WA_OpaquePaintEvent)

        self.threshold = 128
        self.contrast = 4
        self.invert = False
        self.soft = False
        self.scanlines = True
        self.auto = False
        self.color_index = 0
        self.palette = make_palette(COLORS[0][1])
        self.click_through = False
        self.show_help = False
        self.has_region = False
        self.normal_geometry = None

        self.sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        self.frame = None   # keeps the numpy buffer alive while the QImage uses it
        self.image = None
        self.glow = 1       # index into GLOW_LEVELS
        self.glow_frame = None
        self.glow_image = None

        self.hud_text = ""
        self.hud_ms = 0
        self.hud_clock = QElapsedTimer()

        self.selector = Selector(self.region_selected, self.selection_cancelled, self.selection_fullscreen)

        self.frame_timer = QTimer(self, timerType=Qt.PreciseTimer, interval=15)
        self.frame_timer.timeout.connect(self.capture)
        self.key_timer = QTimer(self, interval=25)
        self.key_timer.timeout.connect(self.poll_hotkeys)
        self.key_timer.start()
        self.prev_keys = {}
        self.global_keys = {
            ord("F"): self.toggle_fullscreen,
            ord("R"): self.reselect,
            ord("C"): self.toggle_click_through,
            ord("I"): self.toggle_invert,
            ord("P"): self.screenshot,
            ord("Q"): self.quit,
            VK_OEM_PLUS: lambda: self.nudge_threshold(+8),
            VK_OEM_MINUS: lambda: self.nudge_threshold(-8),
        }
        for index, vk in enumerate(COLOR_KEYS):
            self.global_keys[vk] = lambda index=index: self.set_color(index)

    # --- window plumbing -----------------------------------------------------------------
    @property
    def hwnd(self):
        return int(self.winId())

    def showEvent(self, e):
        super().showEvent(e)
        if not user32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE):
            self.hud("WARNING: capture exclusion unsupported (needs Win10 2004+)", 6000)
        self.frame_timer.start()

    def hideEvent(self, e):
        super().hideEvent(e)
        self.frame_timer.stop()

    def set_click_through(self, on):
        self.click_through = on
        style = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        if on:
            user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
            user32.SetLayeredWindowAttributes(self.hwnd, 0, 255, LWA_ALPHA)
        else:
            user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, style & ~(WS_EX_LAYERED | WS_EX_TRANSPARENT))
        self.update()

    # --- capture & filter ----------------------------------------------------------------
    def capture(self):
        left, top, width, height = window_rect(self.hwnd)
        if width <= 0 or height <= 0:
            return
        shot = self.sct.grab({"left": left, "top": top, "width": width, "height": height})
        bgra = np.frombuffer(shot.bgra, np.uint8).reshape(shot.height, shot.width, 4)

        gray = bgra[..., 2].astype(np.uint16) * 77
        gray += bgra[..., 1].astype(np.uint16) * 150
        gray += bgra[..., 0].astype(np.uint16) * 29
        gray >>= 8

        threshold = int(gray.mean()) if self.auto else self.threshold
        if self.soft:
            out = (gray.astype(np.int16) - threshold) * self.contrast + 128
            out = np.clip(out, 0, 255).astype(np.uint8)
        else:
            out = (gray >= threshold).astype(np.uint8)
            out *= 255
        if self.invert:
            np.subtract(255, out, out=out)
        self.make_glow(out)   # before scanlines, so the halo also softens the dark rows
        if self.scanlines:
            out[2::3] >>= 1

        h, w = out.shape
        self.frame = np.ascontiguousarray(out)
        self.image = QImage(self.frame.data, w, h, w, QImage.Format_Indexed8)
        self.image.setColorTable(self.palette)
        self.update()

    def make_glow(self, out):
        """Blurred low-res copy of the frame; drawn additively it looks like phosphor bloom."""
        strength = GLOW_LEVELS[self.glow][1]
        h, w = out.shape[0] // GLOW_SCALE, out.shape[1] // GLOW_SCALE
        if strength == 0 or h < 2 or w < 2:
            self.glow_frame = self.glow_image = None
            return
        small = out[:h * GLOW_SCALE, :w * GLOW_SCALE].reshape(h, GLOW_SCALE, w, GLOW_SCALE)
        small = small.mean(axis=(1, 3), dtype=np.float32)
        halo = box_blur(box_blur(small, GLOW_RADIUS), GLOW_RADIUS)
        self.glow_frame = np.clip(halo * strength, 0, 255).astype(np.uint8)
        self.glow_image = QImage(self.glow_frame.data, w, h, w, QImage.Format_Indexed8)
        self.glow_image.setColorTable(self.palette)

    def draw_frame(self, p, rect):
        """Draw the filtered frame plus its phosphor glow into rect."""
        p.drawImage(rect, self.image)
        if self.glow_image is not None:
            p.save()
            p.setCompositionMode(QPainter.CompositionMode_Plus)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(rect, self.glow_image)
            p.restore()

    def paintEvent(self, _):
        p = QPainter(self)
        if self.image is not None:
            self.draw_frame(p, self.rect())
        else:
            p.fillRect(self.rect(), Qt.black)

        p.setFont(QFont("Consolas", 10))
        if self.show_help:
            box = self.rect().adjusted(10, 10, -10, -10)
            p.fillRect(box, QColor(0, 0, 0, 220))
            p.setPen(QColor(0, 255, 70))
            p.drawText(box.adjusted(12, 10, -12, -10), Qt.AlignLeft | Qt.AlignTop, HELP)
        elif self.hud_text and self.hud_clock.isValid() and self.hud_clock.elapsed() < self.hud_ms:
            metrics = p.fontMetrics()
            r = metrics.boundingRect(self.hud_text).adjusted(-8, -5, 8, 5)
            r.moveTopLeft(QPoint(8, 8))
            p.fillRect(r, QColor(0, 0, 0, 200))
            p.setPen(QColor(0, 255, 70))
            p.drawText(r, Qt.AlignCenter, self.hud_text)

        if not self.isFullScreen() and not self.click_through:
            p.setPen(QPen(QColor(0, 255, 70, 90), 1))
            p.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def hud(self, text, ms=1500):
        self.hud_text, self.hud_ms = text, ms
        self.hud_clock.restart()
        self.update()

    def status(self):
        mode = f"SOFT x{self.contrast}" if self.soft else "BINARY"
        thr = "AUTO" if self.auto else str(self.threshold)
        return f"{mode}  threshold {thr}" + ("  INVERT" if self.invert else "")

    # --- actions ---------------------------------------------------------------------------
    def nudge_threshold(self, delta):
        self.auto = False
        self.threshold = max(0, min(255, self.threshold + delta))
        self.hud(self.status())

    def nudge_contrast(self, delta):
        self.contrast = max(1, min(16, self.contrast + delta))
        self.hud(self.status())

    def set_color(self, index):
        self.color_index = index
        self.palette = make_palette(COLORS[index][1])
        for image in (self.image, self.glow_image):
            if image is not None:
                image.setColorTable(self.palette)
        self.hud("color: " + COLORS[index][0])

    def cycle_glow(self):
        self.glow = (self.glow + 1) % len(GLOW_LEVELS)
        self.hud("glow: " + GLOW_LEVELS[self.glow][0])

    def toggle_invert(self):
        self.invert = not self.invert
        self.hud(self.status())

    def toggle_soft(self):
        self.soft = not self.soft
        self.hud(self.status())

    def toggle_scanlines(self):
        self.scanlines = not self.scanlines
        self.hud("scanlines " + ("on" if self.scanlines else "off"))

    def toggle_auto(self):
        self.auto = not self.auto
        self.hud(self.status())

    def toggle_help(self):
        self.show_help = not self.show_help
        self.update()

    def toggle_click_through(self):
        if not self.isVisible():
            return
        self.set_click_through(not self.click_through)
        self.hud("click-through " + ("ON  (Ctrl+Alt+C to turn off)" if self.click_through else "off"), 2500)

    def toggle_fullscreen(self):
        if not self.isVisible():
            return
        if self.isFullScreen():
            self.showNormal()
            if self.normal_geometry:
                self.setGeometry(self.normal_geometry)
            self.set_click_through(False)
            self.hud("windowed")
        else:
            self.normal_geometry = self.geometry()
            screen = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
            self.setGeometry(screen.geometry())
            self.showFullScreen()
            # Fullscreen is click-through so the PC stays usable underneath.
            self.set_click_through(True)
            self.hud("FULLSCREEN - Esc or Ctrl+Alt+F to exit, Ctrl+Alt+Q to quit", 3000)

    def reselect(self):
        if self.selector.isVisible():
            return
        if self.isFullScreen():
            self.showNormal()
        self.set_click_through(False)
        self.hide()
        self.selector.start()

    def region_selected(self, rect):
        self.has_region = True
        self.setGeometry(rect)
        self.show()
        self.raise_()
        self.activateWindow()
        self.hud("H = help   Esc = quit", 2500)

    def selection_cancelled(self):
        if self.has_region:
            self.show()
        else:
            self.quit()

    def selection_fullscreen(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        g = screen.geometry()
        self.has_region = True
        self.normal_geometry = QRect(g.center() - QPoint(320, 200), g.center() + QPoint(320, 200))
        self.setGeometry(self.normal_geometry)
        self.show()
        self.toggle_fullscreen()

    def screenshot(self):
        """Save the current filtered frame (without HUD/help) as a PNG."""
        if self.image is None or not self.isVisible():
            return
        folder = Path.home() / "Pictures" / "CRT_Filter"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]  # millisecond precision
        path = folder / f"crt_{stamp}.png"
        shot = QImage(self.image.size(), QImage.Format_RGB32)
        p = QPainter(shot)
        self.draw_frame(p, shot.rect())
        p.end()
        if shot.save(str(path), "PNG"):
            self.hud(f"saved {path}", 3000)
        else:
            self.hud("screenshot failed", 3000)

    def quit(self):
        QApplication.quit()

    # --- input -----------------------------------------------------------------------------
    def poll_hotkeys(self):
        ctrl_alt = key_down(VK_CONTROL) and key_down(VK_MENU)
        for vk, action in self.global_keys.items():
            pressed = ctrl_alt and key_down(vk)
            if pressed and not self.prev_keys.get(vk):
                action()
            self.prev_keys[vk] = pressed
        esc = key_down(VK_ESCAPE)
        if esc and not self.prev_keys.get(VK_ESCAPE) and self.isFullScreen():
            self.toggle_fullscreen()
        self.prev_keys[VK_ESCAPE] = esc

    def keyPressEvent(self, e):
        key = e.key()
        if key == Qt.Key_Escape:
            if self.show_help:
                self.toggle_help()
            elif not self.isFullScreen():   # fullscreen Esc is handled by poll_hotkeys
                self.quit()
        elif e.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
            return  # Ctrl+Alt combos are global hotkeys
        elif key == Qt.Key_Up:
            self.nudge_threshold(+4)
        elif key == Qt.Key_Down:
            self.nudge_threshold(-4)
        elif key == Qt.Key_M:
            self.toggle_soft()
        elif key == Qt.Key_I:
            self.toggle_invert()
        elif key == Qt.Key_S:
            self.toggle_scanlines()
        elif key == Qt.Key_A:
            self.toggle_auto()
        elif key == Qt.Key_G:
            self.cycle_glow()
        elif key == Qt.Key_P:
            self.screenshot()
        elif key == Qt.Key_H:
            self.toggle_help()
        elif key == Qt.Key_F11:
            self.toggle_fullscreen()
        elif Qt.Key_0 <= key <= Qt.Key_9:
            self.set_color(COLOR_KEYS.index(key))   # Qt digit key codes match their ASCII codes

    def wheelEvent(self, e):
        step = 1 if e.angleDelta().y() > 0 else -1
        if e.modifiers() & Qt.ControlModifier:
            self.nudge_contrast(step)
        else:
            self.nudge_threshold(step * 4)

    def edges_at(self, pos, margin=8):
        edges = []
        if pos.x() < margin:
            edges.append(Qt.LeftEdge)
        if pos.x() > self.width() - margin:
            edges.append(Qt.RightEdge)
        if pos.y() < margin:
            edges.append(Qt.TopEdge)
        if pos.y() > self.height() - margin:
            edges.append(Qt.BottomEdge)
        return edges

    def mouseMoveEvent(self, e):
        if self.isFullScreen():
            self.unsetCursor()
            return
        edges = set(self.edges_at(e.position().toPoint()))
        if edges in ({Qt.LeftEdge, Qt.TopEdge}, {Qt.RightEdge, Qt.BottomEdge}):
            self.setCursor(Qt.SizeFDiagCursor)
        elif edges in ({Qt.RightEdge, Qt.TopEdge}, {Qt.LeftEdge, Qt.BottomEdge}):
            self.setCursor(Qt.SizeBDiagCursor)
        elif edges & {Qt.LeftEdge, Qt.RightEdge}:
            self.setCursor(Qt.SizeHorCursor)
        elif edges & {Qt.TopEdge, Qt.BottomEdge}:
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.setCursor(Qt.SizeAllCursor)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or self.isFullScreen():
            return
        edges = self.edges_at(e.position().toPoint())
        if edges:
            self.windowHandle().startSystemResize(reduce(or_, edges))
        else:
            self.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.toggle_fullscreen()

    def contextMenuEvent(self, e):
        menu = QMenu(self)

        def item(text, slot, checked=None, shortcut=""):
            action = menu.addAction(text + (f"\t{shortcut}" if shortcut else ""))
            if checked is not None:
                action.setCheckable(True)
                action.setChecked(checked)
            action.triggered.connect(slot)

        item("Fullscreen", self.toggle_fullscreen, self.isFullScreen(), "Ctrl+Alt+F")
        item("Reselect area", self.reselect, None, "Ctrl+Alt+R")
        item("Click-through", self.toggle_click_through, self.click_through, "Ctrl+Alt+C")
        menu.addSeparator()
        item("Soft contrast mode", self.toggle_soft, self.soft, "M")
        item("Invert", self.toggle_invert, self.invert, "I")
        item("Scanlines", self.toggle_scanlines, self.scanlines, "S")
        item("Auto threshold", self.toggle_auto, self.auto, "A")
        item(f"Glow: {GLOW_LEVELS[self.glow][0]}", self.cycle_glow, None, "G")
        colors = menu.addMenu("Color")
        for index, (name, _) in enumerate(COLORS):
            key = chr(COLOR_KEYS[index])
            action = colors.addAction(f"{name.capitalize()}\t{key} / Ctrl+Alt+{key}")
            action.setCheckable(True)
            action.setChecked(index == self.color_index)
            action.triggered.connect(lambda _=False, index=index: self.set_color(index))
        menu.addSeparator()
        item("Screenshot", self.screenshot, None, "Ctrl+Alt+P")
        item("Help", self.toggle_help, self.show_help, "H")
        item("Quit", self.quit, None, "Ctrl+Alt+Q")
        menu.exec(e.globalPos())


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = FilterWindow()
    window.selector.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
