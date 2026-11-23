// SPDX-License-Identifier: MIT
//! The offline map of the world: OpenStreetMap vector tiles (Protomaps' basemap schema) in MBTiles files, drawn with
//! Cairo in the Omarchy theme's colours.
//!
//! derived/geo/world.mbtiles holds the whole world to zoom 8; derived/geo/regions-*.mbtiles add street-level detail for
//! the regions a person chose (setup makes both: engine/setup/maps.py). Each tile comes from the most detailed file that has
//! it, and a less detailed tile is scaled up where nothing finer exists. Tiles are drawn once into images (at screen
//! scale) and kept; labels for places and help points are placed each frame, without overlapping.
//!
//! Help points (HELP) are marked where a street-level map has them, and a click on a mark names it. The nearest of each
//! kind around the place found (or the map centre) is the engine's answer (`/api/near`, near() in server.py), the one
//! `lastbook place --nearby` gives: measured on the ground there, never here. Where no street-level map covers the
//! place, the engine names the regions whose streets would, and the panel offers to download one: the same setup job
//! Library and downloads starts (setup.rs), shown as it runs, and the help around the place once it is done.

use std::{
    cell::{Cell, RefCell},
    collections::{HashMap, VecDeque},
    f64::consts::PI,
    path::{Path, PathBuf},
    rc::{Rc, Weak},
};

use gtk::{gdk, glib, pango, prelude::*};
use omui::{Theme, Tokens, blend};
use serde_json::{Value, json};

use crate::{
    engine::query,
    mvt::{self, Feature, Geom, Layer},
    setup,
    ui::{App, Route},
};

const TILE: f64 = 256.0;
const MAX_ZOOM: f64 = 19.0;
const DRAW_PER_FRAME: usize = 4;
/// How many points of a kind the nearest-help list shows: enough for a choice, few enough to see every kind.
const PER_GROUP: usize = 4;
/// Below this zoom the map shows more than about 50 km across: its centre is a region, not a place to look around.
const PLACE_ZOOM: f64 = 11.0;

/// The help points the map marks, by the `kind` of the tiles' pois layer and the engine's name for the same (server.py
/// HELP and MAP_HELP): the kinds, the mark, whether it is drawn in the theme's colour for danger, and the heading the
/// legend and the nearest-help list give it. Medical help is one heading: in much of the world a small place has only a
/// clinic or a doctor, and that is where to go.
const HELP: &[(&[&str], &str, bool, &str)] = &[
    (&["hospital", "clinic", "doctors"], "H", true, n_!("Medical help")),
    (&["pharmacy"], "+", true, n_!("Pharmacy")),
    (&["fire_station"], "F", true, n_!("Fire station")),
    (&["police"], "P", false, n_!("Police")),
    (&["drinking_water", "water_point"], "W", false, n_!("Drinking water")),
    (&["shelter"], "S", false, n_!("Shelter: a roof, not an emergency shelter")),
    (&["toilets", "toilet"], "T", false, n_!("Public toilet")),
    (&["supermarket", "marketplace"], "M", false, n_!("Food shop or market")),
];

// --------------------------------------------------------------------------------------------------------- sources --
struct Source {
    db: rusqlite::Connection,
    name: String,
    max_zoom: u32,
    /// A street-level map's box [w, s, e, n] (its `bounds`), and when it was made: the map goes there (`frame`).
    region: Option<([f64; 4], std::time::SystemTime)>,
}

impl Source {
    fn open(path: &Path) -> Option<Self> {
        let db = rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX).ok()?;
        let meta = |k: &str| db.query_row("SELECT value FROM metadata WHERE name=?1", [k], |r| r.get::<_, String>(0)).ok();
        let max_zoom = meta("maxzoom").and_then(|v| v.parse().ok()).unwrap_or(14);
        let name = meta("name").unwrap_or_else(|| path.file_stem().unwrap_or_default().to_string_lossy().into_owned());
        let bounds: Vec<f64> = meta("bounds").unwrap_or_default().split(',').filter_map(|v| v.trim().parse().ok()).collect();
        let made = std::fs::metadata(path).and_then(|m| m.modified()).unwrap_or(std::time::UNIX_EPOCH);
        let region = path.file_name().is_some_and(|n| n.to_string_lossy().starts_with("regions-")).then(|| bounds.try_into().ok()).flatten().map(|b| (b, made));
        Some(Self { db, name, max_zoom, region })
    }

    fn tile(&self, z: u32, x: u32, y: u32) -> Option<Vec<u8>> {
        let row = (1u32 << z) - 1 - y;   // MBTiles rows count from the south (TMS)
        self.db
            .query_row("SELECT tile_data FROM tiles WHERE zoom_level=?1 AND tile_column=?2 AND tile_row=?3", [z, x, row], |r| r.get(0))
            .ok()
    }
}

/// Every map file of the library: regions first (finer), then the world.
fn open_sources(geo: &Path) -> Vec<Source> {
    let mut regions: Vec<PathBuf> = std::fs::read_dir(geo)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with("regions-") && n.ends_with(".mbtiles")))
        .collect();
    regions.sort();
    let mut out: Vec<Source> = regions.iter().filter_map(|p| Source::open(p)).collect();
    out.extend(Source::open(&geo.join("world.mbtiles")));
    out
}

fn files_seen(geo: &Path) -> String {
    let mut v: Vec<String> = std::fs::read_dir(geo)
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|e| {
            let n = e.file_name().to_string_lossy().into_owned();
            n.ends_with(".mbtiles").then(|| format!("{n}:{}", e.metadata().map(|m| m.len()).unwrap_or(0)))
        })
        .collect();
    v.sort();
    v.join("|")
}

// ---------------------------------------------------------------------------------------------------------- colours --
#[derive(Clone)]
struct Style {
    water: String,
    land: String,
    park: String,
    farm: String,
    urban: String,
    building: String,
    minor: String,
    major: String,
    highway: String,
    casing: String,
    rail: String,
    boundary: String,
    text: String,
    dim: String,
    halo: String,
    accent: String,
    danger: String,
}

impl Style {
    /// The map in the theme's colours: its green for what grows, its text colour for what is built.
    fn from(theme: &Theme) -> Self {
        Self::of(&theme.tokens(), &theme.color("green").unwrap_or_else(|| theme.tokens().success))
    }

    fn of(t: &Tokens, green: &str) -> Self {
        let bg = &t.background;
        Self {
            water: blend(bg, &t.accent, if t.light { 0.22 } else { 0.18 }),
            land: bg.clone(),
            park: blend(bg, green, 0.16),
            farm: blend(bg, green, 0.08),
            urban: blend(bg, &t.text, 0.04),
            building: blend(bg, &t.text, 0.13),
            minor: blend(bg, &t.text, 0.24),
            major: blend(bg, &t.text, 0.40),
            highway: blend(bg, &t.accent, 0.55),
            // A road's edge is a shade of the ground, darker in every theme: a shadow, not one of the theme's colours.
            casing: blend(bg, "#000000", 0.25),
            rail: blend(bg, &t.text, 0.30),
            boundary: blend(bg, &t.text, 0.40),
            text: t.text.clone(),
            dim: t.dim_text.clone(),
            halo: bg.clone(),
            accent: t.accent.clone(),
            danger: t.danger.clone(),
        }
    }
}

fn rgb(cr: &cairo::Context, hex: &str, alpha: f64) {
    let c = gdk::RGBA::parse(hex).unwrap_or(gdk::RGBA::BLACK);
    cr.set_source_rgba(f64::from(c.red()), f64::from(c.green()), f64::from(c.blue()), alpha);
}

// ------------------------------------------------------------------------------------------------------ projection --
fn lonlat_to_world(lon: f64, lat: f64) -> (f64, f64) {
    let lat = lat.clamp(-85.0511, 85.0511).to_radians();
    ((lon + 180.0) / 360.0, (1.0 - (lat.tan() + 1.0 / lat.cos()).ln() / PI) / 2.0)
}

fn world_to_lonlat(x: f64, y: f64) -> (f64, f64) {
    let lon = x * 360.0 - 180.0;
    let lat = (PI * (1.0 - 2.0 * y)).sinh().atan().to_degrees();
    (lon, lat)
}

// ------------------------------------------------------------------------------------------------------ the widget --
/// A help point drawn in the last frame: where on screen, what it is, and where in the world (0..1).
#[derive(Clone)]
struct Mark {
    x: f64,
    y: f64,
    kind: String,
    name: String,
    world: (f64, f64),
}

/// A tile's decoded layers, shared by every tile drawn from it (a coarser tile is drawn scaled up for the finer ones).
type Layers = Rc<Vec<Layer>>;

struct Cached {
    image: Option<cairo::ImageSurface>,
    layers: Rc<Vec<Layer>>,
    /// The tile the data came from (it is scaled up when coarser than the tile drawn).
    from: (u32, u32, u32),
}

