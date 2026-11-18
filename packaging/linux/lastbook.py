"""Starts the packaged Linux app: /usr/bin/lastbook runs  /usr/bin/python3 -I -B /usr/lib/lastbook-for-omarchy/lastbook.py.

The folder is laid out as a PyInstaller build is (web/, content/, setup/, engine/setup/roots.pem, llm/ under one root), and this marks the
run as packaged the same way (sys.frozen, sys._MEIPASS), so every packaged-app rule in the engine holds here too: program files from this
folder only, never code from a data folder. -I keeps PYTHON* settings and the person's own site-packages out; lib/ (the wheels this
build installed) comes before the system's dist-packages, which give only gi (PyGObject) and what it needs.
"""
import os
import runpy
import sys

ROOT = os.path.dirname(os.path.realpath(__file__))
sys.frozen = True
sys._MEIPASS = ROOT
sys.path[:0] = [os.path.join(ROOT, 'app'), ROOT, os.path.join(ROOT, 'lib')]
runpy.run_path(os.path.join(ROOT, 'app', 'main.py'), run_name='__main__')
