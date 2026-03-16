"""ClarifyVoice – voice transcription with Gemini AI."""

import base64
import math
import os
import platform
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

import customtkinter as ctk
import keyboard
import numpy as np
import requests
import sounddevice as sd
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

load_env()

API_KEY = os.environ.get("API_KEY", "")
IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
DATA_DIR = (Path(os.environ.get("APPDATA", Path.home())) / "ClarifyVoice") if IS_WIN else (Path.home() / ".clarifyvoice")
DATA_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_PATH = DATA_DIR / "temp_recording.wav"

def find_sox():
    if IS_WIN:
        local = Path(__file__).parent / "extra" / "sox-14.4.2" / "sox.exe"
        if local.exists():
            return str(local)
    return "sox"

SOX_EXE = find_sox()

def get_primary_monitor():
    """Return (width, height) of the primary monitor work area."""
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        try:
            import ctypes
            from ctypes import wintypes
            rect = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
            return rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    return None

# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

PROMPT_INSTRUCTION = (
    "You are an expert editor and transcriber. "
    "Transcribe the audio, then rewrite it to be organized, clear, and comprehensible. "
    "Remove filler words and fix grammar. Tone: professional yet natural. "
    "Write in the first person. NEVER say 'The user says'. "
    "Return ONLY the rewritten text. "
    "Output MUST be in {lang}."
)
TRANSCRIPTION_INSTRUCTION = (
    "You are an expert transcriber. "
    "Transcribe the audio directly. Clean up filler words and fix basic grammar. "
    "Keep the original meaning and structure. Return ONLY the transcribed text. "
    "Output MUST be in {lang}."
)

LANG_NAMES = {"en": "English", "pt": "Brazilian Portuguese"}

def call_gemini(audio_path: Path, mode: str, lang: str = "en") -> str:
    if not API_KEY:
        return "[Error: No API_KEY]"
    audio_b64 = base64.b64encode(audio_path.read_bytes()).decode()
    lang_name = LANG_NAMES.get(lang, "English")
    instruction = (TRANSCRIPTION_INSTRUCTION if mode == "transcription" else PROMPT_INSTRUCTION).format(lang=lang_name)
    prompt = "Transcribe this audio." if mode == "transcription" else "Transcribe and rewrite this audio for clarity."
    body = {
        "contents": [{"parts": [
            {"inlineData": {"mimeType": "audio/wav", "data": audio_b64}},
            {"text": prompt},
        ]}],
        "systemInstruction": {"parts": [{"text": instruction}]},
        "generationConfig": {"temperature": 0.3},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={API_KEY}"
    try:
        r = requests.post(url, json=body, timeout=60)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return f"[Error: {e}]"

# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.proc = None
        self.mic_stream = None
        self.mic_level = 0.0

    def start(self):
        self._safe_delete(AUDIO_PATH)
        args = [SOX_EXE]
        if IS_WIN:
            args += ["-t", "waveaudio", "-d"]
        elif IS_MAC:
            args += ["-t", "coreaudio", "default"]
        else:
            args += ["-t", "pulseaudio", "default"]
        args += ["-r", "16000", "-c", "1", "-b", "16", "-e", "signed-integer", str(AUDIO_PATH)]
        kwargs = {}
        if IS_WIN:
            kwargs["creationflags"] = 0x08000000
            kwargs["cwd"] = str(Path(SOX_EXE).parent)
        self.proc = subprocess.Popen(args, stderr=subprocess.DEVNULL, **kwargs)
        try:
            self.mic_stream = sd.InputStream(channels=1, samplerate=16000, blocksize=1024, callback=self._audio_cb)
            self.mic_stream.start()
        except Exception:
            pass

    def _audio_cb(self, indata, frames, time_info, status):
        self.mic_level = min(1.0, float(np.sqrt(np.mean(indata ** 2))) * 8)

    def stop(self):
        if self.mic_stream:
            try: self.mic_stream.stop(); self.mic_stream.close()
            except Exception: pass
            self.mic_stream = None
        self.mic_level = 0.0
        if self.proc:
            pid = self.proc.pid
            try: self.proc.terminate(); self.proc.wait(timeout=3)
            except Exception: pass
            if IS_WIN:
                try: subprocess.run(["taskkill", "/F", "/PID", str(pid)], creationflags=0x08000000, capture_output=True, timeout=3)
                except Exception: pass
            self.proc = None
            time.sleep(0.8)

    def cancel(self):
        self.stop()
        self._safe_delete(AUDIO_PATH)

    @staticmethod
    def _safe_delete(path):
        for _ in range(5):
            try: path.unlink(missing_ok=True); return
            except PermissionError: time.sleep(0.3)

# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

def copy_and_paste(text):
    if IS_WIN:
        import ctypes
        subprocess.run("clip.exe", input=text.encode("utf-16-le"), check=False, creationflags=0x08000000)
        time.sleep(0.2)
        u = ctypes.windll.user32
        u.keybd_event(0x11, 0, 0, 0); u.keybd_event(0x56, 0, 0, 0)
        u.keybd_event(0x56, 0, 2, 0); u.keybd_event(0x11, 0, 2, 0)
    elif IS_MAC:
        subprocess.run(["pbcopy"], input=text.encode(), check=False)
        subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'], check=False)
    else:
        subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), check=False)
        subprocess.run(["xdotool", "key", "ctrl+v"], check=False)

