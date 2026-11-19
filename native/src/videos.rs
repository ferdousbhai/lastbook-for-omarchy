// SPDX-License-Identifier: MIT
//! Videos: the library's video rails (/api/videos) with thumbnails, a search over every video, and the player.
//!
//! The player is GTK's own video widget (GStreamer, built into GTK) on the file in the library, starting at the moment a
//! citation names. Beside it, the transcript (/api/transcript) follows playback; a line jumps there when clicked.

use std::{
    cell::{Cell, RefCell},
    rc::{Rc, Weak},
    time::Duration,
};

use gtk::{gdk, glib, prelude::*};
use serde_json::Value;

use crate::{
    cards::{activate, clear, clock, section},
    engine::query,
    ui::{App, Route},
};

pub struct Videos {
    pub list_root: gtk::Widget,
    pub player_root: gtk::Widget,
    app: Weak<App>,
    rails: gtk::Box,
    entry: gtk::SearchEntry,
    video: gtk::Video,
    title: gtk::Label,
    meta: gtk::Label,
    cues: gtk::ListBox,
    cue_times: RefCell<Vec<f64>>,
    current: RefCell<Option<String>>,
    seek_to: Cell<f64>,
    loaded: Cell<bool>,
}

/// A thumbnail from the library folder, or None if it can't be read.
fn texture(app: &App, rel: &str) -> Option<gdk::Texture> {
    if rel.is_empty() {
        return None;
    }
    gdk::Texture::from_filename(app.engine.ready.library.join(rel)).ok()
}

impl Videos {
    pub fn build(app: &Rc<App>) -> Rc<Self> {
        // The list: search, then rails.
        let column = gtk::Box::new(gtk::Orientation::Vertical, 12);
        column.set_margin_top(28);
        column.set_margin_bottom(28);
        column.set_margin_start(32);
        column.set_margin_end(32);
        let t = gtk::Label::new(Some("Videos"));
        t.add_css_class("page-title");
        t.set_xalign(0.0);
        let s = gtk::Label::new(Some("Watch it done: practical how-tos first, then channels and courses."));
        s.add_css_class("dim");
        s.set_xalign(0.0);
        let entry = gtk::SearchEntry::new();
        entry.set_placeholder_text(Some("Search video titles and transcripts"));
        let rails = gtk::Box::new(gtk::Orientation::Vertical, 10);
        column.append(&t);
        column.append(&s);
        column.append(&entry);
        column.append(&rails);
        let list_scroll = gtk::ScrolledWindow::builder().child(&column).vexpand(true).hscrollbar_policy(gtk::PolicyType::Never).build();

        // The player: video above, transcript beside.
        let video = gtk::Video::new();
        video.set_autoplay(true);
        video.set_hexpand(true);
        video.set_vexpand(true);
        video.add_css_class("player");
        let title = gtk::Label::new(None);
        title.add_css_class("section-title");
        title.set_xalign(0.0);
        title.set_wrap(true);
        let meta = gtk::Label::new(None);
        meta.add_css_class("meta");
        meta.set_xalign(0.0);
        let left = gtk::Box::new(gtk::Orientation::Vertical, 6);
        left.set_hexpand(true);
        left.set_margin_top(12);
        left.set_margin_start(16);
        left.set_margin_end(16);
        left.set_margin_bottom(12);
        left.append(&video);
        left.append(&title);
        left.append(&meta);
        let cues = gtk::ListBox::new();
        cues.add_css_class("cues");
        let cue_head = gtk::Label::new(Some("Transcript"));
        cue_head.add_css_class("sidebar-heading");
        cue_head.set_xalign(0.0);
        let right = gtk::Box::new(gtk::Orientation::Vertical, 0);
        right.add_css_class("transcript");
        right.set_size_request(320, -1);
        right.append(&cue_head);
        right.append(&gtk::ScrolledWindow::builder().child(&cues).vexpand(true).hscrollbar_policy(gtk::PolicyType::Never).build());
        let player = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        player.append(&left);
        player.append(&right);

        let v = Rc::new(Self {
            list_root: list_scroll.upcast(),
            player_root: player.upcast(),
            app: Rc::downgrade(app),
            rails,
            entry,
            video,
            title,
            meta,
            cues,
            cue_times: RefCell::new(Vec::new()),
            current: RefCell::new(None),
            seek_to: Cell::new(0.0),
            loaded: Cell::new(false),
        });
        let me = Rc::downgrade(&v);
        v.entry.connect_activate(move |e| {
            if let Some(me) = me.upgrade() {
                me.search(e.text().as_str());
            }
        });
        let me = Rc::downgrade(&v);
        v.cues.connect_row_activated(move |_, row| {
            if let Some(me) = me.upgrade()
                && let Some(t) = me.cue_times.borrow().get(row.index() as usize)
                && let Some(s) = me.video.media_stream()
            {
                s.seek((*t * 1e6) as i64);
                s.play();
            }
        });
        // Follow playback in the transcript, four times a second while a video is open.
        let me = Rc::downgrade(&v);
        glib::timeout_add_local(Duration::from_millis(250), move || {
            let Some(me) = me.upgrade() else { return glib::ControlFlow::Break };
            me.follow();
            glib::ControlFlow::Continue
        });
        v
    }

