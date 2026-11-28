// SPDX-License-Identifier: MIT
//! Setup: choosing and downloading a library and the map (engine/setup/api.py, docs/SETUP-API.md).
//!
//! First run fills the window; with a library open, the same page manages it (Library and downloads, in the sidebar). Nothing
//! reaches the network until the person presses Download or Check for newer libraries. The engine says when a job's state changes
//! (`follow`); how far it has got has no such word, so that is read once a second, and only while this page is on screen
//! in the window the person is using (`watch`). Closing the window while a download runs asks first (`guard_close`).
//! When the job is done, Open asks the engine to finish, and the engine asks this program to restart on the new library.
//!
//! What the engine says in words (a reason a download failed, why no library is suggested) is English, written in
//! engine/setup. The window translates each sentence the engine always says the same way (`ENGINE_TEXT`); one that
//! carries a number, a path or a system error is shown as the engine wrote it, under the window's own translated
//! heading or button. The packs' titles and summaries come from the published manifest (`MANIFEST_TEXT`) and are
//! translated the same way, falling back to the manifest's words for a pack this version does not know.

use std::{
    cell::{Cell, RefCell},
    rc::Rc,
    sync::Arc,
    time::Duration,
};

use gtk::{glib, prelude::*};
use omui::{clear, label, tr};
use serde_json::{Value, json};

use crate::{cards::on_click, engine::Engine};

pub struct SetupPage {
    pub root: gtk::Widget,
    engine: Arc<Engine>,
    window: RefCell<Option<gtk::Window>>,
    body: gtk::Box,
    job_box: gtk::Box,
    /// The job's buttons, kept across each redraw of its progress (a keyboard user's focus stays on them).
    job_buttons: KeptButtons,
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
    /// The job as last read, whichever screen is on show: what closing the window is judged by.
    job: RefCell<Value>,
    /// The timer that reads a running job's progress, while there is one to read and someone to see it (`watch`).
    progress: RefCell<Option<glib::SourceId>>,
    /// Keeps the application running while a download finishes with the window put away (`ask_close`).
    held: RefCell<Option<omui::Hold>>,
    /// The desktop's folder dialog (the portal).
    picker: omui::PortalFilePicker,
    /// The line under the buttons: what it says, in English or the engine's words (translated as it is shown, and
    /// again after a language switch), and whether it is an error.
    said: RefCell<(String, bool)>,
    /// Run as the window comes back from a download that finished in the background: the library's window shows
    /// this page, which may not be the screen it was put away on.
    returned: RefCell<Option<Box<dyn Fn()>>>,
}

/// Whether this window's own engine is working on `job`: closing the window would stop it. A job another window runs
/// (`elsewhere`) is not this one's to stop, and a paused, failed or finished one has nothing in flight.
fn runs(job: &Value) -> bool {
    job["elsewhere"] != true && job["state"].as_str().is_some_and(|s| !matches!(s, "done" | "failed" | "paused"))
}

/// Whether `job`'s progress is worth reading on a timer. This engine says when its own job changes state, so its job
/// is read only while it moves; another window's job sends no word here, so it is read until it has ended.
pub fn moves(job: &Value) -> bool {
    match job["state"].as_str() {
        Some("done" | "failed") | None => false,
        Some("paused") => job["elsewhere"] == true,
        Some(_) => true,
    }
}

pub fn gb(bytes: f64) -> String {
    use omui::i18n::format_number;
    if bytes >= 1e9 { tr!("{} GB", format_number(bytes / 1e9, 1)) } else { tr!("{} MB", format_number(bytes / 1e6, 0)) }
}

/// The sentences engine/setup always says the same way, each as the engine writes it: the window shows each in the
/// person's language (`engine_says`). A test checks that the engine still says each of them. The program itself never
/// reads the table: it is there for the catalogs.
#[allow(dead_code)]
const ENGINE_TEXT: &[&str] = &[
    n_!("No library is ready to download yet."),
    n_!("Cancelled. Nothing was changed."),
    n_!("A download is already under way. Pause or cancel it first."),
    n_!("This download is running in another Lastbook window. Use that window to pause or cancel it."),
    n_!("That library is not available yet."),
    n_!("Lastbook does not know that map region."),
    n_!("Choose a library first."),
    n_!("Nothing to add: this library is already here."),
    n_!("Lastbook cannot save in that folder. Choose another."),
    n_!("There is no library to repair yet."),
    n_!("Lastbook cannot tell which library this is, so it cannot repair it. Download a library instead."),
    n_!("Lastbook cannot find that file or folder."),
    n_!("The disk filled up. Free some space, then try again."),
    n_!("The disk filled up while downloading. Free some space, then try again."),
    n_!("The disk filled up while the map was being made. Free some space, then try again."),
    n_!("The disk filled up while the place names were being saved. Free some space, then try again."),
    n_!("The map server sent something that is not a map archive."),
    n_!("The map server sent a map Lastbook cannot use. Try again later."),
    n_!("Lastbook could not reach the map server to find the newest map. Try again later."),
    n_!("The map server sent a map in a form Lastbook cannot read. Try again later."),
    n_!("Lastbook could not download the place names. Try again later."),
    n_!("The place names did not download completely. Try again later."),
    n_!("Lastbook refused to download from an address it does not know."),
    n_!("The download server sent Lastbook to an address that does not exist. Try again later."),
    n_!("The download server redirected too many times. Try again later."),
    n_!("The map server answered with another part of the map than the one asked for."),
];

