// SPDX-License-Identifier: MIT
//! Pieces every screen shares: the result card, section headings, and where a source opens.

use std::rc::{Rc, Weak};

use gtk::{gio, glib, prelude::*};
use omui::{label, tr};
use serde_json::Value;

use crate::ui::Route;

/// `f` on `me`, for a signal or a timer: `me` is held weakly, and once it is gone this does nothing.
pub fn weak<T: 'static>(me: &Rc<T>, f: impl Fn(&Rc<T>) + 'static) -> impl Fn() + 'static {
    let me: Weak<T> = Rc::downgrade(me);
    move || {
        if let Some(me) = me.upgrade() {
            f(&me);
        }
    }
}

/// Runs `f` on `me` when `b` is clicked, holding `me` weakly (`weak`).
pub fn on_click<T: 'static>(b: &impl IsA<gtk::Button>, me: &Rc<T>, f: impl Fn(&Rc<T>) + 'static) {
    let f = weak(me, f);
    b.connect_clicked(move |_| f());
}

/// What a screen shows in place of what it could not load: `what` went wrong in a sentence (translated by the
/// caller), `why` in the engine's own words under it, and a button that runs `again` on `me`. A screen that asked and
/// got nothing must never stay blank. A screen draws it anew after a language switch, as it draws all it shows.
pub fn problem<T: 'static>(what: &str, why: &str, me: &Rc<T>, again: impl Fn(&Rc<T>) + 'static) -> gtk::Widget {
    let state = omui::EmptyState::new(Some(omui::icons::name::WARNING), what, Some(&omui::i18n::translate(why))).compact();
    let retry = gtk::Button::with_label(&tr("Try again"));
    retry.add_css_class("chip");
    on_click(&retry, me, again);
    state.set_child(Some(retry.upcast_ref()));
    state.widget.upcast()
}

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
    text.append(&label(title, "result-title"));
    if !snippet_markup.is_empty() {
        let s = label("", "snippet");
        s.set_markup(snippet_markup);
        s.set_lines(4);
        s.set_ellipsize(gtk::pango::EllipsizeMode::End);
        text.append(&s);
    }
    if !meta.is_empty() {
        text.append(&label(meta, "meta"));
    }
    if let Some(trust) = trust.filter(|t| !t.is_empty()) {
        let l = gtk::Label::new(Some(trust));
        l.add_css_class("trust");
        l.set_xalign(0.0);
        text.append(&l);
    }
    card.append(&text);
    // A card takes the keyboard (`activate`), so a screen reader names it: what it is and where from, as two cards
    // can show pages of one book.
    let name = if meta.is_empty() { title.to_owned() } else { format!("{title}, {meta}") };
    card.update_property(&[gtk::accessible::Property::Label(&name)]);
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
    // A click is the card's own; on a single click the grid would activate the cell too and open the page twice.
    g.set_activate_on_single_click(false);
    // A cell is activated whole, by a screen reader or by Enter on the cell itself: its card opens what it shows.
    g.connect_child_activated(|_, cell| {
        if let Some(card) = cell.child() {
            let _ = card.activate_action("card.open", None);
        }
    });
    g
}

/// An encyclopedia's name, from its ZIM file's name.
pub fn encyclopedia_name(file: &str) -> String {
    if file.starts_with("mdwiki") {
        tr("Medical encyclopedia (MDWiki)")
    } else if file.starts_with("appropedia") {
        "Appropedia".to_owned()
    } else {
        file.to_owned()
    }
}

/// What kind of document a catalog record is, in a word or two: the engine's `kind`, named here so that it is in the
/// person's language (the engine's own `kindLabel` is English).
pub fn kind_label(kind: &str) -> String {
    match kind {
        "pdf" => "PDF".to_owned(),
        "html" => tr("Saved web page"),
        "video" => tr("Video"),
        "transcript" => tr("Transcript"),
        "pptx" => tr("Slides"),
        "zim" => tr("Encyclopedia"),
        "metadata" => tr("Video details"),
        other => other.to_owned(),
    }
}

