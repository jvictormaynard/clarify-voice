import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import install_bootstrap_tools


class BootstrapToolTests(unittest.TestCase):
    def test_extracts_both_pins_in_stable_order(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lock.txt"
            lock.write_text(
                "setuptools==83.0.0\npip==25.3\npip-tools==7.6.0\n", encoding="utf-8"
            )
            self.assertEqual(
                install_bootstrap_tools._pins(lock),
                ["pip==25.3", "setuptools==83.0.0", "pip-tools==7.6.0"],
            )

    def test_rejects_lock_without_bootstrap_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "lock.txt"
            lock.write_text("requests==2.0\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "pip, pip-tools, setuptools"):
                install_bootstrap_tools._pins(lock)

    def test_installs_exact_pins(self):
        with patch.object(install_bootstrap_tools.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(
                install_bootstrap_tools.main(
                    ["--lock-file", "requirements-lock-linux.txt"]
                ),
                0,
            )
        install_command = run.call_args_list[0].args[0]
        self.assertEqual(install_command[-5], "-c")
        self.assertTrue(install_command[-4].endswith("requirements-lock-linux.txt"))
        self.assertEqual(
            install_command[-3:],
            ["pip==25.3", "setuptools==83.0.0", "pip-tools==7.6.0"],
        )
        self.assertEqual(
            run.call_args_list[1].args[0][-4:],
            ["-m", "piptools", "compile", "--help"],
        )


if __name__ == "__main__":
    unittest.main()
