// SPDX-License-Identifier: MIT
//! Pieces every screen shares: the result card, section headings, and where a source opens.

use gtk::{glib, prelude::*};
use serde_json::Value;

use crate::ui::Route;

/// A card whose text is read whole (About's), not a snippet cut at four lines.
pub fn text_card(title: &str, markup: &str, meta: &str) -> gtk::Box {
    let c = card(title, markup, meta, None);
    let mut child = c.first_child().and_then(|t| t.first_child());
    while let Some(w) = child {
        if w.has_css_class("snippet")
            && let Some(l) = w.downcast_ref::<gtk::Label>()
        {
            l.set_lines(-1);
            l.set_ellipsize(gtk::pango::EllipsizeMode::None);
        }
        child = w.next_sibling();
    }
    c
}

pub fn card(title: &str, snippet_markup: &str, meta: &str, trust: Option<&str>) -> gtk::Box {
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
    if !meta.is_empty() {
        let m = gtk::Label::new(Some(meta));
        m.add_css_class("meta");
        m.set_xalign(0.0);
        m.set_wrap(true);
        text.append(&m);
    }
    if let Some(trust) = trust.filter(|t| !t.is_empty()) {
        let l = gtk::Label::new(Some(trust));
        l.add_css_class("trust");
        l.set_xalign(0.0);
        text.append(&l);
    }
    card.append(&text);
    card
}

/// A page's content: a padded column that scrolls. Returns (the scroller, the column).
pub fn column(spacing: i32) -> (gtk::ScrolledWindow, gtk::Box) {
    let b = gtk::Box::new(gtk::Orientation::Vertical, spacing);
    b.set_margin_top(28);
    b.set_margin_bottom(28);
    b.set_margin_start(32);
    b.set_margin_end(32);
    let s = gtk::ScrolledWindow::builder().child(&b).vexpand(true).hscrollbar_policy(gtk::PolicyType::Never).build();
    (s, b)
}

/// A grid of equal cards, two to `max` a row.
pub fn grid(max: u32, spacing: u32) -> gtk::FlowBox {
    let g = gtk::FlowBox::new();
    g.set_selection_mode(gtk::SelectionMode::None);
    g.set_homogeneous(true);
    g.set_min_children_per_line(2);
    g.set_max_children_per_line(max);
    g.set_row_spacing(spacing);
    g.set_column_spacing(spacing);
    g
}

/// An encyclopedia's name, from its ZIM file's name.
pub fn encyclopedia_name(file: &str) -> &str {
    if file.starts_with("mdwiki") {
        "Medical encyclopedia (MDWiki)"
    } else if file.starts_with("appropedia") {
        "Appropedia"
    } else {
        file
    }
}

/// A button whose label wraps: a long one must not set the window's minimum width (a half-screen tile is ~840 px).
pub fn wrap_button(text: &str) -> gtk::Button {
    let l = gtk::Label::new(Some(text));
    l.set_wrap(true);
    l.set_wrap_mode(gtk::pango::WrapMode::WordChar);
    l.set_xalign(0.0);
    let b = gtk::Button::new();
    b.set_child(Some(&l));
    b
}

/// Lets a check button's label wrap, for the same reason.
pub fn wrap_check(b: &gtk::CheckButton) {
    if let Some(l) = b.last_child().and_downcast::<gtk::Label>() {
        l.set_wrap(true);
        l.set_xalign(0.0);
    }
}

pub fn section(text: &str) -> gtk::Label {
    let l = gtk::Label::new(Some(text));
    l.add_css_class("section-title");
    l.set_xalign(0.0);
    l.set_margin_top(10);
    l
}

/// Draws the first `first` of `items` with `draw`, and a "Show all" button for the rest when there are more.
pub fn some_then_all<T: Clone + 'static>(into: &gtk::Box, items: &[T], first: usize, draw: impl Fn(&[T]) + 'static) {
    draw(&items[..items.len().min(first)]);
    if items.len() > first {
        let more = gtk::Button::with_label(&format!("Show all {}", items.len()));
        more.set_halign(gtk::Align::Start);
        into.append(&more);
        let rest = items[first..].to_vec();
        more.connect_clicked(move |b| {
            b.set_visible(false);
            draw(&rest);
        });
    }
}

