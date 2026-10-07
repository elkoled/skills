import os
import sys
from pathlib import Path

_BOOTSTRAPPED = False


# the nested layout keeps the source under openpilot/, the flat one at the repo root
def _is_checkout(root: Path) -> bool:
  return any((root / prefix / "tools" / "lib" / "logreader.py").exists() for prefix in ("openpilot", ""))


def _candidate_roots():
  env = os.environ.get("OPENPILOT_ROOT")
  if env:
    yield Path(env).expanduser()
  for start in (Path.cwd(), Path(__file__).resolve()):
    yield from (start, *start.parents)
  yield Path.home() / "openpilot"


def find_openpilot_root() -> Path:
  for root in _candidate_roots():
    if _is_checkout(root):
      return root.resolve()
  raise RuntimeError("Could not find an openpilot checkout. Set OPENPILOT_ROOT to its path.")


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
