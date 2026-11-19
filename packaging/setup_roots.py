#!/usr/bin/env python3
"""Builds engine/setup/roots.pem: the only root certificates setup's downloader trusts (engine/setup/download.py).

  python3 -I -B packaging/setup_roots.py <path to certifi's cacert.pem>

Setup does not use the system's trust store: a download must not depend on what a computer happens to trust, or on a root
added to it. So it trusts only the roots that the allowed hosts' chains end in today, plus the sibling roots of those same CAs, so a routine rotation does not break downloads. Taken from Mozilla's
bundle as certifi ships it. The hosts are download.py's ALLOWED_HOSTS and the *.hf.co hosts Hugging Face redirects to. Chains checked 19 Nov 2026:
  downloads.lastbook.org, build.protomaps.com, build-metadata.protomaps.dev (Cloudflare)   Google Trust Services, GTS Root R4
  huggingface.co, cas-bridge.xethub.hf.co, us.aws.cdn.hf.co, cdn-lfs*.hf.co   Amazon Root CA 1 (cross-signed by Starfield G2)
  us.gcp.cdn.hf.co, download.geonames.org   Let's Encrypt, ISRG Root YR, cross-signed by ISRG Root X1
No eMudhra or emSign root, ever. packaging/check_tls_roots.py checks every one of these hosts against the built file.
"""
import base64
import hashlib
import re
import sys
from pathlib import Path

ROOTS = ['GTS Root R1', 'GTS Root R3', 'GTS Root R4', 'GlobalSign ECC Root CA - R4',
         'ISRG Root X1', 'ISRG Root X2',
         'Amazon Root CA 1', 'Amazon Root CA 2', 'Amazon Root CA 3', 'Amazon Root CA 4',
         'Starfield Services Root Certificate Authority - G2']
OUT = Path(__file__).resolve().parents[1] / 'engine' / 'setup' / 'roots.pem'
BLOCK = re.compile(r'# Label: "([^"]+)"\n(?:#[^\n]*\n)*(-----BEGIN CERTIFICATE-----\n.*?\n-----END CERTIFICATE-----)', re.S)


def main():
    src = Path(sys.argv[1])
    version = next((l.split('"')[1] for l in (src.parent / '__init__.py').read_text().splitlines() if '__version__' in l), '?')
    found = {label: pem for label, pem in BLOCK.findall(src.read_text())}
    missing = [r for r in ROOTS if r not in found]
    if missing:
        sys.exit(f'not in {src}: {missing}')
    head = ['# The root certificates setup trusts, and no others. Built by packaging/setup_roots.py from certifi ' + version + '.',
            '# Label, then the SHA-256 fingerprint of the certificate.']
    body = []
    for r in ROOTS:
        if re.search(r'emudhra|emsign', r, re.I):
            sys.exit('never an eMudhra or emSign root')
        der = base64.b64decode(''.join(found[r].splitlines()[1:-1]))
        fp = hashlib.sha256(der).hexdigest().upper()
        head.append(f'#   {r}: {":".join(fp[i:i + 2] for i in range(0, 64, 2))}')
        body.append(f'# {r}\n{found[r]}\n')
    OUT.write_text('\n'.join(head) + '\n\n' + '\n'.join(body), encoding='ascii')
    print(f'{OUT}: {len(ROOTS)} roots from certifi {version}')


if __name__ == '__main__':
    main()
