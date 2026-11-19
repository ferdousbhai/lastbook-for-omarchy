// SPDX-License-Identifier: MIT
//! The command line: which arguments open the window and which are a command, and the agent skill.
//!
//! Commands are the engine's (app/cli.py), run in its place with no window and no GTK: JSON on standard output, one
//! line on standard error for an error, exit 0 for success (no matches too), 1 for a runtime error, 2 for bad arguments.

use std::{
    fs, io,
    os::unix::{fs::symlink, process::CommandExt},
    path::{Path, PathBuf},
};

use serde_json::json;

pub const HELP: &str = "lastbook — offline survival library: manuals, health guides, videos, encyclopedias and a map (JSON output)

Commands:
  search QUERY [--kind any|manual|video|encyclopedia] [--limit 1..20]
  read DOC_ID [--pages 3,4,5] [--section TEXT]   text of pages, of an encyclopedia article (zim:…) or of a saved page
  page DOC_ID PAGE [--out FILE.png] [--width 160..1600]   one PDF page as a picture
  transcript VIDEO_ID [--query TEXT]  timed lines of a video: all, or those nearest the query
  place QUERY [--nearby hospital,pharmacy,…]   places on the offline map, and help points near the first
  guides | guide ID                   the situation guides; one guide's branches and the pages they point to
  ask QUESTION                        an answer from the local AI with its sources (slow: loads the 17 GB model)
  status                              library folder and counts, local AI, meaning search, map files
  open TARGET [PAGE] [--dry-run]      show a document (DOC_ID) or a route (guide:hurt, search:burns, map:LAT,LON,LABEL) in a new window
  skill [install]

