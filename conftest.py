"""pytest 引导：把工程根目录放进 sys.path，保证 `from core import ...` 可用。"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