/// The packs' titles and summaries as the manifest this version ships has them (packaging/arch/manifest-v1.json),
/// for the catalogs as `ENGINE_TEXT` is.
#[allow(dead_code)]
const MANIFEST_TEXT: &[&str] = &[
    n_!("Small"),
    n_!("Medium"),
    n_!("All"),
    n_!("Essential first aid, water, food, shelter and repair knowledge."),
    n_!("Small, plus the core how-to videos, all documents and Wikibooks."),
    n_!("Everything in the library: every video, document and encyclopedia, Wikipedia included."),
];

/// A sentence of the engine's, in the person's language when it is one the catalogs know (`ENGINE_TEXT`,
/// `MANIFEST_TEXT`), else as the engine wrote it.
fn engine_says(text: &str) -> String {
    omui::i18n::translate(text)
}

/// What a download is doing, from the engine's word for it.
fn state_text(state: &str) -> String {
    match state {
        "waiting" => tr("Waiting"),
        "downloading" => tr("Downloading"),
        "verifying" => tr("Checking"),
        "unpacking" => tr("Unpacking"),
        "activating" => tr("Putting it in place"),
        "making" | "cutting" => tr("Making the map"),
        "paused" => tr("Paused"),
        "failed" => tr("Failed"),
        "done" => tr("Done"),
        other => other.to_owned(),
    }
}

/// What a download item is called: a pack by its manifest title, a part of the map by what it is.
fn item_title(item: &Value, regions: &[Value]) -> String {
    let (id, title) = (item["id"].as_str().unwrap_or_default(), item["title"].as_str().unwrap_or_default());
    if item["kind"] != "osm" {
        return engine_says(title);
    }
    match id {
        "world" => tr("World map"),
        "places" => tr("Place search"),
        _ => match regions.iter().find(|r| r["id"] == id).and_then(|r| r["name"].as_str()) {
            Some(name) => tr!("Streets for {}", name),
            None => title.to_owned(),
        },
    }
}

/// One item of a download as it is shown here and in the Map's nearest help: what it is and what it is doing, how far
/// it has got, and why it failed. `regions` name the parts of the map (`map_regions`, or the Map's regions for a point).
pub fn item_row(it: &Value, regions: &[Value]) -> gtk::Box {
    let row = gtk::Box::new(gtk::Orientation::Vertical, 4);
    row.add_css_class("result");
    let state = it["state"].as_str().unwrap_or_default();
    let (done, total) = (it["bytes_done"].as_f64().unwrap_or(0.0), it["bytes_total"].as_f64().unwrap_or(0.0));
    // A list of facts, each a phrase of its own joined by a dot, never a sentence glued from parts.
    let mut line = vec![tr!("{} — {}", item_title(it, regions), state_text(state))];
    if total > 0.0 {
        line.push(tr!("{} of {}", gb(done), gb(total)));
    }
    if let Some(r) = it["rate_bps"].as_f64().filter(|r| *r > 0.0) {
        line.push(tr!("{}/s", gb(r)));
    }
    if let Some(e) = it["eta_s"].as_f64().filter(|e| *e > 0.0) {
        line.push(omui::trn!("about {n} minutes left", (e / 60.0).ceil() as i64));
    }
    let line = label(&line.join(" · "), "result-title");
    line.set_max_width_chars(30);   // its natural width, in the Map's panel: it wraps to the width it is given
    row.append(&line);
    let bar = gtk::ProgressBar::new();
    bar.set_fraction(if total > 0.0 { (done / total).clamp(0.0, 1.0) } else if state == "done" { 1.0 } else { 0.0 });
    row.append(&bar);
    if let Some(e) = it["error"]["message"].as_str() {
        row.append(&error_label(e));
    }
    row
}

/// The rows of a download, as this page and the Map's panel show them: each item (`item_row`), the job's own reason
/// where no item gave it, and a note when another window runs it.
pub fn job_rows(into: &gtk::Box, job: &Value, regions: &[Value]) {
    let mut said = Vec::new();   // an item's reason is not repeated as the job's
    for it in job["items"].as_array().into_iter().flatten() {
        said.extend(it["error"]["message"].as_str());
        into.append(&item_row(it, regions));
    }
    if let Some(e) = job["error"]["message"].as_str().filter(|e| !said.contains(e)) {
        into.append(&error_label(e));
    }
    if job["elsewhere"] == true {
        let note = label(&tr("This download is running in another Lastbook window. Use that window to pause or cancel it."), "meta");
        note.set_max_width_chars(30);
        into.append(&note);
    }
}

/// A job's buttons, kept across each redraw of what is around them, so the keyboard's focus can stay on them, and
/// made again only when what they offer (`key`) changes.
pub struct KeptButtons {
    pub row: gtk::Box,
    key: RefCell<String>,
}

impl KeptButtons {
    pub fn new(spacing: i32) -> Self {
        Self { row: gtk::Box::new(gtk::Orientation::Horizontal, spacing), key: RefCell::new(String::new()) }
    }

    /// Takes the row out of the box it is in, to be put back once that is drawn again.
    pub fn detach(&self) {
        if let Some(parent) = self.row.parent().and_downcast::<gtk::Box>() {
            parent.remove(&self.row);
        }
    }

    /// Has `build` make the buttons again, into the emptied row, if they are to offer something else than they do.
    pub fn update(&self, key: &str, build: impl FnOnce(&gtk::Box)) {
        if *self.key.borrow() != key {
            clear(&self.row);
            self.key.replace(key.to_owned());
            build(&self.row);
        }
    }

    /// The next `update` makes the buttons whatever it is given.
    pub fn forget(&self) {
        self.key.replace(String::new());
    }
}