pub struct MapPage {
    pub root: gtk::Widget,
    app: Weak<App>,
    area: gtk::DrawingArea,
    sources: RefCell<Vec<Source>>,
    style: RefCell<Style>,
    /// Map centre in world units (0..1) and zoom.
    cx: Cell<f64>,
    cy: Cell<f64>,
    zoom: Cell<f64>,
    tiles: RefCell<HashMap<(u32, u32, u32), Cached>>,
    order: RefCell<VecDeque<(u32, u32, u32)>>,
    decoded: RefCell<HashMap<(u32, u32, u32), Layers>>,
    pin: RefCell<Option<(f64, f64, String)>>,
    drag_from: Cell<(f64, f64)>,
    status: gtk::Label,
    search: gtk::SearchEntry,
    results: gtk::ListBox,
    results_pop: gtk::Popover,
    found: RefCell<Vec<Value>>,
    /// The help points drawn, for a click; the one a click or the list chose, ringed.
    marks: RefCell<Vec<Mark>>,
    chosen: Cell<Option<(f64, f64)>>,
    /// A clicked help point's name, kind and distance.
    info: gtk::Popover,
    info_text: (gtk::Label, gtk::Label, gtk::Label),
    /// The nearest help around the pin: a panel over the map's right side (all of its width in a narrow window).
    panel: gtk::Box,
    panel_around: gtk::Label,
    panel_note: gtk::Label,
    panel_manage: gtk::Button,
    /// Counts what the panel was asked to show: an answer for an earlier question is dropped.
    asked: Cell<u64>,
    /// Where no street-level map covers the place: a button for each region whose streets would, or the download of one.
    offer: gtk::Box,
    /// The regions the engine named for the place, not yet installed ({id, name}).
    near_regions: RefCell<Vec<Value>>,
    /// The setup job as last read, whether the panel is showing it, the timer that reads its progress, and the buttons
    /// that act on it (made again only when what they offer changes, so the keyboard's focus stays on them).
    job: RefCell<Value>,
    following: Cell<bool>,
    job_timer: RefCell<Option<glib::SourceId>>,
    job_buttons: gtk::Box,
    job_buttons_for: RefCell<String>,
    help_list: gtk::ListBox,
    /// What the list's rows go to, by index: a point ({lat, lon}) or a street-level map ({bounds}); None for a heading.
    help_rows: RefCell<Vec<Option<Value>>>,
    /// The map files open now (names and sizes): a download that adds or finishes one opens them again.
    loaded: RefCell<String>,
    /// Whether the map has been put somewhere yet: the first time it is shown with nothing asked for, it goes to a
    /// street-level map rather than showing the whole world (`show`).
    placed: Cell<bool>,
    /// A box [w, s, e, n] to show whole at the next frame, when the map's size is known.
    frame_to: Cell<Option<[f64; 4]>>,
}

impl MapPage {
    pub fn build(app: &Rc<App>) -> Rc<Self> {
        let area = gtk::DrawingArea::new();
        area.set_hexpand(true);
        area.set_vexpand(true);
        area.set_focusable(true);
        omui::i18n::bind(&area, |a| a.update_property(&[gtk::accessible::Property::Label(&omui::tr("Map"))]));
        let search = gtk::SearchEntry::new();
        omui::i18n::bind(&search, |s| s.set_placeholder_text(Some(&omui::tr("Find a town, or LAT,LON"))));
        search.set_width_chars(22);
        let results = gtk::ListBox::new();
        results.add_css_class("map-results");
        let results_pop = gtk::Popover::new();
        results_pop.set_child(Some(&gtk::ScrolledWindow::builder().child(&results).max_content_height(360).propagate_natural_height(true).min_content_width(360).build()));
        results_pop.set_parent(&search);
        results_pop.set_autohide(true);
        results_pop.set_has_arrow(false);
        let zoom_in = omui::icon_button(omui::icons::name::ZOOM_IN, "");
        omui::i18n::bind_tooltip(&zoom_in, "Zoom in (+)");
        let zoom_out = omui::icon_button(omui::icons::name::ZOOM_OUT, "");
        omui::i18n::bind_tooltip(&zoom_out, "Zoom out (-)");
        let help = omui::icon_button(omui::icons::name::LOCATE, "");
        omui::i18n::bind_tooltip(&help, "Nearest help");
        let legend = gtk::MenuButton::builder().icon_name(omui::icons::name::INFO).has_frame(false).build();
        legend.add_css_class("header-action");
        omui::i18n::bind_tooltip(&legend, "What the marks mean");
        let top = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        top.add_css_class("map-bar");
        top.set_halign(gtk::Align::Start);
        top.set_valign(gtk::Align::Start);
        top.set_margin_top(12);
        top.set_margin_start(12);
        top.append(&search);
        top.append(&zoom_out);
        top.append(&zoom_in);
        top.append(&help);
        top.append(&legend);
        let status = gtk::Label::new(None);
        status.add_css_class("map-status");
        status.set_halign(gtk::Align::End);
        status.set_valign(gtk::Align::End);
        status.set_margin_end(8);
        status.set_margin_bottom(6);
        let overlay = gtk::Overlay::new();
        overlay.set_child(Some(&area));
        overlay.add_overlay(&top);
        overlay.add_overlay(&status);

        let info = gtk::Popover::new();
        info.set_autohide(false);   // the map keeps the keyboard and the pointer; moving it closes this
        // On the overlay, where the map's place is (0, 0): GTK closes a popover of the drawing area as soon as it opens.
        info.set_parent(&overlay);
        let info_text = (gtk::Label::new(None), gtk::Label::new(None), gtk::Label::new(None));
        let info_box = gtk::Box::new(gtk::Orientation::Vertical, 2);
        for (l, class) in [(&info_text.0, "result-title"), (&info_text.1, "meta"), (&info_text.2, "meta")] {
            l.set_xalign(0.0);
            l.set_wrap(true);
            l.set_max_width_chars(36);
            l.add_css_class(class);
            info_box.append(l);
        }
        info.set_child(Some(&info_box));

        let panel = gtk::Box::new(gtk::Orientation::Vertical, 6);
        panel.add_css_class("map-panel");
        panel.set_width_request(300);
        panel.set_halign(gtk::Align::End);
        panel.set_valign(gtk::Align::Start);   // as tall as its rows, and no taller than the map
        panel.set_margin_top(64);
        panel.set_margin_bottom(34);
        panel.set_margin_start(12);
        panel.set_margin_end(12);
        panel.set_visible(false);
        let head = gtk::Box::new(gtk::Orientation::Horizontal, 4);
        let titles = gtk::Box::new(gtk::Orientation::Vertical, 0);
        titles.set_hexpand(true);
        let heading = gtk::Label::new(None);
        omui::i18n::bind_label(&heading, "Nearest help");
        heading.add_css_class("heading");
        heading.set_xalign(0.0);
        let panel_around = gtk::Label::new(None);
        panel_around.add_css_class("meta");
        panel_around.set_xalign(0.0);
        panel_around.set_ellipsize(pango::EllipsizeMode::End);
        titles.append(&heading);
        titles.append(&panel_around);
        let here = omui::icon_button(omui::icons::name::LOCATION, "");
        omui::i18n::bind_tooltip(&here, "Nearest help around the map centre");
        let close = omui::icon_button(omui::icons::name::CLOSE, "");
        omui::i18n::bind_tooltip(&close, "Close");
        head.append(&titles);
        head.append(&here);
        head.append(&close);
        let panel_note = gtk::Label::new(None);
        panel_note.set_wrap(true);
        panel_note.set_max_width_chars(30);
        panel_note.set_xalign(0.0);
        let panel_manage = gtk::Button::new();
        omui::i18n::bind_label(&panel_manage, "Library and downloads");
        panel_manage.set_halign(gtk::Align::Start);
        panel_manage.add_css_class("flat");   // other choices: the download the panel offers comes first
        let offer = gtk::Box::new(gtk::Orientation::Vertical, 6);
        let help_list = gtk::ListBox::new();
        help_list.add_css_class("map-results");
        let foot = gtk::Label::new(None);
        omui::i18n::bind_label(&foot, "Offline map data: it may be out of date and shows no closures or damage. Distances are straight lines.");
        foot.set_wrap(true);
        foot.set_max_width_chars(30);
        foot.set_xalign(0.0);
        foot.add_css_class("meta");
        panel.append(&head);
        panel.append(&panel_note);
        panel.append(&offer);
        panel.append(&panel_manage);
        panel.append(&gtk::ScrolledWindow::builder().child(&help_list).vexpand(true).propagate_natural_height(true).hscrollbar_policy(gtk::PolicyType::Never).build());
        panel.append(&foot);
        overlay.add_overlay(&panel);

        let page = Rc::new(Self {
            root: overlay.upcast(),
            app: Rc::downgrade(app),
            area,
            sources: RefCell::new(Vec::new()),
            style: RefCell::new(Style::from(&app.theme)),
            cx: Cell::new(0.5),
            cy: Cell::new(0.42),
            zoom: Cell::new(2.0),
            tiles: RefCell::new(HashMap::new()),
            order: RefCell::new(VecDeque::new()),
            decoded: RefCell::new(HashMap::new()),
            pin: RefCell::new(None),
            drag_from: Cell::new((0.0, 0.0)),
            status,
            search,
            results,
            results_pop,
            found: RefCell::new(Vec::new()),
            marks: RefCell::new(Vec::new()),
            chosen: Cell::new(None),
            info,
            info_text,
            panel,
            panel_around,
            panel_note,
            panel_manage,
            asked: Cell::new(0),
            offer,
            near_regions: RefCell::new(Vec::new()),
            job: RefCell::new(Value::Null),
            following: Cell::new(false),
            job_timer: RefCell::new(None),
            job_buttons: gtk::Box::new(gtk::Orientation::Horizontal, 6),
            job_buttons_for: RefCell::new(String::new()),
            help_list,
            help_rows: RefCell::new(Vec::new()),
            loaded: RefCell::new(String::new()),
            placed: Cell::new(false),
            frame_to: Cell::new(None),
        });
        let me = Rc::downgrade(&page);
        page.area.set_draw_func(move |_, cr, w, h| {
            if let Some(me) = me.upgrade() {
                me.draw(cr, f64::from(w), f64::from(h));
            }
        });
        let me = Rc::downgrade(&page);
        let theme = app.theme.clone();
        app.theme.on_change(&page.area, move |_| {
            if let Some(me) = me.upgrade() {
                me.style.replace(Style::from(&theme));
                me.tiles.borrow_mut().clear();
                me.order.borrow_mut().clear();
                me.area.queue_draw();
            }
        });
        page.controls(&zoom_in, &zoom_out);
        page.help_controls(&help, &here, &close);
        legend.set_popover(Some(&page.legend()));
        page
    }

