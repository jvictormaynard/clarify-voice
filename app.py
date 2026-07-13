"""ClarifyVoice – voice transcription with Gemini, OpenAI, or Groq."""

import argparse
import base64
import json
import math
import mimetypes
import os
import platform
import subprocess
import sys
import threading
import time
import types
import wave
from pathlib import Path

import requests

try:
    import tkinter as tk
except Exception:
    tk = None

try:
    import customtkinter as ctk
except Exception:
    ctk = types.SimpleNamespace(CTk=object, set_appearance_mode=lambda *_: None)

try:
    import keyboard
except Exception:
    keyboard = None

try:
    import sounddevice as sd
except Exception:
    sd = None

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageTk
except Exception:
    Image = None
    ImageChops = None
    ImageDraw = None
    ImageFont = None
    ImageTk = None

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

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
DATA_DIR = (Path(os.environ.get("APPDATA", Path.home())) / "ClarifyVoice") if IS_WIN else (Path.home() / ".clarifyvoice")
DATA_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_PATH = DATA_DIR / "temp_recording.wav"
CONFIG_PATH = DATA_DIR / "config.json"
STATS_PATH = DATA_DIR / "usage_stats.json"
_STATS_LOCK = threading.Lock()
PILL_FADE_IN_SECONDS = 0.12
PILL_FADE_OUT_SECONDS = 0.14
MICROPHONE_ALERT_SECONDS = 1.5
MICROPHONE_PILL_WIDTH = 100

DEFAULT_CONFIG = {
    "transcription_provider": "gemini",
    "gemini_api_key": os.environ.get("GEMINI_API_KEY", os.environ.get("API_KEY", "")),
    "gemini_base_url": os.environ.get(
        "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"),
    "gemini_model": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
    "openai_api_key": os.environ.get("OPENAI_API_KEY", ""),
    "openai_base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    "openai_audio_model": os.environ.get("OPENAI_AUDIO_MODEL", "whisper-1"),
    "openai_text_model": os.environ.get("OPENAI_TEXT_MODEL", "gpt-4o-mini"),
    "groq_api_key": os.environ.get("GROQ_API_KEY", ""),
    "groq_base_url": os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
    "groq_audio_model": os.environ.get("GROQ_AUDIO_MODEL", "whisper-large-v3-turbo"),
    "groq_text_model": os.environ.get("GROQ_TEXT_MODEL", "llama-3.3-70b-versatile"),
    "refinement_provider": os.environ.get("REFINEMENT_PROVIDER", ""),
    "refinement_model": os.environ.get("REFINEMENT_MODEL", ""),
    "ui_mode": "prompt",
    "ui_language": "en",
}

AUDIO_MODEL_ALIASES = {
    ("groq", "whisper large v3 turbo"): "whisper-large-v3-turbo",
    ("groq", "whisper large v3"): "whisper-large-v3",
    ("openai", "whisper 1"): "whisper-1",
}

# Public list prices used only for the local estimate shown in Statistics.
# Unknown/custom models are deliberately left unpriced instead of guessing.
AUDIO_COST_USD_PER_MINUTE = {
    ("openai", "whisper-1"): 0.006,
    ("openai", "gpt-4o-mini-transcribe"): 0.003,
    ("openai", "gpt-4o-transcribe"): 0.006,
    ("openai", "gpt-4o-transcribe-diarize"): 0.006,
    ("groq", "whisper-large-v3-turbo"): 0.04 / 60,
    ("groq", "whisper-large-v3"): 0.111 / 60,
    # Gemini audio is token-priced. These rates assume 32 audio tokens/second.
    ("gemini", "gemini-2.5-flash"): 1.00 * 32 * 60 / 1_000_000,
    ("gemini", "gemini-2.5-flash-lite"): 0.30 * 32 * 60 / 1_000_000,
}

TEXT_COST_USD_PER_MILLION_TOKENS = {
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
    ("groq", "llama-3.3-70b-versatile"): (0.59, 0.79),
    ("gemini", "gemini-2.5-flash"): (0.30, 2.50),
    ("gemini", "gemini-2.5-flash-lite"): (0.10, 0.40),
}


def _word_count(text: str) -> int:
    return len(str(text or "").split())


def _estimated_text_cost(provider: str, model: str, input_chars: int,
        output_chars: int) -> tuple[float, bool]:
    rates = TEXT_COST_USD_PER_MILLION_TOKENS.get((provider, model))
    if rates is None:
        return 0.0, False
    # A transparent approximation when providers do not return usage metadata.
    input_tokens = max(0, input_chars) / 4
    output_tokens = max(0, output_chars) / 4
    return ((input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000, True)


def _recording_usage_context() -> dict:
    provider = str(APP_CONFIG.get("transcription_provider", "gemini"))
    model_keys = {
        "gemini": "gemini_model", "openai": "openai_audio_model",
        "groq": "groq_audio_model",
    }
    model = _canonical_audio_model(provider, APP_CONFIG.get(
        model_keys.get(provider, "gemini_model"), ""))
    context = {
        "provider": provider,
        "model": model,
        "mode": str(APP_CONFIG.get("ui_mode", "prompt")),
        "refinement_provider": "",
        "refinement_model": "",
    }
    if context["mode"] == "prompt" and provider != "gemini":
        context["refinement_provider"] = str(APP_CONFIG.get(
            "refinement_provider", ""))
        context["refinement_model"] = str(APP_CONFIG.get(
            "refinement_model", ""))
    return context


def _build_recording_usage_event(context: dict, duration_seconds: float,
        result_text: str) -> dict:
    provider = str(context.get("provider", ""))
    model = str(context.get("model", ""))
    duration = max(0.0, float(duration_seconds))
    models = [{"provider": provider, "model": model, "purpose": "transcription"}]
    audio_rate = AUDIO_COST_USD_PER_MINUTE.get((provider, model))
    cost = 0.0 if audio_rate is None else audio_rate * duration / 60
    complete = audio_rate is not None
    output_chars = len(str(result_text or ""))

    refinement_provider = str(context.get("refinement_provider", ""))
    refinement_model = str(context.get("refinement_model", ""))
    if refinement_provider and refinement_model:
        models.append({"provider": refinement_provider, "model": refinement_model,
                       "purpose": "refinement"})
        text_cost, text_known = _estimated_text_cost(
            refinement_provider, refinement_model, output_chars, output_chars)
        cost += text_cost
        complete = complete and text_known
    elif provider == "gemini":
        # Gemini returns the transcript from the same multimodal request.
        text_cost, text_known = _estimated_text_cost(
            provider, model, 0, output_chars)
        cost += text_cost
        complete = complete and text_known

    return {
        "timestamp": time.time(),
        "type": "recording",
        "duration_seconds": round(duration, 3),
        "mode": str(context.get("mode", "transcription")),
        "models": models,
        "word_count": _word_count(result_text),
        "character_count": output_chars,
        "estimated_cost_usd": round(cost, 8),
        "cost_complete": complete,
    }


def _build_rewrite_usage_event(provider: str, model: str, source: str,
        result: str) -> dict:
    cost, complete = _estimated_text_cost(
        provider, model, len(source), len(result))
    return {
        "timestamp": time.time(),
        "type": "rewrite",
        "duration_seconds": 0.0,
        "mode": "rewrite",
        "models": [{"provider": provider, "model": model, "purpose": "refinement"}],
        "word_count": _word_count(result),
        "character_count": len(result),
        "estimated_cost_usd": round(cost, 8),
        "cost_complete": complete,
    }


def _load_usage_events() -> list[dict]:
    try:
        payload = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        events = payload.get("events", []) if isinstance(payload, dict) else []
        return [event for event in events if isinstance(event, dict)]
    except (OSError, ValueError, TypeError):
        return []


def _record_usage_event(event: dict) -> None:
    """Persist anonymous usage metadata; transcript contents are never stored."""
    with _STATS_LOCK:
        events = _load_usage_events()
        events.append(event)
        temp_path = STATS_PATH.with_suffix(".tmp")
        temp_path.write_text(json.dumps({"version": 1, "events": events}, indent=2),
            encoding="utf-8")
        temp_path.replace(STATS_PATH)


def _usage_summary(events=None, now=None) -> dict:
    events = _load_usage_events() if events is None else list(events)
    now = time.time() if now is None else float(now)
    recordings = [event for event in events if event.get("type") == "recording"]
    model_counts = {}
    for event in events:
        for entry in event.get("models", []):
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("model", "")).strip()
            provider = str(entry.get("provider", "")).strip()
            if label:
                key = (provider, label)
                model_counts[key] = model_counts.get(key, 0) + 1
    ranked_models = sorted(model_counts.items(), key=lambda item: (-item[1], item[0][1]))
    total_seconds = sum(max(0.0, float(event.get("duration_seconds", 0) or 0))
        for event in recordings)
    total_cost = sum(max(0.0, float(event.get("estimated_cost_usd", 0) or 0))
        for event in events)
    return {
        "recordings": len(recordings),
        "rewrites": sum(event.get("type") == "rewrite" for event in events),
        "total_seconds": total_seconds,
        "average_seconds": total_seconds / len(recordings) if recordings else 0.0,
        "total_words": sum(max(0, int(event.get("word_count", 0) or 0))
            for event in recordings),
        "total_cost_usd": total_cost,
        "cost_complete": all(bool(event.get("cost_complete", False)) for event in events),
        "last_7_days": sum(float(event.get("timestamp", 0) or 0) >= now - 7 * 86400
            for event in recordings),
        "ranked_models": ranked_models,
        "model_calls": sum(model_counts.values()),
    }


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _canonical_audio_model(provider, model):
    value = str(model or "").strip()
    return AUDIO_MODEL_ALIASES.get((provider, value.casefold()), value)


def _load_app_config():
    config = DEFAULT_CONFIG.copy()
    try:
        stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            config.update({key: value for key, value in stored.items() if key in config})
    except (OSError, ValueError, TypeError):
        pass
    if config["transcription_provider"] not in ("openai", "gemini", "groq"):
        config["transcription_provider"] = "gemini"
    if config["refinement_provider"] not in ("openai", "gemini", "groq"):
        provider = config["transcription_provider"]
        config["refinement_provider"] = provider if provider in ("openai", "groq") else "openai"
    if not str(config["refinement_model"]).strip():
        provider = config["refinement_provider"]
        config["refinement_model"] = str(config.get(
            f"{provider}_text_model",
            "llama-3.3-70b-versatile" if provider == "groq" else "gpt-4o-mini"))
    if config["ui_mode"] not in ("prompt", "transcription"):
        config["ui_mode"] = "prompt"
    if config["ui_language"] not in ("en", "pt"):
        config["ui_language"] = "en"
    config["openai_audio_model"] = _canonical_audio_model(
        "openai", config["openai_audio_model"])
    config["groq_audio_model"] = _canonical_audio_model(
        "groq", config["groq_audio_model"])
    return config


APP_CONFIG = _load_app_config()


def _save_app_config():
    temp_path = CONFIG_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(APP_CONFIG, indent=2), encoding="utf-8")
    temp_path.replace(CONFIG_PATH)


def _autostart_command(executable=None):
    executable = str(executable or sys.executable)
    args = [executable]
    if not getattr(sys, "frozen", False):
        args.append(str(Path(__file__).resolve()))
    args.append("--hidden")
    return subprocess.list2cmdline(args)


def _set_autostart(enabled: bool, registry=None):
    """Enable or disable hidden startup for the current Windows user."""
    if not IS_WIN:
        return
    if registry is None:
        import winreg as registry
    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with registry.CreateKey(registry.HKEY_CURRENT_USER, path) as key:
        if enabled:
            registry.SetValueEx(
                key, "ClarifyVoice", 0, registry.REG_SZ, _autostart_command())
        else:
            try:
                registry.DeleteValue(key, "ClarifyVoice")
            except FileNotFoundError:
                pass


def _is_autostart_enabled(registry=None) -> bool:
    if not IS_WIN:
        return False
    if registry is None:
        import winreg as registry
    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, path) as key:
            value, _kind = registry.QueryValueEx(key, "ClarifyVoice")
        return bool(str(value).strip())
    except OSError:
        return False


def _apply_selected_models(selected, selected_refinement, audio_options,
        text_options, model_keys):
    """Persist independently valid transcription and text-model selections."""
    audio_choice = (selected["provider"], selected["model"])
    if audio_choice in audio_options:
        APP_CONFIG["transcription_provider"] = selected["provider"]
        APP_CONFIG[model_keys[selected["provider"]]] = _canonical_audio_model(
            selected["provider"], selected["model"])
    text_choice = (selected_refinement["provider"], selected_refinement["model"])
    if text_choice in text_options:
        APP_CONFIG["refinement_provider"] = selected_refinement["provider"]
        APP_CONFIG["refinement_model"] = selected_refinement["model"]


def _enable_windows_dpi_awareness():
    """Enable sharp per-monitor rendering before Tk creates any windows."""
    if not IS_WIN:
        return
    try:
        import ctypes
        # PER_MONITOR_AWARE_V2: native-resolution rendering on mixed-DPI displays.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_enable_windows_dpi_awareness()


class _WindowsSingleInstanceApi:
    """Small Win32 adapter for named mutex/event single-instance coordination."""

    ERROR_ALREADY_EXISTS = 183
    WAIT_OBJECT_0 = 0
    INFINITE = 0xFFFFFFFF

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._kernel32 = ctypes.windll.kernel32
        self._kernel32.CreateEventW.argtypes = [
            wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel32.CreateEventW.restype = wintypes.HANDLE
        self._kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        self._kernel32.CreateMutexW.restype = wintypes.HANDLE
        self._kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        self._kernel32.SetEvent.restype = wintypes.BOOL
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def create_event(self, name):
        return self._kernel32.CreateEventW(None, False, False, name)

    def create_mutex(self, name):
        handle = self._kernel32.CreateMutexW(None, True, name)
        return handle, self._kernel32.GetLastError() == self.ERROR_ALREADY_EXISTS

    def set_event(self, handle):
        return self._kernel32.SetEvent(handle)

    def wait_for_event(self, handle):
        return self._kernel32.WaitForSingleObject(handle, self.INFINITE)

    def close(self, handle):
        if handle:
            self._kernel32.CloseHandle(handle)


class SingleInstanceGuard:
    """Allow one UI process and let later launches reveal the hidden instance."""

    MUTEX_NAME = "Local\\ClarifyVoice.SingleInstance.v1"
    EVENT_NAME = "Local\\ClarifyVoice.ShowExisting.v1"

    def __init__(self, api=None, event_handle=None, mutex_handle=None):
        self.api = api
        self.event_handle = event_handle
        self.mutex_handle = mutex_handle

    @classmethod
    def acquire(cls, api=None):
        if not IS_WIN:
            return cls()
        api = api or _WindowsSingleInstanceApi()
        event_handle = api.create_event(cls.EVENT_NAME)
        if not event_handle:
            raise OSError("could not create the single-instance activation event")
        mutex_handle, already_exists = api.create_mutex(cls.MUTEX_NAME)
        if not mutex_handle:
            api.close(event_handle)
            raise OSError("could not create the single-instance mutex")
        if already_exists:
            api.set_event(event_handle)
            api.close(mutex_handle)
            api.close(event_handle)
            return None
        return cls(api, event_handle, mutex_handle)

    def start_activation_listener(self, callback):
        if not self.api or not self.event_handle:
            return

        def wait_loop():
            while self.event_handle:
                result = self.api.wait_for_event(self.event_handle)
                if result != self.api.WAIT_OBJECT_0:
                    return
                callback()

        threading.Thread(target=wait_loop, daemon=True).start()

    def close(self):
        if not self.api:
            return
        self.api.close(self.mutex_handle)
        self.api.close(self.event_handle)
        self.mutex_handle = None
        self.event_handle = None


def _apply_windows_rounded_corners(widget):
    """Ask DWM to clip a normal window to native Windows 11 rounded corners."""
    if not IS_WIN:
        return
    try:
        import ctypes
        from ctypes import wintypes
        widget.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        hwnd = user32.GetParent(widget.winfo_id()) or widget.winfo_id()
        preference = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference))
    except Exception:
        pass

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
            from ctypes import wintypes
            rect = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
            return rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    return None


def _window_executable(hwnd=None):
    """Return the executable owning a Windows window."""
    if not IS_WIN:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        hwnd = hwnd or user32.GetForegroundWindow()
        if not hwnd:
            return None

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value or pid.value == os.getpid():
            return None

        process = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not process:
            return None
        try:
            size = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                executable = path.value
                if Path(executable).name.lower() == "explorer.exe":
                    # Alt+Tab, the desktop, and the taskbar are also hosted by
                    # explorer.exe. Only CabinetWClass is an actual File
                    # Explorer window; ignore transient shell foregrounds.
                    class_name = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(hwnd, class_name, len(class_name))
                    if class_name.value != "CabinetWClass":
                        return None
                return executable
        finally:
            kernel32.CloseHandle(process)
    except Exception:
        pass
    return None


def _foreground_executable():
    return _window_executable()


