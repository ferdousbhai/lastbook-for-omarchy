// SPDX-License-Identifier: MIT
//! The PDF reader: one page at a time, drawn by Poppler at the window's width (and the screen's scale, so text stays
//! sharp), with page and zoom controls. Arrow keys, Page Up/Down, Home and End turn pages; + and - zoom.

use std::{
    cell::{Cell, RefCell},
    rc::{Rc, Weak},
    time::Duration,
};

use gtk::{gdk, glib, prelude::*};
use omui::{icon_button, icons::name};

use crate::{pdf, ui::App};

pub struct Reader {
    pub root: gtk::Widget,
    app: Weak<App>,
    picture: gtk::Picture,
    scroll: gtk::ScrolledWindow,
    meta: gtk::Label,
    entry: gtk::Entry,
    total: gtk::Label,
    note: gtk::Label,
    doc: RefCell<Option<(String, poppler::Document)>>,
    page: Cell<i32>,
    zoom: Cell<f64>,
    last_width: Cell<i32>,
    pending: RefCell<Option<glib::SourceId>>,
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
        let prev = icon_button(name::BACK, "Previous page (Left, Page Up)");
        let entry = gtk::Entry::new();
        entry.add_css_class("page-entry");
        entry.set_width_chars(4);
        entry.set_max_width_chars(5);
        entry.set_tooltip_text(Some("Page number"));
        entry.update_property(&[gtk::accessible::Property::Label("Page number")]);
        gtk::prelude::EditableExt::set_alignment(&entry, 1.0);
        let total = gtk::Label::new(None);
        total.add_css_class("meta");
        let next = icon_button(name::FORWARD, "Next page (Right, Page Down)");
        let out = icon_button(name::ZOOM_OUT, "Smaller (-)");
        let fit = icon_button(name::FIT, "Fit the width (0)");
        let inn = icon_button(name::ZOOM_IN, "Larger (+)");
        bar.append(&meta);
        for w in [prev.upcast_ref::<gtk::Widget>(), entry.upcast_ref(), total.upcast_ref(), next.upcast_ref(), out.upcast_ref(), fit.upcast_ref(), inn.upcast_ref()] {
            bar.append(w);
        }

        let picture = gtk::Picture::new();
        picture.add_css_class("paper");
        picture.set_can_shrink(true);   // drawn at screen pixels, shown at the size it asks for
        picture.set_content_fit(gtk::ContentFit::Contain);
        picture.set_halign(gtk::Align::Center);
        picture.set_valign(gtk::Align::Start);
        picture.set_margin_top(18);
        picture.set_margin_bottom(18);
        let note = gtk::Label::new(None);
        note.add_css_class("status-line");
        note.set_wrap(true);
        note.set_visible(false);
        let canvas = gtk::Box::new(gtk::Orientation::Vertical, 0);
        canvas.add_css_class("reader-canvas");
        canvas.append(&note);
        canvas.append(&picture);
        let scroll = gtk::ScrolledWindow::builder().child(&canvas).vexpand(true).hexpand(true).build();
        let root = gtk::Box::new(gtk::Orientation::Vertical, 0);
        root.append(&bar);
        root.append(&scroll);

