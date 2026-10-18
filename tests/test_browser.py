"""Browser test: a participant and an admin use the plugin the way real people do.

Usage:  python tests/test_browser.py http://127.0.0.1:8000

Needs `pip install playwright requests` and `playwright install chromium`
(or set CHROME to an existing chromium/chrome-headless-shell binary).
Point it at a CTFd that is NOT set up yet and has the plugin mounted. It sets CTFd up,
then drives the real pages: navbar link, login redirect, uploading with the progress bar,
duplicates, validation, delete confirmations, the admin Plugins menu, grouped list,
single / per-user / all downloads, and the language chosen in the user's Settings page.

Optional environment variables:
  REG_EXTRA   JSON of extra registration fields, e.g. '{"fields[1]":"1"}'
  LANGUAGE    CTFd language code to select in Settings (e.g. ko, de); default: leave English
  WORDS       JSON with the expected translated texts for that language, keys:
              nav, title, upload, success, duplicate, zip_user, zip_all
  CHROME      path to a chromium executable
"""

import io
import json
import os
import re
import sys
import zipfile

import requests
from playwright.sync_api import sync_playwright

BASE = sys.argv[1].rstrip("/")
REG_EXTRA = json.loads(os.environ.get("REG_EXTRA", "{}"))
LANGUAGE = os.environ.get("LANGUAGE", "")
WORDS = {
    "nav": "Writeups", "title": "Writeups", "upload": "Upload writeup",
    "success": "Your writeup has been submitted.", "duplicate": "You already submitted this exact file.",
    "zip_user": "Download user ZIP", "zip_all": "Download all (ZIP)",
}
WORDS.update(json.loads(os.environ.get("WORDS", "{}")))
ADMIN_PW = "Adm1n-pass-for-test!"
PDF = b"%PDF-1.4\n% browser test one\n%%EOF\n"
MD = "# notes\nsome text\n".encode()
TXT = b"solve.py output\n"
fails = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (" :: " + str(extra)[:200] if extra and not cond else ""))
    if not cond:
        fails.append(label)


def nonce(s, path):
    t = s.get(BASE + path).text
    m = re.search(r"""['"]csrfNonce['"]\s*:\s*['"]([0-9a-f]+)['"]|name=['"]nonce['"][^>]*value=['"]([0-9a-f]+)['"]""", t)
    return next(g for g in m.groups() if g)


def http_register(name):
    s = requests.Session()
    data = {"name": name, "email": name + "@example.com", "password": name + "-Pass-123!",
            "confirm_password": name + "-Pass-123!", "nonce": nonce(s, "/register")}
    data.update(REG_EXTRA)
    r = s.post(BASE + "/register", data=data, allow_redirects=False)
    check("register " + name, r.status_code == 302, r.status_code)


def login(page, name, pw):
    page.fill("#name", name)
    page.fill("#password", pw)
    page.click("#_submit")
    page.wait_for_load_state("load")


def choose_language(page, code):
    page.goto(BASE + "/settings")
    page.select_option("select[name=language]", code)
    # the settings form saves through an AJAX call: wait for it before navigating away
    with page.expect_response(lambda r: r.url.endswith("/api/v1/users/me") and r.request.method == "PATCH") as saved:
        page.click("form:has(select[name=language]) #_submit")
    check("Settings accepted the language change", saved.value.status == 200, saved.value.status)
    page.goto(BASE + "/settings")
    check("language %s saved in Settings" % code, page.input_value("select[name=language]") == code)


def upload(page, name, data, expect_state):
    page.set_input_files("#writeup-file", files=[{"name": name, "mimeType": "application/octet-stream", "buffer": data}])
    page.click("#writeup-submit")
    page.wait_for_function("s => document.querySelector('#writeup-result').dataset.state === s", arg=expect_state, timeout=20000)


def rows(page):
    return page.locator(".writeups-table tbody tr").count()


def names_on_page(page):
    return page.locator(".writeups-table .writeups-filename").all_inner_texts()


