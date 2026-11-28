"""A stand-in engine for native/dev/qa/offer.jsonl: the real engine over the made-up library, on a computer whose
weather location in Omarchy is Testville (dev/seed.py), which has no street map, and which is taken to be online
(engine/setup/here.py). Nothing reaches the network.

Never packaged."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import standin  # noqa: E402

weather = Path.home() / '.local/state/omarchy/settings/weather.json'
weather.parent.mkdir(parents=True, exist_ok=True)
weather.write_text(json.dumps({'name': 'Testville', 'latitude': 12.5, 'longitude': 34.25}))
os.environ['LASTBOOK_MAP_OFFER'] = '1'
standin.use_real_engine()
standin.run_real_engine()
