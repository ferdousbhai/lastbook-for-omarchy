// SPDX-License-Identifier: MIT
//! Lastbook for Omarchy: the native window (Rust, GTK 4) over the Python engine (app/engine_pipe.py).
//!
//!   lastbook open [--data FOLDER]         the window (and with no command at all)
//!   lastbook COMMAND ...                  the library as JSON, for scripts and agents (cli.rs, app/cli.py)
//!
//! The engine finds the library (--data, LASTBOOK_DATA, the folder chosen last time, or the app's own data folder) and
//! answers over pipes; this window reads the library's files (PDFs, thumbnails, videos) directly.

/// Marks a literal as one of the strings the catalogs list (src/i18n/en.json), for a table of constants that cannot
/// call `omui::tr`: the text is translated where it is shown, with `omui::i18n::translate`. The catalog test counts it
/// as `tr` (`ui::tests`).
macro_rules! n_ {
    ($text:literal) => {
        $text
    };
}

mod cards;
mod cli;
mod encyclopedias;
mod engine;
mod guides;
mod library;
mod map;
mod mvt;
mod pdf;
mod reader;
mod search;
mod setup;
mod videos;
mod web;
mod ui;

use gtk::{gio, glib, prelude::*};
use omui::tr;

/// Omarchy's one name for the app: the command, the `.desktop` file, the icon, and the window's Wayland app id
/// (and X11 class), which omui gives every window of the application.
pub const APP_ID: &str = "lastbook";
/// The application's name on D-Bus, which must have a dot in it: the one it has always had, so nothing that knows it
/// changes. The application is NON_UNIQUE, so it owns no name on the bus by it.
const DBUS_ID: &str = "io.github.ferdousbhai.Lastbook";

fn main() -> glib::ExitCode {
    // A command is answered before anything of GTK starts: only the window needs a display.
    let args: Vec<String> = std::env::args().skip(1).collect();
    let data = match cli::start(&args) {
        cli::Start::Window(data) => data,
        cli::Start::Help => {
            println!("{}", cli::HELP);
            return glib::ExitCode::SUCCESS;
        }
        cli::Start::Version => {
            println!("lastbook {}", env!("CARGO_PKG_VERSION"));
            return glib::ExitCode::SUCCESS;
        }
        cli::Start::Skill(rest) => std::process::exit(cli::skill(&rest)),
        cli::Start::Engine(args) => {
            // Not for `index`: the background service runs it at every login, which is not the person using Lastbook.
            if args.iter().all(|a| a != "index") {
                cli::ensure_skill();
            }
            eprintln!("lastbook: could not start the engine: {}", cli::run_engine(&args));
            std::process::exit(1);
        }
        cli::Start::Unknown(arg) => {
            eprintln!("lastbook: unknown command {arg:?} (see: lastbook --help)");
            std::process::exit(1);
        }
    };
    cli::ensure_skill();
    omui::i18n::register_all(CATALOGS);
    #[cfg(feature = "qa")]
    omui::i18n::register_all(&[("da", include_str!("../dev/qa/da.json"))]);
    let app = omui::application(DBUS_ID, APP_ID, "Lastbook", include_str!("style.css"), move |app| {
        // Installed when the application started; missing only without a display.
        let Some(theme) = omui::Theme::get() else { return };
        gtk::Window::set_default_icon_name(APP_ID);
        // The window first, the engine after: the engine loads the catalog and opens the indexes before it answers,
        // which takes from a moment to many seconds on a large library or a slow disk, and a program that shows
        // nothing meanwhile looks as if it had not started.
        let window = gtk::ApplicationWindow::builder().application(app).title("Lastbook").default_width(1280).default_height(820).build();
        let title = gtk::Label::new(Some("Lastbook"));
        title.add_css_class("title");
        window.set_titlebar(Some(&omui::header(Some(title.upcast_ref()))));
        let waiting = std::rc::Rc::new(omui::EmptyState::new(None, &tr("Opening your library…"), None));
        let spinner = omui::spinner();
        spinner.set_size_request(32, 32);
        waiting.set_child(Some(spinner.upcast_ref()));
        retitle(&window, &waiting, "lastbook-waiting", || tr("Opening your library…"));
        let shell = omui::Shell::new(&window, &waiting.widget);
        // Ctrl+Q and Ctrl+W, on whatever the window shows (waiting for the engine, setup, the library, a failure). They
        // close the window as its own button does, so a download in progress asks first (setup.rs, `guard_close`).
        omui::quit_keys(&window);
        // `?` and F1 list the keys; the library's screens have more of them than setup (ui::shortcuts).
        let library = std::rc::Rc::new(std::cell::Cell::new(false));
        omui::ShortcutOverlay::new({
            let library = library.clone();
            move || ui::shortcuts(library.get())
        })
        .install(&window, false);
        window.present();
        let (app, data) = (app.clone(), data.clone());
        omui::background(
            move || engine::Engine::start(data.as_deref()),
            move |started| {
                if window.application().is_none() {
                    return;   // closed while it waited: the engine leaves as its input closes, here
                }
                match started.unwrap_or_else(|e| Err(engine::EngineError(e))) {
                    Ok(engine) if engine.ready.setup => {
                        // No library yet: the window is setup, and nothing else.
                        omui::i18n::bind(&title, {
                            let window = window.downgrade();
                            move |title| {
                                title.set_text(&tr("Set up Lastbook"));
                                if let Some(window) = window.upgrade() {
                                    window.set_title(Some(&tr("Set up Lastbook")));
                                }
                            }
                        });
                        window.set_default_size(900, 760);
                        let page = setup::SetupPage::build(engine.clone());
                        page.set_window(&window);
                        shell.overlay.set_child(Some(&page.root));
                        page.refresh();
                        setup::follow(&app, &engine, &page);
                        page.guard_close(&app);
                        say(&engine, window.upcast_ref());
                        unsafe { app.set_data("lastbook-setup", page) };
                    }
                    Ok(engine) => {
                        library.set(true);
                        let ui = ui::App::build(window.clone(), shell, engine.clone(), theme);
                        if let Some(page) = ui.manage.borrow().as_ref() {
                            setup::follow(&app, &engine, page);
                            page.guard_close(&app);
                            // A download finished with the window put away is shown where its buttons are.
                            let back = std::rc::Rc::downgrade(&ui);
                            page.on_return(move || {
                                if let Some(ui) = back.upgrade() {
                                    ui.navigate(ui::Route::Manage);
                                }
                            });
                        }
                        say(&engine, window.upcast_ref());
                        // The screens live as long as the application does.
                        unsafe { app.set_data("lastbook-ui", ui) };
                    }
                    Err(e) => {
                        // The reason is the engine's own sentence (engine.rs, EngineError), shown as it came.
                        let failed = std::rc::Rc::new(omui::EmptyState::new(Some(omui::icons::name::WARNING), &tr("Lastbook could not open its library."), Some(&e.to_string())));
                        retitle(&window, &failed, "lastbook-failed", || tr("Lastbook could not open its library."));
                        shell.overlay.set_child(Some(&failed.widget));
                    }
                }
            },
        );
    });
    // NON_UNIQUE: the library's own lock (the engine) decides whether a second window may start.
    app.set_flags(gio::ApplicationFlags::NON_UNIQUE);
    app.run_with_args::<&str>(&[])
}

