// SPDX-License-Identifier: MIT
//! The browsing screens: Library (shelves, then a shelf's collections and documents), Encyclopedias (search inside the
//! ZIM collections), Saved (what the person starred) and About (what this library holds, and credits).

use std::{
    path::PathBuf,
    rc::{Rc, Weak},
};

use gtk::{glib, prelude::*};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::{
    cards::{activate, card, clear, route_of, section},
    engine::query,
    ui::{App, Route},
};

fn column() -> (gtk::ScrolledWindow, gtk::Box) {
    let b = gtk::Box::new(gtk::Orientation::Vertical, 12);
    b.set_margin_top(28);
    b.set_margin_bottom(28);
    b.set_margin_start(32);
    b.set_margin_end(32);
    let s = gtk::ScrolledWindow::builder().child(&b).vexpand(true).hscrollbar_policy(gtk::PolicyType::Never).build();
    (s, b)
}

fn title(text: &str, sub: &str, into: &gtk::Box) {
    let t = gtk::Label::new(Some(text));
    t.add_css_class("page-title");
    t.set_xalign(0.0);
    t.set_wrap(true);
    into.append(&t);
    if !sub.is_empty() {
        let s = gtk::Label::new(Some(sub));
        s.add_css_class("dim");
        s.set_xalign(0.0);
        s.set_wrap(true);
        into.append(&s);
    }
}

fn doc_card(app: &Weak<App>, d: &Value) -> gtk::Widget {
    let mut meta = vec![d["kindLabel"].as_str().unwrap_or_default().to_owned(), d["publisher"].as_str().unwrap_or_default().to_owned()];
    if let Some(y) = d["year"].as_str() {
        meta.push(y.to_owned());
    }
    if let Some(p) = d["pages"].as_i64() {
        meta.push(format!("{p} pages"));
    }
    let c = card(d["title"].as_str().unwrap_or_default(), "", &meta.into_iter().filter(|m| !m.is_empty()).collect::<Vec<_>>().join(" · "), d["trust"].as_str());
    if let Some(r) = route_of(d) {
        activate(&c, app.clone(), r);
    }
    c.upcast()
}

// ------------------------------------------------------------------------------------------------------------ saved --
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Saved {
    pub key: String,
    pub title: String,
    pub sub: String,
    pub at: i64,
}

fn saved_path() -> PathBuf {
    glib::user_data_dir().join("Lastbook").join("saved.json")
}

pub fn load_saved() -> Vec<Saved> {
    std::fs::read(saved_path()).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default()
}

pub fn store_saved(items: &[Saved]) {
    let p = saved_path();
    if let Some(d) = p.parent() {
        let _ = std::fs::create_dir_all(d);
    }
    if let Ok(b) = serde_json::to_vec_pretty(items) {
        let tmp = p.with_extension("tmp");
        if std::fs::write(&tmp, b).is_ok() {
            let _ = std::fs::rename(tmp, p);
        }
    }
}

pub struct Browse {
    pub library_root: gtk::Widget,
    pub shelf_root: gtk::Widget,
    pub enc_root: gtk::Widget,
    pub saved_root: gtk::Widget,
    pub about_root: gtk::Widget,
    app: Weak<App>,
    library: gtk::Box,
    shelf: gtk::Box,
    enc: gtk::Box,
    enc_results: gtk::Box,
    saved: gtk::Box,
    about: gtk::Box,
}

impl Browse {
    pub fn build(app: &Rc<App>) -> Rc<Self> {
        let (ls, library) = column();
        let (ss, shelf) = column();
        let (es, enc) = column();
        let (sv, saved) = column();
        let (ab, about) = column();
        title("Encyclopedias", "Complete offline encyclopedias. Community-written: check safety-critical steps against a manual.", &enc);
        let entry = gtk::SearchEntry::new();
        entry.set_placeholder_text(Some("Search the encyclopedias"));
        enc.append(&entry);
        let enc_results = gtk::Box::new(gtk::Orientation::Vertical, 8);
        enc.append(&enc_results);
        let b = Rc::new(Self {
            library_root: ls.upcast(),
            shelf_root: ss.upcast(),
            enc_root: es.upcast(),
            saved_root: sv.upcast(),
            about_root: ab.upcast(),
            app: Rc::downgrade(app),
            library,
            shelf,
            enc,
            enc_results,
            saved,
            about,
        });
        let me = Rc::downgrade(&b);
        entry.connect_activate(move |e| {
            if let Some(me) = me.upgrade() {
                me.search_encyclopedias(e.text().as_str());
            }
        });
        let _ = &b.enc;
        b
    }

