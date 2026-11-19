// SPDX-License-Identifier: MIT
//! Lastbook for Omarchy: the native window (Rust, GTK 4) over the Python engine (app/engine_pipe.py).
//!
//!   lastbook [--data FOLDER]              the window
//!   lastbook COMMAND ...                  the library as JSON, for scripts and agents (cli.rs, app/cli.py)
//!
//! The engine finds the library (--data, LASTBOOK_DATA, the folder chosen last time, or the app's own data folder) and
//! answers over pipes; this window reads the library's files (PDFs, thumbnails, videos) directly.

mod cards;
mod chat;
mod cli;
mod engine;
mod guides;
mod library;
mod map;
mod md;
mod mvt;
mod pdf;
mod reader;
mod search;
mod setup;
mod theme;
mod videos;
mod web;
mod ui;

use gtk::{gdk, gio, glib, prelude::*};

pub const APP_ID: &str = "io.github.ferdousbhai.Lastbook";

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
            eprintln!("lastbook: could not start the engine: {}", cli::run_engine(&args));
            std::process::exit(1);
        }
        cli::Start::Unknown(arg) => {
            eprintln!("lastbook: unknown command {arg:?} (see: lastbook --help)");
            std::process::exit(2);
        }
    };
    cli::refresh_skill();
    glib::set_prgname(Some(APP_ID));
    glib::set_application_name("Lastbook");
    // NON_UNIQUE: the library's own lock (the engine) decides whether a second window may start.
    let app = gtk::Application::builder().application_id(APP_ID).flags(gio::ApplicationFlags::NON_UNIQUE).build();
    app.connect_activate(move |app| {
        let Some(display) = gdk::Display::default() else { return };
        let theme = theme::Theme::install(&display);
        gtk::Window::set_default_icon_name(APP_ID);
        match engine::Engine::start(data.as_deref()) {
            Ok(engine) if engine.ready.setup => {
                // No library yet: the window is setup, and nothing else.
                setup::restart_on_notice(app, &engine);
                let w = gtk::ApplicationWindow::builder().application(app).title("Set up Lastbook").default_width(900).default_height(760).build();
                let header = gtk::HeaderBar::new();
                header.set_show_title_buttons(false);
                let t = gtk::Label::new(Some("Set up Lastbook"));
                t.add_css_class("header-title");
                header.set_title_widget(Some(&t));
                w.set_titlebar(Some(&header));
                let page = setup::SetupPage::build(engine);
                page.set_window(&w);
                w.set_child(Some(&page.root));
                page.refresh();
                w.present();
                unsafe { app.set_data("lastbook-setup", (page, theme)) };
            }
            Ok(engine) => {
                setup::restart_on_notice(app, &engine);
                let ui = ui::App::build(app, engine, theme);
                ui.window.present();
                // The window (and the theme it holds) lives as long as the application does.
                unsafe { app.set_data("lastbook-ui", ui) };
            }
            Err(e) => {
                let w = gtk::ApplicationWindow::builder().application(app).title("Lastbook").default_width(560).default_height(260).build();
                let l = gtk::Label::new(Some(&format!("Lastbook could not open its library.\n\n{e}")));
                l.add_css_class("empty-state");
                l.set_wrap(true);
                l.set_margin_start(24);
                l.set_margin_end(24);
                w.set_child(Some(&l));
                w.present();
            }
        }
    });
    app.run_with_args::<&str>(&[])
}
