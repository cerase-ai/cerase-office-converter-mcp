"""Put the repo root on the path so `import server` works from anywhere,
including `python -m pytest tests/` run from the package root."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
