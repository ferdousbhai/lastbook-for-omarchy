// SPDX-License-Identifier: MIT
//! The window: a header bar (sidebar toggle, back and forward, the screen's title, save), the sidebar of screens, and a
//! stack of pages. Navigation keeps a history like a browser's, so back and forward work across screens.

use std::{
    cell::{Cell, RefCell},
    collections::HashMap,
    rc::Rc,
    sync::Arc,
};

use gtk::{gdk, glib, prelude::*};
use omui::{Theme, icon_button, icons::name};

use crate::{
    cards::encyclopedia_name,
    chat::ChatPage,
    engine::Engine,
    guides::Guides,
    library::{Browse, Saved, load_saved, store_saved},
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
    Chat { id: Option<String>, about: Option<String> },
    Guide(String),
    Watch { video: String, t: f64 },
    Wiki { path: String },
    Web { path: String, meta: String },
    MapAt { lat: f64, lon: f64, label: String },
    Shelf(String),
    /// Library and downloads (setup, on the library that is open).
    Manage,
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
            "manage" => Route::Manage,
            "map" => {
                let mut it = rest.splitn(3, ',');
                let (lat, lon) = (it.next()?.trim().parse().ok()?, it.next()?.trim().parse().ok()?);
                Route::MapAt { lat, lon, label: it.next().unwrap_or("Here").to_owned() }
            }
            "section" => Route::Section(SECTIONS.iter().find(|s| s.0 == rest).map(|s| s.0)?),
            _ => return None,
        })
    }
}

/// The sidebar's screens, in order: (id, label, icon). About is a screen too, but its way in is the sidebar's footer.
pub const SECTIONS: &[(&str, &str, &str)] = &[
    ("search", "Search", name::SEARCH),
    ("chat", "Chat", name::CHAT),
    ("guides", "Guides", name::INFO),
    ("library", "Library", name::DOCUMENT),
    ("videos", "Videos", name::VIDEO),
    ("map", "Map", name::LOCATION),
    ("saved", "Saved", name::STARRED),
    ("about", "About", name::SETTINGS),
];

pub struct App {
    pub engine: Arc<Engine>,
    pub theme: Rc<Theme>,
    pub window: gtk::ApplicationWindow,
    stack: gtk::Stack,
    sidebar: gtk::ListBox,
    sidebar_shell: gtk::Box,
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
    pub search: RefCell<Option<Rc<SearchPage>>>,
    pub reader: RefCell<Option<Rc<Reader>>>,
    pub chat: RefCell<Option<Rc<ChatPage>>>,
    pub guides: RefCell<Option<Rc<Guides>>>,
    pub videos: RefCell<Option<Rc<Videos>>>,
    pub web: RefCell<Option<Rc<WebPage>>>,
    pub browse: RefCell<Option<Rc<Browse>>>,
    pub map: RefCell<Option<Rc<MapPage>>>,
    pub manage: RefCell<Option<Rc<crate::setup::SetupPage>>>,
}

