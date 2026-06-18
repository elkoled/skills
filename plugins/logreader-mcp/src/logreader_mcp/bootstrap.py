import os
import sys
from pathlib import Path

_BOOTSTRAPPED = False


def _candidate_roots():
  env = os.environ.get("OPENPILOT_ROOT")
  if env:
    yield Path(env).expanduser()
  yield Path.home() / "openpilot"
  here = Path(__file__).resolve()
  for parent in here.parents:
    if (parent / "openpilot" / "__init__.py").exists() or (parent / "tools" / "lib" / "logreader.py").exists():
      yield parent


def find_openpilot_root() -> Path:
  for root in _candidate_roots():
    if (root / "tools" / "lib" / "logreader.py").exists():
      return root
  raise RuntimeError(
    "Could not find an openpilot checkout. Set OPENPILOT_ROOT to its path "
    "(the dir containing tools/lib/logreader.py)."
  )


def bootstrap() -> Path:
  global _BOOTSTRAPPED
  root = find_openpilot_root()
  if not _BOOTSTRAPPED:
    for p in (str(root), str(root / "opendbc_repo")):
      if p not in sys.path:
        sys.path.insert(0, p)
    os.environ.setdefault("OPENPILOT_ROOT", str(root))
    _BOOTSTRAPPED = True
  return root
