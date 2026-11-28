# Contributing to Lastbook

Thank you for helping. Lastbook is meant for the day the internet is gone, so what matters most is that it works offline,
stays correct, and never sends anything anywhere.

## Set up

1. Clone the repository on Omarchy, install Rust, and use the system's Python 3 (`/usr/bin/python3`, which the window starts),
   with `python-numpy` from pacman.
2. Build the window with `cargo build --manifest-path native/Cargo.toml`. `bin/test-deps` fetches the two Python packages
   Arch does not carry (README, "Run from source").
3. Get a data folder: run the window (README, "Run from source") and let setup download the Small pack, or unzip a pack
   from lastbook.org. Point the app at it with `--data` or `LASTBOOK_DATA`.

The README explains the layout.

## Test

```bash
bin/test
```

Run it before you open a pull request. It also drives the window through the scripts in `native/dev/qa` over a small
made-up library, on a broadway display of its own (`gtk4-broadwayd`, part of `gtk4`), so no window opens: a change to
what the window shows or how it is reached may need one of them changed, and `native/dev/qa.sh <script> <folder>` keeps
the pictures they take. If you change the packaging, also run the build: it ends
with a package scan, and that scan must pass. If you change a guide in `content/`, run `tools/validate_topics.py`
against a library, so every id, page and encyclopedia path it cites still exists:

```bash
PYTHONPATH=$(bin/test-deps) python3 tools/validate_topics.py --data /path/to/LastbookData --allow-absent      # or set LASTBOOK_DATA
```

`bin/test-deps` provides libzim, which the encyclopedia paths are checked with. The guides also cite documents and
encyclopedias that only the larger packs hold, and the app leaves those out of a smaller pack; `--allow-absent` lists
them under "NOT IN THIS LIBRARY" without counting them, so on Small the run ends `ERRORS 0` and a new error is yours. A
mistyped id is listed there too, so read that list for anything you added. Leave out `--allow-absent` on the All pack.
It reads the library's files directly and needs no server. `lastbook search` takes the same `--data` and finds the page
you mean to cite, and `lastbook read --pages` reads it: `lastbook --data /path/to/LastbookData search "boil water"`.

## Rules that keep Lastbook what it is

**No network.** The app opens no port and makes no connection but setup's downloads, as the README's
[Privacy and network use](README.md#privacy-and-network-use) sets out. Keep it so:

- No telemetry, analytics, crash reporting, fonts or scripts from the web, update checks, or "phone home" of any kind.
- A new dependency must work fully offline, and must not open a socket or start a server. Say in the pull request what
  it is, its licence, and why it is needed, and add it to `THIRD_PARTY_NOTICES.md`.

**General wording, no household.** Lastbook began as one household's library. The shipped guides use general wording, and
nothing shared may name a real person's home, street, coordinates, machines or accounts.

- Write guides for any reader in any place. Name a region only when the source is about that region.
- Never commit real coordinates of a home, user or machine names, LAN
  addresses, keys or tokens. Test paths use placeholders such as `/path/to/lastbook`.
- The package is built from the committed source, so what you commit is what ships. The package scan
  (`packaging/arch/scan_package.py`) refuses home-folder paths; it cannot know your other personal details.

**Sources first.** A guide card points at a real page of a real source in the library, with its page number. If the
library does not cover something, record that in `content/gaps.json` instead of writing advice from memory.

**The skill says what the commands do.** An agent knows the command line only through `skills/lastbook/SKILL.md`. A
change to a command's arguments, output or errors changes the skill in the same pull request, and the skill stays short:
what an agent needs to answer well, nothing about how the engine works, and nothing a command's own output or error
already says.

**Plain words.** Interface text is short, concrete and calm. Many readers will be stressed, tired, or reading in a second
language.

## Pull requests

- Keep each change small and about one thing, and say what you checked and how.
- Screenshots help for any change you can see, in light and dark mode. `native/dev/preview.sh` takes one of the window
  over a small made-up library, in a nested Hyprland with its own home folder, so nothing of your desktop or your
  library is in the picture; its header lists the screens and themes it takes.
- Be ready to explain anything that touches setup, the bridge, the scans or the downloader. These keep people's libraries
  safe and their computers private.

## Reporting a security or privacy problem

Do not open a public issue. Email ferdous@hey.com with what you found and how to reproduce it.

## Code of conduct

Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).
