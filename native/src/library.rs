// SPDX-License-Identifier: MIT
//! The browsing screens: Library (shelves, then a shelf's collections and documents), Saved (what the person starred)
//! and About (what this library holds, and credits).

use std::{
    path::PathBuf,
    rc::{Rc, Weak},
};

use gtk::{glib, prelude::*};
use omui::{clear, label};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::{
    cards::{self, activate, card, column, route_of, section, some_then_all, text_card},
    engine::{load_json, store_json},
    ui::{App, Route},
};

fn title(text: &str, sub: &str, into: &gtk::Box) {
    into.append(&label(text, "page-title"));
    if !sub.is_empty() {
        into.append(&label(sub, "dim-label"));
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
    load_json(&saved_path())
}

pub fn store_saved(items: &[Saved]) {
    store_json(&saved_path(), items);
}

pub struct Browse {
    pub library_root: gtk::Widget,
    pub shelf_root: gtk::Widget,
    pub saved_root: gtk::Widget,
    pub about_root: gtk::Widget,
    app: Weak<App>,
    library: gtk::Box,
    shelf: gtk::Box,
    saved: gtk::Box,
    about: gtk::Box,
}

impl Browse {
    pub fn build(app: &Rc<App>) -> Rc<Self> {
        let (ls, library) = column(12);
        let (ss, shelf) = column(12);
        let (sv, saved) = column(12);
        let (ab, about) = column(12);
        Rc::new(Self {
            library_root: ls.upcast(),
            shelf_root: ss.upcast(),
            saved_root: sv.upcast(),
            about_root: ab.upcast(),
            app: Rc::downgrade(app),
            library,
            shelf,
            saved,
            about,
        })
    }

    pub fn show_library(self: &Rc<Self>) {
        if self.library.first_child().is_some() {
            return;
        }
        title("Library", "", &self.library);
        let Some(app) = self.app.upgrade() else { return };
        let (engine, me) = (app.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let shelves = engine.get("/api/shelves").await;
            let Some(me) = me.upgrade() else { return };
            let Ok(shelves) = shelves else {
                return clear(&me.library);   // empty again, so the next visit asks again
            };
            let grid = cards::grid(3, 8);
            for s in shelves.as_array().into_iter().flatten() {
                let (read, video) = (s["read"].as_u64().unwrap_or(0), s["video"].as_u64().unwrap_or(0));
                if read + video == 0 {
                    continue;   // nothing on this shelf in this library
                }
                let counts = [(read > 0).then(|| format!("{read} to read")), (video > 0).then(|| format!("{video} video{}", if video == 1 { "" } else { "s" }))]
                    .into_iter()
                    .flatten()
                    .collect::<Vec<_>>()
                    .join(" · ");
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
                    app.report_title("shelf", &name);
                }
            }
            for col in s["collections"].as_array().into_iter().flatten() {
                me.shelf.append(&section(&format!("{} ({})", col["collection"].as_str().unwrap_or_default(), col["count"])));
                let (list, app) = (gtk::Box::new(gtk::Orientation::Vertical, 12), me.app.clone());
                me.shelf.append(&list);
                let items = col["items"].as_array().cloned().unwrap_or_default();
                let l = list.clone();
                some_then_all(&me.shelf, &items, 6, move |docs| docs.iter().for_each(|d| l.append(&doc_card(&app, d))));
            }
        });
    }

    pub fn show_saved(self: &Rc<Self>) {
        clear(&self.saved);
        title("Saved", "", &self.saved);
        let items = load_saved();
        if items.is_empty() {
            let l = gtk::Label::new(Some("Nothing saved yet. Star a page, video or guide to keep it here (Ctrl+D)."));
            l.add_css_class("status-line");
            l.set_xalign(0.0);
            l.set_wrap(true);
            self.saved.append(&l);
        }
        let mut items = items;
        items.sort_by_key(|s| std::cmp::Reverse(s.at));
        for s in items {
            let c = card(&s.title, "", &s.sub, None);
            if let Some(mut r) = Route::from_key(&s.key) {
                if let Route::Web { meta, .. } = &mut r {
                    *meta = s.sub.clone();
                }
                activate(&c, self.app.clone(), r);
            }
            self.saved.append(&c);
        }
    }

    pub fn show_about(self: &Rc<Self>) {
        clear(&self.about);
        title("About this library", "", &self.about);
        let Some(app) = self.app.upgrade() else { return };
        let (engine, me) = (app.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let lib = engine.get("/api/library").await.unwrap_or_default();
            let st = engine.get("/api/assistant/status").await.unwrap_or_default();
            let Some(me) = me.upgrade() else { return };
            clear(&me.about);   // opened twice before the answers came: one set of cards
            title("About this library", "", &me.about);
            let gb = lib["document_bytes"].as_f64().unwrap_or(0.0) / 1e9;
            me.about.append(&text_card(
                "What's in it",
                &glib::markup_escape_text(&format!(
                    "{} PDF manuals ({} pages), {} saved web pages, {} videos and {} complete encyclopedias; {gb:.1} GB of documents.",
                    lib["pdfs"], lib["pdf_pages"], lib["web_pages"], lib["videos"], lib["collections"]
                )),
                "",
            ));
            let ix = &st["index"];
            let search = if ix["state"] == "waiting for Recall" {
                "Meaning-based search is off: Recall for Omarchy isn't running.".to_owned()
            } else {
                format!("Meaning-based search (through Recall for Omarchy): {} passages from {} documents.", ix["chunks"], ix["docs"])
            };
            let state = match st["local"]["state"].as_str().unwrap_or_default() {
                "ready" => "ready",
                "loading" => "starting",
                "stopped" => "starts when you ask",
                "too_big" => "does not fit this computer's memory",
                "error" => "could not start",
                "absent" => "not downloaded",
                "wrong" => "the model file cannot be read",
                _ => "checking",
            };
            let ai = st["local"]["name"].as_str().map(|n| format!("Local AI: {n} — {state}.")).unwrap_or_else(|| "No local AI.".into());
            // What search and the assistant cannot read is said, not hidden.
            let cov = &st["coverage"];
            let (silent, scanned) = (cov["videos"].as_i64().unwrap_or(0) - cov["videos_with_text"].as_i64().unwrap_or(0), cov["pdfs_image_only"].as_i64().unwrap_or(0));
            let unread = match (silent > 0, scanned > 0) {
                (true, true) => format!(" Not readable: videos without captions ({silent}), scanned PDFs without text ({scanned})."),
                (true, false) => format!(" Not readable: videos without captions ({silent})."),
                (false, true) => format!(" Not readable: scanned PDFs without text ({scanned})."),
                (false, false) => String::new(),
            };
            me.about.append(&text_card("Search and the assistant", &glib::markup_escape_text(&format!("{search} {ai}{unread}")), ""));
            me.about.append(&text_card(
                "Privacy",
                &glib::markup_escape_text("Lastbook sends nothing anywhere. Only setup downloads, and only when you start it."),
                "",
            ));
            me.about.append(&text_card(
                "Credits",
                &glib::markup_escape_text("Based on Lastbook (lastbook.org), MIT. The look follows Strata (github.com/lgse/strata), MIT; the icons are Lucide's, ISC. Encyclopedias by Kiwix and their contributors. Map data © OpenStreetMap contributors (ODbL), tiles from Protomaps; place names from GeoNames (CC BY 4.0). Each document keeps its own licence: see Sources and permissions in the library."),
                &format!("Lastbook for Omarchy {}", app_version(&me.app)),
            ));
            let m = card("Library and downloads", "", "", None);
            activate(&m, me.app.clone(), Route::Manage);
            me.about.append(&m);
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