    /// The legend: each mark as the map draws it, with what it stands for.
    fn legend(self: &Rc<Self>) -> gtk::Popover {
        let grid = gtk::Grid::builder().row_spacing(4).column_spacing(8).build();
        for (row, &(_, mark, danger, heading)) in HELP.iter().enumerate() {
            let swatch = gtk::DrawingArea::builder().content_width(18).content_height(18).build();
            let me = Rc::downgrade(self);
            swatch.set_draw_func(move |_, cr, w, h| {
                if let Some(me) = me.upgrade() {
                    let layout = pangocairo::functions::create_layout(cr);
                    layout.set_font_description(Some(&label_font()));
                    draw_mark(cr, &layout, mark, danger, (f64::from(w) / 2.0, f64::from(h) / 2.0), &me.style.borrow());
                }
            });
            let text = gtk::Label::new(None);
            omui::i18n::bind(&text, move |l| l.set_text(&omui::i18n::translate(heading)));
            text.set_xalign(0.0);
            grid.attach(&swatch, 0, row as i32, 1, 1);
            grid.attach(&text, 1, row as i32, 1, 1);
        }
        let note = gtk::Label::new(None);
        omui::i18n::bind_label(&note, "Marks show from street level, where a street map of the area is installed. Click one for its name and distance.");
        note.set_wrap(true);
        note.set_max_width_chars(34);
        note.set_xalign(0.0);
        note.add_css_class("meta");
        grid.attach(&note, 0, HELP.len() as i32, 2, 1);
        let pop = gtk::Popover::new();
        pop.set_child(Some(&grid));
        pop
    }

    fn help_controls(self: &Rc<Self>, help: &gtk::Button, here: &gtk::Button, close: &gtk::Button) {
        let me = Rc::downgrade(self);
        help.connect_clicked(move |_| {
            if let Some(m) = me.upgrade() {
                if m.panel.is_visible() {
                    m.panel.set_visible(false);
                } else {
                    let unpinned = m.pin.borrow().is_none();
                    if m.nearest(unpinned) {
                        m.help_list.grab_focus();
                    }
                }
            }
        });
        let me = Rc::downgrade(self);
        here.connect_clicked(move |_| {
            if let Some(m) = me.upgrade() {
                m.nearest(true);
            }
        });
        let me = Rc::downgrade(self);
        close.connect_clicked(move |_| {
            if let Some(m) = me.upgrade() {
                m.panel.set_visible(false);
                m.area.grab_focus();
            }
        });
        let me = Rc::downgrade(self);
        self.panel_manage.connect_clicked(move |_| {
            if let Some(app) = me.upgrade().and_then(|m| m.app.upgrade()) {
                app.navigate(Route::Manage);
            }
        });
        let me = Rc::downgrade(self);
        self.help_list.connect_row_activated(move |_, row| {
            let Some(m) = me.upgrade() else { return };
            let Some(Some(p)) = m.help_rows.borrow().get(row.index() as usize).cloned() else { return };
            // Over a narrow map the panel hides what the row is for: it gives way to the map.
            if m.panel.halign() != gtk::Align::End {
                m.panel.set_visible(false);
            }
            let bounds: Option<Vec<f64>> = serde_json::from_value(p["bounds"].clone()).ok();
            if let Some(b) = bounds.and_then(|b| b.try_into().ok()) {
                m.frame(b);
            } else if let (Some(lat), Some(lon)) = (p["lat"].as_f64(), p["lon"].as_f64()) {
                m.center(lat, lon, m.zoom.get().max(16.0), None);
                m.chosen.set(Some(lonlat_to_world(lon, lat)));
            }
        });
        // In a narrow window the panel takes the map's width: beside a third of it, its rows would not be readable.
        let me = Rc::downgrade(self);
        self.area.connect_resize(move |_, w, _| {
            if let Some(m) = me.upgrade() {
                m.panel.set_halign(if w < 640 { gtk::Align::Fill } else { gtk::Align::End });
            }
        });
        let click = gtk::GestureClick::new();
        let me = Rc::downgrade(self);
        click.connect_released(move |_, n, x, y| {
            if n == 1
                && let Some(m) = me.upgrade()
            {
                m.info.popdown();
                let off = |k: &Mark| (k.x - x).hypot(k.y - y);
                let hit = m.marks.borrow().iter().filter(|k| off(k) <= 12.0).min_by(|a, b| off(a).total_cmp(&off(b))).cloned();
                if let Some(hit) = hit {
                    m.show_mark(&hit);
                }
            }
        });
        self.area.add_controller(click);
    }

    /// Where distances are measured from: the pin (the place found), else the map centre.
    fn anchor(&self) -> (f64, f64, String) {
        self.pin.borrow().clone().unwrap_or_else(|| {
            let (x, y) = self.seen_centre();
            (x, y, omui::tr("Map centre"))
        })
    }

    /// How far (in world units) the middle of what can be seen is left of the map's centre: half the panel, where it is
    /// beside the map.
    fn shift(&self) -> f64 {
        let beside = self.panel.is_visible() && self.panel.halign() == gtk::Align::End;
        if beside { f64::from(self.panel.width().max(300) + 12) / 2.0 / (TILE * 2f64.powf(self.zoom.get())) } else { 0.0 }
    }

    /// The map centre as the person sees it: the middle of the map left of the panel.
    fn seen_centre(&self) -> (f64, f64) {
        ((self.cx.get() - self.shift()).rem_euclid(1.0), self.cy.get())
    }

