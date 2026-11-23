"""A stand-in engine for native/dev/qa/related.jsonl: the real engine over the made-up library, with two answers
made up, since that library has too little in it to give them: Related lists three neighbours of any document, and
Recall for Omarchy is not installed, whatever this computer has.

Never packaged."""
import os
import runpy
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(REPO / 'app'), str(REPO)]
os.environ['LASTBOOK_DATA'] = sys.argv[sys.argv.index('--data') + 1]   # engine/paths.py reads it when first imported
import retrieval  # noqa: E402
import server  # noqa: E402

retrieval.recall_installed = lambda: False


def related(doc_id='', path='', k=5):
    cat = server.catalog()['by_id']
    return [{**server._src(cat[d]), 'page': None} for d in ('eng-cea09c7220cd4bd0', 'rope')] + [
        {'doc_id': 'zim:mdwiki_preview.zim/Water_purification', 'kind': 'article', 'title': 'Water purification',
         'wiki': 'mdwiki_preview.zim/Water_purification', 'encyclopedia': 'Preview encyclopedia', 'page': None}]


server.related = related
sys.argv[0] = str(REPO / 'app' / 'engine_pipe.py')
runpy.run_path(sys.argv[0], run_name='__main__')
