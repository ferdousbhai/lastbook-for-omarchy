// SPDX-License-Identifier: MIT
//! The Python engine (app/engine_pipe.py) as a child process: JSON lines on its standard input and output, no port.
//!
//! One request a line ({"id", "method", "path", "body"}); answers come back as a head, pieces ("chunk" text or "b64"
//! bytes) and an end, all carrying the id (docs/ENGINE.md). The engine leaves when its input closes, which happens
//! however this program ends.

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
enum Piece {
    Head(u16, String),
    Bytes(Vec<u8>),
    /// False: the answer stopped short, or never came (the engine ended).
    End(bool),
}

#[derive(Debug, Clone, Deserialize)]
pub struct Ready {
    /// True when there is no library yet and the engine runs first-run setup.
    #[serde(default)]
    pub setup: bool,
    pub data: Option<PathBuf>,
    #[serde(default)]
    pub library: PathBuf,
    pub version: Option<String>,
    /// A sentence for the person, when a library switch failed or was undone at this start.
    pub notice: Option<String>,
}

type Waiters = Arc<Mutex<HashMap<u64, async_channel::Sender<Piece>>>>;

pub struct Engine {
    stdin: Mutex<ChildStdin>,
    waiters: Waiters,
    next: AtomicU64,
    pub ready: Ready,
    /// Messages the engine sends on its own (not answers): {"restart": folder} when setup has finished, and
    /// {"job": state} whenever a setup job's state changes (setup.rs, `follow`).
    pub notices: async_channel::Receiver<Value>,
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
pub fn engine_script() -> PathBuf {
    if let Some(p) = std::env::var_os("LASTBOOK_ENGINE") {
        return PathBuf::from(p);
    }
    let installed = PathBuf::from("/usr/lib/lastbook-for-omarchy/app/engine_pipe.py");
    if std::env::current_exe().is_ok_and(|e| e.starts_with("/usr/")) && installed.is_file() {
        return installed;
    }
    // A checkout's own engine, for development builds only: a release build must not carry the build folder's path.
    #[cfg(debug_assertions)]
    {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../app/engine_pipe.py")
    }
    #[cfg(not(debug_assertions))]
    {
        std::env::current_exe()
            .ok()
            .and_then(|e| e.ancestors().nth(4).map(|root| root.join("app/engine_pipe.py")))   // <checkout>/native/target/release/lastbook
            .filter(|p| p.is_file())
            .unwrap_or(installed)
    }
}

/// Python running one of the engine's programs (the file beside engine_pipe.py named `program`).
pub fn python(program: &str) -> Command {
    let mut cmd = Command::new("/usr/bin/python3");
    // -I keeps PYTHON* settings and the user's site-packages out of the packaged engine; a checkout run passes its
    // own PYTHONPATH (LASTBOOK_DEV=1) for dependencies the system lacks.
    if std::env::var_os("LASTBOOK_DEV").is_none() {
        cmd.arg("-I");
    }
    cmd.arg("-B").arg(engine_script().with_file_name(program));
    cmd
}

/// Reads a JSON file of the person's own (saved items); nothing there or unreadable is the default.
pub fn load_json<T: serde::de::DeserializeOwned + Default>(path: &std::path::Path) -> T {
    std::fs::read(path).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
}

/// Writes such a file whole or not at all (`write_atomic`), making its folder first. What stopped it is the caller's
/// to show the person: something they saved that is not on the disk must not look saved.
pub fn store_json<T: serde::Serialize + ?Sized>(path: &std::path::Path, value: &T) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    write_atomic(path, &serde_json::to_vec(value)?)
}

/// Writes `bytes` to `path` whole or not at all: to a file beside it, sent to the disk, then renamed over it.
pub fn write_atomic(path: &std::path::Path, bytes: &[u8]) -> std::io::Result<()> {
    let tmp = path.with_extension("tmp");
    let written = (|| {
        let mut file = std::fs::File::create(&tmp)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        std::fs::rename(&tmp, path)
    })();
    if written.is_err() {
        let _ = std::fs::remove_file(&tmp);   // nothing half-written is left beside the file
    }
    written
}

impl Engine {
    /// Starts the engine and waits for its ready line (it loads the catalog and opens the indexes first, which takes
    /// from a moment to many seconds). Never called on the main thread: the window is up before the engine is
    /// (main.rs).
    pub fn start(data: Option<&str>) -> Result<Arc<Self>, EngineError> {
        let mut cmd = python("engine_pipe.py");
        if let Some(d) = data {
            cmd.arg("--data").arg(d);
        }
        Self::connect(cmd, &engine_script().display().to_string())
    }