With no command, lastbook opens its window. --data FOLDER names the library (else LASTBOOK_DATA, else the window's own).
Exit codes: 0 success (including no matches), 1 runtime error, 2 invalid arguments.
Only `open` starts a window; only `ask` loads the AI model. Errors are one line on standard error.";

/// The commands app/cli.py answers.
const ENGINE: &[&str] = &["search", "read", "page", "transcript", "place", "guides", "guide", "ask", "status", "open"];
const SKILL: &str = include_str!("../../skills/lastbook/SKILL.md");
/// Beside the skill, saying whose it is, as other tools' skills do: what syncs a person's own skills between their
/// computers leaves a marked folder to the Lastbook installed on each.
const SKILL_MARKER: &str = ".managed-by-lastbook";

#[derive(Debug, PartialEq)]
pub enum Start {
    /// No command: the window, on the library --data names (if any).
    Window(Option<String>),
    Help,
    Version,
    /// `skill` and what follows it.
    Skill(Vec<String>),
    /// A command for the engine: every argument as given (--data may come before the command).
    Engine(Vec<String>),
    Unknown(String),
}

/// What the arguments (without the program's name) ask for. The first one that is not --data FOLDER decides.
pub fn start(args: &[String]) -> Start {
    let mut data = None;
    let mut i = 0;
    while let Some(a) = args.get(i) {
        match a.as_str() {
            "--data" => {
                data = args.get(i + 1).cloned();
                i += 2;
            }
            "-h" | "--help" | "help" => return Start::Help,
            "--version" => return Start::Version,
            "skill" => return Start::Skill(args[i + 1..].to_vec()),
            c if ENGINE.contains(&c) => return Start::Engine(args.to_vec()),
            other => return Start::Unknown(other.to_owned()),
        }
    }
    Start::Window(data)
}

/// Becomes the engine's command line (app/cli.py); returns only if it could not start.
pub fn run_engine(args: &[String]) -> io::Error {
    let mut cmd = crate::engine::python("cli.py");
    cmd.args(args);
    if let Ok(exe) = std::env::current_exe() {
        cmd.env("LASTBOOK_BIN", exe); // `open` starts this program again, as a window
    }
    cmd.exec()
}

fn home() -> io::Result<PathBuf> {
    std::env::var_os("HOME").filter(|h| !h.is_empty()).map(PathBuf::from).ok_or_else(|| io::Error::other("HOME is not set"))
}

fn atomic_write(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let tmp = path.with_extension(format!("tmp{}", std::process::id()));
    fs::write(&tmp, bytes)?;
    fs::rename(&tmp, path)
}

/// Installs the skill for this user: ~/.agents/skills/lastbook, linked from Codex's and Claude's skill folders
/// (where nothing of that name is there already).
fn install_skill() -> io::Result<serde_json::Value> {
    let home = home()?;
    let shared = home.join(".agents/skills/lastbook");
    // A skill of that name that Lastbook did not put there is the person's own (or another tool's): it stays.
    if shared.join("SKILL.md").exists() && !shared.join(SKILL_MARKER).is_file() {
        return Err(io::Error::other(format!("{} is there already and is not Lastbook's; move it away first", shared.display())));
    }
    fs::create_dir_all(&shared)?;
    atomic_write(&shared.join("SKILL.md"), SKILL.as_bytes())?;
    atomic_write(&shared.join(SKILL_MARKER), b"")?;
    let codex = std::env::var_os("CODEX_HOME").map(PathBuf::from).unwrap_or_else(|| home.join(".codex")).join("skills/lastbook");
    let claude = home.join(".claude/skills/lastbook");
    let mut links = Vec::new();
    for entry in [codex, claude] {
        if entry.symlink_metadata().is_err() {
            fs::create_dir_all(entry.parent().expect("has a parent"))?;
            symlink(&shared, &entry)?;
            links.push(entry);
        }
    }
    Ok(json!({"installed": shared.join("SKILL.md"), "links": links}))
}

/// `lastbook skill` prints the skill; `lastbook skill install` installs it. The exit code.
pub fn skill(args: &[String]) -> i32 {
    match args {
        [] => {
            print!("{SKILL}");
            0
        }
        [a] if a == "install" => match install_skill() {
            Ok(v) => {
                println!("{v}");
                0
            }
            Err(e) => {
                eprintln!("lastbook: could not install the skill: {e}");
                1
            }
        },
        _ => {
            eprintln!("lastbook: usage: lastbook skill [install]");
            2
        }
    }
}

/// When the window starts: an installed skill is brought up to this version (a package update cannot write into home
/// folders). Nothing is created that `skill install` did not create first.
pub fn refresh_skill() {
    let Ok(home) = home() else { return };
    let skill = home.join(".agents/skills/lastbook/SKILL.md");
    if skill.with_file_name(SKILL_MARKER).is_file() && fs::read(&skill).is_ok_and(|now| now != SKILL.as_bytes()) {
        let _ = atomic_write(&skill, SKILL.as_bytes());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn no_command_opens_the_window() {
        assert_eq!(start(&[]), Start::Window(None));
        assert_eq!(start(&args(&["--data", "/lib"])), Start::Window(Some("/lib".into())));
        assert_eq!(start(&args(&["--data"])), Start::Window(None));
    }

    #[test]
    fn a_command_goes_to_the_engine_with_every_argument() {
        for c in ENGINE {
            assert_eq!(start(&args(&[c, "x"])), Start::Engine(args(&[c, "x"])), "{c}");
            assert!(HELP.contains(&format!("{c} ")), "{c} is in the help");
        }
        let a = args(&["--data", "/lib", "search", "burns", "--limit", "3"]);
        assert_eq!(start(&a), Start::Engine(a.clone()));
        // A folder that has a command's name is still the folder.
        assert_eq!(start(&args(&["--data", "search"])), Start::Window(Some("search".into())));
    }

    #[test]
    fn help_version_skill_and_the_rest() {
        for h in ["-h", "--help", "help"] {
            assert_eq!(start(&args(&[h])), Start::Help);
        }
        assert_eq!(start(&args(&["--version"])), Start::Version);
        assert_eq!(start(&args(&["skill", "install"])), Start::Skill(args(&["install"])));
        assert_eq!(start(&args(&["serach", "x"])), Start::Unknown("serach".into()));
        assert_eq!(start(&args(&["--data", "/lib", "--verbose"])), Start::Unknown("--verbose".into()));
    }

    #[test]
    fn the_skill_names_itself_and_only_real_commands() {
        assert!(SKILL.starts_with("---\nname: lastbook\ndescription: "));
        for line in SKILL.lines().filter_map(|l| l.strip_prefix("lastbook ")) {
            let c = line.split_whitespace().next().unwrap_or_default();
            assert!(ENGINE.contains(&c) || c == "skill", "{c}");
        }
    }
}
