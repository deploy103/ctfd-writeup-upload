"""End-to-end test for the writeups plugin against a disposable, fresh CTFd.

Usage:  python tests/test_e2e.py http://127.0.0.1:8000

Needs `requests`. Point it at a CTFd that has NOT been set up yet and has this plugin
mounted (see README). It creates an admin and a few users, then checks uploads, limits,
permissions, CSRF, downloads and the request-size guard. Exit code 1 if anything fails.
Optional env: REG_EXTRA='{"fields[1]":"1"}' adds required custom registration fields;
EXPECT_KO=1 checks the Korean example catalog (copy examples/ko into writeups/translations/ first).
"""

import concurrent.futures
import io
import json
import os
import re
import socket
import sys
import time
import zipfile
from urllib.parse import urlparse

import requests

BASE = sys.argv[1].rstrip("/")
OK_MSG = "has been submitted"
DUP_MSG = "already submitted"
PDF = b"%PDF-1.4\n% test writeup\n%%EOF\n"
MD = "# 라이트업\n내용\n".encode()
ZB = io.BytesIO()
with zipfile.ZipFile(ZB, "w") as _z:
    _z.writestr("a.txt", "hello")
ZIPB = ZB.getvalue()
REG_EXTRA = json.loads(os.environ.get("REG_EXTRA", "{}"))
fails = []


def check(label, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + label + (" :: " + str(extra) if extra and not cond else ""))
    if not cond:
        fails.append(label)


def nonce(s, path):
    t = s.get(BASE + path).text
    m = re.search(r"""['"]csrfNonce['"]\s*:\s*['"]([0-9a-f]+)['"]|name=['"]nonce['"][^>]*value=['"]([0-9a-f]+)['"]""", t)
    return next(g for g in m.groups() if g)


def session():
    return requests.Session()


def post(s, path, **kw):
    return s.post(BASE + path, allow_redirects=False, **kw)


def upload(s, name, data, n=None, retry=True):
    r = post(s, "/writeups", data={"nonce": n or nonce(s, "/writeups")}, files={"file": (name, data, "application/octet-stream")})
    if r.status_code == 429 and retry:  # per-user limit (30/min): wait out the window
        time.sleep(61)
        return upload(s, name, data, n, retry=False)
    return r


def my_files(s):
    html = s.get(BASE + "/writeups").text
    return list(zip(re.findall(r"/writeups/(\d+)/delete", html), re.findall(r'<td class="writeups-filename">(.*?)</td>', html))), html


def admin_ids(html):
    return re.findall(r"/admin/writeups/(\d+)/download", html)


def login(s, name, pw):
    return post(s, "/login", data={"name": name, "password": pw, "nonce": nonce(s, "/login")})


# --- bootstrap -----------------------------------------------------------------------------------
admin = session()
r = post(admin, "/setup", data={
    "ctf_name": "t", "ctf_description": "t", "user_mode": "users", "challenge_visibility": "private",
    "account_visibility": "public", "score_visibility": "public", "registration_visibility": "public",
    "verify_emails": "false", "team_size": "", "name": "admin", "email": "admin@example.com",
    "password": "Adm1n-pass-for-test!", "ctf_theme": "core", "theme_color": "", "start": "", "end": "",
    "nonce": nonce(admin, "/setup"),
})
check("setup", r.status_code in (200, 302), r.status_code)
check("admin login", login(admin, "admin", "Adm1n-pass-for-test!").status_code == 302)

users = {}
for name in ("alice", "bob", "dave"):
    s = session()
    data = {"name": name, "email": name + "@example.com", "password": name + "-Pass-123!",
            "confirm_password": name + "-Pass-123!", "nonce": nonce(s, "/register")}
    data.update(REG_EXTRA)
    r = post(s, "/register", data=data)
    check("register " + name, r.status_code == 302, r.status_code)
    login(s, name, name + "-Pass-123!")
    users[name] = s
alice, bob, dave, anon = users["alice"], users["bob"], users["dave"], session()

