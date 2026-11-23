// SPDX-License-Identifier: MIT
//! Videos: the library's video rails (/api/videos) with thumbnails, a search over every video, and the player.
//!
//! The player is GTK's own video widget (GStreamer, built into GTK) on the file in the library, starting at the moment a
//! citation names. Beside it, the transcript (/api/transcript) follows playback; a line jumps there when clicked.

use std::{
    cell::{Cell, RefCell},
    rc::{Rc, Weak},
};

use gtk::{gdk, glib, prelude::*};
use omui::{clear, tr};
use serde_json::Value;

use crate::{
    cards::{self, activate, clock, section, some_then_all},
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
    /// When each transcript row is said; None for a transcript that is plain paragraphs, with no times.
    cue_times: RefCell<Vec<Option<f64>>>,
    current: RefCell<Option<String>>,
    seek_to: Cell<f64>,
    loaded: Cell<bool>,
    /// The video on show could not be played: what its line said before, and GStreamer's reason.
    unplayable: RefCell<Option<(String, String)>>,
}

/// A thumbnail from the library folder as a 16:9 tile, or None if it can't be read. The grid lays cards out by the
/// picture's own size, and the files are up to 1280 pixels wide in several shapes: that gave two stretched, cropped
/// cards a row. So every tile is CARD_WIDTH across and 16:9, the picture filling it (cut evenly at the edges where its
/// shape differs), drawn from enough pixels to stay sharp on a 2x screen.
fn texture(app: &App, rel: &str) -> Option<gdk::Paintable> {
    if rel.is_empty() {
        return None;
    }
    let file = crate::reader::library_file(&app.engine.ready.library, rel)?;   // a record names a file inside the library only
    let pixbuf = gtk::gdk_pixbuf::Pixbuf::from_file_at_scale(file, 2 * CARD_WIDTH, -1, true).ok()?;
    let (w, h) = (CARD_WIDTH as f32, CARD_WIDTH as f32 * 9.0 / 16.0);
    let scale = (w / pixbuf.width().max(1) as f32).max(h / pixbuf.height().max(1) as f32);
    let (pw, ph) = (pixbuf.width() as f32 * scale, pixbuf.height() as f32 * scale);
    let tile = gtk::graphene::Rect::new(0.0, 0.0, w, h);
    let snapshot = gtk::Snapshot::new();
    snapshot.push_clip(&tile);
    snapshot.append_texture(&gdk::Texture::for_pixbuf(&pixbuf), &gtk::graphene::Rect::new((w - pw) / 2.0, (h - ph) / 2.0, pw, ph));
    snapshot.pop();
    snapshot.to_paintable(Some(&gtk::graphene::Size::new(w, h)))
}

/// The transcript line being said `now` seconds in: the last one that has begun, or is about to within a quarter of a
/// second (the player tells of its place a few times a second). Lines without a time are never the one.
fn cue_at(times: &[Option<f64>], now: f64) -> Option<usize> {
    times.iter().rposition(|t| t.is_some_and(|t| t <= now + 0.25))
}

/// The width of a video card's picture.
const CARD_WIDTH: i32 = 230;

