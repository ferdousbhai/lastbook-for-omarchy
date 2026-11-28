"""A stand-in engine for native/dev/qa/offer-town.jsonl: the real engine over the made-up library, on a computer with
no weather location, whose time zone is Khartoum's and which is taken to be online (engine/setup/here.py). The download
it starts is made up: nothing reaches the network.

Never packaged."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import standin  # noqa: E402

os.environ['LASTBOOK_MAP_OFFER'] = '1'
standin.use_real_engine()
from engine.setup import api, here  # noqa: E402

here.zone_place = lambda *a: {'zone': 'Africa/Khartoum', 'name': 'Khartoum', 'area': '', 'lat': 15.6, 'lon': 32.53}
standin.made_up_download(api)
standin.run_real_engine()
