// SPDX-License-Identifier: MIT
//! Setup: choosing and downloading a library, the local AI and a detailed map (engine/setup/api.py, docs/SETUP-API.md).
//!
//! First run fills the window; with a library open, the same page manages it (About → Library and downloads). Nothing
//! reaches the network until the person presses Download. Progress is read once a second while a job runs. When the
//! job is done, Open asks the engine to finish, and the engine asks this program to restart on the new library.

use std::{
    cell::{Cell, RefCell},
    rc::Rc,
    sync::Arc,
    time::Duration,
};

use gtk::{gio, glib, prelude::*};
use serde_json::{Value, json};

use crate::{cards::clear, engine::Engine};

pub struct SetupPage {
    pub root: gtk::Widget,
    engine: Arc<Engine>,
    window: RefCell<Option<gtk::Window>>,
    body: gtk::Box,
    job_box: gtk::Box,
    status: gtk::Label,
    state: RefCell<Value>,
    pack: RefCell<Option<String>>,
    map: RefCell<Option<String>>,
    ai: Cell<bool>,
    folder: RefCell<Option<String>>,
    polling: Cell<bool>,
    /// The local AI's model was found (in the library or the Hugging Face cache), so there is nothing to download.
    ai_present: Cell<bool>,
}

fn gb(bytes: f64) -> String {
    if bytes >= 1e9 { format!("{:.1} GB", bytes / 1e9) } else { format!("{:.0} MB", bytes / 1e6) }
}

fn label(text: &str, class: &str) -> gtk::Label {
    let l = gtk::Label::new(Some(text));
    l.add_css_class(class);
    l.set_xalign(0.0);
    l.set_wrap(true);
    l
}

impl SetupPage {
    pub fn build(engine: Arc<Engine>) -> Rc<Self> {
        let body = gtk::Box::new(gtk::Orientation::Vertical, 12);
        body.set_margin_top(28);
        body.set_margin_bottom(28);
        body.set_margin_start(32);
        body.set_margin_end(32);
        let status = label("", "meta");
        let job_box = gtk::Box::new(gtk::Orientation::Vertical, 8);
        let outer = gtk::Box::new(gtk::Orientation::Vertical, 0);
        outer.append(&body);
        let scroll = gtk::ScrolledWindow::builder().child(&outer).vexpand(true).hscrollbar_policy(gtk::PolicyType::Never).build();
        Rc::new(Self {
            root: scroll.upcast(),
            engine,
            window: RefCell::new(None),
            body,
            job_box,
            status,
            state: RefCell::new(Value::Null),
            pack: RefCell::new(None),
            map: RefCell::new(None),
            ai: Cell::new(false),
            folder: RefCell::new(None),
            polling: Cell::new(false),
            ai_present: Cell::new(false),
        })
    }

    pub fn set_window(&self, w: &impl IsA<gtk::Window>) {
        self.window.replace(Some(w.clone().upcast()));
    }