/// Runs `tick` on `me` once a second, until it says to stop or `me` is gone: how a running job's progress is read.
pub fn every_second<T: 'static>(me: &Rc<T>, tick: impl Fn(&Rc<T>) -> glib::ControlFlow + 'static) -> glib::SourceId {
    let me = Rc::downgrade(me);
    glib::timeout_add_local(Duration::from_secs(1), move || match me.upgrade() {
        Some(me) => tick(&me),
        None => glib::ControlFlow::Break,
    })
}

/// A sentence of the engine's, shown as an error.
pub fn error_label(text: &str) -> gtk::Label {
    let l = label(&engine_says(text), "meta");
    l.add_css_class("error");
    l.set_max_width_chars(30);
    l
}

/// Asks `picker` for a folder, titled `title`, and hands `chosen` the folder picked, or `failed` why the desktop could
/// not ask (no portal, say): that is said in the window. Closing the dialog chooses nothing and says nothing.
fn pick_folder(picker: &dyn omui::FilePicker, title: &str, chosen: impl FnOnce(String) + 'static, failed: impl FnOnce(String) + 'static) {
    let options = omui::PickOptions { directory: true, ..omui::PickOptions::new(title) };
    picker.open(
        options,
        Box::new(move |picked| match picked {
            Ok(folders) => {
                if let Some(folder) = folders.into_iter().next() {
                    chosen(folder.to_string_lossy().into_owned());
                }
            }
            Err(omui::PickError::Cancelled) => {}
            Err(e) => failed(e.to_string()),
        }),
    );
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
            job_buttons: KeptButtons::new(8),
            status,
            state: RefCell::new(Value::Null),
            pack: RefCell::new(None),
            chosen: Cell::new(false),
            world: Cell::new(true),
            regions: RefCell::new(Vec::new()),
            folder: RefCell::new(None),
            job: RefCell::new(Value::Null),
            progress: RefCell::new(None),
            held: RefCell::new(None),
            picker: omui::PortalFilePicker::new(),
            said: RefCell::new((String::new(), false)),
            returned: RefCell::new(None),
        })
    }

    /// Writes `text` (English, or the engine's words) on the line under the buttons, as an error or not.
    fn say(&self, text: &str, error: bool) {
        self.said.replace((text.to_owned(), error));
        self.status.set_text(&engine_says(text));
        omui::set_class(&self.status, "error", error);
    }

    /// After a language switch: the page is drawn again from the state last read, and the line under the buttons
    /// written again.
    pub fn retranslate(self: &Rc<Self>) {
        if !self.state.borrow().is_null() {
            self.redraw();
        }
        let (text, error) = self.said.borrow().clone();
        self.say(&text, error);
    }

    pub fn set_window(self: &Rc<Self>, w: &impl IsA<gtk::Window>) {
        let window: gtk::Window = w.clone().upcast();
        // Progress is read only while there is someone to see it: this page on screen, in the window in use.
        let me = Rc::downgrade(self);
        window.connect_is_active_notify(move |w| {
            if let Some(me) = me.upgrade() {
                me.watch(w.is_active());
            }
        });
        for mapped in [true, false] {
            let me = Rc::downgrade(self);
            let seen = move |_: &gtk::Widget| {
                if let Some(me) = me.upgrade() {
                    me.watch(mapped);
                }
            };
            if mapped { self.root.connect_map(seen) } else { self.root.connect_unmap(seen) };
        }
        self.window.replace(Some(window));
    }

    /// What main.rs does as the window comes back from a download finished in the background.
    pub fn on_return(&self, f: impl Fn() + 'static) {
        self.returned.replace(Some(Box::new(f)));
    }

    /// Reads the engine's setup state and draws the page.
    pub fn refresh(self: &Rc<Self>) {
        let (engine, me) = (self.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let st = engine.get("/api/setup/state").await;
            let Some(me) = me.upgrade() else { return };
            match st {
                Ok(st) => me.draw(st),
                Err(e) => {
                    clear(&me.body);
                    me.body.append(&crate::cards::problem(&tr("Lastbook could not read what there is to download."), &e.to_string(), &me, |me| me.refresh()));
                }
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
        self.body.append(&label(&if first { tr("Set up Lastbook") } else { tr("Library and downloads") }, "page-title"));
        if let Some(c) = st["current"].as_object() {
            self.body.append(&label(
                &tr!(
                    "This library: {} · {}",
                    c.get("pack").and_then(Value::as_str).map(engine_says).unwrap_or_else(|| "—".to_owned()),
                    c.get("folder").and_then(Value::as_str).unwrap_or_default()
                ),
                "meta",
            ));
        }

        // Packs
        self.body.append(&crate::cards::section(&tr("Library")));
        // Nothing suggested (no library fits the disk, or none is ready): the engine says why.
        if first && rec["pack"].is_null() && let Some(why) = rec["why"].as_str() {
            let l = label(&engine_says(why), "meta");
            l.add_css_class("error");
            self.body.append(&l);
        }
        let need = &rec["need_bytes"];
        // Each pack, then (managing a library) keeping it as it is: one choice of them.
        let mut choices: Vec<(Option<String>, gtk::CheckButton)> = Vec::new();
        for p in st["manifest"]["packs"].as_array().into_iter().flatten() {
            let id = p["id"].as_str().unwrap_or_default().to_owned();
            let row = gtk::CheckButton::new();
            let text = gtk::Box::new(gtk::Orientation::Vertical, 2);
            let (title, size) = (engine_says(p["title"].as_str().unwrap_or(&id)), gb(p["installed_bytes"].as_f64().unwrap_or(0.0)));
            let head = if rec["pack"] == id.as_str() { tr!("{} · {} (recommended)", title, size) } else { tr!("{} · {}", title, size) };
            text.append(&label(&head, "result-title"));
            text.append(&label(&engine_says(p["summary"].as_str().unwrap_or_default()), "dim-label"));
            if let Some(n) = need[&id].as_f64() {
                text.append(&label(&tr!("Needs {} free while it downloads and unpacks", gb(n)), "meta"));
            }
            row.set_child(Some(&text));
            row.set_sensitive(p["available"].as_bool().unwrap_or(true));
            choices.push((Some(id), row));
        }
        if !first {
            let none = gtk::CheckButton::with_label(&tr("Keep the library as it is"));
            crate::cards::wrap_check(&none);
            choices.push((None, none));
        }
        let mut group: Option<gtk::CheckButton> = None;
        for (choice, row) in choices {
            match &group {
                Some(g) => row.set_group(Some(g)),
                None => group = Some(row.clone()),
            }
            row.add_css_class("choice-row");
            let on = *self.pack.borrow() == choice;   // read first: ticking it writes the choice
            let me = Rc::downgrade(self);
            row.connect_toggled(move |b| {
                if b.is_active()
                    && let Some(me) = me.upgrade()
                {
                    me.pack.replace(choice.clone());
                }
            });
            row.set_active(on);
            self.body.append(&row);
        }

        self.draw_maps(&st);

        // Where
        if first {
            self.body.append(&crate::cards::section(&tr("Where to keep it")));
            let row = gtk::Box::new(gtk::Orientation::Horizontal, 8);
            let path = label(self.folder.borrow().as_deref().unwrap_or_default(), "meta");
            path.set_wrap_mode(gtk::pango::WrapMode::Char);
            path.set_hexpand(true);
            let choose = gtk::Button::with_label(&tr("Choose folder…"));
            choose.add_css_class("chip");
            let shown = path.clone();
            on_click(&choose, self, move |me| {
                let shown = shown.clone();
                me.pick(&tr("Where should the library go?"), move |me, p| {
                    shown.set_text(&p);
                    me.folder.replace(Some(p));
                });
            });
            row.append(&path);
            row.append(&choose);
            self.body.append(&row);
            let use_existing = crate::cards::wrap_button(&tr("Use a library or pack already on this computer…"), "chip");
            use_existing.set_halign(gtk::Align::Start);
            on_click(&use_existing, self, |me| {
                me.pick(&tr("Choose a LastbookData folder, or the folder that holds a pack zip"), |me, p| me.post("/api/setup/use", json!({"path": p})));
            });
            self.body.append(&use_existing);
            for f in st["found"].as_array().into_iter().flatten() {
                let path = f["path"].as_str().unwrap_or_default();
                let b = crate::cards::wrap_button(&if f["kind"] == "pack" { tr!("Use the pack found at {}", path) } else { tr!("Use the library found at {}", path) }, "chip");
                b.set_halign(gtk::Align::Start);
                let p = f["path"].as_str().unwrap_or_default().to_owned();
                on_click(&b, self, move |me| me.post("/api/setup/use", json!({"path": p})));
                self.body.append(&b);
            }
        }

        let actions = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        actions.set_margin_top(12);
        let start = gtk::Button::with_label(&if first { tr("Download and set up") } else { tr("Download") });
        start.add_css_class("suggested-action");
        on_click(&start, self, |me| me.start());
        actions.append(&start);
        if !first {
            let repair = gtk::Button::with_label(&tr("Check every file"));
            repair.add_css_class("chip");
            on_click(&repair, self, |me| me.post("/api/setup/start", json!({"repair": true})));
            actions.append(&repair);
        }
        // The only request setup makes unasked for a download: whether newer libraries have been published since this
        // version was built. Made when this is pressed, never when the page opens.
        let newer = gtk::Button::with_label(&tr("Check for newer libraries"));
        newer.add_css_class("chip");
        let me = Rc::downgrade(self);
        newer.connect_clicked(move |b| {
            let Some(page) = me.upgrade() else { return };
            b.set_sensitive(false);
            let (engine, me, b) = (page.engine.clone(), me.clone(), b.clone());
            glib::spawn_future_local(async move {
                let r = engine.post("/api/setup/check", &json!({})).await;
                b.set_sensitive(true);
                if let (Ok(st), Some(me)) = (r, me.upgrade()) {
                    let fetched = st["manifest"]["source"] == "fetched";
                    me.draw(st);
                    me.say(if fetched { n_!("The list of libraries is up to date.") } else { n_!("Lastbook could not reach downloads.lastbook.org. The list is the one this version came with.") }, false);
                }
            });
        });
        actions.append(&newer);
        self.body.append(&actions);
        self.body.append(&self.status);
        self.body.append(&self.job_box);
        self.state.replace(st.clone());
        self.draw_job(&st["job"]);
    }

    /// Asks for a folder (`pick_folder`), titled `title`, and hands the one picked to `chosen`; why the desktop could
    /// not ask is said on the line under the buttons.
    fn pick(self: &Rc<Self>, title: &str, chosen: impl FnOnce(&Rc<Self>, String) + 'static) {
        let (me, failed) = (Rc::downgrade(self), Rc::downgrade(self));
        pick_folder(
            &self.picker,
            title,
            move |p| {
                if let Some(me) = me.upgrade() {
                    chosen(&me, p);
                }
            },
            move |why| {
                if let Some(me) = failed.upgrade() {
                    me.say(&why, true);
                }
            },
        );
    }

    /// The map: the world to city level, and street-level areas the person picks. Both are made on this computer from
    /// OpenStreetMap's newest daily build; Lastbook hosts no maps.
    fn draw_maps(self: &Rc<Self>, st: &Value) {
        self.body.append(&crate::cards::section(&tr("Map")));
        let have = &st["current"]["osm"];
        let has_world = have["world"].as_bool().unwrap_or(false) && have["places"].as_bool().unwrap_or(false);
        let all: Vec<Value> = st["map_regions"].as_array().cloned().unwrap_or_default();
        let name_of_all = all.clone();
        let name_of = move |id: &str| name_of_all.iter().find(|r| r["id"] == id).and_then(|r| r["name"].as_str()).unwrap_or(id).to_owned();
        // The engine says which regions are offered (maps.regions): countries and their parts, not continents.
        let mut all: Vec<Value> = all.into_iter().filter(|r| r["offered"] == true).collect();
        all.sort_by_key(|r| r["name"].as_str().unwrap_or_default().to_lowercase());
        if has_world {
            let installed: Vec<String> = have["regions"].as_array().into_iter().flatten()
                .map(|r| tr!("{} ({})", r["name"].as_str().unwrap_or_default(), gb(r["bytes"].as_f64().unwrap_or(0.0))))
                .collect();
            self.body.append(&label(&if installed.is_empty() {
                tr("The world map is here, to city level.")
            } else {
                tr!("The world map is here, with streets for {}.", installed.join(", "))
            }, "meta"));
            self.world.set(false);
        } else {
            let check = gtk::CheckButton::with_label(&tr!(
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
        let names: Vec<String> = std::iter::once(tr("Add streets for an area…"))
            .chain(all.iter().map(|r| {
                let parent = r["parent"].as_str().unwrap_or_default();
                if parent.is_empty() { r["name"].as_str().unwrap_or_default().to_owned() } else { tr!("{} · {}", r["name"].as_str().unwrap_or_default(), name_of(parent)) }
            }))
            .collect();
        let refs: Vec<&str> = names.iter().map(String::as_str).collect();
        let dd = gtk::DropDown::from_strings(&refs);
        // Its first line says what it is for; a screen reader hears the same.
        dd.update_property(&[gtk::accessible::Property::Label(&tr("Add streets for an area"))]);
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
            let b = crate::cards::wrap_button(&format!("{} ✕", name_of(id)), "chip");
            omui::i18n::bind_tooltip(&b, "Remove");
            let id = id.clone();
            on_click(&b, self, move |me| {
                me.regions.borrow_mut().retain(|x| *x != id);
                me.redraw();
            });
            chips.insert(&b, -1);
        }
        if !self.regions.borrow().is_empty() {
            self.body.append(&chips);
        }
        // Where the computer seems to be and has no streets (engine/setup/here.py): its regions, one press each.
        if let Some(here) = st["here"].as_object() {
            let place = here.get("place").and_then(Value::as_str).unwrap_or_default();
            self.body.append(&label(&if here.get("exact") == Some(&json!(true)) {
                tr!("No street map yet for {}, your weather location in Omarchy.", place)
            } else {
                tr!("No street map yet near {}, the city of your time zone.", place)
            }, "meta"));
            let offered = gtk::FlowBox::new();
            offered.set_selection_mode(gtk::SelectionMode::None);
            offered.set_column_spacing(6);
            offered.set_row_spacing(6);
            for r in here.get("regions").and_then(Value::as_array).into_iter().flatten() {
                let (Some(id), Some(name)) = (r["id"].as_str(), r["name"].as_str()) else { continue };
                if self.regions.borrow().iter().any(|x| x == id) {
                    continue;
                }
                let b = crate::cards::wrap_button(&tr!("Add {}", name), "chip");
                let id = id.to_owned();
                on_click(&b, self, move |me| {
                    me.regions.borrow_mut().push(id.clone());
                    me.redraw();
                });
                offered.insert(&b, -1);
            }
            self.body.append(&offered);
        }
        let ask = gtk::CheckButton::with_label(&tr("Offer the street map of where this computer is"));
        crate::cards::wrap_check(&ask);
        ask.set_active(st["map_offer"] != json!(false));
        let me = Rc::downgrade(self);
        ask.connect_toggled(move |b| {
            if let Some(me) = me.upgrade() {
                me.set_offer(b.is_active());
            }
        });
        self.body.append(&ask);
        self.body.append(&label(
            &tr("Streets, buildings and help points (hospitals, pharmacies, water, shelters) for the areas you pick; a small country \
             is about 0.1–0.5 GB, a large one several GB. Made on this computer from OpenStreetMap. Lastbook reads where you \
             are from Omarchy's weather location or time zone, and sends nothing to find you."),
            "meta",
        ));
    }

    /// The person's switch for the map offer: kept by the engine, and the page drawn again with what it now offers.
    fn set_offer(self: &Rc<Self>, ask: bool) {
        let (engine, me) = (self.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let r = engine.post("/api/setup/offer", &json!({"ask": ask})).await;
            let Some(me) = me.upgrade() else { return };
            match r {
                Ok(v) => {
                    if !me.state.borrow().is_null() {
                        let mut st = me.state.borrow().clone();
                        st["map_offer"] = v["map_offer"].clone();
                        st["here"] = v["here"].clone();
                        me.state.replace(st);
                        me.redraw();
                    }
                }
                Err(e) => me.say(&e.to_string(), true),
            }
        });
    }

    /// As the library's window opens: where the computer seems to be has no street-level map, it is online and the
    /// person has not switched the offer off (engine/setup/here.py), so they are asked. `show` brings Library and
    /// downloads on screen, with the region already picked where the engine is sure of it (`pick`); nothing is downloaded until they
    /// start it there.
    pub fn offer_map(self: &Rc<Self>, show: impl Fn() + 'static) {
        let (engine, me) = (self.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let Ok(v) = engine.get("/api/setup/here").await else { return };
            let Some(me) = me.upgrade() else { return };
            let here = v["here"].clone();
            let Some(place) = here["place"].as_str().map(str::to_owned) else { return };
            let exact = here["exact"] == json!(true);
            let dialog = omui::Dialog::new(Some(""), Some(""));
            dialog.bind(move |d| {
                d.set_heading(&tr!("Add a street map for {}?", place));
                d.set_body(&if exact {
                    tr!("No street-level map on this computer covers {}, your weather location in Omarchy. Nearest help needs one.", place)
                } else {
                    tr!("No street-level map on this computer is near {}, the city of your time zone. Nearest help needs one.", place)
                });
            });
            for id in ["later", "never", "choose"] {
                dialog.add_response(id, "");
            }
            dialog.set_response_translated("later", "_Not now");
            dialog.set_response_translated("never", "_Don't ask again");
            dialog.set_response_translated("choose", "_Choose a map");
            dialog.set_response_appearance("choose", omui::Appearance::Suggested);
            dialog.set_default_response("choose");
            dialog.set_close_response("later");
            let weak = Rc::downgrade(&me);
            dialog.connect_response(move |_, id| {
                let Some(me) = weak.upgrade() else { return };
                match id {
                    "never" => me.set_offer(false),
                    "choose" => {
                        if let Some(id) = here["pick"].as_str()
                            && !me.regions.borrow().iter().any(|x| x == id)
                        {
                            me.regions.borrow_mut().push(id.to_owned());
                        }
                        show();
                    }
                    _ => {}
                }
            });
            dialog.present(&me.root);
        });
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
            self.say(n_!("Choose something to download first."), false);
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
                    me.say(v["error"].as_str().unwrap_or(n_!("Setup could not start.")), true);
                }
                Ok(v) => {
                    me.say("", false);
                    if v.get("items").is_some() {
                        me.draw_job(&v);
                    } else {
                        me.read_job();
                    }
                }
                Err(e) => me.say(&e.to_string(), true),
            }
        });
    }

    /// Reads the job and draws it: when the engine says its state has changed (`follow`), on the progress timer, and
    /// as the window comes back into use.
    pub fn read_job(self: &Rc<Self>) {
        let (engine, me) = (self.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            if let Ok(job) = engine.get("/api/setup/job").await
                && let Some(me) = me.upgrade()
            {
                me.draw_job(&job);
            }
        });
    }

    /// Starts or stops the reading of progress, as the job, the screen on show or the window in use changes.
    /// `seen_again`: this page has just come back on screen, or its window back into use, so what it shows is brought
    /// up to date at once.
    fn watch(self: &Rc<Self>, seen_again: bool) {
        let seen = self.root.is_mapped() && self.window.borrow().as_ref().is_some_and(|w| w.is_active());
        let wanted = seen && moves(&self.job.borrow());
        if wanted && self.progress.borrow().is_none() {
            let timer = every_second(self, |me| {
                me.read_job();
                glib::ControlFlow::Continue
            });
            self.progress.replace(Some(timer));
            if seen_again {
                self.read_job();
            }
        } else if !wanted && let Some(timer) = self.progress.take() {
            // Never from the timer's own turn: it only reads, and what it reads is drawn (and comes here) later.
            timer.remove();
        }
    }

    /// Closing the window, by its button or the keyboard (main.rs), asks first while a download runs here: the
    /// engine is this window's child, so a window that simply closed would stop the download with it.
    pub fn guard_close(self: &Rc<Self>, app: &gtk::Application) {
        let Some(window) = self.window.borrow().clone() else { return };
        let (me, app) = (Rc::downgrade(self), app.downgrade());
        window.connect_close_request(move |window| {
            let (Some(me), Some(app)) = (me.upgrade(), app.upgrade()) else { return glib::Propagation::Proceed };
            if !runs(&me.job.borrow()) {
                return glib::Propagation::Proceed;
            }
            // Asked a second time while the question is still open: the same question, not another over it.
            if !omui::dialog_open(window) {
                me.ask_close(&app);
            }
            glib::Propagation::Stop
        });
    }

    /// The question a close meets while a download runs. Lastbook neither stops a download of many gigabytes unasked
    /// nor carries one on unseen: the person chooses. Escape answers "keep the window open", which loses nothing and
    /// leaves the download running.
    fn ask_close(self: &Rc<Self>, app: &gtk::Application) {
        // The English is given to the `_translated` setters, so a dialog open while the language changes follows it.
        let dialog = omui::Dialog::new(Some(""), Some(""));
        dialog.set_heading_translated("A download is still running");
        dialog.set_body_translated("Lastbook can finish it in the background and open again when it is done. If you stop it, it carries on from where it stopped the next time you start it.");
        dialog.add_response("stay", "");
        dialog.set_response_translated("stay", "Keep the _window open");
        dialog.add_response("stop", "");
        dialog.set_response_translated("stop", "_Stop and close");
        dialog.add_response("background", "");
        dialog.set_response_translated("background", "_Finish in the background");
        dialog.set_response_appearance("background", omui::Appearance::Suggested);
        dialog.set_default_response("background");
        dialog.set_close_response("stay");
        let (me, app) = (Rc::downgrade(self), app.downgrade());
        dialog.connect_response(move |_, id| {
            let (Some(me), Some(app)) = (me.upgrade(), app.upgrade()) else { return };
            // Any other answer (Escape, the close button, a response this does not know) keeps the window open.
            match id {
                "background" => me.to_background(),
                "stop" => me.stop_and_leave(&app),
                _ => {}
            }
        });
        dialog.present(&self.root);
    }

    /// Puts the window away and keeps the application, and with it the engine and the download, running. The window
    /// comes back when the job stops for any reason (`come_back`).
    fn to_background(self: &Rc<Self>) {
        if !runs(&self.job.borrow()) {
            return;   // it ended while the question was open: the window stays, showing how
        }
        self.held.replace(Some(omui::hold()));
        if let Some(window) = self.window.borrow().as_ref() {
            window.set_visible(false);
        }
    }

    /// Pauses the download, so what has arrived is on the disk in a state the next start carries on from, then leaves.
    fn stop_and_leave(self: &Rc<Self>, app: &gtk::Application) {
        let hold = omui::hold();
        if let Some(window) = self.window.borrow().as_ref() {
            window.set_visible(false);
        }
        let (engine, app) = (self.engine.clone(), app.clone());
        glib::spawn_future_local(async move {
            // Whatever the answer: an engine that cannot pause ends with this program, and its job starts paused next time.
            let _ = engine.post("/api/setup/pause", &json!({})).await;
            drop(hold);
            app.quit();
        });
    }

    /// Shows the window again if it was put away for a download: that has finished, failed or paused, or the engine
    /// has gone, and each needs the person (Open the library, Try again, an explanation).
    fn come_back(&self) {
        if self.held.take().is_none() {
            return;
        }
        if let Some(f) = self.returned.borrow().as_ref() {
            f();
        }
        if let Some(window) = self.window.borrow().as_ref() {
            window.present();
        }
    }

    /// Asks before a download is cancelled: cancelling deletes what has arrived, which may be hours of it. Escape
    /// keeps the download as it is, running if it was.
    fn ask_cancel(self: &Rc<Self>) {
        // In English to the `_translated` setters, which follow a switch of language while the dialog is open.
        let dialog = omui::Dialog::new(Some(""), Some(""));
        dialog.set_heading_translated("Cancel this download?");
        dialog.set_body_translated("What has been downloaded so far is deleted. Your library stays as it is.");
        dialog.add_response("keep", "");
        dialog.set_response_translated("keep", "_Keep the download");
        dialog.add_response("cancel", "");
        dialog.set_response_translated("cancel", "_Cancel the download");
        dialog.set_response_appearance("cancel", omui::Appearance::Destructive);
        dialog.set_default_response("keep");
        dialog.set_close_response("keep");
        let me = Rc::downgrade(self);
        dialog.connect_response(move |_, id| {
            if id == "cancel"
                && let Some(me) = me.upgrade()
            {
                me.post("/api/setup/cancel", json!({}));
            }
        });
        dialog.present(&self.root);
    }

    fn draw_job(self: &Rc<Self>, job: &Value) {
        // The state kept for a redraw has the job as it now is, not as it was when the page opened.
        if let Some(st) = self.state.borrow_mut().as_object_mut() {
            st.insert("job".into(), job.clone());
        }
        self.job.replace(job.clone());
        self.watch(false);
        if !runs(job) {
            self.come_back();
        }
        // Everything but the buttons is drawn again; the buttons stay (detached, then put back) unless they change.
        self.job_buttons.detach();
        clear(&self.job_box);
        if job["items"].as_array().is_none_or(|i| i.is_empty()) {
            return;
        }
        self.job_box.append(&crate::cards::section(&tr("Progress")));
        let regions = self.state.borrow()["map_regions"].as_array().cloned().unwrap_or_default();
        job_rows(&self.job_box, job, &regions);
        let state = job["state"].as_str().unwrap_or_default();
        // What the buttons offer, not the job's every phase (downloading, verifying, unpacking… all offer Pause/Cancel).
        let elsewhere = job["elsewhere"] == true;   // another window's engine runs it: its buttons would act on that one's files
        let offer = match state {
            _ if elsewhere => "elsewhere".to_owned(),
            "done" | "paused" => state.to_owned(),
            "failed" => format!("failed/{}", job["error"]["code"].as_str().unwrap_or_default() == "cancelled"),
            _ => "active".to_owned(),
        };
        self.job_buttons.update(&offer, |row| {
            let add = |text: String, path: &'static str, primary: bool| {
                let b = gtk::Button::with_label(&text);
                b.add_css_class(if primary { "suggested-action" } else { "chip" });
                on_click(&b, self, move |me| {
                    if path == "/api/setup/cancel" {
                        me.ask_cancel();
                    } else {
                        me.post(path, json!({}));
                    }
                });
                row.append(&b);
            };
            match state {
                "done" => add(tr("Open the library"), "/api/setup/finish", true),
                "failed" if job["error"]["code"] == "cancelled" => {}   // the person's own cancel: nothing to try again
                "failed" => {
                    add(tr("Try again"), "/api/setup/retry", true);
                    add(tr("Cancel"), "/api/setup/cancel", false);
                }
                _ if elsewhere => {}
                "paused" => {
                    add(tr("Resume"), "/api/setup/resume", true);
                    add(tr("Cancel"), "/api/setup/cancel", false);
                }
                _ => {
                    add(tr("Pause"), "/api/setup/pause", false);
                    add(tr("Cancel"), "/api/setup/cancel", false);
                }
            }
        });
        self.job_box.append(&self.job_buttons.row);
    }
}

