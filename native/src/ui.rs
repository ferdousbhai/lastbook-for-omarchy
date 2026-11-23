// SPDX-License-Identifier: MIT
//! The window: a header bar (sidebar toggle, back and forward, the screen's title, save), the sidebar of screens, and a
//! stack of pages. Navigation keeps a history like a browser's, so back and forward work across screens.

use std::{
    cell::{Cell, RefCell},
    collections::HashMap,
    rc::{Rc, Weak},
    sync::Arc,
};

use gtk::{gdk, glib, prelude::*};
use omui::{ShortcutSection, Theme, icon_button, icons::name, tr};

use crate::{
    cards::encyclopedia_name,
    encyclopedias::Encyclopedias,
    engine::Engine,
    guides::Guides,
    library::{Browse, Saved, load_saved, put_back, store_saved},
    map::MapPage,
    reader::Reader,
    search::SearchPage,
    videos::Videos,
    web::WebPage,
};

#[derive(Clone, Debug, PartialEq)]
pub enum Route {
    Search { q: String, kind: String },
    Read { doc: String, page: i32 },
    Guide(String),
    Watch { video: String, t: f64 },
    Wiki { path: String },
    Web { path: String, meta: String },
    MapAt { lat: f64, lon: f64, label: String },
    Shelf(String),
    /// A sidebar screen (SECTIONS), Library and downloads ("manage": setup, on the library that is open) among them.
    Section(&'static str),
}

impl Route {
    /// A short text form for what can be saved: read:<doc>:<page>, wiki:<path>, watch:<video>:<t>, guide:<id>,
    /// web:<path>. `from_key` reads these and a few more, for `lastbook open`.
    pub fn key(&self) -> Option<String> {
        Some(match self {
            Route::Read { doc, page } => format!("read:{doc}:{page}"),
            Route::Wiki { path } => format!("wiki:{path}"),
            Route::Watch { video, t } => format!("watch:{video}:{}", *t as i64),
            Route::Guide(id) => format!("guide:{id}"),
            Route::Web { path, .. } => format!("web:{path}"),
            _ => return None,
        })
    }

    /// What a title is remembered under, for the screens that learn theirs only once they have loaded.
    fn title_key(&self) -> Option<String> {
        Some(match self {
            Route::Read { doc, .. } => format!("read:{doc}"),
            Route::Watch { video, .. } => format!("watch:{video}"),
            Route::Web { path, .. } => format!("web:{path}"),
            _ => return None,
        })
    }

