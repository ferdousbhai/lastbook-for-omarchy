"""The shared model server (github.com/ferdousbhai/model-server): one llama.cpp server every app on this computer uses, so a
model another app already loaded is not loaded a second time. When it is installed, llm_local.py asks it through this file;
otherwise Lastbook runs its own program over pipes, as before.

This is the only file of the assistant that opens a connection, and only to this computer: 127.0.0.1, the address
`model-server status` gives, with the key `model-server key` prints (a file only this user can read). Nothing here can
name another computer (app/tests/test_bridge.py checks).
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time

LOOPBACK = 'http://127[.]0[.]0[.]1'
LOAD_WAIT = 600          # longest a question waits for the model to load


class TooLong(Exception):
    """The prompt does not fit the context the server gives the model."""


class SharedGone(Exception):
    """The server or its model went away in the middle of a question."""


class Shared:
    def __init__(self, program, url, key, model):
        self.program, self.url, self.key, self.model = program, url, key, model
        self.special = {}             # template markup already turned into tokens

    @staticmethod
    def program():
        return None if os.environ.get('LASTBOOK_SHARED_AI') == '0' else shutil.which('model-server')

    @classmethod
    def find(cls, model, in_cache):
        """The server, started and offering the model (in_cache() says whether the checked file is what the Hugging Face
        cache offers under that name); or None, and Lastbook runs its own program."""
        program = cls.program()
        if not program:
            return None
        try:
            if not in_cache():
                return None
            if subprocess.run([program, 'start'], stdin=subprocess.DEVNULL, capture_output=True, timeout=60).returncode:
                return None
            key = subprocess.run([program, 'key'], stdin=subprocess.DEVNULL, capture_output=True, timeout=10).stdout.decode().strip()
            status = json.loads(subprocess.run([program, 'status'], stdin=subprocess.DEVNULL, capture_output=True, timeout=10).stdout)
            self = cls(program, str(status['url']).rstrip('/'), key, model)
            if not key or not re.fullmatch(LOOPBACK + r':\d+', self.url):
                return None
            t0 = time.time()
            while self.state() is None:          # a model new to the cache: the server looks again within seconds
                if time.time() - t0 > 45:
                    return None
                time.sleep(1)
            return self
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as e:
            print('local AI: the shared model server is not usable:', type(e).__name__, e, flush=True)
            return None

    def _open(self, method, path, body=None, timeout=30):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', int(self.url.rsplit(':', 1)[1]), timeout=timeout)   # this computer, never another
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
        conn.request(method, path, body=data, headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        conn.socket = conn.sock       # kept: http.client hands the socket to the response, and stopping an answer shuts it
        return conn, conn.getresponse()

    def _call(self, method, path, body=None, timeout=30):
        conn, r = self._open(method, path, body, timeout)
        try:
            raw = r.read()
            if r.status != 200:
                raise OSError(f'{path}: HTTP {r.status} {raw[:200]!r}')
            return json.loads(raw)
        finally:
            conn.close()

    def state(self):
        """'loaded', 'loading', 'sleeping', 'unloaded'...; None when the server does not offer the model (or is gone)."""
        try:
            return next((m['status']['value'] for m in self._call('GET', '/models', timeout=5)['data'] if m['id'] == self.model), None)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def loaded(self):
        return self.state() in ('loaded', 'sleeping')

    def load(self, on_pct=lambda pct: None):
        """Waits until the model is in memory. Returns None, or a sentence saying why it is not."""
        try:
            if not self.loaded():
                self._call('POST', '/models/load', {'model': self.model})
            t0 = time.time()
            while time.time() - t0 < LOAD_WAIT:
                m = next((m for m in self._call('GET', '/models', timeout=5)['data'] if m['id'] == self.model), None)
                st = m and m['status']['value']
                if st in ('loaded', 'sleeping'):
                    return None
                if st in (None, 'unloaded') and time.time() - t0 > 5:
                    return 'The local AI could not load the model file.'
                value = ((m['status'].get('progress') or {}).get('value')) if m else None
                if isinstance(value, (int, float)):
                    on_pct(int(value * 100))
                time.sleep(0.5)
            return 'The local AI took too long to start.'
        except (OSError, ValueError, KeyError, TypeError) as e:
            print('local AI: shared load failed:', type(e).__name__, e, flush=True)
            return 'The local AI could not load the model file.'

    def _tokens(self, segments):
        """The prompt as token numbers. Template markup is read for special tokens; text (a document, a question) never is,
        so nothing in it can start a turn of its own. The server is given numbers only."""
        def tok(text, special, bos=False):
            return self._call('POST', '/tokenize', {'model': self.model, 'content': text, 'parse_special': special,
                                                   'add_special': bos})['tokens']
        if 'bos' not in self.special:
            self.special['bos'] = tok('', True, bos=True)
        out = list(self.special['bos'])
        for text, special in segments:
            if not text:
                continue
            if special:
                if text not in self.special:
                    if len(self.special) > 512:
                        self.special = {'bos': self.special['bos']}
                    self.special[text] = tok(text, True)
                out += self.special[text]
            else:
                out += tok(text, False)
        return out

    def generate(self, segments, max_tokens, cancelled, on_text, seed):
        import queue
        try:
            body = {'model': self.model, 'prompt': self._tokens(segments), 'n_predict': max_tokens, 'temperature': 0.2,
                    'top_k': 64, 'top_p': 0.95, 'min_p': 0.05, 'seed': seed, 'stop': ['<|tool_response>'],
                    'samplers': ['top_k', 'top_p', 'min_p', 'temperature'], 'stream': True, 'cache_prompt': True}
            conn, r = self._open('POST', '/completion', body, timeout=None)
        except (OSError, ValueError, KeyError) as e:
            raise SharedGone(str(e)) from e
        if r.status != 200:
            raw = r.read()[:600].decode('utf-8', 'replace')
            conn.close()
            if r.status == 400 and 'context' in raw:
                raise TooLong()
            print('local AI: the shared model server answered', r.status, raw, flush=True)
            raise SharedGone(raw)
        events = queue.Queue()

        def read():
            try:
                for line in r:
                    if line.startswith(b'data: '):
                        events.put(json.loads(line[6:]))
                events.put(None)
            except Exception as e:    # the connection ended under the reader (an answer was stopped, or the server went)
                events.put(e)
        threading.Thread(target=read, daemon=True, name='local-ai-shared-read').start()
        n_gen = 0
        try:
            while True:
                if cancelled():
                    return {'ev': 'done', 'reason': 'cancel', 'n_gen': n_gen}    # closing the connection stops the server's work
                try:
                    ev = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if ev is None or isinstance(ev, Exception):
                    raise SharedGone(str(ev))
                if ev.get('error'):
                    print('local AI: the shared model server said', ev['error'], flush=True)
                    raise SharedGone(str(ev['error']))
                if ev.get('content'):
                    n_gen += 1
                    on_text(ev['content'])
                if ev.get('stop'):
                    reason = {'eos': 'eog', 'word': 'stop', 'limit': 'length'}.get(ev.get('stop_type'), 'eog')
                    done = {'ev': 'done', 'reason': reason, 'n_prompt': ev.get('tokens_evaluated'),
                            'n_reused': ev.get('tokens_cached'), 'n_gen': ev.get('tokens_predicted')}
                    if ev.get('stopping_word'):
                        done['stop'] = ev['stopping_word']
                    return done
        finally:
            try:                      # the server stops writing the answer when the connection goes
                import socket
                conn.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            r.close()
            conn.close()
