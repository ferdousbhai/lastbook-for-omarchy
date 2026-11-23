// SPDX-License-Identifier: MIT
//! The PDF reader: one page at a time, drawn by Poppler at the window's width (and the screen's scale, so text stays
//! sharp), with page and zoom controls.
//!
//! Keys (`act_for`): Left and Right turn pages, Home and End go to the first and the last; Up and Down (and k, j)
//! scroll within the page, which is taller than the window; Space and Page Down scroll a screenful and turn the page
//! at its foot, Shift+Space, Backspace and Page Up do the same upwards; + and - zoom and 0 fits the width.
//!
//! Pages are drawn off the main thread (pdf::draw_later), since one can take seconds, and the last ones drawn are
//! kept (`Kept`), so going back to a page, or to a width or zoom already seen, draws nothing.
//!
//! Find in the document (`find`, the bar's button) looks through the text the library extracted from it (/api/text, a
//! string a page), so it finds what a scanned page holds too, and turns to the page of each match in turn; Poppler
//! marks the matches on the page on show where the PDF has the text itself. The bar also copies the page's text,
//! and lists other documents on the same subject (`Related`, which the web view's bar has too).

use std::{
    cell::{Cell, RefCell},
    collections::VecDeque,
    path::{Component, Path, PathBuf},
    rc::{Rc, Weak},
    time::Duration,
};

use gtk::{gdk, glib, prelude::*};
use omui::{icon_button, icons::name, tr};
use serde_json::Value;

use crate::{
    cards::{route_of, source_meta},
    pdf,
    ui::App,
};

/// What a key does in the reader.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Act {
    /// To the page after (1) or before (-1), at its top.
    Turn(i32),
    First,
    Last,
    /// A little way down (1) or up (-1) the page.
    Line(i32),
    /// A screenful down (1) or up (-1), and on to the next page from the foot of this one (back from its top).
    Screen(i32),
    ZoomIn,
    ZoomOut,
    Fit,
}

/// What `key` does with `mods` held, if it is the reader's. Keys held with Alt or Super are the window's (Alt+Left is
/// Back); with Ctrl only the zoom keys are taken, as browsers have them, and Ctrl+arrows stay the scrolled window's.
fn act_for(key: gdk::Key, mods: gdk::ModifierType) -> Option<Act> {
    use gdk::{Key, ModifierType as M};
    if mods.intersects(M::ALT_MASK | M::SUPER_MASK | M::META_MASK) {
        return None;
    }
    let zoom = match key {
        Key::plus | Key::equal | Key::KP_Add => Some(Act::ZoomIn),
        Key::minus | Key::KP_Subtract => Some(Act::ZoomOut),
        Key::_0 | Key::KP_0 => Some(Act::Fit),
        _ => None,
    };
    if zoom.is_some() || mods.contains(M::CONTROL_MASK) {
        return zoom;
    }
    Some(match key {
        Key::Right | Key::KP_Right => Act::Turn(1),
        Key::Left | Key::KP_Left => Act::Turn(-1),
        Key::Home | Key::KP_Home => Act::First,
        Key::End | Key::KP_End => Act::Last,
        Key::Down | Key::KP_Down | Key::j => Act::Line(1),
        Key::Up | Key::KP_Up | Key::k => Act::Line(-1),
        Key::space if mods.contains(M::SHIFT_MASK) => Act::Screen(-1),
        Key::space | Key::Page_Down | Key::KP_Page_Down => Act::Screen(1),
        Key::BackSpace | Key::Page_Up | Key::KP_Page_Up => Act::Screen(-1),
        _ => return None,
    })
}

/// A line's worth of scrolling, and the share of the screen a screenful moves by (the rest is kept in view, to read on from).
const LINE: f64 = 60.0;
const SCREENFUL: f64 = 0.9;

/// Where scrolling `by` pixels from `value` comes to, in content `upper` high seen through `page`; None when that
/// is nowhere new, at the top or the foot already.
fn scrolled(value: f64, upper: f64, page: f64, by: f64) -> Option<f64> {
    let to = (value + by).clamp(0.0, (upper - page).max(0.0));
    ((to - value).abs() >= 1.0).then_some(to)
}

/// The file a catalog record names, inside the library: a record is not trusted to name a file anywhere else.
pub(crate) fn library_file(library: &Path, path: &str) -> Option<PathBuf> {
    let rel = Path::new(path);
    (!path.is_empty() && rel.components().all(|c| matches!(c, Component::Normal(_)))).then(|| library.join(rel))
}

/// Text as find compares it: in lower case, every run of white space one space, so a phrase broken across lines in
/// the extracted text is still found.
fn folded(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut space = false;
    for c in s.chars() {
        if c.is_whitespace() {
            space = true;
            continue;
        }
        if space && !out.is_empty() {
            out.push(' ');
        }
        space = false;
        out.extend(c.to_lowercase());
    }
    out
}

/// The page (from 1) of each match of `needle` (folded) in the folded pages, in order.
fn matches(pages: &[String], needle: &str) -> Vec<i32> {
    if needle.is_empty() {
        return Vec::new();
    }
    pages.iter().zip(1..).flat_map(|(text, n)| std::iter::repeat_n(n, text.matches(needle).count())).collect()
}

/// The match to show first: the first on the page on show or after it, else the first of all.
fn first_from(hits: &[i32], page: i32) -> usize {
    hits.iter().position(|&p| p >= page).unwrap_or(0)
}

/// A match's place on the page in points, its top left corner and its bottom right.
type Rect = (f64, f64, f64, f64);

/// A document's extracted text, as it is copied and as find reads it.
struct Text {
    id: String,
    pages: Vec<String>,
    folded: Vec<String>,
}

/// The readers' Related: a button in the bar listing up to five other documents on the subject of the one on show
/// (/api/related), each opened from the list. It is hidden until the engine has named some.
pub(crate) struct Related {
    pub button: gtk::MenuButton,
    list: gtk::Box,
    app: Weak<App>,
    /// What the list is asked for: an answer for a document the person has left is not shown.
    asked: RefCell<String>,
}

