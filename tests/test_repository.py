import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositorySafetyTests(unittest.TestCase):
    def test_dependabot_uses_versioning_strategy_only_where_supported(self):
        config = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        pip_section, actions_section = config.split(
            "  - package-ecosystem: github-actions", 1
        )
        self.assertIn("versioning-strategy: increase-if-necessary", pip_section)
        self.assertNotIn("versioning-strategy:", actions_section)

    def test_windows_builds_never_bundle_dotenv(self):
        scripts = [
            ROOT / "scripts" / "build.ps1",
            ROOT / "scripts" / "deploy.ps1",
            ROOT / "build.bat",
        ]
        for script in scripts:
            content = script.read_text(encoding="utf-8")
            self.assertNotIn('"--add-data", "${envFile};."', content)
            self.assertNotIn('--add-data ".env;."', content)

    def test_deploy_stages_all_python_modules(self):
        content = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
        self.assertIn('Join-Path $repoRoot "*.py"', content)
        self.assertIn("${distribution};distribution", content)
        self.assertNotIn('Join-Path $soxDir "*.txt"', content)
        self.assertNotIn('Join-Path $soxDir "LICENSE.GPL.txt"', content)

    def test_deploy_uses_isolated_repository_requirements(self):
        content = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
        self.assertIn('$venvDir = Join-Path $buildRoot "venv"', content)
        self.assertIn(
            '$requirementsFile = Join-Path $requirementsDir "requirements-dev.txt"',
            content,
        )
        self.assertIn('"-r", $requirementsFile, "-c", $lockFile', content)
        self.assertIn("Build dependencies: $dependencyVersions", content)
        self.assertNotIn(
            '"requests", "sounddevice", "customtkinter", "Pillow", "pyinstaller"',
            content,
        )

    def test_build_setup_bootstraps_the_locked_toolchain_inside_venv(self):
        content = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")
        self.assertIn('"install_bootstrap_tools.py"', content)
        self.assertIn('"requirements-lock-windows.txt"', content)
        self.assertIn('"Could not install the pinned bootstrap tools."', content)

    def test_release_publishes_verified_sox_source(self):
        content = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("sox-14.4.2-source.tar.gz", content)
        self.assertIn(
            "b45f598643ffbd8e363ff24d61166ccec4836fea6d3888881b8df53e3bb55f6c",
            content,
        )

    def test_release_requires_managed_signing_and_provenance(self):
        content = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        for required in (
            "environment: release-signing",
            "id-token: write",
            "azure/login@532459ea530d8321f2fb9bb10d1e0bcf23869a43 # v3",
            "azure/artifact-signing-action@c7ab2a863ab5f9a846ddb8265964877ef296ee82 # v2",
            "scripts\\verify-signature.ps1",
            "actions/attest-build-provenance@",
            "ClarifyVoice-windows-x64.msi.sha256",
            "ClarifyVoice-release-manifest.cab.sha256",
        ):
            self.assertIn(required, content)
        policy = (ROOT / "distribution" / "update-policy.json").read_text(
            encoding="utf-8"
        )
        self.assertIn('"require_rfc3161_timestamp": true', policy)
        self.assertNotIn("AZURE_CLIENT_SECRET", content)
        azure_refs = re.findall(
            r"uses:\s+(azure/(?:login|artifact-signing-action))@([^\s#]+)",
            content,
        )
        self.assertEqual(len(azure_refs), 4)
        self.assertEqual(
            [action for action, _ref in azure_refs].count(
                "azure/artifact-signing-action"
            ),
            3,
        )
        for action, action_ref in azure_refs:
            with self.subTest(action=action):
                self.assertRegex(action_ref, r"^[0-9a-f]{40}$")
        self.assertNotRegex(
            content,
            r"uses:\s+azure/(?:login|artifact-signing-action)@v\d+",
        )

    def test_signature_verification_requires_independent_rfc3161_check(self):
        content = (ROOT / "scripts" / "verify-signature.ps1").read_text(
            encoding="utf-8"
        )
        for required in (
            "Get-AuthenticodeSignature",
            "signtool.exe",
            "verify /pa /all /tw /v",
            "Timestamp status: RFC3161",
            "Timestamp signer:",
            "Timestamp thumbprint:",
            "legacy Authenticode",
        ):
            self.assertIn(required, content)
        self.assertIn("$timestampProtocols.Count -ne 1", content)

    def test_ci_exercises_installer_lifecycle_without_signing_secrets(self):
        content = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("scripts\\test-installer.ps1", content)
        self.assertIn("ClarifyVoice-windows-x64-baseline.msi", content)
        self.assertIn("-PayloadIdentity baseline-msi-0.1.1", content)
        self.assertIn("-SourceExe dist\\baseline\\ClarifyVoice.exe", content)
        self.assertIn("-BaselinePayloadSha256", content)
        self.assertIn("-CurrentPayloadSha256", content)
        self.assertIn("payloads must be byte-distinct", content)
        self.assertIn("Install manifest dependencies", content)
        self.assertIn("scripts/create_release_manifest.py tests", content)
        self.assertNotIn("AZURE_CLIENT_ID", content)

    def test_destructive_installer_smoke_is_hosted_runner_only(self):
        content = (ROOT / "scripts" / "test-installer.ps1").read_text(encoding="utf-8")
        guard_call = content.index("Assert-DisposableHostedRunner\n\n$baseline")
        first_write = content.index("New-Item $configDirectory")
        clean_target_call = content.index("Assert-CleanSmokeTarget\nNew-Item")

        self.assertLess(guard_call, first_write)
        self.assertLess(clean_target_call, first_write)
        for required in (
            'CI = "true"',
            'GITHUB_ACTIONS = "true"',
            'RUNNER_ENVIRONMENT = "github-hosted"',
            'RUNNER_OS = "Windows"',
            "GITHUB_WORKSPACE",
            "RUNNER_TEMP",
            "pre-existing ClarifyVoice state",
            "pre-existing ClarifyVoice autostart entry",
            "no longer the smoke-test sentinel",
        ):
            self.assertIn(required, content)

        for operation, expected_hash in (
            ("clean install", "$baselinePayloadHash"),
            ("upgrade", "$currentPayloadHash"),
            ("repair", "$currentPayloadHash"),
            ("manual rollback", "$baselinePayloadHash"),
        ):
            self.assertIn(f'Assert-Installed "{operation}" {expected_hash}', content)
        self.assertIn("Get-FileHash $installedExe -Algorithm SHA256", content)
        self.assertIn("Uninstall left the installed executable behind", content)

    def test_installer_and_update_contract_files_exist(self):
        required = [
            "distribution/update-policy.json",
            "installer/ClarifyVoice.wxs",
            "scripts/build-installer.ps1",
            "scripts/test-installer.ps1",
            "scripts/create_release_manifest.py",
            "scripts/build-manifest-container.ps1",
            "scripts/verify-signature.ps1",
            "update_security.py",
            "version.py",
        ]
        for relative_path in required:
            self.assertTrue((ROOT / relative_path).is_file(), relative_path)

        build = (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8")
        self.assertIn("${distribution};distribution", build)

        installer = (ROOT / "installer" / "ClarifyVoice.wxs").read_text(
            encoding="utf-8"
        )
        self.assertIn('Id="RemoveAutostartOnUninstall"', installer)
        self.assertIn(
            'Condition="REMOVE=&quot;ALL&quot; AND NOT UPGRADINGPRODUCTCODE"',
            installer,
        )
        self.assertNotIn("<RemoveRegistryValue", installer)

        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("if not _is_msi_installed_build():", app_source)
        self.assertNotIn(
            'if not IS_WIN or not getattr(sys, "frozen", False):\n'
            "                update_status.configure(",
            app_source,
        )

    def test_package_scripts_are_documented_maintainer_aliases(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertTrue(package["private"])
        self.assertEqual(
            set(package["scripts"]),
            {"test", "check", "build", "installer", "setup", "deploy"},
        )

    def test_open_source_community_files_exist(self):
        required = [
            "LICENSE",
            "CONTRIBUTING.md",
            "CODE_OF_CONDUCT.md",
            "SECURITY.md",
            "SUPPORT.md",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
        ]
        for relative_path in required:
            self.assertTrue((ROOT / relative_path).is_file(), relative_path)

    def test_release_skill_is_repository_local_and_complete(self):
        skill_root = ROOT / ".agents" / "skills" / "clarifyvoice-release"
        required = [
            "SKILL.md",
            "agents/openai.yaml",
            "references/release-contract.md",
            "scripts/release_preflight.py",
        ]
        for relative_path in required:
            self.assertTrue((skill_root / relative_path).is_file(), relative_path)

        skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: clarifyvoice-release", skill)
        self.assertIn("ClarifyVoice.exe.sha256", skill)
        self.assertNotIn("/mnt/c/Users/Work/.codex/skills", skill)

    def test_dependency_contract_files_exist_and_have_review_policy(self):
        for lock_name in (
            "requirements-lock-linux.txt",
            "requirements-lock-windows.txt",
            "requirements-lock-runtime-windows.txt",
        ):
            lock = ROOT / lock_name
            self.assertTrue(lock.is_file(), lock_name)
            self.assertIn("pip-compile", lock.read_text(encoding="utf-8"))

        policy = json.loads(
            (ROOT / "dependency-audit.json").read_text(encoding="utf-8")
        )
        self.assertEqual(policy["policy_version"], 1)
        self.assertIsInstance(policy["ignored_vulnerabilities"], dict)

        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[tool.ruff]", pyproject)
        self.assertIn("[tool.mypy]", pyproject)
        checker = ROOT / "scripts" / "check_dependency_lock.py"
        self.assertTrue(checker.is_file())
        self.assertTrue((ROOT / "scripts" / "check_runtime_lock.py").is_file())
        self.assertTrue((ROOT / "scripts" / "install_bootstrap_tools.py").is_file())
        self.assertTrue((ROOT / "scripts" / "add_sbom_component.py").is_file())
        self.assertTrue((ROOT / "scripts" / "sox-runtime-manifest.json").is_file())
        self.assertIn(
            "sox-runtime-manifest.json",
            (ROOT / "scripts" / "build.ps1").read_text(encoding="utf-8"),
        )
        checker_content = checker.read_text(encoding="utf-8")
        self.assertNotIn("--check", checker_content)
        self.assertIn("shutil.copyfile(lockfile, generated)", checker_content)
        self.assertIn('"--allow-unsafe"', checker_content)
        self.assertNotIn('"--upgrade"', checker_content)

        runtime_lock = (ROOT / "requirements-lock-runtime-windows.txt").read_text(
            encoding="utf-8"
        )
        for lock_name in (
            "requirements-lock-linux.txt",
            "requirements-lock-windows.txt",
        ):
            dev_lock = (ROOT / lock_name).read_text(encoding="utf-8")
            self.assertRegex(dev_lock, r"(?im)^pip==[^\n]+$")
            self.assertRegex(dev_lock, r"(?im)^setuptools==[^\n]+$")
        self.assertNotRegex(runtime_lock, r"(?im)^(pip|setuptools)==")
        windows_lock = (ROOT / "requirements-lock-windows.txt").read_text(
            encoding="utf-8"
        )
        for windows_only in ("colorama", "pefile", "pywin32-ctypes"):
            self.assertRegex(windows_lock, rf"(?im)^{windows_only}==")
        self.assertNotRegex(windows_lock, r"(?im)^keyboard==")
        for development_tool in (
            "cyclonedx-bom",
            "mypy",
            "pip-audit",
            "pip-tools",
            "pyinstaller",
            "ruff",
        ):
            self.assertNotRegex(runtime_lock, rf"(?im)^{development_tool}==")

    def test_setup_and_workflows_use_the_shared_lock(self):
        setup = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")
        deploy = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
        start = (ROOT / "start.sh").read_text(encoding="utf-8")
        self.assertIn('"requirements-lock-windows.txt"', setup)
        self.assertIn('"requirements-lock-windows.txt"', deploy)
        self.assertIn('"-c", $lockFile', setup)
        self.assertIn('"-c", $lockFile', deploy)
        self.assertIn("requirements-lock-linux.txt", start)
        self.assertNotIn("--upgrade pip", start)

        for workflow_name in ("ci.yml", "release.yml"):
            workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(
                encoding="utf-8"
            )
            self.assertIn("requirements-lock-windows.txt", workflow)
            self.assertIn("requirements-lock-runtime-windows.txt", workflow)
            self.assertIn("scripts/check_runtime_lock.py", workflow)
            if workflow_name == "ci.yml":
                self.assertIn("requirements-lock-linux.txt", workflow)
            self.assertIn("scripts/check_dependency_lock.py", workflow)
            self.assertIn("scripts/install_bootstrap_tools.py", workflow)
            self.assertNotIn("pip-compile --check", workflow)
            self.assertRegex(workflow, r"uses: actions/[^@]+@[0-9a-f]{40}")
            if workflow_name == "ci.yml":
                self.assertEqual(
                    workflow.count("run: python scripts/dependency_audit.py"), 1
                )
                self.assertNotIn(
                    "if: matrix.os == 'ubuntu-latest'\n        run: python scripts/dependency_audit.py",
                    workflow,
                )

    def test_release_quality_gates_are_fail_fast_steps(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("- name: Run tests", workflow)
        for step_name, command in (
            (
                "Install locked development dependencies",
                "python -m pip install",
            ),
            (
                "Check development dependency lock",
                "python scripts/check_dependency_lock.py",
            ),
            (
                "Check runtime dependency lock",
                "python scripts/check_dependency_lock.py",
            ),
            (
                "Check runtime pins match build lock",
                "python scripts/check_runtime_lock.py",
            ),
            ("Run Ruff lint", "ruff check"),
            ("Run Ruff format check", "ruff format --check"),
            ("Run targeted type checks", "mypy"),
            ("Audit dependencies", "python scripts/dependency_audit.py"),
            ("Run unit tests", "python -m unittest"),
            ("Compile Python sources", "python -m compileall"),
        ):
            block = re.search(
                rf"(?ms)^      - name: {re.escape(step_name)}\n(.*?)(?=^      - name: |\Z)",
                workflow,
            )
            self.assertIsNotNone(block, step_name)
            assert block is not None
            self.assertIn(f"run: {command}", block.group(1), step_name)
            self.assertNotIn("run: |", block.group(1), step_name)

    def test_release_sbom_uses_runtime_only_lock(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "cyclonedx-py requirements requirements-lock-runtime-windows.txt",
            workflow,
        )
        self.assertNotIn(
            "cyclonedx-py requirements requirements-lock-windows.txt", workflow
        )
        self.assertIn("scripts/add_sbom_component.py", workflow)
        self.assertIn("--runtime-root extra\\sox-14.4.2", workflow)
        self.assertIn("--manifest scripts\\sox-runtime-manifest.json", workflow)
        self.assertIn(
            "b45f598643ffbd8e363ff24d61166ccec4836fea6d3888881b8df53e3bb55f6c",
            workflow,
        )

    def test_release_has_sbom_and_provenance_contract(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("ClarifyVoice.sbom.json", workflow)
        self.assertIn("actions/attest-build-provenance@", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("attestations: write", workflow)


if __name__ == "__main__":
    unittest.main()
