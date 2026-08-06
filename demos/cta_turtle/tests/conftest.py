"""tests/conftest.py — sys.path 注入 + 通用 fixture"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for sub in ("", "helpers"):
    p = ROOT / sub if sub else ROOT
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