impl Related {
    pub fn new(app: &Rc<App>) -> Rc<Self> {
        let list = gtk::Box::new(gtk::Orientation::Vertical, 2);
        list.add_css_class("related");
        let popover = gtk::Popover::new();
        popover.set_child(Some(&list));
        let button = gtk::MenuButton::builder().popover(&popover).build();
        omui::i18n::bind(&button, |b| b.set_label(&tr("Related")));
        omui::i18n::bind_tooltip(&button, "Other documents on the same subject");
        button.add_css_class("flat");
        button.set_visible(false);
        Rc::new(Self { button, list, app: Rc::downgrade(app), asked: RefCell::new(String::new()) })
    }

    /// Lists what is related to the document the query names (`doc=<id>`, or `path=<address>` for a page of the web
    /// view).
    pub fn show_for(self: &Rc<Self>, by: &str, value: &str) {
        let asked = crate::engine::query("/api/related", &[(by, value)]);
        if *self.asked.borrow() == asked {
            return;
        }
        self.asked.replace(asked.clone());
        self.button.set_visible(false);
        omui::clear(&self.list);
        let Some(app) = self.app.upgrade() else { return };
        let (engine, me) = (app.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let found = engine.get(&asked).await;
            let Some(me) = me.upgrade() else { return };
            if *me.asked.borrow() != asked {
                return;
            }
            let found = found.ok().and_then(|v| v["results"].as_array().cloned()).unwrap_or_default();
            for r in &found {
                me.add(r);
            }
            me.button.set_visible(me.list.first_child().is_some());
        });
    }

    fn add(&self, r: &Value) {
        let Some(route) = route_of(r) else { return };
        let title = gtk::Label::new(r["title"].as_str());
        title.set_xalign(0.0);
        title.set_wrap(true);
        title.set_max_width_chars(44);
        let meta = gtk::Label::new(Some(&source_meta(r)));
        meta.add_css_class("meta");
        meta.set_xalign(0.0);
        meta.set_wrap(true);
        meta.set_max_width_chars(44);
        let both = gtk::Box::new(gtk::Orientation::Vertical, 2);
        both.append(&title);
        both.append(&meta);
        let row = gtk::Button::builder().child(&both).build();
        row.add_css_class("flat");
        let (app, popover) = (self.app.clone(), self.button.popover());
        row.connect_clicked(move |_| {
            if let Some(p) = &popover {
                p.popdown();
            }
            if let Some(app) = app.upgrade() {
                app.navigate(route.clone());
            }
        });
        self.list.append(&row);
    }
}

/// The find bar of a reading screen: the field, which match is on show, the way to the one before and after.
pub(crate) struct FindBar {
    pub bar: gtk::SearchBar,
    pub entry: gtk::SearchEntry,
    pub found: gtk::Label,
    pub prev: gtk::Button,
    pub next: gtk::Button,
}

impl FindBar {
    /// `placeholder` in English, translated where it is shown.
    pub fn new(placeholder: &'static str) -> Self {
        let entry = gtk::SearchEntry::new();
        entry.set_hexpand(true);
        omui::i18n::bind(&entry, move |e| e.set_placeholder_text(Some(&omui::i18n::translate(placeholder))));
        let found = gtk::Label::new(None);
        found.add_css_class("meta");
        let prev = icon_button(name::UP, "");
        omui::i18n::bind_tooltip(&prev, "Previous match (Shift+Enter)");
        let next = icon_button(name::DOWN, "");
        omui::i18n::bind_tooltip(&next, "Next match (Enter)");
        let row = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        row.add_css_class("find-bar");
        for w in [entry.upcast_ref::<gtk::Widget>(), found.upcast_ref(), prev.upcast_ref(), next.upcast_ref()] {
            row.append(w);
        }
        let bar = gtk::SearchBar::builder().child(&row).show_close_button(true).build();
        bar.connect_entry(&entry);
        Self { bar, entry, found, prev, next }
    }

    /// Opens the bar with the keyboard in its field, what was looked for last chosen, so typing replaces it.
    pub fn open(&self) {
        self.bar.set_search_mode(true);
        self.entry.grab_focus();
        self.entry.select_region(0, -1);
    }

    /// Calls `step` with 1 for the next match (Enter, Ctrl+G, the button) and -1 for the one before (Shift+Enter,
    /// Ctrl+Shift+G, the button).
    pub fn on_step(&self, step: impl Fn(i32) + Clone + 'static) {
        let s = step.clone();
        self.next.connect_clicked(move |_| s(1));
        let s = step.clone();
        self.prev.connect_clicked(move |_| s(-1));
        let s = step.clone();
        self.entry.connect_next_match(move |_| s(1));
        let s = step.clone();
        self.entry.connect_previous_match(move |_| s(-1));
        // Before the field's own text, which takes Enter as activating it.
        let keys = gtk::EventControllerKey::new();
        keys.set_propagation_phase(gtk::PropagationPhase::Capture);
        keys.connect_key_pressed(move |_, key, _, mods| {
            if matches!(key, gdk::Key::Return | gdk::Key::KP_Enter) {
                step(if mods.contains(gdk::ModifierType::SHIFT_MASK) { -1 } else { 1 });
                return glib::Propagation::Stop;
            }
            glib::Propagation::Proceed
        });
        self.entry.add_controller(keys);
    }

    /// Says which match is on show: `at` from 0, of `of`; nothing for an empty field.
    pub fn say(&self, at: usize, of: usize) {
        let q = self.entry.text();
        self.found.set_text(&if q.trim().is_empty() {
            String::new()
        } else if of == 0 {
            tr("No matches")
        } else {
            tr!("Match {} of {}", omui::i18n::format_int(at as i64 + 1), omui::i18n::format_int(of as i64))
        });
        self.prev.set_sensitive(of > 1);
        self.next.set_sensitive(of > 1);
    }
}

/// A drawn page is known by its file, its number (from 1) and its width in device pixels.
type PageKey = (PathBuf, i32, i32);

