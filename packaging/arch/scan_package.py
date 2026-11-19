"""Looks inside a built lastbook-for-omarchy package and exits 1 if it holds what must not ship.

  python3 packaging/arch/scan_package.py lastbook-for-omarchy-<version>-<arch>.pkg.tar.zst [--terms-file FILE]

Refused: test code, the old web window (GTK 3, lastbooksrc, the Python launcher), model libraries, the Mac and Windows shells, the remote_storage module, machine names (FILE: private
words, one per line, kept out of this public script), /Users/... and /home/<name> paths, the household's area and working names in our
own files (ship_edits.json), native code not built for the package's architecture or that links a library from outside the package and the system
(the window, /usr/bin/lastbook, may also link the GTK 4, WebKitGTK, Poppler and GStreamer it depends on), a socket in the local AI's runner, and in our own Python any network module outside the one way out (setup's downloader) and
the engine's request handler, which the app runs with no port, and retrieval.py's local (AF_UNIX) socket to Recall. roots.pem must be there: without it setup downloads nothing.
"""
import argparse
import ast
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BAD_SEGMENTS = {'test', 'tests', 'testing', 'pytest', 'unittest', 'doctest', 'remote_storage', 'webview', 'bottle', 'proxy_tools', 'onnxruntime',
                'tokenizers', 'shell_windows', 'shell_macos', 'clr', 'clr_loader', 'pythonnet', 'lastbook_launcher', 'tkinter', 'PyInstaller',
                'objc', 'AppKit', 'Foundation', 'PyObjCTools', 'vendor-macos', 'fixture', 'real_data_check', 'cdp', 'f2py', 'pypdfium2_cli',
                'shell_linux', 'lastbooksrc', 'libgstlastbooksrc', 'gst'}
BAD_FILE = re.compile(r'(^|/)(test_[^/]*|[^/]*_test\.(py|pyc|so)|[^/]*_tests[^/]*\.so|[^/]*\.onnx|[^/]*\.gguf|mac_[^/]*|win_[^/]*)$', re.I)
TEST_MARKERS = [b'TEST-STUB', b'mac_offscreen', b'stub_chat', b'stub-chat']
P = 'usr/lib/lastbook-for-omarchy/'
USER_PATH = re.compile(rb'/Users/[A-Za-z0-9._-]+|/home/(?!runner\b|user\b)[a-z_][a-z0-9_.-]*')   # one-letter names too
OUR_PY = re.compile(r'^usr/lib/lastbook-for-omarchy/(server|retrieval|encyclopedia|assistant|llm_local)\.py$|^usr/lib/lastbook-for-omarchy/(app|engine)/.*\.py$')
FIRST_PARTY = re.compile(r'^usr/lib/lastbook-for-omarchy/(content|app|engine|setup)/|^usr/lib/lastbook-for-omarchy/[^/]+\.py$')
NET_MODULES = {'socket', 'ssl', 'http.client', 'urllib.request', 'http.server', 'socketserver', 'asyncio', 'ftplib', 'smtplib', 'xmlrpc', 'requests', 'urllib3'}
NET_ALLOWED = {
    P + 'engine/setup/download.py',   # setup's downloader: HTTPS to the allowlist, roots.pem only
    P + 'server.py',                  # the engine's request handler, run through bridge.py over in-memory streams: it has no port to open
    P + 'engine/setup/api.py',        # BaseHTTPRequestHandler for setup's routes, run the same way
    P + 'engine/library.py',          # http.server's handler base, the same
    P + 'assistant.py',               # a chat model on this computer's loopback, when one is set; the app's own is llm_local over pipes
    P + 'retrieval.py',               # Recall's owner-only Unix socket (checked below: AF_UNIX only)
}
ARCH = {'x86_64': 'X86-64', 'aarch64': 'AArch64'}
SOCKET_CALLS = re.compile(r'^(socket|connect|bind|listen|accept4?|getaddrinfo|gethostbyname\w*|sendto|recvfrom|curl_\w+|SSL_\w+)$')
SYSTEM_LIBS = re.compile(r'^(libc|libm|libdl|libpthread|librt|libstdc\+\+|libgcc_s|libgomp|libvulkan|ld-linux-x86-64|ld-linux-aarch64|libz|libglib-2\.0|libgobject-2\.0)\.so')
APP_ID = 'io.github.ferdousbhai.Lastbook'
WINDOW = 'usr/bin/lastbook'
# What the window links besides SYSTEM_LIBS: the GTK 4 stack, WebKitGTK 6, Poppler and GStreamer, all in depends=().
WINDOW_LIBS = re.compile(r'^(libgtk-4|libgdk_pixbuf-2\.0|libgio-2\.0|libgmodule-2\.0|libpango(cairo)?-1\.0|libcairo(-gobject)?|libharfbuzz|libgraphene-1\.0|'
                         r'libwebkitgtk-6\.0|libjavascriptcoregtk-6\.0|libpoppler-glib|libgst[a-z]*-1\.0|libsoup-3\.0|libsqlite3)\.so')   # sqlite: the map files


def bad_name(name):
    parts = re.split(r'[./]', name)
    return sorted((set(parts) & BAD_SEGMENTS) | {p for p in parts if p.endswith('_tests') or p.startswith('test_')})


