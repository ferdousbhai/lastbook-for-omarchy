"""A stand-in engine for native/dev/qa/no-encyclopedias.jsonl: a library with no encyclopedia in it. The list of
encyclopedias fails the first time it is asked and is empty after; every other request is answered with an error.
Never packaged."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import standin  # noqa: E402

standin.fail_but('/api/encyclopedias', [])