# --- participant: several files, own list ----------------------------------------------------------
check("anon is redirected to login", anon.get(BASE + "/writeups", allow_redirects=False).status_code == 302)
check("POST without CSRF nonce is rejected", post(alice, "/writeups", data={}, files={"file": ("w.pdf", PDF)}).status_code == 403)
check("list is empty on first visit", my_files(alice)[0] == [])
for nm, data in [("first writeup.pdf", PDF), ("notes.md", MD), ("archive.zip", ZIPB), ("한글 이름.txt", "안녕".encode())]:
    r = upload(alice, nm, data)
    check("upload %s accepted" % nm, r.status_code == 200 and OK_MSG in r.text, r.status_code)
files, html = my_files(alice)
check("4 files listed, counter 4 / 100", len(files) == 4 and "4 / 100" in html, files)
check("each upload is stored separately", len({i for i, _ in files}) == 4)

bad = {
    "exe extension": ("evil.exe", PDF), "no extension": ("evil", PDF), "double extension": ("evil.pdf.exe", PDF),
    "html disguised as pdf": ("x.pdf", b"<script>alert(1)</script>"), "text disguised as zip": ("x.zip", b"not a zip"),
    "zip header only": ("x.zip", b"PK\x03\x04"), "empty file": ("x.pdf", b""),
    "binary as txt": ("x.txt", b"abc\x00def"), "invalid utf8 md": ("x.md", b"\xff\xfe\xfa"),
}
for label, (nm, data) in bad.items():
    r = upload(alice, nm, data)
    check("reject " + label, r.status_code == 200 and OK_MSG not in r.text and "alert" in r.text, r.status_code)
check("rejected files were not stored", len(my_files(alice)[0]) == 4)
check("reject >20MB", upload(alice, "big.pdf", b"%PDF-" + b"A" * (21 * 1024 * 1024)).status_code == 413)
check("2MB accepted", OK_MSG in upload(alice, "huge.pdf", b"%PDF-" + b"A" * (2 * 1024 * 1024)).text)

# --- duplicates -------------------------------------------------------------------------------------
check("same content, same name rejected", DUP_MSG in upload(alice, "first writeup.pdf", PDF).text)
check("same content, other name rejected", DUP_MSG in upload(alice, "renamed.pdf", PDF).text)
check("duplicates were not stored", len(my_files(alice)[0]) == 5)
mine = alice.get(BASE + "/writeups").text
check("participant page shows human-readable sizes", ">2 MB<" in mine and re.search(r">\d+ B<", mine) is not None and "bytes</td>" not in mine)
check("exact byte count stays in the tooltip", 'title="2097157 bytes"' in mine)
check("another participant may submit the same bytes", OK_MSG in upload(bob, "same.pdf", PDF).text)

# --- escaping ----------------------------------------------------------------------------------------
check("traversal filename handled", OK_MSG in upload(bob, "../../../../etc/cron.d/x.zip", ZIPB).text)
check("html filename accepted", OK_MSG in upload(bob, "<img src=x onerror=alert(1)>.pdf", b"%PDF-1.4\n% bob\n").text)
bob_files, bob_html = my_files(bob)
check("html filename is escaped on the participant page", "<img src=x" not in bob_html and "&lt;img src=x" in bob_html)
check("bob sees only his own files", len(bob_files) == 3 and "first writeup" not in bob_html)

# --- participant delete: own only -----------------------------------------------------------------------
alice_files, _ = my_files(alice)
target = next(i for i, nm in alice_files if nm == "notes.md")
check("IDOR: another user cannot delete it (404)", post(bob, "/writeups/%s/delete" % target, data={"nonce": nonce(bob, "/writeups")}).status_code == 404)
check("delete without nonce refused", post(alice, "/writeups/%s/delete" % target, data={}).status_code == 403)
check("delete via GET refused", alice.get(BASE + "/writeups/%s/delete" % target, allow_redirects=False).status_code in (404, 405))
check("nothing changed so far", len(my_files(alice)[0]) == 5)
check("owner deletes the file", post(alice, "/writeups/%s/delete" % target, data={"nonce": nonce(alice, "/writeups")}).status_code == 302)
check("it is gone", target not in [i for i, _ in my_files(alice)[0]] and len(my_files(alice)[0]) == 4)
check("deleting twice is a 404", post(alice, "/writeups/%s/delete" % target, data={"nonce": nonce(alice, "/writeups")}).status_code == 404)

