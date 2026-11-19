"""The base of the engine's request Handler (server.py subclasses it), and the rules for what in the library may be read.

Files are served only from the library's served folders, judged by where a path really leads; encyclopedias (ZIM) are read
through libzim and searched by their own indexes; a video answers Range requests. Nothing here opens a port: app/bridge.py
runs the Handler over in-memory streams.
"""
from __future__ import annotations
import http.server, json, mimetypes, os, re, threading, urllib.parse
from pathlib import Path

from engine.paths import LIB as ROOT
try:
 from bs4 import BeautifulSoup
except ImportError:BeautifulSoup=None
MAX_PARSED_PAGE=8*1024**2  # parsing a page to rewrite its links takes about 100 times its size in memory and seconds of time; a larger one is served as it is
CLIENT_GONE=(BrokenPipeError,ConnectionResetError,ConnectionAbortedError,TimeoutError)
# The library folders a file may be served from. One may itself be a link to another disk (videos-external).
SERVED_FOLDERS=('documents','pages','videos','videos-external','transcripts','metadata/videos','courses','collections','assets')

def real_path(path):
 """Where a path really ends up: links followed."""
 return os.path.realpath(path)

def parts_below(real,root):
 """The path parts of real below root, or None when real is not inside root."""
 root=str(root)
 if real==root:return ()
 prefix=root.rstrip(os.sep)+os.sep
 return tuple(real[len(prefix):].split(os.sep)) if real.startswith(prefix) else None

def name_refused(parts):
 """The file name itself: hidden files, sign-in files, and yt-dlp's .info.json, which can hold request headers."""
 name=parts[-1].lower() if parts else ''
 return name.startswith(('.','video-auth')) or 'cookie' in name or name.endswith('.info.json')

def folder_refused(parts):
 """parts: below the library's top. Hidden folders stay out everywhere. These four names are the library's own working folders;
 inside a served folder they are just names (a course has a page called tools), and what is served is decided by being there."""
 parts=[x.lower() for x in parts]
 if any(x.startswith('.') for x in parts):return True
 if any('/'.join(parts).startswith(f+'/') for f in SERVED_FOLDERS):return False
 return any(x in ('tools','logs','work','runtime') for x in parts)

def parts_refused(parts,folders=False):
 """Run on what a path really resolves to: a link can end at "x.info.json:s", which the name check alone would pass."""
 return name_refused(parts) or (folders and folder_refused(parts))

def zim_file(name):
 """The file behind a /zim/<name>/ address, after the same checks as any other file; ValueError if refused."""
 if not name or '\0' in name or Path(name).name!=name:raise ValueError('Invalid collection')
 path=ROOT/'collections'/name
 real=real_path(path);parts=parts_below(real,real_path(ROOT/'collections'))
 if parts is None or parts_refused(parts) or not real.lower().endswith('.zim'):raise ValueError('Invalid collection')
 return path

def follow(entry):
 """The entry a redirect ends at (main_entry is usually one, whose own path 'mainPage' cannot be looked up)."""
 for _ in range(8):
  if not entry.is_redirect:break
  entry=entry.get_redirect_entry()
 return entry

def zim_main_path(arc):return follow(arc.main_entry).path

DB=ROOT/'search.sqlite'
ZIMS={};ZIM_LOCK=threading.Lock()
def open_zim(name):
 """The open archive for collections/<name>, opened once and shared by the reader and the assistant. The caller has checked the name (zim_file, or the catalog)."""
 from libzim.reader import Archive
 with ZIM_LOCK:
  if name not in ZIMS:ZIMS[name]=Archive(ROOT/'collections'/name)
  return ZIMS[name]


