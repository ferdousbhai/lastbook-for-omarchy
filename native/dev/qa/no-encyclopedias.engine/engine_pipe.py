"""A stand-in engine for native/dev/qa/no-encyclopedias.jsonl: a library with no encyclopedia in it. The list of
encyclopedias fails the first time it is asked and is empty after; every other request is answered with an error.
Never packaged."""
import json
import sys


def send(obj):
    sys.stdout.write(json.dumps(obj) + '\n')
    sys.stdout.flush()


def answer(rid, status, body):
    send({'id': rid, 'head': status, 'type': 'application/json'})
    send({'id': rid, 'chunk': json.dumps(body)})
    send({'id': rid, 'end': True})


data = sys.argv[sys.argv.index('--data') + 1]
send({'ready': {'setup': False, 'data': data, 'library': data + '/library-data', 'version': 'stand-in'}})
asked = set()
for line in sys.stdin:
    req = json.loads(line)
    if 'cancel' in req:
        continue
    path = req['path']
    if path == '/api/encyclopedias' and path in asked:
        answer(req['id'], 200, [])
    else:
        answer(req['id'], 500, {'error': 'The stand-in engine could not answer.'})
    asked.add(path)
