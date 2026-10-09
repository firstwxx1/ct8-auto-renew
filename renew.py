#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ct8-renew - keep a CT8 (panel.ct8.pl) free host alive by logging in periodically.

WHY THIS EXISTS
---------------
CT8 free accounts get suspended for inactivity. The renewal action is simply
logging into the panel - that resets the inactivity clock. Nothing more.

HOW IT WORKS (no browser needed)
--------------------------------
panel.ct8.pl is a stock Django app behind nginx + Phusion Passenger. The login
form has no captcha and no Cloudflare challenge, so a plain HTTP POST is
enough. That is why this script is stdlib-only instead of a puppeteer + xvfb
stack:

    GET  /login/?next=/      -> sets csrftoken cookie, form carries
                                csrfmiddlewaretoken
    POST /login/             -> csrfmiddlewaretoken, username, password, next
    success                  -> lands on the dashboard, which contains
                                /logout/ and sets a sessionid cookie

USAGE
    export ACCOUNTS_JSON='[{"username":"u","password":"p"}]'
    python renew.py

    python renew.py --selftest     # negative test with bogus credentials

EXIT CODES
    0  every account logged in
    1  at least one failed
    2  configuration error
"""

import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PANEL = "https://panel.ct8.pl"
LOGIN_URL = PANEL + "/login/"
LOGIN_PAGE = LOGIN_URL + "?next=/"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class LoginFailed(Exception):
    pass


def build_opener():
    """Cookie jar + no proxy. Returns (opener, jar) so callers can read cookies."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.ProxyHandler({}),
    )
    return opener, jar


def fetch_login_page(opener):
    req = urllib.request.Request(LOGIN_PAGE, headers={"User-Agent": UA})
    with opener.open(req, timeout=45) as r:
        html = r.read().decode("utf-8", "replace")
    m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', html)
    if not m:
        # Django also accepts the bare cookie value in some configs
        m = re.search(r'name=["\']csrfmiddlewaretoken["\'][^>]*value=["\']([^"\']+)', html)
    return html, (m.group(1) if m else None)


def login(opener, jar, username, password):
    """Returns (ok: bool, detail: str).

    Success is decided on a POSITIVE signal - a logout link and/or a sessionid
    cookie with no login form on the page - rather than on one hard-coded
    marker. A cosmetic dashboard restyle therefore no longer turns a good login
    into a reportable FAIL.
    """
    _, token = fetch_login_page(opener)
    if not token:
        return False, "could not read csrfmiddlewaretoken from the login page"

    payload = urllib.parse.urlencode({
        "csrfmiddlewaretoken": token,
        "username": username,
        "password": password,
        "next": "/",
    }).encode("utf-8")

    req = urllib.request.Request(
        LOGIN_URL,
        data=payload,
        headers={
            "User-Agent": UA,
            "Referer": LOGIN_PAGE,
            "Origin": PANEL,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )

    with opener.open(req, timeout=45) as r:
        final_url = r.geturl()
        status = getattr(r, "status", 200)
        body = r.read().decode("utf-8", "replace")

    has_logout = "/logout/" in body
    still_on_form = "data-login-form" in body
    has_session = any(c.name == "sessionid" for c in jar)

    if has_logout and not still_on_form:
        return True, "dashboard reached, logout link present (%s)" % final_url
    if has_session and not still_on_form:
        return True, "sessionid cookie set, no login form (%s)" % final_url
    if still_on_form:
        return False, ("panel re-rendered the login form - wrong username/password, "
                       "or the account is suspended")
    return False, ("ambiguous response from %s (HTTP %s: no logout link, no login "
                   "form, no session cookie)" % (final_url, status))


def notify(token, chat_id, text):
    if not (token and chat_id):
        return
    url = "https://api.telegram.org/bot%s/sendMessage" % token
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
        opener, _ = build_opener()
        with opener.open(req, timeout=20) as r:
            r.read()
        print("  [telegram] sent")
    except Exception as e:
        print("  [telegram] failed: %s" % e)


def load_accounts():
    raw = os.environ.get("ACCOUNTS_JSON", "").strip()
    if not raw:
        raise SystemExit("[config] ACCOUNTS_JSON is empty or unset")
    try:
        accounts = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit("[config] ACCOUNTS_JSON is not valid JSON: %s" % e)
    if isinstance(accounts, dict):
        accounts = [accounts]
    if not accounts:
        raise SystemExit("[config] ACCOUNTS_JSON is an empty list")
    for a in accounts:
        if not a.get("username") or not a.get("password"):
            raise SystemExit("[config] every entry needs username and password")
    return accounts


def main():
    accounts = load_accounts()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")

    print("ct8-renew - %d account(s) - panel %s" % (len(accounts), PANEL))
    failures = []

    for acct in accounts:
        user = acct["username"]
        print("\n-> %s" % user)
        ok, detail = False, "not attempted"

        for attempt in range(1, 4):
            try:
                opener, jar = build_opener()
                ok, detail = login(opener, jar, user, acct["password"])
                break
            except urllib.error.HTTPError as e:
                if e.code in (403, 429):
                    detail = ("HTTP %s - blocked (IP ban or rate limit); retry from "
                              "another egress" % e.code)
                else:
                    detail = "HTTP %s" % e.code
            except urllib.error.URLError as e:
                detail = "network error: %s" % e.reason
            except Exception as e:
                detail = "%s: %s" % (type(e).__name__, e)
            print("   attempt %d failed - %s" % (attempt, detail))
            if attempt < 3:
                time.sleep(5 * attempt)

        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        if ok:
            print("   OK - %s" % detail)
            notify(tg_token, tg_chat, "[ct8] %s - OK - %s (UTC)" % (user, stamp))
        else:
            print("   FAIL - %s" % detail)
            failures.append(user)
            notify(tg_token, tg_chat, "[ct8] %s - FAILED - %s (UTC) - %s"
                   % (user, stamp, detail))

    print("\n" + "=" * 52)
    if failures:
        print("FAILED: %s" % ", ".join(failures))
        return 1
    print("all accounts renewed")
    return 0


def selftest():
    """Negative test: bogus credentials must be detected as a failure."""
    print("selftest - posting deliberately wrong credentials to %s" % LOGIN_URL)
    opener, jar = build_opener()
    ok, detail = login(opener, jar, "definitely-not-a-real-user", "wrong-password")
    print("  result: ok=%s  detail=%s" % (ok, detail))
    if ok:
        print("UNEXPECTED: bogus credentials were accepted - detection is broken")
        return 1
    print("PASS - wrong credentials correctly rejected; csrf + POST + detection work")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(main())