class Handler(http.server.SimpleHTTPRequestHandler):
 timeout=30  # a client that sends half a request gives its thread back; do_GET allows longer for sending
 SEND_TIMEOUT=600  # a paused video stops reading for minutes at a time; the player asks again with a Range when it resumes
 # The system's MIME table can call .js text/plain, which X-Content-Type-Options: nosniff would then refuse to run.
 extensions_map={**http.server.SimpleHTTPRequestHandler.extensions_map,'.js':'text/javascript','.mjs':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}
 def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(ROOT),**kwargs)
 def log_message(self,*args):pass
 SERVED_ONLY=False  # True: a file must really sit in a served folder, not just somewhere in the library
 def file_ok(self,path,folders=False):
  """May this translated path be read, judged by where it really leads? Links and junctions leaving the allowed places are refused."""
  try:
   if os.path.isfile(path) and os.stat(path).st_nlink>1:return False  # a hard link looks like an ordinary file at an ordinary path, so the real-path checks below cannot see where else it lives
   asked=parts_below(os.path.abspath(path),ROOT)
   if asked is None or parts_refused(asked,folders):return False
   real=real_path(path)
   for folder in SERVED_FOLDERS:
    parts=parts_below(real,real_path(ROOT/folder))
    if parts is not None:return not parts_refused((*folder.split('/'),*parts),folders)
   parts=parts_below(real,real_path(ROOT))
   if parts is None or parts_refused(parts,folders):return False
   return not self.SERVED_ONLY or parts in ((),('reader.css',))
  except (OSError,ValueError):return False
 def send_head(self):
  path=self.translate_path(self.path)
  if not self.file_ok(path,folders=True):
   self.send_error(404);return None
  if os.path.isdir(path):  # the base class would serve a folder's index file without asking
   for index in ('index.html','index.htm'):
    if os.path.exists(os.path.join(path,index)) and not self.file_ok(os.path.join(path,index),folders=True):
     self.send_error(404);return None
  return super().send_head()
 def send_data(self,data,mime='application/json',status=200):
  self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_GET(self):
  self.connection.settimeout(self.SEND_TIMEOUT)
  url=urllib.parse.urlsplit(self.path)
  if url.path=='/api/health':
   self.send_data(b'{"service":"survival-library","offline":true}');return
  if url.path.startswith('/zim/'):
   try:
    parts=url.path.split('/',3);name=urllib.parse.unquote(parts[2]);entrypath=urllib.parse.unquote(parts[3]) if len(parts)>3 else ''
    zim_file(name);arc=open_zim(name)
    if not entrypath:
     self.send_response(302);self.send_header('Location','/zim/'+urllib.parse.quote(name)+'/'+urllib.parse.quote(zim_main_path(arc),safe='/'));self.end_headers();return
    entry=arc.get_entry_by_path(entrypath);item=entry.get_item()
    self.send_data(bytes(item.content),item.mimetype);return
   except CLIENT_GONE:return
   except Exception:self.send_data(b'Not found','text/plain; charset=utf-8',404);return  # libzim's own messages carry the full file path
  if url.path.endswith(('.html','.htm')):
   path=Path(self.translate_path(url.path))
   if path.is_file() and path.name!='index.html' and path.stat().st_size<=MAX_PARSED_PAGE and self.file_ok(path,folders=True):
    try:
     records=json.loads((ROOT/'catalog.json').read_text(encoding='utf-8'))
     mapping={r[k]:r['path'] for r in records for k in ['source_url','final_url'] if r.get(k)}
     soup=BeautifulSoup(path.read_text(encoding='utf-8',errors='replace'),'html.parser')
     for a in soup.select('a[href]'):
      base,fragment=urllib.parse.urldefrag(a['href'])
      if base in mapping:a['href']='/'+mapping[base]+('#'+fragment if fragment else '')
     self.send_data(str(soup).encode(),'text/html; charset=utf-8');return
    except Exception:pass
  if self.headers.get('Range') and url.path.lower().endswith(('.mp4','.webm','.mkv')):
   path=Path(self.translate_path(url.path))
   if not self.file_ok(path,folders=True):
    self.send_error(404);return
   try:
    size=path.stat().st_size;m=re.match(r'bytes=(\d+)-(\d*)',self.headers['Range'])
    if not m:raise ValueError('Bad range')
    start=int(m.group(1));end=min(int(m.group(2)) if m.group(2) else size-1,size-1)
    if start>end:raise ValueError('Bad range')
    self.send_response(206);self.send_header('Content-Type',mimetypes.guess_type(path)[0] or 'video/mp4');self.send_header('Accept-Ranges','bytes');self.send_header('Content-Range',f'bytes {start}-{end}/{size}');self.send_header('Content-Length',str(end-start+1));self.end_headers()
    with path.open('rb') as f:
     f.seek(start);left=end-start+1
     while left:
      b=f.read(min(left,1024**2));self.wfile.write(b);left-=len(b)
   except CLIENT_GONE:pass
   except Exception:self.send_error(416)
   return
  super().do_GET()
