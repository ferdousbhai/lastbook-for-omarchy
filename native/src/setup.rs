// SPDX-License-Identifier: MIT
//! Setup: choosing and downloading a library and the map (engine/setup/api.py, docs/SETUP-API.md).
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
use omui::{clear, label};
use serde_json::{Value, json};

use crate::engine::Engine;

pub struct SetupPage {
    pub root: gtk::Widget,
    engine: Arc<Engine>,
    window: RefCell<Option<gtk::Window>>,
    body: gtk::Box,
    job_box: gtk::Box,
    /// The job's buttons, kept across the once-a-second redraw (a keyboard user's focus stays on them) and made again
    /// only when what they offer changes (`job_buttons_for`).
    job_buttons: gtk::Box,
    job_buttons_for: RefCell<String>,
    status: gtk::Label,
    state: RefCell<Value>,
    /// The library size to download; None keeps the library there is.
    pack: RefCell<Option<String>>,
    /// The choices have their starting values (set once, from the first state read).
    chosen: Cell<bool>,
    /// The world map and place search (cut from OpenStreetMap on this computer, engine/setup/maps.py).
    world: Cell<bool>,
    /// Street-level areas to add (Geofabrik region ids).
    regions: RefCell<Vec<String>>,
    folder: RefCell<Option<String>>,
    polling: Cell<bool>,
    poll_turn: Cell<u32>,
}

/// About how many street-level (zoom 15) map tiles a region's box [west, south, east, north] holds.
fn street_tiles(bbox: &Value) -> f64 {
    let v = |i: usize| bbox[i].as_f64().unwrap_or(0.0);
    let (w, s, e, n) = (v(0), v(1).max(-85.05), v(2), v(3).min(85.05));
    let row = |lat: f64| (1.0 - lat.to_radians().tan().asinh() / std::f64::consts::PI) / 2.0;
    let wide = if e >= w { e - w } else { e - w + 360.0 } / 360.0;
    wide * (row(s) - row(n)) * 4f64.powi(15)
}

fn gb(bytes: f64) -> String {
    if bytes >= 1e9 { format!("{:.1} GB", bytes / 1e9) } else { format!("{:.0} MB", bytes / 1e6) }
}

impl SetupPage {
    pub fn build(engine: Arc<Engine>) -> Rc<Self> {
        let (scroll, body) = crate::cards::column(12);
        let status = label("", "meta");
        let job_box = gtk::Box::new(gtk::Orientation::Vertical, 8);
        Rc::new(Self {
            root: scroll.upcast(),
            engine,
            window: RefCell::new(None),
            body,
            job_box,
            job_buttons: gtk::Box::new(gtk::Orientation::Horizontal, 8),
            job_buttons_for: RefCell::new(String::new()),
            status,
            state: RefCell::new(Value::Null),
            pack: RefCell::new(None),
            chosen: Cell::new(false),
            world: Cell::new(true),
            regions: RefCell::new(Vec::new()),
            folder: RefCell::new(None),
            polling: Cell::new(false),
            poll_turn: Cell::new(0),
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
            if let Some(me) = me.upgrade() {
                me.draw(st);
            }
        });
    }

    /// Draws the page again from the state last read (a choice changed what it shows).
    fn redraw(self: &Rc<Self>) {
        let st = self.state.borrow().clone();
        self.draw(st);
    }