    pub fn show_library(self: &Rc<Self>) {
        if self.library.first_child().is_some() {
            return;
        }
        title("Library", "Every manual, guide and saved page on this computer, by subject.", &self.library);
        let Some(app) = self.app.upgrade() else { return };
        let (engine, me) = (app.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let Ok(shelves) = engine.get("/api/shelves").await else { return };
            let Some(me) = me.upgrade() else { return };
            let grid = gtk::FlowBox::new();
            grid.set_selection_mode(gtk::SelectionMode::None);
            grid.set_homogeneous(true);
            grid.set_min_children_per_line(2);
            grid.set_max_children_per_line(3);
            grid.set_row_spacing(8);
            grid.set_column_spacing(8);
            for s in shelves.as_array().into_iter().flatten() {
                let counts = format!("{} to read · {} video{}", s["read"], s["video"], if s["video"] == 1 { "" } else { "s" });
                let c = card(s["title"].as_str().unwrap_or_default(), "", &counts, None);
                c.add_css_class("guide-card");
                activate(&c, me.app.clone(), Route::Shelf(s["id"].as_str().unwrap_or_default().to_owned()));
                grid.insert(&c, -1);
            }
            me.library.append(&grid);
        });
    }

    pub fn show_shelf(self: &Rc<Self>, id: &str, name: &str) {
        clear(&self.shelf);
        title(name, "", &self.shelf);
        let Some(app) = self.app.upgrade() else { return };
        let (engine, me, id) = (app.engine.clone(), Rc::downgrade(self), id.to_owned());
        glib::spawn_future_local(async move {
            // The shelf's answer has no title: the list of shelves does.
            let name = engine.get("/api/shelves").await.ok().and_then(|v| {
                v.as_array()?.iter().find(|s| s["id"] == id.as_str()).and_then(|s| s["title"].as_str().map(str::to_owned))
            });
            let Ok(s) = engine.get(&format!("/api/shelf/{}", urlencoding::encode(&id))).await else { return };
            let Some(me) = me.upgrade() else { return };
            if let Some(name) = name {
                clear(&me.shelf);
                title(&name, "", &me.shelf);
                if let Some(app) = me.app.upgrade() {
                    app.set_title(&name);
                }
            }
            for col in s["collections"].as_array().into_iter().flatten() {
                me.shelf.append(&section(&format!("{} ({})", col["collection"].as_str().unwrap_or_default(), col["count"])));
                for d in col["items"].as_array().into_iter().flatten() {
                    me.shelf.append(&doc_card(&me.app, d));
                }
            }
        });
    }

    fn search_encyclopedias(self: &Rc<Self>, q: &str) {
        clear(&self.enc_results);
        let Some(app) = self.app.upgrade() else { return };
        if q.trim().is_empty() {
            return;
        }
        let (engine, me, q) = (app.engine.clone(), Rc::downgrade(self), q.to_owned());
        glib::spawn_future_local(async move {
            let r = engine.get(&query("/api/collection-search", &[("q", &q)])).await;
            let Some(me) = me.upgrade() else { return };
            let hits: Vec<Value> = r.ok().and_then(|v| v.as_array().cloned()).unwrap_or_default();
            if hits.is_empty() {
                let l = gtk::Label::new(Some("No articles found. Try another word for the same thing."));
                l.add_css_class("empty-state");
                me.enc_results.append(&l);
                return;
            }
            for h in hits {
                let name = h["collection"].as_str().unwrap_or_default();
                let label = if name.starts_with("mdwiki") { "Medical encyclopedia (MDWiki)" } else if name.starts_with("appropedia") { "Appropedia — appropriate technology" } else { name };
                let c = card(h["title"].as_str().unwrap_or_default(), "", label, None);
                let path = h["path"].as_str().unwrap_or_default().trim_start_matches("zim/").to_owned();
                activate(&c, me.app.clone(), Route::Wiki { path });
                me.enc_results.append(&c);
            }
        });
    }

    pub fn show_saved(self: &Rc<Self>) {
        clear(&self.saved);
        title("Saved", "Pages, articles, videos and guides you starred (the star in the header bar).", &self.saved);
        let items = load_saved();
        if items.is_empty() {
            let l = gtk::Label::new(Some("Nothing saved yet."));
            l.add_css_class("empty-state");
            l.set_xalign(0.0);
            self.saved.append(&l);
        }
        let mut items = items;
        items.sort_by_key(|s| -s.at);
        for s in items {
            let c = card(&s.title, "", &s.sub, None);
            if let Some(r) = Route::from_key(&s.key) {
                activate(&c, self.app.clone(), r);
            }
            self.saved.append(&c);
        }
    }

    pub fn show_about(self: &Rc<Self>) {
        clear(&self.about);
        title("About this library", "Public manuals, health guides, videos and encyclopedias kept on this computer, so it works when the internet and phones don't.", &self.about);
        let Some(app) = self.app.upgrade() else { return };
        let (engine, me) = (app.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let lib = engine.get("/api/library").await.unwrap_or_default();
            let st = engine.get("/api/assistant/status").await.unwrap_or_default();
            let Some(me) = me.upgrade() else { return };
            let gb = lib["document_bytes"].as_f64().unwrap_or(0.0) / 1e9;
            me.about.append(&card(
                "What's in it",
                &glib::markup_escape_text(&format!(
                    "{} PDF manuals ({} pages), {} saved web pages, {} videos and {} complete encyclopedias; {gb:.1} GB of documents.",
                    lib["pdfs"], lib["pdf_pages"], lib["web_pages"], lib["videos"], lib["collections"]
                )),
                "",
                None,
            ));
            let ix = &st["index"];
            let search = if ix["state"] == "waiting for Recall" {
                "Meaning-based search is off: it uses Recall for Omarchy, which isn't running. Search by words works.".to_owned()
            } else {
                format!("Meaning-based search (through Recall for Omarchy): {} passages from {} documents.", ix["chunks"], ix["docs"])
            };
            let ai = st["local"]["name"].as_str().map(|n| format!("Local AI: {n} — {}.", st["local"]["state"].as_str().unwrap_or("unknown"))).unwrap_or_else(|| "No local AI.".into());
            me.about.append(&card("Search and the assistant", &glib::markup_escape_text(&format!("{search} {ai}")), "", None));
            me.about.append(&card(
                "Privacy",
                &glib::markup_escape_text("Lastbook opens no network port and sends nothing anywhere. The engine runs beside this window over pipes; meaning-based search asks Recall over a local socket. Only setup downloads, and only when you start it."),
                "",
                None,
            ));
            me.about.append(&card(
                "Credits",
                &glib::markup_escape_text("Based on Lastbook (lastbook.org), MIT. The look follows Strata (github.com/lgse/strata), MIT. Encyclopedias by Kiwix and their contributors. Map data © OpenStreetMap contributors (ODbL) where included. Each document keeps its own licence: see Sources and permissions in the library."),
                &format!("Lastbook for Omarchy {}", app_version(&me.app)),
                None,
            ));
            if let Some(src) = lib["sources"].as_str() {
                let c = card("Sources and permissions", "", "Where every document came from, and its licence", None);
                activate(&c, me.app.clone(), Route::Read { doc: src.to_owned(), page: 1 });
                me.about.append(&c);
            }
        });
    }
}

fn app_version(app: &Weak<App>) -> String {
    app.upgrade().and_then(|a| a.engine.ready.version.clone()).unwrap_or_else(|| env!("CARGO_PKG_VERSION").to_owned())
}
