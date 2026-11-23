"""A stand-in engine for native/dev/qa/failures.jsonl: it starts, then answers every request with an error, except the
list of shelves, which it gives the second time it is asked (so Try again is seen to ask again).

Each screen that asks and gets nothing must say so in the window. Never packaged."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import standin  # noqa: E402

standin.fail_but('/api/shelves', [{'id': 'water', 'title': 'Water', 'read': 1, 'video': 0}])
