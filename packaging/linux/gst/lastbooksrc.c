/* lastbooksrc: a GStreamer source for lastbook://app/videos/... and lastbook://app/videos-external/... in the Linux app.

   WebKitGTK hands a <video> address that is not http(s) or blob: to GStreamer's playbin as it is, and playbin needs an element that
   claims the address's protocol. Nothing claims lastbook:, so without this every video fails at once (the page says it cannot be played).
   This element reads the video straight from the library folder: no port, no copy in memory, seeking by position.

   Only the app's web process finds it: shell_linux.py puts /usr/lib/lastbook-for-omarchy/gst on GST_PLUGIN_PATH_1_0 for itself and its children, names lastbook
   in WEBKIT_GST_ALLOWED_URI_PROTOCOLS (WebKit's player refuses other schemes before GStreamer sees them), and the library folder is in
   LASTBOOK_LIBRARY (engine/paths.py sets it). The file must be called libgstlastbooksrc.so: GStreamer finds the plugin's entry by its name.
   It opens nothing else:
     - host "app", path under /videos/ or /videos-external/ only, after percent-decoding
     - no "." or ".." segment, no empty segment, no NUL or backslash
     - a regular file, opened read-only
   videos-external may itself be a link to another disk, as the engine allows (engine/library.py SERVED_FOLDERS).
   The page's own policy decides what may ask for a video at all: the app's page, under media-src 'self'. */
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include <gst/gst.h>
#include <gst/base/gstbasesrc.h>

#define PACKAGE "lastbook"
#define VERSION "1.0"

typedef struct {
  GstBaseSrc parent;
  gchar *uri;
  gint fd;
  guint64 size;
} LastbookSrc;

typedef struct {
  GstBaseSrcClass parent_class;
} LastbookSrcClass;

static void lastbook_src_uri_handler_init (gpointer iface, gpointer data);

G_DEFINE_TYPE_WITH_CODE (LastbookSrc, lastbook_src, GST_TYPE_BASE_SRC,
    G_IMPLEMENT_INTERFACE (GST_TYPE_URI_HANDLER, lastbook_src_uri_handler_init));

static GstStaticPadTemplate src_template = GST_STATIC_PAD_TEMPLATE ("src", GST_PAD_SRC, GST_PAD_ALWAYS, GST_STATIC_CAPS_ANY);

enum { PROP_0, PROP_LOCATION };

/* The library file for a lastbook:// address, or NULL if it is not one this element may open. */
static gchar *
lastbook_src_path (const gchar * uri)
{
  const gchar *lib = g_getenv ("LASTBOOK_LIBRARY");
  const gchar *prefix = "lastbook://app/";
  if (lib == NULL || *lib != '/' || uri == NULL || !g_str_has_prefix (uri, prefix))
    return NULL;
  const gchar *rest = uri + strlen (prefix);
  gsize n = strcspn (rest, "?#");          /* the player may add a fragment such as #t=30 */
  gchar *raw = g_strndup (rest, n);
  gchar *rel = g_uri_unescape_string (raw, NULL);
  g_free (raw);
  if (rel == NULL)
    return NULL;
  gboolean ok = (g_str_has_prefix (rel, "videos/") || g_str_has_prefix (rel, "videos-external/"))
      && strchr (rel, '\\') == NULL;
  if (ok) {
    gchar **parts = g_strsplit (rel, "/", -1);
    for (gchar ** p = parts; *p != NULL; p++)
      if (**p == '\0' || strcmp (*p, ".") == 0 || strcmp (*p, "..") == 0)
        ok = FALSE;
    g_strfreev (parts);
  }
  gchar *path = ok ? g_build_filename (lib, rel, NULL) : NULL;
  g_free (rel);
  return path;
}

static gboolean
lastbook_src_start (GstBaseSrc * base)
{
  LastbookSrc *self = (LastbookSrc *) base;
  gchar *path = lastbook_src_path (self->uri);
  struct stat st;
  if (path == NULL) {
    GST_ELEMENT_ERROR (self, RESOURCE, NOT_FOUND, ("Lastbook does not open this address."), (NULL));
    return FALSE;
  }
  self->fd = open (path, O_RDONLY | O_CLOEXEC);
  g_free (path);
  if (self->fd < 0 || fstat (self->fd, &st) != 0 || !S_ISREG (st.st_mode)) {
    if (self->fd >= 0)
      close (self->fd);
    self->fd = -1;
    GST_ELEMENT_ERROR (self, RESOURCE, OPEN_READ, ("This video file could not be opened."), (NULL));
    return FALSE;
  }
  self->size = (guint64) st.st_size;
  return TRUE;
}

static gboolean
lastbook_src_stop (GstBaseSrc * base)
{
  LastbookSrc *self = (LastbookSrc *) base;
  if (self->fd >= 0)
    close (self->fd);
  self->fd = -1;
  return TRUE;
}

static gboolean
lastbook_src_get_size (GstBaseSrc * base, guint64 * size)
{
  LastbookSrc *self = (LastbookSrc *) base;
  if (self->fd < 0)
    return FALSE;
  *size = self->size;
  return TRUE;
}

static gboolean
lastbook_src_is_seekable (GstBaseSrc * base)
{
  return TRUE;
}

