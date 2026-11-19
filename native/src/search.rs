// SPDX-License-Identifier: MIT
//! Search: the library's most relevant passages (keyword and meaning search fused, encyclopedias included: /api/retrieve)
//! above the matching documents (/api/find). PDF pages show as thumbnails drawn with Poppler.

use std::{
    cell::{Cell, RefCell},
    collections::{HashMap, VecDeque},
    path::PathBuf,
    rc::{Rc, Weak},
};

use gtk::{glib, prelude::*};
use omui::clear;
use serde_json::Value;

use crate::{
    cards::{self, activate, card, mark_markup, plain_markup, route_of, section, source_meta},
    engine::query,
    pdf,
    ui::{App, Route},
};

const KINDS: &[(&str, &str)] = &[("any", "Everything"), ("manual", "Manuals & pages"), ("video", "Videos"), ("encyclopedia", "Encyclopedias")];
const THUMB_W: i32 = 64;
const THUMB_H: i32 = 84;   // the box a page is fitted into: 64 wide for a letter or A4 page

pub struct SearchPage {
    pub root: gtk::Widget,
    app: Weak<App>,
    entry: gtk::SearchEntry,
    chips: Vec<(String, gtk::ToggleButton)>,
    status: gtk::Label,
    results: gtk::Box,
    shown: RefCell<(String, String)>,
    generation: Cell<u64>,
    docs: RefCell<HashMap<String, Value>>,
    thumbs: RefCell<VecDeque<(String, i32, gtk::Image)>>,
    thumbing: Cell<bool>,
}

impl SearchPage {
    pub fn build(app: &Rc<App>) -> Rc<Self> {
        let (scroll, column) = cards::column(14);
        let title = gtk::Label::new(Some("Search the library"));
        title.add_css_class("page-title");
        title.set_xalign(0.0);
        let entry = gtk::SearchEntry::new();
        entry.add_css_class("search-large");
        entry.set_placeholder_text(Some("What do you need? A word, a question, a model number…"));
        entry.set_hexpand(true);
        let chips_box = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        let mut chips = Vec::new();
        for (kind, label) in KINDS {
            let b = gtk::ToggleButton::with_label(label);
            b.add_css_class("chip");
            chips_box.append(&b);
            chips.push((kind.to_string(), b));
        }
        let status = gtk::Label::new(None);
        status.add_css_class("meta");
        status.set_xalign(0.0);
        let results = gtk::Box::new(gtk::Orientation::Vertical, 8);
        column.append(&title);
        column.append(&entry);
        column.append(&chips_box);
        column.append(&status);
        column.append(&results);

        let page = Rc::new(Self {
            root: scroll.upcast(),
            app: Rc::downgrade(app),
            entry,
            chips,
            status,
            results,
            shown: RefCell::new((String::from("\u{0}"), String::new())),
            generation: Cell::new(0),
            docs: RefCell::new(HashMap::new()),
            thumbs: RefCell::new(VecDeque::new()),
            thumbing: Cell::new(false),
        });
        let p = Rc::downgrade(&page);
        page.entry.connect_activate(move |e| {
            if let Some(p) = p.upgrade() {
                p.go(e.text().as_str(), &p.current().1);
            }
        });
        for (kind, chip) in &page.chips {
            let p = Rc::downgrade(&page);
            let kind = kind.clone();
            chip.connect_clicked(move |b| {
                if let Some(p) = p.upgrade() {
                    if !b.is_active() && p.current().1 == kind {
                        b.set_active(true);   // a chosen chip stays chosen; another replaces it
                        return;
                    }
                    p.go(p.entry.text().as_str(), &kind);
                }
            });
        }
        page
    }

    pub fn current(&self) -> (String, String) {
        let kind = self.chips.iter().find(|(_, b)| b.is_active()).map(|(k, _)| k.clone()).unwrap_or_else(|| "any".into());
        (self.entry.text().to_string(), kind)
    }

    pub fn focus(&self) {
        self.entry.grab_focus();
    }

    fn go(&self, q: &str, kind: &str) {
        if let Some(app) = self.app.upgrade() {
            app.navigate(Route::Search { q: q.trim().to_owned(), kind: kind.to_owned() });
        }
    }

