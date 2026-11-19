// SPDX-License-Identifier: MIT
//! The offline map of the world: OpenStreetMap vector tiles (Protomaps' basemap schema) in MBTiles files, drawn with
//! Cairo in the Omarchy theme's colours.
//!
//! derived/geo/world.mbtiles holds the whole world to zoom 8; derived/geo/regions-*.mbtiles add street-level detail for
//! the regions a person chose (tools/map_extract.py builds both). Each tile comes from the most detailed file that has
//! it, and a less detailed tile is scaled up where nothing finer exists. Tiles are drawn once into images (at screen
//! scale) and kept; labels for places and help points are placed each frame, without overlapping.

use std::{
    cell::{Cell, RefCell},
    collections::{HashMap, VecDeque},
    f64::consts::PI,
    path::{Path, PathBuf},
    rc::{Rc, Weak},
};

use gtk::{gdk, glib, prelude::*};
use serde_json::Value;

use crate::{
    engine::query,
    mvt::{self, Feature, Geom, Layer},
    theme::{Tokens, blend},
    ui::App,
};

const TILE: f64 = 256.0;
const MAX_ZOOM: f64 = 19.0;
const DRAW_PER_FRAME: usize = 4;

// --------------------------------------------------------------------------------------------------------- sources --
struct Source {
    db: rusqlite::Connection,
    name: String,
    max_zoom: u32,
}

