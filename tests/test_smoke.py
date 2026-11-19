"""端到端冒烟测试：子进程跑 tests/selftest.py（自带假 SOCKS5 与源站，不依赖真实网络）。"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_gateway_selftest_passes():
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tests", "selftest.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, output
    assert "全部通过" in output
