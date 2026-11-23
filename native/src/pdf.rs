// SPDX-License-Identifier: MIT
//! PDF pages drawn by Poppler into textures.
//!
//! A page can take many seconds to draw (one with large photographs is decoded in full at any size), so pages are
//! drawn off the main thread and the window stays answering meanwhile: the reader's on a thread of their own
//! (`draw_later`), the search results' thumbnails on another (`thumb_later`), so a list of thumbnails never keeps the
//! reader's page waiting for more than the one being drawn. Poppler's objects stay on the thread that opened them: the
//! main thread keeps the documents it opens for the session (small: the library has a few hundred) for their page
//! counts, sizes and text, and each drawing thread opens its own. Only one page is drawn at a time, whichever thread
//! asks (`DRAWING`): Poppler's Cairo output shares its font library between documents.

use std::{
    cell::RefCell,
    collections::HashMap,
    path::{Path, PathBuf},
    sync::{Condvar, Mutex, Once, OnceLock},
};

use gtk::{gdk, glib};

thread_local! {
    static DOCS: RefCell<HashMap<String, poppler::Document>> = RefCell::new(HashMap::new());
}

/// Held while Poppler draws.
static DRAWING: Mutex<()> = Mutex::new(());

/// The document at `path`, opened once on the thread that asks.
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

/// A drawn page as plain bytes, which may cross threads; the main thread makes a texture of it.
pub struct Drawn {
    width: i32,
    height: i32,
    stride: usize,
    data: Vec<u8>,
}

impl Drawn {
    /// What the picture holds in memory.
    pub fn bytes(&self) -> usize {
        self.data.len()
    }

    pub fn texture(self) -> gdk::MemoryTexture {
        gdk::MemoryTexture::new(self.width, self.height, gdk::MemoryFormat::B8g8r8a8Premultiplied, &glib::Bytes::from_owned(self.data), self.stride)
    }
}

/// The size of page `index` (0-based) in points, if it has one to draw at.
pub fn page_size(doc: &poppler::Document, index: i32) -> Option<(f64, f64)> {
    let (w, h) = doc.page(index)?.size();
    (w > 0.0 && h > 0.0).then_some((w, h))
}

fn draw(doc: &poppler::Document, index: i32, width: i32) -> Option<Drawn> {
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
        let _one = DRAWING.lock().unwrap_or_else(|e| e.into_inner());
        page.render(&cr);
    }
    surface.flush();
    let stride = surface.stride() as usize;
    let data = surface.data().ok()?.to_vec();
    Some(Drawn { width, height, stride, data })
}

/// One thing to do next, where only the newest asked for matters: putting another in drops the one still waiting.
struct Latest<T> {
    next: Mutex<Option<T>>,
    wake: Condvar,
}

impl<T> Latest<T> {
    const fn new() -> Self {
        Self { next: Mutex::new(None), wake: Condvar::new() }
    }

    fn put(&self, job: T) {
        *self.next.lock().unwrap_or_else(|e| e.into_inner()) = Some(job);
        self.wake.notify_one();
    }

    /// Waits for the next one.
    fn take(&self) -> T {
        let mut next = self.next.lock().unwrap_or_else(|e| e.into_inner());
        loop {
            match next.take() {
                Some(job) => return job,
                None => next = self.wake.wait(next).unwrap_or_else(|e| e.into_inner()),
            }
        }
    }
}

struct Job {
    file: PathBuf,
    index: i32,
    width: i32,
    answer: async_channel::Sender<Option<Drawn>>,
}

/// A thread that draws pages, started when first asked, and the one page it is to draw next.
struct Drawer {
    name: &'static str,
    jobs: Latest<Job>,
    started: Once,
    /// Why the thread could not be started, if it could not.
    failed: OnceLock<String>,
}

impl Drawer {
    const fn new(name: &'static str) -> Self {
        Self { name, jobs: Latest::new(), started: Once::new(), failed: OnceLock::new() }
    }