/// The pages drawn last, newest first, within a budget of bytes: turning back, or to a zoom or window width already
/// seen, shows a page at once. The newest is kept whatever its size.
struct Kept<T> {
    pages: VecDeque<(PageKey, T, usize)>,
    budget: usize,
}

/// A page at reading width is 5 MB at a screen scale of 1 and 20 MB at 2; at full zoom up to 90 MB.
const KEPT_BYTES: usize = 192 * 1024 * 1024;

impl<T: Clone> Kept<T> {
    fn new(budget: usize) -> Self {
        Self { pages: VecDeque::new(), budget }
    }

    fn get(&mut self, key: &PageKey) -> Option<T> {
        let at = self.pages.iter().position(|(k, ..)| k == key)?;
        let page = self.pages.remove(at)?;
        let found = page.1.clone();
        self.pages.push_front(page);
        Some(found)
    }

    /// The same page at any width: to show, stretched, while the right one is drawn.
    fn any_width(&self, file: &Path, page: i32) -> Option<T> {
        self.pages.iter().find(|((f, p, _), ..)| f == file && *p == page).map(|(_, t, _)| t.clone())
    }

    fn put(&mut self, key: PageKey, page: T, bytes: usize) {
        self.pages.retain(|(k, ..)| *k != key);
        self.pages.push_front((key, page, bytes));
        while self.pages.len() > 1 && self.pages.iter().map(|p| p.2).sum::<usize>() > self.budget {
            self.pages.pop_back();
        }
    }
}

/// The document on show.
struct Open {
    id: String,
    doc: poppler::Document,
    file: PathBuf,
}

pub struct Reader {
    pub root: gtk::Widget,
    app: Weak<App>,
    picture: gtk::Picture,
    scroll: gtk::ScrolledWindow,
    meta: gtk::Label,
    entry: gtk::Entry,
    total: gtk::Label,
    prev: gtk::Button,
    next: gtk::Button,
    note: gtk::Label,
    doc: RefCell<Option<Open>>,
    page: Cell<i32>,
    zoom: Cell<f64>,
    last_width: Cell<i32>,
    pending: RefCell<Option<glib::SourceId>>,
    kept: RefCell<Kept<gdk::MemoryTexture>>,
    find: FindBar,
    related: Rc<Related>,
    /// The document's text, fetched the first time it is found in or copied from.
    text: RefCell<Option<Rc<Text>>>,
    /// The page of each match of what is looked for, and which of them is on show.
    hits: RefCell<Vec<i32>>,
    at: Cell<usize>,
    /// Over the page: where Poppler finds the matches on it, in points from its top left, and which is on show.
    marks: gtk::DrawingArea,
    marked: RefCell<(Vec<Rect>, Option<usize>)>,
    /// The page the picture waits for, while it is being drawn. A page that arrives is shown only if it is this one,
    /// so pages that finish out of order never replace the one on show.
    wanted: RefCell<Option<PageKey>>,
    /// The pages asked of the drawing thread and not answered yet: none is asked for twice.
    asked: RefCell<Vec<PageKey>>,
}