    pub fn from_key(key: &str) -> Option<Route> {
        let (kind, rest) = key.split_once(':')?;
        Some(match kind {
            "read" => {
                let (doc, page) = rest.rsplit_once(':').map(|(d, p)| (d, p.parse().unwrap_or(1))).unwrap_or((rest, 1));
                Route::Read { doc: doc.to_owned(), page }
            }
            "wiki" => Route::Wiki { path: rest.to_owned() },
            "watch" => {
                let (v, t) = rest.rsplit_once(':').map(|(v, t)| (v, t.parse().unwrap_or(0.0))).unwrap_or((rest, 0.0));
                Route::Watch { video: v.to_owned(), t }
            }
            "guide" => Route::Guide(rest.to_owned()),
            "web" => Route::Web { path: rest.to_owned(), meta: String::new() },
            "search" => Route::Search { q: rest.to_owned(), kind: "any".into() },
            "shelf" => Route::Shelf(rest.to_owned()),
            "manage" => Route::Section("manage"),
            "map" => {
                let mut it = rest.splitn(3, ',');
                let (lat, lon) = (it.next()?.trim().parse().ok()?, it.next()?.trim().parse().ok()?);
                Route::MapAt { lat, lon, label: it.next().map(str::to_owned).unwrap_or_else(|| tr("Here")) }
            }
            "section" => Route::Section(SECTIONS.iter().find(|s| s.0 == rest).map(|s| s.0)?),
            _ => return None,
        })
    }
}

/// The sidebar's screens, in order: (id, label, icon). About is a screen too, but its way in is the sidebar's footer.
/// The label is English, translated where it is shown.
pub const SECTIONS: &[(&str, &str, &str)] = &[
    ("search", n_!("Search"), name::SEARCH),
    ("guides", n_!("Guides"), name::INFO),
    ("library", n_!("Library"), name::DOCUMENT),
    ("encyclopedias", n_!("Encyclopedias"), name::BOOK),
    ("videos", n_!("Videos"), name::VIDEO),
    ("map", n_!("Map"), name::LOCATION),
    ("saved", n_!("Saved"), name::STARRED),
    ("manage", n_!("Library and downloads"), name::DRIVE),
    ("about", n_!("About"), name::SETTINGS),
];

/// Every key of the window, for the shortcuts overlay (`?`, F1), in the language of the moment. Setup (`library`
/// false) has the general keys alone. README.md's table lists the same.
pub fn shortcuts(library: bool) -> Vec<ShortcutSection> {
    let general = ShortcutSection::new(
        &tr("General"),
        &[
            ("? / F1", &tr("Show or hide the shortcuts")),
            ("Ctrl+W", &tr("Close the window; while a download runs, ask first")),
            ("Ctrl+Q", &tr("Quit; while a download runs, ask first")),
            ("Escape", &tr("Close a question without changing anything")),
        ],
    );
    if !library {
        return vec![general];
    }
    vec![
        ShortcutSection::new(
            &tr("Moving around"),
            &[
                ("Ctrl+K", &tr("Search, with the keyboard in its field")),
                ("Ctrl+F", &tr("Find in the page or document on show; elsewhere, Search")),
                ("Ctrl+B", &tr("Show or hide the sidebar")),
                ("Alt+Left", &tr("Back")),
                ("Alt+Right", &tr("Forward")),
                ("Ctrl+D", &tr("Save this, or remove it from Saved")),
                ("Up, Down", &tr("The screen above or below, in the sidebar")),
                ("Enter, Space", &tr("Open a card")),
            ],
        ),
        ShortcutSection::new(
            &tr("Reader"),
            &[
                ("Left, Right", &tr("The page before, the page after")),
                ("Home, End", &tr("The first page, the last page")),
                ("Up, Down, K, J", &tr("Scroll within the page")),
                ("Space, Page Down", &tr("Down a screenful, then on to the next page")),
                ("Shift+Space, Backspace, Page Up", &tr("Up a screenful, then back to the page before")),
                ("+, -", &tr("Zoom in, zoom out")),
                ("0", &tr("Fit the page to the window's width")),
                ("Enter, Shift+Enter", &tr("In the find bar: the next match, the match before")),
                ("Escape", &tr("Close the find bar")),
            ],
        ),
        ShortcutSection::new(
            &tr("Map"),
            &[("+, -", &tr("Zoom in, zoom out")), ("Left, Right, Up, Down", &tr("Move the map"))],
        ),
        general,
    ]
}

/// What a saved item says under its title, from its key: in the language of the moment, not the one it was saved
/// in. A saved web page keeps what it was saved with, the line its page shows above itself (`stored`).
pub fn saved_sub(key: &str, stored: &str) -> String {
    match key.split(':').next() {
        Some("read") => tr!("Page {}", key.rsplit(':').next().unwrap_or("1")),
        Some("wiki") => tr("Encyclopedia article"),
        Some("watch") => tr("Video"),
        Some("guide") => tr("Guide"),
        _ if !stored.is_empty() => stored.to_owned(),
        _ => tr("Saved page"),
    }
}

const SIDEBAR_WIDTH: i32 = 200;
/// The window width from which the sidebar is beside the screens: under it the widest of them (search, 522 pixels)
/// would not fit, and the sidebar would come and go from one screen to the next.
const SIDEBAR_BESIDE_FROM: i32 = 760;

pub struct App {
    pub engine: Arc<Engine>,
    pub window: gtk::ApplicationWindow,
    /// The window's content under what is shown over it: toasts (an undo, a file that could not be saved) and questions.
    pub shell: Rc<omui::Shell>,
    stack: gtk::Stack,
    sidebar: gtk::ListBox,
    split: omui::Split,
    /// The sidebar's labels, by section id: written again in a language switch.
    section_labels: Vec<(&'static str, gtk::ListBoxRow, gtk::Label)>,
    /// Whether the person keeps the sidebar beside the screens, where the window has room for both.
    sidebar_beside: Cell<bool>,
    /// Whether the split was collapsed when it last told of a change: see its `connect_changed`.
    collapsed: Cell<bool>,
    title: gtk::Label,
    back: gtk::Button,
    forward: gtk::Button,
    star: gtk::Button,
    history: RefCell<Vec<Route>>,
    pos: Cell<usize>,
    syncing: Cell<bool>,
    /// The last way into the sidebar was the keyboard (arrows), not a click: see `sidebar_has_focus`.
    sidebar_keyed: Cell<bool>,
    /// The screen being shown is a step back or forward in the history, not a new visit (a video then carries on).
    revisiting: Cell<bool>,
    /// Titles the screens reported after loading: a screen shown again does not load again, so it does not report again.
    titles: RefCell<HashMap<String, String>>,
    /// The sidebar section of the screen shown, so a row re-selected by focus (not by the person) does nothing.
    section: RefCell<String>,
    search: Rc<SearchPage>,
    reader: Rc<Reader>,
    guides: Rc<Guides>,
    videos: Rc<Videos>,
    web: Rc<WebPage>,
    browse: Rc<Browse>,
    encyclopedias: Rc<Encyclopedias>,
    map: Rc<MapPage>,
    pub manage: Rc<crate::setup::SetupPage>,
}

impl App {
    /// Fills `window`, which main.rs has had on screen since before the engine was ready, with the library's screens
    /// in place of what it showed while waiting.
    pub fn build(window: gtk::ApplicationWindow, shell: Rc<omui::Shell>, engine: Arc<Engine>, theme: Rc<Theme>) -> Rc<Self> {

        let title = gtk::Label::new(Some(&tr("Guides")));
        title.add_css_class("title");
        title.set_ellipsize(gtk::pango::EllipsizeMode::End);
        let header = omui::header(Some(title.upcast_ref()));
        let toggle = icon_button(name::SIDEBAR, "");
        omui::i18n::bind_tooltip(&toggle, "Show or hide the sidebar (Ctrl+B)");
        toggle.add_css_class("sidebar-toggle");
        let back = icon_button(name::BACK, "");
        omui::i18n::bind_tooltip(&back, "Back (Alt+Left)");
        let forward = icon_button(name::FORWARD, "");
        omui::i18n::bind_tooltip(&forward, "Forward (Alt+Right)");
        // Its tooltip says whether what is on screen is saved (`update_star`).
        let star = icon_button(name::STAR, &tr("Save this (Ctrl+D)"));
        header.pack_start(&toggle);
        header.pack_start(&back);
        header.pack_start(&forward);
        header.pack_end(&star);
        window.set_titlebar(Some(&header));

        let sidebar = gtk::ListBox::new();
        sidebar.set_selection_mode(gtk::SelectionMode::Single);
        let mut section_labels = Vec::new();
        for (id, label, icon) in SECTIONS {
            let row = omui::sidebar_row(id, Some(icon), &omui::i18n::translate(label), None);
            if let Some(text) = row.child().and_then(|c| c.first_child()).and_then(|i| i.next_sibling()).and_downcast::<gtk::Label>() {
                // Wrapped, not cut short: "Library and downloads" is longer than the sidebar is wide in English already.
                text.set_ellipsize(gtk::pango::EllipsizeMode::None);
                text.set_wrap(true);
                section_labels.push((*id, row.clone(), text));
            }
            // About's row is there to hold the selection, unseen: the footer is its way in.
            row.set_visible(*id != "about");
            // A gap opens each group: search, browse, yours.
            if matches!(*id, "guides" | "saved") {
                row.set_margin_top(10);
            }
            sidebar.append(&row);
        }
        let footer = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        footer.add_css_class("sidebar-footer");
        footer.append(&omui::icon(name::OFFLINE));
        let offline = gtk::Label::new(None);
        omui::i18n::bind_label(&offline, "Works offline");
        footer.append(&offline);
        let about = gtk::Button::new();
        omui::i18n::bind_label(&about, "About");
        about.add_css_class("flat");
        let gap = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        gap.set_hexpand(true);
        footer.append(&gap);
        footer.append(&about);
        let sidebar_shell = gtk::Box::new(gtk::Orientation::Vertical, 0);
        sidebar_shell.add_css_class("sidebar");
        let scroll = gtk::ScrolledWindow::builder().hscrollbar_policy(gtk::PolicyType::Never).vexpand(true).child(&sidebar).build();
        sidebar_shell.append(&scroll);
        sidebar_shell.append(&footer);

        let stack = gtk::Stack::new();
        stack.set_transition_type(gtk::StackTransitionType::Crossfade);
        stack.set_transition_duration(120);
        // Each screen sizes the window by itself: a homogeneous stack would hold every screen to the widest one's width.
        stack.set_hhomogeneous(false);
        stack.set_vhomogeneous(false);
        // A window too narrow for the sidebar beside the screen on show has one pane, the screen: a third of a
        // display is narrower than the sidebar and the reader's bar together, and a window that asks for more than
        // its tile is wider than its tile.
        let split = omui::Split::new(&sidebar_shell, &stack, (SIDEBAR_WIDTH, SIDEBAR_WIDTH), Some(SIDEBAR_BESIDE_FROM));
        shell.overlay.set_child(Some(&split.widget));

        // The screens hold the window weakly, and none reaches it while it is being built.
        let app = Rc::new_cyclic(|app: &Weak<Self>| {
            let search = SearchPage::build(app);
            stack.add_named(&search.root, Some("search"));
            let reader = Reader::build(app);
            stack.add_named(&reader.root, Some("reader"));
            let guides = Guides::build(app, engine.clone());
            stack.add_named(&guides.list_root, Some("guides"));
            stack.add_named(&guides.page_root, Some("guide"));
            let videos = Videos::build(app);
            stack.add_named(&videos.list_root, Some("videos"));
            stack.add_named(&videos.player_root, Some("watch"));
            let web = WebPage::build(app, engine.clone(), theme.clone());
            stack.add_named(&web.root, Some("web"));
            let browse = Browse::build(app);
            stack.add_named(&browse.library_root, Some("library"));
            stack.add_named(&browse.shelf_root, Some("shelf"));
            stack.add_named(&browse.saved_root, Some("saved"));
            stack.add_named(&browse.about_root, Some("about"));
            let encyclopedias = Encyclopedias::build(app);
            stack.add_named(&encyclopedias.root, Some("encyclopedias"));
            let map = MapPage::build(app, &theme);
            stack.add_named(&map.root, Some("map"));
            let manage = crate::setup::SetupPage::build(engine.clone());
            manage.set_window(&window);
            stack.add_named(&manage.root, Some("manage"));
            Self {
                engine,
                window,
                shell,
                stack,
                sidebar,
                split,
                section_labels,
                sidebar_beside: Cell::new(true),
                collapsed: Cell::new(false),
                title,
                back,
                forward,
                star,
                history: RefCell::new(Vec::new()),
                pos: Cell::new(0),
                syncing: Cell::new(false),
                sidebar_keyed: Cell::new(false),
                revisiting: Cell::new(false),
                titles: RefCell::new(HashMap::new()),
                section: RefCell::new(String::new()),
                search,
                reader,
                guides,
                videos,
                web,
                browse,
                encyclopedias,
                map,
                manage,
            }
        });

        let a = app.clone();
        toggle.connect_clicked(move |_| a.toggle_sidebar());
        // One pane is the screen, until Ctrl+B asks for the sidebar in its place; with room for both again, the
        // sidebar is back beside it unless the person had put it away.
        let a = app.clone();
        app.split.connect_changed(move |split| {
            let collapsed = split.is_collapsed();
            if a.collapsed.replace(collapsed) != collapsed {
                split.set_show_sidebar(!collapsed && a.sidebar_beside.get());
            }
        });
        let a = app.clone();
        about.connect_clicked(move |_| a.navigate(Route::Section("about")));
        let a = app.clone();
        app.back.connect_clicked(move |_| a.go(-1));
        let a = app.clone();
        app.forward.connect_clicked(move |_| a.go(1));
        let a = app.clone();
        app.star.connect_clicked(move |_| a.toggle_saved());
        let a = app.clone();
        app.sidebar.connect_row_selected(move |_, row| {
            if a.syncing.get() {
                return;
            }
            if let Some(row) = row {
                let id = row.widget_name();
                if *a.section.borrow() == id.as_str() {
                    return;
                }
                let route = if id == "search" {
                    let s = a.search.current();
                    Route::Search { q: s.0, kind: s.1 }
                } else {
                    Route::Section(SECTIONS.iter().find(|s| s.0 == id.as_str()).map(|s| s.0).unwrap_or("guides"))
                };
                a.navigate(route);
            }
        });
        // How the person moves in the sidebar: keys keep the focus there, a click hands it to the screen.
        let keys = gtk::EventControllerKey::new();
        keys.set_propagation_phase(gtk::PropagationPhase::Capture);
        let a = app.clone();
        keys.connect_key_pressed(move |_, _, _, _| {
            a.sidebar_keyed.set(true);
            glib::Propagation::Proceed
        });
        app.sidebar.add_controller(keys);
        let click = gtk::GestureClick::new();
        click.set_propagation_phase(gtk::PropagationPhase::Capture);
        let a = app.clone();
        click.connect_pressed(move |_, _, _, _| a.sidebar_keyed.set(false));
        app.sidebar.add_controller(click);
        // A click on the row already selected goes back to that section's first screen (from a guide to the guides).
        // Enter or a click on a row chooses its screen: in a window of one pane, where the arrows only walk the
        // sidebar, the screen then takes its place.
        let a = app.clone();
        app.sidebar.connect_row_activated(move |_, row| {
            if let Some(s) = SECTIONS.iter().find(|s| s.0 == row.widget_name().as_str() && s.0 != "search")
                && a.current() != Some(Route::Section(s.0))   // a click on another row has just gone there (row-selected)
            {
                a.navigate(Route::Section(s.0));
            }
            a.screen_in_view();
        });
        app.keys();
        // A language switch: what was written from text is written again, and the screen on show drawn anew.
        omui::i18n::on_change(&app.window, crate::cards::weak(&app, |a| a.retranslate()));
        app.navigate(Route::Section("guides"));
        // LASTBOOK_OPEN opens a screen at start: it is how `lastbook open` works (app/cli.py). Any Route::from_key form.
        if let Ok(open) = std::env::var("LASTBOOK_OPEN")
            && let Some(r) = Route::from_key(&open)
        {
            app.navigate(r);
        }
        app
    }