    fn draw(self: &Rc<Self>, st: Value) {
        clear(&self.body);
        let first = st["mode"] == "first-run";
        let rec = &st["recommend"];
        if !self.chosen.replace(true) {
            // Only first-run setup suggests downloads; managing a library starts from "keep it as it is", so adding a
            // map never swaps the library for another size.
            self.pack.replace(if first { rec["pack"].as_str().map(str::to_owned) } else { None });
            self.world.set(first);
        }
        if self.folder.borrow().is_none() {
            self.folder.replace(rec["folder"].as_str().map(str::to_owned));
        }
        self.body.append(&label(if first { "Set up Lastbook" } else { "Library and downloads" }, "page-title"));
        if let Some(c) = st["current"].as_object() {
            self.body.append(&label(
                &format!(
                    "This library: {} · {}",
                    c.get("pack").and_then(Value::as_str).unwrap_or("—"),
                    c.get("folder").and_then(Value::as_str).unwrap_or_default()
                ),
                "meta",
            ));
        }

        // Packs
        self.body.append(&crate::cards::section("Library"));
        // Nothing suggested (no library fits the disk, or none is ready): the engine says why.
        if first && rec["pack"].is_null() && let Some(why) = rec["why"].as_str() {
            let l = label(why, "meta");
            l.add_css_class("error");
            self.body.append(&l);
        }
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
            text.append(&label(p["summary"].as_str().unwrap_or_default(), "dim-label"));
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
            let keep = self.pack.borrow().is_none();   // read first: ticking it writes the choice
            none.set_active(keep);
            self.body.append(&none);
        }