        let reader = Rc::new(Self {
            root: root.upcast(),
            app: Rc::downgrade(app),
            picture,
            scroll,
            meta,
            entry,
            total,
            note,
            doc: RefCell::new(None),
            page: Cell::new(1),
            zoom: Cell::new(1.0),
            last_width: Cell::new(0),
            pending: RefCell::new(None),
        });
        let r = Rc::downgrade(&reader);
        prev.connect_clicked(move |_| if let Some(r) = r.upgrade() { r.turn(-1) });
        let r = Rc::downgrade(&reader);
        next.connect_clicked(move |_| if let Some(r) = r.upgrade() { r.turn(1) });
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
        let keys = gtk::EventControllerKey::new();
        let r = Rc::downgrade(&reader);
        keys.connect_key_pressed(move |_, key, _, _| {
            let Some(r) = r.upgrade() else { return glib::Propagation::Proceed };
            if r.entry.has_focus() {
                return glib::Propagation::Proceed;
            }
            match key {
                gdk::Key::Right | gdk::Key::Page_Down | gdk::Key::space => r.turn(1),
                gdk::Key::Left | gdk::Key::Page_Up | gdk::Key::BackSpace => r.turn(-1),
                gdk::Key::Home => r.go_to(1),
                gdk::Key::End => r.go_to(i32::MAX),
                gdk::Key::plus | gdk::Key::equal | gdk::Key::KP_Add => r.set_zoom(r.zoom.get() * 1.2),
                gdk::Key::minus | gdk::Key::KP_Subtract => r.set_zoom(r.zoom.get() / 1.2),
                gdk::Key::_0 => r.set_zoom(1.0),
                _ => return glib::Propagation::Proceed,
            }
            glib::Propagation::Stop
        });
        reader.root.add_controller(keys);
        // Redraw at the new width once the window stops being resized.
        let r = Rc::downgrade(&reader);
        reader.scroll.hadjustment().connect_page_size_notify(move |_| {
            if let Some(r) = r.upgrade() {
                r.schedule_render();
            }
        });
        reader
    }

    pub fn open(self: &Rc<Self>, doc_id: &str, page: i32) {
        if self.doc.borrow().as_ref().is_some_and(|(id, _)| id == doc_id) {
            self.go_to(page);
            return;
        }
        self.doc.replace(None);
        self.picture.set_paintable(gdk::Paintable::NONE);
        self.entry.set_text("");
        self.total.set_text("");
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
                    app.report_title("reader", "Document not found");
                    return me.fail("This document is not in the library.");
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
            let file = app.engine.ready.library.join(rec["path"].as_str().unwrap_or_default());
            let Some(doc) = pdf::document(&file) else {
                return me.fail("The file could not be opened.");
            };
            if doc.n_pages() == 0 {
                return me.fail("This document has no pages.");
            }
            me.total.set_text(&format!("of {}", doc.n_pages()));
            me.doc.replace(Some((id, doc)));
            me.go_to(page);
        });
    }

    fn fail(&self, why: &str) {
        self.note.set_text(why);
        self.note.set_margin_top(60);
        self.note.set_visible(true);
        self.picture.set_paintable(gdk::Paintable::NONE);
    }

    fn pages(&self) -> i32 {
        self.doc.borrow().as_ref().map(|(_, d)| d.n_pages()).unwrap_or(0)
    }

    fn turn(&self, step: i32) {
        self.go_to(self.page.get() + step);
    }

    fn go_to(&self, n: i32) {
        let total = self.pages();
        if total == 0 {
            return;
        }
        let n = n.clamp(1, total);
        self.page.set(n);
        self.entry.set_text(&n.to_string());
        // The page on show is the page that is saved, and the one Back returns to.
        if let (Some(app), Some((doc, _))) = (self.app.upgrade(), self.doc.borrow().as_ref())
            && app.reading(doc)
        {
            app.replace_route(crate::ui::Route::Read { doc: doc.clone(), page: n }, false);
            app.update_star();
        }
        self.render();
        self.scroll.vadjustment().set_value(0.0);
    }

    fn set_zoom(&self, z: f64) {
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

    fn render(&self) {
        let doc = self.doc.borrow();
        let Some((_, doc)) = doc.as_ref() else { return };
        let avail = (self.scroll.width() - 48).max(320);
        self.last_width.set(self.scroll.width());
        // Fit the width up to a comfortable reading size, then zoom from there.
        let logical = ((f64::from(avail)).min(980.0) * self.zoom.get()).round() as i32;
        let scale = self.picture.scale_factor();
        // Sharp on a dense screen, but never a picture of hundreds of megabytes at full zoom.
        if let Some((tex, (w, h))) = pdf::render(doc, self.page.get() - 1, (logical * scale).min(4096)) {
            self.picture.set_paintable(Some(&tex));
            self.picture.set_size_request(logical, (f64::from(logical) * h / w).round() as i32);
        }
    }
}
