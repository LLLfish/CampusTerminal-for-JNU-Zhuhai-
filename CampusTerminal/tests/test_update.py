# SPDX-License-Identifier: GPL-3.0-or-later
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gui.bridge import update


def temporary_directory():
    return tempfile.TemporaryDirectory()


def release(edition="portable", version="1.3.20", *, digest=None, size=None):
    suffix = "portable.zip" if edition == "portable" else "setup.exe"
    return {
        "version": version, "tag_name": f"v{version}", "edition": edition,
        "asset_name": f"CampusTerminal-{version}-windows-x64-{suffix}",
        "asset_url": f"https://github.com/{update.REPOSITORY}/releases/download/v{version}/CampusTerminal-{version}-windows-x64-{suffix}",
        "sha256": digest, "size": size, "repository": update.REPOSITORY,
    }


def packaged_root(root, edition="portable"):
    root.mkdir()
    (root / "Apply-Update.ps1").write_text("# helper", encoding="utf-8")
    (root / "CampusTerminal.exe").write_bytes(b"exe")
    (root / "CampusTerminal.Core.exe").write_bytes(b"core")
    (root / "version.txt").write_text("1.3.19", encoding="ascii")
    if edition == "portable":
        (root / "CampusTerminal.portable").write_text("portable", encoding="ascii")
    else:
        (root / "edition.txt").write_text("installed", encoding="ascii")
    return root


def portable_zip():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zf:
        zf.writestr("CampusTerminal/CampusTerminal.portable", "portable")
        zf.writestr("CampusTerminal/CampusTerminal.exe", "gui")
        zf.writestr("CampusTerminal/CampusTerminal.Core.exe", "core")
        zf.writestr("CampusTerminal/version.txt", "1.3.20")
    return data.getvalue()


class UpdateTests(unittest.TestCase):
    def test_slow_trickle_metadata_and_download_have_overall_deadlines(self):
        response = MagicMock()
        response.geturl.return_value = update.API_URL
        response.headers = {}
        response.read1.return_value = b"a"
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch.object(update.urllib.request, "build_opener", return_value=opener), \
                patch.object(update.time, "monotonic", side_effect=[0, 1, 31]):
            with self.assertRaises(TimeoutError):
                update._read_url(update.API_URL, limit=100)
        response.read.assert_not_called()
        response.geturl.return_value = release()["asset_url"]
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(update.urllib.request, "build_opener", return_value=opener), \
                patch.object(update.time, "monotonic", side_effect=[0, 1, 121]):
            with self.assertRaises(TimeoutError):
                update._download_asset(release()["asset_url"], Path(temp) / "partial.zip", size_limit=100)

    def test_numeric_stable_versions_and_exact_asset_pattern(self):
        self.assertEqual(update._version("v1.3.19"), (1, 3, 19))
        for bad in ("1.3", "1.3.0-rc1", "../1.3.0", "01.2.3"):
            self.assertIsNone(update._version(bad))
        update._release_asset(release("installed"), "installed")
        altered = release("installed")
        altered["asset_name"] = "other.exe"
        with self.assertRaises(ValueError):
            update._release_asset(altered, "installed")

    @patch.object(update, "_read_url")
    def test_check_release_requires_valid_latest_stable_metadata(self, read_url):
        expected = release()
        response = {
            "draft": False, "prerelease": False, "tag_name": "v1.3.20",
            "html_url": "https://github.com/" + update.REPOSITORY + "/releases/tag/v1.3.20",
            "assets": [{"name": expected["asset_name"], "browser_download_url": expected["asset_url"],
                        "digest": "sha256:" + "a" * 64, "size": 25}],
        }
        read_url.return_value = json.dumps(response).encode()
        got = update.check_release("1.3.19", "portable")
        self.assertEqual(got["version"], "1.3.20")
        self.assertEqual(got["sha256"], "a" * 64)
        self.assertIsNone(update.check_release("1.3.20", "portable"))
        with self.assertRaisesRegex(ValueError, "current version"):
            update.check_release("1.3.20-rc1", "portable")
        response["prerelease"] = True
        read_url.return_value = json.dumps(response).encode()
        with self.assertRaisesRegex(ValueError, "stable"):
            update.check_release("1.3.19", "portable")

    def test_metadata_url_digest_and_size_are_constrained(self):
        data = release()
        data["asset_url"] = "http://github.com/a"
        with self.assertRaises(ValueError):
            update._release_asset(data, "portable")
        data = release(digest="not-a-digest")
        with self.assertRaises(ValueError):
            update._release_asset(data, "portable")
        data = release(size=update.MAX_ASSET_BYTES + 1)
        with self.assertRaises(ValueError):
            update._release_asset(data, "portable")

    def test_archive_traversal_and_symlinks_are_rejected(self):
        for member in ("../outside", "C:/outside", "CampusTerminal/link"):
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as zf:
                info = zipfile.ZipInfo(member)
                if member.endswith("link"):
                    info.create_system = 3
                    info.external_attr = (0o120777 << 16)
                zf.writestr(info, "unsafe")
            with temporary_directory() as temp:
                with self.assertRaises(ValueError):
                    update._safe_extract(io.BytesIO(data.getvalue()), Path(temp) / "out")

    @patch.object(update, "_root_core_processes", return_value=[])
    @patch.object(update, "_download_asset")
    def test_prepare_verifies_hash_and_creates_sibling_plan(self, download, _core):
        archive = portable_zip()
        with temporary_directory() as temp:
            parent = Path(temp)
            root = packaged_root(parent / "CampusTerminal")
            digest = hashlib.sha256(archive).hexdigest()
            release_info = release(digest=digest, size=len(archive))
            def download_asset(_url, destination, **_kwargs):
                Path(destination).write_bytes(archive)
                return digest, len(archive)
            download.side_effect = download_asset
            plan_path = Path(update.prepare_update(release_info, root))
            self.assertEqual(plan_path.parent.parent, parent)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["root"], str(root.resolve()))
            self.assertEqual(plan["edition"], "portable")
            self.assertEqual(plan["sha256"], digest)
            self.assertTrue(Path(plan["payload"], "CampusTerminal.portable").is_file())
            bad = release(digest="0" * 64, size=len(archive))
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                update.prepare_update(bad, root)

    def test_checkout_and_downgrade_targets_are_rejected(self):
        with temporary_directory() as temp:
            checkout = Path(temp) / "repo"
            checkout.mkdir()
            (checkout / ".git").mkdir()
            with self.assertRaisesRegex(ValueError, "Source checkout"):
                update._root_kind(checkout)
            root = packaged_root(Path(temp) / "pkg")
            with self.assertRaisesRegex(ValueError, "downgrade"):
                update.prepare_update(release(version="1.3.18", size=100), root)


if __name__ == "__main__":
    unittest.main()
