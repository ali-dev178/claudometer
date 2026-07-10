"""Capture the native Settings window (light + dark) into assets/settings.png.

Unlike make_assets.py (which is pure Pillow), the settings panel is a real Tk
window, so this must run on Windows with a display:

    py assets/capture_settings.py
"""
import os
import sys
import tempfile
import ctypes
from ctypes import wintypes

os.environ.setdefault("CLAUDOMETER_CONFIG",
                      os.path.join(tempfile.gettempdir(), "cw_capture.toml"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk                       # noqa: E402
from PIL import Image, ImageDraw           # noqa: E402
import settings                            # noqa: E402
import widget_bar                          # noqa: E402
import render                              # noqa: E402
from make_assets import drop_shadow        # noqa: E402

OUT = os.path.dirname(os.path.abspath(__file__))
user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32


class BMIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


def _capture(hwnd):
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right, r.bottom
    hdc = user32.GetDC(hwnd)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    user32.PrintWindow(hwnd, memdc, 2)  # PW_RENDERFULLCONTENT
    bmi = BMIH(); bmi.biSize = ctypes.sizeof(BMIH)
    bmi.biWidth, bmi.biHeight = w, -h
    bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    gdi32.DeleteObject(bmp); gdi32.DeleteDC(memdc); user32.ReleaseDC(hwnd, hdc)
    return img


def _grab(theme):
    root = tk.Tk()
    root.geometry("100x24+0+0")  # tiny visible master so the child stays mapped
    cfg = settings.load(); cfg["resume_auto"] = True  # advanced opens by default
    win = widget_bar.SettingsWindow(root, theme, cfg, on_apply=lambda c: None,
                                    on_demo=lambda: None)
    win.top.deiconify(); win.top.lift()
    for _ in range(30):
        win.top.update_idletasks(); win.top.update()
    img = _capture(win.top.winfo_id())
    root.destroy()
    return img


def main():
    light, dark = _grab("light"), _grab("dark")
    m, gap = 44, 46
    W = m + light.width + gap + dark.width + m
    H = m + max(light.height, dark.height) + 34 + m
    bg = render._vgrad(W, H, "#eef1f6", "#e2e7ef").convert("RGBA")
    d = ImageDraw.Draw(bg)
    f = render._font("sb", 13)

    def place(im, x, label):
        sh, pad = drop_shadow(im.size, 10, 20, 70)
        bg.alpha_composite(sh, (x - pad, m - pad + 7))
        bg.paste(im, (x, m))
        d.text((x + im.width / 2, m + im.height + 16), label, font=f, fill="#5b6675", anchor="mm")

    place(light, m, "Light")
    place(dark, m + light.width + gap, "Dark")
    bg.convert("RGB").save(os.path.join(OUT, "settings.png"))
    print("wrote assets/settings.png", (W, H))


if __name__ == "__main__":
    main()
