// SPDX-License-Identifier: MIT
//! Lastbook for Omarchy: the native window (Rust, GTK 4) over the Python engine (app/engine_pipe.py).
//!
//!   lastbook [--data FOLDER]
//!
//! The engine finds the library (--data, LASTBOOK_DATA, the folder chosen last time, or the app's own data folder) and
//! answers over pipes; this window reads the library's files (PDFs, thumbnails, videos) directly.

mod engine;
mod pdf;
mod reader;
mod search;
mod theme;
mod ui;

use gtk::{gdk, gio, glib, prelude::*};

pub const APP_ID: &str = "io.github.ferdousbhai.Lastbook";

fn main() -> glib::ExitCode {
    let mut data: Option<String> = None;
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        match a.as_str() {
            "--data" => data = args.next(),
            "--version" => {
                println!("lastbook {}", env!("CARGO_PKG_VERSION"));
                return glib::ExitCode::SUCCESS;
            }
            _ => {}
        }
    }
    glib::set_prgname(Some(APP_ID));
    glib::set_application_name("Lastbook");
    // NON_UNIQUE: the library's own lock (the engine) decides whether a second window may start.
    let app = gtk::Application::builder().application_id(APP_ID).flags(gio::ApplicationFlags::NON_UNIQUE).build();
    app.connect_activate(move |app| {
        let Some(display) = gdk::Display::default() else { return };
        let _theme = theme::Theme::install(&display);
        gtk::Window::set_default_icon_name(APP_ID);
        match engine::Engine::start(data.as_deref()) {
            Ok(engine) => {
                let ui = ui::App::build(app, engine);
                ui.window.present();
                // The theme and the window live as long as the application does.
                unsafe { app.set_data("lastbook-ui", (ui, _theme)) };
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
