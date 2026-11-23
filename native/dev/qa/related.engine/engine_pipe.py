"""A stand-in engine for native/dev/qa/related.jsonl: the real engine over the made-up library, with two answers
made up, since that library has too little in it to give them: Related lists three neighbours of any document, and
Recall for Omarchy is not installed, whatever this computer has.

Never packaged."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import standin  # noqa: E402

standin.use_real_engine()
import retrieval  # noqa: E402
import server  # noqa: E402

retrieval.recall_installed = lambda: False


def related(doc_id='', path='', k=5):
    cat = server.catalog()['by_id']
    return [{**server._src(cat[d]), 'page': None} for d in ('eng-cea09c7220cd4bd0', 'rope')] + [
        {'doc_id': 'zim:mdwiki_preview.zim/Water_purification', 'kind': 'article', 'title': 'Water purification',
         'wiki': 'mdwiki_preview.zim/Water_purification', 'encyclopedia': 'Preview encyclopedia', 'page': None}]


server.related = related
standin.run_real_engine()