impl Reader {
    pub fn build(app: &Rc<App>) -> Rc<Self> {
        let bar = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        bar.add_css_class("reader-bar");
        // The header bar names the document; this bar says whose it is.
        let meta = gtk::Label::new(None);
        meta.add_css_class("meta");
        meta.set_xalign(0.0);
        meta.set_hexpand(true);
        meta.set_ellipsize(gtk::pango::EllipsizeMode::End);
        let prev = icon_button(name::BACK, "");
        omui::i18n::bind_tooltip(&prev, "Previous page (Left, Page Up)");
        let entry = gtk::Entry::new();
        entry.add_css_class("page-entry");
        entry.set_width_chars(4);
        entry.set_max_width_chars(5);
        omui::i18n::bind_tooltip(&entry, "Page number");
        // A number reads from the end of the field it is typed in, whichever way the language reads.
        gtk::prelude::EditableExt::set_alignment(&entry, 1.0);
        let total = gtk::Label::new(None);
        total.add_css_class("meta");
        let next = icon_button(name::FORWARD, "");
        omui::i18n::bind_tooltip(&next, "Next page (Right, Page Down)");
        let out = icon_button(name::ZOOM_OUT, "");
        omui::i18n::bind_tooltip(&out, "Smaller (-)");
        let fit = icon_button(name::FIT, "");
        omui::i18n::bind_tooltip(&fit, "Fit the width (0)");
        let inn = icon_button(name::ZOOM_IN, "");
        omui::i18n::bind_tooltip(&inn, "Larger (+)");
        let related = Related::new(app);
        let find_button = icon_button(name::SEARCH, "");
        omui::i18n::bind_tooltip(&find_button, "Find in this document (Ctrl+F)");
        let copy = icon_button(name::COPY, "");
        omui::i18n::bind_tooltip(&copy, "Copy this page's text");
        bar.append(&meta);
        for w in [
            related.button.upcast_ref::<gtk::Widget>(),
            find_button.upcast_ref(),
            copy.upcast_ref(),
            prev.upcast_ref(),
            entry.upcast_ref(),
            total.upcast_ref(),
            next.upcast_ref(),
            out.upcast_ref(),
            fit.upcast_ref(),
            inn.upcast_ref(),
        ] {
            bar.append(w);
        }
        let find = FindBar::new(n_!("Find in this document"));

        let picture = gtk::Picture::new();
        picture.add_css_class("paper");
        picture.set_can_shrink(true);   // drawn at screen pixels, shown at the size it asks for
        picture.set_content_fit(gtk::ContentFit::Contain);
        // The page takes the keyboard (`open`, `set_page_buttons`): the keys below turn it from there.
        picture.set_focusable(true);
        // The matches found are marked over the page, which is always the white paper it was printed on: the marks
        // keep one colour in every theme.
        let marks = gtk::DrawingArea::new();
        marks.set_can_target(false);
        let sheet = gtk::Overlay::builder().child(&picture).halign(gtk::Align::Center).valign(gtk::Align::Start).build();
        sheet.add_overlay(&marks);
        sheet.set_margin_top(18);
        sheet.set_margin_bottom(18);
        let note = gtk::Label::new(None);
        note.add_css_class("status-line");
        note.set_wrap(true);
        note.set_visible(false);
        let canvas = gtk::Box::new(gtk::Orientation::Vertical, 0);
        canvas.add_css_class("reader-canvas");
        canvas.append(&note);
        canvas.append(&sheet);
        // The page is taller than the window and takes the keyboard: a viewport that scrolled to show what has the
        // keyboard would open each page at its foot.
        let view = gtk::Viewport::builder().child(&canvas).scroll_to_focus(false).build();
        let scroll = gtk::ScrolledWindow::builder().child(&view).vexpand(true).hexpand(true).build();
        let root = gtk::Box::new(gtk::Orientation::Vertical, 0);
        root.append(&bar);
        root.append(&find.bar);
        root.append(&scroll);

        let reader = Rc::new(Self {
            root: root.upcast(),
            app: Rc::downgrade(app),
            picture,
            scroll,
            meta,
            entry,
            total,
            prev,
            next,
            note,
            doc: RefCell::new(None),
            page: Cell::new(1),
            zoom: Cell::new(1.0),
            last_width: Cell::new(0),
            pending: RefCell::new(None),
            kept: RefCell::new(Kept::new(KEPT_BYTES)),
            wanted: RefCell::new(None),
            asked: RefCell::new(Vec::new()),
            find,
            related,
            text: RefCell::new(None),
            hits: RefCell::new(Vec::new()),
            at: Cell::new(0),
            marks,
            marked: RefCell::new((Vec::new(), None)),
        });
        let r = Rc::downgrade(&reader);
        find_button.connect_clicked(move |_| if let Some(r) = r.upgrade() { r.find(); });
        let r = Rc::downgrade(&reader);
        copy.connect_clicked(move |_| if let Some(r) = r.upgrade() { r.copy_page() });
        let r = Rc::downgrade(&reader);
        reader.find.entry.connect_search_changed(move |_| if let Some(r) = r.upgrade() { r.look() });
        let r = Rc::downgrade(&reader);
        reader.find.on_step(move |way| if let Some(r) = r.upgrade() { r.step(way) });
        // Closed (Escape, its button): the marks go, and the keyboard is the page's again.
        let r = Rc::downgrade(&reader);
        reader.find.bar.connect_search_mode_enabled_notify(move |bar| {
            if let Some(r) = r.upgrade()
                && !bar.is_search_mode()
            {
                r.mark(false);
                r.picture.grab_focus();
            }
        });
        let r = Rc::downgrade(&reader);
        reader.marks.set_draw_func(move |area, cr, width, _| {
            if let Some(r) = r.upgrade() {
                r.draw_marks(area, cr, width);
            }
        });
        let r = Rc::downgrade(&reader);
        reader.prev.connect_clicked(move |_| if let Some(r) = r.upgrade() { r.turn(-1) });
        let r = Rc::downgrade(&reader);
        reader.next.connect_clicked(move |_| if let Some(r) = r.upgrade() { r.turn(1) });
        let r = Rc::downgrade(&reader);
        out.connect_clicked(move |_| if let Some(r) = r.upgrade() { r.set_zoom(r.zoom.get() / 1.2) });
        let r = Rc::downgrade(&reader);
        inn.connect_clicked(move |_| if let Some(r) = r.upgrade() { r.set_zoom(r.zoom.get() * 1.2) });
        let r = Rc::downgrade(&reader);
        fit.connect_clicked(move |_| if let Some(r) = r.upgrade() { r.set_zoom(1.0) });
        let r = Rc::downgrade(&reader);
        reader.entry.connect_activate(move |e| {
            if let (Some(r), Ok(n)) = (r.upgrade(), e.text().trim().parse::<i32>()) {
                r.go_to(n);
            }
        });
        // On the page as well as the screen: the page sits in the scrolled window, whose own Page Up, Page Down, Home
        // and End would answer first, and the window's arrows would move the keyboard off the page.
        reader.root.add_controller(reader.keys());
        reader.picture.add_controller(reader.keys());
        // Redraw at the new width once the window stops being resized.
        let r = Rc::downgrade(&reader);
        reader.scroll.hadjustment().connect_page_size_notify(move |_| {
            if let Some(r) = r.upgrade() {
                r.schedule_render();
            }
        });
        reader
    }

    /// The reader's keys (`act_for`), wherever the keyboard is in the reader but the page field.
    fn keys(self: &Rc<Self>) -> gtk::EventControllerKey {
        let keys = gtk::EventControllerKey::new();
        let r = Rc::downgrade(self);
        keys.connect_key_pressed(move |_, key, _, mods| {
            let Some(r) = r.upgrade() else { return glib::Propagation::Proceed };
            // In the page field, or any other text field, the reader's single keys are characters typed.
            if omui::text_has_focus(&r.root) {
                return glib::Propagation::Proceed;
            }
            let Some(act) = act_for(key, mods) else { return glib::Propagation::Proceed };
            r.act(act);
            glib::Propagation::Stop
        });
        keys
    }

    fn act(self: &Rc<Self>, act: Act) {
        match act {
            Act::Turn(step) => self.turn(step),
            Act::First => self.go_to(1),
            Act::Last => self.go_to(i32::MAX),
            Act::Line(way) => {
                self.scroll_by(f64::from(way) * LINE);
            }
            Act::Screen(way) => {
                let adj = self.scroll.vadjustment();
                if !self.scroll_by(f64::from(way) * adj.page_size() * SCREENFUL) {
                    // Reading on: down from the foot is the top of the next page, up from the top the foot of the one before.
                    let to = self.page.get() + way;
                    if (1..=self.pages()).contains(&to) {
                        self.show_page(to, way < 0);
                    }
                }
            }
            Act::ZoomIn => self.set_zoom(self.zoom.get() * 1.2),
            Act::ZoomOut => self.set_zoom(self.zoom.get() / 1.2),
            Act::Fit => self.set_zoom(1.0),
        }
    }

