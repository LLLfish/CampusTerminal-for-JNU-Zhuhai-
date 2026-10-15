# SPDX-License-Identifier: GPL-3.0-or-later
"""Release-notes extraction and the tag lookup used by the settings changelog row."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gui.bridge import update

NOTES = """# CampusTerminal 1.4.1

## 更新

- 启动时检查新版本。
- 新增「发现新版本后自动更新并重启」。

## 修复

- 修复周期重认证断线。

## 升级

- 从托盘退出后升级。
"""


class UpdateNotesTests(unittest.TestCase):
    def test_update_notes_keep_the_repair_section(self):
        notes = update.update_notes(NOTES)
        self.assertIn("启动时检查新版本", notes)
        self.assertIn("修复周期重认证断线", notes)
        self.assertNotIn("从托盘退出后升级", notes)

    def test_update_notes_empty_without_the_heading(self):
        self.assertEqual(update.update_notes("## 修复\n- 只有修复\n"), "")
        self.assertEqual(update.update_notes(""), "")
        self.assertEqual(update.update_notes(None), "")

    def test_format_and_parse_round_trip(self):
        self.assertEqual(update.format_version(update.parse_version("v1.4.1")), "1.4.1")
        self.assertIsNone(update.parse_version("1.4"))
        self.assertEqual(update.format_version(None), "")


class TagLookupTests(unittest.TestCase):
    def test_release_notes_read_the_tag_endpoint(self):
        metadata = {"tag_name": "v1.4.1", "draft": False, "prerelease": False,
                    "assets": [], "body": NOTES}
        seen = []

        def read_url(url, **_kwargs):
            seen.append(url)
            return json.dumps(metadata).encode("utf-8")

        with patch.object(update, "_read_url", side_effect=read_url):
            notes = update.release_notes("1.4.1")
        self.assertEqual(seen, [f"https://api.github.com/repos/{update.REPOSITORY}/releases/tags/v1.4.1"])
        self.assertIn("启动时检查新版本", notes)

    def test_release_notes_prefer_the_readme_asset(self):
        metadata = {"tag_name": "v1.4.1", "draft": False, "prerelease": False, "body": "## 更新\n- 正文",
                    "assets": [{"name": "README.md", "digest": None, "size": 10,
                                "browser_download_url":
                                    f"https://github.com/{update.REPOSITORY}/releases/download/v1.4.1/README.md"}]}
        seen = []

        def read_url(url, **_kwargs):
            seen.append(url)
            if "api.github.com" in url:
                return json.dumps(metadata).encode("utf-8")
            return NOTES.encode("utf-8")

        with patch.object(update, "_read_url", side_effect=read_url):
            notes = update.release_notes("1.4.1")
        self.assertEqual(len(seen), 2)
        self.assertIn("修复周期重认证断线", notes)

    def test_release_notes_fail_quietly(self):
        with patch.object(update, "_read_url", side_effect=OSError("offline")):
            self.assertEqual(update.release_notes("1.4.1"), "")
        self.assertEqual(update.release_notes("not-a-version"), "")

    def test_release_notes_reject_a_mismatched_tag(self):
        payload = json.dumps({"tag_name": "v9.9.9", "body": NOTES}).encode("utf-8")
        with patch.object(update, "_read_url", return_value=payload):
            self.assertEqual(update.release_notes("1.4.1"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
