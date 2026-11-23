"""A stand-in engine for native/dev/qa/start-failure.jsonl: slow to start, then unable to open the library.

The window must be on screen and answering while this sleeps, and must then say what this says. Never packaged."""
import json
import time

time.sleep(4)
print(json.dumps({'error': 'The stand-in engine has no library to open.'}), flush=True)
