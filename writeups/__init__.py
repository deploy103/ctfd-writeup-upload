"""Writeup upload plugin for CTFd 3.8.

Participants can upload up to MAX_FILES writeup files each and see/delete their own;
only admins can list (grouped by user) and download them.
Auth, CSRF and rate limiting reuse CTFd's own mechanisms. Files are stored under
random names outside CTFd's `Files` table, so `/files/<path>` can never serve them.
"""

import hashlib
import io
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_babel import gettext, lazy_gettext
from sqlalchemy import func, inspect, text
from werkzeug.utils import secure_filename

from CTFd.cache import cache
from CTFd.models import Users, db
from CTFd.plugins import register_user_page_menu_bar
from CTFd.utils.decorators import admins_only, authed_only, require_verified_emails
from CTFd.utils.user import get_current_user

MAX_BYTES = 20 * 1024 * 1024
ALLOWED = {".pdf", ".zip", ".md", ".txt"}
MAX_NAME = 255
SHOW_NAV_LINK = True  # add a "Writeups" link to the navbar of themes that list plugin pages (the default theme does)
MAX_USER_TOTAL = (
    200 * 1024 * 1024
)  # per participant, so one account cannot fill the store
MAX_FILES = 100  # files per participant
MAX_TOTAL = 10 * 1024 * 1024 * 1024  # soft cap across all writeups
MIN_FREE_BYTES = 1024 * 1024 * 1024  # keep this much disk free for the DB and CTFd
USER_LIMIT = 30  # uploads per user per minute
STORED_RE = re.compile(r"[0-9a-f]{32}")
UPLOAD_PATH = "/writeups"
UPLOAD_LIMIT = MAX_BYTES + 64 * 1024  # file + multipart overhead
FORM_LIMIT = 64 * 1024  # every other write under our prefixes is a tiny form


class Writeups(db.Model):
    __tablename__ = "writeups"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    original_name = db.Column(db.String(MAX_NAME), nullable=False)
    ext = db.Column(db.String(8), nullable=False)
    stored_name = db.Column(db.String(64), nullable=False, unique=True)
    size = db.Column(db.Integer, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    created = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    updated = db.Column(
        db.DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    user = db.relationship("Users", foreign_keys="Writeups.user_id", lazy="select")


def _drop_user_unique(engine):
    """One-time upgrade of installs that still have UNIQUE(user_id) (one file per user)."""
    insp = inspect(engine)
    if not insp.has_table("writeups"):
        return
    names = [
        ix["name"]
        for ix in insp.get_indexes("writeups")
        if ix.get("unique") and ix["column_names"] == ["user_id"]
    ]
    names += [
        uc["name"]
        for uc in insp.get_unique_constraints("writeups")
        if uc["column_names"] == ["user_id"] and uc.get("name")
    ]
    if not names or engine.dialect.name not in ("mysql", "mariadb"):
        return  # fresh tables are created from the model; SQLite is only used in tests
    # Add the plain index first (the foreign key needs one), then drop the unique key,
    # in a single statement so the FK is never left without an index.
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE writeups ADD INDEX ix_writeups_user_id (user_id), "
                + ", ".join("DROP INDEX `%s`" % n.replace("`", "") for n in set(names))
            )
        )


def _store_dir():
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "writeups")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _path(stored_name):
    # stored_name is always 32 hex chars generated here; refuse anything else.
    if not STORED_RE.fullmatch(stored_name):
        abort(404)
    return os.path.join(_store_dir(), stored_name)


def _content_ok(ext, data):
    if ext == ".pdf":
        return data.startswith(b"%PDF-")
    if ext == ".zip":
        return data.startswith(b"PK\x03\x04") and zipfile.is_zipfile(io.BytesIO(data))
    try:  # .md / .txt
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return b"\x00" not in data


def _user_slug(w):
    return "{}_{}".format(
        w.user_id, (secure_filename(w.user.name) if w.user else "") or "user"
    )


def _when(w):
    """Upload time for display; falls back to `updated` for rows without `created`."""
    ts = w.created or w.updated
    return ts.strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def _download_name(w):
    return "writeup_{}_{}{}".format(_user_slug(w), w.id, w.ext)


