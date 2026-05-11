import urllib.request
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "funds"))

from http_utils import safe_urlopen


def test_safe_urlopen_rejects_file_scheme():
    req = urllib.request.Request("file:///tmp/secret")

    with pytest.raises(ValueError):
        safe_urlopen(req, timeout=1)


def test_safe_urlopen_rejects_missing_scheme():
    with pytest.raises(ValueError):
        safe_urlopen("example.com/no-scheme", timeout=1)
