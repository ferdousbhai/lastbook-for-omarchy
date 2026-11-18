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
use serde_json::Value;

use crate::{
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
        let column = gtk::Box::new(gtk::Orientation::Vertical, 14);
        column.set_margin_top(28);
        column.set_margin_bottom(28);
        column.set_margin_start(32);
        column.set_margin_end(32);
        let clamp = gtk::Box::new(gtk::Orientation::Vertical, 0);
        clamp.set_halign(gtk::Align::Fill);
        clamp.append(&column);

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
        results.add_css_class("results");
        column.append(&title);
        column.append(&entry);
        column.append(&chips_box);
        column.append(&status);
        column.append(&results);
        let scroll = gtk::ScrolledWindow::builder().hscrollbar_policy(gtk::PolicyType::Never).child(&clamp).vexpand(true).build();

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
            self.status.set_text("Search manuals, health guides, video transcripts and the encyclopedias, all on this computer.");
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
            let (e1, e2) = (engine.clone(), engine.clone());
            let (p1, p2) = (query("/api/retrieve", &[("q", &q), ("k", "8"), ("kind", &kind)]), query("/api/find", &[("q", &q), ("kind", find_kind)]));
            let (relevant, docs) = futures_join(async move { e1.get(&p1).await }, async move { e2.get(&p2).await }).await;
            let Some(page) = page.upgrade() else { return };
            if page.generation.get() != generation {
                return;   // a newer search replaced this one
            }
            page.show_results(relevant.ok(), if kind == "encyclopedia" { None } else { docs.ok() });
        });
    }

    fn show_results(self: &Rc<Self>, relevant: Option<Value>, docs: Option<Value>) {
        let rel: Vec<Value> = relevant.as_ref().and_then(|v| v["results"].as_array().cloned()).unwrap_or_default();
        let found: Vec<Value> = docs.as_ref().and_then(|v| v["results"].as_array().cloned()).unwrap_or_default();
        let meaning = relevant.as_ref().is_some_and(|v| v["semantic"]["chunks"].as_u64().unwrap_or(0) > 0 && v["semantic"]["state"] != "waiting for Recall");
        self.status.set_text(&if rel.is_empty() && found.is_empty() {
            "Nothing in the library matched. Try other words for the same thing.".to_owned()
        } else if meaning {
            "Matched by meaning as well as by words.".to_owned()
        } else {
            "Matched by your words (meaning-based search needs Recall for Omarchy).".to_owned()
        });
        if !rel.is_empty() {
            self.results.append(&section("Most relevant pages"));
            for r in &rel {
                self.results.append(&self.passage_card(r));
            }
        }
        if !found.is_empty() {
            self.results.append(&section(&format!("Documents ({})", found.len())));
            for d in found.iter().take(30) {
                self.docs.borrow_mut().insert(d["id"].as_str().unwrap_or_default().to_owned(), d.clone());
                self.results.append(&self.document_card(d));
            }
        }
        self.pump_thumbs();
    }

    fn passage_card(self: &Rc<Self>, r: &Value) -> gtk::Widget {
        let doc = r["doc_id"].as_str().unwrap_or_default().to_owned();
        let kind = r["kind"].as_str().unwrap_or_default();
        let page = r["page"].as_i64().map(|p| p as i32);
        let mut meta = vec![r["publisher"].as_str().unwrap_or_default().to_owned()];
        if let Some(y) = r["year"].as_str() {
            meta.push(y.to_owned());
        }
        match (kind, page, r["t"].as_f64(), r["section"].as_str()) {
            (_, Some(p), _, _) => meta.push(format!("page {p}{}", r["pages"].as_i64().map(|n| format!(" of {n}")).unwrap_or_default())),
            (_, _, Some(t), _) => meta.push(format!("at {}:{:02}", (t as i64) / 60, (t as i64) % 60)),
            ("article", _, _, Some(s)) if !s.is_empty() => meta.push(s.to_owned()),
            _ => {}
        }
        let title = r["title"].as_str().unwrap_or_default();
        let card = card(title, &plain_markup(r["excerpt"].as_str().unwrap_or_default(), 320), &meta.join(" · "), r["trust"].as_str());
        let thumb = self.thumb_slot(&card, kind, &doc, page.unwrap_or(1));
        let _ = thumb;
        let target = (kind == "pdf").then(|| Route::Read { doc: doc.clone(), page: page.unwrap_or(1) });
        self.activate(&card, target);
        card.upcast()
    }

    fn document_card(self: &Rc<Self>, d: &Value) -> gtk::Widget {
        let id = d["id"].as_str().unwrap_or_default().to_owned();
        let kind = d["kind"].as_str().unwrap_or_default();
        let hits = d["hits"].as_array().cloned().unwrap_or_default();
        let first_page = hits.iter().find_map(|h| h["page"].as_i64()).unwrap_or(1) as i32;
        let snippet = hits.first().and_then(|h| h["snippet"].as_str()).map(|s| mark_markup(s)).unwrap_or_default();
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
        self.activate(&card, (kind == "pdf").then(|| Route::Read { doc: id, page: first_page }));
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
        let Some(target) = target else { return };
        card.set_cursor_from_name(Some("pointer"));
        card.set_focusable(true);
        let click = gtk::GestureClick::new();
        let app = self.app.clone();
        let t = target.clone();
        click.connect_released(move |_, _, _, _| {
            if let Some(app) = app.upgrade() {
                app.navigate(t.clone());
            }
        });
        card.add_controller(click);
        let keys = gtk::EventControllerKey::new();
        let app = self.app.clone();
        keys.connect_key_pressed(move |_, key, _, _| {
            if matches!(key, gtk::gdk::Key::Return | gtk::gdk::Key::KP_Enter | gtk::gdk::Key::space) {
                if let Some(app) = app.upgrade() {
                    app.navigate(target.clone());
                }
                return glib::Propagation::Stop;
            }
            glib::Propagation::Proceed
        });
        card.add_controller(keys);
    }
}