/// Shows what the engine had to tell the person when it started (a library switch that failed or was undone). The
/// notice is the engine's sentence about this person's library, shown as it came; the heading is the window's.
/// Dismissing it is the only answer there is.
fn say(engine: &engine::Engine, window: &gtk::Window) {
    if let Some(notice) = engine.ready.notice.as_deref().filter(|n| !n.is_empty()) {
        // The heading, the notice and the button follow a switch of language while the dialog is open.
        let dialog = omui::Dialog::new(Some(""), Some(""));
        dialog.set_heading_translated("About your library");
        let notice = notice.to_owned();
        dialog.bind(move |dialog| dialog.set_body(&omui::i18n::translate(&notice)));
        dialog.add_response("close", "");
        dialog.set_response_translated("close", "_Close");
        dialog.set_default_response("close");
        dialog.set_close_response("close");
        dialog.present(window);
    }
}

/// Writes `state`'s title again after each switch of language, for as long as the window shows it. The window
/// keeps the state (under `key`); the listener holds it weakly, since a listener still held as the program ends is
/// dropped after GTK's own thread state is gone.
fn retitle(window: &gtk::ApplicationWindow, state: &std::rc::Rc<omui::EmptyState>, key: &str, title: fn() -> String) {
    // SAFETY: the value is only ever read back as the type it was stored as, by nobody but this window.
    unsafe { window.set_data(key, state.clone()) };
    let weak = std::rc::Rc::downgrade(state);
    omui::i18n::on_change(&state.widget, move || {
        if let Some(state) = weak.upgrade() {
            state.set_title(&title());
        }
    });
}

/// The catalogs of the app's own strings. English lists every one; other languages come later.
const CATALOGS: &[(&str, &str)] = &[("en", include_str!("i18n/en.json"))];

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn one_name_for_the_command_the_window_and_the_desktop_entry() {
        assert_eq!(APP_ID, "lastbook");
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
        let desktop = std::fs::read_to_string(root.join("packaging/linux/lastbook.desktop")).unwrap();
        for line in ["Exec=lastbook open", "Icon=lastbook", "StartupWMClass=lastbook"] {
            assert!(desktop.lines().any(|l| l == line), "{line}");
        }
        assert!(!root.join("packaging/linux/io.github.ferdousbhai.Lastbook.desktop").exists());
    }

    #[test]
    fn the_catalogs_match_the_sources() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        omui::i18n::CatalogCheck::new(root.join("src"), root.join("src/i18n")).function("n_", 0, false).assert_ok();
    }
}
