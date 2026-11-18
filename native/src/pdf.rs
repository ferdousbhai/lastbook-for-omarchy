// SPDX-License-Identifier: MIT
//! PDF pages drawn by Poppler into textures, the way Strata previews PDFs. Documents are opened once and kept for the
//! session (small: the library has a few hundred), on the main thread, which is the only one Poppler objects live on.

use std::{cell::RefCell, collections::HashMap, path::Path};

use gtk::{gdk, glib};

thread_local! {
    static DOCS: RefCell<HashMap<String, poppler::Document>> = RefCell::new(HashMap::new());
}

pub fn document(path: &Path) -> Option<poppler::Document> {
    let key = path.to_string_lossy().into_owned();
    if let Some(d) = DOCS.with(|d| d.borrow().get(&key).cloned()) {
        return Some(d);
    }
    let uri = glib::filename_to_uri(path, None).ok()?;
    let doc = poppler::Document::from_file(&uri, None).ok()?;
    DOCS.with(|d| d.borrow_mut().insert(key, doc.clone()));
    Some(doc)
}

/// Page `index` (0-based) as a texture `width` device pixels wide, on white paper, and its size in points.
pub fn render(doc: &poppler::Document, index: i32, width: i32) -> Option<(gdk::MemoryTexture, (f64, f64))> {
    let page = doc.page(index)?;
    let (pw, ph) = page.size();
    if pw <= 0.0 || ph <= 0.0 || width <= 0 {
        return None;
    }
    let scale = f64::from(width) / pw;
    let height = (ph * scale).round().max(1.0) as i32;
    let mut surface = cairo::ImageSurface::create(cairo::Format::ARgb32, width, height).ok()?;
    {
        let cr = cairo::Context::new(&surface).ok()?;
        cr.set_source_rgb(1.0, 1.0, 1.0);
        cr.paint().ok()?;
        cr.scale(scale, scale);
        page.render(&cr);
    }
    surface.flush();
    let stride = surface.stride() as usize;
    let data = surface.data().ok()?.to_vec();
    let texture = gdk::MemoryTexture::new(
        width,
        height,
        gdk::MemoryFormat::B8g8r8a8Premultiplied,
        &glib::Bytes::from_owned(data),
        stride,
    );
    Some((texture, (pw, ph)))
}