    fn keys(self: &Rc<Self>) {
        let keys = gtk::EventControllerKey::new();
        keys.set_propagation_phase(gtk::PropagationPhase::Capture);
        let a = self.clone();
        keys.connect_key_pressed(move |_, key, _, state| {
            let alt = state.contains(gdk::ModifierType::ALT_MASK);
            let ctrl = state.contains(gdk::ModifierType::CONTROL_MASK);
            // By the key's lower case: Caps Lock does not turn Ctrl+F into something else.
            match key.to_lower() {
                gdk::Key::Left if alt => a.go(-1),
                gdk::Key::Right if alt => a.go(1),
                gdk::Key::b if ctrl => a.toggle_sidebar(),
                gdk::Key::d if ctrl => a.toggle_saved(),
                gdk::Key::f if ctrl && a.find_in_page() => {}
                gdk::Key::f | gdk::Key::k if ctrl => a.focus_search(),
                _ => return glib::Propagation::Proceed,
            }
            glib::Propagation::Stop
        });
        self.window.add_controller(keys);
    }

    /// Writes again, in the language now chosen, what was built from text: the sidebar, and every screen, the one on
    /// show at once and the others when they are next shown.
    fn retranslate(self: &Rc<Self>) {
        for (id, row, label) in &self.section_labels {
            let text = SECTIONS.iter().find(|s| s.0 == *id).map(|s| omui::i18n::translate(s.1)).unwrap_or_default();
            label.set_text(&text);
            row.update_property(&[gtk::accessible::Property::Label(&text)]);
        }
        self.search.retranslate();
        self.guides.retranslate();
        self.videos.retranslate();
        self.browse.retranslate();
        self.encyclopedias.retranslate();
        self.map.retranslate();
        self.manage.retranslate();
        self.reader.retranslate();
        // Shown again as a step back would show it: a video carries on where it is.
        if let Some(route) = self.current() {
            self.revisiting.set(true);
            self.show(&route);
            self.revisiting.set(false);
        }
    }

