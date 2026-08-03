import ast
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from spikes.pyside6.model import FakeWorkflow, Surface


ROOT = Path(__file__).resolve().parents[1]
SPIKE = ROOT / "spikes" / "pyside6"
FIXTURES = ROOT / "tests" / "fixtures" / "pyside6"


class PySide6SpikeTests(unittest.TestCase):
    def test_fake_workflow_covers_the_bounded_surface_flow(self):
        workflow = FakeWorkflow()
        workflow = workflow.transition("record")
        self.assertEqual(workflow.surface, Surface.RECORDING)
        workflow = workflow.transition("process").transition("complete")
        self.assertEqual(workflow.surface, Surface.SUCCESS)
        workflow = workflow.transition("show_result")
        self.assertEqual(workflow.surface, Surface.RESULT)
        workflow = workflow.transition("reset").open_settings()
        self.assertEqual(workflow.surface, Surface.SETTINGS)
        self.assertIn("provider", workflow.result)

    def test_show_result_is_gated_until_success(self):
        with self.assertRaises(ValueError):
            FakeWorkflow().transition("show_result")
        source = (SPIKE / "app.py").read_text(encoding="utf-8")
        self.assertIn("self._result_button.setEnabled(surface == Surface.SUCCESS)", source)

    def test_tray_and_initial_pill_lifecycle_are_explicit(self):
        source = (SPIKE / "app.py").read_text(encoding="utf-8")
        self.assertIn("app.setQuitOnLastWindowClosed(False)", source)
        self.assertIn("self._pill.hide()", source)
        self.assertIn("event.ignore()", source)
        self.assertIn("def reveal(self)", source)
        self.assertIn("def showEvent(self, event)", source)

    def test_benchmark_polls_the_process_tree_for_the_real_window(self):
        source = (SPIKE / "benchmark.ps1").read_text(encoding="utf-8")
        self.assertIn("function Get-TreeWindowProcess", source)
        self.assertIn("Get-TreeWindowProcess $process.Id", source)
        self.assertIn("$candidate.MainWindowHandle", source)
        self.assertIn("WindowProcessId", source)
        self.assertIn("finally", source)
        self.assertIn("$process.HasExited", source)

    def test_measurement_protocol_has_no_fixed_target_order(self):
        benchmark = (SPIKE / "benchmark.ps1").read_text(encoding="utf-8")
        aggregate = (SPIKE / "aggregate.ps1").read_text(encoding="utf-8")
        readme = (SPIKE / "README.md").read_text(encoding="utf-8")
        self.assertIn('[ValidateSet("CustomTkinter", "PySide6")]', benchmark)
        self.assertIn("[string]$RunId", benchmark)
        self.assertIn("[int]$Round", benchmark)
        self.assertIn("LastBootUpTime", benchmark)
        self.assertIn("function Get-HostId", benchmark)
        self.assertIn("MachineGuid", benchmark)
        self.assertIn("SHA256", benchmark)
        self.assertIn("HostId = Get-HostId", benchmark)
        self.assertNotIn("CustomTkinterExecutable", benchmark)
        self.assertNotIn("PySide6Executable", benchmark)
        self.assertIn("$bootIds.Count -lt 3", aggregate)
        self.assertIn("ConvertTo-CanonicalHostId", aggregate)
        self.assertIn("$hostIds.Count -ne 1", aggregate)
        self.assertIn("alternating which target is measured first", readme)
        self.assertIn("does not claim a perfectly controlled OS cold state", readme)

    def test_aggregate_fail_closes_invalid_rows_and_excludes_its_output(self):
        aggregate = (SPIKE / "aggregate.ps1").read_text(encoding="utf-8")
        self.assertIn("ConvertTo-ValidMeasurement", aggregate)
        self.assertIn("MainWindowSeen is not true", aggregate)
        self.assertIn("ConvertTo-PositiveInteger", aggregate)
        self.assertIn("ConvertTo-CanonicalBootId", aggregate)
        self.assertIn("$Row.BootId = ConvertTo-CanonicalBootId", aggregate)
        self.assertIn("$Value -cnotmatch $pattern", aggregate)
        self.assertIn('$zone -ceq "Z"', aggregate)
        self.assertIn("$Row.Round = $round", aggregate)
        for metric in ("WindowProcessId", "ProcessCount", "ThreadCount"):
            self.assertIn(f'@("WindowProcessId", "ProcessCount", "ThreadCount")', aggregate)
            self.assertIn(metric, aggregate)
        self.assertIn("InvariantCulture", aggregate)
        self.assertIn("$resolvedInput.ToLowerInvariant() -eq $destinationKey", aggregate)
        self.assertTrue((FIXTURES / "valid_measurements.csv").is_file())
        self.assertTrue((FIXTURES / "invalid_measurements.csv").is_file())
        self.assertTrue((FIXTURES / "invalid_round_spellings.csv").is_file())
        self.assertTrue((FIXTURES / "invalid_boot_spellings.csv").is_file())
        self.assertTrue((FIXTURES / "invalid_boot_id.csv").is_file())
        self.assertTrue((FIXTURES / "invalid_boot_lowercase_z.csv").is_file())
        self.assertTrue((FIXTURES / "invalid_mixed_hosts.csv").is_file())
        self.assertTrue((FIXTURES / "invalid_host_id.csv").is_file())
        self.assertTrue((FIXTURES / "invalid_host_missing.csv").is_file())
        for fixture in FIXTURES.glob("invalid_*_process_id.csv"):
            self.assertTrue(fixture.is_file())
        for name in (
            "invalid_fractional_process_count.csv",
            "invalid_fractional_thread_count.csv",
            "invalid_zero_process_count.csv",
            "invalid_zero_thread_count.csv",
            "invalid_overflow_window_process_id.csv",
            "invalid_overflow_process_count.csv",
            "invalid_overflow_thread_count.csv",
        ):
            self.assertTrue((FIXTURES / name).is_file())

    @unittest.skipUnless(os.name == "nt", "native PowerShell integration runs on Windows CI")
    def test_aggregate_windows_rejects_failures_and_is_idempotent(self):
        script = SPIKE / "aggregate.ps1"
        valid = FIXTURES / "valid_measurements.csv"
        invalid = FIXTURES / "invalid_measurements.csv"
        with tempfile.TemporaryDirectory() as temp_dir:
            summary = Path(temp_dir) / "summary.csv"
            def run_aggregate(inputs):
                def quote(path):
                    return "'" + str(path).replace("'", "''") + "'"

                command = (
                    f"& {quote(script)} -InputCsv @({','.join(quote(path) for path in inputs)}) "
                    f"-OutputCsv {quote(summary)}"
                )
                return subprocess.run(
                    [
                        "powershell.exe", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-Command", command,
                    ], capture_output=True, text=True, check=False,
                )

            first = run_aggregate([valid])
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            rerun = run_aggregate([valid, summary])
            self.assertEqual(rerun.returncode, 0, rerun.stderr or rerun.stdout)
            failed = run_aggregate([invalid])
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("MainWindowSeen", failed.stdout + failed.stderr)
            equivalent_rounds = run_aggregate([FIXTURES / "invalid_round_spellings.csv"])
            self.assertNotEqual(equivalent_rounds.returncode, 0)
            self.assertIn("three independent", equivalent_rounds.stdout + equivalent_rounds.stderr)
            equivalent_boots = run_aggregate([FIXTURES / "invalid_boot_spellings.csv"])
            self.assertNotEqual(equivalent_boots.returncode, 0)
            self.assertIn("three independent", equivalent_boots.stdout + equivalent_boots.stderr)
            invalid_boot = run_aggregate([FIXTURES / "invalid_boot_id.csv"])
            self.assertNotEqual(invalid_boot.returncode, 0)
            self.assertIn("BootId", invalid_boot.stdout + invalid_boot.stderr)
            lowercase_boot = run_aggregate([FIXTURES / "invalid_boot_lowercase_z.csv"])
            self.assertNotEqual(lowercase_boot.returncode, 0)
            self.assertIn("BootId", lowercase_boot.stdout + lowercase_boot.stderr)
            mixed_hosts = run_aggregate([FIXTURES / "invalid_mixed_hosts.csv"])
            self.assertNotEqual(mixed_hosts.returncode, 0)
            self.assertIn("exactly one benchmark host", mixed_hosts.stdout + mixed_hosts.stderr)
            invalid_host = run_aggregate([FIXTURES / "invalid_host_id.csv"])
            self.assertNotEqual(invalid_host.returncode, 0)
            self.assertIn("HostId", invalid_host.stdout + invalid_host.stderr)
            missing_host = run_aggregate([FIXTURES / "invalid_host_missing.csv"])
            self.assertNotEqual(missing_host.returncode, 0)
            self.assertIn("HostId", missing_host.stdout + missing_host.stderr)
            for malformed in sorted(FIXTURES.glob("invalid_*_process_id.csv")) + sorted(
                FIXTURES.glob("invalid_*_process_count.csv")
            ) + sorted(FIXTURES.glob("invalid_*_thread_count.csv")):
                rejected = run_aggregate([malformed])
                self.assertNotEqual(rejected.returncode, 0, malformed.name)

    def test_spike_isolated_from_production_modules(self):
        source = (SPIKE / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        )
        self.assertNotIn("app", imported)
        self.assertNotIn("desktop_state", imported)
        self.assertNotIn("windows_hotkeys", imported)
        self.assertNotIn("requests", imported)
        self.assertNotIn("sounddevice", imported)

    def test_spike_entry_points_and_docs_are_present(self):
        for relative in (
            "__init__.py",
            "app.py",
            "model.py",
            "README.md",
            "requirements.txt",
            "package.ps1",
            "benchmark.ps1",
            "aggregate.ps1",
            ".gitignore",
            "evidence-template.md",
        ):
            self.assertTrue((SPIKE / relative).is_file(), relative)
        decision = (ROOT / "docs" / "pyside6-decision.md").read_text(encoding="utf-8")
        for phrase in (
            "provisional defer",
            "Licensing and redistribution",
            "Migration and rollback outline",
            "Recommendation",
            "pending independent rounds",
            "12–20 engineer-days",
            "Decision gate",
            "artifacts-manifest.json",
        ):
            self.assertIn(phrase, decision)

    def test_evidence_template_is_explicit_about_manual_hooks(self):
        evidence = (SPIKE / "evidence-template.md").read_text(encoding="utf-8")
        for phrase in (
            "three distinct `BootId`",
            "100% DPI screenshot",
            "Production global-hotkey coexistence",
            "Do not commit executable artifacts",
            "defer",
        ):
            self.assertIn(phrase, evidence)

    def test_spike_packaging_does_not_use_production_output_paths(self):
        package = (SPIKE / "package.ps1").read_text(encoding="utf-8")
        self.assertIn('"artifacts"', package)
        self.assertNotIn('"dist"', package)
        self.assertNotIn("scripts\\build.ps1", package)
        self.assertIn("requirements-lock-runtime-windows.txt", package)
        self.assertIn("build-environment.txt", package)
        self.assertIn("artifacts-manifest.json", package)
        self.assertIn("Get-FileHash", package)

    def test_production_requirements_do_not_gain_optional_qt_dependency(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertNotIn("PySide6", requirements)
        self.assertNotIn("pyside6", requirements)


if __name__ == "__main__":
    unittest.main()