    /// Names a clicked help point, and asks the engine how far it is from the anchor.
    fn show_mark(self: &Rc<Self>, Mark { x, y, kind, name, world }: &Mark) {
        let Some(app) = self.app.upgrade() else { return };
        let kind_text = kind_name(kind);
        self.info_text.0.set_text(if name.is_empty() { &kind_text } else { name });
        self.info_text.1.set_text(&kind_text);
        self.info_text.1.set_visible(!name.is_empty());
        self.info_text.2.set_text("");
        self.info.set_pointing_to(Some(&gdk::Rectangle::new(*x as i32 - 8, *y as i32 - 8, 16, 16)));
        self.info.popup();
        self.chosen.set(Some(*world));
        self.area.queue_draw();
        let (ax, ay, from) = self.anchor();
        let ((alon, alat), (lon, lat)) = (world_to_lonlat(ax, ay), world_to_lonlat(world.0, world.1));
        let (a, b, to) = (format!("{alat:.6}"), format!("{alon:.6}"), format!("{lat:.6},{lon:.6}"));
        let (engine, me) = (app.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let r = engine.get(&query("/api/near", &[("lat", &a), ("lon", &b), ("to", &to)])).await;
            if let Some(me) = me.upgrade()
                && let Ok(v) = r
                && let Some(km) = v["km"].as_f64()
            {
                let (far, dir) = (distance(km), direction(v["dir"].as_str().unwrap_or_default()));
                // The centre is no name: a sentence of its own, as a translator needs it.
                me.info_text.2.set_text(&if from == omui::tr("Map centre") { omui::tr!("{} {} of the map centre", far, dir) } else { omui::tr!("{} {} of {}", far, dir, from) });
            }
        });
    }

    /// Shows the panel with the nearest help around the pin; `here` moves the pin to the map centre first. False where
    /// the map shows too wide an area for its centre to be a place to look around (PLACE_ZOOM): the panel asks for one.
    fn nearest(self: &Rc<Self>, here: bool) -> bool {
        let Some(app) = self.app.upgrade() else { return false };
        self.stop_job();
        if here {
            if self.zoom.get() < PLACE_ZOOM {
                self.ask_for_place();
                return false;
            }
            self.panel.set_visible(true);
            let (x, y) = self.seen_centre();
            self.pin.replace(Some((x, y, omui::tr("Map centre"))));
            self.area.queue_draw();
        }
        let (ax, ay, label) = self.anchor();
        let (lon, lat) = world_to_lonlat(ax, ay);
        self.panel_around.set_text(&omui::tr!("Around {}", label));
        self.panel.set_visible(true);
        let (engine, me, a, b) = (app.engine.clone(), Rc::downgrade(self), format!("{lat:.6}"), format!("{lon:.6}"));
        let asked = self.asked.get();
        glib::spawn_future_local(async move {
            let r = engine.get(&query("/api/near", &[("lat", &a), ("lon", &b)])).await;
            if let Some(me) = me.upgrade()
                && me.asked.get() == asked
            {
                me.fill_help(r.ok());
            }
        });
        true
    }

    /// The panel where no place is chosen and the whole map is too wide for its centre to be one: the find field takes
    /// the keyboard, and the street-level maps installed are offered to go to.
    fn ask_for_place(&self) {
        self.stop_job();
        self.panel.set_visible(true);
        self.panel_around.set_text(&omui::tr("The map shows too wide an area"));
        omui::clear(&self.offer);
        self.panel_manage.set_visible(false);
        omui::clear(&self.help_list);
        let mut rows = Vec::new();
        let sources = self.sources.borrow();
        let installed: Vec<(&str, [f64; 4])> = sources.iter().filter_map(|s| s.region.map(|(b, _)| (s.name.as_str(), b))).collect();
        self.panel_note.set_text(&if installed.is_empty() {
            omui::tr("Find a town above, or zoom in to where you are, to see the help near it.")
        } else {
            omui::tr("Find a town above, or zoom in to where you are, to see the help near it. Or go to a street-level map:")
        });
        self.panel_note.set_visible(true);
        if !installed.is_empty() {
            self.add_heading(&omui::tr("Street-level maps installed"));
            rows.push(None);
        }
        for (name, bounds) in installed {
            self.add_row(name, &omui::tr("Show it on the map"));
            rows.push(Some(json!({"bounds": bounds})));
        }
        self.help_rows.replace(rows);
        self.search.grab_focus();
    }

    fn add_heading(&self, text: &str) {
        let h = gtk::Label::new(Some(text));
        h.set_wrap(true);
        h.set_max_width_chars(30);
        h.add_css_class("heading");
        h.set_xalign(0.0);
        h.set_margin_top(6);
        let row = gtk::ListBoxRow::builder().child(&h).activatable(false).selectable(false).focusable(false).build();
        self.help_list.append(&row);
    }

    fn add_row(&self, title: &str, meta: &str) {
        let b = gtk::Box::new(gtk::Orientation::Vertical, 1);
        for (text, class) in [(title, "result-title"), (meta, "meta")] {
            let l = gtk::Label::new(Some(text));
            l.set_xalign(0.0);
            l.set_ellipsize(pango::EllipsizeMode::End);
            l.set_max_width_chars(30);
            l.add_css_class(class);
            b.append(&l);
        }
        self.help_list.append(&b);
    }

    fn fill_help(self: &Rc<Self>, r: Option<Value>) {
        omui::clear(&self.help_list);
        omui::clear(&self.offer);
        let mut rows = Vec::new();
        let points = r.as_ref().and_then(|v| v["points"].as_array()).cloned().unwrap_or_default();
        let covered = r.as_ref().is_some_and(|v| v["covered"].as_bool() == Some(true));
        let regions = r.as_ref().and_then(|v| v["regions"].as_array()).cloned().unwrap_or_default();
        let offered: Vec<Value> = regions.iter().filter(|g| g["installed"] != true).cloned().collect();
        let note = match &r {
            None => omui::tr("The map's help points could not be read."),
            Some(_) if !covered && !offered.is_empty() => omui::tr("No street-level map covers this place yet. Download its streets to see the help near it:"),
            Some(_) if !covered && regions.is_empty() => omui::tr("There is no street-level map to download for this place: it may be out at sea. Find a town to see the help near it."),
            Some(_) if !covered => omui::tr("No street-level map covers this place, so its help points cannot be shown."),
            Some(_) if points.is_empty() => omui::tr("No help points on the map near this place."),
            Some(_) => String::new(),
        };
        self.panel_note.set_text(&note);
        self.panel_note.set_visible(!note.is_empty());
        self.panel_manage.set_visible(r.is_some() && !covered);
        self.near_regions.replace(offered);
        if !self.near_regions.borrow().is_empty() {
            self.offer_downloads(None);
            self.read_job();   // one under way (here or in another window) is shown instead
        }
        for &(kinds, _, _, heading) in HELP {
            let mine: Vec<&Value> = points.iter().filter(|p| kinds.contains(&p["cls"].as_str().unwrap_or_default())).take(PER_GROUP).collect();
            if mine.is_empty() {
                continue;
            }
            self.add_heading(&omui::i18n::translate(heading));
            rows.push(None);
            for p in mine {
                let kind = kind_name(p["cls"].as_str().unwrap_or_default());
                let far = format!("{} {}", distance(p["km"].as_f64().unwrap_or_default()), direction(p["dir"].as_str().unwrap_or_default()));
                // A point the map gives no name is called by its kind, which is then not said twice.
                let (name, meta) = match p["name"].as_str().filter(|n| !n.is_empty()) {
                    Some(n) => (n, format!("{kind} · {far}")),
                    None => (kind.as_str(), far),
                };
                self.add_row(name, &meta);
                rows.push(Some(p.clone()));
            }
        }
        self.help_rows.replace(rows);
    }

    // ------------------------------------------------------------------------------------- adding the streets --
    /// A button for each region whose streets would cover the place, smallest first. Nothing reaches the network until
    /// one is pressed. `error`: why the last press started nothing (no room on the disk, another download under way).
    fn offer_downloads(self: &Rc<Self>, error: Option<&str>) {
        self.following.set(false);
        omui::clear(&self.offer);
        self.offer.set_sensitive(true);
        for g in self.near_regions.borrow().iter() {
            let b = crate::cards::wrap_button(&omui::tr!("Download streets for {}", g["name"].as_str().unwrap_or_default()));
            if let Some(l) = b.child().and_downcast::<gtk::Label>() {
                l.set_max_width_chars(30);
            }
            b.add_css_class(if self.offer.first_child().is_none() { "suggested-action" } else { "chip" });
            let (me, id) = (Rc::downgrade(self), g["id"].clone());
            b.connect_clicked(move |_| {
                if let Some(me) = me.upgrade() {
                    me.start_download(id.clone());
                }
            });
            self.offer.append(&b);
        }
        self.offer.append(&meta(&omui::tr("Its size is known once the download starts: a small country is about 0.1–0.5 GB, a large one several GB.")));
        if let Some(e) = error {
            self.offer.append(&setup::error_label(e));
        }
    }

    /// Starts the job Library and downloads would start for the region: its streets, made on this computer, straight
    /// into the open library (with the world map, if it is missing).
    fn start_download(self: &Rc<Self>, id: Value) {
        let Some(app) = self.app.upgrade() else { return };
        self.offer.set_sensitive(false);
        let (engine, me) = (app.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let r = engine.post("/api/setup/start", &json!({"pack": null, "world": false, "regions": [id]})).await;
            let Some(me) = me.upgrade() else { return };
            match r {
                Ok(v) if v["error"].is_string() => me.offer_downloads(v["error"].as_str()),
                Ok(v) => {
                    me.following.set(true);
                    me.draw_job(&v);
                }
                Err(e) => me.offer_downloads(Some(&e.to_string())),
            }
        });
    }

    fn read_job(self: &Rc<Self>) {
        let Some(app) = self.app.upgrade() else { return };
        let (engine, me) = (app.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            if let Ok(job) = engine.get("/api/setup/job").await
                && let Some(me) = me.upgrade()
            {
                me.draw_job(&job);
            }
        });
    }

    /// The job in the panel, as setup.rs shows it. One that has finished the streets for this place opens them and
    /// shows the help around it; one that is not under way leaves the download buttons.
    fn draw_job(self: &Rc<Self>, job: &Value) {
        if !self.panel.is_visible() || self.near_regions.borrow().is_empty() {
            return;   // the panel was closed, or has moved on to a place that needs no download
        }
        let state = job["state"].as_str().unwrap_or_default();
        let ours = job["items"].as_array().into_iter().flatten().any(|it| self.near_regions.borrow().iter().any(|g| g["id"] == it["id"]));
        let elsewhere = job["elsewhere"] == true;
        let was_following = self.following.get();
        self.job.replace(job.clone());
        match state {
            "done" if ours && was_following => {
                self.stop_job();
                self.show();   // the new map file: drawn, and searched by the engine
                self.nearest(false);
                return;
            }
            "" | "done" => return self.offer_downloads(None),
            "failed" if !ours || job["error"]["code"] == "cancelled" => return self.offer_downloads(None),
            _ => {}
        }
        self.following.set(true);
        let focused = self.job_buttons.focus_child().is_some();   // kept on the button through each redraw, and to Resume after Pause
        if self.job_buttons.parent().is_some() {
            self.offer.remove(&self.job_buttons);
        }
        omui::clear(&self.offer);
        self.offer.set_sensitive(true);
        let regions = self.near_regions.borrow().clone();
        let mut said = Vec::new();   // an item's reason is not repeated as the job's
        for it in job["items"].as_array().into_iter().flatten() {
            said.extend(it["error"]["message"].as_str());
            self.offer.append(&setup::item_row(it, &regions));
        }
        if let Some(e) = job["error"]["message"].as_str().filter(|e| !said.contains(e)) {
            self.offer.append(&setup::error_label(e));
        }
        if elsewhere {
            self.offer.append(&meta(&omui::tr("This download is running in another Lastbook window. Use that window to pause or cancel it.")));
        }
        // Stopping keeps what has arrived (Resume carries on from it); cancelling, which deletes it, is Library and
        // downloads' to ask about.
        let (text, path) = match state {
            _ if elsewhere => (String::new(), ""),
            "paused" => (omui::tr("Resume"), "/api/setup/resume"),
            "failed" => (omui::tr("Try again"), "/api/setup/retry"),
            _ => (omui::tr("Pause"), "/api/setup/pause"),
        };
        if *self.job_buttons_for.borrow() != path {
            omui::clear(&self.job_buttons);
            self.job_buttons_for.replace(path.to_owned());
            if !path.is_empty() {
                let b = gtk::Button::with_label(&text);
                b.add_css_class(if path == "/api/setup/pause" { "chip" } else { "suggested-action" });
                let me = Rc::downgrade(self);
                b.connect_clicked(move |_| {
                    if let Some(me) = me.upgrade() {
                        me.act(path);
                    }
                });
                self.job_buttons.append(&b);
            }
        }
        self.offer.append(&self.job_buttons);
        if focused && let Some(b) = self.job_buttons.first_child() {
            b.grab_focus();
        }
        self.watch_job();
    }

    /// Pause, resume or try again: the job as it then is, or why the engine would not.
    fn act(self: &Rc<Self>, path: &'static str) {
        let Some(app) = self.app.upgrade() else { return };
        let (engine, me) = (app.engine.clone(), Rc::downgrade(self));
        glib::spawn_future_local(async move {
            let r = engine.post(path, &json!({})).await;
            let Some(me) = me.upgrade() else { return };
            match r {
                Ok(v) if v.get("items").is_some() => me.draw_job(&v),
                Ok(v) => me.offer.append(&setup::error_label(v["error"].as_str().unwrap_or_default())),
                Err(e) => me.offer.append(&setup::error_label(&e.to_string())),
            }
        });
    }

    /// Reads a running job's progress once a second while the panel shows it on screen, in the window in use. A job's
    /// change of state has no word of its own here (setup.rs follows those), so this reading also sees it end.
    fn watch_job(self: &Rc<Self>) {
        if !setup::moves(&self.job.borrow()) {
            return self.stop_job_timer();
        }
        if self.job_timer.borrow().is_some() {
            return;
        }
        let me = Rc::downgrade(self);
        let timer = glib::timeout_add_local(std::time::Duration::from_secs(1), move || {
            let Some(m) = me.upgrade() else { return glib::ControlFlow::Break };
            if !m.panel.is_visible() || !m.root.is_mapped() {
                m.job_timer.take();   // `show` reads it again when the map is back
                return glib::ControlFlow::Break;
            }
            if m.root.root().and_downcast::<gtk::Window>().is_some_and(|w| w.is_active()) {
                m.read_job();
            }
            glib::ControlFlow::Continue
        });
        self.job_timer.replace(Some(timer));
    }

    fn stop_job_timer(&self) {
        if let Some(t) = self.job_timer.take() {
            t.remove();
        }
    }

    /// The panel is about to show something else: an answer still on its way is dropped, and the job carries on,
    /// unwatched.
    fn stop_job(&self) {
        self.asked.set(self.asked.get() + 1);
        self.near_regions.borrow_mut().clear();
        self.following.set(false);
        self.stop_job_timer();
        self.job_buttons_for.replace(String::new());
    }

    fn controls(self: &Rc<Self>, zoom_in: &gtk::Button, zoom_out: &gtk::Button) {
        let me = Rc::downgrade(self);
        zoom_in.connect_clicked(move |_| if let Some(m) = me.upgrade() { m.zoom_by(1.0, None) });
        let me = Rc::downgrade(self);
        zoom_out.connect_clicked(move |_| if let Some(m) = me.upgrade() { m.zoom_by(-1.0, None) });
        let drag = gtk::GestureDrag::new();
        let me = Rc::downgrade(self);
        drag.connect_drag_begin(move |_, _, _| {
            if let Some(m) = me.upgrade() {
                m.drag_from.set((m.cx.get(), m.cy.get()));
                m.info.popdown();
                m.area.grab_focus();
            }
        });
        let me = Rc::downgrade(self);
        drag.connect_drag_update(move |_, dx, dy| {
            if let Some(m) = me.upgrade() {
                let scale = TILE * 2f64.powf(m.zoom.get());
                let (fx, fy) = m.drag_from.get();
                m.cx.set((fx - dx / scale).rem_euclid(1.0));
                m.cy.set((fy - dy / scale).clamp(0.0, 1.0));
                m.area.queue_draw();
            }
        });
        self.area.add_controller(drag);
        let scroll = gtk::EventControllerScroll::new(gtk::EventControllerScrollFlags::VERTICAL);
        let pointer = Rc::new(Cell::new(None::<(f64, f64)>));
        let motion = gtk::EventControllerMotion::new();
        let p = pointer.clone();
        motion.connect_motion(move |_, x, y| p.set(Some((x, y))));
        self.area.add_controller(motion);
        let me = Rc::downgrade(self);
        scroll.connect_scroll(move |_, _, dy| {
            if let Some(m) = me.upgrade() {
                m.zoom_by(-dy * 0.5, pointer.get());
            }
            glib::Propagation::Stop
        });
        self.area.add_controller(scroll);
        let click = gtk::GestureClick::new();
        let me = Rc::downgrade(self);
        click.connect_pressed(move |_, n, x, y| {
            if n == 2
                && let Some(m) = me.upgrade()
            {
                m.zoom_by(1.0, Some((x, y)));
            }
        });
        self.area.add_controller(click);
        let keys = gtk::EventControllerKey::new();
        let me = Rc::downgrade(self);
        keys.connect_key_pressed(move |_, key, _, _| {
            let Some(m) = me.upgrade() else { return glib::Propagation::Proceed };
            let step = 120.0 / (TILE * 2f64.powf(m.zoom.get()));
            match key {
                gdk::Key::plus | gdk::Key::equal | gdk::Key::KP_Add => m.zoom_by(1.0, None),
                gdk::Key::minus | gdk::Key::KP_Subtract => m.zoom_by(-1.0, None),
                gdk::Key::Left => m.cx.set((m.cx.get() - step).rem_euclid(1.0)),
                gdk::Key::Right => m.cx.set((m.cx.get() + step).rem_euclid(1.0)),
                gdk::Key::Up => m.cy.set((m.cy.get() - step).clamp(0.0, 1.0)),
                gdk::Key::Down => m.cy.set((m.cy.get() + step).clamp(0.0, 1.0)),
                _ => return glib::Propagation::Proceed,
            }
            m.info.popdown();
            m.area.queue_draw();
            glib::Propagation::Stop
        });
        self.area.add_controller(keys);
        let me = Rc::downgrade(self);
        self.search.connect_activate(move |e| {
            if let Some(m) = me.upgrade() {
                m.find(e.text().as_str());
            }
        });
        let me = Rc::downgrade(self);
        self.results.connect_row_activated(move |_, row| {
            if let Some(m) = me.upgrade()
                && let Some(p) = m.found.borrow().get(row.index() as usize)
                && let (Some(lat), Some(lon)) = (p["lat"].as_f64(), p["lon"].as_f64())
            {
                m.results_pop.popdown();
                let z = match p["kind"].as_str().unwrap_or_default() {
                    "country" => 5.0,
                    "region" | "state" => 7.0,
                    // Help points are marked from 13, where a street map has them.
                    "city" | "town" | "locality" if p["onMap"].as_bool() == Some(true) => 13.0,
                    "city" | "town" | "locality" => 12.0,
                    _ => 14.0,
                };
                m.panel.set_visible(true);   // before centring, which leaves room for it
                m.center(lat, lon, z, Some(p["name"].as_str().map(str::to_owned).unwrap_or_else(|| omui::tr("Place"))));
                m.nearest(false);
            }
        });
    }

    /// After a language switch: the line in the corner is written again as ui.rs shows the map again.
    pub fn retranslate(&self) {
        self.loaded.replace(String::new());
    }

    /// Opens the map files when the map is shown, and again whenever they have changed (setup adds maps while Lastbook runs).
    /// The first time, with nothing asked for, it goes to the street-level map made last rather than showing the whole
    /// world. A download followed in the panel is read again: its timer stops while the map is away.
    pub fn show(self: &Rc<Self>) {
        if self.following.get() && setup::moves(&self.job.borrow()) {
            self.read_job();
        }
        if let Some(app) = self.app.upgrade()
            && let Some(data) = app.engine.ready.data.as_ref()
            && let geo = data.join("derived").join("geo")
            && let seen = files_seen(&geo)
            && *self.loaded.borrow() != seen
        {
            self.loaded.replace(seen);
            self.tiles.borrow_mut().clear();
            self.order.borrow_mut().clear();
            self.decoded.borrow_mut().clear();
            let srcs = open_sources(&geo);
            let names: Vec<String> = srcs.iter().map(|s| s.name.clone()).collect();
            self.status.set_text(&if srcs.is_empty() {
                omui::tr("No map in this library yet: add one in Library and downloads")
            } else {
                omui::tr!("© OpenStreetMap contributors · {}", names.join(", "))
            });
            if !self.placed.replace(true) {
                self.frame_to.set(srcs.iter().filter_map(|s| s.region).max_by_key(|r| r.1).map(|r| r.0));
            }
            self.sources.replace(srcs);
        }
        self.area.queue_draw();
    }

    pub fn show_at(self: &Rc<Self>, lat: f64, lon: f64, label: &str) {
        self.placed.set(true);
        self.show();
        self.center(lat, lon, 15.0, Some(label.to_owned()));
        if self.panel.is_visible() {
            self.nearest(false);
        }
    }

    /// Puts a point in the middle of what can be seen of the map: left of the panel, where it is beside the map.
    fn center(&self, lat: f64, lon: f64, zoom: f64, pin: Option<String>) {
        let (x, y) = lonlat_to_world(lon, lat);
        let zoom = zoom.clamp(0.0, MAX_ZOOM);
        self.zoom.set(zoom);
        self.cx.set((x + self.shift()).rem_euclid(1.0));
        self.cy.set(y);
        if let Some(p) = pin {
            self.pin.replace(Some((x, y, p)));
            self.chosen.set(None);
        }
        self.info.popdown();
        self.area.queue_draw();
    }

    /// Shows the box [w, s, e, n] whole, at the next frame (the map's size may not be known yet).
    fn frame(&self, bounds: [f64; 4]) {
        self.frame_to.set(Some(bounds));
        self.area.queue_draw();
    }

    /// Fits the box [w, s, e, n] into the part of a map `w` × `h` that can be seen: left of the panel, where it is beside it.
    fn fit(&self, [west, south, east, north]: [f64; 4], w: f64, h: f64) {
        let east = if east < west { east + 360.0 } else { east };   // across the antimeridian
        let ((x0, y0), (x1, y1)) = (lonlat_to_world(west, north), lonlat_to_world(east, south));
        let beside = if self.panel.is_visible() && self.panel.halign() == gtk::Align::End { f64::from(self.panel.width().max(300) + 12) } else { 0.0 };
        let scale = ((w - beside).max(100.0) / (x1 - x0).max(1e-9)).min(h / (y1 - y0).max(1e-9));
        let (lon, lat) = world_to_lonlat((x0 + x1) / 2.0, (y0 + y1) / 2.0);
        self.center(lat, lon, ((scale / TILE).log2() - 0.2).clamp(1.0, 16.0), None);
    }

    fn zoom_by(&self, dz: f64, at: Option<(f64, f64)>) {
        let (w, h) = (f64::from(self.area.width()), f64::from(self.area.height()));
        let old = self.zoom.get();
        let new = (old + dz).clamp(1.0, MAX_ZOOM);
        // From the keys and buttons: about the middle of what can be seen, left of the panel where it is beside the map.
        let at = at.or_else(|| (self.shift() > 0.0).then(|| (w / 2.0 - self.shift() * TILE * 2f64.powf(old), h / 2.0)));
        if let Some((px, py)) = at {
            // Keep the point under the pointer where it is.
            let (s0, s1) = (TILE * 2f64.powf(old), TILE * 2f64.powf(new));
            let wx = self.cx.get() + (px - w / 2.0) / s0;
            let wy = self.cy.get() + (py - h / 2.0) / s0;
            self.cx.set((wx - (px - w / 2.0) / s1).rem_euclid(1.0));
            self.cy.set((wy - (py - h / 2.0) / s1).clamp(0.0, 1.0));
        }
        self.zoom.set(new);
        self.info.popdown();
        self.area.queue_draw();
    }

    fn find(self: &Rc<Self>, q: &str) {
        let Some(app) = self.app.upgrade() else { return };
        if q.trim().is_empty() {
            return;
        }
        let (engine, me, q) = (app.engine.clone(), Rc::downgrade(self), q.to_owned());
        glib::spawn_future_local(async move {
            let r = engine.get(&query("/api/places", &[("q", &q)])).await;
            let Some(me) = me.upgrade() else { return };
            let list: Vec<Value> = r.ok().and_then(|v| v.as_array().cloned()).unwrap_or_default();
            omui::clear(&me.results);
            for p in &list {
                let row = gtk::Box::new(gtk::Orientation::Vertical, 2);
                row.set_margin_top(4);
                row.set_margin_bottom(4);
                let t = gtk::Label::new(p["name"].as_str());
                t.set_xalign(0.0);
                t.add_css_class("result-title");
                let d = gtk::Label::new(Some(&format!("{} · {}", p["kind"].as_str().unwrap_or_default(), p["detail"].as_str().unwrap_or_default())));
                d.set_xalign(0.0);
                d.add_css_class("meta");
                row.append(&t);
                row.append(&d);
                me.results.append(&row);
            }
            if list.is_empty() {
                let l = gtk::Label::new(Some(&omui::tr("No place by that name.")));
                l.add_css_class("dim-label");
                me.results.append(&l);
            }
            me.found.replace(list);
            me.results_pop.popup();
        });
    }

    // ------------------------------------------------------------------------------------------------- tiles --
    /// The decoded data for tile (z, x, y): its own, or the nearest coarser tile any source has.
    fn data(&self, z: u32, x: u32, y: u32) -> Option<(Layers, (u32, u32, u32))> {
        for dz in 0..=z {
            let (pz, px, py) = (z - dz, x >> dz, y >> dz);
            if let Some(l) = self.decoded.borrow().get(&(pz, px, py)) {
                return Some((l.clone(), (pz, px, py)));
            }
            let found = self.sources.borrow().iter().filter(|s| s.max_zoom >= pz).find_map(|s| s.tile(pz, px, py));
            if let Some(bytes) = found {
                let layers = Rc::new(mvt::decode(&bytes));
                let mut d = self.decoded.borrow_mut();
                if d.len() > 600 {
                    d.clear();
                }
                d.insert((pz, px, py), layers.clone());
                return Some((layers, (pz, px, py)));
            }
        }
        None
    }

    fn render_tile(&self, z: u32, x: u32, y: u32, px: i32) -> Option<Cached> {
        let (layers, from) = self.data(z, x, y)?;
        let surface = cairo::ImageSurface::create(cairo::Format::ARgb32, px, px).ok()?;
        let st = self.style.borrow().clone();
        {
            let cr = cairo::Context::new(&surface).ok()?;
            rgb(&cr, &st.water, 1.0);
            cr.paint().ok()?;
            // Scale the source tile so that tile (z, x, y) fills the image.
            let dz = z - from.0;
            let k = f64::from(1u32 << dz);
            let (ox, oy) = (f64::from(x - (from.1 << dz)), f64::from(y - (from.2 << dz)));
            let extent = layers.first().map(|l| f64::from(l.extent)).unwrap_or(4096.0);
            let s = f64::from(px) / extent * k;
            cr.translate(-ox * f64::from(px), -oy * f64::from(px));
            cr.scale(s, s);
            let zf = f64::from(z);
            let lw = 1.0 / s;   // one pixel in tile units
            draw_layers(&cr, &layers, &st, zf, lw);
        }
        Some(Cached { image: Some(surface), layers, from })
    }

    fn draw(&self, cr: &cairo::Context, w: f64, h: f64) {
        if let Some(b) = self.frame_to.take() {
            self.fit(b, w, h);
        }
        let st = self.style.borrow().clone();
        rgb(cr, &st.water, 1.0);
        let _ = cr.paint();
        if self.sources.borrow().is_empty() {
            return;
        }
        let zoom = self.zoom.get();
        let tz = zoom.round().clamp(0.0, 18.0) as u32;
        let n = 1u32 << tz;
        let world = TILE * 2f64.powf(zoom);
        let size = world / f64::from(n);   // a tile's size on screen
        let scale = self.area.scale_factor();
        let px = (size * f64::from(scale)).round().clamp(64.0, 1024.0) as i32;
        let (cx, cy) = (self.cx.get() * world, self.cy.get() * world);
        let x0 = ((cx - w / 2.0) / size).floor() as i64;
        let x1 = ((cx + w / 2.0) / size).floor() as i64;
        let y0 = (((cy - h / 2.0) / size).floor() as i64).max(0);
        let y1 = (((cy + h / 2.0) / size).floor() as i64).min(i64::from(n) - 1);
        let mut drawn = 0;
        let mut missing = false;
        let mut visible = Vec::new();
        for ty in y0..=y1 {
            for tx in x0..=x1 {
                let wx = tx.rem_euclid(i64::from(n)) as u32;
                let key = (tz, wx, ty as u32);
                visible.push((key, tx as f64 * size - cx + w / 2.0, ty as f64 * size - cy + h / 2.0));
                if !self.tiles.borrow().contains_key(&key) {
                    if drawn >= DRAW_PER_FRAME {
                        missing = true;
                        continue;
                    }
                    drawn += 1;
                    let c = self.render_tile(tz, wx, ty as u32, px).unwrap_or(Cached { image: None, layers: Rc::new(Vec::new()), from: key });
                    let mut tiles = self.tiles.borrow_mut();
                    tiles.insert(key, c);
                    let mut order = self.order.borrow_mut();
                    order.push_back(key);
                    while order.len() > 300 {
                        if let Some(old) = order.pop_front() {
                            tiles.remove(&old);
                        }
                    }
                }
            }
        }
        // Tiles, then labels over all of them.
        let tiles = self.tiles.borrow();
        for (key, sx, sy) in &visible {
            if let Some(Cached { image: Some(img), .. }) = tiles.get(key) {
                let _ = cr.save();
                cr.translate(*sx, *sy);
                let k = size / f64::from(img.width());
                cr.scale(k, k);
                let _ = cr.set_source_surface(img, 0.0, 0.0);
                cr.source().set_filter(cairo::Filter::Good);
                let _ = cr.paint();
                let _ = cr.restore();
            }
        }
        let mut taken: Vec<(f64, f64, f64, f64)> = Vec::new();
        let layout = pangocairo::functions::create_layout(cr);
        layout.set_font_description(Some(&label_font()));
        let mut marks = Vec::new();
        for pass in 0..2 {
            for (key, sx, sy) in &visible {
                let Some(c) = tiles.get(key) else { continue };
                labels(cr, &layout, c, *key, (*sx, *sy), size, zoom, &st, &mut taken, pass, &mut marks);
            }
        }
        self.marks.replace(marks);
        if let Some((wx, wy)) = self.chosen.get() {
            // Nearest the centre of the copies of the world side by side, as the tiles are.
            let sx = ((wx - self.cx.get() + 0.5).rem_euclid(1.0) - 0.5) * world + w / 2.0;
            rgb(cr, &st.text, 1.0);
            cr.set_line_width(2.0);
            cr.new_path();   // the pin's label leaves a current point, which the ring would be joined to
            cr.arc(sx, wy * world - cy + h / 2.0, 12.0, 0.0, 2.0 * PI);
            let _ = cr.stroke();
        }
        if let Some((px_, py_, label)) = self.pin.borrow().as_ref() {
            let (sx, sy) = (px_ * world - cx + w / 2.0, py_ * world - cy + h / 2.0);
            rgb(cr, &st.danger, 1.0);
            cr.arc(sx, sy - 14.0, 7.0, 0.0, 2.0 * PI);
            cr.move_to(sx - 6.0, sy - 11.0);
            cr.line_to(sx, sy);
            cr.line_to(sx + 6.0, sy - 11.0);
            let _ = cr.fill();
            text(cr, &layout, label, sx + 10.0, sy - 22.0, &st.text, &st.halo, false);
        }
        drop(tiles);
        if missing {
            let area = self.area.clone();
            glib::idle_add_local_once(move || area.queue_draw());
        }
        let (lon, lat) = world_to_lonlat(self.cx.get(), self.cy.get());
        let number = omui::i18n::format_number;
        self.area.set_tooltip_text(Some(&omui::tr!("{}, {} · zoom {}", number(lat, 4), number(lon, 4), number(zoom, 1))));
    }
}