    /// Beside the screen in a window with room for both, in its place in a narrower one.
    fn toggle_sidebar(&self) {
        let show = !self.split.shows_sidebar();
        if !self.split.is_collapsed() {
            self.sidebar_beside.set(show);
        }
        self.split.set_show_sidebar(show);
    }

    /// In a window of one pane, the screen asked for takes the sidebar's place.
    fn screen_in_view(&self) {
        if self.split.is_collapsed() {
            self.split.set_show_sidebar(false);
        }
    }

    /// Ctrl+F on a reading screen finds in it (reader.rs, web.rs); anywhere else it goes to Search, as Ctrl+K does.
    fn find_in_page(&self) -> bool {
        let found = match self.stack.visible_child_name().as_deref() {
            Some("reader") => self.reader.find(),
            Some("web") => self.web.find(),
            _ => false,
        };
        if found {
            self.screen_in_view();
        }
        found
    }

    fn focus_search(self: &Rc<Self>) {
        if self.stack.visible_child_name().as_deref() != Some("search") {
            let (q, kind) = self.search.current();
            self.navigate(Route::Search { q, kind });
        }
        self.screen_in_view();
        self.search.focus();
    }

    fn current(&self) -> Option<Route> {
        self.history.borrow().get(self.pos.get()).cloned()
    }

