# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "packaging" / "Apply-Update.ps1"


def powershell():
    return shutil.which("powershell.exe") or shutil.which("pwsh")


@unittest.skipUnless(powershell(), "PowerShell is required for helper runtime tests")
class UpdateHelperTests(unittest.TestCase):
    def _run_with_mocked_start(self, script, plan_file, capture):
        harness = plan_file.parent / "harness.ps1"
        harness.write_text(
            "function Start-Process {\n"
            "  param([string]$FilePath,[string]$ArgumentList,[switch]$Wait,[switch]$PassThru,[string]$WindowStyle,[string]$WorkingDirectory)\n"
            "  if ($PassThru) { [IO.File]::WriteAllText($env:CAPTURE, $ArgumentList); return [pscustomobject]@{ExitCode=0} }\n"
            "  [IO.File]::AppendAllText($env:CAPTURE, \"`nRESTART:$FilePath|$WorkingDirectory|$WindowStyle\")\n"
            "}\n"
            "& \"$env:HELPER\" -Plan \"$env:PLAN\"\n"
            "if ($LASTEXITCODE -ne $null) { exit $LASTEXITCODE }\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment.update({"HELPER": str(script), "PLAN": str(plan_file), "CAPTURE": str(capture)})
        return subprocess.run([powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(harness)], capture_output=True, text=True, timeout=30, env=environment)

    def test_portable_failure_rolls_back_prior_file_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "target"
            stage = base / "stage"
            payload = stage / "payload"
            target.mkdir()
            payload.mkdir(parents=True)
            (target / "CampusTerminal.portable").write_text("portable")
            (target / "CampusTerminal.exe").write_bytes(b"not-launched-test-fixture")
            (target / "Apply-Update.ps1").write_text(HELPER.read_text(encoding="utf-8"))
            (target / "a.txt").write_text("old")
            (target / "z-conflict").mkdir()
            (payload / "CampusTerminal.portable").write_text("portable")
            (payload / "CampusTerminal.exe").write_bytes(b"updated-fixture")
            (payload / "Apply-Update.ps1").write_text("retained helper")
            (payload / "a.txt").write_text("new")
            (payload / "z-conflict").write_text("cannot replace a directory")
            manifest = {p.relative_to(payload).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in payload.rglob("*") if p.is_file()}
            asset = stage / "asset.zip"
            with zipfile.ZipFile(asset, "w") as archive:
                archive.writestr("placeholder", "payload")
            plan = {
                "schema": 1, "edition": "portable", "root": str(target), "version": "1.3.20",
                "asset": str(asset), "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                "payload": str(payload), "helper": str(stage / "Apply-Update.ps1"),
                "gui_process": None, "core_processes": [], "wait_timeout_seconds": 10,
                "payload_manifest": manifest,
            }
            (stage / "Apply-Update.ps1").write_text(HELPER.read_text(encoding="utf-8"))
            plan_file = stage / "plan.json"
            plan_file.write_text(json.dumps(plan), encoding="utf-8")
            result = subprocess.run([powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-File", str(stage / "Apply-Update.ps1"), "-Plan", str(plan_file)],
                                    capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            report = (stage / "update-error.txt").read_text() if (stage / "update-error.txt").exists() else result.stderr
            self.assertIn("local directory", report)
            self.assertEqual((target / "a.txt").read_text(), "old")
            self.assertEqual((stage / "rollback" / "a.txt").read_text(), "old")
            self.assertTrue((stage / "update-error.txt").is_file())

    def test_cancel_flag_aborts_before_any_target_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target, stage = base / "target", base / "stage"
            target.mkdir()
            stage.mkdir()
            (target / "CampusTerminal.portable").write_text("portable")
            (target / "CampusTerminal.exe").write_text("old")
            (target / "CampusTerminal.Core.exe").write_text("old")
            (target / "version.txt").write_text("1.3.19")
            asset = stage / "asset.zip"
            asset.write_bytes(b"asset")
            staged_helper = stage / "Apply-Update.ps1"
            staged_helper.write_text(HELPER.read_text(encoding="utf-8"))
            plan = {"schema": 1, "edition": "portable", "root": str(target), "version": "1.3.20",
                    "asset": str(asset), "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                    "payload": str(stage / "payload"), "helper": str(staged_helper), "gui_process": None,
                    "core_processes": [], "payload_manifest": {}, "wait_timeout_seconds": 10}
            plan_file = stage / "plan.json"
            plan_file.write_text(json.dumps(plan), encoding="utf-8")
            (stage / "cancel-update").write_text("cancel")
            result = subprocess.run([powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-File", str(staged_helper), "-Plan", str(plan_file)],
                                    capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((target / "CampusTerminal.exe").read_text(), "old")
            self.assertTrue((stage / "update-error.txt").is_file())

    def test_installer_directory_argument_is_quoted_as_one_argument(self):
        with tempfile.TemporaryDirectory(prefix="CampusTerminal space ") as temp:
            base = Path(temp)
            target = base / "target with spaces 中文目录"
            stage = base / "stage"
            target.mkdir()
            stage.mkdir()
            for name, data in (("edition.txt", "installed"), ("version.txt", "1.3.19"),
                               ("CampusTerminal.exe", "fixture"), ("CampusTerminal.Core.exe", "fixture")):
                (target / name).write_text(data)
            (target / "Apply-Update.ps1").write_text(HELPER.read_text(encoding="utf-8"))
            asset = stage / "CampusTerminal-1.3.20-windows-x64-setup.exe"
            asset.write_bytes(b"MZtest-installer")
            staged_helper = stage / "Apply-Update.ps1"
            staged_helper.write_text(HELPER.read_text(encoding="utf-8"))
            plan = {"schema": 1, "edition": "installed", "root": str(target), "version": "1.3.20",
                    "asset": str(asset), "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                    "size": asset.stat().st_size, "payload": None, "helper": str(staged_helper),
                    "gui_process": None, "core_processes": [], "payload_manifest": None,
                    "wait_timeout_seconds": 10}
            plan_file = stage / "plan.json"
            plan_file.write_text(json.dumps(plan), encoding="utf-8")
            capture = stage / "calls.txt"
            result = self._run_with_mocked_start(staged_helper, plan_file, capture)
            report = (stage / "update-error.txt").read_text() if (stage / "update-error.txt").exists() else result.stderr
            self.assertEqual(result.returncode, 0, report)
            self.assertIn(f'/DIR="{target}"', capture.read_text(encoding="utf-8-sig"))
            self.assertIn("/VERYSILENT", capture.read_text(encoding="utf-8-sig"))

    def test_portable_success_preserves_unknown_and_user_data_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "target"
            stage = base / "stage"
            payload = stage / "payload"
            target.mkdir()
            payload.mkdir(parents=True)
            for relative, content in {"CampusTerminal.portable": "portable", "CampusTerminal.exe": "old gui",
                                      "CampusTerminal.Core.exe": "old core", "version.txt": "1.3.19",
                                      "edition.txt": "portable", "local.txt": "keep",
                                      "state/token.dat": "secret", "logs/log.txt": "trace",
                                      "payload/cache.dat": "payload"}.items():
                path = target / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            for relative, content in {"CampusTerminal.portable": "portable", "CampusTerminal.exe": "new gui",
                                      "CampusTerminal.Core.exe": "new core", "version.txt": "1.3.20",
                                      "Apply-Update.ps1": "new helper"}.items():
                (payload / relative).write_text(content)
            manifest = {p.relative_to(payload).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in payload.rglob("*") if p.is_file()}
            asset = stage / "portable.zip"
            asset.write_bytes(b"archive fixture")
            staged_helper = stage / "Apply-Update.ps1"
            staged_helper.write_text(HELPER.read_text(encoding="utf-8"))
            plan = {"schema": 1, "edition": "portable", "root": str(target), "version": "1.3.20",
                    "asset": str(asset), "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                    "payload": str(payload), "helper": str(staged_helper), "gui_process": None,
                    "core_processes": [], "payload_manifest": manifest, "wait_timeout_seconds": 10}
            plan_file = stage / "plan.json"
            plan_file.write_text(json.dumps(plan), encoding="utf-8")
            result = self._run_with_mocked_start(staged_helper, plan_file, stage / "calls.txt")
            report = (stage / "update-error.txt").read_text() if (stage / "update-error.txt").exists() else result.stderr
            self.assertEqual(result.returncode, 0, report)
            self.assertEqual((target / "CampusTerminal.exe").read_text(), "new gui")
            self.assertEqual((target / "Apply-Update.ps1").read_text(), "new helper")
            for relative, expected in (("local.txt", "keep"), ("state/token.dat", "secret"),
                                       ("logs/log.txt", "trace"), ("payload/cache.dat", "payload"),
                                       ("edition.txt", "portable")):
                self.assertEqual((target / relative).read_text(), expected)


if __name__ == "__main__":
    unittest.main()
