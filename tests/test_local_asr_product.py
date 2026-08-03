import threading
import time
import unittest

from local_asr import LocalASRCancelledError
from local_asr_product import (
    LocalASRProductController,
    format_requirements,
)


class FakeInstaller:
    def __init__(self):
        self.requirements_value = {
            "platform": "Windows x64",
            "memory_bytes": 852_000_000,
            "disk_bytes": 510_000_000,
            "download_bytes": 495_584_068,
        }
        self.install_calls = 0
        self.remove_calls = 0
        self.cancel_seen = threading.Event()
        self.release = threading.Event()
        self._status = "not_installed"

    def requirements(self):
        return dict(self.requirements_value)

    def status(self):
        return {
            "state": self._status,
            "detail": "not installed" if self._status == "not_installed" else "ready",
            "requirements": self.requirements(),
        }

    def install(self, callback=None, cancel_event=None):
        self.install_calls += 1
        if callback:
            callback("download:model", 10, 100)
        while not self.release.wait(0.01):
            if cancel_event is not None and cancel_event.is_set():
                self.cancel_seen.set()
                raise LocalASRCancelledError("cancelled")
        self._status = "installed"
        return self.status()

    def remove(self):
        self.remove_calls += 1
        self._status = "not_installed"
        return True


class LocalASRProductControllerTests(unittest.TestCase):
    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition did not complete")

    def test_constructor_is_read_only_and_install_is_explicit(self):
        installer = FakeInstaller()
        states = []
        controller = LocalASRProductController(installer, listener=states.append)

        self.assertEqual(installer.install_calls, 0)
        self.assertEqual(controller.state.status, "not_installed")
        controller.install_async()
        self.wait_for(lambda: installer.install_calls == 1)
        self.assertEqual(controller.state.status, "installing")
        self.assertTrue(any(state.stage == "download:model" for state in states))
        installer.release.set()
        self.wait_for(lambda: controller.state.status == "installed")
        self.assertEqual(controller.state.fraction, 0.0)

    def test_cancel_propagates_to_installer_and_leaves_no_worker(self):
        installer = FakeInstaller()
        controller = LocalASRProductController(installer)
        controller.install_async()
        self.wait_for(lambda: installer.install_calls == 1)
        controller.cancel()
        self.wait_for(lambda: controller.state.status == "cancelled")
        self.assertTrue(installer.cancel_seen.is_set())
        self.assertFalse(controller.busy)

    def test_remove_is_explicit_and_reports_not_installed(self):
        installer = FakeInstaller()
        installer._status = "installed"
        controller = LocalASRProductController(installer)
        controller.remove_async()
        self.wait_for(lambda: controller.state.status == "not_installed")
        self.assertEqual(installer.remove_calls, 1)

    def test_requirement_summary_is_human_readable(self):
        summary = format_requirements({
            "platform": "Windows x64",
            "memory_bytes": 852_000_000,
            "disk_bytes": 510_000_000,
            "download_bytes": 495_584_068,
        })
        self.assertIn("Windows x64", summary)
        self.assertIn("MiB", summary)
        self.assertIn("download", summary)


if __name__ == "__main__":
    unittest.main()