# --- participants never download -------------------------------------------------------------------------
bob_target = bob_files[0][0]
for who, s in (("anon", anon), ("participant", alice)):
    for path in ("/admin/writeups", "/admin/writeups/%s/download" % bob_target, "/admin/writeups/download_all", "/admin/writeups/user/1/download"):
        check("%s cannot open %s" % (who, path), s.get(BASE + path, allow_redirects=False).status_code in (302, 403))
for p in ("/writeups/1", "/plugins/writeups/assets/", "/files/writeups/anything"):
    check("anon cannot fetch %s" % p, anon.get(BASE + p, allow_redirects=False).status_code != 200)

# --- admin: grouped, per-user and overall ZIP -------------------------------------------------------------
page = admin.get(BASE + "/admin/writeups").text
check("admin list is grouped by participant", "alice" in page and "bob" in page and "4 files" in page, re.findall(r"\d+ files", page))
check("admin page shows human-readable sizes", ">2 MB<" in page and re.search(r">\d+ B<", page) is not None and "bytes</td>" not in page)
check("html filename is escaped in the admin page", "<img src=x" not in page and "&lt;img src=x" in page)
first_id = next(i for i, nm in my_files(alice)[0] if nm == "first writeup.pdf")
r = admin.get(BASE + "/admin/writeups/%s/download" % first_id)
cd = r.headers.get("Content-Disposition", "")
check("single download returns identical bytes", r.status_code == 200 and r.content == PDF)
check("download is an octet-stream attachment with nosniff",
      "attachment" in cd and r.headers.get("Content-Type", "").startswith("application/octet-stream")
      and r.headers.get("X-Content-Type-Options") == "nosniff", r.headers)
check("download name is server-made, not the client name", re.search(r'filename="?writeup_\d+_alice_%s\.pdf' % first_id, cd) is not None and "first" not in cd, cd)
uid = re.search(r"/admin/writeups/user/(\d+)/download", page.split("alice")[1]).group(1)
zf = zipfile.ZipFile(io.BytesIO(admin.get(BASE + "/admin/writeups/user/%s/download" % uid).content))
names = zf.namelist()
check("user ZIP: 4 entries in one safe folder", len(names) == 4 and len({n.split("/")[0] for n in names}) == 1
      and all(re.fullmatch(r"%s_alice/\d{3}_[A-Za-z0-9._-]+" % uid, n) for n in names), names)
check("user ZIP: alice's bytes", sorted(zf.read(n) for n in names) == sorted([PDF, ZIPB, "안녕".encode(), b"%PDF-" + b"A" * (2 * 1024 * 1024)]))
check("user ZIP for an unknown user is 404", admin.get(BASE + "/admin/writeups/user/999999/download", allow_redirects=False).status_code == 404)
zf = zipfile.ZipFile(io.BytesIO(admin.get(BASE + "/admin/writeups/download_all").content))
check("all-ZIP: 7 unique entries, no traversal, intact", len(zf.namelist()) == 7 == len(set(zf.namelist()))
      and all(".." not in n and not n.startswith("/") for n in zf.namelist()) and zf.testzip() is None, zf.namelist())
check("HEAD on download-all works", admin.head(BASE + "/admin/writeups/download_all", allow_redirects=False).status_code == 200)

# --- admin delete ------------------------------------------------------------------------------------------
wid = admin_ids(admin.get(BASE + "/admin/writeups").text)[0]
check("admin delete via GET refused", admin.get(BASE + "/admin/writeups/%s/delete" % wid, allow_redirects=False).status_code in (404, 405))
check("admin delete without nonce refused", post(admin, "/admin/writeups/%s/delete" % wid, data={}).status_code == 403)
check("admin delete with nonce", post(admin, "/admin/writeups/%s/delete" % wid, data={"nonce": nonce(admin, "/admin/writeups")}).status_code == 302)
check("row is gone", wid not in admin_ids(admin.get(BASE + "/admin/writeups").text))
check("participant cannot use the admin delete route",
      bob.post(BASE + "/admin/writeups/%s/delete" % admin_ids(admin.get(BASE + "/admin/writeups").text)[0],
               data={"nonce": nonce(bob, "/writeups")}, allow_redirects=False).status_code in (302, 403))

