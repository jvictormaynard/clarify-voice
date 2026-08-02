#!/usr/bin/env python3
"""Maintainer harness for the optional local-ASR sidecar groundwork."""

from __future__ import annotations

import argparse
import ctypes
import json
import platform
import re
import sys
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_asr import (  # noqa: E402
    LocalASRError,
    LocalASRInstaller,
    LocalASRSidecarManager,
)


def _json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _installer(args) -> LocalASRInstaller:
    return LocalASRInstaller(root=Path(args.root).expanduser() if args.root else None)


def _require_windows() -> None:
    if platform.system() != "Windows":
        raise LocalASRError(
            "This pinned sidecar is the Windows x64 build. Run the harness with Windows Python.")


def _confirm(message: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"{message} [y/N] ").strip().casefold() in ("y", "yes")


def _progress(stage: str, current: int, total: int) -> None:
    percent = 100.0 if total <= 0 else current * 100.0 / total
    print(f"\r{stage}: {percent:6.2f}%", end="", file=sys.stderr, flush=True)
    if current >= total:
        print(file=sys.stderr)


def _audio_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / max(1, audio.getframerate())


def _words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.casefold(), flags=re.UNICODE)


def _word_error_rate(reference: str, hypothesis: str) -> float:
    expected = _words(reference)
    actual = _words(hypothesis)
    if not expected:
        return 0.0 if not actual else 1.0
    previous = list(range(len(actual) + 1))
    for expected_word in expected:
        current = [previous[0] + 1]
        for index, actual_word in enumerate(actual, start=1):
            current.append(min(
                current[-1] + 1,
                previous[index] + 1,
                previous[index - 1] + (expected_word != actual_word),
            ))
        previous = current
    return previous[-1] / len(expected)


def _windows_working_set(pid: int | None) -> int | None:
    if platform.system() != "Windows" or not pid:
        return None
    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]
    from ctypes import wintypes
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    process = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not process:
        return None
    try:
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
                process, ctypes.byref(counters), counters.cb):
            return None
        return int(counters.PeakWorkingSetSize)
    finally:
        kernel32.CloseHandle(process)


def _benchmark(args) -> dict:
    _require_windows()
    installer = _installer(args)
    installer.verify()
    audio_path = Path(args.file).expanduser().resolve()
    if not audio_path.is_file():
        raise LocalASRError(f"Audio file does not exist: {audio_path}")
    manager = LocalASRSidecarManager(installer, idle_seconds=0)
    stop_sampling = threading.Event()
    memory_samples: list[int] = []

    def sample_memory():
        while not stop_sampling.wait(0.02):
            current = _windows_working_set(manager.process_id)
            if current is not None:
                memory_samples.append(current)

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()
    try:
        started = time.perf_counter()
        startup_seconds = manager.start()
        inference_started = time.perf_counter()
        text = manager.transcribe(audio_path, args.language)
        inference_seconds = time.perf_counter() - inference_started
        total_seconds = time.perf_counter() - started
    finally:
        stop_sampling.set()
        sampler.join(timeout=1)
        manager.shutdown()
    duration = _audio_duration(audio_path)
    expected = args.expected_text or ""
    return {
        "platform": platform.platform(),
        "engine": installer.manifest["engine"]["version"],
        "model": installer.manifest["recommended_model"]["id"],
        "audio_file": str(audio_path),
        "audio_seconds": round(duration, 3),
        "startup_seconds": round(startup_seconds, 3),
        "inference_seconds": round(inference_seconds, 3),
        "total_seconds": round(total_seconds, 3),
        "real_time_factor": round(inference_seconds / duration, 3) if duration else None,
        "peak_working_set_bytes": max(memory_samples) if memory_samples else None,
        "transcript": text,
        "reference": expected or None,
        "word_error_rate": round(_word_error_rate(expected, text), 4) if expected else None,
        "offline_network_disabled": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ClarifyVoice local-ASR maintainer harness (not product integration)")
    parser.add_argument(
        "--root", help="Override the asset root (recommended for isolated acceptance runs)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Verify installed files without network access")

    install = commands.add_parser("install", help="Download and verify the pinned assets")
    install.add_argument("--yes", action="store_true", help="Accept requirements non-interactively")

    remove = commands.add_parser("remove", help="Remove every local-ASR asset under the root")
    remove.add_argument("--yes", action="store_true", help="Confirm removal non-interactively")

    transcribe = commands.add_parser("transcribe", help="Run one local transcription")
    transcribe.add_argument("--file", required=True)
    transcribe.add_argument("--language", default="en")

    benchmark = commands.add_parser(
        "benchmark", help="Measure startup, latency, memory, and optional WER on Windows")
    benchmark.add_argument("--file", required=True)
    benchmark.add_argument("--language", default="en")
    benchmark.add_argument("--expected-text", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        installer = _installer(args)
        if args.command == "status":
            status = installer.status()
            _json(status)
            return 0 if status["state"] == "installed" else 1
        if args.command == "install":
            _require_windows()
            requirements = installer.requirements()
            print(json.dumps(requirements, indent=2), file=sys.stderr)
            if not _confirm("Download and install these optional assets?", args.yes):
                print("Installation cancelled; no download started.", file=sys.stderr)
                return 2
            _json(installer.install(_progress))
            return 0
        if args.command == "remove":
            if not _confirm(f"Remove all assets under {installer.root}?", args.yes):
                print("Removal cancelled.", file=sys.stderr)
                return 2
            _json({"removed": installer.remove(), "path": str(installer.root)})
            return 0
        if args.command == "transcribe":
            _require_windows()
            manager = LocalASRSidecarManager(installer)
            try:
                text = manager.transcribe(
                    Path(args.file).expanduser().resolve(), args.language)
            finally:
                manager.shutdown()
            _json({"ok": True, "text": text, "loopback_only": True})
            return 0
        if args.command == "benchmark":
            _json(_benchmark(args))
            return 0
    except LocalASRError as error:
        _json({"ok": False, "error": str(error)})
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