    /// Scrolls the page by `by` pixels; false if it is at that end already.
    fn scroll_by(&self, by: f64) -> bool {
        let adj = self.scroll.vadjustment();
        match scrolled(adj.value(), adj.upper(), adj.page_size(), by) {
            Some(to) => {
                adj.set_value(to);
                true
            }
            None => false,
        }
    }

    /// After a language switch: what the reader wrote around the page (its count, its name for a screen reader, a
    /// note, which match find shows) is written again as ui.rs shows the page again.
    pub fn retranslate(&self) {
        self.note.set_visible(false);
        self.find.say(self.at.get(), self.hits.borrow().len());
        let total = self.pages();
        if total > 0 {
            self.total.set_text(&tr!("of {}", omui::i18n::format_int(total.into())));
        }
    }

    pub fn open(self: &Rc<Self>, doc_id: &str, page: i32) {
        if self.doc.borrow().as_ref().is_some_and(|open| open.id == doc_id) {
            self.go_to(page);
            return;
        }
        self.doc.replace(None);
        self.text.replace(None);
        self.hits.borrow_mut().clear();
        self.find.bar.set_search_mode(false);
        self.forget_drawing();   // a page of the document before, still being drawn, is not this one's
        self.picture.set_paintable(gdk::Paintable::NONE);
        self.entry.set_text("");
        self.total.set_text("");
        self.set_page_buttons(false, false);
        self.meta.set_text("");
        self.note.set_visible(false);
        let Some(app) = self.app.upgrade() else { return };
        let engine = app.engine.clone();
        let (id, me) = (doc_id.to_owned(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let rec = engine.get(&format!("/api/doc/{}", urlencoding::encode(&id))).await;
            let Some(me) = me.upgrade() else { return };
            let Some(app) = me.app.upgrade() else { return };
            let rec = match rec {
                Ok(r) => r,
                Err(_) => {
                    app.report_title("reader", &tr("Document not found"));
                    return me.fail(&tr("This document is not in the library."));
                }
            };
            let title = rec["title"].as_str().unwrap_or_default().to_owned();
            if !app.showing("reader") {
                return;   // the person left before the answer came: opening it again asks again
            }
            app.report_title("reader", &title);
            let mut meta = vec![rec["publisher"].as_str().unwrap_or_default().to_owned()];
            if let Some(y) = rec["year"].as_str() {
                meta.push(y.to_owned());
            }
            if let Some(t) = rec["trust"].as_str() {
                meta.push(t.to_owned());
            }
            me.meta.set_text(&meta.join(" · "));
            match rec["kind"].as_str() {
                Some("pdf") => {}
                Some("video") => {
                    app.replace_route(crate::ui::Route::Watch { video: id.clone(), t: -1.0 }, true);
                    return;
                }
                _ => {
                    // Saved web pages and slides open in the web view, as the engine serves them.
                    let path = format!("/{}", rec["path"].as_str().unwrap_or_default());
                    app.replace_route(crate::ui::Route::Web { path, meta: meta.join(" · ") }, true);
                    return;
                }
            }
            let Some(file) = library_file(&app.engine.ready.library, rec["path"].as_str().unwrap_or_default()) else {
                return me.fail(&tr("The file could not be opened."));
            };
            let Some(doc) = pdf::document(&file) else {
                return me.fail(&tr("The file could not be opened."));
            };
            if doc.n_pages() == 0 {
                return me.fail(&tr("This document has no pages."));
            }
            me.total.set_text(&tr!("of {}", omui::i18n::format_int(doc.n_pages().into())));
            me.related.show_for("doc", &id);
            me.doc.replace(Some(Open { id, doc, file }));
            me.go_to(page);
            // Not left where the screen before handed it: in the page field the keys that turn pages are text keys.
            me.picture.grab_focus();
        });
    }

    fn fail(&self, why: &str) {
        self.forget_drawing();
        self.say(why);
        self.picture.set_paintable(gdk::Paintable::NONE);
        self.picture.set_size_request(-1, -1);
    }

    /// A line above the page: why there is none, or that it is on its way.
    fn say(&self, what: &str) {
        self.note.set_text(what);
        self.note.set_margin_top(60);
        self.note.set_visible(true);
    }

    /// Whatever is being drawn is no longer what the picture waits for.
    fn forget_drawing(&self) {
        self.wanted.replace(None);
    }

    fn waits_for(&self, key: &PageKey) -> bool {
        self.wanted.borrow().as_ref() == Some(key)
    }

    fn pages(&self) -> i32 {
        self.doc.borrow().as_ref().map(|open| open.doc.n_pages()).unwrap_or(0)
    }

    fn turn(self: &Rc<Self>, step: i32) {
        self.go_to(self.page.get() + step);
    }

    fn go_to(self: &Rc<Self>, n: i32) {
        self.show_page(n, false);
    }

    /// Shows page `n` (the nearest there is) from its top, or from its foot when it was come to by reading upwards.
    fn show_page(self: &Rc<Self>, n: i32, at_foot: bool) {
        let total = self.pages();
        if total == 0 {
            return;
        }
        let n = n.clamp(1, total);
        self.page.set(n);
        self.entry.set_text(&n.to_string());
        self.set_page_buttons(n > 1, n < total);
        self.picture.update_property(&[gtk::accessible::Property::Label(&tr!("Page {} of {}", omui::i18n::format_int(n.into()), omui::i18n::format_int(total.into())))]);
        // The page on show is the page that is saved, and the one Back returns to.
        let reading = self.doc.borrow().as_ref().map(|open| open.id.clone());
        if let (Some(app), Some(doc)) = (self.app.upgrade(), reading)
            && app.reading(&doc)
        {
            app.replace_route(crate::ui::Route::Read { doc, page: n }, false);
            app.update_star();
        }
        let height = self.render();
        let adj = self.scroll.vadjustment();
        if at_foot {
            // The scrolled window learns the new page's height only when it is next laid out, and would hold the
            // place to the old one's till then: it is told now (the page and its margins of 18 above and below).
            let upper = f64::from(height + 36).max(adj.page_size());
            adj.set_upper(upper);
            adj.set_value(upper - adj.page_size());
        } else {
            adj.set_value(0.0);
        }
        self.mark(false);
    }