/// A button whose label wraps, with the style class `class`: a long one must not set the window's minimum width (a
/// half-screen tile is ~840 px).
pub fn wrap_button(text: &str, class: &str) -> gtk::Button {
    let l = label(text, "");
    l.set_wrap_mode(gtk::pango::WrapMode::WordChar);
    let b = gtk::Button::new();
    b.set_child(Some(&l));
    b.add_css_class(class);
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
    let l = label(text, "section-title");
    l.set_margin_top(10);
    l
}

/// Draws the first `first` of `items` with `draw`, and a "Show all" button for the rest when there are more.
pub fn some_then_all<T: Clone + 'static>(into: &gtk::Box, items: &[T], first: usize, draw: impl Fn(&[T]) + 'static) {
    draw(&items[..items.len().min(first)]);
    if items.len() > first {
        let more = gtk::Button::with_label(&omui::trn!("Show all {n}", items.len()));
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
/// Accepts the engine's source records (retrieve) and catalog records (find, topics' `src`).
pub fn route_of(s: &Value) -> Option<Route> {
    let kind = s["kind"].as_str().unwrap_or_default();
    let id = s["doc_id"].as_str().or_else(|| s["id"].as_str()).unwrap_or_default().to_owned();
    match kind {
        "place" => Some(Route::MapAt {
            lat: s["lat"].as_f64()?,
            lon: s["lon"].as_f64()?,
            label: s["title"].as_str().or_else(|| s["name"].as_str()).map(str::to_owned).unwrap_or_else(|| tr("Place")),
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
        meta.push(s["encyclopedia"].as_str().map(str::to_owned).unwrap_or_else(|| tr("Encyclopedia")));
    } else if let Some(p) = s["publisher"].as_str().filter(|p| !p.is_empty()) {
        meta.push(p.to_owned());
    }
    if let Some(y) = s["year"].as_str() {
        meta.push(y.to_owned());
    }
    if let Some(p) = s["page"].as_i64() {
        meta.push(match s["pages"].as_i64() {
            Some(n) => tr!("page {} of {}", omui::i18n::format_int(p), omui::i18n::format_int(n)),
            None => tr!("page {}", omui::i18n::format_int(p)),
        });
    } else if let Some(t) = s["t"].as_f64() {
        meta.push(tr!("at {}", clock(t)));
    } else if let Some(sec) = s["section"].as_str().filter(|s| !s.is_empty()) {
        meta.push(sec.to_owned());
    }
    meta.join(" · ")
}

/// Makes `w` open `target` on click, Enter or Space, and as its action `card.open` (what a grid's cell activates). No
/// target: `w` stays as it is, opening nothing.
pub fn activate(w: &impl IsA<gtk::Widget>, app: Weak<crate::ui::App>, target: Option<Route>) {
    let Some(target) = target else { return };
    let w = w.as_ref();
    w.set_cursor_from_name(Some("pointer"));
    w.set_focusable(true);
    let open = gio::SimpleAction::new("open", None);
    open.connect_activate(move |_, _| {
        if let Some(app) = app.upgrade() {
            app.navigate(target.clone());
        }
    });
    let group = gio::SimpleActionGroup::new();
    group.add_action(&open);
    w.insert_action_group("card", Some(&group));
    let click = gtk::GestureClick::new();
    click.connect_released(|g, _, _, _| {
        if let Some(w) = g.widget() {
            let _ = w.activate_action("card.open", None);
        }
    });
    w.add_controller(click);
    let keys = gtk::EventControllerKey::new();
    keys.connect_key_pressed(|k, key, _, _| {
        if matches!(key, gtk::gdk::Key::Return | gtk::gdk::Key::KP_Enter | gtk::gdk::Key::space) {
            if let Some(w) = k.widget() {
                let _ = w.activate_action("card.open", None);
            }
            return glib::Propagation::Stop;
        }
        glib::Propagation::Proceed
    });
    w.add_controller(keys);
}