    /// Runs `cmd` as the engine, named `what` in an error, and waits for its ready line.
    fn connect(mut cmd: Command, what: &str) -> Result<Arc<Self>, EngineError> {
        let mut child = cmd
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(|e| EngineError(format!("could not start the engine ({what}): {e}")))?;
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
        let (notice_tx, notices) = async_channel::unbounded();
        std::thread::Builder::new()
            .name("engine-read".into())
            .spawn(move || {
                for line in out.lines() {
                    let Ok(line) = line else { break };
                    let Ok(msg) = serde_json::from_str::<Value>(&line) else { continue };
                    let Some(id) = msg.get("id").and_then(Value::as_u64) else {
                        let _ = notice_tx.send_blocking(msg);
                        continue;
                    };
                    let piece = if let Some(s) = msg.get("head").and_then(Value::as_u64) {
                        Piece::Head(s as u16, msg.get("type").and_then(Value::as_str).unwrap_or_default().to_owned())
                    } else if let Some(c) = msg.get("chunk").and_then(Value::as_str) {
                        Piece::Bytes(c.as_bytes().to_vec())
                    } else if let Some(b) = msg.get("b64").and_then(Value::as_str) {
                        use base64::Engine as _;
                        Piece::Bytes(base64::engine::general_purpose::STANDARD.decode(b).unwrap_or_default())
                    } else if let Some(ok) = msg.get("end") {
                        Piece::End(ok.as_bool() == Some(true))
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
        Ok(Arc::new(Self { stdin: Mutex::new(stdin), waiters, next: AtomicU64::new(1), ready, notices, _child: Mutex::new(child) }))
    }

    /// Sends a request; its answer arrives on the returned channel, piece by piece.
    fn request(&self, method: &str, path: &str, body: Option<&Value>) -> async_channel::Receiver<Piece> {
        let id = self.next.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = async_channel::unbounded();
        self.waiters.lock().unwrap().insert(id, tx);
        let mut req = json!({"id": id, "method": method, "path": path});
        if let Some(b) = body {
            req["body"] = b.clone();
        }
        let line = req.to_string() + "\n";
        if self.stdin.lock().unwrap().write_all(line.as_bytes()).is_err()
            && let Some(tx) = self.waiters.lock().unwrap().remove(&id)
        {
            let _ = tx.send_blocking(Piece::End(false));
        }
        rx
    }

    /// The whole answer to a GET, read as JSON.
    pub async fn get(&self, path: &str) -> Result<Value, EngineError> {
        self.call("GET", path, None).await
    }

    /// The whole answer to a POST of `body`, read as JSON (an error answer's JSON too: setup explains itself in it).
    pub async fn post(&self, path: &str, body: &Value) -> Result<Value, EngineError> {
        self.call("POST", path, Some(body)).await
    }

    async fn call(&self, method: &str, path: &str, body: Option<&Value>) -> Result<Value, EngineError> {
        let (status, _, text) = self.collect(method, path, body).await;
        match serde_json::from_slice::<Value>(&text) {
            Ok(v) if status == 200 || (method == "POST" && v.get("error").is_some()) => Ok(v),
            Ok(v) if v.get("error").is_some() => Err(EngineError(v["error"].as_str().unwrap_or("error").to_owned())),
            _ if status != 200 => Err(EngineError(format!("{path}: {status}"))),
            Ok(v) => Ok(v),
            Err(e) => Err(EngineError(format!("{path}: {e}"))),
        }
    }

    /// The whole answer to a GET as bytes, with its status and content type (pages and their images, for the web view).
    pub async fn fetch(&self, path: &str) -> (u16, String, Vec<u8>) {
        self.collect("GET", path, None).await
    }

    /// The whole answer to a request: its status, content type and body. An answer that stopped short has status 0,
    /// as one that never came does: a cut page must not be shown as if it were whole.
    async fn collect(&self, method: &str, path: &str, body: Option<&Value>) -> (u16, String, Vec<u8>) {
        let rx = self.request(method, path, body);
        let (mut status, mut ctype, mut bytes) = (0, String::new(), Vec::new());
        while let Ok(piece) = rx.recv().await {
            match piece {
                Piece::Head(s, t) => (status, ctype) = (s, t),
                Piece::Bytes(b) => bytes.extend_from_slice(&b),
                Piece::End(ok) => {
                    if !ok {
                        status = 0;
                    }
                    break;
                }
            }
        }
        (status, ctype, bytes)
    }
}

/// `path?key=value&...`, with every value encoded.
pub fn query(path: &str, params: &[(&str, &str)]) -> String {
    let q: Vec<String> = params.iter().map(|(k, v)| format!("{k}={}", urlencoding::encode(v))).collect();
    if q.is_empty() { path.to_owned() } else { format!("{path}?{}", q.join("&")) }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A stand-in engine: a shell that says `lines` and then reads until its input closes, as the engine does.
    fn stand_in(lines: &str) -> Command {
        let mut cmd = Command::new("sh");
        cmd.arg("-c").arg(format!("{lines}\ncat >/dev/null"));
        cmd
    }

    #[test]
    fn the_engine_is_connected_once_it_says_it_is_ready() {
        let engine = Engine::connect(stand_in(r#"sleep 0.1; echo '{"ready":{"setup":true,"version":"9"}}'"#), "test").unwrap();
        assert!(engine.ready.setup);
        assert_eq!(engine.ready.version.as_deref(), Some("9"));
    }

    #[test]
    fn what_the_engine_says_unasked_arrives_as_a_notice() {
        let engine = Engine::connect(stand_in(r#"echo '{"ready":{"setup":true}}'; echo '{"job":"done"}'"#), "test").unwrap();
        assert_eq!(engine.notices.recv_blocking().unwrap(), json!({"job": "done"}));
    }

    #[test]
    fn an_answer_is_put_together_and_one_cut_short_is_no_answer() {
        let engine = Engine::connect(
            stand_in(concat!(
                r#"echo '{"ready":{"setup":true}}'; "#,
                r#"read a; echo '{"id":1,"head":200,"type":"text/html"}'; echo '{"id":1,"chunk":"<p>é"}'; echo '{"id":1,"b64":"IQ=="}'; echo '{"id":1,"end":true}'; "#,
                r#"read b; echo '{"id":2,"head":200,"type":"text/html"}'; echo '{"id":2,"b64":"IQ=="}'; echo '{"id":2,"end":false}'"#
            )),
            "test",
        )
        .unwrap();
        let ctx = gtk::glib::MainContext::new();
        assert_eq!(ctx.block_on(engine.fetch("/pages/a.html")), (200, "text/html".to_owned(), "<p>é!".as_bytes().to_vec()));
        assert_eq!(ctx.block_on(engine.fetch("/pages/b.html")).0, 0, "a page that stopped short is not shown as whole");
    }

    #[test]
    fn an_engine_that_cannot_open_the_library_says_why() {
        let Err(e) = Engine::connect(stand_in(r#"echo '{"error":"The library is on a disk that is not here."}'"#), "test") else {
            panic!("connected to an engine that had no library");
        };
        assert_eq!(e.to_string(), "The library is on a disk that is not here.");
    }

    #[test]
    fn an_engine_that_ends_before_it_is_ready_is_an_error() {
        assert!(Engine::connect(Command::new("true"), "test").is_err());
        let Err(e) = Engine::connect(Command::new("/nonexistent/lastbook-engine"), "the stand-in") else {
            panic!("started a program that is not there");
        };
        assert!(e.to_string().contains("the stand-in"), "{e}");
    }

    #[test]
    fn a_saved_file_is_written_whole_and_a_failure_is_told() {
        let dir = std::env::temp_dir().join(format!("lastbook-store-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let file = dir.join("deeper").join("saved.json");
        store_json(&file, &vec!["a", "b"]).unwrap();
        assert_eq!(load_json::<Vec<String>>(&file), ["a", "b"]);
        store_json(&file, &vec!["c"]).unwrap();
        assert_eq!(load_json::<Vec<String>>(&file), ["c"]);
        assert!(!file.with_extension("tmp").exists(), "the file written beside it is gone once renamed");
        // A folder that cannot be made (a file is in its way): the error comes back, and nothing is written.
        let blocked = file.join("under-a-file").join("saved.json");
        assert!(store_json(&blocked, &vec!["d"]).is_err());
        assert_eq!(load_json::<Vec<String>>(&file), ["c"]);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