fn card(title: &str, snippet_markup: &str, meta: &str, trust: Option<&str>) -> gtk::Box {
    let card = gtk::Box::new(gtk::Orientation::Horizontal, 12);
    card.add_css_class("result");
    let text = gtk::Box::new(gtk::Orientation::Vertical, 4);
    text.set_hexpand(true);
    let t = gtk::Label::new(Some(title));
    t.add_css_class("result-title");
    t.set_xalign(0.0);
    t.set_wrap(true);
    text.append(&t);
    if !snippet_markup.is_empty() {
        let s = gtk::Label::new(None);
        s.set_markup(snippet_markup);
        s.add_css_class("snippet");
        s.set_xalign(0.0);
        s.set_wrap(true);
        s.set_lines(4);
        s.set_ellipsize(gtk::pango::EllipsizeMode::End);
        text.append(&s);
    }
    let m = gtk::Label::new(Some(meta));
    m.add_css_class("meta");
    m.set_xalign(0.0);
    m.set_wrap(true);
    text.append(&m);
    if let Some(trust) = trust.filter(|t| !t.is_empty()) {
        let l = gtk::Label::new(Some(trust));
        l.add_css_class("trust");
        l.set_xalign(0.0);
        text.append(&l);
    }
    card.append(&text);
    card
}

fn section(text: &str) -> gtk::Label {
    let l = gtk::Label::new(Some(text));
    l.add_css_class("section-title");
    l.set_xalign(0.0);
    l.set_margin_top(10);
    l
}

fn clear(b: &gtk::Box) {
    while let Some(c) = b.first_child() {
        b.remove(&c);
    }
}

/// Plain text as Pango markup, shortened.
fn plain_markup(s: &str, max: usize) -> String {
    let short: String = s.chars().take(max).collect();
    let more = if s.chars().count() > max { "…" } else { "" };
    glib::markup_escape_text(&(short + more)).to_string()
}

/// /api/find's snippets are HTML-escaped with <mark> around the matched words: bold in Pango markup.
fn mark_markup(s: &str) -> String {
    let m = s.replace("<mark>", "<b>").replace("</mark>", "</b>");
    if gtk::pango::parse_markup(&m, '\0').is_ok() { m } else { glib::markup_escape_text(s).to_string() }
}

/// Both futures, run together on the main loop.
async fn futures_join<A: 'static, B: 'static>(a: impl std::future::Future<Output = A> + 'static, b: impl std::future::Future<Output = B> + 'static) -> (A, B) {
    let a = glib::spawn_future_local(a);
    let b = glib::spawn_future_local(b);
    (a.await.expect("task"), b.await.expect("task"))
}