/// Follows what the engine says unasked. {"restart": folder}: setup has finished, so this program starts again on the
/// new library and leaves. {"job": state}: a setup job's state has changed, so `page` reads and draws it, whether or
/// not anyone is looking: that is how a window put away for a download knows to come back.
pub fn follow(app: &gtk::Application, engine: &Arc<Engine>, page: &Rc<SetupPage>) {
    let (rx, app, page) = (engine.notices.clone(), app.downgrade(), Rc::downgrade(page));
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
            if msg.get("job").is_some()
                && let Some(page) = page.upgrade()
            {
                page.read_job();
            }
        }
        // The engine's output closed and this window is still here: nothing can answer any more.
        if let Some(page) = page.upgrade() {
            page.come_back();
        }
        if let Some(window) = app.upgrade().and_then(|a| a.active_window()) {
            // Nothing to choose: closing it is the one answer, by its button or Escape.
            // In English to the `_translated` setters, which follow a switch of language while the dialog is open.
            let dialog = omui::Dialog::new(Some(""), Some(""));
            dialog.set_heading_translated("Lastbook's library engine stopped");
            dialog.set_body_translated("Close Lastbook and open it again.");
            dialog.add_response("close", "");
            dialog.set_response_translated("close", "_Close");
            dialog.set_default_response("close");
            dialog.set_close_response("close");
            dialog.present(&window);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Each sentence the window translates for the engine is one the engine still says, word for word: a sentence
    /// reworded there would otherwise be shown in English, untranslated, with nothing to say so.
    #[test]
    fn the_engines_sentences_are_still_the_engines() {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
        let engine: String = ["engine/setup/api.py", "engine/setup/download.py", "engine/setup/maps.py"]
            .iter()
            .map(|f| std::fs::read_to_string(root.join(f)).unwrap())
            .collect();
        for text in ENGINE_TEXT {
            assert!(engine.contains(&format!("'{text}'")), "engine/setup no longer says {text:?}");
        }
        let manifest = std::fs::read_to_string(root.join("packaging/arch/manifest-v1.json")).unwrap();
        for text in MANIFEST_TEXT {
            assert!(manifest.contains(&format!("\"{text}\"")), "the manifest no longer says {text:?}");
        }
    }

    #[test]
    fn a_folder_is_asked_for_through_the_portal_and_its_failure_is_said() {
        let picker = omui::FakeFilePicker::new();
        let (chosen, failed) = (Rc::new(RefCell::new(Vec::new())), Rc::new(RefCell::new(Vec::new())));
        let ask = |picker: &omui::FakeFilePicker| {
            let (c, f) = (chosen.clone(), failed.clone());
            pick_folder(picker, "Where should the library go?", move |p| c.borrow_mut().push(p), move |e| f.borrow_mut().push(e));
        };
        picker.answer(Ok(vec!["/media/usb/LastbookData".into()]));
        ask(&picker);
        picker.answer(Err(omui::PickError::Cancelled));
        ask(&picker);
        picker.answer(Err(omui::PickError::Unavailable(omui::PickDetail::new("no portal"))));
        ask(&picker);
        assert_eq!(*chosen.borrow(), ["/media/usb/LastbookData"]);
        assert_eq!(*failed.borrow(), ["No file chooser is available on this desktop."], "closing the dialog says nothing");
        let asked = picker.asked();
        assert_eq!(asked.len(), 3);
        assert!(asked.iter().all(|(saving, o)| !saving && o.directory && !o.multiple && o.title == "Where should the library go?"));
    }

    #[test]
    fn a_download_says_what_it_is_and_what_it_is_doing() {
        let regions = [json!({"id": "asia/nepal", "name": "Nepal"})];
        assert_eq!(item_title(&json!({"kind": "osm", "id": "world", "title": "World map"}), &regions), "World map");
        assert_eq!(item_title(&json!({"kind": "osm", "id": "asia/nepal", "title": "Nepal streets"}), &regions), "Streets for Nepal");
        assert_eq!(item_title(&json!({"kind": "pack", "id": "small", "title": "Small"}), &regions), "Small");
        assert_eq!(state_text("downloading"), "Downloading");
        assert_eq!(state_text("something new"), "something new", "a state this version does not know is shown as it is");
    }

    #[test]
    fn closing_asks_only_while_this_window_downloads() {
        for state in ["waiting", "downloading", "verifying", "unpacking", "activating"] {
            assert!(runs(&json!({"state": state})), "{state}");
        }
        for state in ["paused", "failed", "done"] {
            assert!(!runs(&json!({"state": state})), "{state}: nothing is in flight");
        }
        assert!(!runs(&Value::Null), "no job at all");
        assert!(!runs(&json!({"state": "downloading", "elsewhere": true})), "another window's download does not stop with this one");
    }

    #[test]
    fn progress_is_read_only_while_there_is_some_to_read() {
        assert!(moves(&json!({"state": "downloading"})));
        assert!(moves(&json!({"state": "unpacking"})));
        // This engine says when its own job is resumed, so a paused job of its own is not read on a timer.
        assert!(!moves(&json!({"state": "paused"})));
        assert!(!moves(&json!({"state": "done"})));
        assert!(!moves(&json!({"state": "failed"})));
        assert!(!moves(&Value::Null));
        // Another window's engine sends no word here: its job is read until it has ended.
        assert!(moves(&json!({"state": "paused", "elsewhere": true})));
        assert!(!moves(&json!({"state": "done", "elsewhere": true})));
    }
}