# ---------------------------------------------------------------------------
# Flag icons (drawn with Pillow)
# ---------------------------------------------------------------------------

def _make_flag(kind, display=(20, 14)):
    """Draw flag at 4x then downscale for smooth anti-aliasing."""
    scale = 4
    w, h = display[0] * scale, display[1] * scale
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = h // 6  # corner radius

    # Rounded rectangle mask
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)

    if kind == "us":
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill="#b22234")
        stripe_h = h / 13
        for i in range(13):
            if i % 2 == 1:
                d.rectangle([0, int(i * stripe_h), w, int((i + 1) * stripe_h)], fill="#ffffff")
        cw, ch = int(w * 0.4), int(h * 0.54)
        d.rectangle([0, 0, cw, ch], fill="#3c3b6e")
        # Stars (small dots in grid)
        for row in range(4):
            for col in range(5):
                sx = int(cw * (col + 0.5) / 5)
                sy = int(ch * (row + 0.5) / 4)
                sr = max(2, w // 30)
                d.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], fill="#ffffff")
    elif kind == "br":
        d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill="#009c3b")
        cx, cy = w // 2, h // 2
        mx, my = int(w * 0.44), int(h * 0.40)
        d.polygon([(cx, cy - my), (cx + mx, cy), (cx, cy + my), (cx - mx, cy)], fill="#ffdf00")
        er = int(min(w, h) * 0.22)
        d.ellipse([cx - er, cy - er, cx + er, cy + er], fill="#002776")
        # White arc band
        band_r = int(er * 0.85)
        d.arc([cx - band_r, cy - int(band_r * 0.4), cx + band_r, cy + int(band_r * 1.4)],
              start=210, end=330, fill="#ffffff", width=max(1, scale))

    img.putalpha(mask)
    return img.resize(display, Image.LANCZOS)

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

# Black & white minimalist
CARD    = "#0a0a0a"
BORDER  = "#1c1c1c"
WHITE   = "#ffffff"
TEXT    = "#ffffff"
DIM     = "#666666"
ACCENT  = "#ffffff"
RED     = "#ffffff"
GREEN   = "#ffffff"
TRANSPARENT = "#010101"  # key color for window transparency