# --- concurrency ------------------------------------------------------------------------------------------
nn = nonce(dave, "/writeups")
bodies = [PDF + b"%" + str(i).encode() * 8 for i in range(8)]
with concurrent.futures.ThreadPoolExecutor(8) as ex:
    rs = list(ex.map(lambda b: upload(dave, "race.pdf", b, n=nn, retry=False), bodies))
check("8 concurrent distinct uploads all succeed", all(r.status_code == 200 for r in rs) and len(my_files(dave)[0]) == 8, len(my_files(dave)[0]))
same = PDF + b"identical race"
with concurrent.futures.ThreadPoolExecutor(10) as ex:
    rs = list(ex.map(lambda i: upload(dave, "same%d.pdf" % i, same, n=nn, retry=False), range(10)))
check("10 concurrent identical uploads: exactly one is stored", sum(OK_MSG in r.text for r in rs) == 1 and len(my_files(dave)[0]) == 9)

# --- request-size guard runs before CTFd parses anything ------------------------------------------------
host, port = urlparse(BASE).hostname, urlparse(BASE).port or 80


def raw(request_bytes, wait=5):
    s = socket.create_connection((host, port), timeout=wait)
    s.sendall(request_bytes)
    try:
        return s.recv(4096).split(b"\r\n", 1)[0].decode()
    except socket.timeout:
        return "TIMEOUT"
    finally:
        s.close()


head = "POST {} HTTP/1.1\r\nHost: x\r\nContent-Type: multipart/form-data; boundary=b\r\n"
status = lambda t: t.split(" ")[1:2]  # noqa: E731
for path, cl in (("/writeups", 5000000000), ("//writeups", 5000000000), ("///writeups", 5000000000), ("/%2Fwriteups", 5000000000),
                 ("/writeups/", 5000000000), ("/writeups/1/delete", 1000000), ("//writeups/1/delete", 1000000),
                 ("/admin/writeups/1/delete", 1000000), ("//admin/writeups/1/delete", 1000000), ("/%2Fadmin/writeups/1/delete", 1000000)):
    check("oversized body to %s -> 413 before parsing" % path, status(raw((head.format(path) + "Content-Length: %d\r\n\r\n--b\r\n" % cl).encode())) == ["413"])
check("a normal small POST still reaches CTFd (403 CSRF)", post(anon, "/writeups", data={"x": "y"}).status_code == 403)

# --- navbar link and language ---------------------------------------------------------------------------
nav = alice.get(BASE + "/challenges").text
check("default theme shows a Writeups link in the navbar", re.search(r'href="[^"]*/writeups"[^>]*>\s*Writeups\s*<', nav) is not None)
check("the link is also shown to logged-out visitors (they are sent to login)", re.search(r'href="[^"]*/writeups"', anon.get(BASE + "/login").text) is not None)
ko = session()
ko.cookies.set("language", "ko")
login(ko, "bob", "bob-Pass-123!")
r = upload(ko, "tr.pdf", b"%PDF-1.4\n% ko\n")
if os.environ.get("EXPECT_KO"):  # run with the example catalog installed (examples/ko -> writeups/translations/ko)
    check("Korean catalog installed: server message translated", "라이트업이 제출되었습니다." in r.text)
    check("Korean catalog installed: page strings translated", "내 제출 현황" in r.text and "내 파일" in r.text)
else:
    check("no catalog installed: English is the default for any language", OK_MSG in r.text and "Your submissions" in r.text)

# --- rate limit last (it exhausts the window) -----------------------------------------------------------------
codes = [upload(bob, "x.exe", PDF, retry=False).status_code for _ in range(35)]
check("per-user rate limit kicks in (429)", 429 in codes, codes)
check("another user from the same IP is not blocked", OK_MSG in upload(alice, "again.pdf", b"%PDF-1.4\n% again\n", retry=False).text)

print("\nFAILED: %d" % len(fails) if fails else "\nALL PASSED")
sys.exit(1 if fails else 0)