    /// Shows the results for (q, kind), unless they are already on screen.
    pub fn run(self: &Rc<Self>, q: &str, kind: &str) {
        for (k, chip) in &self.chips {
            chip.set_active(k == kind);
        }
        if self.entry.text() != q {
            self.entry.set_text(q);
        }
        if *self.shown.borrow() == (q.to_owned(), kind.to_owned()) {
            return;
        }
        self.shown.replace((q.to_owned(), kind.to_owned()));
        let generation = self.generation.get() + 1;
        self.generation.set(generation);
        clear(&self.results);
        self.thumbs.borrow_mut().clear();
        if q.is_empty() {
            self.status.set_text("");
            self.entry.grab_focus();
            return;
        }
        self.status.set_text("Searching…");
        let Some(app) = self.app.upgrade() else { return };
        let engine = app.engine.clone();
        let (q, kind) = (q.to_owned(), kind.to_owned());
        let page = Rc::downgrade(self);
        glib::spawn_future_local(async move {
            let find_kind = match kind.as_str() {
                "manual" => "read",
                "video" => "video",
                _ => "all",
            };
            // "Everything" asks for the library and the encyclopedias separately: the library answers at once, and the
            // articles (ranked by reading them, about a second for a new topic) are added under it when they arrive.
            let split = kind == "any";
            let articles = split.then(|| {
                let (e, p, page) = (engine.clone(), query("/api/retrieve", &[("q", &q), ("k", "3"), ("kind", "encyclopedia")]), page.clone());
                glib::spawn_future_local(async move {
                    let found = e.get(&p).await.ok();
                    (found, page)
                })
            });
            let (e1, e2) = (engine.clone(), engine.clone());
            let (p1, p2) = (
                query("/api/retrieve", &[("q", &q), ("k", "8"), ("kind", if split { "library" } else { &kind })]),
                query("/api/find", &[("q", &q), ("kind", find_kind)]),
            );
            let by_title = kind != "encyclopedia";   // articles have no place in the catalog's titles
            let (relevant, docs) = futures_join(async move { e1.get(&p1).await }, async move { if by_title { e2.get(&p2).await.ok() } else { None } }).await;
            let Some(page) = page.upgrade() else { return };
            if page.generation.get() != generation {
                return;   // a newer search replaced this one
            }
            if relevant.is_err() {
                // The engine did not answer: say so, and let the same words be searched again.
                page.shown.replace(Default::default());
                page.status.set_text("The library did not answer. Try again.");
                return;
            }
            let slot = page.show_results(relevant.ok(), docs);
            drop(page);
            if let Some(articles) = articles
                && let Ok((found, page)) = articles.await
                && let Some(page) = page.upgrade()
                && page.generation.get() == generation
            {
                page.show_articles(&slot, found);
            }
        });
    }

    /// Encyclopedia articles for an "Everything" search, put in the place kept for them under the library's passages.
    fn show_articles(self: &Rc<Self>, slot: &gtk::Box, found: Option<Value>) {
        let rel: Vec<Value> = found.as_ref().and_then(|v| v["results"].as_array().cloned()).unwrap_or_default();
        if rel.is_empty() {
            if self.results.first_child().is_some_and(|c| c == *slot) && slot.next_sibling().is_none() {
                self.status.set_text("Nothing in the library matched. Try other words for the same thing.");
            }
            return;
        }
        self.status.set_text("");
        slot.append(&section("From the encyclopedias"));
        for r in &rel {
            slot.append(&self.passage_card(r));
        }
    }

    /// Draws the results and returns the (empty) place kept for encyclopedia articles.
    fn show_results(self: &Rc<Self>, relevant: Option<Value>, docs: Option<Value>) -> gtk::Box {
        let rel: Vec<Value> = relevant.as_ref().and_then(|v| v["results"].as_array().cloned()).unwrap_or_default();
        let found: Vec<Value> = docs.as_ref().and_then(|v| v["results"].as_array().cloned()).unwrap_or_default();
        let waiting_for_articles = self.shown.borrow().1 == "any";
        self.status.set_text(if rel.is_empty() && found.is_empty() && !waiting_for_articles {
            "Nothing in the library matched. Try other words for the same thing."
        } else if rel.is_empty() && found.is_empty() {
            "Searching…"
        } else {
            ""
        });
        if !rel.is_empty() {
            self.results.append(&section("Most relevant pages"));
            for r in &rel {
                self.results.append(&self.passage_card(r));
            }
        }
        let slot = gtk::Box::new(gtk::Orientation::Vertical, 8);
        self.results.append(&slot);
        if !found.is_empty() {
            self.results.append(&section(&format!("Documents ({})", found.len())));
            for d in found.iter().take(30) {
                self.docs.borrow_mut().insert(d["id"].as_str().unwrap_or_default().to_owned(), d.clone());
                self.results.append(&self.document_card(d));
            }
        }
        self.pump_thumbs();
        slot
    }

    fn passage_card(self: &Rc<Self>, r: &Value) -> gtk::Widget {
        let doc = r["doc_id"].as_str().unwrap_or_default().to_owned();
        let kind = r["kind"].as_str().unwrap_or_default();
        let page = r["page"].as_i64().map(|p| p as i32);
        let title = r["title"].as_str().unwrap_or_default();
        let card = card(title, &plain_markup(r["excerpt"].as_str().unwrap_or_default(), 320), &source_meta(r), r["trust"].as_str());
        self.thumb_slot(&card, kind, &doc, page.unwrap_or(1));
        self.activate(&card, route_of(r));
        card.upcast()
    }