def net_imports(source):
    out = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return sorted(m for m in out if m in NET_MODULES or m.split('.')[0] in {'requests', 'urllib3', 'xmlrpc'})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pkg', type=Path)
    ap.add_argument('--terms-file', type=Path)
    a = ap.parse_args()
    terms = []
    if a.terms_file and a.terms_file.is_file():
        terms = [t.strip().lower().encode() for t in a.terms_file.read_text(encoding='utf-8').splitlines() if t.strip()]
    print(f'private words checked: {len(terms)}' + ('' if terms else ' (no terms file: machine names NOT checked)'))
    here = Path(__file__).resolve().parent
    ship = json.loads((here.parent / 'ship_edits.json').read_text(encoding='utf-8'))
    household = re.compile('|'.join(ship['forbidden']).encode(), re.I)
    problems, remote_hits, user_paths, net_report = [], {}, {}, {}
    n_files = n_elf = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(['bsdtar', '-xf', str(a.pkg), '-C', str(root)], check=True)
        info = dict(line.split(' = ', 1) for line in (root / '.PKGINFO').read_text().splitlines() if ' = ' in line)
        print({k: info.get(k) for k in ('pkgname', 'pkgver', 'arch', 'size')})
        machine = ARCH[info['arch']]
        packaged = {p.relative_to(root).as_posix() for p in root.rglob('*') if (p.is_file() or p.is_symlink()) and not p.name.startswith('.')}
        for rel in sorted(packaged):
            p = root / rel
            if p.is_symlink():
                target = p.readlink().as_posix()
                if target.startswith('/') or '..' in target:
                    problems.append(f'symlink out of the package: {rel} -> {target}')
                continue
            n_files += 1
            if not (rel.startswith((P, 'usr/share/licenses/lastbook-for-omarchy/')) or rel in (WINDOW, f'usr/share/applications/{APP_ID}.desktop')
                    or re.fullmatch(rf'usr/share/icons/hicolor/\d+x\d+/apps/{re.escape(APP_ID)}\.png', rel)):
                problems.append(f'file outside the places the package owns: {rel}')
            if bad_name(rel.removeprefix(P)) or BAD_FILE.search(rel) or p.name in ship['never_ship']:
                problems.append(f'file not allowed: {rel}')
            data = p.read_bytes()
            low = data.lower()
            for t in terms:
                if t in low:
                    problems.append(f'private word #{terms.index(t) + 1} in {rel}')
            for m in TEST_MARKERS:
                if m in data:
                    problems.append(f'test marker {m.decode()} in {rel}')
            if b'remote_storage' in data and not rel.endswith('.pyc'):
                remote_hits[rel] = data.count(b'remote_storage')
            for u in set(USER_PATH.findall(data)):
                user_paths.setdefault(u.decode('ascii', 'replace'), []).append(rel)
            if FIRST_PARTY.search(rel):
                m = household.search(data)
                if m:
                    problems.append(f'household or working name in {rel}: {data[max(0, m.start() - 30):m.end() + 30]!r}')
            if OUR_PY.search(rel):
                mods = net_imports(data.decode('utf-8'))
                if mods:
                    net_report[rel] = mods
                    if rel not in NET_ALLOWED:
                        problems.append(f'network module in {rel}: {mods}')
                    elif rel == P + 'retrieval.py' and (b'AF_INET' in data or b'AF_UNIX' not in data or mods != ['socket']):
                        problems.append(f'retrieval.py may only open the local socket to Recall: {mods}')
            if data[:4] == b'\x7fELF':
                n_elf += 1
                head = subprocess.run(['readelf', '-h', str(p)], capture_output=True, text=True).stdout
                if machine not in head:
                    problems.append(f'not {machine}: {rel}')
                dyn = subprocess.run(['readelf', '-d', str(p)], capture_output=True, text=True).stdout
                beside = {q.name for q in p.parent.iterdir()}
                for lib in re.findall(r'\(NEEDED\)\s+Shared library: \[([^\]]+)\]', dyn):
                    if lib not in beside and not SYSTEM_LIBS.match(lib) and not rel.startswith(P + 'lib/') and not (rel == WINDOW and WINDOW_LIBS.match(lib)):
                        problems.append(f'links outside the package and the system: {rel} -> {lib}')
                if rel.startswith(P + 'llm/'):     # the local AI's runner may not reach a network at all
                    syms = subprocess.run(['nm', '-D', '--undefined-only', str(p)], capture_output=True, text=True).stdout.split()
                    calls = sorted({s.split('@')[0] for s in syms if SOCKET_CALLS.match(s.split('@')[0])})
                    if calls:
                        problems.append(f'network calls in {rel}: {calls[:8]}')
        for rel in remote_hits:
            problems.append(f'remote_storage named in {rel}')
        for u, where in user_paths.items():
            if u.startswith('/Users/') or any(FIRST_PARTY.search(w) or w == WINDOW for w in where):
                problems.append(f'path of a person\'s folder {u} in {where[:3]}')
        for need in [P + f for f in ('engine/setup/roots.pem', 'app/engine_pipe.py', 'app/main.py', 'app/bridge.py', 'setup/manifest-v1.json',
                                      'VERSION', 'encyclopedia.py', 'server.py')] + [WINDOW, f'usr/share/applications/{APP_ID}.desktop']:
            if need not in packaged:
                problems.append(f'missing from the package: {need}')
        if WINDOW in packaged and (root / WINDOW).read_bytes()[:4] != b'\x7fELF':
            problems.append(f'{WINDOW} is not the native window (not an ELF program)')
        llm = P + 'llm/lastbook-llm' in packaged
    print(f'files {n_files}, native binaries {n_elf}, local AI runner: {"included" if llm else "not included"}')
    print(f'network modules in our Python (allowed there): {net_report or "none"}')
    print(f'remote_storage string: {remote_hits or "nowhere"} (the module itself is refused above)')
    print(f'home-folder strings: { {u: w[:3] for u, w in user_paths.items()} or "none"}')
    for line in problems:
        print('PROBLEM:', line)
    print('SCAN FAILED' if problems else 'SCAN PASSED')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
