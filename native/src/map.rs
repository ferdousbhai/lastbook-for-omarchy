// SPDX-License-Identifier: MIT
//! The offline map. Phase 5 replaces this with the native global map; until then it says so.

use std::rc::{Rc, Weak};

use gtk::prelude::*;

use crate::ui::App;

pub struct MapPage {
    pub root: gtk::Widget,
    _app: Weak<App>,
    note: gtk::Label,
}

impl MapPage {
    pub fn build(app: &Rc<App>) -> Rc<Self> {
        let b = gtk::Box::new(gtk::Orientation::Vertical, 12);
        b.set_valign(gtk::Align::Center);
        b.set_halign(gtk::Align::Center);
        let img = gtk::Image::from_icon_name("mark-location-symbolic");
        img.set_pixel_size(48);
        let t = gtk::Label::new(Some("Map"));
        t.add_css_class("page-title");
        let note = gtk::Label::new(Some("The offline map is being rebuilt to cover the whole world."));
        note.add_css_class("empty-state");
        b.append(&img);
        b.append(&t);
        b.append(&note);
        Rc::new(Self { root: b.upcast(), _app: Rc::downgrade(app), note })
    }

    pub fn show(&self) {}

    pub fn show_at(&self, lat: f64, lon: f64, label: &str) {
        self.note.set_text(&format!("{label}: {lat:.4}, {lon:.4}"));
    }
}