    /// Whether there is a page before and a page after the one on show.
    fn set_page_buttons(&self, before: bool, after: bool) {
        // A button that greys under the keyboard would take the keyboard out of the reader with it.
        if !before && self.prev.has_focus() || !after && self.next.has_focus() {
            self.picture.grab_focus();
        }
        self.prev.set_sensitive(before);
        self.next.set_sensitive(after);
    }

    /// Opens the find bar, with the keyboard in it; false when there is no document to find in (ui.rs: Ctrl+F then
    /// goes to Search).
    pub fn find(self: &Rc<Self>) -> bool {
        if self.doc.borrow().is_none() {
            return false;
        }
        self.find.open();
        self.look();
        true
    }

    /// The document's text, fetched once, handed to `then`; nothing if the engine has none for it.
    fn with_text(self: &Rc<Self>, then: impl FnOnce(&Rc<Self>, &Text) + 'static) {
        let Some(id) = self.doc.borrow().as_ref().map(|open| open.id.clone()) else { return };
        let kept = self.text.borrow().clone().filter(|t| t.id == id);
        if let Some(text) = kept {
            return then(self, &text);
        }
        let Some(app) = self.app.upgrade() else { return };
        let (engine, me) = (app.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let answer = engine.get(&format!("/api/text/{}", urlencoding::encode(&id))).await;
            let Some(me) = me.upgrade() else { return };
            if me.doc.borrow().as_ref().map(|open| open.id.as_str()) != Some(id.as_str()) {
                return;   // another document since
            }
            let pages: Vec<String> = answer.as_ref().ok().and_then(|v| v["pages"].as_array().map(|p| p.iter().map(|t| t.as_str().unwrap_or_default().to_owned()).collect())).unwrap_or_default();
            let text = Rc::new(Text { id, folded: pages.iter().map(|p| folded(p)).collect(), pages });
            if answer.is_ok() {
                me.text.replace(Some(text.clone()));   // a failed answer is asked for again next time
            }
            then(&me, &text);
        });
    }

    /// Finds what the field holds, from the page on show on.
    fn look(self: &Rc<Self>) {
        let q = folded(&self.find.entry.text());
        self.with_text(move |me, text| {
            if folded(&me.find.entry.text()) != q {
                return;   // typed on since: that search is the one that counts
            }
            let hits = matches(&text.folded, &q);
            me.at.set(first_from(&hits, me.page.get()));
            let any = !hits.is_empty();
            me.hits.replace(hits);
            if any {
                me.go_to_match();
            } else {
                me.find.say(0, 0);
                me.mark(false);
            }
        });
    }

    /// To the match after (1) or before (-1) the one on show, round from the last to the first.
    fn step(self: &Rc<Self>, way: i32) {
        let n = self.hits.borrow().len();
        if n == 0 {
            return;
        }
        self.at.set((self.at.get() as i64 + i64::from(way)).rem_euclid(n as i64) as usize);
        self.go_to_match();
    }

    fn go_to_match(self: &Rc<Self>) {
        let (at, hits) = (self.at.get(), self.hits.borrow().clone());
        self.find.say(at, hits.len());
        if hits[at] != self.page.get() {
            self.go_to(hits[at]);
        }
        self.mark(true);
    }

    /// Marks the matches on the page on show, while the find bar is open; with `scroll`, scrolls to the one on show.
    fn mark(&self, scroll: bool) {
        let q = self.find.entry.text();
        let page = self.page.get();
        let rects = match self.doc.borrow().as_ref().and_then(|open| open.doc.page(page - 1)) {
            Some(p) if self.find.bar.is_search_mode() && !q.trim().is_empty() => {
                let (_, h) = p.size();
                // Poppler measures from the page's foot; the picture is drawn from its top.
                p.find_text(q.trim()).iter().map(|r| (r.x1(), h - r.y2(), r.x2(), h - r.y1())).collect()
            }
            _ => Vec::new(),
        };
        // The match on show, counted among this page's: the extracted text and Poppler's may count differently, so
        // it is marked only when Poppler found as many.
        let hits = self.hits.borrow();
        let on_page = hits.iter().filter(|&&p| p == page).count();
        let current = hits.get(self.at.get()).filter(|&&p| p == page).map(|_| self.at.get() - hits.iter().position(|&p| p == page).unwrap_or(0));
        let current = current.filter(|_| on_page == rects.len());
        if scroll
            && let Some(&(_, top, _, _)) = current.and_then(|i| rects.get(i)).or(rects.first())
            && let Some((w, _)) = self.doc.borrow().as_ref().and_then(|open| pdf::page_size(&open.doc, page - 1))
        {
            // A third of the way down the window, below the page's margin of 18.
            let (shown_w, shown_h) = self.picture.size_request();
            let adj = self.scroll.vadjustment();
            adj.set_upper(adj.upper().max(f64::from(shown_h + 36)));
            if let Some(to) = scrolled(0.0, adj.upper(), adj.page_size(), 18.0 + top * f64::from(shown_w) / w - adj.page_size() / 3.0) {
                adj.set_value(to);
            } else {
                adj.set_value(0.0);
            }
        }
        self.marked.replace((rects, current));
        self.marks.queue_draw();
    }

    fn draw_marks(&self, _area: &gtk::DrawingArea, cr: &gtk::cairo::Context, width: i32) {
        let page = self.page.get();
        let Some((w, _)) = self.doc.borrow().as_ref().and_then(|open| pdf::page_size(&open.doc, page - 1)) else { return };
        let scale = f64::from(width) / w;
        let (rects, current) = &*self.marked.borrow();
        for (i, &(x1, y1, x2, y2)) in rects.iter().enumerate() {
            // Yellow as a highlighter's on the paper; the match on show in orange.
            if Some(i) == *current {
                cr.set_source_rgba(1.0, 0.55, 0.0, 0.5);
            } else {
                cr.set_source_rgba(1.0, 0.85, 0.0, 0.4);
            }
            cr.rectangle(x1 * scale - 1.0, y1 * scale - 1.0, (x2 - x1) * scale + 2.0, (y2 - y1) * scale + 2.0);
            let _ = cr.fill();
        }
    }

    /// Copies the text of the page on show, as the library extracted it.
    fn copy_page(self: &Rc<Self>) {
        let page = self.page.get();
        self.with_text(move |me, text| {
            let Some(app) = me.app.upgrade() else { return };
            match text.pages.get(page as usize - 1).map(|t| t.trim()).filter(|t| !t.is_empty()) {
                Some(t) => {
                    me.root.clipboard().set_text(t);
                    app.shell.toast(&tr!("Copied the text of page {}.", omui::i18n::format_int(page.into())));
                }
                None => app.shell.toast(&tr("This page has no text to copy: it is a picture of the page.")),
            }
        });
    }

    fn set_zoom(self: &Rc<Self>, z: f64) {
        self.zoom.set(z.clamp(0.4, 4.0));
        self.render();
    }

    fn schedule_render(self: &Rc<Self>) {
        let width = self.scroll.width();
        if width == self.last_width.get() {
            return;
        }
        if let Some(id) = self.pending.take() {
            id.remove();
        }
        let me = Rc::downgrade(self);
        let id = glib::timeout_add_local_once(Duration::from_millis(80), move || {
            if let Some(me) = me.upgrade() {
                me.pending.take();
                me.render();
            }
        });
        self.pending.replace(Some(id));
    }

    /// Shows the page on show at the width and zoom there are now, and answers the height it is shown at. A page
    /// kept from before is shown at once; any other is drawn off the main thread and shown when it arrives, if it is
    /// still the one waited for.
    fn render(self: &Rc<Self>) -> i32 {
        let page = self.page.get();
        let (file, size) = match self.doc.borrow().as_ref() {
            Some(open) => (open.file.clone(), pdf::page_size(&open.doc, page - 1)),
            None => return 0,
        };
        self.forget_drawing();
        let Some((w, h)) = size else {
            self.fail(&tr("This page could not be drawn."));
            return 0;
        };
        self.note.set_visible(false);
        let avail = (self.scroll.width() - 48).max(320);
        self.last_width.set(self.scroll.width());
        // Fit the width up to a comfortable reading size, then zoom from there.
        let logical = ((f64::from(avail)).min(980.0) * self.zoom.get()).round() as i32;
        let height = (f64::from(logical) * h / w).round() as i32;
        // The page's place is its size at once, blank paper until it is drawn.
        self.picture.set_size_request(logical, height);
        // Sharp on a dense screen, but never a picture of hundreds of megabytes at full zoom.
        let key: PageKey = (file, page, (logical * self.picture.scale_factor()).min(4096));
        let kept = self.kept.borrow_mut().get(&key);
        if let Some(tex) = kept {
            self.picture.set_paintable(Some(&tex));
            return height;
        }
        // Meanwhile: the same page as it was drawn at another width, stretched, or blank paper; never another page.
        let meanwhile = self.kept.borrow().any_width(&key.0, page);
        self.picture.set_paintable(meanwhile.as_ref());
        self.wanted.replace(Some(key.clone()));
        // A page that takes long says so.
        let (me, slow) = (Rc::downgrade(self), key.clone());
        glib::timeout_add_local_once(Duration::from_millis(400), move || {
            if let Some(me) = me.upgrade()
                && me.waits_for(&slow)
            {
                me.say(&tr("Drawing this page…"));
            }
        });
        if self.asked.borrow().contains(&key) {
            return height;   // on its way already: turned away from and come back to
        }
        self.asked.borrow_mut().push(key.clone());
        let answer = pdf::draw_later(&key.0, page - 1, key.2);
        let me = Rc::downgrade(self);
        glib::spawn_future_local(async move {
            let answer = answer.recv().await;
            let Some(me) = me.upgrade() else { return };
            me.asked.borrow_mut().retain(|k| *k != key);
            let Ok(drawn) = answer else {
                // No answer: another page was asked for before this one's turn. If this one is wanted again, it is
                // asked for again.
                if me.waits_for(&key) {
                    me.render();
                }
                return;
            };
            let tex = drawn.map(|d| {
                let bytes = d.bytes();
                let tex = d.texture();
                me.kept.borrow_mut().put(key.clone(), tex.clone(), bytes);
                tex
            });
            if !me.waits_for(&key) {
                return;   // kept for when it is come back to, but not what is on show now
            }
            match tex {
                Some(tex) => {
                    me.forget_drawing();
                    me.note.set_visible(false);
                    me.picture.set_paintable(Some(&tex));
                }
                // The drawer's own reason, when it could not start, names the system's error as the system gave it.
                None => me.fail(&pdf::drawer_error().map(str::to_owned).unwrap_or_else(|| tr("This page could not be drawn."))),
            }
        });
        height
    }
}

