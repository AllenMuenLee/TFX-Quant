"""Dedicated installer / updater debug log.

The prompt's "除錯日誌需求" for Feature 16:

    installer／updater 記錄 package/app version、OS/architecture、前置檢查結果、
    元大 API readiness、權限、磁碟空間、每一步驟、exit code 與 rollback 結果；
    帳密、授權資料及完整使用者路徑須遮蔽。

So: one JSON object per line, every value passed through :func:`mask_path` (user
directories collapse to ``<LOCALAPPDATA>`` / ``<APPDATA>`` / ``<USERPROFILE>``
tokens) and every key whose name marks it sensitive reduced to a presence boolean.
No password, certificate password, account number, or absolute home path is ever
written.

Usable two ways:

* from Python — :class:`InstallLogger` (the updater in :mod:`tfx_quant.packaging.migrate`
  uses it);
* from the Inno Setup script — ``python -m tfx_quant.packaging.install_log
  --log <path> --event <name> --field k=v --field k=v`` (see :func:`main`).
"""

from __future__ import annotations

import json
import os
import platform
import sys
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from tfx_quant.packaging.prechecks import PrecheckResult
from tfx_quant.telemetry.masking import field_present, mask_account

_SENSITIVE_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "credential",
    "token",
    "pfx",
    "private_key",
    "apikey",
    "api_key",
)
_ACCOUNT_KEY_MARKERS = ("account", "acno", "user_id", "userid", "歸戶")


def _timestamp_token() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def default_log_dir() -> Path:
    """``%LOCALAPPDATA%\\tfx_quant\\logs`` on Windows, falling back to the home
    directory elsewhere — the same rule :mod:`tfx_quant.desktop.__main__` uses."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "tfx_quant" / "logs"


def _known_user_prefixes() -> list[tuple[str, str]]:
    """(absolute directory, replacement token) pairs, longest path first so the most
    specific match wins (``LOCALAPPDATA`` is under ``USERPROFILE``)."""
    pairs: list[tuple[str, str]] = []
    for env_name, token in (
        ("LOCALAPPDATA", "<LOCALAPPDATA>"),
        ("APPDATA", "<APPDATA>"),
        ("USERPROFILE", "<USERPROFILE>"),
        ("HOMEPATH", "<USERPROFILE>"),
    ):
        value = os.environ.get(env_name)
        if value:
            pairs.append((value.rstrip("\\/"), token))
    try:
        home = str(Path.home())
    except (RuntimeError, OSError):
        home = ""
    if home:
        pairs.append((home.rstrip("\\/"), "<USERPROFILE>"))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return pairs


def mask_path(value: str) -> str:
    """Collapse any user-directory prefix to a token, then scrub a leftover
    ``C:\\Users\\<name>`` segment. A value with no path shape is returned unchanged.
    """
    masked = value
    for prefix, token in _known_user_prefixes():
        if masked.lower().startswith(prefix.lower()):
            masked = token + masked[len(prefix) :]
            break
    # Catch a home directory that is not one of the current process's env vars
    # (e.g. a path captured on another machine, or C:\Users\someone-else\...).
    for sep in ("\\", "/"):
        marker = f"{sep}Users{sep}"
        idx = masked.find(marker)
        if idx != -1:
            rest = masked[idx + len(marker) :]
            tail = rest.split(sep, 1)
            remainder = f"{sep}{tail[1]}" if len(tail) == 2 else ""
            masked = f"{masked[:idx]}{sep}Users{sep}<user>{remainder}"
    return masked


def _looks_like_path(value: str) -> bool:
    return "\\" in value or "/" in value or (len(value) >= 2 and value[1] == ":")


def _sanitize_key_value(key: str, value: Any) -> tuple[str, Any]:
    lowered = key.lower()
    if any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS):
        return f"{key}_present", field_present(value)
    if any(marker in lowered for marker in _ACCOUNT_KEY_MARKERS) and isinstance(value, str):
        return f"{key}_masked", mask_account(value)
    if isinstance(value, str) and _looks_like_path(value):
        return key, mask_path(value)
    if isinstance(value, Path):
        return key, mask_path(str(value))
    return key, value


def sanitize_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Public so tests and the updater can check a payload before it is written."""
    out: dict[str, Any] = {}
    for key, value in fields.items():
        safe_key, safe_value = _sanitize_key_value(key, value)
        out[safe_key] = safe_value
    return out


