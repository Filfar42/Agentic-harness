"""Le risposte asincrone non devono cancellare un QR appena generato."""

from pathlib import Path

import quickjs


def context():
    ctx = quickjs.Context()
    ctx.eval("const document = { addEventListener() {} };")
    ctx.eval((Path(__file__).parents[1] / "web" / "impostazioni.js").read_text(encoding="utf-8"))
    ctx.eval("""
      let open = true;
      let timers = [];
      let requests = [];
      let renders = 0;
      function api(path, options) {
        return new Promise((resolve, reject) => requests.push({path, options, resolve, reject}));
      }
      function setTimeout(fn) { timers.push(fn); return timers.length; }
      function clearTimeout() { timers = []; }
      impAperta = () => open;
      impDisegnaMobile = () => { renders++; };
      Imp.sezione = 'mobile';
    """)
    return ctx


def drain(ctx):
    while ctx.execute_pending_job():
        pass


def test_stale_poll_cannot_erase_new_qr():
    ctx = context()
    ctx.eval("impCaricaMobile(); impAzioneMobile('genera');")
    assert ctx.eval("requests.length") == 2
    ctx.eval("requests[1].resolve({qr:'new-qr', expires_at:12345});")
    drain(ctx)
    assert ctx.eval("requests.length") == 3
    ctx.eval("requests[0].resolve({device:null, pending_expires_at:null});")
    drain(ctx)
    assert ctx.eval("Imp.mobileQr.qr") == "new-qr"
    ctx.eval("requests[2].resolve({device:null, pending_expires_at:12345});")
    drain(ctx)
    assert ctx.eval("Imp.mobileQr.qr") == "new-qr"
    assert ctx.eval("timers.length") == 1
    assert ctx.eval("Imp.mobileBusy || Imp.mobileLoading") is False


def test_consumed_or_replaced_qr_disappears():
    ctx = context()
    ctx.eval("Imp.mobileQr = {qr:'old', expires_at:12345}; impCaricaMobile();")
    ctx.eval("requests[0].resolve({device:{name:'iPhone'}, pending_expires_at:null});")
    drain(ctx)
    assert ctx.eval("Imp.mobileQr") is None
    assert ctx.eval("Imp.mobile.device.name") == "iPhone"


def test_poll_does_not_restart_after_leaving_settings():
    ctx = context()
    ctx.eval("impCaricaMobile(); open = false; impFermaMobile();")
    ctx.eval("requests[0].resolve({device:null, pending_expires_at:null});")
    drain(ctx)
    assert ctx.eval("timers.length") == 0


def test_action_error_is_visible_and_buttons_unlock():
    ctx = context()
    ctx.eval("impAzioneMobile('genera'); requests[0].reject(new Error('Porta occupata'));")
    drain(ctx)
    assert ctx.eval("Imp.mobileError") == "Porta occupata"
    assert ctx.eval("Imp.mobileBusy") is False
    assert ctx.eval("Imp.mobileQr") is None