#[cfg(test)]
mod tests {
    use gdk::{Key, ModifierType as M};

    use super::*;

    #[test]
    fn arrows_scroll_within_the_page_and_turn_it() {
        let none = M::empty();
        assert_eq!(act_for(Key::Down, none), Some(Act::Line(1)));
        assert_eq!(act_for(Key::Up, none), Some(Act::Line(-1)));   // taken, so the keyboard does not leave the page
        assert_eq!(act_for(Key::j, none), Some(Act::Line(1)));
        assert_eq!(act_for(Key::k, none), Some(Act::Line(-1)));
        assert_eq!(act_for(Key::Right, none), Some(Act::Turn(1)));
        assert_eq!(act_for(Key::Left, none), Some(Act::Turn(-1)));
        assert_eq!(act_for(Key::Home, none), Some(Act::First));
        assert_eq!(act_for(Key::End, none), Some(Act::Last));
    }

    #[test]
    fn a_screenful_keys() {
        let none = M::empty();
        assert_eq!(act_for(Key::space, none), Some(Act::Screen(1)));
        assert_eq!(act_for(Key::Page_Down, none), Some(Act::Screen(1)));
        assert_eq!(act_for(Key::space, M::SHIFT_MASK), Some(Act::Screen(-1)));
        assert_eq!(act_for(Key::BackSpace, none), Some(Act::Screen(-1)));
        assert_eq!(act_for(Key::Page_Up, none), Some(Act::Screen(-1)));
    }