fn path(cr: &cairo::Context, f: &Feature, closed: bool) {
    for part in &f.parts {
        let mut it = part.iter();
        if let Some(&(x, y)) = it.next() {
            cr.move_to(f64::from(x), f64::from(y));
            for &(x, y) in it {
                cr.line_to(f64::from(x), f64::from(y));
            }
            if closed {
                cr.close_path();
            }
        }
    }
}

fn layer<'a>(layers: &'a [Layer], name: &str) -> impl Iterator<Item = &'a Feature> {
    layers.iter().filter(move |l| l.name == name).flat_map(|l| l.features.iter())
}

fn visible_at(f: &Feature, z: f64) -> bool {
    f.num("min_zoom").is_none_or(|m| m <= z + 0.5)
}

fn draw_layers(cr: &cairo::Context, layers: &[Layer], st: &Style, z: f64, lw: f64) {
    cr.set_fill_rule(cairo::FillRule::EvenOdd);
    // Land, then what covers it.
    rgb(cr, &st.land, 1.0);
    for f in layer(layers, "earth").filter(|f| f.geom == Geom::Polygon) {
        path(cr, f, true);
    }
    let _ = cr.fill();
    for f in layer(layers, "landcover").chain(layer(layers, "landuse")).filter(|f| f.geom == Geom::Polygon && visible_at(f, z)) {
        let colour = match f.str("kind").unwrap_or_default() {
            "forest" | "wood" | "park" | "nature_reserve" | "national_park" | "protected_area" | "garden" | "golf_course" | "grass" | "grassland" | "scrub" | "meadow" | "cemetery" | "playground" => &st.park,
            "farmland" | "farm" | "orchard" | "vineyard" | "allotments" => &st.farm,
            "urban_area" | "residential" | "industrial" | "commercial" | "retail" | "hospital" | "school" | "university" | "college" | "railway" | "military" | "glacier" | "beach" | "sand" | "barren" | "bare_rock" => &st.urban,
            _ => continue,
        };
        rgb(cr, colour, 1.0);
        path(cr, f, true);
        let _ = cr.fill();
    }
    // Water: areas, then lines.
    rgb(cr, &st.water, 1.0);
    for f in layer(layers, "water").filter(|f| f.geom == Geom::Polygon) {
        path(cr, f, true);
    }
    let _ = cr.fill();
    for f in layer(layers, "water").filter(|f| f.geom == Geom::Line && visible_at(f, z)) {
        let w = if f.str("kind") == Some("river") { 2.2 } else { 1.0 } * (z / 12.0).clamp(0.4, 1.6);
        cr.set_line_width(w * lw);
        path(cr, f, false);
        let _ = cr.stroke();
    }
    // Boundaries.
    for f in layer(layers, "boundaries").filter(|f| f.geom == Geom::Line && visible_at(f, z)) {
        let country = f.str("kind") == Some("country") || f.num("kind_detail").is_some_and(|d| d <= 2.0);
        rgb(cr, &st.boundary, if country { 0.9 } else { 0.55 });
        cr.set_line_width((if country { 1.4 } else { 0.8 }) * lw);
        cr.set_dash(&[4.0 * lw, 3.0 * lw], 0.0);
        path(cr, f, false);
        let _ = cr.stroke();
    }
    cr.set_dash(&[], 0.0);
    // Buildings at street level.
    if z >= 14.0 {
        rgb(cr, &st.building, 1.0);
        for f in layer(layers, "buildings").filter(|f| f.geom == Geom::Polygon) {
            path(cr, f, true);
        }
        let _ = cr.fill();
    }
    // Rail, then roads: casings first, then fills, minor to major.
    rgb(cr, &st.rail, 1.0);
    cr.set_line_width(1.0 * lw);
    for f in layer(layers, "roads").chain(layer(layers, "transit")).filter(|f| f.geom == Geom::Line && f.str("kind") == Some("rail") && visible_at(f, z)) {
        path(cr, f, false);
    }
    let _ = cr.stroke();
    cr.set_line_cap(cairo::LineCap::Round);
    cr.set_line_join(cairo::LineJoin::Round);
    let width = |f: &Feature| -> Option<(f64, &String)> {
        let k = f.str("kind").unwrap_or_default();
        let base = match k {
            "highway" => 3.0,
            "major_road" => 2.2,
            "minor_road" => 1.3,
            "path" | "other" if z >= 15.0 => 0.7,
            _ => return None,
        };
        let grow = 2f64.powf(((z - 12.0) / 2.0).clamp(-2.0, 2.5));
        let colour = match k {
            "highway" => &st.highway,
            "major_road" => &st.major,
            _ => &st.minor,
        };
        Some((base * grow, colour))
    };
    let roads: Vec<&Feature> = layer(layers, "roads").filter(|f| f.geom == Geom::Line && visible_at(f, z)).collect();
    if z >= 12.0 {
        rgb(cr, &st.casing, 1.0);
        for f in &roads {
            if let Some((w, _)) = width(f) {
                cr.set_line_width((w + 1.5) * lw);
                path(cr, f, false);
                let _ = cr.stroke();
            }
        }
    }
    for kind in ["path", "other", "minor_road", "major_road", "highway"] {
        for f in roads.iter().filter(|f| f.str("kind") == Some(kind)) {
            if let Some((w, colour)) = width(f) {
                rgb(cr, colour, 1.0);
                cr.set_line_width(w * lw);
                path(cr, f, false);
                let _ = cr.stroke();
            }
        }
    }
}

