// SPDX-License-Identifier: MIT
//! Pages that are HTML by nature: encyclopedia articles (ZIM) and the library's saved web pages, in WebKitGTK 6.
//!
//! Everything a page loads comes through the `lastbook:` scheme, which this module answers from the engine (its
//! file handler and the checks on what may be served). Nothing else loads: JavaScript is off, a content
//! filter blocks every URL that is not lastbook:, and navigation never leaves it. The session is ephemeral, so no
//! cookies, cache or storage are written. Encyclopedia articles are recoloured to the Omarchy theme; a saved web page
//! is shown as a browser would show it (`page_css`).
//!
//! Find in the page (`find`) is WebKit's own, which marks every match and scrolls to each in turn; text selected in
//! the page copies with Ctrl+C, as WebKit's keys have it. The bar lists other documents on the same subject
//! (reader::Related).

use std::{
    cell::{Cell, RefCell},
    rc::{Rc, Weak},
    sync::Arc,
};

use gtk::{gio, glib, prelude::*};
use omui::{Theme, Tokens};
use serde_json::Value;
use webkit6::prelude::*;

use crate::{
    engine::Engine,
    reader::{FindBar, Related},
    ui::{App, Route},
};

pub const ORIGIN: &str = "lastbook://app";

/// The page an article's pictures are drawn on: white, as they were made for (`page_css`).
const PAPER: &str = "#ffffff";

/// Allows lastbook: and blocks everything else (WebKit content blockers have no negative lookahead: block all, then
/// let lastbook: through).
const NO_NETWORK: &str = r#"[{"trigger":{"url-filter":".*"},"action":{"type":"block"}},
{"trigger":{"url-filter":"^lastbook:"},"action":{"type":"ignore-previous-rules"}}]"#;

pub struct WebPage {
    pub root: gtk::Widget,
    app: Weak<App>,
    view: webkit6::WebView,
    meta: gtk::Label,
    /// Why a link that was clicked went nowhere; gone with the next page.
    note: gtk::Label,
    manager: webkit6::UserContentManager,
    theme: Rc<Theme>,
    themed: RefCell<bool>,
    /// The filter that blocks the network is in place; until then a page asked for waits in `waiting`.
    filtered: Rc<Cell<bool>>,
    waiting: Rc<RefCell<Option<String>>>,
    find: FindBar,
    /// Which match WebKit shows (from 0), and the way the last step went: 0 for a new search, which starts at the first.
    at: Cell<usize>,
    stepped: Cell<i32>,
    related: Rc<Related>,
}

/// How many matches WebKit counts at most.
const MAX_MATCHES: u32 = 1000;

/// The folders of the library a page's own requests may name (the engine's SERVED_PREFIXES without /api/), and the one
/// file beside them.
const PAGE_PREFIXES: &[&str] =
    &["/zim/", "/documents/", "/pages/", "/videos/", "/videos-external/", "/transcripts/", "/metadata/videos/", "/courses/", "/collections/", "/assets/"];
const PAGE_FILES: &[&str] = &["/reader.css"];

/// The path of `target` (a path with its query and fragment) as the engine reads it: percent escapes decoded once,
/// whatever bytes they make.
fn decoded_path(target: &str) -> Vec<u8> {
    let path = target.split(['?', '#']).next().unwrap_or_default();
    urlencoding::decode_binary(path.as_bytes()).into_owned()
}

/// Whether a page may load `target`. A page is given the library's files, never the engine's own routes: those are
/// the window's to call, and one of them (setup's state) goes online. So this names what is allowed, by the path the
/// engine will see, and refuses the rest: an escaped `/%61pi/` is `/api/` to the engine, and no escape elsewhere in
/// the address (a query of bytes that are not UTF-8) makes it anything else.
fn for_pages(target: &str) -> bool {
    let path = decoded_path(target);
    PAGE_FILES.iter().any(|f| path == f.as_bytes()) || PAGE_PREFIXES.iter().any(|p| path.starts_with(p.as_bytes()))
}

