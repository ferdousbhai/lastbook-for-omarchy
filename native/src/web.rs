// SPDX-License-Identifier: MIT
//! Pages that are HTML by nature: encyclopedia articles (ZIM) and the library's saved web pages, in WebKitGTK 6.
//!
//! Everything a page loads comes through the `lastbook:` scheme, which this module answers from the engine (its
//! file handler and the checks on what may be served). Nothing else loads: JavaScript is off, a content
//! filter blocks every URL that is not lastbook:, and navigation never leaves it. The session is ephemeral, so no
//! cookies, cache or storage are written. Encyclopedia articles are recoloured to the Omarchy theme.

use std::{
    cell::RefCell,
    rc::{Rc, Weak},
    sync::Arc,
};

use gtk::{gio, glib, prelude::*};
use omui::Theme;
use webkit6::prelude::*;

use crate::{engine::Engine, ui::App};

pub const ORIGIN: &str = "lastbook://app";

/// Allows lastbook: and blocks everything else (WebKit content blockers have no negative lookahead: block all, then
/// let lastbook: through).
const NO_NETWORK: &str = r#"[{"trigger":{"url-filter":".*"},"action":{"type":"block"}},
{"trigger":{"url-filter":"^lastbook:"},"action":{"type":"ignore-previous-rules"}}]"#;

pub struct WebPage {
    pub root: gtk::Widget,
    app: Weak<App>,
    view: webkit6::WebView,
    meta: gtk::Label,
    manager: webkit6::UserContentManager,
    theme: Rc<Theme>,
    themed: RefCell<bool>,
}

fn register_scheme(engine: Arc<Engine>) {
    let ctx = webkit6::WebContext::default().expect("WebKit's default context");
    ctx.register_uri_scheme("lastbook", move |req| {
        let uri = req.uri().map(|u| u.to_string()).unwrap_or_default();
        let path = uri.strip_prefix(ORIGIN).unwrap_or("/").to_owned();
        // A page is given the library's files, never the engine's own routes: those are the window's to call, and one of
        // them (setup's state) goes online.
        if path.starts_with("/api/") || urlencoding::decode(&path).is_ok_and(|p| p.starts_with("/api/")) {
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
        // Navigation stays on lastbook:; a link to the web goes nowhere (this is an offline library).
        view.connect_decide_policy(|_, decision, kind| {
            if kind == webkit6::PolicyDecisionType::NavigationAction
                && let Some(nav) = decision.downcast_ref::<webkit6::NavigationPolicyDecision>()
                && let Some(action) = nav.navigation_action()
                && let Some(req) = action.clone().request()
                && !req.uri().is_some_and(|u| u.starts_with("lastbook:"))
            {
                decision.ignore();
                return true;
            }
            false
        });
        let store_dir = glib::user_cache_dir().join("Lastbook").join("content-filters");
        let store = webkit6::UserContentFilterStore::new(&store_dir.to_string_lossy());
        let m = manager.clone();
        store.save("lastbook-no-network", &glib::Bytes::from_static(NO_NETWORK.as_bytes()), gio::Cancellable::NONE, move |f| match f {
            Ok(filter) => m.add_filter(&filter),
            Err(e) => eprintln!("content filter: {e}"),
        });

        let bar = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        bar.add_css_class("reader-bar");
        let meta = gtk::Label::new(None);
        meta.add_css_class("meta");
        meta.set_xalign(0.0);
        meta.set_hexpand(true);
        meta.set_ellipsize(gtk::pango::EllipsizeMode::End);
        bar.append(&meta);
        let root = gtk::Box::new(gtk::Orientation::Vertical, 0);
        root.append(&bar);
        root.append(&view);
        let page = Rc::new(Self { root: root.upcast(), app: Rc::downgrade(app), view, meta, manager, theme: app.theme.clone(), themed: RefCell::new(false) });
        // The page on screen follows a theme switch.
        let p = Rc::downgrade(&page);
        page.theme.on_change(&page.view, move |_| {
            if let Some(p) = p.upgrade() {
                p.apply_style(*p.themed.borrow());
            }
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

    fn apply_style(&self, themed: bool) {
        self.manager.remove_all_style_sheets();
        let t = self.theme.tokens();
        let css = if themed {
            format!(
                "html, body, .mw-page-container, .mw-body, #content, .mw-content-container {{ background: {bg} !important; color: {text} !important; }}\n\
                 body {{ max-width: 52em; margin: 0 auto !important; padding: 1.5em 2em !important; line-height: 1.55; }}\n\
                 a, a:visited {{ color: {accent} !important; }}\n\
                 table, th, td, .infobox, .navbox, .wikitable, .thumbinner, figure, .toc {{ background: {surface} !important; color: {text} !important; border-color: {border} !important; }}\n\
                 h1, h2, h3, h4 {{ color: {text} !important; border-color: {border} !important; font-family: 'JetBrains Mono', monospace; }}\n\
                 .mw-header, .vector-header-container, .mw-footer, .zim-footer, .mw-editsection, .vector-page-toolbar, #siteSub {{ display: none !important; }}\n\
                 img {{ background: #fff; }}\n\
                 ::selection {{ background: {accent}55; }}\n",
                bg = t.background, text = t.text, accent = t.accent, surface = t.surface, border = t.border
            )
        } else {
            format!("html {{ background: {bg}; }}\n", bg = t.background)
        };
        let sheet = webkit6::UserStyleSheet::new(&css, webkit6::UserContentInjectedFrames::AllFrames, webkit6::UserStyleLevel::User, &[], &[]);
        self.manager.add_style_sheet(&sheet);
    }

    /// Loads `path` (served by the engine, e.g. /zim/<file>/<article> or /pages/...). `themed`: recolour it to the theme
    /// (encyclopedia articles); a saved web page keeps its own look.
    pub fn open(&self, path: &str, meta: &str, themed: bool) {
        self.themed.replace(themed);
        self.apply_style(themed);
        self.meta.set_text(meta);
        let uri = format!("{ORIGIN}{}", if path.starts_with('/') { path.to_owned() } else { format!("/{path}") });
        if self.view.uri().as_deref() != Some(uri.as_str()) {
            self.view.load_uri(&uri);
        }
    }
}
