# SPDX-License-Identifier: GPL-3.0-or-later
"""Validated GitHub release lookup and external update handoff."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

import psutil

REPOSITORY = "HakureiTree/CampusTerminal-for-JNU-Zhuhai-"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_ASSET_BYTES = 512 * 1024 * 1024
HTTP_TIMEOUT = 15
_VERSION = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")
_API_HOSTS = {"api.github.com"}
_ASSET_HOSTS = {"github.com", "release-assets.githubusercontent.com"}


def _version(value):
    if not isinstance(value, str) or not (match := _VERSION.fullmatch(value.strip())):
        return None
    return tuple(int(part) for part in match.groups())


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _asset_pattern(version, edition):
    suffix = "portable.zip" if edition == "portable" else "setup.exe"
    return f"CampusTerminal-{version}-windows-x64-{suffix}"


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if (parsed.scheme != "https" or parsed.hostname not in self.allowed_hosts or
                parsed.port not in (None, 443) or parsed.username or parsed.password):
            raise urllib.error.HTTPError(newurl, code, "Untrusted redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_url(url, *, limit, headers=None, allowed_hosts=_API_HOSTS):
    started = time.monotonic()
    request = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(_SafeRedirect(allowed_hosts))
    with opener.open(request, timeout=HTTP_TIMEOUT) as response:
        final = urllib.parse.urlparse(response.geturl())
        if (final.scheme != "https" or final.hostname not in allowed_hosts or
                final.port not in (None, 443) or final.username or final.password):
            raise ValueError("Untrusted download URL")
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > limit:
            raise ValueError("Release asset exceeds size limit")
        chunks, total = [], 0
        # read1 performs one buffered/socket read; read(n) may wait for n bytes
        # forever when a peer trickles bytes inside the per-socket timeout.
        reader = getattr(response, "read1", response.read)
        while True:
            if time.monotonic() - started > 30:
                raise TimeoutError("Release metadata exceeded total timeout")
            block = reader(min(64 * 1024, limit - total + 1))
            if not block:
                return b"".join(chunks)
            total += len(block)
            if total > limit:
                raise ValueError("Release metadata exceeds size limit")
            chunks.append(block)


def _download_asset(url, destination, *, size_limit, expected_size=None):
    request = urllib.request.Request(url, headers={"User-Agent": "CampusTerminal-Updater"})
    opener = urllib.request.build_opener(_SafeRedirect(_ASSET_HOSTS))
    started = time.monotonic()
    total = 0
    digest = hashlib.sha256()
    with opener.open(request, timeout=HTTP_TIMEOUT) as response, Path(destination).open("xb") as output:
        final = urllib.parse.urlparse(response.geturl())
        if (final.scheme != "https" or final.hostname not in _ASSET_HOSTS or
                final.port not in (None, 443) or final.username or final.password):
            raise ValueError("Untrusted download URL")
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > size_limit:
            raise ValueError("Release asset exceeds size limit")
        reader = getattr(response, "read1", response.read)
        while True:
            if time.monotonic() - started > 120:
                raise TimeoutError("Release asset download exceeded total timeout")
            block = reader(64 * 1024)
            if not block:
                break
            total += len(block)
            if total > size_limit or (expected_size is not None and total > expected_size):
                raise ValueError("Release asset exceeds declared size")
            digest.update(block)
            output.write(block)
    if expected_size is not None and total != expected_size:
        raise ValueError("Release asset size mismatch")
    return digest.hexdigest(), total


def _release_asset(release, edition):
    if not isinstance(release, dict):
        raise ValueError("Invalid release metadata")
    version = release.get("version")
    parsed = _version(version)
    if parsed is None or release.get("edition") != edition:
        raise ValueError("Invalid release version or edition")
    if _version(release.get("tag_name")) != parsed:
        raise ValueError("Release tag and version do not match")
    expected = _asset_pattern(version, edition)
    name, url = release.get("asset_name"), release.get("asset_url")
    parsed_url = urllib.parse.urlparse(url or "")
    expected_path = f"/{REPOSITORY}/releases/download/{release.get('tag_name', 'v' + version)}/{expected}"
    if (name != expected or parsed_url.scheme != "https" or parsed_url.hostname != "github.com" or
            parsed_url.path != expected_path or parsed_url.port not in (None, 443) or
            parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment):
        raise ValueError("Unexpected release asset")
    if release.get("repository") != REPOSITORY:
        raise ValueError("Unexpected release repository")
    digest = release.get("sha256")
    if digest is not None and not _DIGEST.fullmatch(str(digest)):
        raise ValueError("Invalid asset digest")
    size = release.get("size")
    if size is not None and (not isinstance(size, int) or size <= 0 or size > MAX_ASSET_BYTES):
        raise ValueError("Invalid asset size")
    return parsed, expected, url, digest.lower() if digest else None


def check_release(current_version, edition):
    """Return a newer validated stable release, or None when already current."""
    current = _version(current_version)
    if current is None or edition not in ("portable", "installed"):
        raise ValueError("Invalid current version or distribution edition")
    try:
        metadata = json.loads(_read_url(API_URL, limit=2 * 1024 * 1024,
                                        headers={"Accept": "application/vnd.github+json",
                                                 "User-Agent": "CampusTerminal-Updater"},
                                        allowed_hosts=_API_HOSTS))
        if not isinstance(metadata, dict) or metadata.get("draft") or metadata.get("prerelease"):
            raise ValueError("Latest release is not a stable published release")
        repository = metadata.get("html_url", "")
        if repository.rstrip("/") != f"https://github.com/{REPOSITORY}/releases/tag/{metadata.get('tag_name', '')}":
            raise ValueError("Latest release repository or tag URL is invalid")
        version = _version(metadata.get("tag_name"))
        if version is None:
            raise ValueError("Latest release tag is not a stable numeric version")
        if version <= current:
            return None
        expected = _asset_pattern(metadata["tag_name"].removeprefix("v"), edition)
        assets = metadata.get("assets")
        if not isinstance(assets, list):
            raise ValueError("Latest release assets metadata is invalid")
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("name") != expected:
                continue
            url = asset.get("browser_download_url", "")
            digest_value = asset.get("digest")
            digest = digest_value.removeprefix("sha256:") if isinstance(digest_value, str) else None
            result = {
                "version": metadata["tag_name"].removeprefix("v"),
                "tag_name": metadata["tag_name"],
                "edition": edition,
                "asset_name": expected,
                "asset_url": url,
                "sha256": digest,
                "size": asset.get("size"),
                "repository": REPOSITORY,
            }
            _release_asset(result, edition)
            if (not isinstance(asset.get("size"), int) or asset["size"] <= 0 or
                    asset["size"] > MAX_ASSET_BYTES):
                raise ValueError("Latest release asset size is invalid")
            return result
        raise ValueError(f"Latest release has no expected {edition} asset")
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError, json.JSONDecodeError):
        raise
    raise ValueError("Latest release metadata is incomplete")


def _root_kind(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise ValueError("Source checkout cannot be an update target")
    if (root / "CampusTerminal.portable").is_file():
        edition = "portable"
    elif ((root / "edition.txt").is_file() and
          (root / "edition.txt").read_text(encoding="utf-8").strip().lower() == "installed" and
          (root / "CampusTerminal.exe").is_file()):
        edition = "installed"
    else:
        raise ValueError("Target is not a packaged CampusTerminal distribution")
    if not (root / "Apply-Update.ps1").is_file():
        raise ValueError("Update helper is missing from this distribution")
    if any(not (root / name).is_file() for name in ("CampusTerminal.exe", "CampusTerminal.Core.exe", "version.txt")):
        raise ValueError("Packaged distribution is missing an executable or version marker")
    if _version((root / "version.txt").read_text(encoding="ascii").strip()) is None:
        raise ValueError("Packaged distribution version marker is invalid")
    return root, edition


def _process_identity(process):
    try:
        return {"pid": process.pid, "created": process.create_time()}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _root_core_processes(root):
    target = str((root / "CampusTerminal.Core.exe").resolve()).casefold()
    found = []
    for process in psutil.process_iter(("exe", "cmdline")):
        try:
            exe = process.info.get("exe")
            if exe and str(Path(exe).resolve()).casefold() == target:
                identity = _process_identity(process)
                if identity:
                    found.append(identity)
        except (OSError, psutil.Error):
            continue
    return found


def _safe_extract(archive, destination):
    destination = destination.resolve()
    total = 0
    seen = set()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            relative = PurePosixPath(name)
            mode = info.external_attr >> 16
            reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                        *(f"LPT{i}" for i in range(1, 10))}
            normalized = "/".join(part.casefold() for part in relative.parts)
            if (len(seen) >= 50000 or not name or name.startswith("/") or ".." in relative.parts or
                    ":" in name or "\x00" in name or any(part.endswith((" ", ".")) for part in relative.parts) or
                    any(part.split(".")[0].upper() in reserved for part in relative.parts) or
                    (mode & 0o170000) == 0o120000 or normalized in seen):
                raise ValueError("Unsafe path in update archive")
            seen.add(normalized)
            total += info.file_size
            if total > 1024 * 1024 * 1024:
                raise ValueError("Expanded update exceeds size limit")
            target = (destination / Path(*relative.parts)).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError("Unsafe path in update archive")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
    # GitHub portable archives contain a single top-level application directory.
    children = list(destination.iterdir())
    payload = children[0] if len(children) == 1 and children[0].is_dir() else destination
    if not ((payload / "CampusTerminal.portable").is_file() and
            (payload / "CampusTerminal.exe").is_file() and
            (payload / "CampusTerminal.Core.exe").is_file() and
            (payload / "version.txt").is_file()):
        raise ValueError("Portable archive has an unexpected layout")
    return payload


def prepare_update(release, root):
    """Download and verify a release into sibling staging; return its external plan path."""
    version, asset_name, url, expected_digest = _release_asset(release, release.get("edition"))
    if release.get("size") is None:
        raise ValueError("Release asset size is missing")
    target_root, edition = _root_kind(root)
    if edition != release["edition"]:
        raise ValueError("Release edition does not match target distribution")
    installed_version = _version((target_root / "version.txt").read_text(encoding="ascii").strip()) if (target_root / "version.txt").is_file() else None
    if installed_version and version <= installed_version:
        raise ValueError("Refusing downgrade or same-version update")
    stage = Path(tempfile.mkdtemp(prefix=".CampusTerminal-update-", dir=target_root.parent)).resolve()
    try:
        archive_or_installer = stage / asset_name
        digest, asset_size = _download_asset(url, archive_or_installer,
                                             size_limit=MAX_ASSET_BYTES,
                                             expected_size=release.get("size"))
        if expected_digest and digest != expected_digest:
            raise ValueError("Release asset SHA-256 mismatch")
        payload = None
        if edition == "portable":
            payload = _safe_extract(archive_or_installer, stage / "payload")
            actual_version = _version((payload / "version.txt").read_text(encoding="ascii").strip())
            if actual_version != version or (payload / "CampusTerminal.exe").stat().st_size <= 0 or (payload / "CampusTerminal.Core.exe").stat().st_size <= 0:
                raise ValueError("Portable contents do not match validated release")
            if not (payload / "Apply-Update.ps1").is_file():
                shutil.copy2(target_root / "Apply-Update.ps1", payload / "Apply-Update.ps1")
        else:
            with archive_or_installer.open("rb") as installer_file:
                is_pe = installer_file.read(2) == b"MZ"
            if archive_or_installer.stat().st_size <= 0 or not is_pe:
                raise ValueError("Installer asset is not a valid Windows executable")
        helper = stage / "Apply-Update.ps1"
        shutil.copy2(target_root / "Apply-Update.ps1", helper)
        try:
            gui = psutil.Process(os.getpid())
            gui_identity = {"pid": gui.pid, "created": gui.create_time()}
        except psutil.Error as exc:
            raise RuntimeError("Unable to record GUI process identity") from exc
        plan = {
            "schema": 1, "edition": edition, "root": str(target_root), "version": release["version"],
            "asset": str(archive_or_installer), "sha256": digest, "size": asset_size,
            "payload": str(payload) if payload else None, "helper": str(helper),
            "gui_process": gui_identity, "core_processes": _root_core_processes(target_root),
            "payload_manifest": ({str(path.relative_to(payload)).replace("\\", "/"): _file_sha256(path)
                                  for path in payload.rglob("*") if path.is_file()} if payload else None),
            "wait_timeout_seconds": 180,
        }
        plan_path = stage / "update-plan.json"
        plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        return str(plan_path)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def cancel_update(plan_path):
    """Ask the staged helper to abort before touching the target root."""
    path = Path(plan_path).resolve(strict=True)
    plan = json.loads(path.read_text(encoding="utf-8"))
    helper = Path(plan["helper"]).resolve(strict=True)
    if helper.parent != path.parent:
        raise ValueError("Update plan is outside its staging directory")
    (path.parent / "cancel-update").write_text("cancel\n", encoding="ascii")


def launch_update(plan_path):
    """Start the staged helper and return immediately so GUI can request clean shutdown."""
    plan_path = Path(plan_path).resolve(strict=True)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    helper = Path(plan["helper"]).resolve(strict=True)
    if helper.parent != plan_path.parent or helper.name != "Apply-Update.ps1":
        raise ValueError("Update helper is not in the private staging directory")
    if Path(plan["root"]).resolve(strict=True) == helper.parent:
        raise ValueError("Invalid update staging location")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-WindowStyle", "Hidden", "-File", str(helper), "-Plan", str(plan_path)]
    return subprocess.Popen(command, close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) |
                            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
