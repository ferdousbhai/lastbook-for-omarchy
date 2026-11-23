// SPDX-License-Identifier: MIT
//! Encyclopedias: each one the library holds, opened at its front page, and an article picked at random to read, so
//! that they can be browsed and not only met in a search (/api/encyclopedias, /api/encyclopedia/random). An article
//! opens in the web view (`Route::Wiki`), and its links lead on from there.

use std::{
    cell::Cell,
    rc::{Rc, Weak},
};

use gtk::{glib, prelude::*};
use omui::{clear, icons::name, label, tr};
use serde_json::Value;

use crate::{
    cards::{self, activate, card, column, encyclopedia_name, on_click, plain_markup, section},
    ui::{App, Route},
};

pub struct Encyclopedias {
    pub root: gtk::Widget,
    app: Weak<App>,
    list: gtk::Box,
    /// Holds the "Something to read" card, which "Another article" replaces.
    pick: gtk::Box,
    /// The list was drawn: it is kept as it is until a language switch, or a visit after it failed to load.
    loaded: Cell<bool>,
}

impl Encyclopedias {
    pub fn build(app: &Weak<App>) -> Rc<Self> {
        let (scroll, list) = column(12);
        let pick = gtk::Box::new(gtk::Orientation::Vertical, 0);
        Rc::new(Self { root: scroll.upcast(), app: app.clone(), list, pick, loaded: Cell::new(false) })
    }

    /// After a language switch: drawn again when next shown (the screen on show at once, by ui.rs).
    pub fn retranslate(&self) {
        self.loaded.set(false);
    }

    pub fn show(self: &Rc<Self>) {
        if self.loaded.replace(true) {
            return;
        }
        clear(&self.list);
        self.list.append(&label(&tr("Encyclopedias"), "page-title"));
        let Some(app) = self.app.upgrade() else { return };
        let (engine, me) = (app.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let found = engine.get("/api/encyclopedias").await;
            let Some(me) = me.upgrade() else { return };
            match found {
                Ok(v) => me.draw(v.as_array().map(Vec::as_slice).unwrap_or_default()),
                Err(e) => {
                    me.loaded.set(false);
                    me.list.append(&cards::problem(&tr("Lastbook could not read the list of encyclopedias."), &e.to_string(), &me, |me| me.show()));
                }
            }
        });
    }

    fn draw(self: &Rc<Self>, all: &[Value]) {
        clear(&self.list);
        let head = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        let title = label(&tr("Encyclopedias"), "page-title");
        title.set_hexpand(true);
        head.append(&title);
        self.list.append(&head);
        if all.is_empty() {
            let state = omui::EmptyState::new(
                Some(name::BOOK),
                &tr("No encyclopedia is installed"),
                Some(&tr("Encyclopedias come with the library's packs: add one in Library and downloads.")),
            );
            let go = gtk::Button::with_label(&tr("Library and downloads"));
            go.add_css_class("chip");
            let app = self.app.clone();
            go.connect_clicked(move |_| {
                if let Some(app) = app.upgrade() {
                    app.navigate(Route::Section("manage"));
                }
            });
            state.set_child(Some(go.upcast_ref()));
            self.list.append(&state.widget);
            return;
        }
        // An article picked at random opens at once; "Something to read" shows one first, to choose by its opening.
        let random = gtk::Button::with_label(&tr("Random article"));
        random.set_valign(gtk::Align::Center);
        on_click(&random, self, |me| {
            me.random(|me, a| {
                if let (Some(app), Some(path)) = (me.app.upgrade(), a["wiki"].as_str()) {
                    app.navigate(Route::Wiki { path: path.to_owned() });
                }
            });
        });
        head.append(&random);

        let row = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        let heading = section(&tr("Something to read"));
        heading.set_hexpand(true);
        row.append(&heading);
        let another = gtk::Button::with_label(&tr("Another article"));
        another.add_css_class("flat");
        another.set_valign(gtk::Align::End);
        on_click(&another, self, |me| me.fill_pick());
        row.append(&another);
        self.list.append(&row);
        clear(&self.pick);
        self.list.append(&self.pick);
        self.fill_pick();

        self.list.append(&section(&tr("Front pages")));
        let grid = cards::grid(3, 8);
        for e in all {
            let file = e["file"].as_str().unwrap_or_default();
            let known = encyclopedia_name(file);
            let name = if known == file { e["title"].as_str().unwrap_or(file).to_owned() } else { known };
            let articles = e["articles"].as_i64().map(|n| omui::trn!("{n} articles", n)).unwrap_or_default();
            let c = card(&name, &glib::markup_escape_text(e["description"].as_str().unwrap_or_default()), &articles, None);
            c.add_css_class("guide-card");
            activate(&c, self.app.clone(), e["main"].as_str().map(|main| Route::Wiki { path: main.to_owned() }));
            grid.insert(&c, -1);
        }
        self.list.append(&grid);
    }

    /// "Something to read": a random article's title and opening, which open it. The one before stays until the
    /// next has come.
    fn fill_pick(self: &Rc<Self>) {
        self.random(|me, a| {
            clear(&me.pick);
            let file = a["file"].as_str().unwrap_or_default();
            let c = card(a["title"].as_str().unwrap_or_default(), &plain_markup(a["snippet"].as_str().unwrap_or_default(), 400), &encyclopedia_name(file), None);
            activate(&c, me.app.clone(), a["wiki"].as_str().map(|path| Route::Wiki { path: path.to_owned() }));
            me.pick.append(&c);
        });
    }

    /// Asks the engine for a random article of any encyclopedia, and hands it to `then`; says so if there is none.
    fn random(self: &Rc<Self>, then: impl FnOnce(Rc<Self>, Value) + 'static) {
        let Some(app) = self.app.upgrade() else { return };
        let (engine, me) = (app.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let picked = engine.get("/api/encyclopedia/random").await;
            let Some(me) = me.upgrade() else { return };
            match picked {
                Ok(a) => then(me, a),
                Err(e) => {
                    if let Some(app) = me.app.upgrade() {
                        app.shell.toast(&tr!("Lastbook could not pick an article: {}", omui::i18n::translate(&e.to_string())));
                    }
                }
            }
        });
    }
}
