import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import check_dependency_lock


class DependencyLockCheckerTests(unittest.TestCase):
    def test_compile_command_is_seeded_with_versioned_pins(self):
        observed = {}

        def fake_compile(command, **_kwargs):
            output_path = Path(command[command.index("--output-file") + 1])
            observed["seeded"] = output_path.is_file()
            observed["pins"] = "requests==2.34.2" in output_path.read_text()
            observed["upgrade"] = "--upgrade" in command
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(check_dependency_lock.subprocess, "run", fake_compile):
            result = check_dependency_lock.main(
                ["--lock-file", "requirements-lock-linux.txt"]
            )

        self.assertEqual(result, 0)
        self.assertTrue(observed["seeded"])
        self.assertTrue(observed["pins"])
        self.assertFalse(observed["upgrade"])


if __name__ == "__main__":
    unittest.main()
