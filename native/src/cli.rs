// SPDX-License-Identifier: MIT
//! The command line: which arguments open the window and which are a command, and the agent skill.
//!
//! Commands are the engine's (app/cli.py), run in its place with no window and no GTK: JSON on standard output, one
//! line on standard error for an error, exit 0 for success (no matches too) and 1 for any failure, bad arguments too, as
//! Omarchy's commands have it: the line on standard error says what went wrong.

use std::{
    fs, io,
    os::unix::{fs::symlink, process::CommandExt},
    path::{Path, PathBuf},
};

use serde_json::json;

pub const HELP: &str = "lastbook — offline survival library: manuals, health guides, videos, encyclopedias and a map (JSON output)

Commands:
  search QUERY [--kind any|manual|video|encyclopedia] [--limit 1..20] [--read]   ranked passages; --read adds the whole page of the best four
  read DOC_ID [--pages 3,4,5] [--section TEXT]   text of pages, of an encyclopedia article (zim:…) or of a saved page
  page DOC_ID PAGE [--out FILE.png] [--width 160..1600]   one PDF page as a picture
  transcript VIDEO_ID [--query TEXT]  timed lines of a video: all, or those nearest the query
  place QUERY|LAT,LON [--nearby hospital,pharmacy,…]   places on the offline map, or a point, and help points near the first
  guides | guide ID                   the situation guides; one guide's branches and the pages they point to
  status                              library folder and counts, meaning search, map files
  index [--wait SECONDS] [--pause SECONDS]   build the meaning-search index until nothing is pending (the lastbook-index user service runs this)
  open                                the window (as lastbook alone does)
  open TARGET [PAGE] [--dry-run]      show a document (DOC_ID) or a route (guide:hurt, search:burns, map:LAT,LON,LABEL) in a new window
  skill [install|uninstall]           print the agent skill; install or remove it (it installs itself on first use)

With no command, or `open` alone, lastbook opens its window. --data FOLDER names the library (else LASTBOOK_DATA, else the window's own).
Exit codes: 0 success (including no matches), 1 any failure (invalid arguments too).
Only `open` starts a window. Errors are one line on standard error.";

/// The commands app/cli.py answers.
const ENGINE: &[&str] = &["search", "read", "page", "transcript", "place", "guides", "guide", "status", "open", "index"];
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
            // `open` with nothing to show is the window itself (the desktop entry's `Exec=lastbook open`); with a
            // target it is the engine's, which finds the target and starts a window on it.
            "open" if window_only(&args[i + 1..], &mut data) => return Start::Window(data),
            c if ENGINE.contains(&c) => return Start::Engine(args.to_vec()),
            other => return Start::Unknown(other.to_owned()),
        }
    }
    Start::Window(data)
}