    /// Reads the engine's setup state and draws the page.
    pub fn refresh(self: &Rc<Self>) {
        let (engine, me) = (self.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let Ok(st) = engine.get("/api/setup/state").await else { return };
            // With a library open, the assistant knows whether its model is on this computer (setup only looks in the library).
            let local = engine.get("/api/assistant/status").await.ok().map(|s| s["local"]["state"].as_str().unwrap_or_default().to_owned());
            if let Some(me) = me.upgrade() {
                me.ai_present.set(local.as_deref().is_some_and(|s| matches!(s, "ready" | "stopped" | "loading" | "checking" | "measuring")));
                me.draw(st);
            }
        });
    }

    fn draw(self: &Rc<Self>, st: Value) {
        clear(&self.body);
        let first = st["mode"] == "first-run";
        let rec = &st["recommend"];
        if self.pack.borrow().is_none() {
            self.pack.replace(rec["pack"].as_str().map(str::to_owned).or_else(|| st["current"]["pack"].as_str().map(str::to_owned)));
            // Only first-run setup suggests the model; managing a library never ticks a 17 GB download by itself.
            self.ai.set(first && rec["ai"].as_bool().unwrap_or(false));
        }
        if self.folder.borrow().is_none() {
            self.folder.replace(rec["folder"].as_str().map(str::to_owned));
        }
        self.body.append(&label(if first { "Set up Lastbook" } else { "Library and downloads" }, "page-title"));
        self.body.append(&label(
            if first {
                "Choose what to keep on this computer. Everything is checked as it arrives, and after this Lastbook works with no internet."
            } else {
                "Add to this library, change its detailed map, add the local AI, or check every file."
            },
            "dim",
        ));
        if let Some(c) = st["current"].as_object() {
            self.body.append(&label(
                &format!(
                    "This library: {} · map {} · local AI {} · {}",
                    c.get("pack").and_then(Value::as_str).unwrap_or("—"),
                    c.get("map").and_then(Value::as_str).unwrap_or("base"),
                    if c.get("model").is_some_and(|m| !m.is_null()) { "installed" } else { "not installed" },
                    c.get("folder").and_then(Value::as_str).unwrap_or_default()
                ),
                "meta",
            ));
        }

        // Packs
        self.body.append(&crate::cards::section("Library"));
        let need = &rec["need_bytes"];
        let mut group: Option<gtk::CheckButton> = None;
        for p in st["manifest"]["packs"].as_array().into_iter().flatten() {
            let id = p["id"].as_str().unwrap_or_default().to_owned();
            let row = gtk::CheckButton::new();
            if let Some(g) = &group {
                row.set_group(Some(g));
            } else {
                group = Some(row.clone());
            }
            let text = gtk::Box::new(gtk::Orientation::Vertical, 2);
            let mut head = format!("{} · {}", p["title"].as_str().unwrap_or(&id), gb(p["installed_bytes"].as_f64().unwrap_or(0.0)));
            if rec["pack"] == id.as_str() {
                head.push_str("  (recommended)");
            }
            text.append(&label(&head, "result-title"));
            text.append(&label(p["summary"].as_str().unwrap_or_default(), "dim"));
            if let Some(n) = need[&id].as_f64() {
                text.append(&label(&format!("Needs {} free while it downloads and unpacks", gb(n)), "meta"));
            }
            row.set_child(Some(&text));
            row.add_css_class("choice-row");
            row.set_sensitive(p["available"].as_bool().unwrap_or(true));
            row.set_active(self.pack.borrow().as_deref() == Some(&id));
            let me = Rc::downgrade(self);
            row.connect_toggled(move |b| {
                if b.is_active()
                    && let Some(me) = me.upgrade()
                {
                    me.pack.replace(Some(id.clone()));
                }
            });
            self.body.append(&row);
        }
        if !first {
            let none = gtk::CheckButton::with_label("Keep the library as it is");
            if let Some(g) = &group {
                none.set_group(Some(g));
            }
            none.add_css_class("choice-row");
            crate::cards::wrap_check(&none);
            let me = Rc::downgrade(self);
            none.connect_toggled(move |b| {
                if b.is_active()
                    && let Some(me) = me.upgrade()
                {
                    me.pack.replace(None);
                }
            });
            none.set_active(self.pack.borrow().is_none() || self.pack.borrow().as_deref() == st["current"]["pack"].as_str());
            if none.is_active() {
                self.pack.replace(None);
            }
            self.body.append(&none);
        }

        // Local AI
        self.body.append(&crate::cards::section("Local AI"));
        let ai = &st["hardware"]["ai"];
        let model = st["manifest"]["models"].as_array().and_then(|m| m.first()).cloned().unwrap_or_default();
        let sw = gtk::CheckButton::with_label(&format!(
            "Download {} ({}) to answer questions with citations",
            model["title"].as_str().unwrap_or("the local AI"),
            gb(model["size"].as_f64().unwrap_or(0.0))
        ));
        sw.add_css_class("choice-row");
        crate::cards::wrap_check(&sw);
        sw.set_active(self.ai.get());
        let present = self.ai_present.get() || !st["current"]["model"].is_null();
        if present {
            sw.set_label(Some("The local AI is already on this computer"));
            sw.set_active(false);
            self.ai.set(false);
        }
        sw.set_sensitive(ai["fits"].as_bool().unwrap_or(false) && !present);
        let me = Rc::downgrade(self);
        sw.connect_toggled(move |b| {
            if let Some(me) = me.upgrade() {
                me.ai.set(b.is_active());
            }
        });
        self.body.append(&sw);
        self.body.append(&label(ai["why"].as_str().unwrap_or_default(), "meta"));

        // Detailed map
        let maps: Vec<Value> = st["manifest"]["maps"].as_array().cloned().unwrap_or_default();
        if !maps.is_empty() {
            self.body.append(&crate::cards::section("Detailed map"));
            let mut names = vec!["No detailed map (the base map only)".to_owned()];
            names.extend(maps.iter().map(|m| {
                format!("{} · {}", m["title"].as_str().unwrap_or_default(), gb(m["installed_bytes"].as_f64().unwrap_or(0.0)))
            }));
            let refs: Vec<&str> = names.iter().map(String::as_str).collect();
            let dd = gtk::DropDown::from_strings(&refs);
            dd.set_enable_search(true);
            dd.add_css_class("map-choice");
            let current = self.map.borrow().clone();
            if let Some(i) = current.and_then(|c| maps.iter().position(|m| m["id"] == c.as_str())) {
                dd.set_selected(i as u32 + 1);
            }
            let me = Rc::downgrade(self);
            let ids: Vec<String> = maps.iter().map(|m| m["id"].as_str().unwrap_or_default().to_owned()).collect();
            dd.connect_selected_notify(move |d| {
                if let Some(me) = me.upgrade() {
                    let i = d.selected() as usize;
                    me.map.replace(if i == 0 { None } else { ids.get(i - 1).cloned() });
                }
            });
            self.body.append(&dd);
            self.body.append(&label("Local streets, buildings, terrain and places for one area. You choose it: Lastbook never detects your location.", "meta"));
        }

        // Where
        if first {
            self.body.append(&crate::cards::section("Where to keep it"));
            let row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
            let path = label(self.folder.borrow().as_deref().unwrap_or_default(), "meta");
            path.set_wrap_mode(gtk::pango::WrapMode::Char);
            path.set_hexpand(true);
            let choose = gtk::Button::with_label("Choose folder…");
            choose.add_css_class("chip");
            let me = Rc::downgrade(self);
            let pl = path.clone();
            choose.connect_clicked(move |_| {
                if let Some(me) = me.upgrade() {
                    me.choose_folder(pl.clone());
                }
            });
            row.append(&path);
            row.append(&choose);
            self.body.append(&row);
            let use_existing = crate::cards::wrap_button("Use a library or pack already on this computer…");
            use_existing.add_css_class("chip");
            use_existing.set_halign(gtk::Align::Start);
            let me = Rc::downgrade(self);
            use_existing.connect_clicked(move |_| {
                if let Some(me) = me.upgrade() {
                    me.use_existing();
                }
            });
            self.body.append(&use_existing);
            for f in st["found"].as_array().into_iter().flatten() {
                let b = crate::cards::wrap_button(&format!("Use {} found at {}", f["kind"].as_str().unwrap_or("library"), f["path"].as_str().unwrap_or_default()));
                b.add_css_class("chip");
                b.set_halign(gtk::Align::Start);
                let (me, p) = (Rc::downgrade(self), f["path"].as_str().unwrap_or_default().to_owned());
                b.connect_clicked(move |_| {
                    if let Some(me) = me.upgrade() {
                        me.post("/api/setup/use", json!({"path": p}));
                    }
                });
                self.body.append(&b);
            }
        }

        let actions = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        actions.set_margin_top(12);
        let start = gtk::Button::with_label(if first { "Download and set up" } else { "Download" });
        start.add_css_class("primary");
        let me = Rc::downgrade(self);
        start.connect_clicked(move |_| {
            if let Some(me) = me.upgrade() {
                me.start();
            }
        });
        actions.append(&start);
        if !first {
            let repair = gtk::Button::with_label("Check every file");
            repair.add_css_class("chip");
            let me = Rc::downgrade(self);
            repair.connect_clicked(move |_| {
                if let Some(me) = me.upgrade() {
                    me.post("/api/setup/start", json!({"repair": true, "ai": false}));
                }
            });
            actions.append(&repair);
        }
        self.body.append(&actions);
        self.body.append(&self.status);
        self.body.append(&self.job_box);
        self.state.replace(st.clone());
        self.draw_job(&st["job"]);
        if st["job"]["state"].as_str().is_some_and(|s| !matches!(s, "done" | "failed" | "cancelled")) {
            self.poll();
        }
    }

    fn choose_folder(self: &Rc<Self>, shown: gtk::Label) {
        let dialog = gtk::FileDialog::builder().title("Where should the library go?").modal(true).build();
        let me = Rc::downgrade(self);
        let parent = self.window.borrow().clone();
        dialog.select_folder(parent.as_ref(), gio::Cancellable::NONE, move |r| {
            if let (Ok(f), Some(me)) = (r, me.upgrade())
                && let Some(p) = f.path()
            {
                let p = p.to_string_lossy().into_owned();
                shown.set_text(&p);
                me.folder.replace(Some(p));
            }
        });
    }

    fn use_existing(self: &Rc<Self>) {
        let dialog = gtk::FileDialog::builder().title("Choose a LastbookData folder or a pack zip").modal(true).build();
        let me = Rc::downgrade(self);
        let parent = self.window.borrow().clone();
        dialog.select_folder(parent.as_ref(), gio::Cancellable::NONE, move |r| {
            if let (Ok(f), Some(me)) = (r, me.upgrade())
                && let Some(p) = f.path()
            {
                me.post("/api/setup/use", json!({"path": p.to_string_lossy()}));
            }
        });
    }

    fn start(self: &Rc<Self>) {
        let mut body = json!({"pack": *self.pack.borrow(), "ai": self.ai.get(), "map": *self.map.borrow()});
        if let Some(f) = self.folder.borrow().as_ref()
            && self.state.borrow()["mode"] == "first-run"
        {
            body["folder"] = json!(f);
        }
        if body["pack"].is_null() && !self.ai.get() && body["map"].is_null() {
            self.status.set_text("Choose something to download first.");
            return;
        }
        self.post("/api/setup/start", body);
    }

    fn post(self: &Rc<Self>, path: &'static str, body: Value) {
        let (engine, me, path) = (self.engine.clone(), Rc::downgrade(self), path.to_owned());
        glib::spawn_future_local(async move {
            let r = engine.post(&path, &body).await;
            let Some(me) = me.upgrade() else { return };
            match r {
                Ok(v) if v.get("error").is_some() => {
                    let mut t = v["error"].as_str().unwrap_or("Setup could not start.").to_owned();
                    if let (Some(n), Some(f)) = (v["need_bytes"].as_f64(), v["free_bytes"].as_f64()) {
                        t.push_str(&format!(" It needs {} and {} is free.", gb(n), gb(f)));
                    }
                    me.status.set_text(&t);
                    me.status.add_css_class("danger");
                }
                Ok(v) => {
                    me.status.remove_css_class("danger");
                    me.status.set_text("");
                    if v.get("items").is_some() {
                        me.draw_job(&v);
                    }
                    me.poll();
                }
                Err(e) => me.status.set_text(&e.to_string()),
            }
        });
    }

    fn poll(self: &Rc<Self>) {
        if self.polling.replace(true) {
            return;
        }
        let me = Rc::downgrade(self);
        glib::timeout_add_local(Duration::from_secs(1), move || {
            let Some(me) = me.upgrade() else { return glib::ControlFlow::Break };
            let (engine, weak) = (me.engine.clone(), Rc::downgrade(&me));
            glib::spawn_future_local(async move {
                if let Ok(job) = engine.get("/api/setup/job").await
                    && let Some(me) = weak.upgrade()
                {
                    me.draw_job(&job);
                    if job["state"].as_str().is_none_or(|s| matches!(s, "done" | "failed" | "cancelled")) {
                        me.polling.set(false);
                    }
                }
            });
            if me.polling.get() { glib::ControlFlow::Continue } else { glib::ControlFlow::Break }
        });
    }

    fn draw_job(self: &Rc<Self>, job: &Value) {
        clear(&self.job_box);
        let Some(items) = job["items"].as_array().filter(|i| !i.is_empty()) else { return };
        self.job_box.append(&crate::cards::section("Progress"));
        for it in items {
            let row = gtk::Box::new(gtk::Orientation::Vertical, 4);
            row.add_css_class("result");
            let state = it["state"].as_str().unwrap_or_default();
            let (done, total) = (it["bytes_done"].as_f64().unwrap_or(0.0), it["bytes_total"].as_f64().unwrap_or(0.0));
            let mut line = format!("{} — {state}", it["title"].as_str().unwrap_or_default());
            if total > 0.0 {
                line.push_str(&format!(" · {} of {}", gb(done), gb(total)));
            }
            if let Some(r) = it["rate_bps"].as_f64().filter(|r| *r > 0.0) {
                line.push_str(&format!(" · {}/s", gb(r)));
            }
            if let Some(e) = it["eta_s"].as_f64().filter(|e| *e > 0.0) {
                line.push_str(&format!(" · about {} min left", (e / 60.0).ceil()));
            }
            row.append(&label(&line, "result-title"));
            let bar = gtk::ProgressBar::new();
            bar.set_fraction(if total > 0.0 { (done / total).clamp(0.0, 1.0) } else if state == "done" { 1.0 } else { 0.0 });
            row.append(&bar);
            if let Some(e) = it["error"].as_str() {
                let l = label(e, "meta");
                l.add_css_class("danger");
                row.append(&l);
            }
            self.job_box.append(&row);
        }
        let state = job["state"].as_str().unwrap_or_default();
        let buttons = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        let mut add = |text: &str, path: &'static str, primary: bool| {
            let b = gtk::Button::with_label(text);
            b.add_css_class(if primary { "primary" } else { "chip" });
            let me = Rc::downgrade(self);
            b.connect_clicked(move |_| {
                if let Some(me) = me.upgrade() {
                    me.post(path, json!({}));
                }
            });
            buttons.append(&b);
        };
        match state {
            "done" => add("Open the library", "/api/setup/finish", true),
            "failed" => {
                add("Try again", "/api/setup/retry", true);
                add("Cancel", "/api/setup/cancel", false);
            }
            "paused" => {
                add("Resume", "/api/setup/resume", true);
                add("Cancel", "/api/setup/cancel", false);
            }
            "cancelled" => {}
            _ => {
                add("Pause", "/api/setup/pause", false);
                add("Cancel", "/api/setup/cancel", false);
            }
        }
        if let Some(e) = job["error"].as_str() {
            let l = label(e, "meta");
            l.add_css_class("danger");
            self.job_box.append(&l);
        }
        self.job_box.append(&buttons);
    }
}

/// When the engine says setup has finished, start this program again on the new library and leave.
pub fn restart_on_notice(app: &gtk::Application, engine: &Arc<Engine>) {
    let (rx, app) = (engine.notices.clone(), app.downgrade());
    glib::spawn_future_local(async move {
        while let Ok(msg) = rx.recv().await {
            if let Some(folder) = msg["restart"].as_str() {
                if let Ok(exe) = std::env::current_exe() {
                    let _ = std::process::Command::new(exe).arg("--data").arg(folder).spawn();
                }
                if let Some(app) = app.upgrade() {
                    app.quit();
                }
                return;
            }
        }
    });
}
