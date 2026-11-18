'use strict';
/* Runs in <head>, before the first paint: the classes on <html> that pick the look. No page content is touched here.
   plat-desktop: the one look, in the app's GTK window (which has its own title bar) and in a browser over the server version.
   side-off: the sidebar was hidden. inactive: the window is not in front. scrolled: the page is not at its top. */
(() => {
  const c = document.documentElement.classList;
  c.add('plat-desktop');
  try { if (localStorage.getItem('sg.side-off') === '1') c.add('side-off') } catch { }
  const front = () => c.toggle('inactive', !document.hasFocus());
  addEventListener('blur', () => setTimeout(front, 0));   // later: focus moving into a frame of this page is still this window
  addEventListener('focus', front);
  front();   // also now: a window that opens behind another app starts inactive, until it is brought in front
  addEventListener('scroll', () => c.toggle('scrolled', scrollY > 2), { passive: true });
})();
