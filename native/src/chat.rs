// SPDX-License-Identifier: MIT
//! Chat: questions answered by the local AI from the library, streamed as they are written, every claim cited.
//!
//! The engine's /api/chat streams server-sent events (found, sources, state, tool, delta, reset, done, error, stopped);
//! this page draws them as they come: the steps the assistant takes, the answer (Markdown, re-drawn at most every 60 ms),
//! and the sources it cited, each opening where it came from. Chats are kept in the user's data folder.

use std::{
    cell::{Cell, RefCell},
    path::PathBuf,
    rc::{Rc, Weak},
    time::Duration,
};

use gtk::{gdk, glib, prelude::*};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{
    cards::{activate, card, clear, plain_markup, route_of, section, source_meta},
    engine::{Piece, query},
    md,
    ui::{App, Route},
};

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Msg {
    pub role: String,
    pub content: String,
    #[serde(default)]
    pub sources: Vec<Value>,
    #[serde(default)]
    pub cited: Vec<String>,
    #[serde(default)]
    pub steps: Vec<String>,
    #[serde(default)]
    pub error: Option<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Conversation {
    pub id: String,
    pub title: String,
    pub updated: i64,
    #[serde(default)]
    pub about: Option<String>,
    pub messages: Vec<Msg>,
}

struct Live {
    request: u64,
    chat: String,
    answer: gtk::Box,
    steps: gtk::Box,
    sources: gtk::Box,
    note: gtk::Label,
    text: String,
    dirty: bool,
}

pub struct ChatPage {
    pub root: gtk::Widget,
    app: Weak<App>,
    list: gtk::ListBox,
    convo: gtk::Box,
    scroll: gtk::ScrolledWindow,
    input: gtk::TextView,
    send: gtk::Button,
    status: gtk::Label,
    about_bar: gtk::Box,
    about_label: gtk::Label,
    chats: RefCell<Vec<Conversation>>,
    current: RefCell<Option<String>>,
    pending_about: RefCell<Option<String>>,
    live: RefCell<Option<Live>>,
    online: Cell<bool>,
}

fn store_path() -> PathBuf {
    glib::user_data_dir().join("Lastbook").join("chats.json")
}

fn now() -> i64 {
    glib::real_time() / 1_000_000
}

impl ChatPage {
    pub fn build(app: &Rc<App>) -> Rc<Self> {
        // The list of chats: a narrow column beside the conversation.
        let list = gtk::ListBox::new();
        list.add_css_class("chat-list");
        list.set_selection_mode(gtk::SelectionMode::Single);
        let new = gtk::Button::with_label("New chat");
        new.add_css_class("primary");
        new.set_margin_start(8);
        new.set_margin_end(8);
        new.set_margin_top(8);
        new.set_margin_bottom(6);
        let column = gtk::Box::new(gtk::Orientation::Vertical, 0);
        column.add_css_class("chat-column");
        column.set_size_request(200, -1);
        column.append(&new);
        column.append(&gtk::ScrolledWindow::builder().child(&list).vexpand(true).hscrollbar_policy(gtk::PolicyType::Never).build());

        let convo = gtk::Box::new(gtk::Orientation::Vertical, 18);
        convo.add_css_class("conversation");
        convo.set_margin_top(24);
        convo.set_margin_bottom(24);
        convo.set_margin_start(32);
        convo.set_margin_end(32);
        let scroll = gtk::ScrolledWindow::builder().child(&convo).vexpand(true).hscrollbar_policy(gtk::PolicyType::Never).build();

        let about_bar = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        about_bar.add_css_class("about-bar");
        let about_label = gtk::Label::new(None);
        about_label.set_xalign(0.0);
        about_label.set_hexpand(true);
        about_label.set_ellipsize(gtk::pango::EllipsizeMode::End);
        let about_x = gtk::Button::from_icon_name("window-close-symbolic");
        about_x.add_css_class("header-action");
        about_x.set_tooltip_text(Some("Don't use this page as context"));
        about_bar.append(&gtk::Image::from_icon_name("x-office-document-symbolic"));
        about_bar.append(&about_label);
        about_bar.append(&about_x);
        about_bar.set_visible(false);

        let input = gtk::TextView::new();
        input.add_css_class("composer");
        input.set_wrap_mode(gtk::WrapMode::WordChar);
        input.set_top_margin(8);
        input.set_bottom_margin(8);
        input.set_left_margin(10);
        input.set_right_margin(10);
        input.set_accepts_tab(false);
        let input_scroll = gtk::ScrolledWindow::builder().child(&input).hexpand(true).max_content_height(160).propagate_natural_height(true).build();
        input_scroll.add_css_class("composer-frame");
        let send = gtk::Button::with_label("Ask");
        send.add_css_class("primary");
        send.set_valign(gtk::Align::End);
        let row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        row.append(&input_scroll);
        row.append(&send);
        let status = gtk::Label::new(None);
        status.add_css_class("meta");
        status.set_xalign(0.0);
        status.set_wrap(true);
        let composer = gtk::Box::new(gtk::Orientation::Vertical, 6);
        composer.add_css_class("composer-bar");
        composer.append(&about_bar);
        composer.append(&row);
        composer.append(&status);

        let right = gtk::Box::new(gtk::Orientation::Vertical, 0);
        right.set_hexpand(true);
        right.append(&scroll);
        right.append(&composer);
        let root = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        root.append(&column);
        root.append(&right);

        let chats: Vec<Conversation> = std::fs::read(store_path()).ok().and_then(|b| serde_json::from_slice(&b).ok()).unwrap_or_default();
        let page = Rc::new(Self {
            root: root.upcast(),
            app: Rc::downgrade(app),
            list,
            convo,
            scroll,
            input,
            send,
            status,
            about_bar,
            about_label,
            chats: RefCell::new(chats),
            current: RefCell::new(None),
            pending_about: RefCell::new(None),
            live: RefCell::new(None),
            online: Cell::new(false),
        });

        let p = Rc::downgrade(&page);
        new.connect_clicked(move |_| if let Some(p) = p.upgrade() { p.go(None, None) });
        let p = Rc::downgrade(&page);
        page.send.connect_clicked(move |_| {
            if let Some(p) = p.upgrade() {
                if p.live.borrow().is_some() { p.stop() } else { p.ask() }
            }
        });
        let p = Rc::downgrade(&page);
        about_x.connect_clicked(move |_| {
            if let Some(p) = p.upgrade() {
                p.pending_about.replace(None);
                p.about_bar.set_visible(false);
            }
        });
        let keys = gtk::EventControllerKey::new();
        let p = Rc::downgrade(&page);
        keys.connect_key_pressed(move |_, key, _, state| {
            let Some(p) = p.upgrade() else { return glib::Propagation::Proceed };
            match key {
                gdk::Key::Return | gdk::Key::KP_Enter if !state.contains(gdk::ModifierType::SHIFT_MASK) => {
                    if p.live.borrow().is_none() {
                        p.ask();
                    }
                    glib::Propagation::Stop
                }
                gdk::Key::Escape if p.live.borrow().is_some() => {
                    p.stop();
                    glib::Propagation::Stop
                }
                _ => glib::Propagation::Proceed,
            }
        });
        page.input.add_controller(keys);
        let p = Rc::downgrade(&page);
        page.list.connect_row_activated(move |_, row| {
            if let Some(p) = p.upgrade() {
                p.go(Some(row.widget_name().to_string()), None);
            }
        });
        page.fill_list();
        // Answers are re-drawn from their Markdown at most every 60 ms while they stream.
        let p = Rc::downgrade(&page);
        glib::timeout_add_local(Duration::from_millis(60), move || {
            let Some(p) = p.upgrade() else { return glib::ControlFlow::Break };
            p.redraw_live();
            glib::ControlFlow::Continue
        });
        page
    }

    fn go(&self, id: Option<String>, about: Option<String>) {
        if let Some(app) = self.app.upgrade() {
            app.navigate(Route::Chat { id, about });
        }
    }

    /// Shows chat `id` (None: a new one), with `about` as the page to ask about.
    pub fn show(self: &Rc<Self>, id: Option<&str>, about: Option<&str>) {
        let exists = id.is_some_and(|id| self.chats.borrow().iter().any(|c| c.id == id));
        self.current.replace(if exists { id.map(str::to_owned) } else { None });
        self.pending_about.replace(about.map(str::to_owned));
        self.about_bar.set_visible(about.is_some());
        if let Some(a) = about {
            self.describe_about(a);
        }
        self.fill_list();
        self.draw();
        self.refresh_status();
        self.warm();
        let input = self.input.clone();
        glib::idle_add_local_once(move || {
            input.grab_focus();
        });
    }

    fn describe_about(self: &Rc<Self>, about: &str) {
        self.about_label.set_text("Asking about the page you were reading");
        let Some(app) = self.app.upgrade() else { return };
        let (kind, rest) = about.split_once(':').unwrap_or(("", ""));
        if kind != "doc" {
            return;
        }
        let (doc, page) = rest.rsplit_once(':').unwrap_or((rest, "1"));
        let (engine, label, doc, page) = (app.engine.clone(), self.about_label.clone(), doc.to_owned(), page.to_owned());
        glib::spawn_future_local(async move {
            if let Ok(d) = engine.get(&format!("/api/doc/{}", urlencoding::encode(&doc))).await {
                label.set_text(&format!("Asking about: {} · page {page}", d["title"].as_str().unwrap_or_default()));
            }
        });
    }

    fn warm(&self) {
        // Chat is open: the local AI starts loading now, so the first question doesn't wait for it.
        if let Some(app) = self.app.upgrade() {
            let _ = app.engine.request("POST", "/api/assistant/local", Some(&json!({"action": "warm"})));
        }
    }

    fn refresh_status(self: &Rc<Self>) {
        let Some(app) = self.app.upgrade() else { return };
        let engine = app.engine.clone();
        let me = Rc::downgrade(self);
        glib::spawn_future_local(async move {
            let Ok(s) = engine.get("/api/assistant/status").await else { return };
            let Some(me) = me.upgrade() else { return };
            let local = &s["local"];
            let state = local["state"].as_str().unwrap_or_default();
            me.online.set(s["online"].as_bool().unwrap_or(false) || matches!(state, "ready" | "stopped" | "loading"));
            let line = match state {
                "ready" => format!("{} is ready on this computer.", local["name"].as_str().unwrap_or("The local AI")),
                "loading" => format!("Starting the local AI{}…", local["pct"].as_i64().map(|p| format!(" ({p}%)")).unwrap_or_default()),
                "stopped" => "The local AI starts when you ask; answers come from the library and cite their pages.".into(),
                "checking" | "measuring" => "Checking the local AI's model file…".into(),
                "too_big" => format!("The local AI doesn't fit this computer: {}", local["message"].as_str().unwrap_or_default()),
                "off" => "The local AI is turned off. Questions show the matching pages instead.".into(),
                _ if s["online"].as_bool().unwrap_or(false) => format!("Local model {} is ready.", s["model"].as_str().unwrap_or_default()),
                _ => "No local AI on this computer: questions show the matching pages instead.".into(),
            };
            me.status.set_text(&line);
            if matches!(state, "loading" | "checking" | "measuring") {
                let me = Rc::downgrade(&me);
                glib::timeout_add_local_once(Duration::from_secs(1), move || {
                    if let Some(me) = me.upgrade() {
                        me.refresh_status();
                    }
                });
            }
        });
    }

    fn fill_list(&self) {
        clear(&self.list);
        let mut chats = self.chats.borrow().clone();
        chats.sort_by_key(|c| -c.updated);
        for c in chats.iter().take(200) {
            let row = gtk::ListBoxRow::new();
            row.add_css_class("sidebar-row");
            row.set_widget_name(&c.id);
            let b = gtk::Box::new(gtk::Orientation::Vertical, 2);
            let t = gtk::Label::new(Some(&c.title));
            t.set_xalign(0.0);
            t.set_ellipsize(gtk::pango::EllipsizeMode::End);
            let n = c.messages.iter().filter(|m| m.role == "user").count();
            let d = glib::DateTime::from_unix_local(c.updated).ok().and_then(|d| d.format("%b %e").ok()).map(|s| s.to_string()).unwrap_or_default();
            let m = gtk::Label::new(Some(&format!("{d} · {n} question{}", if n == 1 { "" } else { "s" })));
            m.add_css_class("meta");
            m.set_xalign(0.0);
            b.append(&t);
            b.append(&m);
            row.set_child(Some(&b));
            self.list.append(&row);
            if self.current.borrow().as_deref() == Some(&c.id) {
                self.list.select_row(Some(&row));
            }
        }
    }

    fn save(&self) {
        let path = store_path();
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        if let Ok(bytes) = serde_json::to_vec(&*self.chats.borrow()) {
            let tmp = path.with_extension("tmp");
            if std::fs::write(&tmp, bytes).is_ok() {
                let _ = std::fs::rename(&tmp, &path);
            }
        }
    }

    /// Draws the current conversation from its saved messages.
    fn draw(self: &Rc<Self>) {
        clear(&self.convo);
        let id = self.current.borrow().clone();
        let chat = id.and_then(|id| self.chats.borrow().iter().find(|c| c.id == id).cloned());
        let Some(chat) = chat else {
            let hello = gtk::Box::new(gtk::Orientation::Vertical, 8);
            hello.set_margin_top(40);
            let t = gtk::Label::new(Some("Ask the library"));
            t.add_css_class("page-title");
            t.set_xalign(0.0);
            let s = gtk::Label::new(Some("Answers come only from the manuals, guides, videos and encyclopedias on this computer, and every sentence links to the page it came from."));
            s.add_css_class("dim");
            s.set_wrap(true);
            s.set_xalign(0.0);
            hello.append(&t);
            hello.append(&s);
            for q in ["How do I make water safe to drink?", "Someone is bleeding badly from a leg wound", "How do I treat a second-degree burn?", "What should I do during an earthquake?"] {
                let b = crate::cards::wrap_button(q);
                b.add_css_class("chip");
                b.set_halign(gtk::Align::Start);
                let me = Rc::downgrade(self);
                let q = q.to_owned();
                b.connect_clicked(move |_| {
                    if let Some(me) = me.upgrade() {
                        me.input.buffer().set_text(&q);
                        me.ask();
                    }
                });
                hello.append(&b);
            }
            self.convo.append(&hello);
            self.send.set_label("Ask");
            return;
        };
        for m in &chat.messages {
            if m.role == "user" {
                self.convo.append(&user_bubble(&m.content));
            } else {
                let w = self.answer_widget();
                self.fill_answer(&w, m);
                self.convo.append(&w.0);
            }
        }
        self.scroll_to_end();
    }

    /// (frame, steps, answer, sources, note)
    fn answer_widget(&self) -> (gtk::Box, gtk::Box, gtk::Box, gtk::Box, gtk::Label) {
        let frame = gtk::Box::new(gtk::Orientation::Vertical, 8);
        frame.add_css_class("answer");
        let steps = gtk::Box::new(gtk::Orientation::Vertical, 2);
        steps.add_css_class("steps");
        let answer = gtk::Box::new(gtk::Orientation::Vertical, 6);
        let note = gtk::Label::new(None);
        note.add_css_class("meta");
        note.set_xalign(0.0);
        note.set_wrap(true);
        let sources = gtk::Box::new(gtk::Orientation::Vertical, 6);
        frame.append(&steps);
        frame.append(&answer);
        frame.append(&note);
        frame.append(&sources);
        (frame, steps, answer, sources, note)
    }

    fn fill_answer(self: &Rc<Self>, w: &(gtk::Box, gtk::Box, gtk::Box, gtk::Box, gtk::Label), m: &Msg) {
        let (_, steps, answer, sources, note) = w;
        for s in &m.steps {
            steps.append(&step_label(s));
        }
        let cited = m.cited.clone();
        let srcs = m.sources.clone();
        md::render(answer, &m.content, &|k| cited.iter().any(|c| c == k), self.citation_opener(srcs.clone()));
        if let Some(e) = &m.error {
            note.set_text(e);
            note.add_css_class("danger");
        }
        self.draw_sources(sources, &m.sources, &m.cited);
    }

    fn citation_opener(&self, sources: Vec<Value>) -> impl Fn(&str) + Clone + 'static {
        let app = self.app.clone();
        move |uri: &str| {
            let key = uri.trim_start_matches("cite:");
            if let (Some(app), Some(s)) = (app.upgrade(), sources.iter().find(|s| s["key"] == key))
                && let Some(r) = route_of(s)
            {
                app.navigate(r);
            }
        }
    }

    fn draw_sources(&self, into: &gtk::Box, sources: &[Value], cited: &[String]) {
        clear(into);
        let shown: Vec<&Value> = if cited.is_empty() {
            sources.iter().collect()
        } else {
            cited.iter().filter_map(|k| sources.iter().find(|s| s["key"] == k.as_str())).collect()
        };
        if shown.is_empty() {
            return;
        }
        into.append(&section(if cited.is_empty() { "Matching pages" } else { "Sources" }));
        for s in shown {
            let key = s["key"].as_str().unwrap_or_default().trim_start_matches('S').to_owned();
            let title = if key.is_empty() { s["title"].as_str().unwrap_or_default().to_owned() } else { format!("{key}  {}", s["title"].as_str().unwrap_or_default()) };
            let c = card(&title, &plain_markup(s["excerpt"].as_str().unwrap_or_default(), 220), &source_meta(s), s["trust"].as_str());
            c.add_css_class("source-card");
            if let Some(r) = route_of(s) {
                activate(&c, self.app.clone(), r);
            }
            into.append(&c);
        }
    }

    /// Asks `text` in the chat on screen (scripted checks: LASTBOOK_OPEN=chat:<question>).
    pub fn ask_text(self: &Rc<Self>, text: &str) {
        self.input.buffer().set_text(text);
        self.ask();
    }

    fn ask(self: &Rc<Self>) {
        let buf = self.input.buffer();
        let text = buf.text(&buf.start_iter(), &buf.end_iter(), false).trim().to_owned();
        if text.is_empty() || self.live.borrow().is_some() {
            return;
        }
        buf.set_text("");
        let Some(app) = self.app.upgrade() else { return };
        // A new chat gets an id and a title from its first question.
        let current = self.current.borrow().clone();   // not borrowed across the match: a new chat sets it
        let id = match current {
            Some(id) => id,
            None => {
                let id = format!("c{}", glib::real_time());
                let title: String = text.chars().take(80).collect();
                self.chats.borrow_mut().push(Conversation { id: id.clone(), title, updated: now(), about: self.pending_about.borrow().clone(), messages: vec![] });
                self.current.replace(Some(id.clone()));
                app.replace_route(Route::Chat { id: Some(id.clone()), about: None }, false);
                self.draw();
                id
            }
        };
        let (history, about) = {
            let mut chats = self.chats.borrow_mut();
            let c = chats.iter_mut().find(|c| c.id == id).expect("current chat");
            c.messages.push(Msg { role: "user".into(), content: text.clone(), ..Default::default() });
            c.updated = now();
            let history: Vec<Value> = c.messages.iter().filter(|m| m.error.is_none() || m.role == "user").map(|m| json!({"role": m.role, "content": m.content})).collect();
            (history, c.about.clone())
        };
        self.fill_list();
        if self.convo.first_child().is_some_and(|w| w.has_css_class("conversation-hello")) || self.chats.borrow().iter().find(|c| c.id == id).is_some_and(|c| c.messages.len() == 1) {
            clear(&self.convo);
        }
        self.convo.append(&user_bubble(&text));
        let w = self.answer_widget();
        self.convo.append(&w.0);
        let thinking = step_label("Searching the library…");
        w.1.append(&thinking);
        self.scroll_to_end();
        self.send.set_label("Stop");
        let (request, rx) = app.engine.request("POST", "/api/chat", Some(&json!({"messages": history, "about": about})));
        self.live.replace(Some(Live { request, chat: id.clone(), answer: w.2.clone(), steps: w.1.clone(), sources: w.3.clone(), note: w.4.clone(), text: String::new(), dirty: false }));
        let me = Rc::downgrade(self);
        glib::spawn_future_local(async move {
            let mut buf = String::new();
            let mut msg = Msg { role: "assistant".into(), ..Default::default() };
            let mut status = 0u16;
            while let Ok(piece) = rx.recv().await {
                let Some(me) = me.upgrade() else { return };
                match piece {
                    Piece::Head(s, _) => status = s,
                    Piece::Bytes(_) => {}
                    Piece::Chunk(c) => {
                        buf.push_str(&c);
                        while let Some(i) = buf.find("\n\n") {
                            let event: String = buf.drain(..i + 2).collect();
                            for line in event.lines() {
                                if let Some(data) = line.strip_prefix("data: ")
                                    && let Ok(ev) = serde_json::from_str::<Value>(data)
                                {
                                    me.on_event(&ev, &mut msg, &thinking);
                                }
                            }
                        }
                    }
                    Piece::End(_) => break,
                }
            }
            let Some(me) = me.upgrade() else { return };
            if status != 200 && msg.error.is_none() && msg.content.is_empty() {
                msg.error = Some(format!("The engine refused the question ({status})."));
            }
            me.finish(msg).await;
        });
    }

    fn on_event(self: &Rc<Self>, ev: &Value, msg: &mut Msg, thinking: &gtk::Label) {
        let live = self.live.borrow();
        let Some(l) = live.as_ref() else { return };
        match ev["type"].as_str().unwrap_or_default() {
            "found" => thinking.set_text(&format!("Matched {} passage{} for your question", ev["count"], if ev["count"] == 1 { "" } else { "s" })),
            "sources" => msg.sources = ev["sources"].as_array().cloned().unwrap_or_default(),
            "state" => {
                let s = ev["state"].as_str().unwrap_or_default();
                let text = match s {
                    "starting" => format!("Starting the local AI{}…", ev["pct"].as_i64().map(|p| format!(" ({p}%)")).unwrap_or_default()),
                    "thinking" => "Thinking…".into(),
                    "reading" => "Reading the sources…".into(),
                    "searching" => "Searching…".into(),
                    "writing" => "Writing…".into(),
                    _ => String::new(),
                };
                if !text.is_empty() {
                    l.note.set_text(&text);
                }
            }
            "tool" if ev["phase"] == "done" => {
                let s = step_text(ev);
                l.steps.append(&step_label(&s));
                msg.steps.push(s);
            }
            "delta" => {
                drop(live);
                if let Some(l) = self.live.borrow_mut().as_mut() {
                    l.text.push_str(ev["text"].as_str().unwrap_or_default());
                    l.dirty = true;
                }
                return;
            }
            "reset" => {
                drop(live);
                if let Some(l) = self.live.borrow_mut().as_mut() {
                    l.text.clear();
                    l.dirty = true;
                }
                return;
            }
            "done" => {
                msg.content = ev["answer"].as_str().unwrap_or_default().to_owned();
                let cited: Vec<Value> = ev["sources"].as_array().cloned().unwrap_or_default();
                msg.cited = cited.iter().filter_map(|s| s["key"].as_str().map(str::to_owned)).collect();
                let mut all = cited.clone();
                all.extend(ev["consulted"].as_array().cloned().unwrap_or_default());
                if !all.is_empty() {
                    msg.sources = all;
                }
                if let Some(t) = ev["latency"]["total_s"].as_f64() {
                    msg.steps.push(format!("Answered in {t:.0} s"));
                }
            }
            "error" => {
                msg.error = Some(ev["text"].as_str().unwrap_or("The assistant stopped.").to_owned());
                if let Some(s) = ev["sources"].as_array() {
                    msg.sources = s.clone();
                }
                if ev["error"] == "model_unavailable" {
                    msg.steps.push("No local AI answered: here are the matching pages.".into());
                }
            }
            "stopped" => msg.error = Some("Stopped.".into()),
            _ => {}
        }
    }

    fn redraw_live(self: &Rc<Self>) {
        let (text, answer, sources) = {
            let mut live = self.live.borrow_mut();
            let Some(l) = live.as_mut() else { return };
            if !l.dirty {
                return;
            }
            l.dirty = false;
            (l.text.clone(), l.answer.clone(), l.sources.clone())
        };
        let _ = sources;
        md::render(&answer, &text, &|_| false, |_| {});
        self.scroll_to_end();
    }

    async fn finish(self: &Rc<Self>, mut msg: Msg) {
        let Some(l) = self.live.take() else { return };
        // Without a model the engine answers with an error and no pages: show the matching pages instead.
        if msg.content.is_empty()
            && msg.sources.is_empty()
            && let Some(app) = self.app.upgrade()
        {
            let q = self.chats.borrow().iter().find(|c| c.id == l.chat).and_then(|c| c.messages.iter().rev().find(|m| m.role == "user").map(|m| m.content.clone())).unwrap_or_default();
            if let Ok(r) = app.engine.get(&query("/api/retrieve", &[("q", &q), ("k", "6")])).await {
                msg.sources = r["results"].as_array().cloned().unwrap_or_default();
            }
        }
        clear(&l.steps);
        l.note.set_text("");
        let w = (gtk::Box::new(gtk::Orientation::Vertical, 0), l.steps.clone(), l.answer.clone(), l.sources.clone(), l.note.clone());
        self.fill_answer(&w, &msg);
        {
            let mut chats = self.chats.borrow_mut();
            if let Some(c) = chats.iter_mut().find(|c| c.id == l.chat) {
                c.messages.push(msg);
                c.updated = now();
            }
        }
        self.save();
        self.fill_list();
        self.send.set_label("Ask");
        self.refresh_status();
    }

    fn stop(&self) {
        if let (Some(app), Some(l)) = (self.app.upgrade(), self.live.borrow().as_ref()) {
            app.engine.cancel(l.request);
        }
    }

    fn scroll_to_end(&self) {
        let adj = self.scroll.vadjustment();
        glib::idle_add_local_once(move || adj.set_value(adj.upper()));
    }
}

