// SPDX-License-Identifier: MIT
//! Guides: short curated paths for urgent needs and learning tracks (content/topics, served resolved by /api/topics).
//! A guide shows its situations ("What's happening?"), each with the pages to read, the limits its sources set (when to
//! stop or get help), what to prepare, what to learn, and related guides.

use std::{
    cell::RefCell,
    rc::{Rc, Weak},
};

use gtk::{glib, prelude::*};
use omui::{clear, label};
use serde_json::Value;

use crate::{
    cards::{self, activate, card, clock, column, route_of, section},
    ui::{App, Route},
};

pub struct Guides {
    pub list_root: gtk::Widget,
    pub page_root: gtk::Widget,
    app: Weak<App>,
    list: gtk::Box,
    page: gtk::Box,
    page_scroll: gtk::ScrolledWindow,
    topics: RefCell<Vec<Value>>,
    /// A guide asked for before the guides arrived from the engine, drawn when they do.
    waiting: RefCell<Option<String>>,
}

impl Guides {
    pub fn build(app: &Rc<App>) -> Rc<Self> {
        let (list_scroll, list) = column(12);
        let (page_scroll, page) = column(12);
        let g = Rc::new(Self {
            list_root: list_scroll.upcast(),
            page_root: page_scroll.clone().upcast(),
            app: Rc::downgrade(app),
            list,
            page,
            page_scroll,
            topics: RefCell::new(Vec::new()),
            waiting: RefCell::new(None),
        });
        let engine = app.engine.clone();
        let me = Rc::downgrade(&g);
        glib::spawn_future_local(async move {
            let topics = engine.get("/api/topics").await.ok().and_then(|v| v.as_array().cloned()).unwrap_or_default();
            if let Some(me) = me.upgrade() {
                me.topics.replace(topics);
                me.fill_list();
                if let Some(id) = me.waiting.take()
                    && let Some(title) = me.show(&id)
                    && let Some(app) = me.app.upgrade()
                {
                    app.report_title("guide", &title);
                }
            }
        });
        g
    }

    fn fill_list(&self) {
        clear(&self.list);
        let t = gtk::Label::new(Some("Guides"));
        t.add_css_class("page-title");
        t.set_xalign(0.0);
        self.list.append(&t);
        for (mode, heading) in [("now", "Right now"), ("learn", "Learn and prepare")] {
            let topics: Vec<Value> = self.topics.borrow().iter().filter(|t| t["mode"].as_str().unwrap_or("now") == mode).cloned().collect();
            if topics.is_empty() {
                continue;
            }
            self.list.append(&section(heading));
            let grid = cards::grid(3, 8);
            for t in topics {
                let c = card(t["title"].as_str().unwrap_or_default(), &glib::markup_escape_text(t["short"].as_str().unwrap_or_default()), "", None);
                c.add_css_class("guide-card");
                activate(&c, self.app.clone(), Route::Guide(t["id"].as_str().unwrap_or_default().to_owned()));
                grid.insert(&c, -1);
            }
            self.list.append(&grid);
        }
    }

