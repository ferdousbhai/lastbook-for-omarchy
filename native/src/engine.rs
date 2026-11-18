// SPDX-License-Identifier: MIT
//! The Python engine (app/engine_pipe.py) as a child process: JSON lines on its standard input and output, no port.
//!
//! One request a line ({"id", "method", "path", "body"}); answers come back as a head, text chunks and an end, all
//! carrying the id, so a streamed answer (Chat) arrives as it is written. The engine leaves when its input closes,
//! which happens however this program ends.

use std::{
    collections::HashMap,
    io::{BufRead, BufReader, Write},
    path::PathBuf,
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
};

use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Debug, Clone)]
pub enum Piece {
    Head(u16, String),
    Chunk(String),
    Bytes(Vec<u8>),
    End(bool),
}

#[derive(Debug, Clone, Deserialize)]
pub struct Ready {
    pub data: PathBuf,
    pub library: PathBuf,
    pub version: Option<String>,
}

type Waiters = Arc<Mutex<HashMap<u64, async_channel::Sender<Piece>>>>;

pub struct Engine {
    stdin: Mutex<ChildStdin>,
    waiters: Waiters,
    next: AtomicU64,
    pub ready: Ready,
    _child: Mutex<Child>,
}

#[derive(Debug)]
pub struct EngineError(pub String);

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// Where the engine's program files are: LASTBOOK_ENGINE, else beside the installed binary, else this checkout's app/.
fn engine_script() -> PathBuf {
    if let Some(p) = std::env::var_os("LASTBOOK_ENGINE") {
        return PathBuf::from(p);
    }
    let installed = PathBuf::from("/usr/lib/lastbook-for-omarchy/app/engine_pipe.py");
    if std::env::current_exe().is_ok_and(|e| e.starts_with("/usr/")) && installed.is_file() {
        return installed;
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../app/engine_pipe.py")
}

impl Engine {
    /// Starts the engine and waits for its ready line (it loads the catalog and opens the indexes first).
    pub fn start(data: Option<&str>) -> Result<Arc<Self>, EngineError> {
        let script = engine_script();
        let mut cmd = Command::new("/usr/bin/python3");
        // -I keeps PYTHON* settings and the user's site-packages out of the packaged engine; a checkout run passes its
        // own PYTHONPATH (LASTBOOK_DEV=1) for dependencies the system lacks.
        if std::env::var_os("LASTBOOK_DEV").is_none() {
            cmd.arg("-I");
        }
        cmd.arg("-B").arg(&script);
        if let Some(d) = data {
            cmd.arg("--data").arg(d);
        }
        let mut child = cmd
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|e| EngineError(format!("could not start the engine ({}): {e}", script.display())))?;
        let stdin = child.stdin.take().expect("piped");
        let mut out = BufReader::new(child.stdout.take().expect("piped"));
        let mut first = String::new();
        out.read_line(&mut first).map_err(|e| EngineError(format!("the engine did not start: {e}")))?;
        let head: Value = serde_json::from_str(&first).map_err(|_| EngineError("the engine did not start".into()))?;
        let ready: Ready = match head.get("ready") {
            Some(r) if !r.is_null() => serde_json::from_value(r.clone()).map_err(|e| EngineError(e.to_string()))?,
            _ => return Err(EngineError(head.get("error").and_then(Value::as_str).unwrap_or("no library").to_owned())),
        };
        let waiters: Waiters = Arc::default();
        let w = waiters.clone();
        std::thread::Builder::new()
            .name("engine-read".into())
            .spawn(move || {
                for line in out.lines() {
                    let Ok(line) = line else { break };
                    let Ok(msg) = serde_json::from_str::<Value>(&line) else { continue };
                    let Some(id) = msg.get("id").and_then(Value::as_u64) else { continue };
                    let piece = if let Some(s) = msg.get("head").and_then(Value::as_u64) {
                        Piece::Head(s as u16, msg.get("type").and_then(Value::as_str).unwrap_or_default().to_owned())
                    } else if let Some(c) = msg.get("chunk").and_then(Value::as_str) {
                        Piece::Chunk(c.to_owned())
                    } else if let Some(b) = msg.get("b64").and_then(Value::as_str) {
                        use base64::Engine as _;
                        Piece::Bytes(base64::engine::general_purpose::STANDARD.decode(b).unwrap_or_default())
                    } else if let Some(ok) = msg.get("end").and_then(Value::as_bool) {
                        Piece::End(ok)
                    } else {
                        continue;
                    };
                    let end = matches!(piece, Piece::End(_));
                    let tx = if end { w.lock().unwrap().remove(&id) } else { w.lock().unwrap().get(&id).cloned() };
                    if let Some(tx) = tx {
                        let _ = tx.send_blocking(piece);
                    }
                }
                // The engine ended: every request still waiting gets an end, so nothing hangs.
                for (_, tx) in w.lock().unwrap().drain() {
                    let _ = tx.send_blocking(Piece::End(false));
                }
            })
            .expect("thread");
        Ok(Arc::new(Self { stdin: Mutex::new(stdin), waiters, next: AtomicU64::new(1), ready, _child: Mutex::new(child) }))
    }

    /// Sends a request; its answer arrives on the returned channel, piece by piece.
    pub fn request(&self, method: &str, path: &str, body: Option<&Value>) -> (u64, async_channel::Receiver<Piece>) {
        let id = self.next.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = async_channel::unbounded();
        self.waiters.lock().unwrap().insert(id, tx);
        let mut req = json!({"id": id, "method": method, "path": path});
        if let Some(b) = body {
            req["body"] = b.clone();
        }
        let line = req.to_string() + "\n";
        if self.stdin.lock().unwrap().write_all(line.as_bytes()).is_err() {
            if let Some(tx) = self.waiters.lock().unwrap().remove(&id) {
                let _ = tx.send_blocking(Piece::End(false));
            }
        }
        (id, rx)
    }

    pub fn cancel(&self, id: u64) {
        let _ = self.stdin.lock().unwrap().write_all(format!("{{\"cancel\":{id}}}\n").as_bytes());
    }

    /// The whole answer to a GET, read as JSON.
    pub async fn get(&self, path: &str) -> Result<Value, EngineError> {
        let (_, rx) = self.request("GET", path, None);
        let (mut status, mut text) = (0, String::new());
        while let Ok(piece) = rx.recv().await {
            match piece {
                Piece::Head(s, _) => status = s,
                Piece::Chunk(c) => text.push_str(&c),
                Piece::Bytes(_) => {}
                Piece::End(_) => break,
            }
        }
        if status != 200 {
            return Err(EngineError(format!("{path}: {status}")));
        }
        serde_json::from_str(&text).map_err(|e| EngineError(format!("{path}: {e}")))
    }
}

impl Engine {
    /// The whole answer to a GET as bytes, with its status and content type (pages and their images, for the web view).
    pub async fn fetch(&self, path: &str) -> (u16, String, Vec<u8>) {
        let (_, rx) = self.request("GET", path, None);
        let (mut status, mut ctype, mut body) = (0, String::new(), Vec::new());
        while let Ok(piece) = rx.recv().await {
            match piece {
                Piece::Head(s, t) => (status, ctype) = (s, t),
                Piece::Chunk(c) => body.extend_from_slice(c.as_bytes()),
                Piece::Bytes(b) => body.extend_from_slice(&b),
                Piece::End(_) => break,
            }
        }
        (status, ctype, body)
    }
}

/// `path?key=value&...`, with every value encoded.
pub fn query(path: &str, params: &[(&str, &str)]) -> String {
    let q: Vec<String> = params.iter().map(|(k, v)| format!("{k}={}", urlencoding::encode(v))).collect();
    if q.is_empty() { path.to_owned() } else { format!("{path}?{}", q.join("&")) }
}
