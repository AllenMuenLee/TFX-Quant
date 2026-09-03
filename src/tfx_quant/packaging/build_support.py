"""Reusable, unit-tested helpers for the repeatable Windows build.

The orchestration CLI lives at ``installer/build.py`` (repo root, not importable);
everything with logic worth testing is here so ``tests/packaging`` can exercise it
without a network download or an Inno Setup install.

Build outputs that satisfy the prompt's "使乾淨環境可重建相同產物" / build-log
requirements:

* ``build-manifest.json`` — source revision (+ dirty flag), tool versions, the
  embedded-Python archive hash, every locked dependency with its hash, UTC time.
* ``SHA256SUMS`` — one line per staged file.
* ``third_party_licenses.txt`` — inventory walked from the staged ``*.dist-info``.
* ``RELEASE-NOTES-<version>.md`` — rendered from ``installer/RELEASE-NOTES.template.md``.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# --- embedded CPython -------------------------------------------------------------

EMBED_PYTHON_VERSION = "3.11.9"
EMBED_PYTHON_URL = "https://www.python.org/ftp/python/{version}/python-{version}-embed-win32.zip"
# SHA-256 of python-3.11.9-embed-win32.zip as published on python.org.
EMBED_PYTHON_SHA256 = "daf24de7fb3b173e94e56a201d3f38dfedebbdc7ed1925f7aeb8ed588e2b4189"


class BuildError(RuntimeError):
    """A build step cannot proceed safely."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str) -> None:
    actual = sha256_file(path)
    if actual.lower() != expected.lower():
        raise BuildError(f"checksum mismatch for {path.name}: expected {expected}, got {actual}")


def download_file(url: str, dest: Path, *, expected_sha256: str | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - pinned python.org URL
        dest.write_bytes(response.read())
    if expected_sha256 is not None:
        verify_sha256(dest, expected_sha256)
    return dest


def resolve_python_embed_zip(
    *, cache_dir: Path, explicit_path: Path | None = None, allow_download: bool = True
) -> Path:
    """Return a verified ``python-<ver>-embed-win32.zip``.

    Order: an explicitly supplied path, then a cached copy, then a download from
    python.org (only when ``allow_download``). The archive is checksum-verified
    against :data:`EMBED_PYTHON_SHA256` in every case.
    """
    if explicit_path is not None:
        verify_sha256(explicit_path, EMBED_PYTHON_SHA256)
        return explicit_path
    cached = cache_dir / f"python-{EMBED_PYTHON_VERSION}-embed-win32.zip"
    if cached.is_file():
        verify_sha256(cached, EMBED_PYTHON_SHA256)
        return cached
    if not allow_download:
        raise BuildError(
            f"offline build: {cached} is missing and downloads are disabled; "
            "supply --python-embed <path> or populate the cache directory"
        )
    return download_file(
        EMBED_PYTHON_URL.format(version=EMBED_PYTHON_VERSION),
        cached,
        expected_sha256=EMBED_PYTHON_SHA256,
    )


# --- source revision -------------------------------------------------------------


def resolve_source_revision(repo_root: Path) -> tuple[str, bool]:
    """``(commit_sha, is_dirty)``. ``("unknown", True)`` if git is unavailable."""
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return commit, bool(status)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown", True


# --- staged file inventory ------------------------------------------------------


def iter_staged_files(stage_dir: Path) -> Iterator[Path]:
    for path in sorted(stage_dir.rglob("*")):
        if path.is_file():
            yield path


def write_sha256sums(stage_dir: Path, out_path: Path) -> int:
    lines: list[str] = []
    for path in iter_staged_files(stage_dir):
        if path == out_path:
            continue
        rel = path.relative_to(stage_dir).as_posix()
        lines.append(f"{sha256_file(path)}  {rel}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


# --- locked dependencies ------------------------------------------------------


_LOCK_ENTRY = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s\\]+)")
_LOCK_HASH = re.compile(r"--hash=sha256:(?P<hash>[0-9a-f]+)")


def parse_requirements_lock(text: str) -> list[dict[str, object]]:
    """Extract ``{name, version, hashes}`` from a ``pip-compile --generate-hashes``
    lock file. Order preserved; comment/blank lines ignored."""
    entries: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCK_ENTRY.match(line)
        if match:
            current = {
                "name": match.group("name").lower(),
                "version": match.group("version"),
                "hashes": [],
            }
            entries.append(current)
        hash_match = _LOCK_HASH.search(line)
        if hash_match and current is not None:
            hashes = current["hashes"]
            assert isinstance(hashes, list)
            hashes.append(hash_match.group("hash"))
    return entries


# --- license inventory --------------------------------------------------------