def _zip_names(rows):
    """Flat-per-user, ASCII-only archive names: <id>_<user>/<seq>_<file><ext>."""
    seq = {}
    for w in rows:
        seq[w.user_id] = seq.get(w.user_id, 0) + 1
        stem = secure_filename(os.path.splitext(w.original_name)[0])[:60] or "file"
        yield "{}/{:03d}_{}{}".format(_user_slug(w), seq[w.user_id], stem, w.ext)


def _harden(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


def _attachment(path, name):
    return _harden(
        send_file(
            path,
            as_attachment=True,
            download_name=name,
            mimetype="application/octet-stream",
        )
    )


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _archive_link(path):
    """Hard-link a stored file under a temporary name so its inode stays stable.

    A participant replacing or an admin deleting the file while a download-all is
    running only unlinks the original name; the archive keeps reading the old inode.
    No data is copied, and only one file is open at a time while streaming.
    """
    fd, link = tempfile.mkstemp(dir=_store_dir(), prefix=".archive-")
    try:
        os.close(fd)
        os.remove(link)
        os.link(path, link)
    except BaseException:
        _remove(link)
        raise
    return link


class _ZipSink:
    """Write-only, non-seekable target so ZipFile streams instead of buffering."""

    def __init__(self):
        self.buf, self.pos = bytearray(), 0

    def write(self, data):
        self.buf += data
        self.pos += len(data)
        return len(data)

    def tell(self):
        return self.pos

    def flush(self):
        pass

    def drain(self):
        out = bytes(self.buf)
        self.buf.clear()
        return out


def _stream_zip(entries):
    """Yield a ZIP of (arcname, hard-link path) entries holding one chunk in memory."""
    sink = _ZipSink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as z:
        for arcname, path in entries:
            info = zipfile.ZipInfo(arcname)
            info.external_attr = 0o600 << 16
            with open(path, "rb") as src, z.open(info, "w") as dst:
                while chunk := src.read(64 * 1024):
                    dst.write(chunk)
                    yield sink.drain()
        # leaving the `with` writes the central directory
    yield sink.drain()


class _BodyLimit:
    """Reject oversized or length-less writes before CTFd's CSRF hook parses them.

    CTFd reads `request.form` in a global before_request hook, which parses the whole
    multipart body even for anonymous requests, so the size check in the view comes
    too late. This runs ahead of Flask and also covers requests that bypass nginx.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        if environ.get("REQUEST_METHOD") in ("POST", "PUT", "PATCH", "DELETE"):
            # Werkzeug routes "//writeups" (and, after gunicorn decodes it, "/%2Fwriteups")
            # to "/writeups" without a redirect, so compare the path the same way.
            path = "/" + environ.get("PATH_INFO", "").lstrip("/")
            limit = None
            if path == UPLOAD_PATH:
                limit = UPLOAD_LIMIT
            elif path.startswith(UPLOAD_PATH + "/") or path.startswith(
                "/admin/writeups"
            ):
                limit = FORM_LIMIT
            if limit is not None:
                raw = environ.get("CONTENT_LENGTH") or ""
                if not raw.isdigit():
                    return self._reject(start_response, "411 Length Required")
                if int(raw) > limit:
                    return self._reject(start_response, "413 Payload Too Large")
        return self.wsgi_app(environ, start_response)

    @staticmethod
    def _reject(start_response, status):
        body = status.encode()
        start_response(
            status,
            [
                ("Content-Type", "text/plain"),
                ("Content-Length", str(len(body))),
                ("Connection", "close"),
            ],
        )
        return [body]


def _user_rate_limited(user_id, limit=USER_LIMIT, interval=60):
    """Per-user counter so one user behind a shared IP cannot block the others.

    Same get/set scheme as CTFd's own `ratelimit`, so concurrent requests can overshoot
    the limit slightly. It is a courtesy limit: the caps that actually bound resource use
    are the request-size limits, the one-file-per-user rule and the (soft) total cap.
    There is deliberately no per-IP limit, it would let one user block a whole NAT.
    """
    key = "writeups:rl:{}".format(user_id)
    count = int(cache.get(key) or 0)
    if count >= limit:
        return True
    cache.set(key, count + 1, timeout=interval)
    return False


def _zip_response(rows, filename):
    """Stream a ZIP of the given rows; every file is hard-linked first (see _archive_link)."""
    entries = []
    try:
        for arc, w in zip(_zip_names(rows), rows):
            entries.append((arc, _archive_link(_path(w.stored_name))))
    except OSError:  # a record without its file: fail loudly instead of omitting it
        for _, link in entries:
            _remove(link)
        abort(409)
    resp = Response(_stream_zip(entries), mimetype="application/octet-stream")
    # Runs when the response is closed, even if the body was never iterated (HEAD).
    resp.call_on_close(lambda: [_remove(link) for _, link in entries])
    resp.headers["Content-Disposition"] = 'attachment; filename="%s"' % filename
    return _harden(resp)


def _validate_upload(f):
    """Return (name, ext, data, error_message) for an uploaded form file."""
    name = (f.filename or "").strip() if f else ""
    ext = os.path.splitext(name)[1].lower()
    if not f or not name:
        return name, ext, b"", gettext("Choose a file.")
    if ext not in ALLOWED:
        types = ", ".join(sorted(ALLOWED))
        return name, ext, b"", gettext("Allowed formats: %(types)s", types=types)
    data = f.stream.read(MAX_BYTES + 1)
    if not data:
        return name, ext, data, gettext("Empty files cannot be uploaded.")
    if len(data) > MAX_BYTES:
        mb = MAX_BYTES // 1024 // 1024
        return name, ext, data, gettext("Files can be at most %(mb)d MB.", mb=mb)
    if not _content_ok(ext, data):
        msg = gettext("The file content does not match its extension.")
        return name, ext, data, msg
    return name, ext, data, None


def _disk_error(size):
    """Cheap pre-check before anything is written: keep free space for the DB and CTFd."""
    if shutil.disk_usage(_store_dir()).free < MIN_FREE_BYTES + size:
        return gettext("The server is low on disk space. Please contact an organizer.")
    return None


def _quota_error(user_id, size, digest):
    """Duplicate and per-user/global limits. Call it while holding the user's row lock."""
    if db.session.query(
        Writeups.query.filter_by(user_id=user_id, sha256=digest).exists()
    ).scalar():
        return gettext("You already submitted this exact file.")
    count, used = (
        db.session.query(
            func.count(Writeups.id), func.coalesce(func.sum(Writeups.size), 0)
        )
        .filter(Writeups.user_id == user_id)
        .one()
    )
    if count >= MAX_FILES:
        return gettext(
            "You can submit up to %(n)d files. Delete one to upload another.",
            n=MAX_FILES,
        )
    if used + size > MAX_USER_TOTAL:
        return gettext(
            "Your files would exceed %(mb)d MB in total. Delete one to upload another.",
            mb=MAX_USER_TOTAL // 1024 // 1024,
        )
    total = db.session.query(func.coalesce(func.sum(Writeups.size), 0)).scalar()
    if total + size > MAX_TOTAL:
        return gettext("Writeup storage is full. Please contact an organizer.")
    return None


def load(app):
    app.db.create_all()
    try:
        _drop_user_unique(db.engine)
    except Exception:  # another worker may have upgraded the table first
        app.logger.warning("writeups: user_id unique upgrade skipped", exc_info=True)
    app.wsgi_app = _BodyLimit(app.wsgi_app)
    if SHOW_NAV_LINK:
        # Shown to everyone, like CTFd's own custom pages; logged-out visitors are sent to the login page.
        register_user_page_menu_bar(lazy_gettext("Writeups"), UPLOAD_PATH)
    # Ship our Korean strings inside the plugin so they never conflict with the
    # global catalog; Flask-Babel merges every directory listed here.
    dirs = app.config.get("BABEL_TRANSLATION_DIRECTORIES") or "translations"
    mine = os.path.join(os.path.dirname(__file__), "translations")
    if mine not in dirs.split(";"):
        app.config["BABEL_TRANSLATION_DIRECTORIES"] = dirs + ";" + mine
    bp = Blueprint("writeups", __name__, template_folder="templates")

    @bp.route("/writeups", methods=["GET", "POST"])
    @authed_only
    @require_verified_emails
    def mine():
        user = get_current_user()
        errors, infos = [], []
        if request.method == "POST":
            if _user_rate_limited(user.id):
                return Response(
                    gettext("Too many uploads. Try again in a minute."), 429
                )
            name, ext, data, error = _validate_upload(request.files.get("file"))
            error = error or _disk_error(len(data))
            if error:
                errors.append(error)
            else:
                stored = os.urandom(16).hex()
                fd, tmp = tempfile.mkstemp(dir=_store_dir(), prefix=".tmp")
                try:
                    with os.fdopen(fd, "wb") as out:
                        out.write(data)
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, _path(stored))
                except BaseException:
                    _remove(tmp)
                    raise
                uid = user.id
                digest = hashlib.sha256(data).hexdigest()
                try:
                    # One upload per user at a time: lock the user's row, check the
                    # limits and insert in the same transaction. Racing requests
                    # queue here, so the count and size limits are exact.
                    # End the transaction first: under REPEATABLE READ the snapshot
                    # taken by earlier reads (login check) would hide rows committed
                    # by the request we just waited for.
                    db.session.commit()
                    db.session.query(Users.id).filter_by(
                        id=uid
                    ).with_for_update().first()
                    error = _quota_error(uid, len(data), digest)
                    if error:
                        db.session.rollback()
                    else:
                        db.session.add(
                            Writeups(
                                user_id=uid,
                                original_name=name[:MAX_NAME],
                                ext=ext,
                                stored_name=stored,
                                size=len(data),
                                sha256=digest,
                            )
                        )
                        db.session.commit()
                except Exception:
                    db.session.rollback()
                    _remove(_path(stored))
                    raise
                if error:
                    _remove(_path(stored))
                    errors.append(error)
                else:
                    infos.append(gettext("Your writeup has been submitted."))
        rows = (
            Writeups.query.filter_by(user_id=user.id).order_by(Writeups.id.desc()).all()
        )
        return render_template(
            "writeups_mine.html",
            writeups=rows,
            total_size=sum(w.size for w in rows),
            errors=errors,
            infos=infos,
            allowed=sorted(ALLOWED),
            max_mb=MAX_BYTES // 1024 // 1024,
            max_files=MAX_FILES,
            max_user_mb=MAX_USER_TOTAL // 1024 // 1024,
        )

    @bp.route("/writeups/<int:writeup_id>/delete", methods=["POST"])
    @authed_only
    @require_verified_emails
    def delete_mine(writeup_id):
        user = get_current_user()
        if _user_rate_limited(user.id):
            return Response(gettext("Too many uploads. Try again in a minute."), 429)
        # Another user's id is a plain 404: ownership is part of the lookup.
        w = Writeups.query.filter_by(id=writeup_id, user_id=user.id).first_or_404()
        stored = w.stored_name
        gone = Writeups.query.filter_by(id=writeup_id, user_id=user.id).delete(
            synchronize_session=False
        )
        db.session.commit()
        if gone:
            _remove(_path(stored))
        return redirect(url_for("writeups.mine"))

    @bp.route("/admin/writeups")
    @admins_only
    def admin_list():
        rows = (
            Writeups.query.join(Users)
            .order_by(func.lower(Users.name), Users.id, Writeups.id)
            .all()
        )
        groups = {}
        for w in rows:
            g = groups.setdefault(w.user_id, {"user": w.user, "rows": [], "size": 0})
            g["rows"].append(w)
            g["size"] += w.size
        return render_template(
            "writeups_admin.html",
            groups=list(groups.values()),
            count=len(rows),
            total_size=sum(w.size for w in rows),
        )

    @bp.route("/admin/writeups/<int:writeup_id>/download")
    @admins_only
    def admin_download(writeup_id):
        w = Writeups.query.get_or_404(writeup_id)
        return _attachment(_path(w.stored_name), _download_name(w))

    @bp.route("/admin/writeups/user/<int:user_id>/download")
    @admins_only
    def admin_download_user(user_id):
        rows = Writeups.query.filter_by(user_id=user_id).order_by(Writeups.id).all()
        if not rows:
            abort(404)
        return _zip_response(rows, "writeups_{}.zip".format(_user_slug(rows[0])))

    @bp.route("/admin/writeups/download_all")
    @admins_only
    def admin_download_all():
        rows = Writeups.query.order_by(Writeups.user_id, Writeups.id).all()
        return _zip_response(rows, "writeups.zip")

    @bp.route("/admin/writeups/<int:writeup_id>/delete", methods=["POST"])
    @admins_only
    def admin_delete(writeup_id):
        w = Writeups.query.filter_by(id=writeup_id).first_or_404()
        stored = w.stored_name
        gone = Writeups.query.filter_by(id=writeup_id).delete(synchronize_session=False)
        db.session.commit()
        if gone:
            _remove(_path(stored))
        return redirect(url_for("writeups.admin_list"))

    app.jinja_env.globals.setdefault("writeup_when", _when)
    app.register_blueprint(bp)