    /// Stars or un-stars what is on screen. Un-starring can be undone for a few seconds, since one key does it; a
    /// change that could not be written to the disk is said, not left looking saved.
    fn toggle_saved(self: &Rc<Self>) {
        let Some(key) = self.current().and_then(|r| r.key()) else { return };
        let mut items = load_saved();
        let removed = items.iter().position(|s| s.key == key).map(|i| items.remove(i));
        if removed.is_none() {
            let title = self.title.text().to_string();
            // A saved web page keeps whose it is: the line the page shows above itself.
            let meta = match self.current() {
                Some(Route::Web { meta, .. }) => meta,
                _ => String::new(),
            };
            let sub = saved_sub(&key, &meta);
            items.push(Saved { key, title, sub, at: glib::real_time() / 1_000_000 });
        }
        let stored = self.store(&items);
        self.update_star();
        if let (true, Some(item)) = (stored, removed) {
            let me = Rc::downgrade(self);
            self.shell.toast_action(&tr("Removed from Saved."), &tr("Undo"), 8, move || {
                let Some(me) = me.upgrade() else { return };
                let mut items = load_saved();
                put_back(&mut items, item.clone());
                me.store(&items);
                me.update_star();
            });
        }
    }

    /// Writes the saved items, and says so in the window if they could not be written. Whether they were.
    fn store(&self, items: &[Saved]) -> bool {
        match store_saved(items) {
            Ok(()) => true,
            Err(e) => {
                self.shell.toast(&tr!("Lastbook could not write your saved items: {}", e));
                false
            }
        }
    }