def collect_license_inventory(site_packages: Path) -> list[dict[str, str]]:
    inventory: list[dict[str, str]] = []
    for dist_info in sorted(site_packages.glob("*.dist-info")):
        metadata = dist_info / "METADATA"
        name = version = license_name = ""
        if metadata.is_file():
            for line in metadata.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Name: "):
                    name = line[6:].strip()
                elif line.startswith("Version: "):
                    version = line[9:].strip()
                elif line.startswith("License: ") and line[9:].strip() not in ("", "UNKNOWN"):
                    license_name = line[9:].strip()
                elif line.startswith("Classifier: License :: "):
                    license_name = line.split("::")[-1].strip()
                elif not line.strip():
                    break
        license_files = sorted(
            p.name
            for p in dist_info.glob("*")
            if p.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))
        )
        inventory.append(
            {
                "name": name or dist_info.name.split("-")[0],
                "version": version,
                "license": license_name or "see package",
                "license_files": ", ".join(license_files),
            }
        )
    return inventory


def render_license_text(inventory: list[dict[str, str]]) -> str:
    header = (
        "第三方套件授權清單 / Third-party license inventory\n"
        f"產生時間 (UTC): {datetime.now(UTC).isoformat()}\n"
        "元大交易/行情 API 元件不在此清單內，且不隨本安裝檔散布。\n" + "=" * 72 + "\n\n"
    )
    body = "\n".join(
        f"- {item['name']} {item['version']} — {item['license']}"
        + (f" ({item['license_files']})" if item["license_files"] else "")
        for item in inventory
    )
    return header + body + "\n"


# --- build manifest ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolVersions:
    python: str
    pip: str
    build_script: str = "1"


def build_manifest(
    *,
    app_version: str,
    source_revision: str,
    source_dirty: bool,
    tools: ToolVersions,
    python_embed_zip: Path,
    requirements_lock_text: str,
    stage_dir: Path,
) -> dict[str, object]:
    return {
        "schema": "tfx-quant/build-manifest/1",
        "app_version": app_version,
        "built_at_utc": datetime.now(UTC).isoformat(),
        "source_revision": source_revision,
        "source_dirty": source_dirty,
        "tools": {
            "python": tools.python,
            "pip": tools.pip,
            "build_script": tools.build_script,
        },
        "python_embed": {
            "version": EMBED_PYTHON_VERSION,
            "archive": python_embed_zip.name,
            "sha256": sha256_file(python_embed_zip),
        },
        "dependencies": parse_requirements_lock(requirements_lock_text),
        "staged_file_count": sum(1 for _ in iter_staged_files(stage_dir)),
    }


def write_build_manifest(manifest: Mapping[str, object], out_path: Path) -> None:
    out_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# --- release notes ---------------------------------------------------------


def render_release_notes(template: str, context: Mapping[str, str]) -> str:
    """Substitute ``{{ key }}`` placeholders. An unknown placeholder is left as-is
    so a half-filled template is obvious rather than silently blank."""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        return context.get(key, match.group(0))

    return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", _sub, template)


# --- release manifest --------------------------------------------------------


def make_release_manifest(
    *,
    installer_path: Path,
    stage_build_manifest: Mapping[str, object],
    signed: bool,
    signature_subject: str | None,
) -> dict[str, object]:
    """Ties the produced installer to the exact source it was built from, so a
    client-site binary can be matched against the repo tag and symbols."""
    return {
        "schema": "tfx-quant/release-manifest/1",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "installer": {
            "filename": installer_path.name,
            "sha256": sha256_file(installer_path),
            "size_bytes": installer_path.stat().st_size,
        },
        "app_version": stage_build_manifest.get("app_version"),
        "source_revision": stage_build_manifest.get("source_revision"),
        "source_dirty": stage_build_manifest.get("source_dirty"),
        "build_manifest": stage_build_manifest,
        "signature": {
            "signed": signed,
            "subject": signature_subject,
        },
    }


# --- embedded-python path file ------------------------------------------------


def rewrite_embed_pth(runtime_dir: Path, extra_paths: list[str]) -> Path:
    """The embeddable distribution ships ``python311._pth`` which disables ``site``
    and pins ``sys.path``. Append our own directories and re-enable ``import site``
    so ``Lib\\site-packages`` (pip ``--target`` output) is importable."""
    candidates = sorted(runtime_dir.glob("python*._pth"))
    if not candidates:
        raise BuildError(f"no python*._pth in {runtime_dir}")
    pth = candidates[0]
    existing = pth.read_text(encoding="utf-8").splitlines()
    lines = [line for line in existing if line.strip() and not line.strip().startswith("#")]
    for extra in extra_paths:
        if extra not in lines:
            lines.append(extra)
    if "import site" not in lines:
        lines.append("import site")
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return pth