/// Where a clicked link to another address of the library leads.
#[derive(Debug, PartialEq)]
enum Linked {
    Open(Route),
    /// A file the web view cannot show and no reader of the window's can be given: why, in English, translated where
    /// it is shown.
    Not(&'static str),
}

const VIDEO_SUFFIXES: &[&str] = &[".mp4", ".m4v", ".webm", ".mkv", ".mov"];

/// The route for a link to `target` (the address after the origin). An encyclopedia article and a web page open
/// here. A PDF or a video would be a blank page in a web view with no JavaScript and no network, so it opens in its
/// own reader, found by its path in the catalog (`catalog` reads it, and is asked only for those).
fn linked(target: &str, catalog: impl FnOnce() -> Option<Value>) -> Linked {
    if let Some(article) = target.strip_prefix("/zim/") {
        return Linked::Open(Route::Wiki { path: article.to_owned() });
    }
    let file = String::from_utf8_lossy(&decoded_path(target)).trim_start_matches('/').to_owned();
    let lower = file.to_lowercase();
    let (pdf, video) = (lower.ends_with(".pdf"), VIDEO_SUFFIXES.iter().any(|s| lower.ends_with(s)));
    if !pdf && !video {
        return Linked::Open(Route::Web { path: target.to_owned(), meta: String::new() });   // another file: not this one's source
    }
    let catalog = catalog();
    let record = catalog.as_ref().and_then(Value::as_array).and_then(|records| records.iter().find(|r| r["path"].as_str() == Some(file.as_str())));
    match record.and_then(|r| Some((r["id"].as_str()?, r["kind"].as_str()?))) {
        Some((id, "pdf")) => {
            // A link may name its page, as browsers read it: file.pdf#page=12.
            let page = target.split_once('#').and_then(|(_, f)| f.split('&').find_map(|p| p.strip_prefix("page="))).and_then(|n| n.parse().ok());
            Linked::Open(Route::Read { doc: id.to_owned(), page: page.unwrap_or(1) })
        }
        Some((id, "video")) => Linked::Open(Route::Watch { video: id.to_owned(), t: -1.0 }),
        _ if pdf => Linked::Not(n_!("That link is to a PDF this library does not list, so it cannot be opened here.")),
        _ => Linked::Not(n_!("That link is to a video this library does not list, so it cannot be played here.")),
    }
}

/// The user style sheet for a page, if it gets one. An encyclopedia article (`themed`) is recoloured to the theme.
/// A saved web page gets none: it was written for a browser's white page and dark text, and is readable only as
/// that. Its own background, if it sets one, fills the view as in a browser; a theme background put under it would
/// leave its dark text on the dark themes' near-black.
fn page_css(themed: bool, t: &Tokens) -> Option<String> {
    themed.then(|| {
        format!(
            "html, body, .mw-page-container, .mw-body, #content, .mw-content-container {{ background: {bg} !important; color: {text} !important; }}\n\
             body {{ max-width: 52em; margin: 0 auto !important; padding: 1.5em 2em !important; line-height: 1.55; }}\n\
             a, a:visited {{ color: {accent} !important; }}\n\
             table, th, td, .infobox, .navbox, .wikitable, .thumbinner, figure, .toc {{ background: {surface} !important; color: {text} !important; border-color: {border} !important; }}\n\
             h1, h2, h3, h4 {{ color: {text} !important; border-color: {border} !important; font-family: 'JetBrains Mono', monospace; }}\n\
             .mw-header, .vector-header-container, .mw-footer, .zim-footer, .mw-editsection, .vector-page-toolbar, #siteSub {{ display: none !important; }}\n\
             img {{ background: {paper}; }}\n\
             ::selection {{ background: {accent}55; }}\n",
            bg = t.background,
            text = t.text,
            accent = t.accent,
            surface = t.surface,
            border = t.border,
            // An article's diagrams are drawn for a white page, often in black lines on nothing: they keep a white
            // page under them in every theme, as the reader's paper does, or they vanish on a dark one.
            paper = PAPER,
        )
    })
}

fn register_scheme(engine: Arc<Engine>) {
    let ctx = webkit6::WebContext::default().expect("WebKit's default context");
    ctx.register_uri_scheme("lastbook", move |req| {
        let uri = req.uri().map(|u| u.to_string()).unwrap_or_default();
        let path = uri.strip_prefix(ORIGIN).unwrap_or("/").to_owned();
        if !for_pages(&path) {
            let mut e = glib::Error::new(gio::IOErrorEnum::PermissionDenied, "not for pages");
            return req.finish_error(&mut e);
        }
        let engine = engine.clone();
        let req = req.clone();
        glib::spawn_future_local(async move {
            let (status, ctype, body) = engine.fetch(&path).await;
            if status == 200 || status == 206 {
                let len = body.len() as i64;
                let stream = gio::MemoryInputStream::from_bytes(&glib::Bytes::from_owned(body));
                let ctype = ctype.split(';').next().unwrap_or("").trim().to_owned();
                req.finish(&stream, len, (!ctype.is_empty()).then_some(ctype.as_str()));
            } else {
                let mut e = glib::Error::new(gio::IOErrorEnum::NotFound, &format!("{path}: {status}"));
                req.finish_error(&mut e);
            }
        });
    });
    // lastbook: pages may load one another (an article's images, links between articles), and are no secure context.
    if let Some(sec) = ctx.security_manager() {
        sec.register_uri_scheme_as_local("lastbook");
        sec.register_uri_scheme_as_cors_enabled("lastbook");
    }
}

impl WebPage {
    pub fn build(app: &Rc<App>) -> Rc<Self> {
        register_scheme(app.engine.clone());
        let session = webkit6::NetworkSession::new_ephemeral();
        let manager = webkit6::UserContentManager::new();
        let view = webkit6::WebView::builder().network_session(&session).user_content_manager(&manager).build();
        if let Some(s) = webkit6::prelude::WebViewExt::settings(&view) {
            s.set_enable_javascript(false);
            s.set_enable_javascript_markup(false);
            s.set_enable_developer_extras(false);
            s.set_enable_back_forward_navigation_gestures(false);
            s.set_default_font_family("sans-serif");
        }
        view.set_vexpand(true);
        view.set_hexpand(true);
        let store_dir = glib::user_cache_dir().join("Lastbook").join("content-filters");
        let store = webkit6::UserContentFilterStore::new(&store_dir.to_string_lossy());
        // No page loads before the filter that blocks the network is in place, and none at all if it cannot be.
        let (filtered, waiting) = (Rc::new(Cell::new(false)), Rc::new(RefCell::new(None::<String>)));
        let blocked = gtk::Label::new(None);
        omui::i18n::bind_label(&blocked, "Pages cannot be shown: Lastbook could not block them from reaching the internet.");
        blocked.add_css_class("error");
        blocked.set_wrap(true);
        blocked.set_visible(false);
        let (m, f2, w2, v2, b2) = (manager.clone(), filtered.clone(), waiting.clone(), view.clone(), blocked.clone());
        store.save("lastbook-no-network", &glib::Bytes::from_static(NO_NETWORK.as_bytes()), gio::Cancellable::NONE, move |f| match f {
            Ok(filter) => {
                m.add_filter(&filter);
                f2.set(true);
                if let Some(uri) = w2.take() {
                    v2.load_uri(&uri);
                }
            }
            Err(e) => {
                eprintln!("content filter: {e}");
                b2.set_visible(true);
            }
        });

        let bar = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        bar.add_css_class("reader-bar");
        let meta = gtk::Label::new(None);
        meta.add_css_class("meta");
        meta.set_xalign(0.0);
        meta.set_hexpand(true);
        meta.set_ellipsize(gtk::pango::EllipsizeMode::End);
        let related = Related::new(app);
        let find_button = omui::icon_button(omui::icons::name::SEARCH, "");
        omui::i18n::bind_tooltip(&find_button, "Find in this page (Ctrl+F)");
        bar.append(&meta);
        bar.append(&related.button);
        bar.append(&find_button);
        let find = FindBar::new(n_!("Find in this page"));
        let root = gtk::Box::new(gtk::Orientation::Vertical, 0);
        let note = gtk::Label::new(None);
        note.add_css_class("status-line");
        note.set_wrap(true);
        note.set_visible(false);
        root.append(&bar);
        root.append(&find.bar);
        root.append(&blocked);
        root.append(&note);
        root.append(&view);
        let page = Rc::new(Self {
            root: root.upcast(),
            app: Rc::downgrade(app),
            view,
            meta,
            note,
            manager,
            theme: app.theme.clone(),
            themed: RefCell::new(false),
            filtered,
            waiting,
            find,
            at: Cell::new(0),
            stepped: Cell::new(0),
            related,
        });
        let p = Rc::downgrade(&page);
        find_button.connect_clicked(move |_| if let Some(p) = p.upgrade() { p.find(); });
        let p = Rc::downgrade(&page);
        page.find.entry.connect_search_changed(move |_| if let Some(p) = p.upgrade() { p.look() });
        let p = Rc::downgrade(&page);
        page.find.on_step(move |way| {
            if let Some(p) = p.upgrade()
                && let Some(fc) = p.view.find_controller()
                && fc.search_text().is_some()
            {
                p.stepped.set(way);
                if way < 0 { fc.search_previous() } else { fc.search_next() }
            }
        });
        if let Some(fc) = page.view.find_controller() {
            let p = Rc::downgrade(&page);
            fc.connect_found_text(move |_, count| {
                if let Some(p) = p.upgrade() {
                    let n = count.min(MAX_MATCHES) as usize;
                    let at = match p.stepped.replace(0) {
                        0 => 0,
                        way => (p.at.get() as i64 + i64::from(way)).rem_euclid(n.max(1) as i64) as usize,
                    };
                    p.at.set(at);
                    p.find.say(at, n);
                }
            });
            let p = Rc::downgrade(&page);
            fc.connect_failed_to_find_text(move |_| {
                if let Some(p) = p.upgrade() {
                    p.stepped.set(0);
                    p.find.say(0, 0);
                }
            });
        }
        // Closed (Escape, its button): WebKit's marks go, and the keyboard is the page's again.
        let p = Rc::downgrade(&page);
        page.find.bar.connect_search_mode_enabled_notify(move |bar| {
            if let Some(p) = p.upgrade()
                && !bar.is_search_mode()
            {
                if let Some(fc) = p.view.find_controller() {
                    fc.search_finish();
                }
                p.view.grab_focus();
            }
        });
        // The page on screen follows a theme switch.
        let p = Rc::downgrade(&page);
        page.theme.on_change(&page.view, move |_| {
            if let Some(p) = p.upgrade() {
                p.apply_style(*p.themed.borrow());
            }
        });
        // Navigation stays on lastbook:; a link to the web goes nowhere (this is an offline library). A link to another
        // page of the library is a step in the window's own history, so the title, the star and Back know of it.
        let p = Rc::downgrade(&page);
        page.view.connect_decide_policy(move |v, decision, kind| {
            if kind != webkit6::PolicyDecisionType::NavigationAction {
                return false;
            }
            let Some(action) = decision.downcast_ref::<webkit6::NavigationPolicyDecision>().and_then(|n| n.navigation_action()) else { return false };
            let Some(uri) = action.request().and_then(|r| r.uri()) else { return false };
            let Some(path) = uri.strip_prefix(ORIGIN) else {
                decision.ignore();
                return true;
            };
            let page_of = |u: &str| u.split('#').next().unwrap_or(u).to_owned();
            if action.navigation_type() == webkit6::NavigationType::LinkClicked
                && v.uri().is_some_and(|now| page_of(&now) != page_of(&uri))   // not a jump within the page
                && let Some(p) = p.upgrade()
                && let Some(app) = p.app.upgrade()
            {
                decision.ignore();
                let library = app.engine.ready.library.clone();
                match linked(path, || serde_json::from_slice(&std::fs::read(library.join("catalog.json")).ok()?).ok()) {
                    Linked::Open(route) => {
                        glib::idle_add_local_once(move || app.navigate(route));
                    }
                    Linked::Not(why) => {
                        p.note.set_text(&omui::i18n::translate(why));
                        p.note.set_visible(true);
                    }
                }
                return true;
            }
            false
        });
        let p = Rc::downgrade(&page);
        page.view.connect_title_notify(move |v| {
            if let Some(p) = p.upgrade()
                && let Some(t) = v.title().filter(|t| !t.is_empty())
                && let Some(app) = p.app.upgrade()
            {
                app.report_title("web", &t);
            }
        });
        page
    }