ctk.set_appearance_mode("dark")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("ClarifyVoice")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(fg_color=TRANSPARENT)

        # Make the keyed color fully transparent — window becomes truly rounded
        if IS_WIN:
            self.attributes("-transparentcolor", TRANSPARENT)

        self.recorder = Recorder()
        self.app_state = "ready"
        self.mode = "prompt"
        self.lang = "en"
        self.result_text = ""
        self._wave_running = False
        self._timer_running = False
        self._drag_x = 0
        self._drag_y = 0
        self._saved_pos = None

        sw = self.winfo_screenwidth()
        self.geometry(f"380x48+{sw - 400}+16")

        self._build_ui()
        self.bind("<Escape>", self._on_escape)
        keyboard.add_hotkey("alt+l", lambda: self.after(0, self.toggle_recording))
        keyboard.add_hotkey("alt+r", lambda: self.after(0, self._toggle_visibility))
        keyboard.add_hotkey("escape", lambda: self.after(0, self._on_escape))

    def _build_ui(self):
        # === IDLE CARD ===
        self.idle_card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=24,
            border_width=1, border_color=BORDER)
        self.idle_card.pack(fill="both", expand=True, padx=2, pady=2)

        bar = ctk.CTkFrame(self.idle_card, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=10)
        self._make_draggable(bar)

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        self._make_draggable(left)

        self.dot_lbl = ctk.CTkLabel(left, text="\u25cf", text_color=GREEN,
            font=ctk.CTkFont(size=11), anchor="w")
        self.dot_lbl.pack(side="left", padx=(0, 8))

        self.lbl = ctk.CTkLabel(left, text="Ready", text_color=TEXT,
            font=ctk.CTkFont(size=13, weight="bold"), anchor="w")
        self.lbl.pack(side="left", padx=(0, 6))
        self._make_draggable(self.lbl)

        self.sub = ctk.CTkLabel(left, text="Alt+L", text_color=DIM,
            font=ctk.CTkFont(size=10), anchor="w")
        self.sub.pack(side="left")
        self._make_draggable(self.sub)

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.pack(side="right")

        self._flag_en = ctk.CTkImage(light_image=_make_flag("us"), dark_image=_make_flag("us"), size=(20, 14))
        self._flag_br = ctk.CTkImage(light_image=_make_flag("br"), dark_image=_make_flag("br"), size=(20, 14))
        self.lang_btn = ctk.CTkButton(right, text="", image=self._flag_en, width=32, height=26, corner_radius=13,
            fg_color="#151515", hover_color="#222222", command=self._toggle_lang)
        self.lang_btn.pack(side="left", padx=(0, 4))

        self.mode_btn = ctk.CTkButton(right, text="Prompt", width=62, height=26, corner_radius=13,
            fg_color="#151515", hover_color="#222222", text_color=DIM,
            font=ctk.CTkFont(size=11), command=self._toggle_mode)
        self.mode_btn.pack(side="left", padx=(0, 4))

        self.close_btn = ctk.CTkButton(right, text="\u2014", width=26, height=26, corner_radius=13,
            fg_color="transparent", hover_color="#151515", text_color="#444444",
            font=ctk.CTkFont(size=10), command=self.quit)
        self.close_btn.pack(side="left")

        # Result panel (inside idle card, hidden by default)
        self.result_frame = ctk.CTkFrame(self.idle_card, fg_color="transparent")

        self.result_box = ctk.CTkTextbox(self.result_frame, fg_color="#050505", text_color="#cccccc",
            font=ctk.CTkFont(size=12), corner_radius=10, border_width=1, border_color=BORDER,
            wrap="word", height=100)
        self.result_box.pack(fill="both", expand=True, padx=14, pady=(0, 6))

        brow = ctk.CTkFrame(self.result_frame, fg_color="transparent")
        brow.pack(fill="x", padx=14, pady=(0, 10))

        self.copy_btn = ctk.CTkButton(brow, text="Copy", width=52, height=26, corner_radius=13,
            fg_color="#151515", hover_color="#222222", text_color=WHITE,
            font=ctk.CTkFont(size=11), command=self._copy)
        self.copy_btn.pack(side="left", padx=(0, 4))

        ctk.CTkButton(brow, text="Dismiss", width=56, height=26, corner_radius=13,
            fg_color="transparent", hover_color="#151515", text_color=DIM,
            font=ctk.CTkFont(size=11), command=self._hide_result).pack(side="left")

        # === RECORDING CARD (hidden by default) ===
        self.rec_card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=24,
            border_width=1, border_color=BORDER)

        rec_inner = ctk.CTkFrame(self.rec_card, fg_color="transparent")
        rec_inner.pack(expand=True, padx=12, pady=8)

        # Timer
        self.timer_lbl = ctk.CTkLabel(rec_inner, text="0:00", text_color=WHITE,
            font=ctk.CTkFont(size=13, weight="bold"), anchor="w")
        self.timer_lbl.pack(side="left", padx=(0, 8))

        # Waveform — fixed-width canvas
        W_W, W_H = 160, 26
        self.wave_cv = tk.Canvas(rec_inner, width=W_W, height=W_H, bg=CARD,
            highlightthickness=0, bd=0)
        self.wave_cv.pack(side="left")
        self._wave_n = 24
        self._wave_gap = W_W / self._wave_n
        self._wave_mid = W_H // 2
        self._wave_lines = []
        for i in range(self._wave_n):
            x = int(i * self._wave_gap + self._wave_gap / 2)
            ln = self.wave_cv.create_line(x, self._wave_mid, x, self._wave_mid,
                fill=WHITE, width=2, capstyle="round")
            self._wave_lines.append(ln)

        # Get primary monitor size for centering
        self._primary_mon = get_primary_monitor()

    # -- Drag --
    def _make_draggable(self, w):
        w.bind("<Button-1>", self._ds); w.bind("<B1-Motion>", self._dm)
    def _ds(self, e):
        self._drag_x = e.x_root - self.winfo_x(); self._drag_y = e.y_root - self.winfo_y()
    def _dm(self, e):
        self.geometry(f"+{e.x_root - self._drag_x}+{e.y_root - self._drag_y}")

    # -- State --
    def _set_state(self, s, t=""):
        self.app_state = s
        if s == "ready":
            self._wave_running = False
            self._timer_running = False
            # Switch to idle card
            self.rec_card.pack_forget()
            self.idle_card.pack(fill="both", expand=True, padx=2, pady=2)
            self.lbl.configure(text=t or "Ready", text_color=TEXT)
            self.sub.configure(text="Alt+L")
            self.dot_lbl.configure(text_color=GREEN)
            # Restore position
            if self._saved_pos:
                self.geometry(f"380x48+{self._saved_pos[0]}+{self._saved_pos[1]}")
                self._saved_pos = None
            else:
                x, y = self.winfo_x(), self.winfo_y()
                self.geometry(f"380x48+{x}+{y}")
        elif s == "recording":
            self._saved_pos = (self.winfo_x(), self.winfo_y())
            # Move + resize first, then swap content — no visible lag
            rw, rh = 230, 48
            if self._primary_mon:
                sw, sh = self._primary_mon
            else:
                sw = self.winfo_screenwidth()
                sh = self.winfo_screenheight()
            rx = (sw - rw) // 2
            ry = sh - rh - 80
            self.geometry(f"{rw}x{rh}+{rx}+{ry}")
            self.idle_card.pack_forget()
            self.rec_card.pack(fill="both", expand=True, padx=2, pady=2)
            self._wave_running = True; self._wave_tick()
            self._timer_running = True; self._timer_tick()
        elif s == "processing":
            self._wave_running = False
            self._timer_running = False
            self.rec_card.pack_forget()
            self.idle_card.pack(fill="both", expand=True, padx=2, pady=2)
            self.lbl.configure(text="Processing\u2026", text_color=ACCENT)
            self.sub.configure(text="")
            self.dot_lbl.configure(text_color=ACCENT)
            if self._saved_pos:
                self.geometry(f"380x48+{self._saved_pos[0]}+{self._saved_pos[1]}")
                self._saved_pos = None
            else:
                x, y = self.winfo_x(), self.winfo_y()
                self.geometry(f"380x48+{x}+{y}")

    # -- Wave --
    def _wave_tick(self):
        if not self._wave_running: return
        lv = self.recorder.mic_level; t = time.time()
        mid = self._wave_mid
        for i, ln in enumerate(self._wave_lines):
            x = int(i * self._wave_gap + self._wave_gap / 2)
            wave = 0.3 * math.sin(t * 5.0 + i * 0.8)
            amp = max(0.05, min(1.0, lv + wave))
            h = max(2, int(mid * amp))
            self.wave_cv.coords(ln, x, mid - h, x, mid + h)
        self.after(45, self._wave_tick)

    # -- Timer --
    def _timer_tick(self):
        if not self._timer_running: return
        elapsed = int(time.time() - self._rec_start)
        m, s = divmod(elapsed, 60)
        self.timer_lbl.configure(text=f"{m}:{s:02d}")
        self.after(500, self._timer_tick)

    # -- Actions --
    def _toggle_mode(self):
        self.mode = "transcription" if self.mode == "prompt" else "prompt"
        self.mode_btn.configure(text="Transcribe" if self.mode == "transcription" else "Prompt")

    def _toggle_lang(self):
        self.lang = "pt" if self.lang == "en" else "en"
        self.lang_btn.configure(image=self._flag_br if self.lang == "pt" else self._flag_en)

    def _cancel(self, e=None):
        if self.app_state == "recording":
            self._set_state("ready")
            threading.Thread(target=self.recorder.cancel, daemon=True).start()

    def _on_escape(self, e=None):
        if self.app_state == "recording": self._cancel()
        elif self.result_frame.winfo_manager(): self._hide_result()

    def _copy(self):
        if self.result_text:
            threading.Thread(target=lambda: copy_and_paste(self.result_text), daemon=True).start()
            self.copy_btn.configure(text="OK!")
            self.after(1200, lambda: self.copy_btn.configure(text="Copy"))

    def _hide_result(self):
        self.result_frame.pack_forget()
        x, y = self.winfo_x(), self.winfo_y()
        self.geometry(f"380x48+{x}+{y}")

    def _show_result(self, text):
        self.result_text = text
        self.result_box.configure(state="normal")
        self.result_box.delete("0.0", "end")
        self.result_box.insert("0.0", text)
        self.result_box.configure(state="disabled")
        self.result_frame.pack(fill="both", expand=True)
        x, y = self.winfo_x(), self.winfo_y()
        h = min(360, max(160, text.count("\n") * 20 + 140))
        self.geometry(f"400x{h}+{x}+{y}")

    # -- Recording --
    def toggle_recording(self):
        if self.app_state == "recording": self._stop_recording()
        elif self.app_state == "ready": self._start_recording()

    def _start_recording(self):
        if self.result_frame.winfo_manager(): self._hide_result()
        self._rec_start = time.time()
        self._set_state("recording")
        def start():
            try: self.recorder.start()
            except Exception as e: self.after(0, lambda: self._set_state("ready", f"Err: {e}"))
        threading.Thread(target=start, daemon=True).start()

    def _stop_recording(self):
        elapsed = time.time() - self._rec_start
        if elapsed < 3:
            self._set_state("ready", "Too short")
            threading.Thread(target=self.recorder.cancel, daemon=True).start()
            return
        self._set_state("processing")
        def run():
            self.recorder.stop()
            time.sleep(0.3)
            if not AUDIO_PATH.exists() or AUDIO_PATH.stat().st_size < 1000:
                self.after(0, lambda: self._set_state("ready", "No audio")); return
            text = call_gemini(AUDIO_PATH, self.mode, self.lang)
            Recorder._safe_delete(AUDIO_PATH)
            if text and not text.startswith("[Error"):
                self.after(0, lambda: self._on_result(text))
            else:
                self.after(0, lambda: self._set_state("ready", "Error"))
        threading.Thread(target=run, daemon=True).start()

    def _on_result(self, text):
        self._set_state("ready")
        self._show_result(text)
        threading.Thread(target=lambda: copy_and_paste(text), daemon=True).start()

    # -- Visibility --
    def _toggle_visibility(self):
        if self.winfo_viewable(): self.withdraw()
        else: self.deiconify(); self.attributes("-topmost", True)


if __name__ == "__main__":
    if not API_KEY:
        print("Error: Set API_KEY in .env"); sys.exit(1)
    App().mainloop()