    pub fn update_star(&self) {
        let key = self.current().and_then(|r| r.key());
        self.star.set_visible(key.is_some());
        let saved = key.is_some_and(|k| load_saved().iter().any(|s| s.key == k));
        self.star.set_icon_name(if saved { name::STARRED } else { name::STAR });
        let tip = if saved { tr("Saved — click to remove (Ctrl+D)") } else { tr("Save this (Ctrl+D)") };
        self.star.set_tooltip_text(Some(&tip));
        self.star.update_property(&[gtk::accessible::Property::Label(&tip)]);
    }

    /// Shows `route` and records it, dropping anything ahead of the current place (as a browser does).
    pub fn navigate(self: &Rc<Self>, route: Route) {
        {
            let mut h = self.history.borrow_mut();
            let pos = self.pos.get();
            if h.get(pos) == Some(&route) {
                drop(h);
                self.show(&route);
                return;
            }
            if !h.is_empty() {
                h.truncate(pos + 1);
            }
            // A new search replaces the search before it, so back leaves search rather than stepping through every query.
            if matches!((h.last(), &route), (Some(Route::Search { .. }), Route::Search { .. })) {
                h.pop();
            }
            h.push(route.clone());
            self.pos.set(h.len() - 1);
        }
        self.show(&route);
    }