    fn ask(&'static self, file: &Path, index: i32, width: i32) -> async_channel::Receiver<Option<Drawn>> {
        let (answer, rx) = async_channel::bounded(1);
        self.started.call_once(|| {
            let spawned = std::thread::Builder::new().name(self.name.into()).spawn(|| {
                loop {
                    let job = self.jobs.take();
                    let drawn = document(&job.file).and_then(|doc| draw(&doc, job.index, job.width));
                    let _ = job.answer.send_blocking(drawn);
                }
            });
            if let Err(e) = spawned {
                let _ = self.failed.set(format!("Lastbook cannot draw PDF pages: this computer would not start another thread ({e})."));
            }
        });
        let job = Job { file: file.to_owned(), index, width, answer };
        match self.failed.get() {
            Some(_) => refuse(job),
            None => self.jobs.put(job),
        }
        rx
    }
}

static PAGES: Drawer = Drawer::new("lastbook-pdf");
static THUMBS: Drawer = Drawer::new("lastbook-thumbs");

/// What stops every page from being drawn, in a sentence for the reader to show; None while pages can be drawn.
pub fn drawer_error() -> Option<&'static str> {
    PAGES.failed.get().map(String::as_str)
}

/// Answers a job nothing will draw, so the reader says so instead of waiting for a page that never comes.
fn refuse(job: Job) {
    let _ = job.answer.try_send(None);
}

/// Has page `index` (0-based) of the PDF at `file` drawn `width` device pixels wide off the main thread. The answer
/// comes on the channel: the page, or None if it cannot be drawn. The channel closes with no answer when a later call
/// came before this page's turn: the reader shows one page, so only the newest asked for is drawn. A page already
/// being drawn is finished first (Poppler cannot be stopped), and its answer is still sent.
pub fn draw_later(file: &Path, index: i32, width: i32) -> async_channel::Receiver<Option<Drawn>> {
    PAGES.ask(file, index, width)
}

/// The same for a thumbnail, on a thread of its own, answered as `draw_later` answers. Search asks for the next
/// thumbnail once the one before has come, so none is dropped unless the results it was for are gone.
pub fn thumb_later(file: &Path, index: i32, width: i32) -> async_channel::Receiver<Option<Drawn>> {
    THUMBS.ask(file, index, width)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_newest_job_is_kept() {
        let jobs = Latest::new();
        jobs.put(1);
        jobs.put(2);
        jobs.put(3);
        assert_eq!(jobs.take(), 3);
        assert!(jobs.next.lock().unwrap().is_none());
    }

    #[test]
    fn a_job_put_later_wakes_the_one_waiting() {
        static JOBS: Latest<&str> = Latest::new();
        let waiting = std::thread::spawn(|| JOBS.take());
        std::thread::sleep(std::time::Duration::from_millis(30));
        JOBS.put("page");
        assert_eq!(waiting.join().unwrap(), "page");
    }

    #[test]
    fn a_job_dropped_unanswered_closes_its_channel() {
        let (answer, rx) = async_channel::bounded::<Option<Drawn>>(1);
        let jobs = Latest::new();
        jobs.put(Job { file: PathBuf::from("a.pdf"), index: 0, width: 100, answer });
        let (answer, newer) = async_channel::bounded::<Option<Drawn>>(1);
        jobs.put(Job { file: PathBuf::from("a.pdf"), index: 1, width: 100, answer });
        assert!(rx.recv_blocking().is_err());   // replaced before its turn
        assert_eq!(jobs.take().index, 1);
        assert!(newer.recv_blocking().is_err());   // taken and dropped here, as nothing drew it
    }

    #[test]
    fn a_job_nothing_will_draw_is_answered_at_once() {
        let (answer, rx) = async_channel::bounded::<Option<Drawn>>(1);
        refuse(Job { file: PathBuf::from("a.pdf"), index: 0, width: 100, answer });
        assert!(matches!(rx.recv_blocking(), Ok(None)));   // the reader says the page could not be drawn
    }
}
