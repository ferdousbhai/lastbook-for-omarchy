// SPDX-License-Identifier: MIT
//! The window: a header bar (sidebar toggle, back and forward, the screen's title, search), the sidebar of screens,
//! and a stack of pages. Navigation keeps a history like a browser's, so back and forward work across screens.

use std::{
    cell::{Cell, RefCell},
    rc::Rc,
    sync::Arc,
};

use gtk::{gdk, glib, prelude::*};

use crate::{chat::ChatPage, engine::Engine, guides::Guides, reader::Reader, search::SearchPage};

#[derive(Clone, Debug, PartialEq)]
pub enum Route {
    Search { q: String, kind: String },
    Read { doc: String, page: i32 },
    Chat { id: Option<String>, about: Option<String> },
    Guide(String),
    Watch { video: String, t: f64 },
    Wiki { path: String },
    MapAt { lat: f64, lon: f64, label: String },
    Section(&'static str),
}

/// The sidebar's screens: (id, label, icon). Screens not yet native say so and stay reachable in the old window.
pub const SECTIONS: &[(&str, &str, &str)] = &[
    ("guides", "Guides", "help-about-symbolic"),
    ("search", "Search", "system-search-symbolic"),
    ("chat", "Chat", "user-available-symbolic"),
    ("videos", "Videos", "video-x-generic-symbolic"),
    ("map", "Map", "mark-location-symbolic"),
    ("library", "Library", "x-office-document-symbolic"),
    ("encyclopedias", "Encyclopedias", "accessories-dictionary-symbolic"),
    ("saved", "Saved", "starred-symbolic"),
];

pub struct App {
    pub engine: Arc<Engine>,
    pub window: gtk::ApplicationWindow,
    stack: gtk::Stack,
    sidebar: gtk::ListBox,
    sidebar_shell: gtk::Box,
    title: gtk::Label,
    back: gtk::Button,
    forward: gtk::Button,
    history: RefCell<Vec<Route>>,
    pos: Cell<usize>,
    syncing: Cell<bool>,
    /// The sidebar section of the screen shown, so a row re-selected by focus (not by the person) does nothing.
    section: RefCell<String>,
    pub search: RefCell<Option<Rc<SearchPage>>>,
    pub reader: RefCell<Option<Rc<Reader>>>,
    pub chat: RefCell<Option<Rc<ChatPage>>>,
    pub guides: RefCell<Option<Rc<Guides>>>,
}

fn header_button(icon: &str, tip: &str) -> gtk::Button {
    let b = gtk::Button::from_icon_name(icon);
    b.add_css_class("header-action");
    b.set_tooltip_text(Some(tip));
    b
}

impl App {
    pub fn build(app: &gtk::Application, engine: Arc<Engine>) -> Rc<Self> {
        let window = gtk::ApplicationWindow::builder().application(app).title("Lastbook").default_width(1280).default_height(820).build();

        let header = gtk::HeaderBar::new();
        header.set_show_title_buttons(false);   // Hyprland tiles the window and Super+W closes it
        let toggle = header_button("sidebar-show-symbolic", "Show or hide the sidebar (Ctrl+B)");
        toggle.add_css_class("sidebar-toggle");
        let back = header_button("go-previous-symbolic", "Back (Alt+Left)");
        let forward = header_button("go-next-symbolic", "Forward (Alt+Right)");
        header.pack_start(&toggle);
        header.pack_start(&back);
        header.pack_start(&forward);
        let title = gtk::Label::new(Some("Search"));
        title.add_css_class("header-title");
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
            window,
            stack,
            sidebar,
            sidebar_shell,
            title,
            back,
            forward,
            history: RefCell::new(Vec::new()),
            pos: Cell::new(0),
            syncing: Cell::new(false),
            section: RefCell::new(String::new()),
            search: RefCell::new(None),
            reader: RefCell::new(None),
            chat: RefCell::new(None),
            guides: RefCell::new(None),
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
        for (id, label, icon) in SECTIONS {
            if app.stack.child_by_name(id).is_none() {
                app.stack.add_named(&placeholder(label, icon), Some(id));
            }
        }

        let a = app.clone();
        toggle.connect_clicked(move |_| a.toggle_sidebar());
        let a = app.clone();
        app.back.connect_clicked(move |_| a.go(-1));
        let a = app.clone();
        app.forward.connect_clicked(move |_| a.go(1));
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
                    let id: &'static str = SECTIONS.iter().find(|s| s.0 == id.as_str()).map(|s| s.0).unwrap_or("search");
                    Route::Section(id)
                };
                a.navigate(route);
            }
        });
        app.keys();
        app.navigate(Route::Section("guides"));
        // LASTBOOK_OPEN opens a screen at start (scripted checks): "search:<words>" or "read:<document id>:<page>".
        if let Ok(open) = std::env::var("LASTBOOK_OPEN") {
            match open.split_once(':') {
                Some(("search", q)) => app.navigate(Route::Search { q: q.to_owned(), kind: "any".into() }),
                Some(("chat", q)) => {
                    app.navigate(Route::Chat { id: None, about: None });
                    if let Some(c) = app.chat.borrow().as_ref() {
                        c.ask_text(q);
                    }
                }
                Some(("guide", id)) => app.navigate(Route::Guide(id.to_owned())),
                Some(("read", rest)) => {
                    let (doc, page) = rest.rsplit_once(':').map(|(d, p)| (d, p.parse().unwrap_or(1))).unwrap_or((rest, 1));
                    app.navigate(Route::Read { doc: doc.to_owned(), page });
                }
                _ => {}
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
                ("reader", "search", String::new())
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
            Route::Watch { .. } => ("videos", "videos", "Videos".to_owned()),
            Route::Wiki { .. } => ("encyclopedias", "encyclopedias", "Encyclopedias".to_owned()),
            Route::MapAt { .. } => ("map", "map", "Map".to_owned()),
            Route::Section(id) => (*id, *id, SECTIONS.iter().find(|s| s.0 == *id).map(|s| s.1).unwrap_or("").to_owned()),
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
    }

    /// Replaces the current history entry without showing anything (a new chat getting its id).
    pub fn replace_route(&self, route: Route) {
        let pos = self.pos.get();
        if let Some(r) = self.history.borrow_mut().get_mut(pos) {
            *r = route;
        }
    }

    pub fn set_title(&self, title: &str) {
        self.title.set_text(title);
        self.window.set_title(Some(&format!("{title} — Lastbook")));
    }
}

fn placeholder(label: &str, icon: &str) -> gtk::Widget {
    let b = gtk::Box::new(gtk::Orientation::Vertical, 12);
    b.set_valign(gtk::Align::Center);
    b.set_halign(gtk::Align::Center);
    let img = gtk::Image::from_icon_name(icon);
    img.set_pixel_size(48);
    img.add_css_class("dim");
    let t = gtk::Label::new(Some(label));
    t.add_css_class("page-title");
    let note = gtk::Label::new(Some("This screen is still being rebuilt as part of the native app."));
    note.add_css_class("empty-state");
    b.append(&img);
    b.append(&t);
    b.append(&note);
    b.upcast()
}