/// Whether what follows `open` names no target: nothing, or --data FOLDER (which then names the library).
fn window_only(rest: &[String], data: &mut Option<String>) -> bool {
    match rest {
        [] => true,
        [flag, folder] if flag == "--data" => {
            *data = Some(folder.clone());
            true
        }
        _ => false,
    }
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

/// Where the person's choice not to have the skill is kept (`skill uninstall`), so nothing puts it back.
fn declined(home: &Path) -> PathBuf {
    // Tests give a home of their own and must never reach the real settings folder through the environment.
    let named = if cfg!(test) { None } else { std::env::var_os("XDG_CONFIG_HOME").filter(|c| !c.is_empty()) };
    named.map(PathBuf::from).unwrap_or_else(|| home.join(".config")).join("Lastbook/skill-declined")
}

/// The other agents' skill folders that link to the shared one.
fn skill_links(home: &Path) -> [PathBuf; 2] {
    let named = if cfg!(test) { None } else { std::env::var_os("CODEX_HOME").filter(|c| !c.is_empty()) };
    let codex = named.map(PathBuf::from).unwrap_or_else(|| home.join(".codex"));
    [codex.join("skills/lastbook"), home.join(".claude/skills/lastbook")]
}

/// Installs the skill for this user: ~/.agents/skills/lastbook, linked from Codex's and Claude's skill folders
/// (where nothing of that name is there already).
fn install_skill(home: &Path) -> io::Result<serde_json::Value> {
    let shared = home.join(".agents/skills/lastbook");
    // A skill of that name that Lastbook did not put there is the person's own (or another tool's): it stays.
    if shared.join("SKILL.md").exists() && !shared.join(SKILL_MARKER).is_file() {
        return Err(io::Error::other(format!("{} is there already and is not Lastbook's; move it away first", shared.display())));
    }
    fs::create_dir_all(&shared)?;
    crate::engine::write_atomic(&shared.join("SKILL.md"), SKILL.as_bytes())?;
    crate::engine::write_atomic(&shared.join(SKILL_MARKER), b"")?;
    let mut links = Vec::new();
    for entry in skill_links(home) {
        if entry.symlink_metadata().is_err() {
            fs::create_dir_all(entry.parent().expect("has a parent"))?;
            symlink(&shared, &entry)?;
            links.push(entry);
        }
    }
    let _ = fs::remove_file(declined(home)); // asked for again
    Ok(json!({"installed": shared.join("SKILL.md"), "links": links}))
}

/// Removes the skill Lastbook installed, and the links to it, and remembers the choice: nothing installs it again
/// until `skill install`. A skill of that name that is not Lastbook's is left alone.
fn uninstall_skill(home: &Path) -> io::Result<serde_json::Value> {
    let shared = home.join(".agents/skills/lastbook");
    let mut removed = Vec::new();
    if shared.join(SKILL_MARKER).is_file() {
        for entry in skill_links(home) {
            if fs::read_link(&entry).is_ok_and(|to| to == shared) {
                fs::remove_file(&entry)?;
                removed.push(entry);
            }
        }
        fs::remove_file(shared.join("SKILL.md")).or_else(|e| if e.kind() == io::ErrorKind::NotFound { Ok(()) } else { Err(e) })?;
        fs::remove_file(shared.join(SKILL_MARKER))?;
        let _ = fs::remove_dir(&shared); // only if nothing else is in it
        removed.push(shared);
    }
    let mark = declined(home);
    fs::create_dir_all(mark.parent().expect("has a parent"))?;
    crate::engine::write_atomic(&mark, b"")?;
    Ok(json!({"removed": removed}))
}

/// `lastbook skill` prints the skill; `lastbook skill install` installs it; `lastbook skill uninstall` removes it and
/// keeps it removed. The exit code.
pub fn skill(args: &[String]) -> i32 {
    let run = |what: &str, f: fn(&Path) -> io::Result<serde_json::Value>| match home().and_then(|h| f(&h)) {
        Ok(v) => {
            println!("{v}");
            0
        }
        Err(e) => {
            eprintln!("lastbook: could not {what} the skill: {e}");
            1
        }
    };
    match args {
        [] => {
            print!("{SKILL}");
            0
        }
        [a] if a == "install" => run("install", install_skill),
        [a] if a == "uninstall" => run("remove", uninstall_skill),
        _ => {
            eprintln!("lastbook: usage: lastbook skill [install|uninstall]");
            1
        }
    }
}

/// Whenever Lastbook runs, as the window or as a command: the agent skill is there and is this version's. A package
/// cannot write into home folders, so this is how a plain `pacman -S` install gets the skill and how an update reaches
/// it. Says nothing (a command's standard output is its JSON answer) and never fails the run. Left alone: a skill of
/// that name that is not Lastbook's, and a person's `skill uninstall`.
pub fn ensure_skill() {
    if let Ok(home) = home() {
        ensure_skill_in(&home);
    }
}

fn ensure_skill_in(home: &Path) {
    if declined(home).exists() {
        return;
    }
    let shared = home.join(".agents/skills/lastbook");
    let (skill, ours) = (shared.join("SKILL.md"), shared.join(SKILL_MARKER).is_file());
    if !skill.exists() {
        let _ = install_skill(home);
    } else if ours && fs::read(&skill).is_ok_and(|now| now != SKILL.as_bytes()) {
        let _ = crate::engine::write_atomic(&skill, SKILL.as_bytes());
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
        // The desktop entry's command, and the same with a library named before or after it.
        assert_eq!(start(&args(&["open"])), Start::Window(None));
        assert_eq!(start(&args(&["open", "--data", "/lib"])), Start::Window(Some("/lib".into())));
        assert_eq!(start(&args(&["--data", "/lib", "open"])), Start::Window(Some("/lib".into())));
        // With a target, `open` is the engine's: it finds the document and starts a window on it.
        assert_eq!(start(&args(&["open", "guide:hurt"])), Start::Engine(args(&["open", "guide:hurt"])));
        assert_eq!(start(&args(&["open", "--dry-run"])), Start::Engine(args(&["open", "--dry-run"])));
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
    fn a_failure_exits_1_whatever_it_was() {
        // Omarchy's commands exit 0 or 1: bad arguments are a failure like any other, told apart by the line on
        // standard error. (The usage check comes before the home folder is looked at, so this touches nothing.)
        assert_eq!(skill(&args(&["frobnicate"])), 1);
        assert_eq!(skill(&args(&["install", "now"])), 1);
        assert!(HELP.contains("1 any failure") && !HELP.contains(" 2 "), "the help says so");
    }

    #[test]
    fn the_skill_installs_itself_and_stays_removed() {
        let home = std::env::temp_dir().join(format!("lastbook-skill-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&home);
        let shared = home.join(".agents/skills/lastbook");
        let claude = home.join(".claude/skills/lastbook");
        // First use installs it, with its links.
        ensure_skill_in(&home);
        assert_eq!(fs::read_to_string(shared.join("SKILL.md")).unwrap(), SKILL);
        assert_eq!(fs::read_link(&claude).unwrap(), shared);
        // An older copy is brought up to date.
        fs::write(shared.join("SKILL.md"), "old").unwrap();
        ensure_skill_in(&home);
        assert_eq!(fs::read_to_string(shared.join("SKILL.md")).unwrap(), SKILL);
        // Removed by the person: gone, links too, and not put back.
        uninstall_skill(&home).unwrap();
        assert!(!shared.exists() && claude.symlink_metadata().is_err());
        ensure_skill_in(&home);
        assert!(!shared.exists());
        // Asked for again: back.
        install_skill(&home).unwrap();
        ensure_skill_in(&home);
        assert!(shared.join("SKILL.md").is_file());
        // A skill of that name that is not Lastbook's is never touched.
        uninstall_skill(&home).unwrap();
        fs::remove_file(declined(&home)).unwrap();
        fs::create_dir_all(&shared).unwrap();
        fs::write(shared.join("SKILL.md"), "someone else's").unwrap();
        ensure_skill_in(&home);
        assert_eq!(fs::read_to_string(shared.join("SKILL.md")).unwrap(), "someone else's");
        uninstall_skill(&home).unwrap();
        assert_eq!(fs::read_to_string(shared.join("SKILL.md")).unwrap(), "someone else's");
        let _ = fs::remove_dir_all(&home);
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