# --- set CTFd up (the only step done over plain HTTP) --------------------------------------------------
s = requests.Session()
r = s.post(BASE + "/setup", data={
    "ctf_name": "Demo CTF", "ctf_description": "demo", "user_mode": "users", "challenge_visibility": "private",
    "account_visibility": "public", "score_visibility": "public", "registration_visibility": "public",
    "verify_emails": "false", "team_size": "", "name": "admin", "email": "admin@example.com",
    "password": ADMIN_PW, "ctf_theme": "core", "theme_color": "", "start": "", "end": "",
    "nonce": nonce(s, "/setup"),
}, allow_redirects=False)
check("CTFd set up with the default 'core' theme", r.status_code in (200, 302), r.status_code)
http_register("alice")
http_register("frank")

with sync_playwright() as p:
    kw = {"executable_path": os.environ["CHROME"]} if os.environ.get("CHROME") else {}
    browser = p.chromium.launch(**kw)

    # ============================== participant ==========================================================
    ctx = browser.new_context(viewport={"width": 1200, "height": 800}, locale="en-US", accept_downloads=True)
    page = ctx.new_page()
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept() if ACCEPT[0] else d.dismiss()))
    ACCEPT = [True]

    page.goto(BASE + "/")
    link = page.locator('.navbar-nav a[href$="/writeups"]')
    check("logged out: the navbar shows a Writeups link on the default theme", link.count() == 1)
    link.click()
    page.wait_for_load_state("load")
    check("logged out: the link sends the visitor to the login page and remembers the target",
          "/login" in page.url and "next=" in page.url, page.url)
    login(page, "alice", "alice-Pass-123!")
    check("after login the visitor lands on /writeups", page.url.split("?")[0].endswith("/writeups"), page.url)

    if LANGUAGE:
        choose_language(page, LANGUAGE)
        page.goto(BASE + "/writeups")

    check("navbar link text", page.locator('.navbar-nav a[href$="/writeups"]').first.inner_text().strip() == WORDS["nav"],
          page.locator('.navbar-nav a[href$="/writeups"]').first.inner_text())
    check("page heading", page.locator("h1").first.inner_text().strip() == WORDS["title"], page.locator("h1").first.inner_text())
    check("upload button label", page.locator("#writeup-submit").inner_text().strip() == WORDS["upload"], page.locator("#writeup-submit").inner_text())
    check("empty state: counter 0 / 100 and no file table", page.inner_text("#writeup-count").strip() == "0 / 100" and rows(page) == 0)
    check("no download link is offered to participants", page.locator("a[href*='download']").count() == 0)
    check("the cancel button is hidden while idle", not page.locator("#writeup-cancel").is_visible())

    upload(page, "web-writeup.pdf", PDF, "success")
    check("upload shows the success message", page.inner_text("#writeup-status").strip() == WORDS["success"], page.inner_text("#writeup-status"))
    check("progress bar reached 100", page.eval_on_selector("#writeup-progress", "e => e.value") == 100)
    check("file list updated without reload, counter 1 / 100",
          names_on_page(page) == ["web-writeup.pdf"] and page.inner_text("#writeup-count").strip() == "1 / 100", names_on_page(page))
    check("file input is cleared and the form is usable again",
          page.eval_on_selector("#writeup-file", "e => e.value === '' && !e.disabled") and page.locator("#writeup-submit").is_enabled())
    check("cancel button hidden again after the upload", not page.locator("#writeup-cancel").is_visible())

    upload(page, "same-content-other-name.pdf", PDF, "error")
    check("identical content is refused with the duplicate message", page.inner_text("#writeup-status").strip() == WORDS["duplicate"], page.inner_text("#writeup-status"))
    check("the refused duplicate was not added", names_on_page(page) == ["web-writeup.pdf"])

    page.set_input_files("#writeup-file", files=[{"name": "evil.exe", "mimeType": "application/octet-stream", "buffer": PDF + b"x"}])
    page.click("#writeup-submit")
    page.wait_for_function("document.querySelector('#writeup-result').dataset.state === 'error'")
    check("a disallowed extension is stopped in the browser (nothing added)", rows(page) == 1)
    page.set_input_files("#writeup-file", files=[{"name": "fake.pdf", "mimeType": "application/pdf", "buffer": b"<html>not a pdf</html>"}])
    page.click("#writeup-submit")
    page.wait_for_function("document.querySelector('#writeup-result').dataset.state === 'error' && !document.querySelector('#writeup-submit').disabled")
    check("a fake PDF is rejected by the server (nothing added)", rows(page) == 1)

    upload(page, "notes.md", MD, "success")
    upload(page, "solve.txt", TXT, "success")
    check("three files, newest first, counter 3 / 100",
          names_on_page(page) == ["solve.txt", "notes.md", "web-writeup.pdf"] and page.inner_text("#writeup-count").strip() == "3 / 100", names_on_page(page))

    # delete: the confirm dialog must be able to cancel the action
    confirm_text = page.locator(".writeups-table tbody tr", has_text="notes.md").locator("form.writeup-delete").get_attribute("data-confirm")
    ACCEPT[0] = False
    page.locator(".writeups-table tbody tr", has_text="notes.md").locator("form.writeup-delete button").click()
    page.wait_for_timeout(500)
    check("delete: a confirmation dialog appears with the translated text", dialogs and dialogs[-1] == confirm_text, dialogs)
    check("delete: cancelling the dialog keeps the file", rows(page) == 3)
    ACCEPT[0] = True
    page.locator(".writeups-table tbody tr", has_text="notes.md").locator("form.writeup-delete button").click()
    page.wait_for_load_state("load")
    check("delete: confirming removes only that file", sorted(names_on_page(page)) == ["solve.txt", "web-writeup.pdf"], names_on_page(page))
    check("counter is 2 / 100 after the delete", page.inner_text("#writeup-count").strip() == "2 / 100")
    page.reload()
    check("the list survives a page reload", rows(page) == 2)
    upload(page, "notes-again.md", MD, "success")
    check("deleted content can be uploaded again", rows(page) == 3)
    row = page.locator(".writeups-table tbody tr", has_text="notes-again.md").locator("form.writeup-delete button")
    row.click()
    page.wait_for_load_state("load")
    check("back to 2 files", rows(page) == 2)

    resp = page.goto(BASE + "/admin/writeups")
    check("a participant cannot open the admin page", "/admin/writeups" not in page.url or (resp and resp.status in (302, 403)), (page.url, resp.status if resp else None))
    check("the admin page content is not shown to the participant", page.locator("a[href*='/download']").count() == 0)

    # second participant, so the admin has two groups
    ctx_f = browser.new_context(viewport={"width": 1200, "height": 800}, locale="en-US")
    pf = ctx_f.new_page()
    pf.goto(BASE + "/login")
    login(pf, "frank", "frank-Pass-123!")
    pf.goto(BASE + "/writeups")
    if LANGUAGE:
        choose_language(pf, LANGUAGE)
        pf.goto(BASE + "/writeups")
    upload(pf, "frank-notes.txt", b"frank's writeup\n", "success")
    check("a second participant only sees his own file", names_on_page(pf) == ["frank-notes.txt"])
    ctx_f.close()

    # ============================== admin ================================================================
    actx = browser.new_context(viewport={"width": 1200, "height": 800}, locale="en-US", accept_downloads=True)
    ap = actx.new_page()
    adialogs = []
    AACCEPT = [False]
    ap.on("dialog", lambda d: (adialogs.append(d.message), d.accept() if AACCEPT[0] else d.dismiss()))
    ap.goto(BASE + "/login")
    login(ap, "admin", ADMIN_PW)
    if LANGUAGE:
        choose_language(ap, LANGUAGE)
    ap.goto(BASE + "/admin")
    plugin_link = ap.locator('.dropdown-menu a[href$="/admin/writeups"]')
    check("admin: the plugin appears in the admin Plugins menu", plugin_link.count() == 1)
    ap.locator("li.dropdown", has=plugin_link).locator("a.dropdown-toggle").click()
    plugin_link.click()
    ap.wait_for_load_state("load")
    check("admin: the menu link opens the writeups page", ap.url.split("?")[0].endswith("/admin/writeups"), ap.url)

    groups = ap.locator("tr.writeups-group").all_inner_texts()
    check("admin: files are grouped by participant (alice, frank), sorted by name", len(groups) == 2 and groups[0].lower().startswith("alice") and groups[1].lower().startswith("frank"), groups)
    check("admin: the alice group reports 2 files (name and count are separated)", re.match(r"alice\s+\D*?2(?!\d)", groups[0]) is not None, groups[0])
    check("admin: summary shows 2 participants and 3 files", re.search(r"2.*3", ap.locator(".writeups-admin p.mb-0").first.inner_text()) is not None, ap.locator(".writeups-admin p.mb-0").first.inner_text())
    check("admin: the user-ZIP and all-ZIP buttons carry the expected labels",
          ap.locator("tr.writeups-group a", has_text=WORDS["zip_user"]).count() == 2 and ap.locator("a", has_text=WORDS["zip_all"]).count() == 1)

    # downloads, in a real browser
    first_row = ap.locator("tr:not(.writeups-group)", has_text="web-writeup.pdf")
    with ap.expect_download() as d:
        first_row.locator('a[href$="/download"]').click()
    got = open(d.value.path(), "rb").read()
    check("admin: downloading one file returns the exact bytes", got == PDF)
    check("admin: the download name is server-made (not the client name)", re.fullmatch(r"writeup_\d+_alice_\d+\.pdf", d.value.suggested_filename) is not None, d.value.suggested_filename)

    with ap.expect_download() as d:
        ap.locator("tr.writeups-group a", has_text=WORDS["zip_user"]).first.click()
    z = zipfile.ZipFile(d.value.path())
    names = z.namelist()
    check("admin: the per-user ZIP holds alice's 2 files in one safe folder",
          len(names) == 2 and len({n.split('/')[0] for n in names}) == 1 and all(re.fullmatch(r"\d+_alice/\d{3}_[A-Za-z0-9._-]+", n) for n in names), names)
    check("admin: the per-user ZIP has the right bytes", sorted(z.read(n) for n in names) == sorted([PDF, TXT]) and z.testzip() is None)

    with ap.expect_download() as d:
        ap.locator("a", has_text=WORDS["zip_all"]).click()
    z = zipfile.ZipFile(d.value.path())
    check("admin: the all-ZIP holds all 3 files of both participants", len(z.namelist()) == 3 and z.testzip() is None, z.namelist())

    # delete from the admin side
    n_before = ap.locator("tr:not(.writeups-group) a[href$='/download']").count()
    frank_row = ap.locator("tr:not(.writeups-group)", has_text="frank-notes.txt")
    AACCEPT[0] = False
    frank_row.locator("form.writeup-delete button").click()
    ap.wait_for_timeout(500)
    check("admin: delete asks for confirmation", len(adialogs) >= 1)
    check("admin: cancelling keeps the file", ap.locator("tr:not(.writeups-group) a[href$='/download']").count() == n_before)
    AACCEPT[0] = True
    frank_row.locator("form.writeup-delete button").click()
    ap.wait_for_load_state("load")
    check("admin: confirming deletes the file and its now-empty group disappears",
          ap.locator("tr:not(.writeups-group) a[href$='/download']").count() == n_before - 1 and ap.locator("tr.writeups-group").count() == 1)

    # the participant sees the effect of an admin deletion
    alice_row = ap.locator("tr:not(.writeups-group)", has_text="solve.txt")
    alice_row.locator("form.writeup-delete button").click()
    ap.wait_for_load_state("load")
    page.goto(BASE + "/writeups")
    check("a file deleted by the admin disappears from the participant's list", names_on_page(page) == ["web-writeup.pdf"], names_on_page(page))

    browser.close()

print("\nFAILED: %d" % len(fails) if fails else "\nALL PASSED")
sys.exit(1 if fails else 0)
