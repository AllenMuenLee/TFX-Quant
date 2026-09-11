"""Run native destruction in a child so heap failures cannot kill pytest."""

import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.real_api
@pytest.mark.skipif(
    sys.platform != "win32" or struct.calcsize("P") != 4,
    reason="Installed quote OCX requires 32-bit Windows Python",
)
def test_quote_host_repeated_native_teardown_and_recreation(tmp_path):
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "faulthandler",
            str(Path(__file__).with_name("quote_host_probe.py")),
            "25",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    (tmp_path / "native-probe.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    assert result.returncode == 0, (
        f"Native exit 0x{result.returncode & 0xFFFFFFFF:08X}\n{result.stdout}\n{result.stderr}"
    )
    assert result.stdout.count('"phase": "collected"') == 25
    assert "PROBE_OK" in result.stdout
