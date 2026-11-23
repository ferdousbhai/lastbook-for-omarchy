"""A stand-in engine for native/dev/qa/failures.jsonl: it starts, then answers every request with an error, except the
list of shelves, which it gives the second time it is asked (so Try again is seen to ask again).

Each screen that asks and gets nothing must say so in the window. Never packaged."""
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
    if path == '/api/shelves' and path in asked:
        answer(req['id'], 200, [{'id': 'water', 'title': 'Water', 'read': 1, 'video': 0}])
    else:
        answer(req['id'], 500, {'error': 'The stand-in engine could not answer.'})
    asked.add(path)