/// A line of small print in the panel, as wide as the panel.
fn meta(text: &str) -> gtk::Label {
    let l = omui::label(text, "meta");
    l.set_max_width_chars(30);
    l
}

/// A help point's mark and colour (HELP), by its kind on the map or in the engine's answer.
fn help_kind(kind: &str) -> Option<(&'static str, bool)> {
    HELP.iter().find(|h| h.0.contains(&kind)).map(|h| (h.1, h.2))
}

/// What a help point is, in words: its own kind where the legend's heading covers several.
fn kind_name(kind: &str) -> String {
    match kind {
        "hospital" => omui::tr("Hospital"),
        "clinic" => omui::tr("Clinic"),
        "doctors" => omui::tr("Doctor"),
        "water_point" => omui::tr("Water point"),
        "supermarket" => omui::tr("Supermarket"),
        "marketplace" => omui::tr("Market"),
        _ => HELP.iter().find(|h| h.0.contains(&kind)).map(|h| omui::i18n::translate(h.3)).unwrap_or_default(),
    }
}

/// The engine's eight compass directions (haversine() in server.py), in words.
fn direction(d: &str) -> String {
    match d {
        "north" => omui::tr("north"),
        "north-east" => omui::tr("north-east"),
        "east" => omui::tr("east"),
        "south-east" => omui::tr("south-east"),
        "south" => omui::tr("south"),
        "south-west" => omui::tr("south-west"),
        "west" => omui::tr("west"),
        "north-west" => omui::tr("north-west"),
        _ => d.to_owned(),
    }
}

