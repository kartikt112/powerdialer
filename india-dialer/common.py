"""Paths, config loading and the dependency hop shared by serve.py and import.py.

`python3` on a stock Mac is the system 3.9 without PyYAML. When PyYAML is
missing and the repo's `.venv` exists, the calling script re-runs itself
inside it, the same way dialer/serve.py does. Python 3.9 compatible.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.environ.get("INDIA_DATA_DIR") or os.path.join(HERE, "data")
CONFIG_PATH = os.path.join(HERE, "config.yaml")


def _have(module):
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def ensure_deps():
    """PyYAML is required. phonenumbers is optional but tells Indian mobiles
    from landlines far better, so its absence is also a reason to hop."""
    if _have("yaml") and _have("phonenumbers"):
        return
    venv_python = os.path.join(ROOT, ".venv", "bin", "python")
    inside = os.path.realpath(sys.prefix) == os.path.realpath(os.path.join(ROOT, ".venv"))
    if os.path.exists(venv_python) and not inside and not os.environ.get("INDIA_NO_VENV_HOP"):
        os.environ["INDIA_NO_VENV_HOP"] = "1"
        os.execv(venv_python, [venv_python] + sys.argv)
    if _have("yaml"):
        return                               # run on, with the stdlib number heuristic
    sys.exit("PyYAML is required. From the repo root run:\n"
             "  python3 -m venv .venv && .venv/bin/pip install -r india-dialer/requirements.txt")


def load_config(path=CONFIG_PATH):
    import yaml
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def deep_copy(obj):
    return json.loads(json.dumps(obj, default=str))