def _normalize_app_icon(icon, size=64):
    """Normalize brightness, transparent padding, and visual footprint."""
    icon = icon.convert("RGBA")
    alpha_channel = icon.getchannel("A")
    visible_box = alpha_channel.point(lambda value: 255 if value >= 128 else 0).getbbox()
    if visible_box:
        icon = icon.crop(visible_box)

    opaque = [(r, g, b) for r, g, b, a in icon.getdata() if a >= 192]
    if opaque:
        brightness = sorted(max(pixel) for pixel in opaque)
        saturation = sum(max(pixel) - min(pixel) for pixel in opaque) / len(opaque)
        high = brightness[min(len(brightness) - 1, int(len(brightness) * 0.96))]
        if saturation < 24 and high < 220 and high > 0:
            factor = min(1.8, 255 / high)
            red, green, blue, alpha_channel = icon.split()
            red = red.point(lambda value: min(255, int(value * factor)))
            green = green.point(lambda value: min(255, int(value * factor)))
            blue = blue.point(lambda value: min(255, int(value * factor)))
            icon = Image.merge("RGBA", (red, green, blue, alpha_channel))

    target = size - 8
    ratio = min(target / icon.width, target / icon.height)
    resized = icon.resize(
        (max(1, round(icon.width * ratio)), max(1, round(icon.height * ratio))),
        Image.Resampling.LANCZOS)
    normalized = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    normalized.alpha_composite(
        resized, ((size - resized.width) // 2, (size - resized.height) // 2))
    return normalized


def _packaged_app_icon(executable, size=64):
    """Use the shell-ready visual asset supplied by packaged Windows apps."""
    try:
        executable_path = Path(executable)
        for parent in executable_path.parents:
            assets = parent / "assets"
            if not assets.is_dir():
                continue
            patterns = (
                f"Square44x44Logo.targetsize-{size}*_altform-unplated.png",
                f"Square44x44Logo.targetsize-{size}*.png",
                "Square44x44Logo.scale-200.png",
                "Square44x44Logo.png",
                "icon.png",
            )
            for pattern in patterns:
                candidate = next(assets.glob(pattern), None)
                if candidate:
                    with Image.open(candidate) as image:
                        return _normalize_app_icon(image.copy(), size)
            break
    except Exception:
        pass
    return None


def _executable_icon(executable, size=64):
    """Extract an executable icon as a high-resolution Pillow RGBA image."""
    if not IS_WIN or not executable or Image is None:
        return None
    packaged_icon = _packaged_app_icon(executable, size)
    if packaged_icon is not None:
        return packaged_icon
    try:
        import ctypes
        from ctypes import wintypes

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

        shell32, user32, gdi32 = ctypes.windll.shell32, ctypes.windll.user32, ctypes.windll.gdi32
        user32.PrivateExtractIconsW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.UINT),
            wintypes.UINT, wintypes.UINT]
        user32.PrivateExtractIconsW.restype = wintypes.UINT
        user32.DrawIconEx.argtypes = [
            wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON,
            ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
        user32.DrawIconEx.restype = wintypes.BOOL
        user32.DestroyIcon.argtypes = [wintypes.HICON]
        user32.DestroyIcon.restype = wintypes.BOOL
        shell32.ExtractIconExW.argtypes = [
            wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.HICON),
            ctypes.POINTER(wintypes.HICON), wintypes.UINT]
        shell32.ExtractIconExW.restype = wintypes.UINT
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
        gdi32.SelectObject.restype = wintypes.HANDLE
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        large_icon = wintypes.HICON()
        icon_id = wintypes.UINT()
        extracted = user32.PrivateExtractIconsW(
            str(executable), 0, size, size,
            ctypes.byref(large_icon), ctypes.byref(icon_id), 1, 0)
        if extracted != 1 or not large_icon:
            # PrivateExtractIconsW may leave the output slot undefined on
            # failure. Never pass that value to DrawIconEx or DestroyIcon.
            large_icon = wintypes.HICON()
            if shell32.ExtractIconExW(str(executable), 0, ctypes.byref(large_icon), None, 1) != 1:
                return None

        dc = gdi32.CreateCompatibleDC(0)
        bits = ctypes.c_void_p()
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = size
        info.bmiHeader.biHeight = -size  # top-down pixels
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        bitmap = gdi32.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        old_bitmap = gdi32.SelectObject(dc, bitmap)
        try:
            ctypes.memset(bits, 0, size * size * 4)
            user32.DrawIconEx(dc, 0, 0, large_icon, size, size, 0, None, 0x0003)
            pixels = bytearray(ctypes.string_at(bits, size * size * 4))

            # DrawIconEx returns premultiplied BGRA. Pillow/Tk expect straight
            # alpha; without this conversion translucent edges are darkened a
            # second time when composited onto the pill.
            for offset in range(0, len(pixels), 4):
                alpha = pixels[offset + 3]
                if 0 < alpha < 255:
                    pixels[offset] = min(255, pixels[offset] * 255 // alpha)
                    pixels[offset + 1] = min(255, pixels[offset + 1] * 255 // alpha)
                    pixels[offset + 2] = min(255, pixels[offset + 2] * 255 // alpha)

            icon = Image.frombytes("RGBA", (size, size), bytes(pixels), "raw", "BGRA")
            return _normalize_app_icon(icon, size)
        finally:
            gdi32.SelectObject(dc, old_bitmap)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(dc)
            user32.DestroyIcon(large_icon)
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

FAITHFUL_REWRITE_INSTRUCTION = (
    "Perform a faithful editorial rewrite that is organized, clear, and "
    "comprehensible. This is editing, not summarization: "
    "preserve every requirement, constraint, example, named service, provider, "
    "model, technical identifier, and relationship expressed by the speaker. "
    "Do not generalize, omit, merge, or invent technical details. Preserve the "
    "speaker's perspective and intent, including imperative wording when the "
    "speaker is dictating a task. Preserve attention directives such as "
    "'observe' or 'note' instead of recasting them as 'I request' or describing "
    "the speaker from outside. For example, Portuguese 'Observe no X que...' "
    "should remain a directive such as 'Observe que, no X,...', rather than "
    "being reduced to 'No X,...'. When editing API-related text, keep credentials "
    "such as API keys distinct from routing choices such as base URLs, endpoints, "
    "and proxies. When a normal API is contrasted with a proxy, express the two "
    "routing modes clearly: a conventional API key using the official endpoint, "
    "or a custom base URL/proxy. Never claim that a proxy eliminates authentication "
    "unless the speaker states that explicitly and unambiguously. Prefer the "
    "original framing and make the smallest "
    "structural edits needed for clarity. Remove filler words, redundant "
    "introductions, repetition, and false "
    "starts, and fix grammar and punctuation. Use paragraphs, bullet points, "
    "and light Markdown formatting for technical identifiers when they make the "
    "result easier to read. Tone: professional yet natural. "
    "NEVER say 'The user says'. "
)
PROMPT_INSTRUCTION = (
    "You are an expert editor and transcriber. Transcribe the audio first. "
    + FAITHFUL_REWRITE_INSTRUCTION +
    "Return ONLY the rewritten text. "
    "Output MUST be in {lang}."
)
TRANSCRIPTION_INSTRUCTION = (
    "You are an expert transcriber. "
    "Transcribe the audio directly. Clean up filler words and fix basic grammar. "
    "Keep the original meaning and structure. Return ONLY the transcribed text. "
    "Output MUST be in {lang}."
)
SELECTION_REWRITE_INSTRUCTION = (
    "You are a substantive editor. The input is existing text, not audio. "
    "Rewrite it at the sentence and paragraph level to improve coherence, "
    "cohesion, logical progression, clarity, concision, and natural flow. "
    "Do not behave like a spellchecker and do not limit the edit to grammar, "
    "punctuation, or isolated word substitutions. Identify the central point, "
    "put ideas in the clearest order, combine fragments and overlapping "
    "sentences, eliminate tautologies and false starts, and make relationships "
    "between ideas explicit when the source already supports them. You may "
    "substantially restructure sentences whenever that produces stronger prose. "
    "Preserve the original language, meaning, facts, requirements, constraints, "
    "examples, names, technical identifiers, degree of certainty, perspective, "
    "and tone. Keep informal text natural instead of making it unnecessarily "
    "formal. Never invent details, strengthen a claim, or replace a specific "
    "concept with a different one. Correct spelling, grammar, punctuation, "
    "capitalization, and sentence boundaries. Expand informal abbreviations "
    "when their standard form is unambiguous, such as Portuguese 'vc' to "
    "'você'. Preserve useful Markdown. For a genuinely simple sentence that "
    "only has objective writing errors, a light edit is enough; for a disjointed "
    "or repetitive passage, perform a real structural rewrite. Example: "
    "'Acredito que não precisamos fixar esses preços. Cada freela é diferente. "
    "A proposta depende do projeto e do preço oferecido.' can become "
    "'Não acho que devamos trabalhar com preços fixos, porque cada freela tem "
    "suas particularidades. A proposta deve ser definida caso a caso, "
    "considerando o projeto e o valor oferecido.' Return ONLY the rewritten "
    "text, with no explanation or surrounding quotation marks. Before "
    "answering, silently check whether the result merely fixes surface errors "
    "while retaining avoidable repetition or a weak sequence of ideas; if it "
    "does, rewrite it once more at the structural level. Never return "
    "the input unchanged when it has an objective writing error or a clear "
    "structural weakness."
)

LANG_NAMES = {"en": "English", "pt": "Brazilian Portuguese"}

STRINGS = {
    "en": {
        "ready": "Ready", "processing": "Processing\u2026", "too_short": "Too short",
        "no_audio": "No audio", "error": "Error", "prompt": "Prompt",
        "transcribe": "Transcribe", "copy": "Copy", "copied": "OK!",
        "dismiss": "Dismiss", "hint": "Alt+L", "hint_stop": "Alt+L stop",
        "rewriting": "Rewriting…", "no_selection": "No text selected",
        "rewrite_failed": "Rewrite failed", "rewrite_copied": "Result copied",
        "settings": "Settings", "provider": "Provider:",
        "settings_section": "Settings", "models_section": "Models",
        "providers_section": "Providers", "statistics_section": "Statistics",
        "statistics_title": "Usage overview",
        "statistics_subtitle": "Local totals from successful ClarifyVoice actions",
        "stat_recordings": "Recordings", "stat_recording_time": "Recording time",
        "stat_estimated_cost": "Estimated cost", "stat_words": "Words transcribed",
        "most_used_models": "Most used models", "no_statistics": "No usage recorded yet",
        "stat_average": "Average recording", "stat_last_7_days": "Last 7 days",
        "stat_rewrites": "Text rewrites", "stat_uses": "{count} uses",
        "cost_disclaimer": "Approximate public API pricing; unknown or custom models are excluded.",
        "autostart": "Start Clarify automatically",
        "autostart_subtitle": "Run in the background and start hidden when you sign in to Windows.",
        "choose_model": "Models", "model_subtitle": "Configure transcription and text processing",
        "transcription_model": "Transcription",
        "text_refinement_model": "Text refinement",
        "refinement_subtitle": "Choose an LLM for text rewriting and transcript refinement",
        "multimodal_refinement": "This multimodal model handles transcription and text refinement in one request.",
        "providers_subtitle": "Connect and manage AI providers",
        "add_provider": "+ Add provider", "active": "Active",
        "not_configured": "Not configured", "validating": "Validating…",
        "validation_failed": "Validation failed: {error}",
        "validate_save": "Validate & save", "back": "Back",
        "deactivate": "Deactivate provider", "credentials_valid": "Credentials validated",
        "no_active_models": "No active providers. Add one to choose a model.",
        "api_key": "API key", "api_key_placeholder": "Paste the provider API key",
        "base_url": "Custom URL", "custom_endpoint": "Custom endpoint", "model": "Model",
        "refresh_models": "Refresh models", "loading_models": "Loading models…",
        "models_found": "{count} audio model(s) available",
        "no_models": "No compatible audio models announced by this endpoint",
        "models_error": "Could not load models: {error}",
        "prompt_model": "Text refinement model (Prompt mode)",
        "openai_prompt_hint": "Whisper transcribes; this model organizes the result.",
        "gemini_proxy_hint": "Proxy requires /v1beta/models/{model}:generateContent",
        "apply": "Apply", "save": "Save", "cancel": "Cancel",
    },
    "pt": {
        "ready": "Pronto", "processing": "Processando\u2026", "too_short": "Muito curto",
        "no_audio": "Sem \u00e1udio", "error": "Erro", "prompt": "Prompt",
        "transcribe": "Transcrever", "copy": "Copiar", "copied": "OK!",
        "dismiss": "Fechar", "hint": "Alt+L", "hint_stop": "Alt+L parar",
        "rewriting": "Reescrevendo…", "no_selection": "Nenhum texto selecionado",
        "rewrite_failed": "Falha ao reescrever", "rewrite_copied": "Resultado copiado",
        "settings": "Configura\u00e7\u00f5es", "provider": "Provedor:",
        "settings_section": "Configura\u00e7\u00f5es", "models_section": "Modelos",
        "providers_section": "Provedores", "statistics_section": "Estatísticas",
        "statistics_title": "Visão geral de uso",
        "statistics_subtitle": "Totais locais de ações concluídas no ClarifyVoice",
        "stat_recordings": "Gravações", "stat_recording_time": "Tempo de gravação",
        "stat_estimated_cost": "Custo estimado", "stat_words": "Palavras transcritas",
        "most_used_models": "Modelos mais utilizados", "no_statistics": "Nenhum uso registrado ainda",
        "stat_average": "Média por gravação", "stat_last_7_days": "Últimos 7 dias",
        "stat_rewrites": "Reescritas de texto", "stat_uses": "{count} usos",
        "cost_disclaimer": "Preços públicos aproximados; modelos desconhecidos ou personalizados são excluídos.",
        "autostart": "Iniciar o Clarify automaticamente",
        "autostart_subtitle": "Executar em segundo plano e iniciar oculto ao entrar no Windows.",
        "choose_model": "Modelos", "model_subtitle": "Configure a transcri\u00e7\u00e3o e o processamento do texto",
        "transcription_model": "Transcri\u00e7\u00e3o",
        "text_refinement_model": "Refinamento de texto",
        "refinement_subtitle": "Escolha um LLM para reescrever textos e refinar transcri\u00e7\u00f5es",
        "multimodal_refinement": "Este modelo multimodal faz a transcri\u00e7\u00e3o e o refinamento em uma única requisi\u00e7ão.",
        "providers_subtitle": "Conecte e gerencie provedores de IA",
        "add_provider": "+ Adicionar provedor", "active": "Ativo",
        "not_configured": "N\u00e3o configurado", "validating": "Validando…",
        "validation_failed": "Falha na valida\u00e7\u00e3o: {error}",
        "validate_save": "Validar e salvar", "back": "Voltar",
        "deactivate": "Desativar provedor", "credentials_valid": "Credenciais validadas",
        "no_active_models": "Nenhum provedor ativo. Adicione um para escolher um modelo.",
        "api_key": "Chave de API", "api_key_placeholder": "Cole a chave do provedor",
        "base_url": "URL personalizada", "custom_endpoint": "Endpoint personalizado",
        "model": "Modelo",
        "refresh_models": "Atualizar modelos", "loading_models": "Carregando modelos…",
        "models_found": "{count} modelo(s) de áudio disponível(is)",
        "no_models": "Este endpoint não anuncia modelos de áudio compatíveis",
        "models_error": "Não foi possível carregar os modelos: {error}",
        "prompt_model": "Modelo de refinamento do texto (modo Prompt)",
        "openai_prompt_hint": "O Whisper transcreve; este modelo organiza o resultado.",
        "gemini_proxy_hint": "O proxy precisa expor /v1beta/models/{model}:generateContent",
        "apply": "Aplicar", "save": "Salvar", "cancel": "Cancelar",
    },
}

def _audio_mime_type(audio_path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(audio_path))
    if guessed and guessed.startswith("audio/"):
        return guessed
    return "audio/wav"


def _provider_url(base_url: str, version: str, endpoint: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.lower().endswith(f"/{version.lower()}"):
        return f"{base}/{endpoint.lstrip('/')}"
    return f"{base}/{version}/{endpoint.lstrip('/')}"


def _http_error(provider: str, error) -> str:
    if isinstance(error, requests.HTTPError) and error.response is not None:
        detail = error.response.text.strip().replace("\n", " ")[:300]
        return f"[Error: {provider} HTTP {error.response.status_code}: {detail}]"
    return f"[Error: {provider}: {error}]"


OPENAI_OFFICIAL_AUDIO_MODELS = (
    "whisper-1",
    "gpt-4o-mini-transcribe",
    "gpt-4o-transcribe",
    "gpt-4o-transcribe-diarize",
)
GROQ_OFFICIAL_AUDIO_MODELS = (
    "whisper-large-v3-turbo",
    "whisper-large-v3",
)


def _provider_model_entries(payload) -> list[dict]:
    entries = payload.get("models", payload.get("data", []))
    return entries if isinstance(entries, list) else []


def _parse_audio_models(provider: str, payload) -> list[str]:
    models = []
    for entry in _provider_model_entries(payload):
        if not isinstance(entry, dict):
            continue
        identifier_fields = (("name", "id") if provider == "gemini" else ("id", "name"))
        model_id = str(next(
            (entry.get(field) for field in identifier_fields if entry.get(field)), "")).strip()
        if model_id.startswith("models/"):
            model_id = model_id.removeprefix("models/")
        if not model_id:
            continue
        if provider == "gemini":
            methods = entry.get("supportedGenerationMethods")
            if methods is not None and "generateContent" not in methods:
                continue
            if "gemini" not in model_id.lower():
                continue
        elif not any(token in model_id.lower() for token in ("whisper", "transcribe")):
            continue
        models.append(model_id)
    return sorted(set(models), key=str.lower)


def _parse_text_models(provider: str, payload) -> list[str]:
    """Return generative language models, excluding ASR and other modalities."""
    excluded = (
        "whisper", "transcribe", "transcription", "speech", "tts", "audio",
        "embedding", "embed", "moderation", "dall-e", "image", "realtime",
    )
    models = []
    for entry in _provider_model_entries(payload):
        if not isinstance(entry, dict):
            continue
        identifier_fields = (("name", "id") if provider == "gemini" else ("id", "name"))
        model_id = str(next(
            (entry.get(field) for field in identifier_fields if entry.get(field)), "")).strip()
        if model_id.startswith("models/"):
            model_id = model_id.removeprefix("models/")
        if not model_id or any(token in model_id.lower() for token in excluded):
            continue
        if provider == "gemini":
            methods = entry.get("supportedGenerationMethods")
            if methods is not None and "generateContent" not in methods:
                continue
            if "gemini" not in model_id.lower():
                continue
        models.append(model_id)
    return sorted(set(models), key=str.lower)


def _provider_model_headers(provider: str, api_key: str, base_url: str) -> dict:
    headers = {}
    if provider == "gemini":
        headers["x-goog-api-key"] = api_key
        if "generativelanguage.googleapis.com" not in base_url.lower():
            headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _fetch_provider_models(provider: str, api_key: str, base_url: str) -> list[str]:
    """Return only transcription-capable models announced by the provider."""
    base_url = base_url.strip().rstrip("/")
    if provider == "openai" and "api.openai.com" in base_url.lower():
        return list(OPENAI_OFFICIAL_AUDIO_MODELS)
    if provider == "groq" and "api.groq.com" in base_url.lower():
        return list(GROQ_OFFICIAL_AUDIO_MODELS)
    if not base_url:
        raise ValueError("base URL is empty")
    version = "v1beta" if provider == "gemini" else "v1"
    response = requests.get(
        _provider_url(base_url, version, "models"),
        headers=_provider_model_headers(provider, api_key, base_url), timeout=12)
    response.raise_for_status()
    return _parse_audio_models(provider, response.json())


def _validate_provider_credentials(provider: str, api_key: str, base_url: str) -> dict:
    """Validate a provider key using its non-generative model-list endpoint."""
    api_key = api_key.strip()
    base_url = base_url.strip().rstrip("/")
    if not api_key:
        raise ValueError("API key is required")
    if not base_url:
        raise ValueError("base URL is required")
    version = "v1beta" if provider == "gemini" else "v1"
    response = requests.get(
        _provider_url(base_url, version, "models"),
        headers=_provider_model_headers(provider, api_key, base_url), timeout=12)
    response.raise_for_status()
    return response.json()


def _make_provider_icon(provider: str, size: int = 64):
    """Load the real provider mark bundled with the application."""
    if Image is None:
        return None
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    path = root / "assets" / "providers" / f"{provider}.png"
    try:
        image = Image.open(path).convert("RGBA")
        return image.resize((size, size), Image.Resampling.LANCZOS)
    except OSError:
        return None


def call_gemini(audio_path: Path, mode: str, lang: str = "en") -> str:
    api_key = str(APP_CONFIG.get("gemini_api_key", "")).strip()
    if not api_key:
        return "[Error: No Gemini API key]"
    audio_b64 = base64.b64encode(audio_path.read_bytes()).decode()
    lang_name = LANG_NAMES.get(lang, "English")
    instruction = (TRANSCRIPTION_INSTRUCTION if mode == "transcription" else PROMPT_INSTRUCTION).format(lang=lang_name)
    prompt = "Transcribe this audio." if mode == "transcription" else "Transcribe and rewrite this audio for clarity."
    body = {
        "contents": [{"parts": [
            {"inlineData": {"mimeType": _audio_mime_type(audio_path), "data": audio_b64}},
            {"text": prompt},
        ]}],
        "systemInstruction": {"parts": [{"text": instruction}]},
        "generationConfig": {"temperature": 0.0 if mode == "transcription" else 0.1},
    }
    base_url = str(APP_CONFIG.get("gemini_base_url", ""))
    model = str(APP_CONFIG.get("gemini_model", "gemini-2.5-flash")).strip()
    if model.startswith("models/"):
        model = model.removeprefix("models/")
    if not model:
        return "[Error: No Gemini model]"
    url = _provider_url(base_url, "v1beta", f"models/{model}:generateContent")
    if not url:
        return "[Error: No Gemini base URL]"
    headers = {"x-goog-api-key": api_key}
    if "generativelanguage.googleapis.com" not in base_url.lower():
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        r = requests.post(url, headers=headers, json=body, timeout=60)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return _http_error("Gemini", e)


def _rewrite_openai_compatible(
        provider: str, transcript: str, lang: str, model_override: str = "",
        instruction: str = "") -> str:
    label = "Groq" if provider == "groq" else "OpenAI"
    api_key = str(APP_CONFIG.get(f"{provider}_api_key", "")).strip()
    url = _provider_url(
        str(APP_CONFIG.get(f"{provider}_base_url", "")), "v1", "chat/completions")
    default_model = "llama-3.3-70b-versatile" if provider == "groq" else "gpt-4o-mini"
    model = (model_override.strip()
        or str(APP_CONFIG.get(f"{provider}_text_model", default_model)).strip()
        or default_model)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": instruction or PROMPT_INSTRUCTION.format(
                lang=LANG_NAMES.get(lang, "English"))},
            {"role": "user", "content": transcript},
        ],
        "temperature": 0.1,
    }
    try:
        response = requests.post(
            url, headers={"Authorization": f"Bearer {api_key}"}, json=body, timeout=60)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as error:
        return _http_error(f"{label} prompt", error)


def _rewrite_openai(transcript: str, lang: str) -> str:
    return _rewrite_openai_compatible("openai", transcript, lang)


def _rewrite_gemini_text(
        transcript: str, lang: str, model: str, instruction: str = "") -> str:
    api_key = str(APP_CONFIG.get("gemini_api_key", "")).strip()
    base_url = str(APP_CONFIG.get("gemini_base_url", ""))
    model = model.removeprefix("models/").strip()
    if not api_key or not model:
        return "[Error: Gemini refinement is not configured]"
    headers = {"x-goog-api-key": api_key}
    if "generativelanguage.googleapis.com" not in base_url.lower():
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "contents": [{"parts": [{"text": transcript}]}],
        "systemInstruction": {"parts": [{"text": instruction or PROMPT_INSTRUCTION.format(
            lang=LANG_NAMES.get(lang, "English"))}]},
        "generationConfig": {"temperature": 0.1},
    }
    try:
        response = requests.post(
            _provider_url(base_url, "v1beta", f"models/{model}:generateContent"),
            headers=headers, json=body, timeout=60)
        response.raise_for_status()
        return response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as error:
        return _http_error("Gemini refinement", error)


def _refine_transcript(transcript: str, lang: str) -> str:
    provider = str(APP_CONFIG.get("refinement_provider", "openai"))
    model = str(APP_CONFIG.get("refinement_model", "")).strip()
    if provider == "gemini":
        return _rewrite_gemini_text(transcript, lang, model)
    if provider in ("openai", "groq"):
        return _rewrite_openai_compatible(provider, transcript, lang, model)
    return "[Error: No text refinement model configured]"


def rewrite_selected_text(text: str) -> str:
    """Rewrite selected prose with the configured text-refinement model."""
    source = str(text).strip()
    if not source:
        return "[Error: No text selected]"
    provider = str(APP_CONFIG.get("refinement_provider", "")).strip().lower()
    model = str(APP_CONFIG.get("refinement_model", "")).strip()
    if provider not in ("gemini", "openai", "groq") or not model:
        return "[Error: No text refinement model configured]"
    if not str(APP_CONFIG.get(f"{provider}_api_key", "")).strip():
        return f"[Error: No {provider.title()} API key]"
    if provider == "gemini":
        result = _rewrite_gemini_text(
            source, "en", model, SELECTION_REWRITE_INSTRUCTION)
    else:
        result = _rewrite_openai_compatible(
            provider, source, "en", model, SELECTION_REWRITE_INSTRUCTION)
    if not result or not result.strip():
        return "[Error: Provider returned an empty rewrite]"
    return result.strip()


def call_openai(audio_path: Path, mode: str, lang: str = "en") -> str:
    return _call_openai_compatible_audio("openai", audio_path, mode, lang)


def _call_openai_compatible_audio(
        provider: str, audio_path: Path, mode: str, lang: str = "en") -> str:
    label = "Groq" if provider == "groq" else "OpenAI"
    api_key = str(APP_CONFIG.get(f"{provider}_api_key", "")).strip()
    if not api_key:
        return f"[Error: No {label} API key]"
    url = _provider_url(
        str(APP_CONFIG.get(f"{provider}_base_url", "")), "v1", "audio/transcriptions")
    if not url:
        return f"[Error: No {label} base URL]"
    default_model = "whisper-large-v3-turbo" if provider == "groq" else "whisper-1"
    try:
        with audio_path.open("rb") as audio_file:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (audio_path.name, audio_file, _audio_mime_type(audio_path))},
                data={
                    "model": _canonical_audio_model(provider, APP_CONFIG.get(
                        f"{provider}_audio_model", default_model)),
                    "response_format": "json",
                    "language": lang,
                },
                timeout=60,
            )
        response.raise_for_status()
        payload = response.json()
        transcript = str(payload.get("text", "")).strip()
        if not transcript:
            return f"[Error: {label} returned an empty transcription]"
        return (_refine_transcript(transcript, lang)
                if mode == "prompt" else transcript)
    except Exception as error:
        return _http_error(f"{label} Whisper", error)


def call_groq(audio_path: Path, mode: str, lang: str = "en") -> str:
    return _call_openai_compatible_audio("groq", audio_path, mode, lang)


def call_transcription_provider(audio_path: Path, mode: str, lang: str = "en") -> str:
    provider = APP_CONFIG.get("transcription_provider")
    if provider == "openai":
        return call_openai(audio_path, mode, lang)
    if provider == "groq":
        return call_groq(audio_path, mode, lang)
    return call_gemini(audio_path, mode, lang)

# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class MicrophoneUnavailableError(RuntimeError):
    """Raised when Windows has no active microphone input to record."""


def _has_active_microphone():
    """Return whether PortAudio can see a usable default input device.

    ``None`` means the lightweight meter is unavailable, so SoX remains the
    source of truth instead of rejecting a recording pre-emptively.
    """
    if sd is None:
        return None
    try:
        device = sd.query_devices(kind="input")
        return int(device.get("max_input_channels", 0)) > 0
    except Exception:
        return False