/// A distance the engine measured, in km: metres to the nearest ten under one kilometre.
fn distance(km: f64) -> String {
    let number = omui::i18n::format_number;
    if km < 1.0 {
        omui::tr!("{} m", number((km * 100.0).round() * 10.0, 0))
    } else {
        omui::tr!("{} km", number(km, if km < 10.0 { 1 } else { 0 }))
    }
}

/// The interface font, at the size labels need (the default would be a serif).
fn label_font() -> pango::FontDescription {
    pango::FontDescription::from_string(&format!("JetBrains Mono {}px", (11.0 * Theme::text_scale()).round()))
}

/// A help point's mark: a disc in its colour, with its letter in the ground's.
fn draw_mark(cr: &cairo::Context, layout: &pango::Layout, mark: &str, danger: bool, (x, y): (f64, f64), st: &Style) {
    rgb(cr, if danger { &st.danger } else { &st.accent }, 1.0);
    cr.arc(x, y, 8.0, 0.0, 2.0 * PI);
    let _ = cr.fill();
    layout.set_markup(&format!("<b><span size='small'>{}</span></b>", glib::markup_escape_text(mark)));
    let (tw, th) = layout.pixel_size();
    rgb(cr, &st.halo, 1.0);
    cr.move_to(x - f64::from(tw) / 2.0, y - f64::from(th) / 2.0);
    pangocairo::functions::show_layout(cr, layout);
    layout.set_attributes(None);   // the mark's bold must not carry into the next label
}

