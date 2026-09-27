"""Il rientro dallo shortcut usa il cookie, senza riutilizzare il codice QR."""

import json
import re
from pathlib import Path

import quickjs
import pytest

WEB = Path(__file__).parents[1] / "web_mobile"


def page(name, status, code=""):
    ctx = quickjs.Context()
    ctx.eval("""
      let nodes = {};
      let navigations = [];
      let requests = [];
      let document = { getElementById(id) {
        return nodes[id] || (nodes[id] = {disabled: true, textContent: ''});
      }};
      let history = {replaceState() {}};
      let location = {hash: '', replace(url) { navigations.push(url); }};
      let responseStatus;
      async function fetch(url, options) {
        requests.push({url, options});
        if (responseStatus === 0) throw new Error('offline');
        return {ok: responseStatus === 200, status: responseStatus, json: async () => ({ok:true})};
      }
    """)
    ctx.eval(f"responseStatus = {status}; location.hash = {json.dumps('#' + code if code else '')};")
    script = re.search(r"<script>(.*?)</script>", (WEB / name).read_text(encoding="utf-8"), re.S)
    assert script
    ctx.eval(script[1])
    while ctx.execute_pending_job():
        pass
    return ctx


@pytest.mark.parametrize("name", ["access.html", "pair.html"])
def test_existing_device_enters_chat_from_root_or_old_qr(name):
    ctx = page(name, 200, code="used-qr")
    assert ctx.eval("JSON.stringify(navigations)") == '["/"]'
    assert ctx.eval("requests.length") == 1
    assert ctx.eval("requests[0].url") == "/api/mobile/session"
    assert ctx.eval("requests[0].options.credentials") == "same-origin"
    assert ctx.eval("requests[0].options.method") == "POST"


def test_missing_or_revoked_cookie_never_redirects_or_retries_forever():
    ctx = page("access.html", 401)
    assert ctx.eval("nodes.title.textContent") == "Associazione richiesta"
    assert ctx.eval("navigations.length") == 0
    assert ctx.eval("requests.length") == 1
    assert ctx.eval("nodes.retry.disabled") is False


@pytest.mark.parametrize("status", [0, 503])
def test_network_failure_is_distinct_from_an_unpaired_device(status):
    ctx = page("access.html", status)
    assert ctx.eval("nodes.title.textContent") == "Collegamento non disponibile"
    assert ctx.eval("navigations.length") == 0
    assert ctx.eval("nodes.retry.disabled") is False


@pytest.mark.parametrize("code,disabled", [("unused-qr", False), ("", True)])
def test_only_a_new_qr_can_offer_pairing_when_cookie_is_missing(code, disabled):
    ctx = page("pair.html", 401, code)
    assert ctx.eval("navigations.length") == 0
    assert ctx.eval("nodes.pair.disabled") is disabled