static GstFlowReturn
lastbook_src_fill (GstBaseSrc * base, guint64 offset, guint length, GstBuffer * buf)
{
  LastbookSrc *self = (LastbookSrc *) base;
  GstMapInfo map;
  if (offset >= self->size)
    return GST_FLOW_EOS;
  if (!gst_buffer_map (buf, &map, GST_MAP_WRITE))
    return GST_FLOW_ERROR;
  gsize want = MIN ((guint64) length, self->size - offset), got = 0;
  while (got < want) {
    ssize_t r = pread (self->fd, map.data + got, want - got, (off_t) (offset + got));
    if (r < 0 && errno == EINTR)
      continue;
    if (r <= 0)
      break;
    got += (gsize) r;
  }
  gst_buffer_unmap (buf, &map);
  if (got == 0) {
    GST_ELEMENT_ERROR (self, RESOURCE, READ, ("This video file could not be read."), (NULL));
    return GST_FLOW_ERROR;
  }
  gst_buffer_set_size (buf, got);
  GST_BUFFER_OFFSET (buf) = offset;
  GST_BUFFER_OFFSET_END (buf) = offset + got;
  return GST_FLOW_OK;
}

static void
lastbook_src_set_property (GObject * object, guint id, const GValue * value, GParamSpec * pspec)
{
  LastbookSrc *self = (LastbookSrc *) object;
  if (id == PROP_LOCATION) {
    g_free (self->uri);
    self->uri = g_value_dup_string (value);
  } else
    G_OBJECT_WARN_INVALID_PROPERTY_ID (object, id, pspec);
}

static void
lastbook_src_get_property (GObject * object, guint id, GValue * value, GParamSpec * pspec)
{
  LastbookSrc *self = (LastbookSrc *) object;
  if (id == PROP_LOCATION)
    g_value_set_string (value, self->uri);
  else
    G_OBJECT_WARN_INVALID_PROPERTY_ID (object, id, pspec);
}

static void
lastbook_src_finalize (GObject * object)
{
  LastbookSrc *self = (LastbookSrc *) object;
  g_free (self->uri);
  if (self->fd >= 0)
    close (self->fd);
  G_OBJECT_CLASS (lastbook_src_parent_class)->finalize (object);
}

static void
lastbook_src_class_init (LastbookSrcClass * klass)
{
  GObjectClass *gobject_class = G_OBJECT_CLASS (klass);
  GstElementClass *element_class = GST_ELEMENT_CLASS (klass);
  GstBaseSrcClass *base_class = GST_BASE_SRC_CLASS (klass);
  gobject_class->set_property = lastbook_src_set_property;
  gobject_class->get_property = lastbook_src_get_property;
  gobject_class->finalize = lastbook_src_finalize;
  g_object_class_install_property (gobject_class, PROP_LOCATION,
      g_param_spec_string ("location", "Location", "lastbook:// address of a library video", NULL, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
  gst_element_class_add_static_pad_template (element_class, &src_template);
  gst_element_class_set_static_metadata (element_class, "Lastbook library video source", "Source/File",
      "Reads lastbook://app/videos/ files from the Lastbook library folder", "Lastbook");
  base_class->start = lastbook_src_start;
  base_class->stop = lastbook_src_stop;
  base_class->get_size = lastbook_src_get_size;
  base_class->is_seekable = lastbook_src_is_seekable;
  base_class->fill = lastbook_src_fill;
}

static void
lastbook_src_init (LastbookSrc * self)
{
  self->fd = -1;
  gst_base_src_set_format (GST_BASE_SRC (self), GST_FORMAT_BYTES);
  gst_base_src_set_blocksize (GST_BASE_SRC (self), 256 * 1024);
}

static GstURIType
lastbook_src_uri_get_type (GType type)
{
  return GST_URI_SRC;
}

static const gchar *const *
lastbook_src_uri_get_protocols (GType type)
{
  static const gchar *protocols[] = { "lastbook", NULL };
  return protocols;
}

static gchar *
lastbook_src_uri_get_uri (GstURIHandler * handler)
{
  return g_strdup (((LastbookSrc *) handler)->uri);
}

static gboolean
lastbook_src_uri_set_uri (GstURIHandler * handler, const gchar * uri, GError ** error)
{
  gchar *path = lastbook_src_path (uri);
  if (path == NULL) {
    g_set_error (error, GST_URI_ERROR, GST_URI_ERROR_BAD_URI, "Lastbook does not open this address");
    return FALSE;
  }
  g_free (path);
  g_object_set (handler, "location", uri, NULL);
  return TRUE;
}

static void
lastbook_src_uri_handler_init (gpointer iface, gpointer data)
{
  GstURIHandlerInterface *i = (GstURIHandlerInterface *) iface;
  i->get_type = lastbook_src_uri_get_type;
  i->get_protocols = lastbook_src_uri_get_protocols;
  i->get_uri = lastbook_src_uri_get_uri;
  i->set_uri = lastbook_src_uri_set_uri;
}

static gboolean
plugin_init (GstPlugin * plugin)
{
  return gst_element_register (plugin, "lastbooksrc", GST_RANK_PRIMARY, lastbook_src_get_type ());
}

GST_PLUGIN_DEFINE (GST_VERSION_MAJOR, GST_VERSION_MINOR, lastbooksrc, "Lastbook library video source", plugin_init, VERSION, "MIT", "Lastbook",
    "https://lastbook.org")