#[allow(clippy::too_many_arguments)]
fn labels(cr: &cairo::Context, layout: &pango::Layout, c: &Cached, key: (u32, u32, u32), (sx, sy): (f64, f64), size: f64, zoom: f64, st: &Style, taken: &mut Vec<(f64, f64, f64, f64)>, pass: u32, marks: &mut Vec<Mark>) {
    let (z, x, y) = key;
    let dz = z - c.from.0;
    let k = f64::from(1u32 << dz);
    let (ox, oy) = (f64::from(x - (c.from.1 << dz)), f64::from(y - (c.from.2 << dz)));
    let extent = c.layers.first().map(|l| f64::from(l.extent)).unwrap_or(4096.0);
    let to_screen = |px: f32, py: f32| -> (f64, f64) { (sx + (f64::from(px) / extent * k - ox) * size, sy + (f64::from(py) / extent * k - oy) * size) };
    let inside = |(a, b): (f64, f64)| a >= sx && a < sx + size && b >= sy && b < sy + size;
    if pass == 0 {
        // Places: countries and cities first (by rank), then smaller ones.
        let mut places: Vec<&Feature> = layer(&c.layers, "places").filter(|f| visible_at(f, zoom) && f.str("name").is_some()).collect();
        places.sort_by(|a, b| {
            let r = |f: &Feature| -(f.num("population_rank").unwrap_or(0.0)) + f.num("min_zoom").unwrap_or(10.0);
            r(a).partial_cmp(&r(b)).unwrap_or(std::cmp::Ordering::Equal)
        });
        for f in places {
            let Some(&(px, py)) = f.parts.first().and_then(|p| p.first()) else { continue };
            let pos = to_screen(px, py);
            if !inside(pos) {
                continue;
            }
            let kind = f.str("kind").unwrap_or_default();
            let big = matches!(kind, "country" | "region");
            let name = f.str("name").unwrap_or_default();
            if let Some(rect) = fit(layout, name, pos, big, taken) {
                taken.push(rect);
                if !big {
                    rgb(cr, &st.text, 0.9);
                    cr.arc(pos.0, pos.1, 2.2, 0.0, 2.0 * PI);
                    let _ = cr.fill();
                }
                text(cr, layout, name, rect.0, rect.1, if big { &st.dim } else { &st.text }, &st.halo, big);
            }
        }
    } else if zoom >= 13.0 {
        // Help points: hospitals, pharmacies, police, fire stations, water, shelters.
        for f in layer(&c.layers, "pois").filter(|f| f.geom == Geom::Point) {
            let kind = f.str("kind").unwrap_or_default();
            let Some((mark, danger)) = help_kind(kind) else { continue };
            let Some(&(px, py)) = f.parts.first().and_then(|p| p.first()) else { continue };
            let pos = to_screen(px, py);
            if !inside(pos) || taken.iter().any(|r| overlaps(r, &(pos.0 - 8.0, pos.1 - 8.0, 16.0, 16.0))) {
                continue;
            }
            draw_mark(cr, layout, mark, danger, pos, st);
            taken.push((pos.0 - 8.0, pos.1 - 8.0, 16.0, 16.0));
            let n = f64::from(1u32 << c.from.0);
            let world = ((f64::from(c.from.1) + f64::from(px) / extent) / n, (f64::from(c.from.2) + f64::from(py) / extent) / n);
            marks.push(Mark { x: pos.0, y: pos.1, kind: kind.to_owned(), name: f.str("name").unwrap_or_default().to_owned(), world });
            if zoom >= 15.0
                && let Some(name) = f.str("name")
                && let Some(rect) = fit(layout, name, (pos.0 + 6.0, pos.1), false, taken)
            {
                taken.push(rect);
                text(cr, layout, name, rect.0, rect.1, &st.text, &st.halo, false);
            }
        }
    }
}

fn overlaps(a: &(f64, f64, f64, f64), b: &(f64, f64, f64, f64)) -> bool {
    a.0 < b.0 + b.2 && b.0 < a.0 + a.2 && a.1 < b.1 + b.3 && b.1 < a.1 + a.3
}

/// Where a label for `name` at `pos` goes (to the right of the point, or centred for a country), if it overlaps nothing.
fn fit(layout: &pango::Layout, name: &str, pos: (f64, f64), centred: bool, taken: &[(f64, f64, f64, f64)]) -> Option<(f64, f64, f64, f64)> {
    layout.set_attributes(None);
    layout.set_text(name);
    let (tw, th) = layout.pixel_size();
    let (tw, th) = (f64::from(tw), f64::from(th));
    let rect = if centred { (pos.0 - tw / 2.0, pos.1 - th / 2.0, tw, th) } else { (pos.0 + 5.0, pos.1 - th / 2.0, tw, th) };
    (!taken.iter().any(|r| overlaps(r, &(rect.0 - 3.0, rect.1 - 2.0, rect.2 + 6.0, rect.3 + 4.0)))).then_some(rect)
}

#[allow(clippy::too_many_arguments)]
fn text(cr: &cairo::Context, layout: &pango::Layout, s: &str, x: f64, y: f64, colour: &str, halo: &str, caps: bool) {
    let shown = if caps { s.to_uppercase() } else { s.to_owned() };
    layout.set_attributes(None);
    layout.set_text(&shown);
    cr.move_to(x, y);
    pangocairo::functions::layout_path(cr, layout);
    rgb(cr, halo, 0.9);
    cr.set_line_width(3.0);
    cr.set_line_join(cairo::LineJoin::Round);
    let _ = cr.stroke();
    rgb(cr, colour, 1.0);
    cr.move_to(x, y);
    pangocairo::functions::show_layout(cr, layout);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn projection_round_trips() {
        for (lon, lat) in [(0.0, 0.0), (115.2, -8.65), (-122.4, 37.8), (139.7, 35.7)] {
            let (x, y) = lonlat_to_world(lon, lat);
            let (lo, la) = world_to_lonlat(x, y);
            assert!((lo - lon).abs() < 1e-9 && (la - lat).abs() < 1e-9, "{lon},{lat} -> {lo},{la}");
        }
        assert_eq!(lonlat_to_world(-180.0, 0.0).0, 0.0);
    }
}