class Recorder:
    def __init__(self):
        self.proc = None
        self._process_job = None
        self.mic_stream = None
        self.mic_level = 0.0

    def start(self):
        # A forced app update/restart can leave SoX alive after its parent exits.
        # Every recorder writes to the same temp path, so such an orphan keeps
        # growing the next request until the provider rejects the huge upload.
        self.stop()
        self._stop_stale_windows_recorders()
        self._safe_delete(AUDIO_PATH)
        if _has_active_microphone() is False:
            raise MicrophoneUnavailableError("No active microphone")
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
        if IS_WIN:
            self._process_job = self._assign_kill_on_close_job(self.proc)
        try:
            self.mic_stream = sd.RawInputStream(
                channels=1, samplerate=16000, blocksize=256,
                dtype="int16", callback=self._audio_cb)
            self.mic_stream.start()
        except Exception:
            pass
        # WaveAudio can accept process creation and then exit immediately when
        # the Windows input endpoint is disabled. Give it a brief opportunity
        # to report that failure before treating the pill as a live recording.
        time.sleep(0.18)
        if self.proc.poll() is not None:
            self.proc = None
            self._close_process_job()
            raise MicrophoneUnavailableError("No active microphone")

    def _audio_cb(self, indata, frames, time_info, status):
        samples = memoryview(indata).cast("h")
        if samples:
            mean_square = sum(sample * sample for sample in samples) / len(samples)
            # Preserve the previous normalized-float RMS calibration.
            self.mic_level = min(1.0, math.sqrt(mean_square) / 32768.0 * 16)

    def stop(self):
        if self.mic_stream:
            try: self.mic_stream.stop(); self.mic_stream.close()
            except Exception: pass
            self.mic_stream = None
        self.mic_level = 0.0
        if self.proc:
            pid = self.proc.pid
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                if IS_WIN:
                    try: subprocess.run(["taskkill", "/F", "/PID", str(pid)], creationflags=0x08000000, capture_output=True, timeout=3)
                    except Exception: pass
            if IS_WIN:
                self._close_process_job()
            self.proc = None
            time.sleep(0.8)

    @staticmethod
    def _assign_kill_on_close_job(proc):
        """Tie SoX to this app so Windows kills it if the app is force-closed."""
        try:
            import ctypes
            from ctypes import wintypes

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class BASIC_LIMITS(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class EXTENDED_LIMITS(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BASIC_LIMITS),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.windll.kernel32
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                return None
            limits = EXTENDED_LIMITS()
            limits.BasicLimitInformation.LimitFlags = 0x00002000
            configured = kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
            assigned = configured and kernel32.AssignProcessToJobObject(
                job, wintypes.HANDLE(proc._handle))
            if assigned:
                return job
            kernel32.CloseHandle(job)
        except Exception:
            pass
        return None

    def _close_process_job(self):
        if not self._process_job:
            return
        try:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self._process_job)
        except Exception:
            pass
        self._process_job = None

    @staticmethod
    def _stop_stale_windows_recorders():
        """Stop orphaned SoX instances that still target our temp WAV file."""
        if not IS_WIN:
            return
        target = str(AUDIO_PATH).replace("'", "''")
        script = (
            f"$target = '{target}'; "
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -ieq 'sox.exe' -and "
            "$_.CommandLine -like ('*' + $target + '*') } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
            "-ErrorAction SilentlyContinue }")
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                creationflags=0x08000000, capture_output=True, timeout=8)
        except Exception:
            pass

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

def _foreground_window_handle():
    if not IS_WIN:
        return None
    import ctypes
    from ctypes import wintypes
    ctypes.windll.user32.GetForegroundWindow.restype = wintypes.HWND
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def _clipboard_sequence_number():
    if not IS_WIN:
        return 0
    import ctypes
    return int(ctypes.windll.user32.GetClipboardSequenceNumber())


def _open_windows_clipboard(timeout=0.25):
    """Open the clipboard with a short bounded retry for transient contention."""
    import ctypes
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ctypes.windll.user32.OpenClipboard(None):
            return
        time.sleep(0.01)
    raise OSError("clipboard is busy")


def _get_windows_clipboard_text():
    if not IS_WIN:
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _open_windows_clipboard()
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _set_windows_clipboard_text(text):
    if not IS_WIN:
        return False
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    encoded = (str(text) + "\0").encode("utf-16-le")
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    handle = kernel32.GlobalAlloc(0x0002, len(encoded))  # GMEM_MOVEABLE
    if not handle:
        raise OSError("could not allocate clipboard memory")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise OSError("could not lock clipboard memory")
    ctypes.memmove(pointer, encoded, len(encoded))
    kernel32.GlobalUnlock(handle)
    _open_windows_clipboard()
    transferred = False
    try:
        if not user32.EmptyClipboard():
            raise OSError("could not clear clipboard")
        if not user32.SetClipboardData(13, handle):  # CF_UNICODETEXT
            raise OSError("could not set clipboard text")
        transferred = True
        return True
    finally:
        user32.CloseClipboard()
        if not transferred:
            kernel32.GlobalFree(handle)


def _send_key_chord(chord):
    keyboard.send(chord)


def _copy_selected_text(timeout=0.7):
    """Copy a selection and return text only when the clipboard changed."""
    previous_sequence = _clipboard_sequence_number()
    _send_key_chord("ctrl+c")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _clipboard_sequence_number() != previous_sequence:
            return _get_windows_clipboard_text()
        time.sleep(0.02)
    return None


def _same_selected_text(left, right):
    def normalize(value):
        return str(value).replace("\r\n", "\n").replace("\r", "\n")
    return normalize(left) == normalize(right)


def _estimate_result_lines(text, chars_per_line=54):
    """Estimate wrapped lines before Tk has a rendered textbox width."""
    logical_lines = str(text).splitlines() or [""]
    return sum(max(1, math.ceil(len(line.expandtabs(4)) / chars_per_line))
               for line in logical_lines)


def _result_text_height(text, display_lines=None):
    lines = max(1, int(display_lines or _estimate_result_lines(text)))
    return min(220, max(38, 18 + lines * 18))


def _result_window_height(header_height, result_height):
    # Header has 10px vertical pack padding; the root card has 2px.
    return min(360, max(96, int(header_height) + 20 + int(result_height) + 4))

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
        # At 20x14, the literal 13 stripes and 50 stars collapse into blurry
        # sub-pixels. Use a small-size optical version with fewer, crisp marks.
        stripe_h = h / 7
        for i in range(7):
            if i % 2 == 1:
                d.rectangle([0, int(i * stripe_h), w, int((i + 1) * stripe_h)], fill="#ffffff")
        cw, ch = int(w * 0.46), int(h * 0.57)
        d.rectangle([0, 0, cw, ch], fill="#3c3b6e")
        # Six clean star hints remain legible after downsampling.
        for row in range(2):
            for col in range(3):
                sx = int(cw * (col + 0.55) / 3.1)
                sy = int(ch * (row + 0.55) / 2.1)
                sr = max(2, round(scale * 0.65))
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
WINDOW_FADE_IN_MS = 150
WINDOW_FADE_OUT_MS = 140
WINDOW_FADE_FRAME_MS = 10

ctk.set_appearance_mode("dark")


def _window_opacity(widget):
    if IS_WIN:
        return max(0.0, min(1.0, float(
            getattr(widget, "_clarify_opacity", 1.0))))
    try:
        return max(0.0, min(1.0, float(widget.attributes("-alpha"))))
    except (tk.TclError, TypeError, ValueError):
        return 1.0


def _set_window_opacity(widget, opacity):
    """Keep a Tk window and its optional native backdrop at the same alpha."""
    opacity = max(0.0, min(1.0, float(opacity)))
    if IS_WIN:
        try:
            import ctypes
            hwnd = _windows_window_handle(widget)
            user32 = ctypes.windll.user32
            ex_style = user32.GetWindowLongW(hwnd, -20)
            if not ex_style & 0x00080000:  # WS_EX_LAYERED
                user32.SetWindowLongW(hwnd, -20, ex_style | 0x00080000)
            user32.SetLayeredWindowAttributes(
                hwnd, 0, round(opacity * 255), 0x00000002)  # LWA_ALPHA
            widget._clarify_opacity = opacity
        except Exception:
            return
    else:
        try:
            widget.attributes("-alpha", opacity)
        except tk.TclError:
            return
    for name in ("_smooth_backdrop", "_main_backdrop"):
        backdrop = getattr(widget, name, None)
        if backdrop is not None:
            backdrop.set_opacity(opacity)


def _windows_window_handle(widget):
    """Return the native wrapper HWND used by a Tk/CustomTkinter window."""
    if not IS_WIN:
        return None
    import ctypes
    from ctypes import wintypes
    widget.update_idletasks()
    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    child = widget.winfo_id()
    return user32.GetParent(child) or child


def _configure_windows_tool_window(widget):
    """Hide an owned ClarifyVoice window from Alt+Tab without blocking focus."""
    if not IS_WIN:
        return
    try:
        import ctypes
        hwnd = _windows_window_handle(widget)
        user32 = ctypes.windll.user32
        ex_style = user32.GetWindowLongW(hwnd, -20)
        ex_style |= 0x00000080   # WS_EX_TOOLWINDOW
        ex_style &= ~0x00040000  # WS_EX_APPWINDOW
        user32.SetWindowLongW(hwnd, -20, ex_style)
        user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)  # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
    except Exception:
        pass


def _make_window_draggable(widget, *handles):
    """Use one or more header widgets as a native-feeling drag surface."""
    drag = {"x": 0, "y": 0}

    def start(event):
        drag["x"] = event.x_root - widget.winfo_x()
        drag["y"] = event.y_root - widget.winfo_y()

    def move(event):
        widget.geometry(
            f"+{event.x_root - drag['x']}+{event.y_root - drag['y']}")

    for handle in handles:
        handle.bind("<Button-1>", start, add="+")
        handle.bind("<B1-Motion>", move, add="+")


def _animate_window_opacity(widget, target, duration_ms, on_complete=None):
    """Animate global window opacity with a compact smoothstep curve."""
    previous_job = getattr(widget, "_clarify_fade_job", None)
    if previous_job is not None:
        try:
            widget.after_cancel(previous_job)
        except tk.TclError:
            pass
    start = _window_opacity(widget)
    target = max(0.0, min(1.0, float(target)))
    started = time.perf_counter()
    duration = max(0.001, float(duration_ms) / 1000.0)

    def tick():
        try:
            if not widget.winfo_exists():
                return
        except tk.TclError:
            return
        progress = min(1.0, (time.perf_counter() - started) / duration)
        eased = progress * progress * (3.0 - 2.0 * progress)
        _set_window_opacity(widget, start + (target - start) * eased)
        if progress >= 1.0:
            widget._clarify_fade_job = None
            if on_complete is not None:
                on_complete()
            return
        widget._clarify_fade_job = widget.after(WINDOW_FADE_FRAME_MS, tick)

    tick()


def _fade_in_window(widget, show=None):
    _set_window_opacity(widget, 0.0)
    (show or widget.deiconify)()
    _animate_window_opacity(widget, 1.0, WINDOW_FADE_IN_MS)


def _fade_out_window(widget, on_complete):
    if getattr(widget, "_clarify_fading_out", False):
        return
    widget._clarify_fading_out = True

    def finish():
        try:
            on_complete()
        finally:
            try:
                if widget.winfo_exists():
                    _set_window_opacity(widget, 1.0)
                    widget._clarify_fading_out = False
            except tk.TclError:
                pass

    _animate_window_opacity(widget, 0.0, WINDOW_FADE_OUT_MS, finish)


def _draw_checkmark(draw, center_x, center_y, scale, progress, color):
    """Draw the rounded ClarifyVoice completion mark at any size or color."""
    progress = max(0.0, min(1.0, float(progress)))
    points = [
        (center_x - 9 * scale, center_y),
        (center_x - 2 * scale, center_y + 7 * scale),
        (center_x + 11 * scale, center_y - 8 * scale),
    ]
    first_leg = math.dist(points[0], points[1])
    second_leg = math.dist(points[1], points[2])
    distance = progress * (first_leg + second_leg)
    visible = [points[0]]
    if distance <= first_leg:
        ratio = distance / first_leg if first_leg else 1.0
        visible.append(tuple(
            points[0][axis] + (points[1][axis] - points[0][axis]) * ratio
            for axis in (0, 1)))
    else:
        visible.append(points[1])
        ratio = min(1.0, (distance - first_leg) / second_leg)
        visible.append(tuple(
            points[1][axis] + (points[2][axis] - points[1][axis]) * ratio
            for axis in (0, 1)))
    if len(visible) < 2:
        return
    width = max(1, round(2.7 * scale))
    radius = 1.35 * scale
    draw.line(visible, fill=color, width=width, joint="curve")
    for x, y in (visible[0], visible[-1]):
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius), fill=color)