impl Source {
    fn open(path: &Path) -> Option<Self> {
        let db = rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX).ok()?;
        let meta = |k: &str| db.query_row("SELECT value FROM metadata WHERE name=?1", [k], |r| r.get::<_, String>(0)).ok();
        let max_zoom = meta("maxzoom").and_then(|v| v.parse().ok()).unwrap_or(14);
        let name = meta("name").unwrap_or_else(|| path.file_stem().unwrap_or_default().to_string_lossy().into_owned());
        Some(Self { db, name, max_zoom })
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
    fn from(t: &Tokens) -> Self {
        let bg = &t.background;
        Self {
            water: blend(bg, &t.accent, if t.light { 0.22 } else { 0.18 }),
            land: bg.clone(),
            park: blend(bg, "#4caf50", 0.16),
            farm: blend(bg, "#9ccc65", 0.08),
            urban: blend(bg, &t.text, 0.04),
            building: blend(bg, &t.text, 0.13),
            minor: blend(bg, &t.text, 0.24),
            major: blend(bg, &t.text, 0.40),
            highway: blend(bg, &t.accent, 0.55),
            casing: blend(bg, if t.light { "#000000" } else { "#000000" }, 0.25),
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
    style_gen: Cell<u64>,
    /// Map centre in world units (0..1) and zoom.
    cx: Cell<f64>,
    cy: Cell<f64>,
    zoom: Cell<f64>,
    tiles: RefCell<HashMap<(u32, u32, u32), Cached>>,
    order: RefCell<VecDeque<(u32, u32, u32)>>,
    decoded: RefCell<HashMap<(u32, u32, u32), Rc<Vec<Layer>>>>,
    pin: RefCell<Option<(f64, f64, String)>>,
    drag_from: Cell<(f64, f64)>,
    status: gtk::Label,
    search: gtk::SearchEntry,
    results: gtk::ListBox,
    results_pop: gtk::Popover,
    found: RefCell<Vec<Value>>,
    loaded: Cell<bool>,
}

impl MapPage {
    pub fn build(app: &Rc<App>) -> Rc<Self> {
        let area = gtk::DrawingArea::new();
        area.set_hexpand(true);
        area.set_vexpand(true);
        area.set_focusable(true);
        area.add_css_class("map");
        let search = gtk::SearchEntry::new();
        search.set_placeholder_text(Some("Find a place, town or address"));
        search.set_width_chars(22);
        let results = gtk::ListBox::new();
        results.add_css_class("map-results");
        let results_pop = gtk::Popover::new();
        results_pop.set_child(Some(&gtk::ScrolledWindow::builder().child(&results).max_content_height(360).propagate_natural_height(true).min_content_width(360).build()));
        results_pop.set_parent(&search);
        results_pop.set_autohide(true);
        results_pop.set_has_arrow(false);
        let zoom_in = gtk::Button::from_icon_name("zoom-in-symbolic");
        let zoom_out = gtk::Button::from_icon_name("zoom-out-symbolic");
        for b in [&zoom_in, &zoom_out] {
            b.add_css_class("header-action");
            b.add_css_class("map-button");
        }
        zoom_in.set_tooltip_text(Some("Zoom in (+)"));
        zoom_out.set_tooltip_text(Some("Zoom out (-)"));
        let top = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        top.add_css_class("map-bar");
        top.set_halign(gtk::Align::Start);
        top.set_valign(gtk::Align::Start);
        top.set_margin_top(12);
        top.set_margin_start(12);
        top.append(&search);
        top.append(&zoom_out);
        top.append(&zoom_in);
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

        let page = Rc::new(Self {
            root: overlay.upcast(),
            app: Rc::downgrade(app),
            area,
            sources: RefCell::new(Vec::new()),
            style: RefCell::new(Style::from(&app.theme.tokens.borrow())),
            style_gen: Cell::new(0),
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
            loaded: Cell::new(false),
        });
        let me = Rc::downgrade(&page);
        page.area.set_draw_func(move |_, cr, w, h| {
            if let Some(me) = me.upgrade() {
                me.draw(cr, f64::from(w), f64::from(h));
            }
        });
        let me = Rc::downgrade(&page);
        app.theme.on_change(move |t| {
            if let Some(me) = me.upgrade() {
                me.style.replace(Style::from(t));
                me.style_gen.set(me.style_gen.get() + 1);
                me.tiles.borrow_mut().clear();
                me.area.queue_draw();
            }
        });
        page.controls(&zoom_in, &zoom_out);
        page
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
                    "city" | "town" | "locality" => 12.0,
                    _ => 14.0,
                };
                m.center(lat, lon, z, Some(p["name"].as_str().unwrap_or("Place").to_owned()));
            }
        });
    }

    /// Opens the map files the first time the map is shown.
    pub fn show(self: &Rc<Self>) {
        if !self.loaded.replace(true)
            && let Some(app) = self.app.upgrade()
            && let Some(data) = app.engine.ready.data.as_ref()
        {
            let srcs = open_sources(&data.join("derived").join("geo"));
            let names: Vec<String> = srcs.iter().map(|s| s.name.clone()).collect();
            self.status.set_text(&if srcs.is_empty() {
                "No map in this library yet: About → Library and downloads".to_owned()
            } else {
                format!("© OpenStreetMap contributors · {}", names.join(", "))
            });
            self.sources.replace(srcs);
        }
        self.area.queue_draw();
    }

    pub fn show_at(self: &Rc<Self>, lat: f64, lon: f64, label: &str) {
        self.show();
        self.center(lat, lon, 15.0, Some(label.to_owned()));
    }

    fn center(&self, lat: f64, lon: f64, zoom: f64, pin: Option<String>) {
        let (x, y) = lonlat_to_world(lon, lat);
        self.cx.set(x);
        self.cy.set(y);
        self.zoom.set(zoom.clamp(0.0, MAX_ZOOM));
        if let Some(p) = pin {
            self.pin.replace(Some((x, y, p)));
        }
        self.area.queue_draw();
    }

    fn zoom_by(&self, dz: f64, at: Option<(f64, f64)>) {
        let (w, h) = (f64::from(self.area.width()), f64::from(self.area.height()));
        let old = self.zoom.get();
        let new = (old + dz).clamp(1.0, MAX_ZOOM);
        if let Some((px, py)) = at {
            // Keep the point under the pointer where it is.
            let (s0, s1) = (TILE * 2f64.powf(old), TILE * 2f64.powf(new));
            let wx = self.cx.get() + (px - w / 2.0) / s0;
            let wy = self.cy.get() + (py - h / 2.0) / s0;
            self.cx.set((wx - (px - w / 2.0) / s1).rem_euclid(1.0));
            self.cy.set((wy - (py - h / 2.0) / s1).clamp(0.0, 1.0));
        }
        self.zoom.set(new);
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
            let list: Vec<Value> = r.ok().and_then(|v| v.as_array().cloned().or_else(|| v["results"].as_array().cloned())).unwrap_or_default();
            crate::cards::clear(&me.results);
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
                let l = gtk::Label::new(Some("No place by that name in the offline gazetteer."));
                l.add_css_class("dim");
                me.results.append(&l);
            }
            me.found.replace(list);
            me.results_pop.popup();
        });
    }

    // ------------------------------------------------------------------------------------------------- tiles --
    /// The decoded data for tile (z, x, y): its own, or the nearest coarser tile any source has.
    fn data(&self, z: u32, x: u32, y: u32) -> Option<(Rc<Vec<Layer>>, (u32, u32, u32))> {
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
        // The interface font, at the size labels need (the default would be a serif).
        let font = format!("JetBrains Mono {}px", (11.0 * crate::theme::Theme::text_scale()).round());
        layout.set_font_description(Some(&pango::FontDescription::from_string(&font)));
        for pass in 0..2 {
            for (key, sx, sy) in &visible {
                let Some(c) = tiles.get(key) else { continue };
                if c.from != *key && pass == 0 {
                    // a scaled-up tile's labels belong to its coarser zoom: draw them, but after the tile's own
                }
                labels(cr, &layout, c, *key, *sx, *sy, size, zoom, &st, &mut taken, pass);
            }
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
        self.area.set_tooltip_text(Some(&format!("{lat:.4}, {lon:.4} · zoom {zoom:.1}")));
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
            "urban_area" | "residential" | "industrial" | "commercial" | "retail" | "hospital" | "school" | "university" | "college" | "railway" | "military" => &st.urban,
            "glacier" | "beach" | "sand" | "barren" | "bare_rock" => &st.urban,
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

/// Help points worth showing on a survival map, and their mark.
fn help_kind(kind: &str) -> Option<&'static str> {
    Some(match kind {
        "hospital" | "clinic" | "doctors" => "H",
        "pharmacy" => "+",
        "police" => "P",
        "fire_station" => "F",
        "drinking_water" | "water_point" => "W",
        "shelter" => "S",
        "toilets" => "T",
        "supermarket" | "marketplace" => "M",
        _ => return None,
    })
}

#[allow(clippy::too_many_arguments)]
fn labels(cr: &cairo::Context, layout: &pango::Layout, c: &Cached, key: (u32, u32, u32), sx: f64, sy: f64, size: f64, zoom: f64, st: &Style, taken: &mut Vec<(f64, f64, f64, f64)>, pass: u32) {
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
            let Some(mark) = f.str("kind").and_then(help_kind) else { continue };
            let Some(&(px, py)) = f.parts.first().and_then(|p| p.first()) else { continue };
            let pos = to_screen(px, py);
            if !inside(pos) || taken.iter().any(|r| overlaps(r, &(pos.0 - 8.0, pos.1 - 8.0, 16.0, 16.0))) {
                continue;
            }
            let colour = if mark == "H" || mark == "+" || mark == "F" { &st.danger } else { &st.accent };
            rgb(cr, colour, 1.0);
            cr.arc(pos.0, pos.1, 8.0, 0.0, 2.0 * PI);
            let _ = cr.fill();
            layout.set_markup(&format!("<b><span size='small'>{mark}</span></b>"));
            let (tw, th) = layout.pixel_size();
            rgb(cr, &st.halo, 1.0);
            cr.move_to(pos.0 - f64::from(tw) / 2.0, pos.1 - f64::from(th) / 2.0);
            pangocairo::functions::show_layout(cr, layout);
            layout.set_attributes(None);   // the mark's bold must not carry into the next label
            taken.push((pos.0 - 8.0, pos.1 - 8.0, 16.0, 16.0));
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
