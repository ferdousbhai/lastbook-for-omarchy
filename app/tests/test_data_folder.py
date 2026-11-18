#!/usr/bin/env python3
"""Where the app looks for its library, and the first-open question. Temporary folders and a made-up home only: never the real Documents.

  python3 -B app/tests/test_data_folder.py [-v]

Never packaged."""
import json, os, shutil, stat, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parents[1])]
import main  # noqa: E402


def library(folder):
    (folder / 'library-data').mkdir(parents=True)
    (folder / 'library-data' / 'catalog.json').write_text('{}', encoding='utf-8')
    return folder


class Lookup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='lastbook-lookup-')).resolve()
        self.home = self.tmp / 'home'
        self.home.mkdir()
        env = {k: v for k, v in os.environ.items() if k != 'LASTBOOK_DATA' and not k.startswith('XDG_')}
        env['HOME'] = str(self.home)
        # the window itself is never opened here: main() only asks it whether another copy is open
        patches = [mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(sys.modules, {'shell_linux': SimpleNamespace(claim=lambda folder: True, before_exec=lambda: None)})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(Path.home(), self.home)

    def tearDown(self):
        for p in self.tmp.rglob('*'):
            if p.is_dir():
                p.chmod(0o755)
        shutil.rmtree(self.tmp)

    def settings(self):
        return self.home / '.config' / 'Lastbook' / 'settings.json'

    def test_nothing_found(self):
        (self.home / 'Downloads' / 'Other').mkdir(parents=True)
        self.assertIsNone(main.data_folder(None))

    def test_order(self):
        app_data = library(main.default_parent() / 'LastbookData')
        chosen = library(self.home / 'Documents' / 'Mine')
        main.remember(chosen)
        self.assertEqual(main.data_folder(None), chosen)
        shutil.rmtree(chosen)
        self.assertEqual(main.data_folder(None), app_data)

    def test_fresh_start_does_not_probe_personal_folders(self):
        for name in ('Downloads', 'Documents', 'Desktop'):
            library(self.home / name / 'LastbookData')
        original_stat, original_iterdir, original_open = Path.stat, Path.iterdir, Path.open
        protected = [self.home / name for name in ('Downloads', 'Documents', 'Desktop')]

        def guard(path):
            self.assertFalse(any(path == root or root in path.parents for root in protected), str(path))

        def stat(path, *args, **kwargs):
            guard(path)
            return original_stat(path, *args, **kwargs)

        def iterdir(path):
            guard(path)
            return original_iterdir(path)

        def read(path, *args, **kwargs):
            guard(path)
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, 'stat', stat), mock.patch.object(Path, 'iterdir', iterdir), mock.patch.object(Path, 'open', read):
            self.assertIsNone(main.data_folder(None))
            self.assertEqual(main.libraries_here(), [])
            with mock.patch.object(sys, 'argv', ['Lastbook']), mock.patch.object(main, 'run_setup', return_value=7):
                self.assertEqual(main.main(), 7)

    def test_named_folder_wins(self):
        library(self.home / 'Downloads' / 'LastbookData')
        given = self.tmp / 'given'
        self.assertEqual(main.data_folder(str(given)), given)
        with mock.patch.dict(os.environ, {'LASTBOOK_DATA': str(given)}):
            self.assertEqual(main.data_folder(None), given)

    def test_not_writable_skipped(self):
        locked = library(self.tmp / 'locked' / 'LastbookData')
        main.remember(locked)
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        if os.access(locked, os.W_OK):
            self.skipTest('running as a user who can write anywhere')
        found = library(main.default_parent() / 'LastbookData')
        self.assertEqual(main.data_folder(None), found)
        self.assertIn('cannot save', main.check(locked)[1])

    def test_check_takes_the_folder_or_its_parent(self):
        data = library(self.tmp / 'Lastbook-Small' / 'LastbookData')
        self.assertEqual(main.check(data), (data, None))
        self.assertEqual(main.check(data.parent), (data, None))
        found, why = main.check(self.tmp)
        self.assertIsNone(found)
        self.assertIn('is not a Lastbook library', why)
        self.assertNotIn(str(self.tmp.parent), why)   # the folder's name, not its whole path

    def test_remembered_gone_searches_again(self):
        gone = library(self.tmp / 'usb' / 'LastbookData')
        main.remember(gone)
        self.assertEqual(json.loads(self.settings().read_text())['library'], str(gone))
        shutil.rmtree(gone)
        found = library(main.default_parent() / 'LastbookData')
        self.assertEqual(main.data_folder(None), found)
        shutil.rmtree(found)
        self.assertIsNone(main.data_folder(None))

    def test_bad_settings_ignored(self):
        self.settings().parent.mkdir(parents=True)
        self.settings().write_text('not json')
        found = library(main.default_parent() / 'LastbookData')
        self.assertEqual(main.data_folder(None), found)

    def test_ask_until_good_then_remember(self):
        good = library(self.tmp / 'Lastbook-Small' / 'LastbookData')
        asked = []
        answers = iter([str(self.tmp / 'empty'), str(good.parent)])
        (self.tmp / 'empty').mkdir()
        with mock.patch.object(main, 'choose_folder', lambda m: asked.append(m) or next(answers)):
            self.assertEqual(main.ask_for_library(), good)
        self.assertEqual(asked[0], main.ASK)
        self.assertIn('is not a Lastbook library', asked[1])
        self.assertEqual(main.data_folder(None), good)   # remembered

    def test_cancel_drops_old_reason(self):
        good = library(self.tmp / 'Lastbook-Small' / 'LastbookData')
        (self.tmp / 'empty').mkdir()
        asked = []
        answers = iter([str(self.tmp / 'empty'), '', str(good)])   # wrong folder, then the picker is cancelled
        with mock.patch.object(main, 'choose_folder', lambda m: asked.append(m) or next(answers)):
            self.assertEqual(main.ask_for_library(), good)
        self.assertIn('is not a Lastbook library', asked[1])
        self.assertEqual(asked[2], main.ASK)

    def test_quit(self):
        with mock.patch.object(main, 'choose_folder', lambda m: None):
            self.assertIsNone(main.ask_for_library())
            with mock.patch.object(sys, 'argv', ['Lastbook', '--ask']):     # the old question, kept behind --ask
                self.assertEqual(main.main(), 0)
            with mock.patch.object(sys, 'argv', ['Lastbook']), mock.patch.object(main, 'run_setup', lambda: 7):
                self.assertEqual(main.main(), 7)       # no library: the window opens on setup (engine/setup/)
        self.assertFalse(self.settings().exists())

    def test_xdg_places(self):
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.tmp / 'config'), 'XDG_DATA_HOME': str(self.tmp / 'data')}):
            self.assertEqual(main.app_folders(), (self.tmp / 'config' / 'Lastbook', self.tmp / 'data' / 'Lastbook'))
            self.assertEqual(main.default_parent(), self.tmp / 'data' / 'Lastbook')
            local = library(main.default_parent() / 'LastbookData')
            self.assertEqual(main.data_folder(None), local)
            chosen = library(self.tmp / 'D' / 'LastbookData')
            main.remember(chosen)
            self.assertTrue((self.tmp / 'config' / 'Lastbook' / 'settings.json').is_file())
            self.assertEqual(main.data_folder(None), chosen)

    def test_file_picker_starts_in_app_storage(self):
        picker = mock.Mock(return_value=str(self.tmp / 'model.gguf'))
        glib = SimpleNamespace(idle_add=lambda fn: fn())     # GTK's main loop runs the dialog; here it runs at once
        with mock.patch.dict(sys.modules, {'shell_linux': SimpleNamespace(pick=picker), 'gi': SimpleNamespace(), 'gi.repository': SimpleNamespace(GLib=glib)}):
            self.assertEqual(main.pick_folder(files=True), str(self.tmp / 'model.gguf'))
        picker.assert_called_once_with(True, str(main.app_folders()[1]))

    def test_model_discovery_stays_inside_selected_library(self):
        import llm_local
        data = self.home / 'Downloads' / 'LastbookData'
        (data / 'models').mkdir(parents=True)
        outside = self.home / 'Downloads' / llm_local.FILE
        outside.write_bytes(b'not selected')
        original_stat = Path.stat

        def stat(path, *args, **kwargs):
            if path == data or data in path.parents:
                return original_stat(path, *args, **kwargs)
            self.assertFalse(any(path == self.home / name or self.home / name in path.parents
                                 for name in ('Downloads', 'Documents', 'Desktop')), str(path))
            return original_stat(path, *args, **kwargs)

        with mock.patch.object(llm_local.paths, 'DATA', data), \
                mock.patch.dict(os.environ, {'LASTBOOK_MODEL_DIR': ''}), \
                mock.patch.object(llm_local, '_load', return_value={}), \
                mock.patch.object(Path, 'stat', stat):
            # The library, then the Hugging Face cache at the one pinned revision (where other local apps keep models);
            # never the person's other folders.
            snap = llm_local.hf_snapshot()
            self.assertEqual(llm_local.places(), [data / 'models', data, snap])
            self.assertTrue(str(snap).endswith('/huggingface/hub/models--unsloth--gemma-4-26B-A4B-it-GGUF/snapshots/'
                                               'c099eb48e663fd284577b04978a94ffccb261841'), snap)
            local = llm_local.Local()
            local.runner = self.tmp / 'runner'
            local.discover()
            self.assertEqual(local.state, 'absent')

    def test_explicit_model_directory_is_kept(self):
        import llm_local
        chosen = self.home / 'Documents' / 'Models'
        with mock.patch.dict(os.environ, {'LASTBOOK_MODEL_DIR': str(chosen)}):
            self.assertEqual(llm_local.places()[0], chosen)


class SetupRelaunch(unittest.TestCase):
    def test_relaunch_discards_previous_library_overrides(self):
        """Packaged: /usr/bin/python3 with its own flags and the launcher again, without this run's arguments."""
        def restarted(executable, argv):
            self.assertEqual(argv, [sys.executable, '-I', '-B', '/usr/lib/lastbook-for-omarchy/lastbook.py'])
            self.assertNotIn('LASTBOOK_DATA', os.environ)

        with mock.patch.object(main, 'FROZEN', True), \
                mock.patch.object(sys, 'orig_argv', ['/usr/bin/python3', '-I', '-B', '/usr/lib/lastbook-for-omarchy/lastbook.py', '--data', '/old/library', '--ask']), \
                mock.patch.object(sys, 'argv', ['/usr/lib/lastbook-for-omarchy/lastbook.py', '--data', '/old/library', '--ask']), \
                mock.patch.dict(os.environ, {'LASTBOOK_DATA': '/old/library'}), \
                mock.patch.dict(sys.modules, {'shell_linux': SimpleNamespace(before_exec=lambda: None)}), \
                mock.patch.object(os, 'execv', side_effect=restarted) as restart:
            main.relaunch()
            restart.assert_called_once()


if __name__ == '__main__':
    unittest.main()