fn user_bubble(text: &str) -> gtk::Widget {
    let l = gtk::Label::new(Some(text));
    l.add_css_class("question");
    l.set_wrap(true);
    l.set_xalign(0.0);
    l.set_selectable(true);
    l.set_halign(gtk::Align::End);
    l.set_max_width_chars(70);
    l.upcast()
}

fn step_label(text: &str) -> gtk::Label {
    let l = gtk::Label::new(Some(text));
    l.add_css_class("step");
    l.set_xalign(0.0);
    l.set_wrap(true);
    l
}

/// What a tool call did, in a few words (as the web page said it).
fn step_text(ev: &Value) -> String {
    let (a, r) = (&ev["args"], &ev["summary"]);
    let s = |v: &Value| v.as_str().unwrap_or_default().to_owned();
    match ev["name"].as_str().unwrap_or_default() {
        "search_library" => format!("Searched “{}” — {} passage{}", s(&a["query"]), r["count"], if r["count"] == 1 { "" } else { "s" }),
        "read_pages" if r["error"].is_null() => format!("Read {}, page {}", s(&r["title"]), a["pages"].as_array().map(|p| p.iter().map(|x| x.to_string()).collect::<Vec<_>>().join(", ")).unwrap_or_default()),
        "read_article" if r["error"].is_null() => format!("Read the encyclopedia article “{}”", s(&r["title"])),
        "get_timed_transcript" if r["error"].is_null() => format!("Read the transcript of {}", s(&r["title"])),
        "lookup_place" => format!("Looked up “{}” on the offline map", s(&a["query"])),
        "view_page" => format!("Looked at the picture of {}, page {}", s(&r["title"]), a["page"]),
        name => format!("{name} (nothing found)"),
    }
}