    pub fn show(&self, id: &str) -> Option<String> {
        clear(&self.page);
        if self.topics.borrow().is_empty() {
            self.waiting.replace(Some(id.to_owned()));
            return None;
        }
        let t = self.topics.borrow().iter().find(|t| t["id"] == id).cloned()?;
        let title = t["title"].as_str().unwrap_or_default().to_owned();
        self.page.append(&label(&title, "page-title"));
        self.page.append(&label(t["intro"].as_str().unwrap_or_default(), "dim-label"));

        if let Some(choices) = t["choices"].as_array().filter(|c| !c.is_empty()) {
            self.page.append(&section("What's happening?"));
            for (i, c) in choices.iter().enumerate() {
                let exp = gtk::Expander::new(None);
                exp.add_css_class("choice");
                let head = gtk::Box::new(gtk::Orientation::Vertical, 2);
                head.append(&label(c["label"].as_str().unwrap_or_default(), "result-title"));
                if let Some(hint) = c["hint"].as_str() {
                    head.append(&label(hint, "meta"));
                }
                exp.set_label_widget(Some(&head));
                let items = gtk::Box::new(gtk::Orientation::Vertical, 6);
                items.set_margin_top(8);
                for r in c["read"].as_array().into_iter().flatten() {
                    items.append(&self.item(r));
                }
                // A choice that belongs to another guide leads there.
                if let Some(to) = c["goto"].as_str().map(|g| g.split('/').next().unwrap_or(g).to_owned())
                    && let Some(name) = self.topics.borrow().iter().find(|t| t["id"] == to.as_str()).and_then(|t| t["title"].as_str().map(str::to_owned))
                {
                    let go = crate::cards::wrap_button(&format!("Open the guide: {name}"));
                    go.add_css_class("chip");
                    go.set_halign(gtk::Align::Start);
                    let app = self.app.clone();
                    go.connect_clicked(move |_| {
                        if let Some(app) = app.upgrade() {
                            app.navigate(Route::Guide(to.clone()));
                        }
                    });
                    items.append(&go);
                }
                exp.set_child(Some(&items));
                exp.set_expanded(i == 0 && choices.len() == 1);
                self.page.append(&exp);
            }
        }
        if let Some(limits) = t["limits"].as_array().filter(|l| !l.is_empty()) {
            self.page.append(&section("When to stop and get help"));
            let b = gtk::Box::new(gtk::Orientation::Vertical, 6);
            b.add_css_class("limits");
            for l in limits {
                let row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
                let icon = omui::icon(omui::icons::name::WARNING);
                icon.add_css_class("error");
                icon.set_valign(gtk::Align::Start);
                let text = gtk::Label::new(l["text"].as_str());
                text.set_wrap(true);
                text.set_xalign(0.0);
                text.set_hexpand(true);
                row.append(&icon);
                row.append(&text);
                if let Some(src) = l["src"].as_object() {
                    let s = gtk::Label::new(Some(&format!("{}{}", src.get("publisher").and_then(Value::as_str).unwrap_or_default(), l["page"].as_i64().map(|p| format!(", p. {p}")).unwrap_or_default())));
                    s.add_css_class("meta");
                    s.set_valign(gtk::Align::Start);
                    s.set_wrap(true);
                    s.set_max_width_chars(28);
                    row.append(&s);
                    if let Some(r) = route_of(&merge(l)) {
                        activate(&row, self.app.clone(), r);
                    }
                }
                b.append(&row);
            }
            self.page.append(&b);
        }
        for (key, heading) in [("prepare", "Get ready before it happens"), ("learn", "Learn more")] {
            if let Some(items) = t[key].as_array().filter(|i| !i.is_empty()) {
                self.page.append(&section(heading));
                for r in items {
                    self.page.append(&self.item(r));
                }
            }
        }
        if let Some(route) = t["route"].as_array().filter(|r| !r.is_empty()) {
            self.page.append(&section("Step by step"));
            for step in route {
                let l = gtk::Label::new(step["title"].as_str());
                l.add_css_class("result-title");
                l.set_xalign(0.0);
                self.page.append(&l);
                if let Some(goal) = step["goal"].as_str() {
                    self.page.append(&label(goal, "dim-label"));
                }
                for r in step["read"].as_array().into_iter().flatten() {
                    self.page.append(&self.item(r));
                }
            }
        }
        if let Some(related) = t["related"].as_array().filter(|r| !r.is_empty()) {
            self.page.append(&section("Related guides"));
            let row = gtk::FlowBox::new();
            row.set_selection_mode(gtk::SelectionMode::None);
            for r in related.iter().filter_map(Value::as_str) {
                let name = self.topics.borrow().iter().find(|t| t["id"] == r).and_then(|t| t["title"].as_str().map(str::to_owned));
                let Some(name) = name else { continue };
                let b = crate::cards::wrap_button(&name);
                b.add_css_class("chip");
                let (app, id) = (self.app.clone(), r.to_owned());
                b.connect_clicked(move |_| {
                    if let Some(app) = app.upgrade() {
                        app.navigate(Route::Guide(id.clone()));
                    }
                });
                row.insert(&b, -1);
            }
            self.page.append(&row);
        }
        // What the curators looked for and did not find, and where the sources disagree: said, not hidden.
        let gaps = |key: &str| t["gaps"][key].as_array().cloned().unwrap_or_default();
        let (missing, disagree) = (gaps("missing"), gaps("disagree"));
        if !missing.is_empty() || !disagree.is_empty() {
            let exp = gtk::Expander::new(Some("What this guide does not cover"));
            exp.add_css_class("choice");
            let b = gtk::Box::new(gtk::Orientation::Vertical, 6);
            b.set_margin_top(8);
            for (heading, lines) in [("", missing), ("Where the sources disagree", disagree)] {
                if !heading.is_empty() && !lines.is_empty() {
                    b.append(&label(heading, "result-title"));
                }
                for l in lines.iter().filter_map(Value::as_str) {
                    let text = gtk::Label::new(Some(l));
                    text.set_wrap(true);
                    text.set_xalign(0.0);
                    text.add_css_class("dim-label");
                    b.append(&text);
                }
            }
            exp.set_child(Some(&b));
            self.page.append(&exp);
        }
        self.page_scroll.vadjustment().set_value(0.0);
        Some(title)
    }

    fn item(&self, r: &Value) -> gtk::Widget {
        let src = &r["src"];
        let mut meta = Vec::new();
        if let Some(p) = src["publisher"].as_str() {
            meta.push(p.to_owned());
        }
        if let Some(y) = src["year"].as_str() {
            meta.push(y.to_owned());
        }
        if let Some(p) = r["page"].as_i64() {
            meta.push(format!("page {p}"));
        } else if let Some(d) = src["duration"].as_f64() {
            meta.push(format!("video, {}", clock(d)));
        } else if r["zim"].is_string() {
            meta.push("Encyclopedia article".into());
        }
        let title = r["label"].as_str().or_else(|| r["title"].as_str()).unwrap_or_default();
        let c = card(title, &glib::markup_escape_text(r["note"].as_str().unwrap_or_default()), &meta.join(" · "), src["trust"].as_str());
        if let Some(route) = route_of(&merge(r)) {
            activate(&c, self.app.clone(), route);
        }
        c.upcast()
    }
}

/// A guide item names its document (`doc`, `page`, `zim`) beside the catalog record (`src`): one record for route_of.
fn merge(r: &Value) -> Value {
    let mut v = r["src"].clone();
    if !v.is_object() {
        v = serde_json::json!({});
    }
    if let Some(p) = r.get("page") {
        v["page"] = p.clone();
    }
    if let Some(z) = r.get("zim") {
        v["zim"] = z.clone();
        v["kind"] = "".into();
    }
    if let Some(d) = r.get("doc") {
        v["id"] = d.clone();
    }
    v
}
