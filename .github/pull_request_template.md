## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## How I checked it

<!-- Tests you ran, builds, and screenshots for anything you can see (light and dark). -->

## Checklist

- [ ] The tests pass (`bin/test`).
- [ ] No new network access: no port, socket, telemetry, automatic update check or new allowed host. Network use only through `engine/setup/download.py`.
- [ ] No personal or household details: no home area, coordinates, user or machine names, LAN addresses, keys or tokens.
- [ ] Guide changes cite real pages in the library, and `tools/validate_topics.py --allow-absent` passes (ERRORS 0).
- [ ] Any new dependency is offline-only and listed in `THIRD_PARTY_NOTICES.md` with its licence.