@dataclass(frozen=True, slots=True)
class _EnvSnapshot:
    os_version: str
    architecture: str
    python_version: str
    is_admin: bool

    def as_fields(self) -> dict[str, Any]:
        return {
            "os_version": self.os_version,
            "architecture": self.architecture,
            "python_version": self.python_version,
            "privileges": "admin" if self.is_admin else "standard",
        }


def _is_admin() -> bool:
    if sys.platform != "win32":
        geteuid = getattr(os, "geteuid", None)
        return geteuid() == 0 if geteuid is not None else False
    try:
        import ctypes

        is_admin = ctypes.windll.shell32.IsUserAnAdmin
        return bool(is_admin())
    except Exception:
        return False


def _env_snapshot() -> _EnvSnapshot:
    return _EnvSnapshot(
        os_version=f"{platform.system()} {platform.release()} ({platform.version()})",
        architecture=f"{platform.architecture()[0]} {platform.machine()}",
        python_version=platform.python_version(),
        is_admin=_is_admin(),
    )


class InstallLogger:
    """Append-only JSON-lines writer. One instance per installer/updater run."""

    def __init__(
        self,
        *,
        phase: str,
        package_version: str,
        app_version: str,
        log_path: Path | None = None,
    ) -> None:
        self._phase = phase
        self._path = log_path or (default_log_dir() / f"{phase}-{_timestamp_token()}.log")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self._path.open("a", encoding="utf-8", newline="\n")
        self._env = _env_snapshot()
        self.event(
            "run_started",
            phase=phase,
            package_version=package_version,
            app_version=app_version,
            **self._env.as_fields(),
        )

    @property
    def path(self) -> Path:
        return self._path

    def event(self, event_name: str, **fields: Any) -> None:
        record = {
            "ts_utc": datetime.now(UTC).isoformat(),
            "phase": self._phase,
            "event": event_name,
            **sanitize_fields(fields),
        }
        self._stream.write(json.dumps(record, ensure_ascii=False, default=str))
        self._stream.write("\n")
        self._stream.flush()

    def precheck_results(self, results: Iterable[PrecheckResult]) -> None:
        for result in results:
            self.event(
                "precheck",
                name=result.name,
                passed=result.passed,
                severity=result.severity.value,
                message=result.message,
            )

    @contextmanager
    def step(self, name: str, **fields: Any) -> Iterator[None]:
        started = datetime.now(UTC)
        self.event("step_started", step=name, **fields)
        try:
            yield
        except BaseException as exc:  # noqa: BLE001 - re-raised after logging
            self.event(
                "step_finished",
                step=name,
                exit_code=1,
                error_type=type(exc).__name__,
                duration_ms=(datetime.now(UTC) - started).total_seconds() * 1000,
            )
            raise
        else:
            self.event(
                "step_finished",
                step=name,
                exit_code=0,
                duration_ms=(datetime.now(UTC) - started).total_seconds() * 1000,
            )

    def close(self, *, exit_code: int = 0, rollback_result: str | None = None) -> None:
        self.event("run_finished", exit_code=exit_code, rollback_result=rollback_result or "none")
        self._stream.close()

    def __enter__(self) -> InstallLogger:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close(exit_code=1 if exc_type is not None else 0)


def append_event(log_path: Path, event: str, fields: Mapping[str, Any]) -> None:
    """Write one sanitized JSON line to ``log_path`` without a run banner — for the
    Inno Setup script, which appends many events to the same file across a run."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts_utc": datetime.now(UTC).isoformat(),
        "phase": "installer",
        "event": event,
        **sanitize_fields(fields),
    }
    with log_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, default=str))
        stream.write("\n")


def _parse_fields(pairs: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--field expects key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        fields[key] = value
    return fields


def main(argv: list[str] | None = None) -> int:
    """``python -m tfx_quant.packaging.install_log`` — a one-shot event appender the
    Inno Setup script shells out to."""
    import argparse

    parser = argparse.ArgumentParser(prog="tfx_quant.packaging.install_log")
    parser.add_argument("--log", type=Path, required=True, help="log file to append to")
    parser.add_argument("--event", required=True)
    parser.add_argument("--field", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args(argv)

    append_event(args.log, args.event, _parse_fields(args.field))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