    /// Opens the find bar, with the keyboard in it (ui.rs: Ctrl+F on this screen).
    pub fn find(&self) -> bool {
        self.find.open();
        self.look();
        true
    }

    /// Finds what the field holds in the page, from its top; an empty field takes WebKit's marks away.
    fn look(&self) {
        let Some(fc) = self.view.find_controller() else { return };
        let q = self.find.entry.text();
        self.stepped.set(0);
        if q.trim().is_empty() {
            fc.search_finish();
            self.find.say(0, 0);
        } else {
            let how = webkit6::FindOptions::CASE_INSENSITIVE | webkit6::FindOptions::WRAP_AROUND;
            fc.search(q.trim(), how.bits(), MAX_MATCHES);
        }
    }

    fn apply_style(&self, themed: bool) {
        self.manager.remove_all_style_sheets();
        if let Some(css) = page_css(themed, &self.theme.tokens()) {
            let sheet = webkit6::UserStyleSheet::new(&css, webkit6::UserContentInjectedFrames::AllFrames, webkit6::UserStyleLevel::User, &[], &[]);
            self.manager.add_style_sheet(&sheet);
        }
    }

    /// Loads `path` (served by the engine, e.g. /zim/<file>/<article> or /pages/...). `themed`: recolour it to the theme
    /// (encyclopedia articles); a saved web page keeps its own look, on a browser's white page.
    pub fn open(&self, path: &str, meta: &str, themed: bool) {
        self.themed.replace(themed);
        self.apply_style(themed);
        self.meta.set_text(meta);
        self.note.set_visible(false);
        self.find.bar.set_search_mode(false);
        self.related.show_for("path", path);
        let uri = format!("{ORIGIN}{}", if path.starts_with('/') { path.to_owned() } else { format!("/{path}") });
        if !self.filtered.get() {
            self.waiting.replace(Some(uri));   // loaded once the filter is in place
        } else if self.view.uri().as_deref() != Some(uri.as_str()) {
            self.view.load_uri(&uri);
        }
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn pages_are_given_library_files() {
        for ok in [
            "/zim/mdwiki.zim/Water_purification",
            "/pages/rope.html",
            "/pages/rope.html?pdf-page=2#top",
            "/documents/safe%20water.pdf",
            "/reader.css",
            "/assets/a.png",
            "/metadata/videos/x.jpg",
        ] {
            assert!(for_pages(ok), "{ok}");
        }
    }

    #[test]
    fn pages_never_reach_the_engines_routes() {
        for no in [
            "/api/setup/state",
            "/api/",
            "/%61pi/setup/state",
            "/%61pi/setup/state?%ff",   // an escape that is not UTF-8 no longer hides the path
            "/%61pi/setup/state?x=%E9",
            "/%61%70%69/find?q=a",
            "/api%2Fsetup/state",
            "/%2Fapi/setup/state",
            "/%ffapi/setup/state",
            "/API/setup/state",
            "/",
            "",
            "/reader.css/../api/setup/state",
            "/search.sqlite",
            "/catalog.json",
            "//api/setup/state",
            "api/setup/state",
            "/%2561pi/setup/state",   // escaped twice: the engine decodes once, and serves no such folder
        ] {
            assert!(!for_pages(no), "{no}");
        }
    }

    fn catalog() -> Option<Value> {
        Some(json!([
            {"id": "nwss", "kind": "pdf", "path": "documents/field survival/nwss.pdf"},
            {"id": "video-1", "kind": "video", "path": "videos/water/boil [abc].mp4"},
            {"id": "rope", "kind": "html", "path": "pages/rope.html"},
        ]))
    }

    #[test]
    fn a_link_to_a_pdf_opens_the_reader() {
        assert_eq!(linked("/documents/field%20survival/nwss.pdf", catalog), Linked::Open(Route::Read { doc: "nwss".into(), page: 1 }));
        assert_eq!(linked("/documents/field%20survival/nwss.pdf#page=12", catalog), Linked::Open(Route::Read { doc: "nwss".into(), page: 12 }));
        assert_eq!(linked("/videos/water/boil%20%5Babc%5D.mp4", catalog), Linked::Open(Route::Watch { video: "video-1".into(), t: -1.0 }));
        assert!(matches!(linked("/documents/unlisted.PDF", catalog), Linked::Not(_)));
        assert!(matches!(linked("/documents/unlisted.pdf", || None), Linked::Not(_)));
    }

    #[test]
    fn other_links_open_as_pages_without_reading_the_catalog() {
        let unread = || -> Option<Value> { panic!("the catalog is read for a PDF or a video alone") };
        assert_eq!(linked("/pages/rope.html#knots", unread), Linked::Open(Route::Web { path: "/pages/rope.html#knots".into(), meta: String::new() }));
        assert_eq!(linked("/zim/mdwiki.zim/Rope", unread), Linked::Open(Route::Wiki { path: "mdwiki.zim/Rope".into() }));
    }

    fn tokens(background: &str, text: &str, light: bool) -> Tokens {
        Tokens {
            name: "test".into(),
            background: background.into(),
            surface: "#222222".into(),
            text: text.into(),
            accent: "#7aa2f7".into(),
            danger: "#f7768e".into(),
            success: "#9ece6a".into(),
            warning: "#e0af68".into(),
            muted: "#414868".into(),
            highlight: "#292e42".into(),
            border: "#414868".into(),
            dim_text: "#888888".into(),
            light,
        }
    }

    #[test]
    fn a_saved_page_is_never_put_on_the_theme_background() {
        // Dark or light, a saved page gets no rule of the window's: its dark text stays on a browser's white page.
        assert_eq!(page_css(false, &tokens("#1a1b26", "#a9b1d6", false)), None);
        assert_eq!(page_css(false, &tokens("#eff1f5", "#4c4f69", true)), None);
    }

    #[test]
    fn an_article_takes_background_and_text_together() {
        let css = page_css(true, &tokens("#1a1b26", "#a9b1d6", false)).unwrap();
        assert!(css.contains("background: #1a1b26 !important; color: #a9b1d6 !important;"));
    }
}
