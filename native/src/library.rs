// SPDX-License-Identifier: MIT
//! The browsing screens: Library (shelves, then a shelf's collections and documents), Saved (what the person starred)
//! and About (what this library holds, and credits).

use std::{
    cell::Cell,
    path::PathBuf,
    rc::{Rc, Weak},
};

use gtk::{glib, prelude::*};
use omui::{clear, label, tr};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::{
    cards::{self, activate, card, column, route_of, section, some_then_all, text_card},
    engine::{load_json, store_json},
    ui::{App, Route},
};

fn doc_card(app: &Weak<App>, d: &Value) -> gtk::Widget {
    let mut meta = vec![cards::kind_label(d["kind"].as_str().unwrap_or_default()), d["publisher"].as_str().unwrap_or_default().to_owned()];
    if let Some(y) = d["year"].as_str() {
        meta.push(y.to_owned());
    }
    if let Some(p) = d["pages"].as_i64() {
        meta.push(omui::trn!("{n} pages", p));
    }
    let c = card(d["title"].as_str().unwrap_or_default(), "", &meta.into_iter().filter(|m| !m.is_empty()).collect::<Vec<_>>().join(" · "), d["trust"].as_str());
    activate(&c, app.clone(), route_of(d));
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

/// Writes the saved items; the error, if it could not, is for the window to show.
pub fn store_saved(items: &[Saved]) -> std::io::Result<()> {
    store_json(&saved_path(), items)
}

/// Puts back an item un-saved by mistake (the toast's Undo), unless it has been saved again since.
pub fn put_back(items: &mut Vec<Saved>, item: Saved) {
    if !items.iter().any(|s| s.key == item.key) {
        items.push(item);
    }
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
    /// The shelves did not load: what the Library shows is the reason, and the next visit asks again.
    library_failed: Cell<bool>,
}

impl Browse {
    pub fn build(app: &Weak<App>) -> Rc<Self> {
        let (ls, library) = column(12);
        let (ss, shelf) = column(12);
        let (sv, saved) = column(12);
        let (ab, about) = column(12);
        Rc::new(Self {
            library_root: ls.upcast(),
            shelf_root: ss.upcast(),
            saved_root: sv.upcast(),
            about_root: ab.upcast(),
            app: app.clone(),
            library,
            shelf,
            saved,
            about,
            library_failed: Cell::new(false),
        })
    }

    /// After a language switch: every screen here is drawn again when it is next shown (the one on show at once, by
    /// ui.rs).
    pub fn retranslate(&self) {
        clear(&self.library);
    }

    pub fn show_library(self: &Rc<Self>) {
        if self.library.first_child().is_some() && !self.library_failed.replace(false) {
            return;
        }
        clear(&self.library);
        self.library.append(&label(&tr("Library"), "page-title"));
        let Some(app) = self.app.upgrade() else { return };
        let (engine, me) = (app.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let shelves = engine.get("/api/shelves").await;
            let Some(me) = me.upgrade() else { return };
            let shelves = match shelves {
                Ok(shelves) => shelves,
                Err(e) => {
                    me.library_failed.set(true);
                    me.library.append(&cards::problem(&tr("Lastbook could not read the library's shelves."), &e.to_string(), &me, |me| me.show_library()));
                    return;
                }
            };
            let grid = cards::grid(3, 8);
            for s in shelves.as_array().into_iter().flatten() {
                let (read, video) = (s["read"].as_u64().unwrap_or(0), s["video"].as_u64().unwrap_or(0));
                if read + video == 0 {
                    continue;   // nothing on this shelf in this library
                }
                let counts = [(read > 0).then(|| omui::trn!("{n} to read", read)), (video > 0).then(|| omui::trn!("{n} videos", video))]
                    .into_iter()
                    .flatten()
                    .collect::<Vec<_>>()
                    .join(" · ");
                let c = card(s["title"].as_str().unwrap_or_default(), "", &counts, None);
                c.add_css_class("guide-card");
                activate(&c, me.app.clone(), Some(Route::Shelf(s["id"].as_str().unwrap_or_default().to_owned())));
                grid.insert(&c, -1);
            }
            me.library.append(&grid);
        });
    }

    pub fn show_shelf(self: &Rc<Self>, id: &str, name: &str) {
        clear(&self.shelf);
        self.shelf.append(&label(name, "page-title"));
        let Some(app) = self.app.upgrade() else { return };
        let (engine, me, id, shown) = (app.engine.clone(), Rc::downgrade(self), id.to_owned(), name.to_owned());
        glib::spawn_future_local(async move {
            // The shelf's answer has no title: the list of shelves does.
            let name = engine.get("/api/shelves").await.ok().and_then(|v| {
                v.as_array()?.iter().find(|s| s["id"] == id.as_str()).and_then(|s| s["title"].as_str().map(str::to_owned))
            });
            let shelf = engine.get(&format!("/api/shelf/{}", urlencoding::encode(&id))).await;
            let Some(me) = me.upgrade() else { return };
            let s = match shelf {
                Ok(s) => s,
                Err(e) => {
                    let id = id.clone();
                    me.shelf.append(&cards::problem(&tr("Lastbook could not read this shelf."), &e.to_string(), &me, move |me| me.show_shelf(&id, &shown)));
                    return;
                }
            };
            if let Some(name) = name {
                clear(&me.shelf);
                me.shelf.append(&label(&name, "page-title"));
                if let Some(app) = me.app.upgrade() {
                    app.report_title("shelf", &name);
                }
            }
            for col in s["collections"].as_array().into_iter().flatten() {
                let count = omui::i18n::format_int(col["count"].as_i64().unwrap_or(0));
                me.shelf.append(&section(&tr!("{} ({})", col["collection"].as_str().unwrap_or_default(), count)));
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
        self.saved.append(&label(&tr("Saved"), "page-title"));
        let items = load_saved();
        if items.is_empty() {
            self.saved.append(&label(&tr("Nothing saved yet. Star a page, video or guide to keep it here (Ctrl+D)."), "status-line"));
        }
        let mut items = items;
        items.sort_by_key(|s| std::cmp::Reverse(s.at));
        for s in items {
            let c = card(&s.title, "", &crate::ui::saved_sub(&s.key, &s.sub), None);
            let route = Route::from_key(&s.key).map(|mut r| {
                if let Route::Web { meta, .. } = &mut r {
                    *meta = s.sub.clone();
                }
                r
            });
            activate(&c, self.app.clone(), route);
            self.saved.append(&c);
        }
    }

    pub fn show_about(self: &Rc<Self>) {
        clear(&self.about);
        self.about.append(&label(&tr("About this library"), "page-title"));
        let Some(app) = self.app.upgrade() else { return };
        let (engine, me) = (app.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let lib = engine.get("/api/library").await.unwrap_or_default();
            let Some(me) = me.upgrade() else { return };
            clear(&me.about);   // opened twice before the answers came: one set of cards
            me.about.append(&label(&tr("About this library"), "page-title"));
            let (n, gb) = (|k: &str| lib[k].as_i64().unwrap_or(0), lib["document_bytes"].as_f64().unwrap_or(0.0) / 1e9);
            // One sentence a count each would glue fragments, which no language but English can take: each count is
            // a line of its own.
            let holds = [
                omui::trn!("{n} PDF manuals", n("pdfs")),
                omui::trn!("{n} pages in the manuals", n("pdf_pages")),
                omui::trn!("{n} saved web pages", n("web_pages")),
                omui::trn!("{n} videos", n("videos")),
                omui::trn!("{n} complete encyclopedias", n("collections")),
                tr!("{} GB of documents", omui::i18n::format_number(gb, 1)),
            ];
            me.about.append(&text_card(&tr("What's in it"), &glib::markup_escape_text(&holds.join("\n")), ""));
            let ix = &lib["index"];
            let search = if ix["state"] == "waiting for Recall" {
                tr("Meaning search is off: Recall for Omarchy is not running.")
            } else {
                omui::trnf(
                    "Meaning search (through Recall for Omarchy): {n} passages from {} documents.",
                    ix["chunks"].as_i64().unwrap_or(0),
                    &[&omui::i18n::format_int(ix["docs"].as_i64().unwrap_or(0))],
                )
            };
            me.about.append(&text_card(&tr("Search"), &glib::markup_escape_text(&search), ""));
            me.about.append(&text_card(
                &tr("Privacy"),
                &glib::markup_escape_text(&tr("Lastbook sends nothing anywhere. Only setup downloads, and only when you start it.")),
                "",
            ));
            me.about.append(&text_card(
                &tr("Credits"),
                &glib::markup_escape_text(&tr("Based on Lastbook (lastbook.org), MIT. The look follows Strata (github.com/lgse/strata), MIT; the icons are Lucide's, ISC. Encyclopedias by Kiwix and their contributors. Map data © OpenStreetMap contributors (ODbL), tiles from Protomaps; place names from GeoNames (CC BY 4.0). Each document keeps its own licence: see Sources and permissions in the library.")),
                &tr!("Lastbook for Omarchy {}", app_version(&me.app)),
            ));
            let m = card(&tr("Library and downloads"), "", "", None);
            activate(&m, me.app.clone(), Some(Route::Section("manage")));
            me.about.append(&m);
            if let Some(src) = lib["sources"].as_str() {
                let c = card(&tr("Sources and permissions"), "", &tr("Where every document came from, and its licence"), None);
                activate(&c, me.app.clone(), Some(Route::Read { doc: src.to_owned(), page: 1 }));
                me.about.append(&c);
            }
        });
    }
}

fn app_version(app: &Weak<App>) -> String {
    app.upgrade().and_then(|a| a.engine.ready.version.clone()).unwrap_or_else(|| env!("CARGO_PKG_VERSION").to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn saved(key: &str, at: i64) -> Saved {
        Saved { key: key.into(), title: "A page".into(), sub: "Page 3".into(), at }
    }

    /// The app id became `lastbook` (main.rs); what a person keeps is under the folder named Lastbook, never under
    /// the app id, so the rename leaves their saved items (and the engine's settings, app/main.py) where they were.
    #[test]
    fn saved_items_stay_where_they_were_across_the_rename() {
        assert!(saved_path().ends_with("Lastbook/saved.json"), "{}", saved_path().display());
        assert!(!saved_path().to_string_lossy().contains("io.github"));
    }

    #[test]
    fn undo_puts_an_unsaved_item_back_as_it_was() {
        let mut items = vec![saved("read:a:1", 10), saved("guide:water", 20)];
        let gone = items.remove(0);
        put_back(&mut items, gone.clone());
        assert!(items.contains(&gone), "back with the time it was first saved at");
        assert_eq!(items.len(), 2);
    }

    #[test]
    fn undo_does_not_save_twice_what_was_saved_again_meanwhile() {
        let mut items = vec![saved("read:a:1", 30)];
        put_back(&mut items, saved("read:a:1", 10));
        assert_eq!(items, [saved("read:a:1", 30)]);
    }
}