def _make_checkmark_image(size=20, color=(5, 5, 5, 255)):
    """Render the pill's checkmark as a supersampled reusable button icon."""
    supersample = 4
    pixels = size * supersample
    image = Image.new("RGBA", (pixels, pixels), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    _draw_checkmark(
        draw, pixels / 2, pixels / 2,
        (size / 26) * supersample, 1.0, color)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def _pill_status_font(pixel_size):
    """Load a compact native-looking font for the layered pill."""
    if ImageFont is None:
        return None
    windir = Path(os.environ.get("WINDIR", "C:/Windows"))
    candidates = (
        windir / "Fonts" / "segoeui.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for path in candidates:
        try:
            return ImageFont.truetype(str(path), pixel_size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def _draw_microphone_unavailable(
        draw, width, height, scale, include_icon=True):
    """Draw the pill's red attention icon and concise microphone status."""
    label = "Mic off"
    font = _pill_status_font(round(14 * scale))
    text_box = draw.textbbox((0, 0), label, font=font)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    center_y = height / 2
    if include_icon:
        _draw_rounded_warning_icon(
            draw, 20 * scale, center_y, 14 * scale, scale)
        text_x = 37 * scale
    else:
        text_x = (width - text_width) / 2
    text_y = center_y - text_height / 2 - text_box[1]
    draw.text(
        (text_x, text_y), label, font=font,
        fill=(242, 242, 242, 255))


def _draw_rounded_warning_icon(draw, center_x, center_y, size, scale):
    """Draw a filled warning triangle with deliberately softened corners."""
    red = (255, 69, 58, 255)
    corner_radius = min(size * 0.09, 1.25 * scale)
    top = (center_x, center_y - size / 2 + corner_radius * 0.55)
    left = (
        center_x - size / 2 + corner_radius * 0.7,
        center_y + size / 2 - corner_radius * 0.65)
    right = (
        center_x + size / 2 - corner_radius * 0.7,
        center_y + size / 2 - corner_radius * 0.65)
    points = (top, left, right)
    draw.polygon(points, fill=red)
    draw.line(
        (*points, top), fill=red, width=max(1, round(corner_radius * 2)),
        joint="curve")

    mark_width = max(1.0 * scale, size * 0.09)
    draw.rounded_rectangle(
        (center_x - mark_width / 2, center_y - size * 0.21,
         center_x + mark_width / 2, center_y + size * 0.05),
        radius=mark_width / 2, fill=(18, 18, 18, 255))
    dot_radius = max(0.55 * scale, size * 0.045)
    dot_y = center_y + size * 0.25
    draw.ellipse(
        (center_x - dot_radius, dot_y - dot_radius,
         center_x + dot_radius, dot_y + dot_radius),
        fill=(18, 18, 18, 255))


def _make_microphone_warning_image(size=24):
    supersample = 4
    pixels = size * supersample
    image = Image.new("RGBA", (pixels, pixels), (0, 0, 0, 0))
    _draw_rounded_warning_icon(
        ImageDraw.Draw(image), pixels / 2, pixels / 2,
        (size - 2) * supersample, supersample)
    return image.resize((size, size), Image.Resampling.LANCZOS)


class LayeredRecordingOverlay:
    """Small Win32 status pill with true per-pixel alpha composition."""

    def __init__(self, x, y, width=142, height=42, icon=None,
            initial_opacity=255):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.width = width
        self.height = height
        self.scale = 3
        self.icon = icon

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class SIZE(ctypes.Structure):
            _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

        class BLENDFUNCTION(ctypes.Structure):
            _fields_ = [
                ("BlendOp", wintypes.BYTE), ("BlendFlags", wintypes.BYTE),
                ("SourceConstantAlpha", wintypes.BYTE), ("AlphaFormat", wintypes.BYTE),
            ]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

        self.POINT = POINT
        self.SIZE = SIZE
        self.BLENDFUNCTION = BLENDFUNCTION
        self.user32 = ctypes.windll.user32
        self.gdi32 = ctypes.windll.gdi32
        self.kernel32 = ctypes.windll.kernel32

        self.user32.CreateWindowExW.restype = wintypes.HWND
        self.user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.user32.GetDC.restype = wintypes.HDC
        self.user32.GetDC.argtypes = [wintypes.HWND]
        self.user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        self.user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.gdi32.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        self.gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
        self.gdi32.SelectObject.restype = wintypes.HANDLE
        self.gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        self.gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        self.gdi32.DeleteDC.argtypes = [wintypes.HDC]
        self.user32.UpdateLayeredWindow.restype = wintypes.BOOL
        self.user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
            wintypes.HDC, ctypes.POINTER(POINT), wintypes.COLORREF,
            ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]

        ex_style = 0x00080000 | 0x00000080 | 0x08000000 | 0x00000020  # + TRANSPARENT
        self.hwnd = self.user32.CreateWindowExW(
            ex_style, "STATIC", "", 0x80000000, x, y, width, height,
            None, None, self.kernel32.GetModuleHandleW(None), None)
        if not self.hwnd:
            raise ctypes.WinError()

        self.screen_dc = self.user32.GetDC(None)
        self.memory_dc = self.gdi32.CreateCompatibleDC(self.screen_dc)
        self.bits = ctypes.c_void_p()
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        self.bitmap = self.gdi32.CreateDIBSection(
            self.memory_dc, ctypes.byref(info), 0, ctypes.byref(self.bits), None, 0)
        self.old_bitmap = self.gdi32.SelectObject(self.memory_dc, self.bitmap)
        self.position = POINT(x, y)
        self.size = SIZE(width, height)
        self.source = POINT(0, 0)
        self.blend = BLENDFUNCTION(
            0, 0, max(0, min(255, round(initial_opacity))), 1)
        self._build_base()
        # The first state-specific frame is rendered by the existing animation
        # tick. Uploading only the base here also permits compact status pills
        # whose width is intentionally smaller than the recording waveform.
        self._upload(self.base)
        self.user32.SetWindowPos(
            self.hwnd, -1, x, y, width, height, 0x0010 | 0x0040)  # NOACTIVATE|SHOWWINDOW

    def _build_base(self):
        scale = self.scale
        width, height = self.width * scale, self.height * scale
        base = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(base)
        draw.rounded_rectangle(
            (1 * scale, 1 * scale, width - 1 * scale - 1, height - 1 * scale - 1),
            radius=(self.height / 2 - 1) * scale,
            fill=CARD, outline=BORDER, width=scale)
        self.plain_base = base.resize(
            (self.width, self.height), Image.Resampling.LANCZOS)
        if self.icon is not None:
            icon_size = 24 * scale
            icon = self.icon.resize((icon_size, icon_size), Image.Resampling.LANCZOS)
            base.alpha_composite(icon, (7 * scale, 9 * scale))
        self.base = base.resize((self.width, self.height), Image.Resampling.LANCZOS)

    def set_icon(self, icon):
        self.icon = icon
        self._build_base()

    def _wave_mask(self, level, timestamp, collapse=0.0):
        scale = self.scale
        wave_width = 96
        mask_width = wave_width * scale
        mask_height = self.height * scale
        mask = Image.new("L", (mask_width, mask_height), 0)
        draw = ImageDraw.Draw(mask)
        mid = mask_height / 2
        count = 12
        bar_width = 2.8 * scale

        for index in range(count):
            position = index / (count - 1)
            envelope = 0.58 + 0.42 * math.sin(position * math.pi)
            motion = 0.12 * math.sin(timestamp * 5.2 + index * 0.82)
            amplitude = max(0.22, min(0.98, (level * 1.45 + 0.24 + motion) * envelope))
            half_height = max(3.2 * scale, (self.height / 2 - 8) * scale * amplitude)
            half_height = max(0.7 * scale, half_height * (1.0 - collapse))
            x = (index + 0.5) * (mask_width / count)
            draw.rounded_rectangle(
                (x - bar_width / 2, mid - half_height,
                 x + bar_width / 2, mid + half_height),
                radius=bar_width / 2, fill=255)

        mask = mask.resize((wave_width, self.height), Image.Resampling.BICUBIC)
        return mask

    def render(self, level, timestamp):
        frame = self.base.copy()
        mask = self._wave_mask(level, timestamp)
        frame.paste((255, 255, 255, 255), (38, 0, 134, self.height), mask)

        self._upload(frame)

    def render_processing(self, level, timestamp, transition=1.0):
        """Collapse the waveform into a quiet indeterminate progress line."""
        scale = self.scale
        transition = max(0.0, min(1.0, transition))
        eased = 1.0 - (1.0 - transition) ** 3
        frame = self.base.copy()

        if transition < 1.0:
            wave_mask = self._wave_mask(level, timestamp, collapse=eased)
            wave_mask = wave_mask.point(lambda alpha: round(alpha * (1.0 - eased)))
            frame.paste((255, 255, 255, 255), (38, 0, 134, self.height), wave_mask)

        left, right = 45 * scale, 127 * scale
        center_y = self.height * scale / 2
        line_width = (right - left) * (0.28 + 0.72 * eased)
        line_left = (left + right - line_width) / 2
        layer = Image.new("RGBA", (self.width * scale, self.height * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        track_alpha = round(155 * eased)
        draw.rounded_rectangle(
            (line_left, center_y - scale, line_left + line_width, center_y + scale),
            radius=scale, fill=(76, 76, 76, track_alpha))

        # A soft segment glides across the track; its sine-based position turns
        # around smoothly instead of jumping back to the beginning.
        travel = (math.sin(timestamp * 3.2 - math.pi / 2) + 1.0) / 2.0
        segment_width = line_width * 0.32
        segment_left = line_left + (line_width - segment_width) * travel
        draw.rounded_rectangle(
            (segment_left, center_y - scale, segment_left + segment_width, center_y + scale),
            radius=scale, fill=(245, 245, 245, round(245 * eased)))
        layer = layer.resize((self.width, self.height), Image.Resampling.LANCZOS)
        frame.alpha_composite(layer)

        self._upload(frame)

    def render_success(self, transition=1.0):
        """Fade the progress line into a progressively drawn green check."""
        scale = self.scale
        transition = max(0.0, min(1.0, transition))
        eased = 1.0 - (1.0 - transition) ** 3
        frame = self.base.copy()
        layer = Image.new("RGBA", (self.width * scale, self.height * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        center_x, center_y = 86 * scale, self.height * scale / 2

        if eased < 1.0:
            half_width = 41 * scale * (1.0 - eased)
            draw.rounded_rectangle(
                (center_x - half_width, center_y - scale,
                 center_x + half_width, center_y + scale),
                radius=scale, fill=(100, 100, 100, round(120 * (1.0 - eased))))

        _draw_checkmark(
            draw, center_x, center_y, scale, eased, (48, 209, 88, 255))
        layer = layer.resize((self.width, self.height), Image.Resampling.LANCZOS)
        frame.alpha_composite(layer)

        self._upload(frame)

    def render_microphone_unavailable(self):
        """Replace the waveform with a static alert, preserving pill layout."""
        scale = self.scale
        frame = self.plain_base.copy()
        layer = Image.new(
            "RGBA", (self.width * scale, self.height * scale), (0, 0, 0, 0))
        _draw_microphone_unavailable(
            ImageDraw.Draw(layer), self.width * scale,
            self.height * scale, scale)
        layer = layer.resize(
            (self.width, self.height), Image.Resampling.LANCZOS)
        frame.alpha_composite(layer)
        self._upload(frame)

    def _upload(self, frame):
        # UpdateLayeredWindow expects premultiplied BGRA. ImageChops performs
        # this in Pillow's C implementation instead of a per-pixel Python loop.
        red, green, blue, alpha = frame.split()
        premultiplied = Image.merge("RGBA", (
            ImageChops.multiply(blue, alpha),
            ImageChops.multiply(green, alpha),
            ImageChops.multiply(red, alpha),
            alpha,
        )).tobytes()
        self.ctypes.memmove(self.bits, premultiplied, len(premultiplied))
        self._present()

    def _present(self):
        self.user32.UpdateLayeredWindow(
            self.hwnd, self.screen_dc, self.ctypes.byref(self.position),
            self.ctypes.byref(self.size), self.memory_dc,
            self.ctypes.byref(self.source), 0,
            self.ctypes.byref(self.blend), 0x00000002)

    def set_opacity(self, opacity):
        """Change only the global alpha, reusing the already uploaded frame."""
        if not getattr(self, "hwnd", None):
            return
        self.blend.SourceConstantAlpha = max(
            0, min(255, round(float(opacity) * 255)))
        self._present()

    def place_behind(self, target_hwnd, x, y):
        self.position.x = x
        self.position.y = y
        self.user32.SetWindowPos(
            self.hwnd, target_hwnd, x, y, self.width, self.height,
            0x0010 | 0x0040)  # NOACTIVATE|SHOWWINDOW

    def hide(self):
        if getattr(self, "hwnd", None):
            self.user32.ShowWindow(self.hwnd, 0)

    def destroy(self):
        if not getattr(self, "hwnd", None):
            return
        self.gdi32.SelectObject(self.memory_dc, self.old_bitmap)
        self.gdi32.DeleteObject(self.bitmap)
        self.gdi32.DeleteDC(self.memory_dc)
        self.user32.ReleaseDC(None, self.screen_dc)
        self.user32.DestroyWindow(self.hwnd)
        self.hwnd = None


class LayeredBackdropSurface(LayeredRecordingOverlay):
    """Static alpha-composited rounded background for an interactive Tk window."""

    def __init__(self, x, y, width, height, radius):
        self.radius = radius
        super().__init__(x, y, width, height, icon=None, initial_opacity=255)

    def _build_base(self):
        scale = self.scale
        width, height = self.width * scale, self.height * scale
        base = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(base)
        draw.rounded_rectangle(
            (scale, scale, width - scale - 1, height - scale - 1),
            radius=min(self.radius, self.height / 2 - 1) * scale,
            fill=CARD, outline=BORDER, width=scale)
        self.base = base.resize((self.width, self.height), Image.Resampling.LANCZOS)

    def render(self, _level=0.0, _timestamp=0.0):
        self._upload(self.base)


class SmoothTkBackdrop:
    """Keep a native layered surface aligned directly behind a Tk toplevel."""

    def __init__(self, widget, radius):
        self.widget = widget
        self.radius = radius
        self.surface = None
        self._sync_job = None
        self.opacity = _window_opacity(widget)
        widget.bind("<Configure>", self._schedule_sync, add="+")
        widget.bind("<Map>", self._schedule_sync, add="+")
        widget.bind("<Unmap>", self._hide, add="+")
        widget.bind("<Destroy>", self._destroy, add="+")
        widget.after_idle(self._sync)

    def _schedule_sync(self, event=None):
        if event is not None and event.widget is not self.widget:
            return
        if self._sync_job is None:
            self._sync_job = self.widget.after_idle(self._sync)

    def _target_hwnd(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        return user32.GetParent(self.widget.winfo_id()) or self.widget.winfo_id()

    def _sync(self):
        self._sync_job = None
        try:
            if not self.widget.winfo_exists() or not self.widget.winfo_viewable():
                self._hide()
                return
            self.widget.update_idletasks()
            x, y = self.widget.winfo_x(), self.widget.winfo_y()
            width, height = self.widget.winfo_width(), self.widget.winfo_height()
            if width <= 1 or height <= 1:
                return
            dpi_scale = self.widget.winfo_fpixels("1i") / 96.0
            physical_radius = self.radius * dpi_scale
            if (self.surface is None or self.surface.width != width
                    or self.surface.height != height):
                if self.surface is not None:
                    self.surface.destroy()
                self.surface = LayeredBackdropSurface(
                    x, y, width, height, min(physical_radius, height / 2))
                self.surface.set_opacity(self.opacity)
            self.surface.place_behind(self._target_hwnd(), x, y)
        except Exception:
            self._destroy()

    def set_opacity(self, opacity):
        self.opacity = max(0.0, min(1.0, float(opacity)))
        if self.surface is not None:
            self.surface.set_opacity(self.opacity)

    def _hide(self, event=None):
        if event is not None and event.widget is not self.widget:
            return
        if self.surface is not None:
            self.surface.hide()

    def _destroy(self, event=None):
        if event is not None and event.widget is not self.widget:
            return
        if self.surface is not None:
            self.surface.destroy()
            self.surface = None

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class App(ctk.CTk):
    def __init__(self, start_hidden=False):
        super().__init__()
        if start_hidden:
            self.withdraw()
        self.title("ClarifyVoice")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(fg_color=TRANSPARENT)
        if IS_WIN:
            self.attributes("-transparentcolor", TRANSPARENT)

        self.recorder = Recorder()
        self.app_state = "ready"
        self.mode = str(APP_CONFIG.get("ui_mode", "prompt"))
        self.lang = str(APP_CONFIG.get("ui_language", "en"))
        self.result_text = ""
        self._rewrite_active = False
        self._wave_running = False
        self._timer_running = False
        self._drag_x = 0
        self._drag_y = 0
        self._saved_pos = None
        self._was_hidden_before_recording = False
        self._app_icon_cache = {}
        self._focused_executable = None
        self._focused_icon = None
        self._focused_icon_image = None
        self._recording_overlay = None
        self._display_level = 0.0
        self._last_wave_time = 0.0
        self._next_wave_frame = 0.0
        self._pill_transition_started = 0.0
        self._pill_fade_started = 0.0
        self._pill_pending_ready = None
        self._success_job = None
        self._microphone_alert_job = None

        sw = self.winfo_screenwidth()
        self.geometry(f"380x48+{sw - 400}+16")

        self._build_ui()
        self._configure_overlay_window()
        self._main_backdrop = SmoothTkBackdrop(self, 24) if IS_WIN else None
        self.bind("<Escape>", self._on_escape)
        keyboard.add_hotkey("alt+l", self._recording_hotkey)
        keyboard.add_hotkey("alt+k", self._rewrite_hotkey, suppress=IS_WIN)
        keyboard.add_hotkey("alt+r", lambda: self.after(0, self._toggle_visibility))
        keyboard.add_hotkey("escape", lambda: self.after(0, self._on_escape))
        if start_hidden:
            self.withdraw()

    def _configure_overlay_window(self):
        """Keep ClarifyVoice out of Alt+Tab and prevent it stealing focus."""
        if not IS_WIN:
            return
        try:
            import ctypes
            from ctypes import wintypes
            self.update_idletasks()
            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            user32.SetWindowLongW.restype = ctypes.c_long
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user32.SetWindowPos.restype = wintypes.BOOL
            hwnd = user32.GetParent(self.winfo_id()) or self.winfo_id()
            ex_style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            ex_style |= 0x00000080   # WS_EX_TOOLWINDOW
            ex_style |= 0x08000000   # WS_EX_NOACTIVATE
            ex_style &= ~0x00040000  # WS_EX_APPWINDOW
            user32.SetWindowLongW(hwnd, -20, ex_style)
            user32.SetWindowPos(
                hwnd, -1, 0, 0, 0, 0,
                0x0001 | 0x0002 | 0x0010 | 0x0020)  # NOSIZE|NOMOVE|NOACTIVATE|FRAMECHANGED
            self._overlay_hwnd = hwnd
        except Exception:
            self._overlay_hwnd = None

    def _show_without_activation(self):
        self.deiconify()
        self.attributes("-topmost", True)
        if IS_WIN and getattr(self, "_overlay_hwnd", None):
            try:
                import ctypes
                ctypes.windll.user32.SetWindowPos(
                    self._overlay_hwnd, -1, 0, 0, 0, 0,
                    0x0001 | 0x0002 | 0x0010 | 0x0040)  # NOSIZE|NOMOVE|NOACTIVATE|SHOWWINDOW
            except Exception:
                pass

    def _quit_with_fade(self):
        self.quit()

    def _show_if_hidden(self):
        """Reveal an Alt+R-hidden app when another launch requests activation."""
        if self._recording_overlay is None and not self.winfo_viewable():
            self._show_without_activation()

    def _build_ui(self):
        # === IDLE CARD ===
        self._idle_card_pad = 0 if IS_WIN else 2
        self.idle_card = ctk.CTkFrame(
            self, fg_color="transparent" if IS_WIN else CARD,
            corner_radius=24, border_width=0 if IS_WIN else 1,
            border_color=BORDER)
        self.idle_card.pack(
            fill="both", expand=True, padx=self._idle_card_pad,
            pady=self._idle_card_pad)

        bar = ctk.CTkFrame(self.idle_card, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=10)
        self._idle_bar = bar
        self._make_draggable(bar)

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        self._make_draggable(left)

        self.lbl = ctk.CTkLabel(left, text=self._t("ready"), text_color=TEXT,
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
        self.lang_btn = ctk.CTkButton(right, text="",
            image=self._flag_br if self.lang == "pt" else self._flag_en,
            width=32, height=26, corner_radius=13,
            fg_color="#151515", hover_color="#222222", command=self._toggle_lang)
        self.lang_btn.pack(side="left", padx=(0, 4))

        self.mode_btn = ctk.CTkButton(right,
            text=self._t("transcribe") if self.mode == "transcription" else self._t("prompt"),
            width=62, height=26, corner_radius=13,
            fg_color="#151515", hover_color="#222222", text_color=DIM,
            font=ctk.CTkFont(size=11), command=self._toggle_mode)
        self.mode_btn.pack(side="left", padx=(0, 4))

        self.gear_btn = ctk.CTkButton(right, text="\u2630", width=26, height=26, corner_radius=13,
            fg_color="transparent", hover_color="#151515", text_color="#444444",
            font=ctk.CTkFont(size=12), command=self._open_settings)
        self.gear_btn.pack(side="left", padx=(0, 2))

        self.close_btn = ctk.CTkButton(right, text="\u2014", width=26, height=26, corner_radius=13,
            fg_color="transparent", hover_color="#151515", text_color="#444444",
            font=ctk.CTkFont(size=10), command=self._quit_with_fade)
        self.close_btn.pack(side="left")

        # Result panel (inside idle card, hidden by default)
        # CTkFrame defaults to 200px tall; keep this container content-sized.
        self.result_frame = ctk.CTkFrame(
            self.idle_card, fg_color="transparent", height=1)

        self.result_box = ctk.CTkTextbox(self.result_frame, fg_color="#050505", text_color="#cccccc",
            font=ctk.CTkFont(size=12), corner_radius=10, border_width=1, border_color=BORDER,
            wrap="word", height=38)
        self.result_box.pack(fill="x", padx=14, pady=(0, 6))

        brow = ctk.CTkFrame(self.result_frame, fg_color="transparent")
        brow.pack(fill="x", padx=14, pady=(0, 10))

        self.copy_btn = ctk.CTkButton(brow, text="Copy", width=52, height=26, corner_radius=13,
            fg_color="#151515", hover_color="#222222", text_color=WHITE,
            font=ctk.CTkFont(size=11), command=self._copy)
        self.copy_btn.pack(side="left", padx=(0, 4))

        self.dismiss_btn = ctk.CTkButton(brow, text="Dismiss", width=56, height=26, corner_radius=13,
            fg_color="transparent", hover_color="#151515", text_color=DIM,
            font=ctk.CTkFont(size=11), command=self._hide_result)
        self.dismiss_btn.pack(side="left")

        # === RECORDING CARD (hidden by default) ===
        self.rec_card = ctk.CTkFrame(self, fg_color=CARD, corner_radius=20,
            border_width=1, border_color=BORDER)

        rec_inner = ctk.CTkFrame(self.rec_card, fg_color="transparent")
        rec_inner.pack(expand=True, padx=2, pady=4)

        # Icon of the application receiving the recording.
        fallback = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        fallback_draw = ImageDraw.Draw(fallback)
        fallback_draw.rounded_rectangle((3, 5, 29, 27), radius=5,
            fill="#202020", outline="#777777", width=2)
        fallback_draw.line((4, 11, 28, 11), fill="#777777", width=2)
        self._fallback_app_icon_image = fallback
        self._fallback_app_icon = ctk.CTkImage(
            light_image=fallback, dark_image=fallback, size=(22, 22))
        microphone_warning = _make_microphone_warning_image(24)
        self._microphone_warning_icon = ctk.CTkImage(
            light_image=microphone_warning, dark_image=microphone_warning,
            size=(24, 24))
        self.app_icon_lbl = ctk.CTkLabel(rec_inner, text="", width=24, height=24,
            image=self._fallback_app_icon)
        self.app_icon_lbl.pack(side="left", padx=(0, 6))

        # Waveform — fixed-width canvas
        W_W, W_H = 104, 28
        self.wave_cv = tk.Canvas(rec_inner, width=W_W, height=W_H, bg=CARD,
            highlightthickness=0, bd=0)
        self.wave_cv.pack(side="left")
        self._wave_w = W_W
        self._wave_h = W_H
        self._wave_n = 12
        self._wave_gap = W_W / self._wave_n
        self._wave_mid = W_H // 2
        self._wave_image = None
        self._wave_image_id = self.wave_cv.create_image(0, 0, anchor="nw")

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
    def _set_state(self, s, t="", after_ready=None, _skip_pill_fade=False):
        if s != "microphone_unavailable" and getattr(
                self, "_microphone_alert_job", None) is not None:
            try:
                self.after_cancel(self._microphone_alert_job)
            except tk.TclError:
                pass
            self._microphone_alert_job = None
        if (s == "ready" and not _skip_pill_fade
                and self.app_state in (
                    "recording", "processing", "rewriting", "success",
                    "microphone_unavailable")
                and self._wave_running):
            self._pill_pending_ready = (t, after_ready)
            self.app_state = "dismissing"
            self._timer_running = False
            self._pill_transition_started = time.perf_counter()
            return

        had_overlay = self._recording_overlay is not None
        self.app_state = s
        if s == "ready":
            self._wave_running = False
            self._timer_running = False
            if had_overlay:
                self._recording_overlay.destroy()
                self._recording_overlay = None
            try:
                self.attributes("-alpha", 1.0)
            except tk.TclError:
                pass
            # Switch to idle card
            self.rec_card.pack_forget()
            if hasattr(self, "app_icon_lbl"):
                self.app_icon_lbl.configure(image=(
                    getattr(self, "_focused_icon", None)
                    or self._fallback_app_icon))
            self.idle_card.pack(
                fill="both", expand=True, padx=self._idle_card_pad,
                pady=self._idle_card_pad)
            self.lbl.configure(text=t or self._t("ready"), text_color=TEXT)
            self.sub.configure(text=self._t("hint"))
            # Restore position
            if self._saved_pos:
                x, y = self._saved_pos
                self._saved_pos = None
            else:
                x, y = self.winfo_x(), self.winfo_y()
            self.geometry(f"380x48+{x}+{y}")
            if self._was_hidden_before_recording:
                self.withdraw()
                self._was_hidden_before_recording = False
            elif had_overlay:
                self._show_without_activation()
            if after_ready is not None:
                after_ready()
        elif s in (
                "recording", "processing", "rewriting", "success",
                "microphone_unavailable"):
            starting_pill = not self._wave_running
            if self._saved_pos is None:
                self._saved_pos = (self.winfo_x(), self.winfo_y())
            rw = MICROPHONE_PILL_WIDTH if (
                s == "microphone_unavailable" and IS_WIN) else 142
            rh = 42
            if self._primary_mon:
                sw, sh = self._primary_mon
            else:
                sw = self.winfo_screenwidth()
                sh = self.winfo_screenheight()
            rx = (sw - rw) // 2
            ry = sh - rh - 80
            if IS_WIN:
                self.withdraw()
                if (self._recording_overlay is not None
                        and self._recording_overlay.width != rw):
                    self._recording_overlay.destroy()
                    self._recording_overlay = None
                if self._recording_overlay is None:
                    try:
                        self._recording_overlay = LayeredRecordingOverlay(
                            rx, ry, rw, rh,
                            self._focused_icon_image or self._fallback_app_icon_image,
                            initial_opacity=0)
                        self._pill_fade_started = time.perf_counter()
                    except Exception:
                        self._recording_overlay = None
                        # The Tk fallback still contains the fixed-width wave
                        # canvas, so retain its original width if native layered
                        # window creation is unavailable.
                        fallback_width = 142
                        fallback_x = (sw - fallback_width) // 2
                        self.geometry(
                            f"{fallback_width}x{rh}+{fallback_x}+{ry}")
                        self.idle_card.pack_forget()
                        self.rec_card.pack(fill="both", expand=True, padx=2, pady=2)
                        self._show_without_activation()
                        if starting_pill:
                            self.attributes("-alpha", 0.0)
                            self._pill_fade_started = time.perf_counter()
            else:
                self.geometry(f"{rw}x{rh}+{rx}+{ry}")
                self.idle_card.pack_forget()
                self.rec_card.pack(fill="both", expand=True, padx=2, pady=2)
                if starting_pill:
                    try:
                        self.attributes("-alpha", 0.0)
                    except tk.TclError:
                        pass
                    self._pill_fade_started = time.perf_counter()
            now = time.perf_counter()
            if s == "recording":
                self._display_level = 0.0
                self._timer_running = True
                self._focused_icon_tick()
            else:
                self._timer_running = False
            if s == "microphone_unavailable":
                if hasattr(self, "app_icon_lbl"):
                    self.app_icon_lbl.configure(
                        image=self._microphone_warning_icon)
                # A recorder failure can arrive after the recording pill has
                # already appeared. Restart alpha from zero so the error itself
                # always gets a clean fade in.
                self._set_pill_opacity(0.0)
                self._pill_fade_started = now
                if self._microphone_alert_job is not None:
                    try:
                        self.after_cancel(self._microphone_alert_job)
                    except tk.TclError:
                        pass
                visible_ms = round(max(
                    0.0, MICROPHONE_ALERT_SECONDS
                    - PILL_FADE_OUT_SECONDS) * 1000)
                self._microphone_alert_job = self.after(
                    visible_ms, self._dismiss_microphone_alert)
            else:
                if hasattr(self, "app_icon_lbl"):
                    self.app_icon_lbl.configure(image=(
                        getattr(self, "_focused_icon", None)
                        or self._fallback_app_icon))
            self._pill_transition_started = now
            self._last_wave_time = now
            self._next_wave_frame = self._last_wave_time
            if not self._wave_running:
                self._wave_running = True
                self._wave_tick()

    def _set_pill_opacity(self, opacity):
        opacity = max(0.0, min(1.0, opacity))
        if self._recording_overlay is not None:
            self._recording_overlay.set_opacity(opacity)
        else:
            try:
                self.attributes("-alpha", opacity)
            except tk.TclError:
                pass

    def _finish_pill_dismissal(self):
        pending = self._pill_pending_ready or ("", None)
        self._pill_pending_ready = None
        self._set_state(
            "ready", pending[0], after_ready=pending[1],
            _skip_pill_fade=True)

    def _dismiss_microphone_alert(self):
        self._microphone_alert_job = None
        if self.app_state == "microphone_unavailable":
            self._set_state("ready")

    def _show_success_then(self, callback, delay=850):
        """Show a completed check long enough to register before restoring UI."""
        self._set_state("success")
        if self._success_job is not None:
            try:
                self.after_cancel(self._success_job)
            except tk.TclError:
                pass
        def finish():
            self._success_job = None
            callback()
        self._success_job = self.after(delay, finish)

    # -- Wave --
    def _wave_tick(self):
        if not self._wave_running: return
        now = time.perf_counter()

        if self.app_state == "dismissing":
            progress = min(
                1.0, (now - self._pill_transition_started)
                / PILL_FADE_OUT_SECONDS)
            eased = progress * progress * (3.0 - 2.0 * progress)
            self._set_pill_opacity(1.0 - eased)
            if progress >= 1.0:
                self._finish_pill_dismissal()
                return
            self.after(16, self._wave_tick)
            return

        if self._pill_fade_started:
            progress = min(
                1.0, (now - self._pill_fade_started)
                / PILL_FADE_IN_SECONDS)
            eased = progress * progress * (3.0 - 2.0 * progress)
            self._set_pill_opacity(eased)
            if progress >= 1.0:
                self._pill_fade_started = 0.0

        if self._recording_overlay is not None:
            if self.app_state == "recording":
                elapsed = max(0.001, min(0.1, now - self._last_wave_time))
                self._last_wave_time = now
                target = self.recorder.mic_level
                time_constant = 0.035 if target > self._display_level else 0.14
                blend = 1.0 - math.exp(-elapsed / time_constant)
                self._display_level += (target - self._display_level) * blend
                self._recording_overlay.render(self._display_level, now)
            elif self.app_state in ("processing", "rewriting"):
                transition = (now - self._pill_transition_started) / 0.28
                self._recording_overlay.render_processing(
                    self._display_level, now, transition)
            elif self.app_state == "success":
                transition = (now - self._pill_transition_started) / 0.32
                self._recording_overlay.render_success(transition)
            elif self.app_state == "microphone_unavailable":
                self._recording_overlay.render_microphone_unavailable()

            frame_time = 1.0 / 60.0
            self._next_wave_frame += frame_time
            if self._next_wave_frame < now:
                self._next_wave_frame = now + frame_time
            delay = max(1, round((self._next_wave_frame - time.perf_counter()) * 1000))
            self.after(delay, self._wave_tick)
            return

        if self.app_state in (
                "processing", "rewriting", "success",
                "microphone_unavailable"):
            self._render_tk_status()
            self.after(33, self._wave_tick)
            return

        lv = self.recorder.mic_level; t = time.time()
        scale = 4
        width, height = self._wave_w * scale, self._wave_h * scale
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        mid = height / 2
        bar_w = 3.2 * scale

        for i in range(self._wave_n):
            # A soft center-weighted envelope keeps the waveform composed even
            # when the microphone level is nearly constant.
            position = i / (self._wave_n - 1)
            envelope = 0.58 + 0.42 * math.sin(position * math.pi)
            motion = 0.13 * math.sin(t * 5.0 + i * 0.82)
            amp = max(0.22, min(0.96, (lv * 1.35 + 0.24 + motion) * envelope))
            half_h = max(3.2 * scale, (height / 2 - 2 * scale) * amp)
            x = (i + 0.5) * (width / self._wave_n)
            box = tuple(round(value) for value in (
                x - bar_w / 2, mid - half_h,
                x + bar_w / 2, mid + half_h))
            draw.rounded_rectangle(box, radius=round(bar_w / 2), fill=255)

        # Bicubic avoids the bright/dark ringing that Lanczos creates around
        # tiny high-contrast bars. Compositing the mask onto CARD ensures all
        # antialias pixels blend into the pill instead of forming a halo.
        mask = mask.resize(
            (self._wave_w, self._wave_h), Image.Resampling.BICUBIC)
        frame = Image.composite(
            Image.new("RGB", (self._wave_w, self._wave_h), WHITE),
            Image.new("RGB", (self._wave_w, self._wave_h), CARD),
            mask)
        self._wave_image = ImageTk.PhotoImage(frame)
        self.wave_cv.itemconfigure(self._wave_image_id, image=self._wave_image)
        self.after(33, self._wave_tick)

    def _render_tk_status(self):
        """Antialiased fallback for platforms without the layered overlay."""
        scale = 4
        width, height = self._wave_w * scale, self._wave_h * scale
        frame = Image.new("RGB", (width, height), CARD)
        draw = ImageDraw.Draw(frame)
        now = time.perf_counter()
        transition = max(0.0, min(1.0, (now - self._pill_transition_started) / 0.3))
        eased = 1.0 - (1.0 - transition) ** 3
        cy = height / 2

        if self.app_state == "microphone_unavailable":
            _draw_microphone_unavailable(
                draw, width, height, scale, include_icon=False)
        elif self.app_state in ("processing", "rewriting"):
            left, right = 7 * scale, (self._wave_w - 7) * scale
            line_width = (right - left) * (0.28 + 0.72 * eased)
            line_left = (left + right - line_width) / 2
            draw.rounded_rectangle(
                (line_left, cy - scale, line_left + line_width, cy + scale),
                radius=scale, fill="#4c4c4c")
            travel = (math.sin(now * 3.2 - math.pi / 2) + 1.0) / 2.0
            segment_width = line_width * 0.32
            segment_left = line_left + (line_width - segment_width) * travel
            draw.rounded_rectangle(
                (segment_left, cy - scale, segment_left + segment_width, cy + scale),
                radius=scale, fill="#f5f5f5")
        else:
            cx = width / 2
            _draw_checkmark(draw, cx, cy, scale, eased, "#30d158")

        frame = frame.resize(
            (self._wave_w, self._wave_h), Image.Resampling.LANCZOS)
        self._wave_image = ImageTk.PhotoImage(frame)
        self.wave_cv.itemconfigure(self._wave_image_id, image=self._wave_image)

    # -- Focused application icon --
    def _update_focused_icon(self, executable=None):
        executable = executable or _foreground_executable()
        if not executable or executable == self._focused_executable:
            return

        image = self._app_icon_cache.get(executable)
        if image is None:
            image = _executable_icon(executable)
            if image is not None:
                if len(self._app_icon_cache) >= 16:
                    self._app_icon_cache.pop(next(iter(self._app_icon_cache)))
                self._app_icon_cache[executable] = image
        # Only lock onto an executable after successful extraction, so a
        # transient shell/API failure is retried on the next lightweight poll.
        if image is not None:
            self._focused_executable = executable
        self._focused_icon_image = image or self._fallback_app_icon_image
        if self._recording_overlay is not None:
            self._recording_overlay.set_icon(self._focused_icon_image)
        else:
            self._focused_icon = ctk.CTkImage(
                light_image=self._focused_icon_image,
                dark_image=self._focused_icon_image, size=(22, 22))
            self.app_icon_lbl.configure(image=self._focused_icon)

    def _focused_icon_tick(self):
        if not self._timer_running: return
        self._update_focused_icon()
        # Tk owns this polling callback, avoiding unsafe cross-thread UI calls.
        # A foreground-window query at 10 Hz has negligible CPU/memory cost.
        self.after(100, self._focused_icon_tick)

    # -- Actions --
    def _toggle_mode(self):
        self.mode = "transcription" if self.mode == "prompt" else "prompt"
        self.mode_btn.configure(text=self._t("transcribe") if self.mode == "transcription" else self._t("prompt"))
        self._save_ui_preferences()

    def _t(self, key):
        return STRINGS.get(self.lang, STRINGS["en"]).get(key, key)

    def _toggle_lang(self):
        self.lang = "pt" if self.lang == "en" else "en"
        self.lang_btn.configure(image=self._flag_br if self.lang == "pt" else self._flag_en)
        self._refresh_ui_text()
        self._save_ui_preferences()

    def _save_ui_preferences(self):
        APP_CONFIG["ui_mode"] = self.mode
        APP_CONFIG["ui_language"] = self.lang
        try:
            _save_app_config()
        except OSError:
            pass

    def _refresh_ui_text(self):
        self.mode_btn.configure(text=self._t("transcribe") if self.mode == "transcription" else self._t("prompt"))
        self.copy_btn.configure(text=self._t("copy"))
        self.dismiss_btn.configure(text=self._t("dismiss"))
        if self.app_state == "ready":
            if self.lbl.cget("text") not in ("", ):
                self.lbl.configure(text=self._t("ready"))
            self.sub.configure(text=self._t("hint"))
        elif self.app_state in ("processing", "rewriting"):
            self.lbl.configure(text=self._t(self.app_state))
        elif self.app_state == "recording":
            self.sub.configure(text=self._t("hint_stop"))

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
            self.copy_btn.configure(text=self._t("copied"))
            self.after(1200, lambda: self.copy_btn.configure(text=self._t("copy")))

    def _hide_result(self):
        self.result_frame.pack_forget()
        x, y = self.winfo_x(), self.winfo_y()
        self.geometry(f"380x48+{x}+{y}")

    def _show_result(self, text):
        text = str(text).strip()
        self.result_text = text
        self.result_box.configure(state="normal")
        self.result_box.delete("0.0", "end")
        self.result_box.insert("0.0", text)
        self.result_box.configure(state="disabled")
        self.result_box.configure(height=38)
        self.result_frame.pack(fill="x")
        x, y = self.winfo_x(), self.winfo_y()
        self.geometry(f"400x48+{x}+{y}")
        self.update_idletasks()
        display_lines = None
        textbox = getattr(self.result_box, "_textbox", None)
        if textbox is not None:
            try:
                count = textbox.count("1.0", "end-1c", "displaylines")
                display_lines = count[0] if count else None
            except (tk.TclError, TypeError):
                pass
        text_height = _result_text_height(text, display_lines)
        self.result_box.configure(height=text_height)
        self.update_idletasks()
        # Do not query CTkFrame.winfo_reqheight(): its 200px default leaks into
        # layout measurement even though the visible buttons are 26px tall.
        result_content_height = text_height + 26 + 16
        h = _result_window_height(
            self._idle_bar.winfo_reqheight(), result_content_height)
        self.geometry(f"400x{h}+{x}+{y}")

    # -- Selected-text rewrite --
    def _rewrite_hotkey(self):
        if not IS_WIN or self.app_state != "ready" or self._rewrite_active:
            return
        target_window = _foreground_window_handle()
        if not target_window:
            return
        self._rewrite_target_executable = _foreground_executable()
        self._rewrite_active = True
        self.after(0, self._begin_rewrite_feedback)
        threading.Thread(
            target=self._rewrite_selection_worker,
            args=(target_window,), daemon=True).start()

    def _begin_rewrite_feedback(self):
        if self.result_frame.winfo_manager():
            self._hide_result()
        self._update_focused_icon(
            getattr(self, "_rewrite_target_executable", None))
        self._was_hidden_before_recording = not self.winfo_viewable()
        self._set_state("rewriting")

    def _finish_rewrite(self, text=None, status_key=None):
        def restore_result():
            self._rewrite_active = False
            if text:
                self._show_result(text)

        def finish():
            self._set_state(
                "ready", self._t(status_key) if status_key else "",
                after_ready=restore_result)

        if text:
            self._show_success_then(finish)
        else:
            finish()

    @staticmethod
    def _restore_clipboard_text(previous_text):
        if previous_text is None:
            return
        try:
            _set_windows_clipboard_text(previous_text)
        except OSError:
            pass

    def _rewrite_selection_worker(self, target_window):
        previous_clipboard = None
        try:
            try:
                previous_clipboard = _get_windows_clipboard_text()
            except OSError:
                previous_clipboard = None

            release_deadline = time.monotonic() + 0.8
            while keyboard.is_pressed("alt") and time.monotonic() < release_deadline:
                time.sleep(0.01)
            if keyboard.is_pressed("alt"):
                self.after(0, lambda: self._finish_rewrite(status_key="no_selection"))
                return

            selected_text = _copy_selected_text()
            if not selected_text or not selected_text.strip():
                self._restore_clipboard_text(previous_clipboard)
                self.after(0, lambda: self._finish_rewrite(status_key="no_selection"))
                return

            rewritten = rewrite_selected_text(selected_text)
            if not rewritten or rewritten.startswith("[Error"):
                self._restore_clipboard_text(previous_clipboard)
                self.after(0, lambda: self._finish_rewrite(status_key="rewrite_failed"))
                return

            try:
                _record_usage_event(_build_rewrite_usage_event(
                    str(APP_CONFIG.get("refinement_provider", "")),
                    str(APP_CONFIG.get("refinement_model", "")),
                    selected_text, rewritten))
            except OSError:
                pass

            selection_is_safe = _foreground_window_handle() == target_window
            if selection_is_safe:
                current_selection = _copy_selected_text()
                selection_is_safe = (
                    current_selection is not None
                    and _same_selected_text(current_selection, selected_text))

            _set_windows_clipboard_text(rewritten)
            if selection_is_safe and _foreground_window_handle() == target_window:
                time.sleep(0.04)
                _send_key_chord("ctrl+v")
                self.after(0, lambda: self._finish_rewrite(text=rewritten))
            else:
                self.after(0, lambda: self._finish_rewrite(
                    text=rewritten, status_key="rewrite_copied"))
        except Exception:
            self._restore_clipboard_text(previous_clipboard)
            self.after(0, lambda: self._finish_rewrite(status_key="rewrite_failed"))

    # -- Recording --
    def _recording_hotkey(self):
        if self._rewrite_active:
            return
        # Capture the target synchronously, before Tk can take foreground focus.
        target = _foreground_executable() if self.app_state != "recording" else None
        self.after(0, lambda: self.toggle_recording(target))

    def toggle_recording(self, target_executable=None):
        if self._rewrite_active:
            return
        if self.app_state == "recording": self._stop_recording()
        elif self.app_state == "microphone_unavailable": self._set_state("ready")
        elif self.app_state == "ready": self._start_recording(target_executable)

    def _start_recording(self, target_executable=None):
        if self.result_frame.winfo_manager(): self._hide_result()
        # Capture the target before showing ClarifyVoice can affect foreground focus.
        self._update_focused_icon(target_executable)
        self._was_hidden_before_recording = not self.winfo_viewable()
        if self._was_hidden_before_recording and not IS_WIN:
            self._show_without_activation()
        if _has_active_microphone() is False:
            self._set_state("microphone_unavailable")
            return
        self._rec_start = time.time()
        self._recording_usage = _recording_usage_context()
        self._recording_usage["mode"] = self.mode
        self._set_state("recording")
        def start():
            try:
                self.recorder.start()
            except MicrophoneUnavailableError:
                self.after(0, self._show_microphone_unavailable)
            except Exception as e: self.after(0, lambda: self._set_state("ready", f"Err: {e}"))
        threading.Thread(target=start, daemon=True).start()

    def _show_microphone_unavailable(self):
        # Ignore a delayed recorder failure if the user already stopped it.
        if self.app_state == "recording":
            self._set_state("microphone_unavailable")

    def _stop_recording(self):
        elapsed = time.time() - self._rec_start
        if elapsed < 3:
            self._set_state("ready", self._t("too_short"))
            threading.Thread(target=self.recorder.cancel, daemon=True).start()
            return
        self._set_state("processing")
        def run():
            self.recorder.stop()
            time.sleep(0.3)
            if not AUDIO_PATH.exists() or AUDIO_PATH.stat().st_size < 1000:
                self.after(0, lambda: self._set_state("ready", self._t("no_audio"))); return
            text = call_transcription_provider(AUDIO_PATH, self.mode, self.lang)
            Recorder._safe_delete(AUDIO_PATH)
            if text and not text.startswith("[Error"):
                try:
                    _record_usage_event(_build_recording_usage_event(
                        getattr(self, "_recording_usage", {}), elapsed, text))
                except OSError:
                    pass
                self.after(0, lambda: self._on_result(text))
            else:
                self.after(0, lambda: self._set_state("ready", self._t("error")))
        threading.Thread(target=run, daemon=True).start()

    def _on_result(self, text):
        threading.Thread(target=lambda: copy_and_paste(text), daemon=True).start()
        def finish():
            self._set_state(
                "ready", after_ready=lambda: self._show_result(text))
        self._show_success_then(finish)

    # -- Settings --
    def _open_settings_legacy(self):
        if hasattr(self, "_settings_win") and self._settings_win and self._settings_win.winfo_exists():
            self._settings_win.focus()
            return
        win = ctk.CTkToplevel(self)
        self._settings_win = win
        win.withdraw()
        win.title("Settings")
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(fg_color=CARD)

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        ww = 460
        win.geometry(f"{ww}x1+{(sw - ww) // 2}+{sh // 2}")
        win._smooth_backdrop = SmoothTkBackdrop(win, 16) if IS_WIN else None

        outer = ctk.CTkFrame(win, fg_color=CARD, corner_radius=16,
            border_width=1, border_color=BORDER)
        outer.pack(fill="both", expand=True, padx=2, pady=2)

        header = ctk.CTkFrame(outer, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(13, 0))
        ctk.CTkLabel(header, text=self._t("settings"), text_color=TEXT,
            font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
        header_actions = ctk.CTkFrame(header, fg_color="transparent")
        header_actions.pack(side="right")

        apply_button = ctk.CTkButton(
            header_actions, text=self._t("apply"), width=72, height=28, corner_radius=14,
            fg_color="#f5f5f5", hover_color="#ffffff", text_color="#050505",
            font=ctk.CTkFont(size=12, weight="bold"))
        apply_button.pack(side="right")
        ctk.CTkButton(header_actions, text="\u2715", width=24, height=24, corner_radius=12,
            fg_color="transparent", hover_color="#222222", text_color=DIM,
            font=ctk.CTkFont(size=12),
            command=lambda: _fade_out_window(win, win.destroy)).pack(
                side="right", padx=(0, 6))

        drafts = {
            "gemini": {
                "key": str(APP_CONFIG.get("gemini_api_key", "")),
                "base": str(APP_CONFIG.get("gemini_base_url", "")),
                "audio_model": str(APP_CONFIG.get("gemini_model", "gemini-2.5-flash")),
                "text_model": "",
            },
            "openai": {
                "key": str(APP_CONFIG.get("openai_api_key", "")),
                "base": str(APP_CONFIG.get("openai_base_url", "")),
                "audio_model": str(APP_CONFIG.get("openai_audio_model", "whisper-1")),
                "text_model": str(APP_CONFIG.get("openai_text_model", "gpt-4o-mini")),
            },
            "groq": {
                "key": str(APP_CONFIG.get("groq_api_key", "")),
                "base": str(APP_CONFIG.get("groq_base_url", "")),
                "audio_model": str(APP_CONFIG.get(
                    "groq_audio_model", "whisper-large-v3-turbo")),
                "text_model": str(APP_CONFIG.get(
                    "groq_text_model", "llama-3.3-70b-versatile")),
            },
        }
        official_bases = {
            "gemini": str(DEFAULT_CONFIG["gemini_base_url"]),
            "openai": str(DEFAULT_CONFIG["openai_base_url"]),
            "groq": str(DEFAULT_CONFIG["groq_base_url"]),
        }
        for provider_id, values in drafts.items():
            values["custom_endpoint"] = (
                values["base"].strip().rstrip("/").lower()
                != official_bases[provider_id].strip().rstrip("/").lower())
        current_provider = {"id": str(APP_CONFIG.get("transcription_provider", "gemini"))}

        provider_row = ctk.CTkFrame(outer, fg_color="transparent")
        provider_row.pack(fill="x", padx=16, pady=(12, 10))
        ctk.CTkLabel(provider_row, text=self._t("provider"), text_color=TEXT,
            font=ctk.CTkFont(size=12, weight="bold"), anchor="w").pack(side="left", padx=(2, 10))
        provider_menu = ctk.CTkOptionMenu(
            provider_row, values=["Gemini", "OpenAI", "Groq"], width=150, height=30,
            corner_radius=15, fg_color="#171717", button_color="#292929",
            button_hover_color="#353535", dropdown_fg_color="#111111",
            dropdown_hover_color="#252525", text_color=TEXT,
            font=ctk.CTkFont(size=12), dropdown_font=ctk.CTkFont(size=12))
        provider_menu.pack(side="left")

        ctk.CTkFrame(outer, height=1, fg_color="#202020", corner_radius=0).pack(
            fill="x", padx=18, pady=(0, 10))

        ctk.CTkLabel(outer, text=self._t("model"), text_color=DIM,
            font=ctk.CTkFont(size=11), anchor="w").pack(
                fill="x", padx=18, pady=(0, 4))
        model_row = ctk.CTkFrame(outer, fg_color="transparent")
        model_row.pack(fill="x", padx=16, pady=(0, 3))
        model_menu = ctk.CTkComboBox(
            model_row, values=[""], height=30, corner_radius=10,
            fg_color="#050505", border_color=BORDER, border_width=1,
            button_color="#292929", button_hover_color="#353535",
            dropdown_fg_color="#111111", dropdown_hover_color="#252525",
            text_color=TEXT, font=ctk.CTkFont(size=12),
            dropdown_font=ctk.CTkFont(size=12))
        model_menu.pack(side="left", fill="x", expand=True)
        refresh_button = ctk.CTkButton(
            model_row, text="\u21bb", width=30, height=30, corner_radius=15,
            fg_color="#171717", hover_color="#292929", text_color=DIM,
            font=ctk.CTkFont(size=16))
        refresh_button.pack(side="right", padx=(7, 0))
        model_status = ctk.CTkLabel(outer, text="", text_color="#555555",
            font=ctk.CTkFont(size=10), anchor="w")
        model_status.pack(fill="x", padx=18, pady=(0, 9))

        key_label = ctk.CTkLabel(outer, text=self._t("api_key"), text_color=DIM,
            font=ctk.CTkFont(size=11), anchor="w")
        key_label.pack(fill="x", padx=18, pady=(0, 4))
        key_entry = ctk.CTkEntry(
            outer, fg_color="#050505", text_color=TEXT, border_color=BORDER,
            border_width=1, corner_radius=10, height=30,
            font=ctk.CTkFont(size=12), placeholder_text=self._t("api_key_placeholder"),
            show="\u2022")
        key_entry.pack(fill="x", padx=16, pady=(0, 8))

        endpoint_section = ctk.CTkFrame(outer, fg_color="transparent")
        endpoint_section.pack(fill="x", padx=16, pady=(0, 8))
        endpoint_switch = ctk.CTkSwitch(
            endpoint_section, text=self._t("custom_endpoint"), width=42,
            height=22, switch_width=36, switch_height=18, corner_radius=9,
            border_width=1, fg_color="#171717", progress_color="#e7e7e7",
            button_color="#777777", button_hover_color="#999999",
            text_color=DIM, font=ctk.CTkFont(size=11))
        endpoint_switch.pack(fill="x", padx=2)
        base_fields = ctk.CTkFrame(endpoint_section, fg_color="transparent")
        base_label = ctk.CTkLabel(base_fields, text=self._t("base_url"), text_color=DIM,
            font=ctk.CTkFont(size=11), anchor="w")
        base_label.pack(fill="x", padx=2, pady=(8, 4))
        base_entry = ctk.CTkEntry(
            base_fields, fg_color="#050505", text_color=TEXT, border_color=BORDER,
            border_width=1, corner_radius=10, height=30, font=ctk.CTkFont(size=12))
        base_entry.pack(fill="x")

        prompt_fields = ctk.CTkFrame(outer, fg_color="transparent")
        ctk.CTkLabel(prompt_fields, text=self._t("prompt_model"), text_color=DIM,
            font=ctk.CTkFont(size=11), anchor="w").pack(fill="x", padx=2, pady=(0, 4))
        text_model_entry = ctk.CTkEntry(
            prompt_fields, fg_color="#050505", text_color=TEXT, border_color=BORDER,
            border_width=1, corner_radius=10, height=30, font=ctk.CTkFont(size=12))
        text_model_entry.pack(fill="x", pady=(0, 4))
        prompt_hint = ctk.CTkLabel(prompt_fields, text=self._t("openai_prompt_hint"),
            text_color="#555555", font=ctk.CTkFont(size=10), anchor="w")
        prompt_hint.pack(fill="x", padx=2)

        hint = ctk.CTkLabel(outer, text=self._t("gemini_proxy_hint"), text_color="#555555",
            font=ctk.CTkFont(size=10), anchor="w")
        hint.pack(fill="x", padx=18, pady=(0, 10))

        def resize_to_content():
            if not win.winfo_exists():
                return
            win.update_idletasks()
            dpi_scale = win.winfo_fpixels("1i") / 96.0
            required_physical = max(
                round(280 * dpi_scale), outer.winfo_reqheight() + round(4 * dpi_scale))
            logical_height = max(1, round(required_physical / dpi_scale))
            physical_width = round(ww * dpi_scale)
            x = max(0, (sw - physical_width) // 2)
            y = max(0, (sh - required_physical) // 2)
            win.geometry(f"{ww}x{logical_height}+{x}+{y}")

        def store_visible_fields():
            provider = current_provider["id"]
            drafts[provider]["key"] = key_entry.get().strip()
            drafts[provider]["base"] = base_entry.get().strip().rstrip("/")
            drafts[provider]["custom_endpoint"] = bool(endpoint_switch.get())
            drafts[provider]["audio_model"] = model_menu.get().strip()
            if provider in ("openai", "groq"):
                drafts[provider]["text_model"] = text_model_entry.get().strip()

        def fill_entry(entry, value):
            entry.delete(0, "end")
            entry.insert(0, value)

        model_request = {"generation": 0}

        def refresh_models():
            if not win.winfo_exists():
                return
            provider = current_provider["id"]
            key = key_entry.get().strip()
            base = (base_entry.get().strip() if endpoint_switch.get()
                    else official_bases[provider])
            selected = model_menu.get().strip()
            model_request["generation"] += 1
            generation = model_request["generation"]
            model_status.configure(text=self._t("loading_models"), text_color="#666666")
            refresh_button.configure(state="disabled")

            def load():
                try:
                    models = _fetch_provider_models(provider, key, base)
                    error = None
                except Exception as exc:
                    models = []
                    error = str(exc).replace("\n", " ")[:100]

                def finish():
                    if (not win.winfo_exists() or generation != model_request["generation"]
                            or provider != current_provider["id"]):
                        return
                    choices = list(models)
                    if selected and selected not in choices:
                        choices.insert(0, selected)
                    model_menu.configure(values=choices or [selected or ""])
                    if selected:
                        model_menu.set(selected)
                    if error:
                        message = self._t("models_error").format(error=error)
                        color = "#8a6666"
                    elif models:
                        message = self._t("models_found").format(count=len(models))
                        color = "#666666"
                    else:
                        message = self._t("no_models")
                        color = "#8a6666"
                    model_status.configure(text=message, text_color=color)
                    refresh_button.configure(state="normal")
                    win.after_idle(resize_to_content)

                win.after(0, finish)

            threading.Thread(target=load, daemon=True).start()

        def toggle_custom_endpoint():
            provider = current_provider["id"]
            enabled = bool(endpoint_switch.get())
            drafts[provider]["custom_endpoint"] = enabled
            if enabled:
                base_fields.pack(fill="x")
            else:
                base_fields.pack_forget()
            win.after_idle(resize_to_content)
            win.after(80, refresh_models)

        def select_provider(display_name, store_current=True):
            if store_current:
                store_visible_fields()
            provider = display_name.lower()
            current_provider["id"] = provider
            values = drafts[provider]
            fill_entry(key_entry, values["key"])
            fill_entry(base_entry, values["base"])
            if values["custom_endpoint"]:
                endpoint_switch.select()
                base_fields.pack(fill="x")
            else:
                endpoint_switch.deselect()
                base_fields.pack_forget()
            model_menu.configure(values=[values["audio_model"]])
            model_menu.set(values["audio_model"])
            key_label.configure(text=f"{display_name} {self._t('api_key')}")
            if provider in ("openai", "groq"):
                fill_entry(text_model_entry, values["text_model"])
                prompt_fields.pack(fill="x", padx=16, pady=(0, 8), before=hint)
                hint.configure(text="")
            else:
                prompt_fields.pack_forget()
                hint.configure(text=self._t("gemini_proxy_hint"))
            model_status.configure(text="")
            win.after_idle(resize_to_content)
            win.after(80, refresh_models)

        provider_menu.configure(command=select_provider)
        refresh_button.configure(command=refresh_models)
        endpoint_switch.configure(command=toggle_custom_endpoint)
        initial_display = {
            "openai": "OpenAI", "groq": "Groq", "gemini": "Gemini",
        }.get(current_provider["id"], "Gemini")
        provider_menu.set(initial_display)
        select_provider(initial_display, store_current=False)

        def apply_changes():
            store_visible_fields()
            APP_CONFIG.update({
                "transcription_provider": current_provider["id"],
                "gemini_api_key": drafts["gemini"]["key"],
                "gemini_base_url": (drafts["gemini"]["base"]
                    if drafts["gemini"]["custom_endpoint"] and drafts["gemini"]["base"]
                    else official_bases["gemini"]),
                "gemini_model": drafts["gemini"]["audio_model"] or "gemini-2.5-flash",
                "openai_api_key": drafts["openai"]["key"],
                "openai_base_url": (drafts["openai"]["base"]
                    if drafts["openai"]["custom_endpoint"] and drafts["openai"]["base"]
                    else official_bases["openai"]),
                "openai_audio_model": drafts["openai"]["audio_model"] or "whisper-1",
                "openai_text_model": drafts["openai"]["text_model"] or "gpt-4o-mini",
                "groq_api_key": drafts["groq"]["key"],
                "groq_base_url": (drafts["groq"]["base"]
                    if drafts["groq"]["custom_endpoint"] and drafts["groq"]["base"]
                    else official_bases["groq"]),
                "groq_audio_model": (
                    drafts["groq"]["audio_model"] or "whisper-large-v3-turbo"),
                "groq_text_model": (
                    drafts["groq"]["text_model"] or "llama-3.3-70b-versatile"),
            })
            try:
                _save_app_config()
                _fade_out_window(win, win.destroy)
            except OSError as error:
                hint.configure(text=f"Could not save settings: {error}", text_color="#ef4444")

        apply_button.configure(command=apply_changes)
        resize_to_content()
        _configure_windows_tool_window(win)
        _fade_in_window(win)
        win.lift()
        win.focus_force()

    def _open_settings_rebuild(self):
        if (hasattr(self, "_settings_win") and self._settings_win
                and self._settings_win.winfo_exists()):
            self._settings_win.focus()
            return

        win = ctk.CTkToplevel(self)
        self._settings_win = win
        win.withdraw()
        win.title(self._t("settings"))
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(fg_color=CARD)

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        window_width = 720
        win.geometry(f"{window_width}x500+{max(0, (sw-window_width)//2)}+{max(0, (sh-500)//2)}")
        win._smooth_backdrop = SmoothTkBackdrop(win, 18) if IS_WIN else None

        # DWM already supplies the single rounded window outline. A second
        # CTk border here creates the visible double-edge around the menu.
        outer = ctk.CTkFrame(
            win, fg_color=CARD, corner_radius=18, border_width=0)
        outer.pack(fill="both", expand=True)

        header = ctk.CTkFrame(outer, fg_color="transparent", height=48)
        header.pack(fill="x", padx=18, pady=(12, 8))
        header.pack_propagate(False)
        title_label = ctk.CTkLabel(header, text=self._t("settings_section"),
            text_color=TEXT, font=ctk.CTkFont(size=15, weight="bold"))
        title_label.pack(side="left")
        header_actions = ctk.CTkFrame(header, fg_color="transparent")
        header_actions.pack(side="right")
        apply_button = ctk.CTkButton(header_actions, text=self._t("apply"),
            width=76, height=30, corner_radius=15, fg_color="#f5f5f5",
            hover_color="#ffffff", text_color="#050505",
            font=ctk.CTkFont(size=12, weight="bold"))
        apply_button.pack(side="right")
        ctk.CTkButton(header_actions, text="\u2715", width=26, height=26,
            corner_radius=13, fg_color="transparent", hover_color="#222222",
            text_color=DIM,
            command=lambda: _fade_out_window(win, win.destroy)).pack(
                side="right", padx=(0, 7))

        body = ctk.CTkFrame(outer, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        sidebar = ctk.CTkFrame(body, width=164, fg_color="#090909", corner_radius=13)
        sidebar.pack(side="left", fill="y", padx=(0, 10))
        sidebar.pack_propagate(False)
        content = ctk.CTkFrame(body, fg_color="#0b0b0b", corner_radius=13)
        content.pack(side="left", fill="both", expand=True)

        provider_names = {"gemini": "Gemini", "openai": "OpenAI", "groq": "Groq"}
        provider_ids = ("gemini", "openai", "groq")
        model_config_keys = {
            "gemini": "gemini_model", "openai": "openai_audio_model",
            "groq": "groq_audio_model",
        }
        default_models = {
            "gemini": "gemini-2.5-flash", "openai": "whisper-1",
            "groq": "whisper-large-v3-turbo",
        }
        default_bases = {
            provider: str(DEFAULT_CONFIG[f"{provider}_base_url"])
            for provider in provider_ids
        }
        provider_state = {
            provider: {
                "status": "not_configured", "models": [], "error": "",
                "generation": 0,
            } for provider in provider_ids
        }
        selected = {
            "provider": str(APP_CONFIG.get("transcription_provider", "gemini")),
            "model": "",
        }
        selected["model"] = str(APP_CONFIG.get(
            model_config_keys.get(selected["provider"], "gemini_model"),
            default_models.get(selected["provider"], "gemini-2.5-flash")))
        view = {"page": "settings", "provider": None}
        model_popup = {"win": None}

        provider_images = {}
        if Image is not None:
            for provider in provider_ids:
                source = _make_provider_icon(provider, 64)
                if source is not None:
                    provider_images[provider] = ctk.CTkImage(
                        light_image=source, dark_image=source, size=(24, 24))

        nav_buttons = {}

        def resize_to_content():
            if not win.winfo_exists():
                return
            win.update_idletasks()
            dpi_scale = win.winfo_fpixels("1i") / 96.0
            requested_physical = outer.winfo_reqheight() + round(4 * dpi_scale)
            min_physical = round(480 * dpi_scale)
            max_physical = max(min_physical, sh - round(40 * dpi_scale))
            physical_height = min(max(requested_physical, min_physical), max_physical)
            logical_height = round(physical_height / dpi_scale)
            physical_width = round(window_width * dpi_scale)
            x = max(0, (sw - physical_width) // 2)
            y = max(0, (sh - physical_height) // 2)
            win.geometry(f"{window_width}x{logical_height}+{x}+{y}")

        def close_model_popup():
            popup = model_popup.get("win")
            if popup is not None and popup.winfo_exists():
                _fade_out_window(popup, popup.destroy)
            model_popup["win"] = None

        def clear_content():
            close_model_popup()
            for child in content.winfo_children():
                child.destroy()

        def active_model_options():
            options = []
            for provider in provider_ids:
                if provider_state[provider]["status"] == "active":
                    options.extend((provider, model) for model in provider_state[provider]["models"])
            return options

        def ensure_valid_selection():
            options = active_model_options()
            if (selected["provider"], selected["model"]) not in options and options:
                selected["provider"], selected["model"] = options[0]

        def set_nav(page):
            for name, button in nav_buttons.items():
                button.configure(
                    fg_color="#1b1b1b" if name == page else "transparent",
                    text_color=TEXT if name == page else DIM)

        def show_page(page, provider=None):
            view["page"], view["provider"] = page, provider
            set_nav("providers" if page == "provider_detail" else page)
            if page == "settings":
                render_settings()
            elif page == "providers":
                render_providers()
            else:
                render_provider_detail(provider)
            win.after_idle(resize_to_content)

        def select_model(provider, model):
            selected["provider"], selected["model"] = provider, model
            close_model_popup()
            render_settings()

        def open_model_menu(anchor):
            close_model_popup()
            options = active_model_options()
            popup = ctk.CTkToplevel(win)
            model_popup["win"] = popup
            popup.withdraw()
            popup.overrideredirect(True)
            popup.attributes("-topmost", True)
            popup.configure(fg_color=TRANSPARENT)
            if IS_WIN:
                popup.attributes("-transparentcolor", TRANSPARENT)
            popup._smooth_backdrop = SmoothTkBackdrop(popup, 12) if IS_WIN else None
            anchor.update_idletasks()
            width = max(360, anchor.winfo_width())
            height = min(292, max(92, 46 * (len(options) + 1) + 12))
            x, y = anchor.winfo_rootx(), anchor.winfo_rooty() + anchor.winfo_height() + 5
            popup.geometry(f"{width}x{height}+{x}+{y}")
            shell = ctk.CTkFrame(popup, fg_color="#111111", corner_radius=12,
                border_width=1, border_color="#292929")
            shell.pack(fill="both", expand=True, padx=2, pady=2)
            rows = ctk.CTkScrollableFrame(shell, fg_color="transparent",
                scrollbar_button_color="#303030", scrollbar_button_hover_color="#444444")
            rows.pack(fill="both", expand=True, padx=5, pady=5)
            for provider, model in options:
                button = ctk.CTkButton(rows, text=model,
                    image=provider_images.get(provider), compound="left", anchor="w",
                    height=38, corner_radius=9, fg_color="transparent",
                    hover_color="#252525", text_color=TEXT,
                    font=ctk.CTkFont(size=12),
                    command=lambda p=provider, m=model: select_model(p, m))
                button.pack(fill="x", pady=1)
            ctk.CTkFrame(rows, height=1, fg_color="#252525").pack(
                fill="x", padx=5, pady=4)
            ctk.CTkButton(rows, text=self._t("add_provider"), anchor="w",
                height=38, corner_radius=9, fg_color="transparent",
                hover_color="#252525", text_color="#b8b8b8",
                font=ctk.CTkFont(size=12, weight="bold"),
                command=lambda: (close_model_popup(), show_page("providers"))).pack(fill="x")
            _configure_windows_tool_window(popup)
            _fade_in_window(popup)
            popup.lift()
            popup.focus_force()
            popup.bind("<Escape>", lambda _event: close_model_popup())

        def render_settings():
            clear_content()
            title_label.configure(text=self._t("settings_section"))
            ensure_valid_selection()
            inner = ctk.CTkFrame(content, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=24, pady=22)
            ctk.CTkLabel(inner, text=self._t("choose_model"), text_color=TEXT,
                font=ctk.CTkFont(size=18, weight="bold"), anchor="w").pack(fill="x")
            ctk.CTkLabel(inner, text=self._t("model_subtitle"), text_color=DIM,
                font=ctk.CTkFont(size=11), anchor="w").pack(fill="x", pady=(2, 18))
            options = active_model_options()
            if options:
                provider = selected["provider"]
                picker_text = selected["model"]
                picker = ctk.CTkButton(inner, text=f"{picker_text}     ⌄",
                    image=provider_images.get(provider), compound="left", anchor="w",
                    height=44, corner_radius=11, fg_color="#141414",
                    hover_color="#1d1d1d", border_width=1, border_color="#292929",
                    text_color=TEXT, font=ctk.CTkFont(size=12),
                    command=lambda: open_model_menu(picker))
                picker.pack(fill="x")
                ctk.CTkLabel(inner, text=provider_names[provider], text_color="#666666",
                    font=ctk.CTkFont(size=10), anchor="w").pack(
                        fill="x", padx=4, pady=(6, 0))
            else:
                empty = ctk.CTkFrame(inner, fg_color="#111111", corner_radius=11,
                    border_width=1, border_color="#262626")
                empty.pack(fill="x")
                ctk.CTkLabel(empty, text=self._t("no_active_models"), text_color=DIM,
                    font=ctk.CTkFont(size=11), wraplength=390, justify="left").pack(
                        anchor="w", padx=14, pady=(13, 7))
                ctk.CTkButton(empty, text=self._t("add_provider"), width=130,
                    height=30, corner_radius=15, fg_color="#242424",
                    hover_color="#303030", text_color=TEXT,
                    command=lambda: show_page("providers")).pack(
                        anchor="w", padx=12, pady=(0, 12))

        def status_text(provider):
            status = provider_state[provider]["status"]
            if status == "active":
                return self._t("active"), "#69c58a"
            if status == "validating":
                return self._t("validating"), "#b9a66b"
            return self._t("not_configured"), "#777777"

        def render_providers():
            clear_content()
            title_label.configure(text=self._t("providers_section"))
            inner = ctk.CTkFrame(content, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=22, pady=20)
            ctk.CTkLabel(inner, text=self._t("providers_section"), text_color=TEXT,
                font=ctk.CTkFont(size=18, weight="bold"), anchor="w").pack(fill="x")
            ctk.CTkLabel(inner, text=self._t("providers_subtitle"), text_color=DIM,
                font=ctk.CTkFont(size=11), anchor="w").pack(fill="x", pady=(2, 14))
            for provider in provider_ids:
                card = ctk.CTkFrame(inner, fg_color="#121212", corner_radius=12,
                    border_width=1, border_color="#242424", height=66)
                card.pack(fill="x", pady=4)
                card.pack_propagate(False)
                ctk.CTkLabel(card, text="", image=provider_images.get(provider),
                    width=34).pack(side="left", padx=(13, 6))
                labels = ctk.CTkFrame(card, fg_color="transparent")
                labels.pack(side="left", fill="y", pady=10)
                ctk.CTkLabel(labels, text=provider_names[provider], text_color=TEXT,
                    font=ctk.CTkFont(size=12, weight="bold"), anchor="w").pack(anchor="w")
                status, color = status_text(provider)
                ctk.CTkLabel(labels, text=f"\u25cf  {status}", text_color=color,
                    font=ctk.CTkFont(size=10), anchor="w").pack(anchor="w", pady=(2, 0))
                ctk.CTkButton(card, text="\u203a", width=34, height=34, corner_radius=17,
                    fg_color="transparent", hover_color="#282828", text_color=DIM,
                    font=ctk.CTkFont(size=20),
                    command=lambda p=provider: show_page("provider_detail", p)).pack(
                        side="right", padx=10)
                def bind_card(widget, provider_id=provider):
                    widget.bind("<Button-1>", lambda _event, p=provider_id:
                        show_page("provider_detail", p))
                    for child in widget.winfo_children():
                        bind_card(child, provider_id)
                bind_card(card)

        def validate_provider(provider, api_key, base_url, on_done=None):
            state = provider_state[provider]
            state["generation"] += 1
            generation = state["generation"]
            state.update(status="validating", error="")
            if view["page"] in ("providers", "provider_detail"):
                show_page(view["page"], view["provider"])

            def run():
                try:
                    catalog = _validate_provider_credentials(provider, api_key, base_url)
                    models = _parse_audio_models(provider, catalog)
                    if (provider == "openai" and "api.openai.com" in base_url.lower()
                            and not models):
                        models = list(OPENAI_OFFICIAL_AUDIO_MODELS)
                    if (provider == "groq" and "api.groq.com" in base_url.lower()
                            and not models):
                        models = list(GROQ_OFFICIAL_AUDIO_MODELS)
                    text_models = _parse_text_models(provider, catalog)
                    error = ""
                except Exception as exc:
                    models = []
                    text_models = []
                    if isinstance(exc, requests.HTTPError) and exc.response is not None:
                        error = f"HTTP {exc.response.status_code}"
                    else:
                        error = str(exc).replace("\n", " ")[:120]

                def finish():
                    if not win.winfo_exists() or generation != state["generation"]:
                        return
                    if error:
                        state.update(status="not_configured", models=[], error=error)
                    else:
                        state.update(status="active", models=models, error="")
                    ensure_valid_selection()
                    if on_done:
                        on_done(not error)
                    elif view["page"] in ("settings", "providers"):
                        show_page(view["page"], view["provider"])

                win.after(0, finish)

            threading.Thread(target=run, daemon=True).start()

        def render_provider_detail(provider):
            clear_content()
            title_label.configure(text=provider_names[provider])
            inner = ctk.CTkFrame(content, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=22, pady=18)
            ctk.CTkButton(inner, text=f"\u2039  {self._t('back')}", width=70,
                height=26, anchor="w", fg_color="transparent", hover_color="#202020",
                text_color=DIM, command=lambda: show_page("providers")).pack(anchor="w")
            heading = ctk.CTkFrame(inner, fg_color="transparent")
            heading.pack(fill="x", pady=(12, 14))
            ctk.CTkLabel(heading, text="", image=provider_images.get(provider),
                width=36).pack(side="left", padx=(0, 8))
            ctk.CTkLabel(heading, text=provider_names[provider], text_color=TEXT,
                font=ctk.CTkFont(size=17, weight="bold")).pack(side="left")
            status, color = status_text(provider)
            ctk.CTkLabel(heading, text=f"\u25cf  {status}", text_color=color,
                font=ctk.CTkFont(size=10)).pack(side="right")

            ctk.CTkLabel(inner, text=self._t("api_key"), text_color=DIM,
                font=ctk.CTkFont(size=11), anchor="w").pack(fill="x", padx=2, pady=(0, 4))
            key_entry = ctk.CTkEntry(inner, height=32, corner_radius=10,
                fg_color="#050505", text_color=TEXT, border_color=BORDER,
                border_width=1, show="\u2022", font=ctk.CTkFont(size=12))
            key_entry.pack(fill="x", pady=(0, 10))
            key_entry.insert(0, str(APP_CONFIG.get(f"{provider}_api_key", "")))

            saved_base = str(APP_CONFIG.get(f"{provider}_base_url", default_bases[provider]))
            custom = saved_base.rstrip("/").lower() != default_bases[provider].rstrip("/").lower()
            endpoint_switch = ctk.CTkSwitch(inner, text=self._t("custom_endpoint"),
                height=22, switch_width=36, switch_height=18, corner_radius=9,
                border_width=1, fg_color="#171717", progress_color="#e7e7e7",
                button_color="#777777", text_color=DIM, font=ctk.CTkFont(size=11))
            endpoint_switch.pack(fill="x", padx=2)
            endpoint_fields = ctk.CTkFrame(inner, fg_color="transparent")
            ctk.CTkLabel(endpoint_fields, text=self._t("base_url"), text_color=DIM,
                font=ctk.CTkFont(size=11), anchor="w").pack(fill="x", padx=2, pady=(8, 4))
            base_entry = ctk.CTkEntry(endpoint_fields, height=32, corner_radius=10,
                fg_color="#050505", text_color=TEXT, border_color=BORDER,
                border_width=1, font=ctk.CTkFont(size=12))
            base_entry.pack(fill="x")
            base_entry.insert(0, saved_base)
            if custom:
                endpoint_switch.select()
                endpoint_fields.pack(fill="x")

            def toggle_endpoint():
                if endpoint_switch.get():
                    endpoint_fields.pack(fill="x")
                else:
                    endpoint_fields.pack_forget()
                win.after_idle(resize_to_content)
            endpoint_switch.configure(command=toggle_endpoint)


            message = ctk.CTkLabel(inner, text="", text_color="#d17878",
                font=ctk.CTkFont(size=10), anchor="w", justify="left", wraplength=430)
            message.pack(fill="x", padx=2, pady=(8, 4))
            if provider_state[provider]["error"]:
                message.configure(text=self._t("validation_failed").format(
                    error=provider_state[provider]["error"]))

            actions = ctk.CTkFrame(inner, fg_color="transparent")
            actions.pack(fill="x", pady=(4, 0))
            validate_button = ctk.CTkButton(actions, text=self._t("validate_save"),
                width=130, height=32, corner_radius=16, fg_color="#ededed",
                hover_color="#ffffff", text_color="#050505",
                font=ctk.CTkFont(size=11, weight="bold"))
            validate_button.pack(side="left")

            def validation_done(success):
                if not win.winfo_exists():
                    return
                if success:
                    model_key = model_config_keys[provider]
                    models = provider_state[provider]["models"]
                    current_model = str(APP_CONFIG.get(model_key, default_models[provider]))
                    if models and current_model not in models:
                        APP_CONFIG[model_key] = models[0]
                    try:
                        _save_app_config()
                    except OSError as exc:
                        provider_state[provider].update(status="not_configured", error=str(exc))
                show_page("provider_detail", provider)

            def validate_and_save():
                key = key_entry.get().strip()
                base = (base_entry.get().strip() if endpoint_switch.get()
                        else default_bases[provider])
                if not key:
                    message.configure(text=self._t("validation_failed").format(
                        error=self._t("api_key")))
                    return
                APP_CONFIG[f"{provider}_api_key"] = key
                APP_CONFIG[f"{provider}_base_url"] = base or default_bases[provider]
                if text_entry is not None:
                    APP_CONFIG[f"{provider}_text_model"] = text_entry.get().strip()
                validate_provider(provider, key, base or default_bases[provider], validation_done)
            validate_button.configure(command=validate_and_save)

            def deactivate():
                APP_CONFIG[f"{provider}_api_key"] = ""
                APP_CONFIG[f"{provider}_base_url"] = default_bases[provider]
                provider_state[provider].update(
                    status="not_configured", models=[], error="",
                    generation=provider_state[provider]["generation"] + 1)
                try:
                    _save_app_config()
                except OSError:
                    pass
                ensure_valid_selection()
                show_page("providers")

            if provider_state[provider]["status"] == "active":
                ctk.CTkButton(actions, text=self._t("deactivate"), height=32,
                    corner_radius=16, fg_color="transparent", hover_color="#251717",
                    border_width=1, border_color="#3a2222", text_color="#b67b7b",
                    font=ctk.CTkFont(size=11), command=deactivate).pack(side="right")

        def apply_settings():
            options = active_model_options()
            if (selected["provider"], selected["model"]) in options:
                APP_CONFIG["transcription_provider"] = selected["provider"]
                APP_CONFIG[model_config_keys[selected["provider"]]] = selected["model"]
            try:
                _save_app_config()
                _fade_out_window(win, win.destroy)
            except OSError:
                pass

        apply_button.configure(command=apply_settings)
        nav_buttons["settings"] = ctk.CTkButton(sidebar,
            text=self._t("settings_section"), anchor="w", height=38,
            corner_radius=9, fg_color="#1b1b1b", hover_color="#242424",
            text_color=TEXT, font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: show_page("settings"))
        nav_buttons["settings"].pack(fill="x", padx=9, pady=(12, 3))
        nav_buttons["providers"] = ctk.CTkButton(sidebar,
            text=self._t("providers_section"), anchor="w", height=38,
            corner_radius=9, fg_color="transparent", hover_color="#242424",
            text_color=DIM, font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: show_page("providers"))
        nav_buttons["providers"].pack(fill="x", padx=9, pady=3)

        render_settings()
        for provider in provider_ids:
            key = str(APP_CONFIG.get(f"{provider}_api_key", "")).strip()
            if key:
                validate_provider(
                    provider, key,
                    str(APP_CONFIG.get(f"{provider}_base_url", default_bases[provider])))
        resize_to_content()
        _configure_windows_tool_window(win)
        _fade_in_window(win)
        win.lift()
        win.focus_force()

    def _open_settings(self):
        if (hasattr(self, "_settings_win") and self._settings_win
                and self._settings_win.winfo_exists()):
            self._settings_win.focus()
            return

        win = ctk.CTkToplevel(self)
        self._settings_win = win
        win.withdraw()
        win.title(self._t("settings"))
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(fg_color=CARD)

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        width, height = 720, 540
        win.geometry(f"{width}x{height}+{max(0, (sw-width)//2)}+{max(0, (sh-height)//2)}")
        win._smooth_backdrop = None

        font_family = "Segoe UI Variable Text" if IS_WIN else "Arial"
        display_family = "Segoe UI Variable Display Semibold" if IS_WIN else font_family
        font_title = ctk.CTkFont(family=display_family, size=18)
        font_section = ctk.CTkFont(family=display_family, size=15)
        font_body = ctk.CTkFont(family=font_family, size=12)
        font_label = ctk.CTkFont(family=font_family, size=11)
        font_caption = ctk.CTkFont(family=font_family, size=10)

        # The native rounded DWM outline is the only perimeter border.
        outer = ctk.CTkFrame(
            win, fg_color=CARD, corner_radius=18, border_width=0)
        outer.pack(fill="both", expand=True)
        header = ctk.CTkFrame(outer, fg_color="transparent", height=48)
        header.pack(fill="x", padx=18, pady=(12, 8))
        header.pack_propagate(False)
        header_title = ctk.CTkLabel(header, text=self._t("models_section"),
            text_color=TEXT, font=font_section)
        header_title.pack(side="left")
        _make_window_draggable(win, header, header_title)
        header_actions = ctk.CTkFrame(header, fg_color="transparent")
        header_actions.pack(side="right")
        ctk.CTkButton(header_actions, text="\u2715", width=26, height=26,
            corner_radius=13, fg_color="transparent", hover_color="#222222",
            text_color=DIM,
            command=lambda: _fade_out_window(win, win.destroy)).pack(
                side="right")

        footer = ctk.CTkFrame(outer, fg_color="transparent", height=38)
        footer.pack(side="bottom", fill="x", padx=18, pady=(0, 14))
        footer.pack_propagate(False)
        apply_button = ctk.CTkButton(footer, text=self._t("apply"),
            width=82, height=32, corner_radius=16, fg_color="#f5f5f5",
            hover_color="#ffffff", text_color="#050505", font=font_body)
        apply_button.pack(side="right")
        undo_button = ctk.CTkButton(footer, text="\u21a9", width=32, height=32,
            corner_radius=16, fg_color="transparent", hover_color="#1b1b1b",
            text_color="#a0a0a0", font=ctk.CTkFont(
                family=font_family, size=16))

        body = ctk.CTkFrame(outer, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        sidebar = ctk.CTkFrame(body, width=164, fg_color="#090909", corner_radius=13)
        sidebar.pack(side="left", fill="y", padx=(0, 10))
        sidebar.pack_propagate(False)
        content = ctk.CTkFrame(body, fg_color="#0b0b0b", corner_radius=13)
        content.pack(side="left", fill="both", expand=True)

        provider_ids = ("gemini", "openai", "groq")
        provider_names = {"gemini": "Gemini", "openai": "OpenAI", "groq": "Groq"}
        model_keys = {
            "gemini": "gemini_model", "openai": "openai_audio_model",
            "groq": "groq_audio_model",
        }
        default_models = {
            "gemini": "gemini-2.5-flash", "openai": "whisper-1",
            "groq": "whisper-large-v3-turbo",
        }
        default_bases = {
            provider: str(DEFAULT_CONFIG[f"{provider}_base_url"])
            for provider in provider_ids
        }
        state = {
            provider: {"status": "not_configured", "models": [], "text_models": [],
                       "error": "", "generation": 0}
            for provider in provider_ids
        }
        selected = {
            "provider": str(APP_CONFIG.get("transcription_provider", "gemini")),
            "model": "",
        }
        selected["model"] = str(APP_CONFIG.get(
            model_keys.get(selected["provider"], "gemini_model"),
            default_models.get(selected["provider"], "gemini-2.5-flash")))
        selected_refinement = {
            "provider": str(APP_CONFIG.get("refinement_provider", "openai")),
            "model": str(APP_CONFIG.get("refinement_model", "gpt-4o-mini")),
        }

        images = {}
        picker_images = {}
        card_canvas_images = {}
        for provider in provider_ids:
            source = _make_provider_icon(provider, 96)
            if source is not None:
                images[provider] = ctk.CTkImage(
                    light_image=source, dark_image=source, size=(24, 24))
                picker_images[provider] = ctk.CTkImage(
                    light_image=source, dark_image=source, size=(18, 18))
                card_canvas_images[provider] = ImageTk.PhotoImage(
                    source.resize((24, 24), Image.Resampling.LANCZOS))

        add_provider_text = self._t("add_provider")

        class VirtualModelList(ctk.CTkFrame):
            """Small recycled Canvas list; only visible model rows are drawn."""

            def __init__(self, parent, viewport_height=145):
                super().__init__(parent, fg_color="transparent")
                self.row_height = 38
                self.items = []
                self.selected = None
                self.hovered = None
                self.on_select = None
                self.on_add = None
                self._icons = {}
                for provider_id in provider_ids:
                    icon = _make_provider_icon(provider_id, 32)
                    if icon is not None:
                        self._icons[provider_id] = ImageTk.PhotoImage(
                            icon.resize((16, 16), Image.Resampling.LANCZOS))
                self.canvas = tk.Canvas(self, height=viewport_height,
                    background="#111111", highlightthickness=0, borderwidth=0,
                    cursor="hand2", yscrollincrement=self.row_height)
                self.scrollbar = ctk.CTkScrollbar(self, orientation="vertical",
                    width=9, command=self._scrollbar_command,
                    button_color="#303030", button_hover_color="#444444")
                self.canvas.configure(yscrollcommand=self.scrollbar.set)
                self.canvas.pack(side="left", fill="both", expand=True)
                self.scrollbar.pack(side="right", fill="y", padx=(3, 0))
                self.canvas.bind("<Configure>", lambda _event: self._render_rows())
                self.canvas.bind("<MouseWheel>", self._mousewheel)
                self.canvas.bind("<Motion>", self._motion)
                self.canvas.bind("<Leave>", self._leave)
                self.canvas.bind("<Button-1>", self._click)

            def set_items(self, items, selected=None):
                new_items = list(items)
                changed = new_items != self.items
                self.items = new_items
                self.selected = selected
                row_count = len(self.items) + 1
                self.canvas.configure(scrollregion=(
                    0, 0, max(1, self.canvas.winfo_width()), row_count * self.row_height))
                if changed:
                    self.canvas.yview_moveto(0)
                    self.hovered = None
                self._render_rows()

            def _scrollbar_command(self, *args):
                self.canvas.yview(*args)
                self._render_rows()

            def _mousewheel(self, event):
                steps = -1 if event.delta > 0 else 1
                self.canvas.yview_scroll(steps, "units")
                self._render_rows()
                return "break"

            def _index_at(self, y):
                index = int(self.canvas.canvasy(y) // self.row_height)
                return index if 0 <= index <= len(self.items) else None

            def _motion(self, event):
                index = self._index_at(event.y)
                if index != self.hovered:
                    self.hovered = index
                    self._render_rows()

            def _leave(self, _event):
                if self.hovered is not None:
                    self.hovered = None
                    self._render_rows()

            def _click(self, event):
                index = self._index_at(event.y)
                if index is None:
                    return
                if index == len(self.items):
                    if self.on_add:
                        self.on_add()
                elif self.on_select:
                    self.on_select(*self.items[index])

            def _render_rows(self):
                if not self.canvas.winfo_exists():
                    return
                self.canvas.delete("all")
                width = max(1, self.canvas.winfo_width())
                top = max(0, self.canvas.canvasy(0))
                viewport = max(1, self.canvas.winfo_height())
                first = max(0, int(top // self.row_height))
                last = min(len(self.items), int((top + viewport) // self.row_height) + 1)
                for index in range(first, last):
                    provider, model = self.items[index]
                    y = index * self.row_height
                    fill = "#252525" if index == self.hovered else (
                        "#202020" if (provider, model) == self.selected else "#111111")
                    self.canvas.create_rectangle(
                        0, y, width, y + self.row_height, fill=fill, outline="")
                    icon = self._icons.get(provider)
                    if icon is not None:
                        self.canvas.create_image(15, y + self.row_height / 2,
                            image=icon, anchor="w")
                    self.canvas.create_text(40, y + self.row_height / 2,
                        text=model, fill="#e8e8e8", anchor="w",
                        font=(font_family, 10))
                add_index = len(self.items)
                add_y = add_index * self.row_height
                if add_index >= first and add_y <= top + viewport + self.row_height:
                    fill = "#252525" if add_index == self.hovered else "#111111"
                    self.canvas.create_rectangle(
                        0, add_y, width, add_y + self.row_height, fill=fill, outline="")
                    self.canvas.create_line(8, add_y, width - 8, add_y, fill="#252525")
                    self.canvas.create_text(14, add_y + self.row_height / 2,
                        text=add_provider_text, fill="#b8b8b8", anchor="w",
                        font=(font_family, 10))

        pages = {
            "models": ctk.CTkFrame(content, fg_color="transparent"),
            "statistics": ctk.CTkFrame(content, fg_color="transparent"),
            "settings": ctk.CTkFrame(content, fg_color="transparent"),
            "providers": ctk.CTkFrame(content, fg_color="transparent"),
        }
        detail_pages = {
            provider: ctk.CTkFrame(content, fg_color="transparent")
            for provider in provider_ids
        }
        current_page = {"name": None}
        nav_buttons = {}
        card_buttons = {}
        card_status_buttons = {}
        detail_status_labels = {}
        detail_messages = {}
        validate_buttons = {}
        deactivate_buttons = {}
        detail_inputs = {}
        model_menu_visible = {"value": False}
        model_signature = {"value": None}

        def status_presentation(provider):
            status = state[provider]["status"]
            if status == "active":
                return self._t("active"), "#69c58a"
            if status == "validating":
                return self._t("validating"), "#b9a66b"
            return self._t("not_configured"), "#d36f6f"

        def active_options():
            return [
                (provider, model)
                for provider in provider_ids
                if state[provider]["status"] == "active"
                for model in state[provider]["models"]
            ]

        def active_text_options():
            return [
                (provider, model)
                for provider in provider_ids
                if state[provider]["status"] == "active"
                for model in state[provider]["text_models"]
            ]

        # Models page is built once. Only its values and model rows change.
        models_inner = ctk.CTkFrame(pages["models"], fg_color="transparent")
        models_inner.pack(fill="both", expand=True, padx=24, pady=22)
        ctk.CTkLabel(models_inner, text=self._t("choose_model"), text_color=TEXT,
            font=font_title, anchor="w").pack(fill="x")
        ctk.CTkLabel(models_inner, text=self._t("model_subtitle"), text_color=DIM,
            font=font_label, anchor="w").pack(fill="x", pady=(2, 14))
        transcription_block = ctk.CTkFrame(models_inner, fg_color="transparent")
        transcription_block.pack(fill="x")
        ctk.CTkLabel(transcription_block, text=self._t("transcription_model"),
            text_color=DIM, font=font_label, anchor="w").pack(
                fill="x", padx=2, pady=(0, 4))
        picker = ctk.CTkButton(transcription_block, text="", anchor="w",
            width=360, height=38,
            corner_radius=11, fg_color="#141414", hover_color="#1d1d1d",
            border_width=1, border_color="#292929", text_color=TEXT,
            font=font_body)
        picker.pack(anchor="w")
        empty_message = ctk.CTkFrame(transcription_block, fg_color="#111111",
            corner_radius=11, border_width=1, border_color="#262626")
        ctk.CTkLabel(empty_message, text=self._t("no_active_models"), text_color=DIM,
            font=font_label, wraplength=390, justify="left").pack(
                anchor="w", padx=14, pady=(13, 7))
        empty_add = ctk.CTkButton(empty_message, text=self._t("add_provider"),
            width=140, height=30, corner_radius=15, fg_color="#242424",
            hover_color="#303030", text_color=TEXT)
        empty_add.pack(anchor="w", padx=12, pady=(0, 12))
        menu_shell = ctk.CTkFrame(transcription_block, width=360,
            fg_color="#111111", corner_radius=11, border_width=1,
            border_color="#292929")
        menu_rows = VirtualModelList(menu_shell, viewport_height=145)
        menu_rows.pack(fill="both", expand=True, padx=5, pady=5)

        multimodal_hint = ctk.CTkLabel(models_inner,
            text=self._t("multimodal_refinement"), text_color="#777777",
            font=font_label, anchor="w", justify="left", wraplength=430)

        refinement_block = ctk.CTkFrame(models_inner, fg_color="transparent")
        refinement_block.pack(fill="x", pady=(18, 0))
        ctk.CTkLabel(refinement_block, text=self._t("text_refinement_model"),
            text_color=DIM, font=font_label, anchor="w").pack(
                fill="x", padx=2, pady=(0, 2))
        ctk.CTkLabel(refinement_block, text=self._t("refinement_subtitle"),
            text_color="#666666", font=font_caption, anchor="w").pack(
                fill="x", padx=2, pady=(0, 5))
        refinement_picker = ctk.CTkButton(refinement_block, text="", anchor="w",
            width=360, height=38, corner_radius=11, fg_color="#141414",
            hover_color="#1d1d1d",
            border_width=1, border_color="#292929", text_color=TEXT,
            font=font_body)
        refinement_picker.pack(anchor="w")
        refinement_empty = ctk.CTkLabel(refinement_block,
            text=self._t("no_active_models"), text_color="#8a6666",
            font=font_caption, anchor="w", wraplength=430)
        refinement_menu_shell = ctk.CTkFrame(refinement_block, width=360,
            fg_color="#111111", corner_radius=11, border_width=1,
            border_color="#292929")
        refinement_menu_rows = VirtualModelList(
            refinement_menu_shell, viewport_height=145)
        refinement_menu_rows.pack(fill="both", expand=True, padx=5, pady=5)
        refinement_menu_visible = {"value": False}
        refinement_signature = {"value": None}

        # Settings intentionally contains only the Windows autostart flag.
        preferences_inner = ctk.CTkFrame(
            pages["settings"], fg_color="transparent")
        preferences_inner.pack(fill="both", expand=True, padx=24, pady=22)
        autostart_switch = ctk.CTkSwitch(
            preferences_inner, text=self._t("autostart"), height=26,
            switch_width=40, switch_height=20, corner_radius=10,
            border_width=1, fg_color="#171717", progress_color="#e7e7e7",
            button_color="#777777", text_color=TEXT, font=font_body)
        autostart_switch.pack(fill="x", anchor="w")
        if _is_autostart_enabled():
            autostart_switch.select()
        ctk.CTkLabel(
            preferences_inner, text=self._t("autostart_subtitle"),
            text_color=DIM, font=font_caption, anchor="w", justify="left",
            wraplength=430).pack(fill="x", padx=50, pady=(3, 0))

        saved_settings = {
            "transcription": (selected["provider"], selected["model"]),
            "refinement": (
                selected_refinement["provider"], selected_refinement["model"]),
            "autostart": bool(autostart_switch.get()),
        }

        def current_settings():
            return {
                "transcription": (selected["provider"], selected["model"]),
                "refinement": (
                    selected_refinement["provider"], selected_refinement["model"]),
                "autostart": bool(autostart_switch.get()),
            }

        def refresh_dirty_state():
            if current_settings() != saved_settings:
                if not undo_button.winfo_manager():
                    undo_button.pack(side="right", padx=(0, 8))
            else:
                undo_button.pack_forget()

        def restore_saved_settings():
            selected["provider"], selected["model"] = saved_settings["transcription"]
            (selected_refinement["provider"],
             selected_refinement["model"]) = saved_settings["refinement"]
            if saved_settings["autostart"]:
                autostart_switch.select()
            else:
                autostart_switch.deselect()
            model_menu_visible["value"] = False
            menu_shell.pack_forget()
            refinement_menu_visible["value"] = False
            refinement_menu_shell.pack_forget()
            refresh_model_ui(rebuild_menu=False)
            refresh_dirty_state()

        undo_button.configure(command=restore_saved_settings)
        autostart_switch.configure(command=refresh_dirty_state)

        # Statistics page reads only anonymous local counters (never transcripts).
        statistics_inner = ctk.CTkFrame(
            pages["statistics"], fg_color="transparent")
        statistics_inner.pack(fill="both", expand=True, padx=22, pady=18)
        ctk.CTkLabel(statistics_inner, text=self._t("statistics_title"),
            text_color=TEXT, font=font_title, anchor="w").pack(fill="x")
        ctk.CTkLabel(statistics_inner, text=self._t("statistics_subtitle"),
            text_color=DIM, font=font_label, anchor="w").pack(
                fill="x", pady=(1, 10))

        metrics_grid = ctk.CTkFrame(statistics_inner, fg_color="transparent")
        metrics_grid.pack(fill="x")
        metrics_grid.grid_columnconfigure((0, 1), weight=1)
        metric_specs = (
            ("recordings", "stat_recordings"),
            ("duration", "stat_recording_time"),
            ("cost", "stat_estimated_cost"),
            ("words", "stat_words"),
        )
        metric_values = {}
        for index, (metric_key, label_key) in enumerate(metric_specs):
            card = ctk.CTkFrame(metrics_grid, height=62, corner_radius=11,
                fg_color="#131313", border_width=1, border_color="#252525")
            card.grid(row=index // 2, column=index % 2, sticky="nsew",
                padx=(0, 4) if index % 2 == 0 else (4, 0), pady=4)
            card.grid_propagate(False)
            ctk.CTkLabel(card, text=self._t(label_key), text_color=DIM,
                font=font_caption, anchor="w").pack(fill="x", padx=12, pady=(8, 0))
            value_label = ctk.CTkLabel(card, text="—", text_color=TEXT,
                font=font_section, anchor="w")
            value_label.pack(fill="x", padx=12, pady=(0, 7))
            metric_values[metric_key] = value_label

        ctk.CTkLabel(statistics_inner, text=self._t("most_used_models"),
            text_color=TEXT, font=font_section, anchor="w").pack(
                fill="x", pady=(12, 5))
        models_list = ctk.CTkFrame(statistics_inner, fg_color="transparent")
        models_list.pack(fill="x")
        model_rows = []
        for _index in range(3):
            row = ctk.CTkFrame(models_list, height=31, corner_radius=8,
                fg_color="#121212")
            row.pack(fill="x", pady=2)
            row.pack_propagate(False)
            name_label = ctk.CTkLabel(row, text="", text_color="#e7e7e7",
                font=font_label, anchor="w")
            name_label.pack(side="left", fill="x", expand=True, padx=10)
            count_label = ctk.CTkLabel(row, text="", text_color=DIM,
                font=font_caption)
            count_label.pack(side="right", padx=10)
            model_rows.append((row, name_label, count_label))
        statistics_empty = ctk.CTkLabel(models_list,
            text=self._t("no_statistics"), text_color=DIM, font=font_label,
            anchor="w")

        statistics_extras = ctk.CTkLabel(statistics_inner, text="", text_color=DIM,
            font=font_caption, anchor="w")
        statistics_extras.pack(fill="x", pady=(8, 1))
        statistics_cost_note = ctk.CTkLabel(statistics_inner,
            text=self._t("cost_disclaimer"), text_color="#686868",
            font=font_caption, anchor="w", justify="left", wraplength=430)
        statistics_cost_note.pack(fill="x")

        def refresh_statistics():
            summary = _usage_summary()
            metric_values["recordings"].configure(text=str(summary["recordings"]))
            metric_values["duration"].configure(
                text=_format_duration(summary["total_seconds"]))
            cost = summary["total_cost_usd"]
            cost_text = f"≈ US$ {cost:.4f}" if cost < 0.01 else f"≈ US$ {cost:.2f}"
            metric_values["cost"].configure(text=cost_text)
            metric_values["words"].configure(text=f"{summary['total_words']:,}")

            ranked = summary["ranked_models"][:3]
            if ranked:
                statistics_empty.pack_forget()
            else:
                statistics_empty.pack(fill="x", pady=8)
            for index, (row, name_label, count_label) in enumerate(model_rows):
                if index >= len(ranked):
                    row.pack_forget()
                    continue
                (provider, model), count = ranked[index]
                name_label.configure(text=f"{model}  ·  {provider.title()}")
                count_label.configure(text=self._t("stat_uses").format(count=count))
                row.pack(fill="x", pady=2)
            statistics_extras.configure(text="  ·  ".join((
                f"{self._t('stat_average')}: {_format_duration(summary['average_seconds'])}",
                f"{self._t('stat_last_7_days')}: {summary['last_7_days']}",
                f"{self._t('stat_rewrites')}: {summary['rewrites']}",
            )))

        def select_model(provider, model):
            selected["provider"], selected["model"] = provider, model
            model_menu_visible["value"] = False
            menu_shell.pack_forget()
            refinement_menu_visible["value"] = False
            refinement_menu_shell.pack_forget()
            refresh_model_ui(rebuild_menu=False)
            refresh_dirty_state()

        def select_refinement_model(provider, model):
            selected_refinement["provider"] = provider
            selected_refinement["model"] = model
            refinement_menu_visible["value"] = False
            refinement_menu_shell.pack_forget()
            refresh_refinement_ui(rebuild_menu=False)
            refresh_dirty_state()

        def rebuild_model_menu(options):
            signature = tuple(options)
            model_signature["value"] = signature
            menu_rows.on_select = select_model
            menu_rows.on_add = lambda: show_page("providers")
            menu_rows.set_items(options, (selected["provider"], selected["model"]))

        def rebuild_refinement_menu(options):
            signature = tuple(options)
            refinement_signature["value"] = signature
            refinement_menu_rows.on_select = select_refinement_model
            refinement_menu_rows.on_add = lambda: show_page("providers")
            refinement_menu_rows.set_items(options, (
                selected_refinement["provider"], selected_refinement["model"]))

        def refresh_refinement_ui(rebuild_menu=True):
            if not active_options():
                refinement_block.pack_forget()
                refinement_menu_shell.pack_forget()
                refinement_menu_visible["value"] = False
                multimodal_hint.pack_forget()
                return
            if selected["provider"] == "gemini":
                multimodal_hint.pack(fill="x", pady=(18, 0))
            else:
                multimodal_hint.pack_forget()
            refinement_block.pack(fill="x", pady=(18, 0))
            options = active_text_options()
            current = (selected_refinement["provider"], selected_refinement["model"])
            refinement_pending = (
                state.get(selected_refinement["provider"], {}).get("status") == "validating")
            if options and current not in options and not refinement_pending:
                selected_refinement["provider"], selected_refinement["model"] = options[0]
            if rebuild_menu:
                rebuild_refinement_menu(options)
            else:
                refinement_menu_rows.set_items(options, (
                    selected_refinement["provider"], selected_refinement["model"]))
            if options:
                refinement_empty.pack_forget()
                refinement_picker.pack(anchor="w")
                refinement_picker.configure(
                    text=f"{selected_refinement['model']}     \u2304",
                    image=picker_images.get(selected_refinement["provider"]),
                    compound="left", state="normal")
            else:
                refinement_picker.pack_forget()
                refinement_menu_shell.pack_forget()
                refinement_menu_visible["value"] = False
                refinement_empty.pack(fill="x", pady=(5, 0))

        def refresh_model_ui(rebuild_menu=True):
            options = active_options()
            transcription_pending = (
                state.get(selected["provider"], {}).get("status") == "validating")
            if (options and (selected["provider"], selected["model"]) not in options
                    and not transcription_pending):
                selected["provider"], selected["model"] = options[0]
            if rebuild_menu:
                rebuild_model_menu(options)
            else:
                menu_rows.set_items(options, (selected["provider"], selected["model"]))
            if options:
                empty_message.pack_forget()
                picker.pack(anchor="w")
                picker.configure(text=f"{selected['model']}     \u2304",
                    image=picker_images.get(selected["provider"]),
                    compound="left", state="normal")
            else:
                picker.pack_forget()
                menu_shell.pack_forget()
                model_menu_visible["value"] = False
                empty_message.pack(fill="x")
            refresh_refinement_ui(rebuild_menu=rebuild_menu)

        def toggle_model_menu():
            if not active_options():
                return
            model_menu_visible["value"] = not model_menu_visible["value"]
            if model_menu_visible["value"]:
                refinement_menu_visible["value"] = False
                refinement_menu_shell.pack_forget()
                menu_shell.pack(anchor="w")
            else:
                menu_shell.pack_forget()
        picker.configure(command=toggle_model_menu)

        def toggle_refinement_menu():
            if not active_text_options():
                return
            refinement_menu_visible["value"] = not refinement_menu_visible["value"]
            if refinement_menu_visible["value"]:
                model_menu_visible["value"] = False
                menu_shell.pack_forget()
                refinement_menu_shell.pack(anchor="w")
            else:
                refinement_menu_shell.pack_forget()
        refinement_picker.configure(command=toggle_refinement_menu)

        model_refresh_job = {"id": None}

        def schedule_model_refresh():
            if model_refresh_job["id"] is not None:
                win.after_cancel(model_refresh_job["id"])
            def refresh_once():
                model_refresh_job["id"] = None
                refresh_model_ui()
            model_refresh_job["id"] = win.after(60, refresh_once)

        # Providers page and cards are also created once.
        providers_inner = ctk.CTkFrame(pages["providers"], fg_color="transparent")
        providers_inner.pack(fill="both", expand=True, padx=22, pady=20)
        ctk.CTkLabel(providers_inner, text=self._t("providers_section"), text_color=TEXT,
            font=font_title, anchor="w").pack(fill="x")
        ctk.CTkLabel(providers_inner, text=self._t("providers_subtitle"), text_color=DIM,
            font=font_label, anchor="w").pack(fill="x", pady=(2, 14))
        for provider in provider_ids:
            card = ctk.CTkFrame(providers_inner, height=50, corner_radius=11,
                fg_color="#121212", border_width=1, border_color="#242424")
            card.pack(fill="x", pady=3)
            card.pack_propagate(False)
            canvas = tk.Canvas(card, height=46, background="#121212",
                highlightthickness=0, borderwidth=0, cursor="hand2")
            canvas.pack(fill="both", expand=True, padx=2, pady=2)
            icon = card_canvas_images.get(provider)
            icon_id = None
            if icon is not None:
                canvas._provider_icon = icon
                icon_id = canvas.create_image(14, 0, image=icon, anchor="w")
            name_id = canvas.create_text(48, 0, text=provider_names[provider],
                fill="#eeeeee", font=(font_family, 10), anchor="w")
            status_id = canvas.create_text(420, 0, text="", fill="#777777",
                font=(font_family, 9), anchor="e")

            def center_card_content(event, icon_item=icon_id,
                    name_item=name_id, status_item=status_id):
                center_y = event.height / 2
                if icon_item is not None:
                    event.widget.coords(icon_item, 14, center_y)
                event.widget.coords(name_item, 48, center_y)
                event.widget.coords(
                    status_item, max(80, event.width - 14), center_y)

            canvas.bind("<Configure>", center_card_content)
            canvas.bind("<Enter>", lambda event:
                event.widget.configure(background="#1d1d1d"))
            canvas.bind("<Leave>", lambda event:
                event.widget.configure(background="#121212"))
            canvas.bind("<Button-1>", lambda _event, p=provider:
                show_page(f"detail:{p}"))
            card_buttons[provider] = card
            card_status_buttons[provider] = (canvas, status_id)

        def refresh_provider_ui(provider):
            status, color = status_presentation(provider)
            status_canvas, status_id = card_status_buttons[provider]
            status_canvas.itemconfigure(status_id, text=status, fill=color)
            if provider in detail_status_labels:
                detail_status_labels[provider].configure(text=status, text_color=color)
                error = state[provider]["error"]
                detail_messages[provider].configure(
                    text=(self._t("validation_failed").format(error=error) if error else ""))
                validating = state[provider]["status"] == "validating"
                validate_buttons[provider].configure(
                    state="disabled" if validating else "normal")
                if state[provider]["status"] == "active":
                    deactivate_buttons[provider].pack(side="right")
                else:
                    deactivate_buttons[provider].pack_forget()

        def validate_provider(provider, api_key, base_url, persist=False):
            provider_state = state[provider]
            provider_state["generation"] += 1
            generation = provider_state["generation"]
            provider_state.update(status="validating", error="")
            refresh_provider_ui(provider)

            def run():
                try:
                    catalog = _validate_provider_credentials(provider, api_key, base_url)
                    models = _parse_audio_models(provider, catalog)
                    if (provider == "openai" and "api.openai.com" in base_url.lower()
                            and not models):
                        models = list(OPENAI_OFFICIAL_AUDIO_MODELS)
                    if (provider == "groq" and "api.groq.com" in base_url.lower()
                            and not models):
                        models = list(GROQ_OFFICIAL_AUDIO_MODELS)
                    text_models = _parse_text_models(provider, catalog)
                    error = ""
                except Exception as exc:
                    models = []
                    text_models = []
                    if isinstance(exc, requests.HTTPError) and exc.response is not None:
                        error = f"HTTP {exc.response.status_code}"
                    else:
                        error = str(exc).replace("\n", " ")[:120]

                def finish():
                    if not win.winfo_exists() or generation != provider_state["generation"]:
                        return
                    if error:
                        provider_state.update(
                            status="not_configured", models=[], text_models=[], error=error)
                    else:
                        provider_state.update(
                            status="active", models=models,
                            text_models=text_models, error="")
                        if persist:
                            APP_CONFIG[f"{provider}_api_key"] = api_key
                            APP_CONFIG[f"{provider}_base_url"] = base_url
                            current_model = str(APP_CONFIG.get(
                                model_keys[provider], default_models[provider]))
                            if models and current_model not in models:
                                APP_CONFIG[model_keys[provider]] = models[0]
                            try:
                                _save_app_config()
                            except OSError as exc:
                                provider_state.update(
                                    status="not_configured", models=[],
                                    text_models=[], error=str(exc))
                    refresh_provider_ui(provider)
                    schedule_model_refresh()

                win.after(0, finish)
            threading.Thread(target=run, daemon=True).start()

        # Provider forms are built only when first opened, then remain persistent.
        def ensure_provider_detail(provider):
            if provider in detail_status_labels:
                return
            page = detail_pages[provider]
            inner = ctk.CTkFrame(page, fg_color="transparent")
            inner.pack(fill="both", expand=True, padx=22, pady=18)
            ctk.CTkButton(inner, text=f"\u2039  {self._t('back')}", width=70,
                height=26, anchor="w", fg_color="transparent", hover_color="#202020",
                text_color=DIM, command=lambda: show_page("providers")).pack(anchor="w")
            heading = ctk.CTkFrame(inner, fg_color="transparent")
            heading.pack(fill="x", pady=(12, 14))
            ctk.CTkLabel(heading, text="", image=images.get(provider), width=36).pack(
                side="left", padx=(0, 8))
            ctk.CTkLabel(heading, text=provider_names[provider], text_color=TEXT,
                font=font_section).pack(side="left")
            detail_status_labels[provider] = ctk.CTkLabel(heading, text="",
                font=font_caption)
            detail_status_labels[provider].pack(side="right")
            ctk.CTkLabel(inner, text=self._t("api_key"), text_color=DIM,
                font=font_label, anchor="w").pack(fill="x", padx=2, pady=(0, 4))
            key_entry = ctk.CTkEntry(inner, height=32, corner_radius=10,
                fg_color="#050505", text_color=TEXT, border_color=BORDER,
                border_width=1, show="\u2022", font=font_body)
            key_entry.pack(fill="x", pady=(0, 10))
            key_entry.insert(0, str(APP_CONFIG.get(f"{provider}_api_key", "")))
            saved_base = str(APP_CONFIG.get(f"{provider}_base_url", default_bases[provider]))
            custom = saved_base.rstrip("/").lower() != default_bases[provider].rstrip("/").lower()
            endpoint_switch = ctk.CTkSwitch(inner, text=self._t("custom_endpoint"),
                height=22, switch_width=36, switch_height=18, corner_radius=9,
                border_width=1, fg_color="#171717", progress_color="#e7e7e7",
                button_color="#777777", text_color=DIM, font=font_label)
            endpoint_switch.pack(fill="x", padx=2)
            endpoint_fields = ctk.CTkFrame(inner, fg_color="transparent")
            ctk.CTkLabel(endpoint_fields, text=self._t("base_url"), text_color=DIM,
                font=font_label, anchor="w").pack(fill="x", padx=2, pady=(8, 4))
            base_entry = ctk.CTkEntry(endpoint_fields, height=32, corner_radius=10,
                fg_color="#050505", text_color=TEXT, border_color=BORDER,
                border_width=1, font=font_body)
            base_entry.pack(fill="x")
            base_entry.insert(0, saved_base)
            if custom:
                endpoint_switch.select()
                endpoint_fields.pack(fill="x")
            endpoint_switch.configure(command=lambda s=endpoint_switch, f=endpoint_fields:
                f.pack(fill="x") if s.get() else f.pack_forget())
            message = ctk.CTkLabel(inner, text="", text_color="#d17878",
                font=font_caption, anchor="w", justify="left", wraplength=430)
            message.pack(fill="x", padx=2, pady=(8, 4))
            detail_messages[provider] = message
            actions = ctk.CTkFrame(inner, fg_color="transparent")
            actions.pack(fill="x", pady=(4, 0))
            validate_button = ctk.CTkButton(actions, text=self._t("validate_save"),
                width=130, height=32, corner_radius=16, fg_color="#ededed",
                hover_color="#ffffff", text_color="#050505",
                font=font_label)
            validate_button.pack(side="left")
            validate_buttons[provider] = validate_button
            deactivate_button = ctk.CTkButton(actions, text=self._t("deactivate"),
                height=32, corner_radius=16, fg_color="transparent",
                hover_color="#251717", border_width=1, border_color="#3a2222",
                text_color="#b67b7b", font=font_label)
            deactivate_buttons[provider] = deactivate_button
            detail_inputs[provider] = {
                "key": key_entry, "base": base_entry, "switch": endpoint_switch,
            }

            def validate_from_page(provider_id=provider):
                inputs = detail_inputs[provider_id]
                key = inputs["key"].get().strip()
                base = (inputs["base"].get().strip() if inputs["switch"].get()
                        else default_bases[provider_id])
                if not key:
                    state[provider_id]["error"] = self._t("api_key")
                    refresh_provider_ui(provider_id)
                    return
                validate_provider(provider_id, key, base or default_bases[provider_id],
                    persist=True)
            validate_button.configure(command=validate_from_page)

            def deactivate(provider_id=provider):
                state[provider_id]["generation"] += 1
                state[provider_id].update(
                    status="not_configured", models=[], text_models=[], error="")
                APP_CONFIG[f"{provider_id}_api_key"] = ""
                APP_CONFIG[f"{provider_id}_base_url"] = default_bases[provider_id]
                try:
                    _save_app_config()
                except OSError:
                    pass
                refresh_provider_ui(provider_id)
                refresh_model_ui()
                show_page("providers")
            deactivate_button.configure(command=deactivate)

        def page_frame(name):
            if name.startswith("detail:"):
                provider = name.split(":", 1)[1]
                ensure_provider_detail(provider)
                return detail_pages[provider]
            return pages[name]

        def show_page(name):
            if current_page["name"] == name:
                return
            if current_page["name"] is not None:
                page_frame(current_page["name"]).pack_forget()
            model_menu_visible["value"] = False
            menu_shell.pack_forget()
            refinement_menu_visible["value"] = False
            refinement_menu_shell.pack_forget()
            current_page["name"] = name
            if name == "statistics":
                refresh_statistics()
            page_frame(name).pack(fill="both", expand=True)
            section = "providers" if name.startswith("detail:") else name
            header_title.configure(text=(provider_names[name.split(":", 1)[1]]
                if name.startswith("detail:") else self._t(f"{name}_section")))
            for nav_name, button in nav_buttons.items():
                button.configure(
                    fg_color="#1b1b1b" if nav_name == section else "transparent",
                    text_color=TEXT if nav_name == section else DIM)

        empty_add.configure(command=lambda: show_page("providers"))
        nav_buttons["models"] = ctk.CTkButton(sidebar,
            text=self._t("models_section"), anchor="w", height=38,
            corner_radius=9, fg_color="#1b1b1b", hover_color="#242424",
            text_color=TEXT, font=font_body,
            command=lambda: show_page("models"))
        nav_buttons["models"].pack(fill="x", padx=9, pady=(12, 3))
        nav_buttons["providers"] = ctk.CTkButton(sidebar,
            text=self._t("providers_section"), anchor="w", height=38,
            corner_radius=9, fg_color="transparent", hover_color="#242424",
            text_color=DIM, font=font_body,
            command=lambda: show_page("providers"))
        nav_buttons["providers"].pack(fill="x", padx=9, pady=3)
        nav_buttons["statistics"] = ctk.CTkButton(sidebar,
            text=self._t("statistics_section"), anchor="w", height=38,
            corner_radius=9, fg_color="transparent", hover_color="#242424",
            text_color=DIM, font=font_body,
            command=lambda: show_page("statistics"))
        nav_buttons["statistics"].pack(fill="x", padx=9, pady=3)
        nav_buttons["settings"] = ctk.CTkButton(sidebar,
            text=self._t("settings_section"), anchor="w", height=38,
            corner_radius=9, fg_color="transparent", hover_color="#242424",
            text_color=DIM, font=font_body,
            command=lambda: show_page("settings"))
        nav_buttons["settings"].pack(fill="x", padx=9, pady=3)

        apply_feedback_job = {"id": None, "active": False}
        apply_check_images = []
        for frame_index in range(8):
            alpha = round(255 * frame_index / 7)
            source = _make_checkmark_image(20, (5, 5, 5, alpha))
            apply_check_images.append(ctk.CTkImage(
                light_image=source, dark_image=source, size=(20, 20)))
        apply_check_label = ctk.CTkLabel(
            apply_button, text="", width=22, height=22,
            fg_color="#f5f5f5", image=apply_check_images[0])

        def animate_apply_confirmation():
            if apply_feedback_job["id"] is not None:
                try:
                    win.after_cancel(apply_feedback_job["id"])
                except tk.TclError:
                    pass
            apply_feedback_job["active"] = True
            apply_button.configure(text="", image=None)
            apply_check_label.configure(image=apply_check_images[0])
            apply_check_label.place(relx=0.5, rely=0.5, anchor="center")
            apply_check_label.lift()

            def fade_step(step=0, reverse=False):
                if not win.winfo_exists():
                    return
                progress = min(1.0, step / 7)
                if reverse:
                    progress = 1.0 - progress
                frame_index = max(0, min(7, round(progress * 7)))
                apply_check_label.configure(image=apply_check_images[frame_index])
                if step < 7:
                    apply_feedback_job["id"] = win.after(
                        18, lambda: fade_step(step + 1, reverse))
                elif not reverse:
                    apply_feedback_job["id"] = win.after(
                        520, lambda: fade_step(0, True))
                else:
                    apply_feedback_job["id"] = None
                    apply_feedback_job["active"] = False
                    apply_check_label.place_forget()
                    apply_button.configure(
                        text=self._t("apply"), text_color="#050505",
                        font=font_body)

            fade_step()

        def apply_settings():
            if apply_feedback_job["active"]:
                return
            _apply_selected_models(
                selected, selected_refinement, active_options(),
                active_text_options(), model_keys)
            try:
                _set_autostart(bool(autostart_switch.get()))
                _save_app_config()
            except OSError:
                return
            saved_settings.clear()
            saved_settings.update(current_settings())
            refresh_dirty_state()
            animate_apply_confirmation()
        apply_button.configure(command=apply_settings)

        for provider in provider_ids:
            refresh_provider_ui(provider)
        refresh_model_ui()
        show_page("models")
        _configure_windows_tool_window(win)
        _fade_in_window(win)
        win.lift()
        win.focus()
        win.after_idle(lambda: _apply_windows_rounded_corners(win))

        # Existing keys are revalidated in the background. Each completion
        # updates only that provider's labels and the model menu.
        for provider in provider_ids:
            key = str(APP_CONFIG.get(f"{provider}_api_key", "")).strip()
            if key:
                validate_provider(provider, key,
                    str(APP_CONFIG.get(f"{provider}_base_url", default_bases[provider])))

    # -- Visibility --
    def _toggle_visibility(self):
        if self.winfo_viewable(): self.withdraw()
        else: self._show_without_activation()


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ClarifyVoice")
    subparsers = parser.add_subparsers(dest="command")

    transcribe = subparsers.add_parser("transcribe", help="Transcribe an existing audio file")
    transcribe.add_argument("--file", required=True, help="Path to an audio file")
    transcribe.add_argument("--mode", choices=["transcription", "prompt"], default="transcription")
    transcribe.add_argument("--lang", choices=["en", "pt"], default="en")

    stdin_transcribe = subparsers.add_parser("headless-transcribe-stdin", help="Transcribe PCM16 mono 16kHz from stdin")
    stdin_transcribe.add_argument("--mode", choices=["transcription", "prompt"], default="transcription")
    stdin_transcribe.add_argument("--lang", choices=["en", "pt"], default="en")

    return parser


def _run_cli(argv: list[str]) -> int:
    parser = _build_cli_parser()
    args = parser.parse_args(argv)

    if args.command not in ("transcribe", "headless-transcribe-stdin"):
        parser.print_help()
        return 0

    if args.command == "headless-transcribe-stdin":
        raw_audio = sys.stdin.buffer.read()
        if not raw_audio:
            print(json.dumps({
                "ok": False,
                "error": "no_audio_stdin",
            }, ensure_ascii=False))
            return 1

        temp_path = DATA_DIR / f"headless_stdin_{int(time.time() * 1000)}.wav"
        try:
            with wave.open(str(temp_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(raw_audio)

            text = call_transcription_provider(temp_path, args.mode, args.lang)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass

        if text.startswith("[Error"):
            print(json.dumps({
                "ok": False,
                "error": text,
                "mode": args.mode,
                "lang": args.lang,
            }, ensure_ascii=False))
            return 1

        print(json.dumps({
            "ok": True,
            "text": text,
            "mode": args.mode,
            "lang": args.lang,
        }, ensure_ascii=False))
        return 0

    audio_path = Path(args.file).expanduser().resolve()
    if not audio_path.exists() or not audio_path.is_file():
        print(json.dumps({
            "ok": False,
            "error": "audio_file_not_found",
            "file": str(audio_path),
        }, ensure_ascii=False))
        return 1

    text = call_transcription_provider(audio_path, args.mode, args.lang)
    if text.startswith("[Error"):
        print(json.dumps({
            "ok": False,
            "error": text,
            "file": str(audio_path),
            "mode": args.mode,
            "lang": args.lang,
        }, ensure_ascii=False))
        return 1

    print(json.dumps({
        "ok": True,
        "text": text,
        "file": str(audio_path),
        "mode": args.mode,
        "lang": args.lang,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    start_hidden = sys.argv[1:] == ["--hidden"]
    if len(sys.argv) > 1 and not start_hidden:
        raise SystemExit(_run_cli(sys.argv[1:]))

    instance_guard = SingleInstanceGuard.acquire()
    if instance_guard is None:
        raise SystemExit(0)

    app = App(start_hidden=start_hidden)
    app._single_instance_guard = instance_guard
    instance_guard.start_activation_listener(
        lambda: app.after(0, app._show_if_hidden))
    selected_key = APP_CONFIG.get(
        f"{APP_CONFIG.get('transcription_provider', 'gemini')}_api_key", "")
    if not start_hidden and not str(selected_key).strip():
        app.after(300, app._open_settings)
    app.mainloop()