        self.draw_maps(&st);

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
        start.add_css_class("suggested-action");
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
                    me.post("/api/setup/start", json!({"repair": true}));
                }
            });
            actions.append(&repair);
        }
        self.body.append(&actions);
        self.body.append(&self.status);
        self.body.append(&self.job_box);
        self.state.replace(st.clone());
        self.draw_job(&st["job"]);
        if st["job"]["state"].as_str().is_some_and(|s| !matches!(s, "done" | "failed")) {
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
        let dialog = gtk::FileDialog::builder().title("Choose a LastbookData folder, or the folder that holds a pack zip").modal(true).build();
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

    /// The map: the world to city level, and street-level areas the person picks. Both are made on this computer from
    /// OpenStreetMap's newest daily build; Lastbook hosts no maps.
    fn draw_maps(self: &Rc<Self>, st: &Value) {
        self.body.append(&crate::cards::section("Map"));
        let have = &st["current"]["osm"];
        let has_world = have["world"].as_bool().unwrap_or(false) && have["places"].as_bool().unwrap_or(false);
        let all: Vec<Value> = st["map_regions"].as_array().cloned().unwrap_or_default();
        let name_of_all = all.clone();
        let name_of = move |id: &str| name_of_all.iter().find(|r| r["id"] == id).and_then(|r| r["name"].as_str()).unwrap_or(id).to_owned();
        // Offered are countries and their parts. Continents, and countries so large that street level runs to tens of
        // GB (Russia, the US, Canada, Australia), are left out: their parts are in the list.
        let ids: Vec<String> = all.iter().filter_map(|r| r["id"].as_str().map(str::to_owned)).collect();
        let parents: std::collections::HashSet<String> = all.iter().filter_map(|r| r["parent"].as_str().map(str::to_owned)).collect();
        let mut all: Vec<Value> = all
            .into_iter()
            .filter(|r| {
                let id = r["id"].as_str().unwrap_or_default();
                let top = r["parent"].as_str().is_none_or(str::is_empty);
                let has_parts = parents.contains(id) || ids.iter().any(|i| i.strip_prefix(id).is_some_and(|rest| rest.starts_with('/')));
                !top && !(has_parts && street_tiles(&r["bbox"]) > 40e6)
            })
            .collect();
        all.sort_by_key(|r| r["name"].as_str().unwrap_or_default().to_lowercase());
        if has_world {
            let installed: Vec<String> = have["regions"].as_array().into_iter().flatten()
                .map(|r| format!("{} ({})", r["name"].as_str().unwrap_or_default(), gb(r["bytes"].as_f64().unwrap_or(0.0))))
                .collect();
            self.body.append(&label(&if installed.is_empty() {
                "The world map is here, to city level.".to_owned()
            } else {
                format!("The world map is here, with streets for {}.", installed.join(", "))
            }, "meta"));
            self.world.set(false);
        } else {
            let check = gtk::CheckButton::with_label(&format!(
                "World map and place search, to city level · about {}",
                gb(st["world_bytes"].as_f64().unwrap_or(650e6))
            ));
            crate::cards::wrap_check(&check);
            check.set_active(self.world.get());
            let me = Rc::downgrade(self);
            check.connect_toggled(move |b| {
                if let Some(me) = me.upgrade() {
                    me.world.set(b.is_active());
                }
            });
            self.body.append(&check);
        }
        if all.is_empty() {
            return;
        }
        // Street-level areas: pick from a searchable list; each pick shows as a chip that removes it when clicked.
        let names: Vec<String> = std::iter::once("Add streets for an area…".to_owned())
            .chain(all.iter().map(|r| {
                let parent = r["parent"].as_str().unwrap_or_default();
                if parent.is_empty() { r["name"].as_str().unwrap_or_default().to_owned() } else { format!("{} · {}", r["name"].as_str().unwrap_or_default(), name_of(parent)) }
            }))
            .collect();
        let refs: Vec<&str> = names.iter().map(String::as_str).collect();
        let dd = gtk::DropDown::from_strings(&refs);
        // Its first line says what it is for; a screen reader hears the same.
        dd.update_property(&[gtk::accessible::Property::Label("Add streets for an area")]);
        dd.set_enable_search(true);
        dd.set_search_match_mode(gtk::StringFilterMatchMode::Substring);
        dd.set_expression(Some(gtk::PropertyExpression::new(gtk::StringObject::static_type(), gtk::Expression::NONE, "string")));
        let chips = gtk::FlowBox::new();
        chips.set_selection_mode(gtk::SelectionMode::None);
        chips.set_column_spacing(6);
        chips.set_row_spacing(6);
        let ids: Vec<String> = all.iter().map(|r| r["id"].as_str().unwrap_or_default().to_owned()).collect();
        let me = Rc::downgrade(self);
        dd.connect_selected_notify(move |d| {
            let i = d.selected() as usize;
            if i == 0 {
                return;
            }
            if let Some(me) = me.upgrade()
                && let Some(id) = ids.get(i - 1)
            {
                if !me.regions.borrow().contains(id) {
                    me.regions.borrow_mut().push(id.clone());
                }
                me.redraw();
            }
        });
        self.body.append(&dd);
        for id in self.regions.borrow().iter() {
            let b = crate::cards::wrap_button(&format!("{} ✕", name_of(id)));
            b.add_css_class("chip");
            b.set_tooltip_text(Some("Remove"));
            let me = Rc::downgrade(self);
            let id = id.clone();
            b.connect_clicked(move |_| {
                if let Some(me) = me.upgrade() {
                    me.regions.borrow_mut().retain(|x| *x != id);
                    me.redraw();
                }
            });
            chips.insert(&b, -1);
        }
        if !self.regions.borrow().is_empty() {
            self.body.append(&chips);
        }
        self.body.append(&label(
            "Streets, buildings and help points (hospitals, pharmacies, water, shelters) for the areas you pick; a small country \
             is about 0.1–0.5 GB, a large one several GB. Made on this computer from OpenStreetMap: you choose the areas, and \
             Lastbook never detects your location.",
            "meta",
        ));
    }

    fn start(self: &Rc<Self>) {
        let regions = self.regions.borrow().clone();
        let mut body = json!({"pack": *self.pack.borrow(), "world": self.world.get(), "regions": regions});
        if let Some(f) = self.folder.borrow().as_ref()
            && self.state.borrow()["mode"] == "first-run"
        {
            body["folder"] = json!(f);
        }
        if body["pack"].is_null() && !self.world.get() && self.regions.borrow().is_empty() {
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
                Ok(v) if v["error"].is_string() => {   // a job's answer carries "error": null
                    me.status.set_text(v["error"].as_str().unwrap_or("Setup could not start."));
                    me.status.add_css_class("error");
                }
                Ok(v) => {
                    me.status.remove_css_class("error");
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
        // Each start has its own number, so a timer from the start before stops, however soon this one follows.
        let turn = self.poll_turn.get() + 1;
        self.poll_turn.set(turn);
        let me = Rc::downgrade(self);
        glib::timeout_add_local(Duration::from_secs(1), move || {
            let Some(me) = me.upgrade() else { return glib::ControlFlow::Break };
            if me.poll_turn.get() != turn {
                return glib::ControlFlow::Break;
            }
            let (engine, weak) = (me.engine.clone(), Rc::downgrade(&me));
            glib::spawn_future_local(async move {
                if let Ok(job) = engine.get("/api/setup/job").await
                    && let Some(me) = weak.upgrade()
                {
                    me.draw_job(&job);
                    if job["state"].as_str().is_none_or(|s| matches!(s, "done" | "failed")) {
                        me.polling.set(false);
                    }
                }
            });
            if me.polling.get() { glib::ControlFlow::Continue } else { glib::ControlFlow::Break }
        });
    }

    fn draw_job(self: &Rc<Self>, job: &Value) {
        // The state kept for a redraw has the job as it now is, not as it was when the page opened.
        if let Some(st) = self.state.borrow_mut().as_object_mut() {
            st.insert("job".into(), job.clone());
        }
        // Everything but the buttons is drawn again; the buttons stay (detached, then put back) unless they change.
        if self.job_buttons.parent().is_some() {
            self.job_box.remove(&self.job_buttons);
        }
        clear(&self.job_box);
        let Some(items) = job["items"].as_array().filter(|i| !i.is_empty()) else { return };
        self.job_box.append(&crate::cards::section("Progress"));
        let mut said: Vec<String> = Vec::new();   // an item's reason is not repeated as the job's
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
            if let Some(e) = it["error"]["message"].as_str() {
                said.push(e.to_owned());
                let l = label(e, "meta");
                l.add_css_class("error");
                row.append(&l);
            }
            self.job_box.append(&row);
        }
        let state = job["state"].as_str().unwrap_or_default();
        // What the buttons offer, not the job's every phase (downloading, verifying, unpacking… all offer Pause/Cancel).
        let offer = match state {
            "done" | "paused" => state.to_owned(),
            "failed" => format!("failed/{}", job["error"]["code"].as_str().unwrap_or_default() == "cancelled"),
            _ => "active".to_owned(),
        };
        let fresh = *self.job_buttons_for.borrow() != offer;
        if fresh {
            clear(&self.job_buttons);
            self.job_buttons_for.replace(offer);
        }
        let buttons = self.job_buttons.clone();
        let add = |text: &str, path: &'static str, primary: bool| {
            if !fresh {
                return;
            }
            let b = gtk::Button::with_label(text);
            b.add_css_class(if primary { "suggested-action" } else { "chip" });
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
            "failed" if job["error"]["code"] == "cancelled" => {}   // the person's own cancel: nothing to try again
            "failed" => {
                add("Try again", "/api/setup/retry", true);
                add("Cancel", "/api/setup/cancel", false);
            }
            "paused" => {
                add("Resume", "/api/setup/resume", true);
                add("Cancel", "/api/setup/cancel", false);
            }
            _ => {
                add("Pause", "/api/setup/pause", false);
                add("Cancel", "/api/setup/cancel", false);
            }
        }
        if let Some(e) = job["error"]["message"].as_str().filter(|e| !said.iter().any(|s| s == e)) {
            let l = label(e, "meta");
            l.add_css_class("error");
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
        // The engine's output closed and this window is still here: nothing can answer any more.
        if let Some(window) = app.upgrade().and_then(|a| a.active_window()) {
            gtk::AlertDialog::builder().modal(true).message("Lastbook's library engine stopped").detail("Close Lastbook and open it again.").build().show(Some(&window));
        }
    });
}