    /// Loads the rails the first time the list is shown.
    pub fn show_list(self: &Rc<Self>) {
        if let Some(s) = self.video.media_stream() {
            s.pause();
        }
        if self.loaded.replace(true) {
            return;
        }
        let Some(app) = self.app.upgrade() else { return };
        let engine = app.engine.clone();
        let me = Rc::downgrade(self);
        glib::spawn_future_local(async move {
            let Ok(rails) = engine.get("/api/videos").await else { return };
            let Some(me) = me.upgrade() else { return };
            clear(&me.rails);
            for rail in rails.as_array().into_iter().flatten() {
                let items: Vec<Value> = rail["items"].as_array().cloned().unwrap_or_default();
                if items.is_empty() {
                    continue;
                }
                me.rails.append(&section(&format!("{} ({})", rail["title"].as_str().unwrap_or_default(), rail["count"])));
                me.rails.append(&me.grid(&items));
            }
        });
    }

    fn search(self: &Rc<Self>, q: &str) {
        let Some(app) = self.app.upgrade() else { return };
        if q.trim().is_empty() {
            self.loaded.set(false);
            self.show_list();
            return;
        }
        let (engine, q) = (app.engine.clone(), q.to_owned());
        let me = Rc::downgrade(self);
        glib::spawn_future_local(async move {
            let Ok(r) = engine.get(&query("/api/videos/all", &[("q", &q)])).await else { return };
            let Some(me) = me.upgrade() else { return };
            clear(&me.rails);
            let items: Vec<Value> = r["items"].as_array().cloned().unwrap_or_default();
            me.rails.append(&section(&format!("{} video{} for “{q}”", r["total"], if r["total"] == 1 { "" } else { "s" })));
            me.rails.append(&me.grid(&items));
        });
    }

    fn grid(&self, items: &[Value]) -> gtk::FlowBox {
        let grid = gtk::FlowBox::new();
        grid.set_selection_mode(gtk::SelectionMode::None);
        grid.set_homogeneous(true);
        grid.set_min_children_per_line(2);
        grid.set_max_children_per_line(5);
        grid.set_row_spacing(10);
        grid.set_column_spacing(10);
        let Some(app) = self.app.upgrade() else { return grid };
        for it in items {
            let card = gtk::Box::new(gtk::Orientation::Vertical, 4);
            card.add_css_class("video-card");
            let overlay = gtk::Overlay::new();
            let pic = gtk::Picture::new();
            pic.set_content_fit(gtk::ContentFit::Cover);
            pic.set_size_request(200, 112);
            pic.add_css_class("video-thumb");
            if let Some(tex) = texture(&app, it["thumb"].as_str().unwrap_or_default()) {
                pic.set_paintable(Some(&tex));
            }
            overlay.set_child(Some(&pic));
            if let Some(d) = it["duration"].as_f64() {
                let badge = gtk::Label::new(Some(&clock(d)));
                badge.add_css_class("badge");
                badge.set_halign(gtk::Align::End);
                badge.set_valign(gtk::Align::End);
                badge.set_margin_end(6);
                badge.set_margin_bottom(6);
                overlay.add_overlay(&badge);
            }
            let title = gtk::Label::new(it["title"].as_str());
            title.add_css_class("result-title");
            title.set_xalign(0.0);
            title.set_wrap(true);
            title.set_lines(2);
            title.set_ellipsize(gtk::pango::EllipsizeMode::End);
            let meta = gtk::Label::new(it["publisher"].as_str());
            meta.add_css_class("meta");
            meta.set_xalign(0.0);
            meta.set_ellipsize(gtk::pango::EllipsizeMode::End);
            card.append(&overlay);
            card.append(&title);
            card.append(&meta);
            activate(&card, self.app.clone(), Route::Watch { video: it["id"].as_str().unwrap_or_default().to_owned(), t: 0.0 });
            grid.insert(&card, -1);
        }
        grid
    }

