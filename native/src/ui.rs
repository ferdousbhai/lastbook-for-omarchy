// SPDX-License-Identifier: MIT
//! The window: a header bar (sidebar toggle, back and forward, the screen's title, save), the sidebar of screens, and a
//! stack of pages. Navigation keeps a history like a browser's, so back and forward work across screens.

use std::{
    cell::{Cell, RefCell},
    rc::Rc,
    sync::Arc,
};

use gtk::{gdk, glib, prelude::*};

use crate::{
    chat::ChatPage,
    engine::Engine,
    guides::Guides,
    library::{Browse, Saved, load_saved, store_saved},
    map::MapPage,
    reader::Reader,
    search::SearchPage,
    theme::Theme,
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
    /// A short text form for what can be saved (and for LASTBOOK_OPEN): read:<doc>:<page>, wiki:<path>,
    /// watch:<video>:<t>, guide:<id>, web:<path>.
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

/// The sidebar's screens: (id, label, icon).
pub const SECTIONS: &[(&str, &str, &str)] = &[
    ("guides", "Guides", "help-about-symbolic"),
    ("search", "Search", "system-search-symbolic"),
    ("chat", "Chat", "user-available-symbolic"),
    ("videos", "Videos", "video-x-generic-symbolic"),
    ("map", "Map", "mark-location-symbolic"),
    ("library", "Library", "x-office-document-symbolic"),
    ("encyclopedias", "Encyclopedias", "accessories-dictionary-symbolic"),
    ("saved", "Saved", "starred-symbolic"),
    ("about", "About", "dialog-information-symbolic"),
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

fn header_button(icon: &str, tip: &str) -> gtk::Button {
    let b = gtk::Button::from_icon_name(icon);
    b.add_css_class("header-action");
    b.set_tooltip_text(Some(tip));
    b
}

impl App {
    pub fn build(app: &gtk::Application, engine: Arc<Engine>, theme: Rc<Theme>) -> Rc<Self> {
        let window = gtk::ApplicationWindow::builder().application(app).title("Lastbook").default_width(1280).default_height(820).build();

        let header = gtk::HeaderBar::new();
        header.set_show_title_buttons(false);   // Hyprland tiles the window and Super+W closes it
        let toggle = header_button("sidebar-show-symbolic", "Show or hide the sidebar (Ctrl+B)");
        toggle.add_css_class("sidebar-toggle");
        let back = header_button("go-previous-symbolic", "Back (Alt+Left)");
        let forward = header_button("go-next-symbolic", "Forward (Alt+Right)");
        let star = header_button("non-starred-symbolic", "Save this (Ctrl+D)");
        header.pack_start(&toggle);
        header.pack_start(&back);
        header.pack_start(&forward);
        header.pack_end(&star);
        let title = gtk::Label::new(Some("Guides"));
        title.add_css_class("header-title");
        title.set_ellipsize(gtk::pango::EllipsizeMode::End);
        header.set_title_widget(Some(&title));
        window.set_titlebar(Some(&header));

        let sidebar = gtk::ListBox::new();
        sidebar.add_css_class("sidebar");
        sidebar.set_selection_mode(gtk::SelectionMode::Single);
        for (id, label, icon) in SECTIONS {
            let row = gtk::ListBoxRow::new();
            row.add_css_class("sidebar-row");
            row.set_widget_name(id);
            let b = gtk::Box::new(gtk::Orientation::Horizontal, 10);
            b.append(&gtk::Image::from_icon_name(icon));
            b.append(&gtk::Label::new(Some(label)));
            row.set_child(Some(&b));
            sidebar.append(&row);
        }
        let footer = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        footer.add_css_class("sidebar-footer");
        footer.append(&gtk::Image::from_icon_name("network-offline-symbolic"));
        footer.append(&gtk::Label::new(Some("Works offline")));
        let sidebar_shell = gtk::Box::new(gtk::Orientation::Vertical, 0);
        sidebar_shell.add_css_class("sidebar-shell");
        sidebar_shell.set_size_request(200, -1);
        let scroll = gtk::ScrolledWindow::builder().hscrollbar_policy(gtk::PolicyType::Never).vexpand(true).child(&sidebar).build();
        sidebar_shell.append(&scroll);
        sidebar_shell.append(&footer);

        let stack = gtk::Stack::new();
        stack.set_hexpand(true);
        stack.set_transition_type(gtk::StackTransitionType::Crossfade);
        stack.set_transition_duration(120);
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
        let w = Rc::downgrade(&web);
        app.theme.on_change(move |t| {
            if let Some(w) = w.upgrade() {
                w.set_tokens(t);
            }
        });
        web.set_tokens(&app.theme.tokens.borrow());
        app.web.replace(Some(web));
        let browse = Browse::build(&app);
        app.stack.add_named(&browse.library_root, Some("library"));
        app.stack.add_named(&browse.shelf_root, Some("shelf"));
        app.stack.add_named(&browse.enc_root, Some("encyclopedias"));
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
        app.keys();
        app.navigate(Route::Section("guides"));
        // LASTBOOK_OPEN opens a screen at start, for scripted checks: any saved form (read:, wiki:, watch:, guide:, web:,
        // search:, shelf:, section:) or chat:<question>.
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
            match key {
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
                _ => "Saved page".into(),
            };
            items.push(Saved { key, title, sub, at: glib::real_time() / 1_000_000 });
        }
        store_saved(&items);
        self.update_star();
    }

    fn update_star(&self) {
        let key = self.current().and_then(|r| r.key());
        self.star.set_visible(key.is_some());
        let saved = key.is_some_and(|k| load_saved().iter().any(|s| s.key == k));
        self.star.set_icon_name(if saved { "starred-symbolic" } else { "non-starred-symbolic" });
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
        self.show(&route);
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
                    v.play(video, *t);
                }
                ("watch", "videos", String::new())
            }
            Route::Wiki { path } => {
                let name = path.split('/').next().unwrap_or_default();
                let enc = if name.starts_with("mdwiki") { "Medical encyclopedia (MDWiki)" } else if name.starts_with("appropedia") { "Appropedia" } else { name };
                if let Some(w) = self.web.borrow().as_ref() {
                    w.open(&format!("/zim/{path}"), &format!("{enc} · community-written: check safety-critical steps against a manual"), true);
                }
                let article = path.rsplit('/').next().unwrap_or_default().replace('_', " ");
                ("web", "encyclopedias", urlencoding::decode(&article).map(|s| s.into_owned()).unwrap_or(article))
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
        if std::env::var_os("LASTBOOK_DEBUG").is_some() {
            eprintln!("route {route:?} -> page {page} visible {:?}", self.stack.visible_child_name());
        }
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

    /// Replaces the current history entry and shows it (a document that turned out to be a web page, a new chat's id).
    pub fn replace_route(self: &Rc<Self>, route: Route, show: bool) {
        let pos = self.pos.get();
        if let Some(r) = self.history.borrow_mut().get_mut(pos) {
            *r = route.clone();
        }
        if show {
            self.show(&route);
        }
    }

    pub fn set_title(&self, title: &str) {
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
