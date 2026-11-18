#!/usr/bin/env python3
"""Where the app looks for its library. Temporary folders and a made-up home only: never the real Documents.

  python3 -B app/tests/test_data_folder.py [-v]

Never packaged."""
import json, os, shutil, stat, sys, tempfile, unittest
from pathlib import Path
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
        p = mock.patch.dict(os.environ, env, clear=True)
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


if __name__ == '__main__':
    unittest.main()