    /// Opens video `id` at `t` seconds.
    pub fn play(self: &Rc<Self>, id: &str, t: f64) {
        self.seek_to.set(t);
        if self.current.borrow().as_deref() == Some(id) {
            if let Some(s) = self.video.media_stream() {
                s.seek((t * 1e6) as i64);
                s.play();
            }
            return;
        }
        self.current.replace(Some(id.to_owned()));
        clear(&self.cues);
        self.cue_times.borrow_mut().clear();
        let Some(app) = self.app.upgrade() else { return };
        let (engine, id) = (app.engine.clone(), id.to_owned());
        let me = Rc::downgrade(self);
        glib::spawn_future_local(async move {
            let Ok(rec) = engine.get(&format!("/api/doc/{}", urlencoding::encode(&id))).await else { return };
            let Some(me) = me.upgrade() else { return };
            let Some(app) = me.app.upgrade() else { return };
            let title = rec["title"].as_str().unwrap_or_default().to_owned();
            app.set_title(&title);
            me.title.set_text(&title);
            let mut meta = vec![rec["publisher"].as_str().unwrap_or_default().to_owned()];
            if let Some(y) = rec["year"].as_str() {
                meta.push(y.to_owned());
            }
            if let Some(d) = rec["duration"].as_f64() {
                meta.push(clock(d));
            }
            if let Some(t) = rec["trust"].as_str() {
                meta.push(t.to_owned());
            }
            me.meta.set_text(&meta.join(" · "));
            let file = gtk::gio::File::for_path(app.engine.ready.library.join(rec["path"].as_str().unwrap_or_default()));
            let stream = gtk::MediaFile::for_file(&file);
            let start = me.seek_to.get();
            if start > 0.0 {
                // Seek once the stream knows its length; seeking earlier is ignored.
                let weak = stream.downgrade();
                stream.connect_prepared_notify(move |_| {
                    if let Some(s) = weak.upgrade()
                        && s.is_prepared()
                    {
                        s.seek((start * 1e6) as i64);
                    }
                });
            }
            // A file this computer cannot decode says so instead of staying black.
            let note = me.meta.clone();
            let base = me.meta.text().to_string();
            stream.connect_error_notify(move |s| {
                if let Some(e) = s.error() {
                    note.set_text(&format!("{base} · can't play here: {} (an AV1 video needs gst-plugin-dav1d)", e.message()));
                    note.add_css_class("danger");
                }
            });
            me.meta.remove_css_class("danger");
            me.video.set_media_stream(Some(&stream));
            if let Ok(tx) = engine.get(&format!("/api/transcript/{}", urlencoding::encode(&id))).await {
                for c in tx["cues"].as_array().into_iter().flatten() {
                    let t = c["t"].as_f64().unwrap_or(0.0);
                    let row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
                    row.add_css_class("cue");
                    let tl = gtk::Label::new(Some(&clock(t)));
                    tl.add_css_class("cue-time");
                    tl.set_valign(gtk::Align::Start);
                    let text = gtk::Label::new(c["text"].as_str());
                    text.set_wrap(true);
                    text.set_xalign(0.0);
                    text.set_hexpand(true);
                    row.append(&tl);
                    row.append(&text);
                    me.cues.append(&row);
                    me.cue_times.borrow_mut().push(t);
                }
                if tx["cues"].as_array().is_none_or(|c| c.is_empty()) {
                    let l = gtk::Label::new(Some("This video has no transcript."));
                    l.add_css_class("dim");
                    me.cues.append(&l);
                }
            }
        });
    }

    pub fn pause(&self) {
        if let Some(s) = self.video.media_stream() {
            s.pause();
        }
    }

    fn follow(&self) {
        let Some(s) = self.video.media_stream() else { return };
        if !s.is_playing() {
            return;
        }
        let now = s.timestamp() as f64 / 1e6;
        let times = self.cue_times.borrow();
        let idx = times.iter().rposition(|t| *t <= now + 0.25);
        if let Some(i) = idx
            && let Some(row) = self.cues.row_at_index(i as i32)
            && self.cues.selected_row().as_ref() != Some(&row)
        {
            self.cues.select_row(Some(&row));
        }
    }
}