impl Videos {
    pub fn build(app: &Rc<App>) -> Rc<Self> {
        // The list: search, then rails.
        let (list_scroll, column) = cards::column(12);
        let t = gtk::Label::new(None);
        omui::i18n::bind_label(&t, "Videos");
        t.add_css_class("page-title");
        t.set_xalign(0.0);
        let entry = gtk::SearchEntry::new();
        omui::i18n::bind(&entry, |e| e.set_placeholder_text(Some(&tr("Search video titles"))));
        let rails = gtk::Box::new(gtk::Orientation::Vertical, 10);
        column.append(&t);
        column.append(&entry);
        column.append(&rails);

        // The player: video above, transcript beside.
        let video = gtk::Video::new();
        video.set_autoplay(true);
        video.set_hexpand(true);
        video.set_vexpand(true);
        video.add_css_class("player");
        let title = omui::label("", "section-title");
        let meta = gtk::Label::new(None);
        meta.add_css_class("meta");
        meta.set_xalign(0.0);
        meta.set_wrap(true);
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
        let cue_head = gtk::Label::new(None);
        omui::i18n::bind_label(&cue_head, "Transcript");
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
            unplayable: RefCell::new(None),
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
                && let Some(Some(t)) = me.cue_times.borrow().get(row.index() as usize).copied()
                && let Some(s) = me.video.media_stream()
            {
                s.seek((t * 1e6) as i64);
                s.play();
            }
        });
        v
    }

    /// After a language switch: the list is drawn again when it is next shown. The player carries on; what it wrote
    /// beside the video's own words follows the language by itself (`i18n::bind`).
    pub fn retranslate(&self) {
        self.loaded.set(false);
        self.say_unplayable();
    }

    /// Says that the video on show cannot be played here, if it cannot, in the language of the moment.
    fn say_unplayable(&self) {
        if let Some((base, why)) = self.unplayable.borrow().as_ref() {
            self.meta.set_text(&tr!("{} · cannot play here: {} (an AV1 video needs gst-plugin-dav1d)", base, why));
            self.meta.add_css_class("error");
        }
    }

    /// Loads the rails the first time the list is shown.
    pub fn show_list(self: &Rc<Self>) {
        if self.loaded.replace(true) {
            return;
        }
        let Some(app) = self.app.upgrade() else { return };
        let engine = app.engine.clone();
        let me = Rc::downgrade(self);
        glib::spawn_future_local(async move {
            let rails = engine.get("/api/videos").await;
            let Some(me) = me.upgrade() else { return };
            clear(&me.rails);
            let rails = match rails {
                Ok(rails) => rails,
                Err(e) => {
                    me.loaded.set(false);   // the next visit asks again, as the button does
                    let again = Rc::downgrade(&me);
                    me.rails.append(&cards::problem(&tr("Lastbook could not read the library's videos."), &e.to_string(), move || {
                        if let Some(me) = again.upgrade() {
                            me.show_list();
                        }
                    }));
                    return;
                }
            };
            for rail in rails.as_array().into_iter().flatten() {
                let items: Vec<Value> = rail["items"].as_array().cloned().unwrap_or_default();
                if items.is_empty() {
                    continue;
                }
                let count = omui::i18n::format_int(rail["count"].as_i64().unwrap_or(0));
                me.rails.append(&section(&tr!("{} ({})", rail["title"].as_str().unwrap_or_default(), count)));
                me.show_cards(&items);
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
            let found = engine.get(&query("/api/videos/all", &[("q", &q)])).await;
            let Some(me) = me.upgrade() else { return };
            clear(&me.rails);
            let r = match found {
                Ok(r) => r,
                Err(e) => {
                    let (again, q) = (Rc::downgrade(&me), q.clone());
                    me.rails.append(&cards::problem(&tr("Lastbook could not search the videos."), &e.to_string(), move || {
                        if let Some(me) = again.upgrade() {
                            me.search(&q);
                        }
                    }));
                    return;
                }
            };
            let items: Vec<Value> = r["items"].as_array().cloned().unwrap_or_default();
            me.rails.append(&section(&omui::trnf("{n} videos for “{}”", r["total"].as_i64().unwrap_or(0), &[&q])));
            me.show_cards(&items);
        });
    }

    /// A grid of the first few of `items`, the rest behind a button: every card reads its thumbnail from disk.
    fn show_cards(self: &Rc<Self>, items: &[Value]) {
        let grid = cards::grid(5, 10);
        self.rails.append(&grid);
        let me = Rc::downgrade(self);
        some_then_all(&self.rails, items, 10, move |part| {
            if let Some(me) = me.upgrade() {
                me.fill(&grid, part);
            }
        });
    }

    fn fill(&self, grid: &gtk::FlowBox, items: &[Value]) {
        let Some(app) = self.app.upgrade() else { return };
        for it in items {
            let card = gtk::Box::new(gtk::Orientation::Vertical, 4);
            card.add_css_class("video-card");
            let overlay = gtk::Overlay::new();
            let pic = gtk::Picture::new();
            pic.set_content_fit(gtk::ContentFit::Contain);
            pic.set_size_request(CARD_WIDTH, CARD_WIDTH * 9 / 16);
            pic.set_can_shrink(false);
            for w in [pic.upcast_ref::<gtk::Widget>(), overlay.upcast_ref()] {
                w.set_halign(gtk::Align::Start);         // the tile keeps its size, over its title, the length badge in its corner
                w.set_valign(gtk::Align::Start);
                w.set_hexpand(false);
                w.set_vexpand(false);
            }
            card.set_halign(gtk::Align::Start);            // as wide as its tile: the title wraps under the picture
            card.set_valign(gtk::Align::Start);
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
            title.set_max_width_chars(26);                // no wider than its tile, so the tile sets the column
            title.set_width_chars(1);
            title.set_yalign(0.0);
            title.set_valign(gtk::Align::Start);
            title.set_ellipsize(gtk::pango::EllipsizeMode::End);
            let meta = gtk::Label::new(it["publisher"].as_str());
            meta.add_css_class("meta");
            meta.set_xalign(0.0);
            meta.set_ellipsize(gtk::pango::EllipsizeMode::End);
            meta.set_max_width_chars(26);
            card.append(&overlay);
            card.append(&title);
            card.append(&meta);
            activate(&card, self.app.clone(), Route::Watch { video: it["id"].as_str().unwrap_or_default().to_owned(), t: -1.0 });
            grid.insert(&card, -1);
        }
    }

    /// Opens video `id` at `t` seconds; a negative `t` is no moment in particular (its card, a document that is a video).
    /// `revisit`: shown again by Back or Forward.
    pub fn play(self: &Rc<Self>, id: &str, t: f64, revisit: bool) {
        self.seek_to.set(t.max(0.0));
        if self.current.borrow().as_deref() == Some(id) {
            // The same video again carries on where it was (Back to it, its card once more); a link to a moment, 0:00
            // included, goes there.
            if let Some(s) = self.video.media_stream() {
                if !revisit && t >= 0.0 {
                    s.seek((t * 1e6) as i64);
                }
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
            let rec = engine.get(&format!("/api/doc/{}", urlencoding::encode(&id))).await;
            let Some(me) = me.upgrade() else { return };
            let rec = match rec {
                Ok(rec) => rec,
                Err(e) => return me.no_video(&tr("This video is not in the library."), &e.to_string()),
            };
            let Some(app) = me.app.upgrade() else { return };
            let title = rec["title"].as_str().unwrap_or_default().to_owned();
            app.report_title("watch", &title);
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
            let Some(path) = crate::reader::library_file(&app.engine.ready.library, rec["path"].as_str().unwrap_or_default()) else {
                return me.no_video(&tr("This video's file is not in the library."), &tr("The catalog names a file outside the library's folders, which Lastbook does not open."));
            };
            let file = gtk::gio::File::for_path(path);
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
            let (failed, base) = (Rc::downgrade(&me), me.meta.text().to_string());
            stream.connect_error_notify(move |s| {
                if let (Some(e), Some(me)) = (s.error(), failed.upgrade()) {
                    // GStreamer's own message, as it gave it, inside the window's sentence (`say_unplayable`).
                    me.unplayable.replace(Some((base.clone(), e.message().to_string())));
                    me.say_unplayable();
                }
            });
            // The transcript follows playback from the player's own word of where it is: no timer, and nothing at all
            // while the video is paused or the window is put away.
            let follower = Rc::downgrade(&me);
            stream.connect_timestamp_notify(move |s| {
                if let Some(me) = follower.upgrade() {
                    me.follow(s.upcast_ref());
                }
            });
            me.meta.remove_css_class("error");
            me.unplayable.replace(None);
            me.video.set_media_stream(Some(&stream));
            let tx = engine.get(&format!("/api/transcript/{}", urlencoding::encode(&id))).await;
            if me.current.borrow().as_deref() != Some(id.as_str()) {
                return;   // another video was opened while this one's transcript was read
            }
            if let Err(e) = &tx {
                let (l, e) = (gtk::Label::new(None), e.to_string());
                omui::i18n::bind(&l, move |l| l.set_text(&tr!("Lastbook could not read this video's transcript: {}", e)));
                l.add_css_class("error");
                l.set_wrap(true);
                l.set_xalign(0.0);
                me.cues.append(&l);
            }
            if let Ok(tx) = tx {
                for c in tx["cues"].as_array().into_iter().flatten() {
                    let t = c["t"].as_f64();
                    let row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
                    let tl = gtk::Label::new(t.map(clock).as_deref());
                    tl.set_visible(t.is_some());
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
                    let l = gtk::Label::new(None);
                    omui::i18n::bind_label(&l, "This video has no transcript.");
                    l.add_css_class("dim-label");
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

    /// What the player shows in place of a video it cannot open: `what` in a sentence, and `why` under it.
    fn no_video(&self, what: &str, why: &str) {
        self.unplayable.replace(None);
        self.title.set_text(what);
        if let Some(app) = self.app.upgrade() {
            app.report_title("watch", &tr("Video not found"));
        }
        self.meta.set_text(why);
        self.meta.add_css_class("error");
        self.video.set_media_stream(gtk::MediaStream::NONE);
        self.current.replace(None);
    }

    /// Selects the transcript line being said. Called as `stream` tells of its place (`timestamp`), so only while it
    /// plays; a stream replaced by the next video's tells nothing more that matters.
    fn follow(&self, stream: &gtk::MediaStream) {
        if self.video.media_stream().as_ref() != Some(stream) || !stream.is_playing() {
            return;
        }
        let idx = cue_at(&self.cue_times.borrow(), stream.timestamp() as f64 / 1e6);
        if let Some(i) = idx
            && let Some(row) = self.cues.row_at_index(i as i32)
            && self.cues.selected_row().as_ref() != Some(&row)
        {
            self.cues.select_row(Some(&row));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_transcript_follows_the_players_place() {
        let times = [Some(0.0), Some(4.0), None, Some(9.5)];
        assert_eq!(cue_at(&times, 0.0), Some(0));
        assert_eq!(cue_at(&times, 3.7), Some(0));
        assert_eq!(cue_at(&times, 3.8), Some(1), "a line about to begin is the one");
        assert_eq!(cue_at(&times, 9.0), Some(1), "a line without a time is never the one");
        assert_eq!(cue_at(&times, 60.0), Some(3));
        assert_eq!(cue_at(&[Some(5.0)], 1.0), None);
        assert_eq!(cue_at(&[None, None], 1.0), None, "a transcript of plain paragraphs has no line to follow");
    }
}