    fn go(self: &Rc<Self>, step: i32) {
        let len = self.history.borrow().len() as i32;
        let next = self.pos.get() as i32 + step;
        if next < 0 || next >= len {
            return;
        }
        self.pos.set(next as usize);
        let route = self.history.borrow()[next as usize].clone();
        self.revisiting.set(true);
        self.show(&route);
        self.revisiting.set(false);
    }

    fn show(self: &Rc<Self>, route: &Route) {
        if !matches!(route, Route::Watch { .. }) {
            self.videos.pause();
        }
        let (page, section, title) = match route {
            Route::Search { q, kind } => {
                self.search.run(q, kind);
                ("search", "search", tr("Search"))
            }
            Route::Read { doc, page } => {
                self.reader.open(doc, *page);
                ("reader", "library", String::new())
            }
            Route::Guide(id) => {
                let title = self.guides.show(id).unwrap_or_else(|| tr("Guides"));
                ("guide", "guides", title)
            }
            Route::Watch { video, t } => {
                self.videos.play(video, *t, self.revisiting.get());
                ("watch", "videos", String::new())
            }
            Route::Wiki { path } => {
                let enc = encyclopedia_name(path.split('/').next().unwrap_or_default());
                self.web.open(&format!("/zim/{path}"), &tr!("{} · community-written: check safety-critical steps against a manual", enc), true);
                let article = path.rsplit('/').next().unwrap_or_default().replace('_', " ");
                ("web", "encyclopedias", urlencoding::decode(&article).map(|s| s.into_owned()).unwrap_or(article))
            }
            Route::Web { path, meta } => {
                self.web.open(path, meta, false);
                ("web", "library", String::new())
            }
            Route::MapAt { lat, lon, label } => {
                self.map.show_at(*lat, *lon, label);
                ("map", "map", tr("Map"))
            }
            Route::Shelf(id) => {
                let name = id.replace('-', " ");
                self.browse.show_shelf(id, &name);
                ("shelf", "library", tr("Library"))
            }
            Route::Section(id) => {
                match *id {
                    "videos" => self.videos.show_list(),
                    "library" => self.browse.show_library(),
                    "saved" => self.browse.show_saved(),
                    "about" => self.browse.show_about(),
                    "map" => self.map.show(),
                    "encyclopedias" => self.encyclopedias.show(),
                    "manage" => self.manage.refresh(),
                    _ => {}
                }
                (*id, *id, SECTIONS.iter().find(|s| s.0 == *id).map(|s| omui::i18n::translate(s.1)).unwrap_or_default())
            }
        };
        self.stack.set_visible_child_name(page);
        // Not under someone walking the sidebar with the arrows: each row would take the sidebar away from them.
        if !self.sidebar_has_focus() {
            self.screen_in_view();
        }
        let title = if title.is_empty() { route.title_key().and_then(|k| self.titles.borrow().get(&k).cloned()).unwrap_or_default() } else { title };
        // A page with no <title> of its own is named by its file until (if ever) it says otherwise.
        let title = match route {
            Route::Web { path, .. } if title.is_empty() => {
                let file = path.split(['?', '#']).next().unwrap_or(path).rsplit('/').next().unwrap_or_default();
                urlencoding::decode(file).map(|f| f.into_owned()).unwrap_or_else(|_| file.to_owned())
            }
            _ => title,
        };
        if !title.is_empty() {
            self.set_title(&title);
        }
        self.section.replace(section.to_owned());
        self.syncing.set(true);
        let mut i = 0;
        while let Some(row) = self.sidebar.row_at_index(i) {
            if row.widget_name() == section {
                self.sidebar.select_row(Some(&row));
            }
            i += 1;
        }
        self.syncing.set(false);
        let len = self.history.borrow().len();
        self.back.set_sensitive(self.pos.get() > 0);
        self.forward.set_sensitive(self.pos.get() + 1 < len);
        self.update_star();
    }