    #[test]
    fn keys_held_with_modifiers_are_left_to_the_window() {
        assert_eq!(act_for(Key::Left, M::ALT_MASK), None);   // Back
        assert_eq!(act_for(Key::Right, M::ALT_MASK), None);
        assert_eq!(act_for(Key::Down, M::CONTROL_MASK), None);   // the scrolled window's own
        assert_eq!(act_for(Key::d, M::CONTROL_MASK), None);
        assert_eq!(act_for(Key::j, M::CONTROL_MASK), None);
        assert_eq!(act_for(Key::plus, M::SUPER_MASK), None);
        assert_eq!(act_for(Key::a, M::empty()), None);
        assert_eq!(act_for(Key::Tab, M::empty()), None);
    }

    #[test]
    fn zoom_keys() {
        assert_eq!(act_for(Key::plus, M::SHIFT_MASK), Some(Act::ZoomIn));
        assert_eq!(act_for(Key::equal, M::empty()), Some(Act::ZoomIn));
        assert_eq!(act_for(Key::equal, M::CONTROL_MASK), Some(Act::ZoomIn));
        assert_eq!(act_for(Key::minus, M::CONTROL_MASK), Some(Act::ZoomOut));
        assert_eq!(act_for(Key::_0, M::empty()), Some(Act::Fit));
    }

    #[test]
    fn scrolling_stops_at_the_ends() {
        // A page 1268 high in a window of 700: 568 to scroll through.
        assert_eq!(scrolled(0.0, 1268.0, 700.0, 60.0), Some(60.0));
        assert_eq!(scrolled(0.0, 1268.0, 700.0, 630.0), Some(568.0));
        assert_eq!(scrolled(568.0, 1268.0, 700.0, 630.0), None);   // at the foot: a screenful turns the page
        assert_eq!(scrolled(0.0, 1268.0, 700.0, -60.0), None);
        assert_eq!(scrolled(30.0, 1268.0, 700.0, -60.0), Some(0.0));
        // A page that fits has nowhere to scroll.
        assert_eq!(scrolled(0.0, 700.0, 700.0, 630.0), None);
        assert_eq!(scrolled(0.0, 500.0, 700.0, -630.0), None);
    }

    #[test]
    fn a_record_names_a_file_inside_the_library_only() {
        let lib = Path::new("/lib");
        assert_eq!(library_file(lib, "documents/a b.pdf"), Some(PathBuf::from("/lib/documents/a b.pdf")));
        assert_eq!(library_file(lib, "/etc/passwd"), None);
        assert_eq!(library_file(lib, "../outside.pdf"), None);
        assert_eq!(library_file(lib, "documents/../../outside.pdf"), None);
        assert_eq!(library_file(lib, ""), None);
    }

    #[test]
    fn find_reads_across_lines_and_case() {
        assert_eq!(folded("  Boil the\nWATER\t for  one minute "), "boil the water for one minute");
        let pages: Vec<String> = ["Boil the water.\nBoil it again.", "", "Store the boiled\n water."].iter().map(|p| folded(p)).collect();
        assert_eq!(matches(&pages, &folded("boil")), vec![1, 1, 3]);
        assert_eq!(matches(&pages, &folded("boiled water")), vec![3]);
        assert_eq!(matches(&pages, &folded("rope")), Vec::<i32>::new());
        assert_eq!(matches(&pages, ""), Vec::<i32>::new());
    }

    #[test]
    fn find_starts_from_the_page_on_show() {
        assert_eq!(first_from(&[1, 1, 3, 7], 1), 0);
        assert_eq!(first_from(&[1, 1, 3, 7], 2), 2);
        assert_eq!(first_from(&[1, 1, 3, 7], 7), 3);
        assert_eq!(first_from(&[1, 1, 3, 7], 9), 0);   // none after it: round to the first
    }

    fn key(page: i32, width: i32) -> PageKey {
        (PathBuf::from("a.pdf"), page, width)
    }

    #[test]
    fn kept_pages_are_found_again_and_the_oldest_go_first() {
        let mut kept = Kept::new(100);
        kept.put(key(1, 800), "one", 40);
        kept.put(key(2, 800), "two", 40);
        assert_eq!(kept.get(&key(1, 800)), Some("one"));   // used: now the newest
        kept.put(key(3, 800), "three", 40);
        assert_eq!(kept.get(&key(2, 800)), None);
        assert_eq!(kept.get(&key(1, 800)), Some("one"));
        assert_eq!(kept.get(&key(3, 800)), Some("three"));
        assert_eq!(kept.get(&key(3, 900)), None);   // another width is another picture
        assert_eq!(kept.any_width(Path::new("a.pdf"), 3), Some("three"));
        assert_eq!(kept.any_width(Path::new("b.pdf"), 3), None);
    }

    #[test]
    fn the_newest_page_is_kept_whatever_its_size() {
        let mut kept = Kept::new(100);
        kept.put(key(1, 800), "small", 40);
        kept.put(key(2, 4096), "huge", 500);
        assert_eq!(kept.get(&key(2, 4096)), Some("huge"));
        assert_eq!(kept.get(&key(1, 800)), None);
        kept.put(key(2, 4096), "again", 500);
        assert_eq!(kept.pages.len(), 1);
    }
}
