# ctfd-writeup-upload

[한국어 안내](README.ko.md)

A [CTFd](https://github.com/CTFd/CTFd) plugin that lets participants upload their **writeups as files** after a CTF,
and lets admins download them, grouped by participant.

Most existing writeup plugins store plain text or only support manual grading. This one stores real files
(`.pdf`, `.zip`, `.md`, `.txt`) and treats them as untrusted input.

| Participant (`/writeups`) | Admin (`/admin/writeups`) |
| --- | --- |
| ![participant page](docs/participant.png) | ![admin page](docs/admin.png) |

(Screenshots: stock CTFd 3.8.3 with the default `core` theme, no customisation.)

## Features

**Participants** (log in, then open `/writeups`)

- Upload up to **100 files** each: `.pdf .zip .md .txt`, max **20 MB** per file, **200 MB** in total
- Every upload is a new file (nothing is replaced). A list shows name, size and time, and the file count (`4 / 100`)
- Delete your own files. Submitting the **same content twice is rejected**, whatever the file name
- Upload progress bar
- Participants **cannot download** anything, not even their own files

**Admins** (`/admin/writeups`, also linked from the admin *Plugins* menu)

- Files grouped by participant, with file count and total size
- Download one file, **one participant's files as a ZIP**, or **everything as a ZIP** (streamed, no temporary disk)
- Delete a file

Works **out of the box on CTFd's default `core` theme** (the one tested): a *Writeups* link appears in the navbar on
its own, the pages use plain Bootstrap markup, and no theme or template has to be edited. Other themes may style the
pages differently or may not list plugin links, see [Requirements](#requirements). Everything is in **English** unless you add a
translation (see [Translating](#translating)).

## Requirements

- CTFd **3.8.x** (tested on the stock `ctfd/ctfd:3.8.3` image with the default `core` theme)
- **MariaDB/MySQL** recommended (the per-user limits rely on row locking). SQLite works for throw-away test installs only
- No theme changes needed on the default theme. The navbar link is added through CTFd's plugin menu API. Themes that do not render plugin
  links (or that already have their own link) can set `SHOW_NAV_LINK = False` and link to `/writeups` themselves

## Install

Copy the `writeups/` folder to `CTFd/plugins/writeups/` and restart CTFd. With Docker Compose, mount it read-only:

```yaml
services:
  ctfd:
    volumes:
      - ./writeups:/opt/CTFd/CTFd/plugins/writeups:ro
```

The `writeups` table is created automatically on start. Uploaded files go to `UPLOAD_FOLDER/writeups/`
(`/var/uploads/writeups/` in the official image); include that folder in your backups.

## Configuration

The limits are constants at the top of `writeups/__init__.py`:

| Constant | Default | Meaning |
| --- | --- | --- |
| `MAX_BYTES` | 20 MiB | largest single file |
| `MAX_FILES` | 100 | files per participant |
| `MAX_USER_TOTAL` | 200 MiB | total size per participant |
| `MAX_TOTAL` | 10 GiB | soft cap for all writeups |
| `MIN_FREE_BYTES` | 1 GiB | free disk space that must remain |
| `USER_LIMIT` | 30 | uploads and deletes per participant per minute |
| `ALLOWED` | `.pdf .zip .md .txt` | accepted extensions |
| `SHOW_NAV_LINK` | `True` | add the *Writeups* link to the navbar |

## Translating

English is built in and is the default. The language a user sees follows CTFd's normal language setting
(the user's own *Settings → Language*, or the admin default).

`writeups/translations/messages.pot` is the template with every message. To add a language, for example German:

```sh
pip install babel
pybabel init -i writeups/translations/messages.pot -d writeups/translations -l de
# edit writeups/translations/de/LC_MESSAGES/messages.po (this is the text file you translate)
pybabel compile -f -d writeups/translations      # writes messages.mo, the file CTFd actually reads
```

Restart CTFd afterwards. Only the compiled `messages.mo` is needed at run time. The language must be one that CTFd
offers. A ready-made **Korean** example is in `examples/ko/`; copy it to `writeups/translations/ko/` to use it.
The catalogs are merged with CTFd's own, so they do not touch `CTFd/translations`.

If you change the plugin's messages, regenerate the template with
`pybabel extract -F babel.cfg -k lazy_gettext -o writeups/translations/messages.pot .` and run
`pybabel update -i writeups/translations/messages.pot -d writeups/translations`.

## Security design

- **Auth and CSRF** use CTFd's own mechanisms (`authed_only`, `admins_only`, `require_verified_emails`, the global
  CSRF nonce). Deletes are POST-only and match both the file id and its owner (someone else's id is a 404).
- **Request size is limited before CTFd parses the body.** CTFd reads form data in a global `before_request` hook, which
  parses the whole multipart body even for anonymous requests, so a size check inside the view runs too late. The plugin
  wraps the WSGI app and answers `413` / `411` first. It normalises the path the way Werkzeug does, so `//writeups`
  and `/%2Fwriteups` are covered, and it also protects direct access to gunicorn. nginx still buffers up to its own
  `client_max_body_size` for every CTFd POST, so keep that sensible.
- **File validation:** extension allow-list plus a content check (PDF signature, a real ZIP central directory, UTF-8 text
  without NUL bytes). Client file names are shown escaped and are never used as paths.
- **Storage:** random 32-hex names in `UPLOAD_FOLDER/writeups` (directory `0700`, files `0600`). Files are not
  registered in CTFd's `Files` table, so `/files/<path>` can never serve them.
- **Downloads** are admin-only, sent as `application/octet-stream` attachments with `nosniff` and a sandbox CSP.
  Download names and ZIP entry names are generated by the server (ASCII only).
- **Concurrency:** upload, limits and the duplicate check run under a per-user row lock (`SELECT ... FOR UPDATE`), so
  racing requests cannot exceed 100 files / 200 MB or store the same content twice.
- **Download-all** hard-links the files, streams the ZIP and removes the links when the response closes.
- The server **never extracts** uploaded archives. A submitted ZIP can be a zip bomb or contain path-traversal entries,
  so open downloaded files in a sandbox or VM.

## Known limits

- The total cap and the per-user rate limit are approximate (CTFd's cache get/set is not atomic).
- Deleting a user removes the database rows but leaves the files on disk. A crash between writing a file and
  committing its row can also leave one. Reconcile with the database while uploads are paused.
- Hidden or unverified users can upload when email verification is off. In team mode the limits are per account.
- The automatic `UNIQUE(user_id)` upgrade (for tables created by an early one-file-per-user version) and the row
  locking are written for MariaDB/MySQL.

## Tests

Both tests talk to a running CTFd. Start a **fresh, not yet set up** CTFd with the plugin mounted. They set CTFd up themselves.

**`tests/test_browser.py`** drives a real browser the way people use the plugin: the navbar link, the login redirect,
uploading with the progress bar, duplicates and validation, delete confirmations, the language chosen on the
*Settings* page, then the admin side (Plugins menu, grouped list, single / per-user / all downloads, deletes).

```sh
pip install playwright requests && playwright install chromium
python tests/test_browser.py http://127.0.0.1:8000
# with a translation installed, e.g. the Korean example (the WORDS are the expected translated texts):
LANGUAGE=ko WORDS='{"nav":"라이트업","title":"라이트업","upload":"라이트업 업로드","success":"라이트업이 제출되었습니다.","duplicate":"이미 제출한 것과 똑같은 파일입니다.","zip_user":"사용자 ZIP 다운로드","zip_all":"전체 다운로드 (ZIP)"}' \
  python tests/test_browser.py http://127.0.0.1:8000
```

**`tests/test_e2e.py`** checks the HTTP behaviour in depth: uploads and rejections, the limits, duplicates, escaping,
deletes and ownership, permissions, CSRF, downloads and ZIPs, concurrency and the request-size guard
(including `//writeups`-style aliases).

```sh
pip install requests
python tests/test_e2e.py http://127.0.0.1:8000
EXPECT_KO=1 python tests/test_e2e.py http://127.0.0.1:8000   # with examples/ko copied into writeups/translations/ko
```

For a CTFd that requires extra registration fields add `REG_EXTRA='{"fields[1]":"1"}'` to either command.
If `test_e2e.py` runs into the per-user rate limit it pauses for a minute and retries, so a full run can take a few minutes.

Tested combinations (all passing): stock `ctfd/ctfd:3.8.3` with the default theme in English, with the Korean example
installed, and with a German catalog created by following the *Translating* steps above (only 7 messages translated,
the rest correctly fall back to English); and a customised CTFd 3.8.3 fork using its `core` theme.

## Prior art and credits

The code was written from scratch. Before writing it I reviewed these existing plugins; **no code was copied or
adapted** from them:

- [lapchynski/ctfd-writeups](https://github.com/lapchynski/ctfd-writeups): text writeups; no file upload or download
- [IdanRosenzweig/ctfd_plugin_writeups](https://github.com/IdanRosenzweig/ctfd_plugin_writeups): text writeups, no admin download
- [0xc7m/ctfd-manual-verification](https://github.com/0xc7m/ctfd-manual-verification): manual grading, no file upload

The file-storage approach follows the principles of CTFd's own uploader (random directory, sanitised name).

## License

[MIT](LICENSE)