/// Plain text as Pango markup, shortened.
pub fn plain_markup(s: &str, max: usize) -> String {
    let short: String = s.chars().take(max).collect();
    let more = if s.chars().count() > max { "…" } else { "" };
    glib::markup_escape_text(&(short + more)).to_string()
}

/// /api/find's snippets are HTML-escaped with <mark> around the matched words: bold in Pango markup.
pub fn mark_markup(s: &str) -> String {
    let m = s.replace("<mark>", "<b>").replace("</mark>", "</b>");
    if gtk::pango::parse_markup(&m, '\0').is_ok() { m } else { glib::markup_escape_text(s).to_string() }
}

pub fn clock(t: f64) -> String {
    let t = t.max(0.0) as i64;
    if t >= 3600 { format!("{}:{:02}:{:02}", t / 3600, t / 60 % 60, t % 60) } else { format!("{}:{:02}", t / 60, t % 60) }
}

/// Where a source opens: a PDF page, a saved page, a video moment, an encyclopedia article or a place on the map.
/// Accepts the engine's source records (chat, retrieve) and catalog records (find, topics' `src`).
pub fn route_of(s: &Value) -> Option<Route> {
    let kind = s["kind"].as_str().unwrap_or_default();
    let id = s["doc_id"].as_str().or_else(|| s["id"].as_str()).unwrap_or_default().to_owned();
    match kind {
        "place" => Some(Route::MapAt {
            lat: s["lat"].as_f64()?,
            lon: s["lon"].as_f64()?,
            label: s["title"].as_str().or_else(|| s["name"].as_str()).unwrap_or("Place").to_owned(),
        }),
        "video" => Some(Route::Watch { video: id, t: s["t"].as_f64().unwrap_or(-1.0) }),   // no moment given: no seek
        "article" => Some(Route::Wiki { path: s["wiki"].as_str()?.to_owned() }),
        "zim" => None,
        "" => s["zim"].as_str().map(|z| Route::Wiki { path: z.trim_start_matches('/').trim_start_matches("zim/").to_owned() }),
        _ if !id.is_empty() => Some(Route::Read { doc: id, page: s["page"].as_i64().unwrap_or(1) as i32 }),
        _ => None,
    }
}

/// "Publisher · year · page 3 of 40" or "at 1:20" or the article's section.
pub fn source_meta(s: &Value) -> String {
    let mut meta = Vec::new();
    if s["kind"] == "article" {
        meta.push(s["encyclopedia"].as_str().unwrap_or("Encyclopedia").to_owned());
    } else if let Some(p) = s["publisher"].as_str().filter(|p| !p.is_empty()) {
        meta.push(p.to_owned());
    }
    if let Some(y) = s["year"].as_str() {
        meta.push(y.to_owned());
    }
    if let Some(p) = s["page"].as_i64() {
        meta.push(format!("page {p}{}", s["pages"].as_i64().map(|n| format!(" of {n}")).unwrap_or_default()));
    } else if let Some(t) = s["t"].as_f64() {
        meta.push(format!("at {}", clock(t)));
    } else if let Some(sec) = s["section"].as_str().filter(|s| !s.is_empty()) {
        meta.push(sec.to_owned());
    }
    meta.join(" · ")
}

/// Makes `w` open `target` on click, Enter or Space.
pub fn activate(w: &impl IsA<gtk::Widget>, app: std::rc::Weak<crate::ui::App>, target: Route) {
    let w = w.as_ref();
    w.set_cursor_from_name(Some("pointer"));
    w.set_focusable(true);
    let click = gtk::GestureClick::new();
    let (a, t) = (app.clone(), target.clone());
    click.connect_released(move |_, _, _, _| {
        if let Some(app) = a.upgrade() {
            app.navigate(t.clone());
        }
    });
    w.add_controller(click);
    let keys = gtk::EventControllerKey::new();
    keys.connect_key_pressed(move |_, key, _, _| {
        if matches!(key, gtk::gdk::Key::Return | gtk::gdk::Key::KP_Enter | gtk::gdk::Key::space) {
            if let Some(app) = app.upgrade() {
                app.navigate(target.clone());
            }
            return glib::Propagation::Stop;
        }
        glib::Propagation::Proceed
    });
    w.add_controller(keys);
}