impl App {
    pub fn build(app: &gtk::Application, engine: Arc<Engine>, theme: Rc<Theme>) -> Rc<Self> {
        let window = gtk::ApplicationWindow::builder().application(app).title("Lastbook").default_width(1280).default_height(820).build();

        let title = gtk::Label::new(Some("Guides"));
        title.add_css_class("title");
        title.set_ellipsize(gtk::pango::EllipsizeMode::End);
        let header = omui::header(Some(title.upcast_ref()));
        let toggle = icon_button(name::SIDEBAR, "Show or hide the sidebar (Ctrl+B)");
        toggle.add_css_class("sidebar-toggle");
        let back = icon_button(name::BACK, "Back (Alt+Left)");
        let forward = icon_button(name::FORWARD, "Forward (Alt+Right)");
        let star = icon_button(name::STAR, "Save this (Ctrl+D)");
        header.pack_start(&toggle);
        header.pack_start(&back);
        header.pack_start(&forward);
        header.pack_end(&star);
        window.set_titlebar(Some(&header));

        let sidebar = gtk::ListBox::new();
        sidebar.set_selection_mode(gtk::SelectionMode::Single);
        for (id, label, icon) in SECTIONS {
            let row = omui::sidebar_row(id, Some(icon), label, None);
            // About's row is there to hold the selection, unseen: the footer is its way in.
            row.set_visible(*id != "about");
            // A gap opens each group: ask, browse, yours.
            if matches!(*id, "guides" | "saved") {
                row.set_margin_top(10);
            }
            sidebar.append(&row);
        }
        let footer = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        footer.add_css_class("sidebar-footer");
        footer.append(&omui::icon(name::OFFLINE));
        footer.append(&gtk::Label::new(Some("Works offline")));
        let about = gtk::Button::with_label("About");
        about.add_css_class("flat");
        let gap = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        gap.set_hexpand(true);
        footer.append(&gap);
        footer.append(&about);
        let sidebar_shell = gtk::Box::new(gtk::Orientation::Vertical, 0);
        sidebar_shell.add_css_class("sidebar");
        sidebar_shell.set_size_request(200, -1);
        sidebar_shell.set_hexpand(false);
        let scroll = gtk::ScrolledWindow::builder().hscrollbar_policy(gtk::PolicyType::Never).vexpand(true).child(&sidebar).build();
        sidebar_shell.append(&scroll);
        sidebar_shell.append(&footer);

        let stack = gtk::Stack::new();
        stack.set_hexpand(true);
        stack.set_transition_type(gtk::StackTransitionType::Crossfade);
        stack.set_transition_duration(120);
        // Each screen sizes the window by itself: a homogeneous stack would hold every screen to the widest one's width.
        stack.set_hhomogeneous(false);
        stack.set_vhomogeneous(false);
        let body = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        body.append(&sidebar_shell);
        body.append(&stack);
        window.set_child(Some(&body));

        let app = Rc::new(Self {
            engine,
            theme,
            window,
            stack,
            sidebar,
            sidebar_shell,
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
            search: RefCell::new(None),
            reader: RefCell::new(None),
            chat: RefCell::new(None),
            guides: RefCell::new(None),
            videos: RefCell::new(None),
            web: RefCell::new(None),
            browse: RefCell::new(None),
            map: RefCell::new(None),
            manage: RefCell::new(None),
        });

        let search = SearchPage::build(&app);
        app.stack.add_named(&search.root, Some("search"));
        app.search.replace(Some(search));
        let reader = Reader::build(&app);
        app.stack.add_named(&reader.root, Some("reader"));
        app.reader.replace(Some(reader));
        let chat = ChatPage::build(&app);
        app.stack.add_named(&chat.root, Some("chat"));
        app.chat.replace(Some(chat));
        let guides = Guides::build(&app);
        app.stack.add_named(&guides.list_root, Some("guides"));
        app.stack.add_named(&guides.page_root, Some("guide"));
        app.guides.replace(Some(guides));
        let videos = Videos::build(&app);
        app.stack.add_named(&videos.list_root, Some("videos"));
        app.stack.add_named(&videos.player_root, Some("watch"));
        app.videos.replace(Some(videos));
        let web = WebPage::build(&app);
        app.stack.add_named(&web.root, Some("web"));
        app.web.replace(Some(web));
        let browse = Browse::build(&app);
        app.stack.add_named(&browse.library_root, Some("library"));
        app.stack.add_named(&browse.shelf_root, Some("shelf"));
        app.stack.add_named(&browse.saved_root, Some("saved"));
        app.stack.add_named(&browse.about_root, Some("about"));
        app.browse.replace(Some(browse));
        let map = MapPage::build(&app);
        app.stack.add_named(&map.root, Some("map"));
        app.map.replace(Some(map));
        let manage = crate::setup::SetupPage::build(app.engine.clone());
        manage.set_window(&app.window);
        app.stack.add_named(&manage.root, Some("manage"));
        app.manage.replace(Some(manage));

        let a = app.clone();
        toggle.connect_clicked(move |_| a.toggle_sidebar());
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
                    let s = a.search.borrow().as_ref().map(|s| s.current()).unwrap_or_default();
                    Route::Search { q: s.0, kind: s.1 }
                } else if id == "chat" {
                    Route::Chat { id: None, about: None }
                } else {
                    let id: &'static str = SECTIONS.iter().find(|s| s.0 == id.as_str()).map(|s| s.0).unwrap_or("guides");
                    Route::Section(id)
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
        let a = app.clone();
        app.sidebar.connect_row_activated(move |_, row| {
            if let Some(s) = SECTIONS.iter().find(|s| s.0 == row.widget_name().as_str() && !matches!(s.0, "search" | "chat"))
                && a.current() != Some(Route::Section(s.0))   // a click on another row has just gone there (row-selected)
            {
                a.navigate(Route::Section(s.0));
            }
        });
        app.keys();
        app.navigate(Route::Section("guides"));
        // LASTBOOK_OPEN opens a screen at start: it is how `lastbook open` works (app/cli.py). Any Route::from_key form,
        // or chat:<question>.
        if let Ok(open) = std::env::var("LASTBOOK_OPEN") {
            if let Some(q) = open.strip_prefix("chat:") {
                app.navigate(Route::Chat { id: None, about: None });
                if let Some(c) = app.chat.borrow().as_ref() {
                    c.ask_text(q);
                }
            } else if let Some(r) = Route::from_key(&open) {
                app.navigate(r);
            }
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
                gdk::Key::f | gdk::Key::k if ctrl => a.focus_search(),
                _ => return glib::Propagation::Proceed,
            }
            glib::Propagation::Stop
        });
        self.window.add_controller(keys);
    }

    fn toggle_sidebar(&self) {
        self.sidebar_shell.set_visible(!self.sidebar_shell.is_visible());
    }

    fn focus_search(self: &Rc<Self>) {
        if self.stack.visible_child_name().as_deref() != Some("search") {
            let (q, kind) = self.search.borrow().as_ref().map(|s| s.current()).unwrap_or_default();
            self.navigate(Route::Search { q, kind });
        }
        if let Some(s) = self.search.borrow().as_ref() {
            s.focus();
        }
    }

    fn current(&self) -> Option<Route> {
        self.history.borrow().get(self.pos.get()).cloned()
    }

    /// Stars or un-stars what is on screen.
    fn toggle_saved(self: &Rc<Self>) {
        let Some(key) = self.current().and_then(|r| r.key()) else { return };
        let mut items = load_saved();
        if let Some(i) = items.iter().position(|s| s.key == key) {
            items.remove(i);
        } else {
            let title = self.title.text().to_string();
            let sub = match key.split(':').next() {
                Some("read") => format!("Page {}", key.rsplit(':').next().unwrap_or("1")),
                Some("wiki") => "Encyclopedia article".into(),
                Some("watch") => "Video".into(),
                Some("guide") => "Guide".into(),
                // A saved web page keeps whose it is: the line the page shows above itself.
                _ => match self.current() {
                    Some(Route::Web { meta, .. }) if !meta.is_empty() => meta,
                    _ => "Saved page".into(),
                },
            };
            items.push(Saved { key, title, sub, at: glib::real_time() / 1_000_000 });
        }
        store_saved(&items);
        self.update_star();
    }

    pub fn update_star(&self) {
        let key = self.current().and_then(|r| r.key());
        self.star.set_visible(key.is_some());
        let saved = key.is_some_and(|k| load_saved().iter().any(|s| s.key == k));
        self.star.set_icon_name(if saved { name::STARRED } else { name::STAR });
        self.star.set_tooltip_text(Some(if saved { "Saved — click to remove (Ctrl+D)" } else { "Save this (Ctrl+D)" }));
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
        if !matches!(route, Route::Watch { .. })
            && let Some(v) = self.videos.borrow().as_ref()
        {
            v.pause();
        }
        let (page, section, title) = match route {
            Route::Search { q, kind } => {
                if let Some(s) = self.search.borrow().as_ref() {
                    s.run(q, kind);
                }
                ("search", "search", "Search".to_owned())
            }
            Route::Read { doc, page } => {
                if let Some(r) = self.reader.borrow().as_ref() {
                    r.open(doc, *page);
                }
                ("reader", "library", String::new())
            }
            Route::Chat { id, about } => {
                if let Some(c) = self.chat.borrow().as_ref() {
                    c.show(id.as_deref(), about.as_deref());
                }
                ("chat", "chat", "Chat".to_owned())
            }
            Route::Guide(id) => {
                let title = self.guides.borrow().as_ref().and_then(|g| g.show(id)).unwrap_or_else(|| "Guides".into());
                ("guide", "guides", title)
            }
            Route::Watch { video, t } => {
                if let Some(v) = self.videos.borrow().as_ref() {
                    v.play(video, *t, self.revisiting.get());
                }
                ("watch", "videos", String::new())
            }
            Route::Wiki { path } => {
                let enc = encyclopedia_name(path.split('/').next().unwrap_or_default());
                if let Some(w) = self.web.borrow().as_ref() {
                    w.open(&format!("/zim/{path}"), &format!("{enc} · community-written: check safety-critical steps against a manual"), true);
                }
                let article = path.rsplit('/').next().unwrap_or_default().replace('_', " ");
                ("web", "library", urlencoding::decode(&article).map(|s| s.into_owned()).unwrap_or(article))
            }
            Route::Web { path, meta } => {
                if let Some(w) = self.web.borrow().as_ref() {
                    w.open(path, meta, false);
                }
                ("web", "library", String::new())
            }
            Route::MapAt { lat, lon, label } => {
                if let Some(m) = self.map.borrow().as_ref() {
                    m.show_at(*lat, *lon, label);
                }
                ("map", "map", "Map".to_owned())
            }
            Route::Shelf(id) => {
                let name = id.replace('-', " ");
                if let Some(b) = self.browse.borrow().as_ref() {
                    b.show_shelf(id, &name);
                }
                ("shelf", "library", "Library".to_owned())
            }
            Route::Manage => {
                if let Some(m) = self.manage.borrow().as_ref() {
                    m.refresh();
                }
                ("manage", "about", "Library and downloads".to_owned())
            }
            Route::Section(id) => {
                match *id {
                    "videos" => {
                        if let Some(v) = self.videos.borrow().as_ref() {
                            v.show_list();
                        }
                    }
                    "library" | "saved" | "about" => {
                        if let Some(b) = self.browse.borrow().as_ref() {
                            match *id {
                                "library" => b.show_library(),
                                "saved" => b.show_saved(),
                                _ => b.show_about(),
                            }
                        }
                    }
                    "map" => {
                        if let Some(m) = self.map.borrow().as_ref() {
                            m.show();
                        }
                    }
                    _ => {}
                }
                (*id, *id, SECTIONS.iter().find(|s| s.0 == *id).map(|s| s.1).unwrap_or("").to_owned())
            }
        };
        self.stack.set_visible_child_name(page);
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

    /// Replaces the current history entry, and shows it if asked (a document that turned out to be a web page; a new
    /// chat's id and a turned page change the entry only).
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
    /// A click on a row means "go there", and the screen takes the keyboard (Chat's composer, the search field).
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
        self.window.set_title(Some(&format!("{title} — Lastbook")));
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
        assert_eq!(Route::from_key("nonsense"), None);
    }
}