    /// Replaces the current history entry, and shows it if asked (a document that turned out to be a web page; a
    /// turned page changes the entry only).
    pub fn replace_route(self: &Rc<Self>, route: Route, show: bool) {
        let pos = self.pos.get();
        if let Some(r) = self.history.borrow_mut().get_mut(pos) {
            *r = route.clone();
        }
        if show {
            self.show(&route);
        }
    }

    /// Whether stack page `page` is the one on show: an answer that arrives after the person moved on must not rename
    /// or replace the screen they moved to.
    pub fn showing(&self, page: &str) -> bool {
        self.stack.visible_child_name().as_deref() == Some(page)
    }

    /// Whether the keyboard is in the sidebar: a screen shown by arrowing through it leaves the focus there.
    /// A click on a row means "go there", and the screen takes the keyboard (the search field).
    pub fn sidebar_has_focus(&self) -> bool {
        self.sidebar_keyed.get()
            && gtk::prelude::GtkWindowExt::focus(&self.window).is_some_and(|f| f.is_ancestor(&self.sidebar) || f == *self.sidebar.upcast_ref::<gtk::Widget>())
    }

    /// Whether document `doc` is the screen in the history's current place.
    pub fn reading(&self, doc: &str) -> bool {
        matches!(self.current(), Some(Route::Read { doc: d, .. }) if d == doc)
    }

    /// A title a screen learned once it had loaded.
    pub fn report_title(&self, page: &str, title: &str) {
        if self.showing(page) {
            self.set_title(title);
        }
    }

    fn set_title(&self, title: &str) {
        if let Some(k) = self.current().and_then(|r| r.title_key()) {
            self.titles.borrow_mut().insert(k, title.to_owned());
        }
        self.title.set_text(title);
        self.window.set_title(Some(&tr!("{} — Lastbook", title)));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn saved_keys_round_trip() {
        for r in [
            Route::Read { doc: "med-1".into(), page: 12 },
            Route::Wiki { path: "mdwiki_en_all_maxi_2025-11.zim/Snakebite".into() },
            Route::Watch { video: "video-abc".into(), t: 75.0 },
            Route::Guide("hurt".into()),
        ] {
            assert_eq!(Route::from_key(&r.key().unwrap()), Some(r));
        }
        assert_eq!(Route::Search { q: "x".into(), kind: "any".into() }.key(), None, "a search is not saved");
        assert_eq!(Route::from_key("section:map"), Some(Route::Section("map")));
        assert_eq!(Route::from_key("section:manage"), Some(Route::Section("manage")));
        assert_eq!(Route::from_key("manage:"), Some(Route::Section("manage")), "app/cli.py's `lastbook open manage`");
        assert_eq!(Route::from_key("nonsense"), None);
    }
}