    fn document_card(self: &Rc<Self>, d: &Value) -> gtk::Widget {
        let id = d["id"].as_str().unwrap_or_default().to_owned();
        let kind = d["kind"].as_str().unwrap_or_default();
        let hits = d["hits"].as_array().cloned().unwrap_or_default();
        let first_page = hits.iter().find_map(|h| h["page"].as_i64()).unwrap_or(1) as i32;
        let snippet = hits.first().and_then(|h| h["snippet"].as_str()).map(mark_markup).unwrap_or_default();
        let mut meta = vec![d["kindLabel"].as_str().unwrap_or(kind).to_owned(), d["publisher"].as_str().unwrap_or_default().to_owned()];
        if let Some(y) = d["year"].as_str() {
            meta.push(y.to_owned());
        }
        let pages: Vec<String> = hits.iter().filter_map(|h| h["page"].as_i64()).map(|p| p.to_string()).collect();
        if !pages.is_empty() {
            meta.push(format!("pages {}", pages.join(", ")));
        }
        let card = card(d["title"].as_str().unwrap_or_default(), &snippet, &meta.join(" · "), d["trust"].as_str());
        if kind == "pdf" {
            self.queue_thumb(&card, d["path"].as_str().unwrap_or_default(), first_page);
        }
        let target = if kind == "pdf" { Some(Route::Read { doc: id, page: first_page }) } else { route_of(d) };
        self.activate(&card, target);
        card.upcast()
    }

    /// A passage only names its document: its file comes from /api/doc (once per document).
    fn thumb_slot(self: &Rc<Self>, card: &gtk::Box, kind: &str, doc: &str, page: i32) -> Option<()> {
        if kind != "pdf" {
            return None;
        }
        if let Some(path) = self.docs.borrow().get(doc).and_then(|d| d["path"].as_str().map(str::to_owned)) {
            self.queue_thumb(card, &path, page);
            return Some(());
        }
        let app = self.app.upgrade()?;
        let engine = app.engine.clone();
        let (card, doc, page_ref) = (card.clone(), doc.to_owned(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            if let Ok(d) = engine.get(&format!("/api/doc/{}", urlencoding::encode(&doc))).await
                && let Some(p) = page_ref.upgrade()
            {
                let path = d["path"].as_str().unwrap_or_default().to_owned();
                p.docs.borrow_mut().insert(doc, d);
                p.queue_thumb(&card, &path, page);
                p.pump_thumbs();
            }
        });
        Some(())
    }

    fn queue_thumb(&self, card: &gtk::Box, path: &str, page: i32) {
        // An image of fixed size: a picture would grow to the card's height and come out a different width each time.
        let pic = gtk::Image::new();
        pic.add_css_class("thumb");
        pic.set_pixel_size(THUMB_H);
        pic.set_valign(gtk::Align::Start);
        card.prepend(&pic);
        self.thumbs.borrow_mut().push_back((path.to_owned(), page, pic));
    }

    /// Draws queued thumbnails one per idle turn, so a long result list never holds up typing or scrolling.
    fn pump_thumbs(self: &Rc<Self>) {
        if self.thumbing.replace(true) {
            return;
        }
        let page = Rc::downgrade(self);
        glib::idle_add_local(move || {
            let Some(p) = page.upgrade() else { return glib::ControlFlow::Break };
            let Some((path, n, pic)) = p.thumbs.borrow_mut().pop_front() else {
                p.thumbing.set(false);
                return glib::ControlFlow::Break;
            };
            let Some(app) = p.app.upgrade() else { return glib::ControlFlow::Break };
            let file: PathBuf = app.engine.ready.library.join(&path);
            if let Some(doc) = pdf::document(&file)
                && let Some((tex, _)) = pdf::render(&doc, n - 1, THUMB_W * pic.scale_factor())
            {
                pic.set_paintable(Some(&tex));
            }
            glib::ControlFlow::Continue
        });
    }

    fn activate(&self, card: &gtk::Box, target: Option<Route>) {
        if let Some(target) = target {
            activate(card, self.app.clone(), target);
        }
    }
}

/// Both futures, run together on the main loop.
async fn futures_join<A: 'static, B: 'static>(a: impl std::future::Future<Output = A> + 'static, b: impl std::future::Future<Output = B> + 'static) -> (A, B) {
    let a = glib::spawn_future_local(a);
    let b = glib::spawn_future_local(b);
    (a.await.expect("task"), b.await.expect("task"))
}
