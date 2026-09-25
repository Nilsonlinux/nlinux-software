import os
import threading
import webbrowser
from http.server import ThreadingHTTPServer

# WebKitGTK sandbox workaround (bubblewrap fails in this container; compositing
# stays ENABLED so the page paints reliably — disabling it caused reload-loops).
os.environ.setdefault("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS", "1")
# XWayland backend: reliable window mapping under the Umbriel compositor.
os.environ.setdefault("GDK_BACKEND", "x11")

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gio, Gtk, WebKit2

from nlinux.web_server import BoutiqueHandler, build_payload


_INJECT = """(function(){
  if (window.__injDone) return;
  var base = 'http://127.0.0.1:%(port)s/api/log';
  function post(m){ try{ var x=new XMLHttpRequest(); x.open('POST',base,true); x.setRequestHeader('Content-Type','application/json'); x.send(JSON.stringify({msg:String(m).slice(0,400)})); }catch(e){} }
  window.addEventListener('error', function(ev){
    var el = ev.target;
    post('INJECT_ERR: '+ev.message+' @'+(ev.filename||'')+':'+(ev.lineno||'')+' el='+((el&&el.tagName)||'')+' src='+((el&&el.src)||(el&&el.href)||''));
  }, true);
  window.addEventListener('unhandledrejection', function(ev){ post('INJECT_REJ: '+(ev.reason||{}).message||ev.reason); });
  window.addEventListener('load', function(){
    setTimeout(function(){
      var b=document.body, cs=b?getComputedStyle(b):null;
      var store=document.getElementById('store-name');
      var cards=document.querySelectorAll('.card');
      var sh0=document.styleSheets[0];
      var rules=0, shref='none';
      try{ rules=sh0.cssRules.length; shref=sh0.href||'inline'; }catch(e){ rules=-2; }
      var varbg=''; try{ varbg=getComputedStyle(document.documentElement).getPropertyValue('--bg').trim(); }catch(e){}
      var p1='', p2='', p3='';
      try{ var el=document.elementFromPoint(document.documentElement.clientWidth/2, 90);
        p1=(el?el.tagName+'.'+el.className.toString().slice(0,40)+' pe='+getComputedStyle(el).pointerEvents+' dis='+getComputedStyle(el).display:'none'); }catch(e){ p1='ERR'; }
      try{ var el2=document.elementFromPoint(document.documentElement.clientWidth/2, 360);
        p2=(el2?el2.tagName+'.'+el2.className.toString().slice(0,30):'none'); }catch(e){ p2='ERR'; }
      post('INJECT_REPORT ready='+document.readyState+' title='+JSON.stringify(document.title)+
        ' css='+document.styleSheets.length+' sheet='+shref+' rules='+rules+
        ' varBg='+JSON.stringify(varbg)+' bodyBg='+(cs?cs.backgroundColor:'n/a')+
        ' bodyChild='+(b?b.children.length:-1)+' cards='+cards.length+
        ' store='+(store?JSON.stringify(store.textContent):'none')+
        ' w='+document.documentElement.clientWidth+' h='+document.documentElement.clientHeight+
        ' hit1='+p1+' hit2='+p2);
    }, 3500);
  });
  window.__injDone = true;
})();
"""


def _make_user_script(src):
    args = [
        ("frames_jtime", None),
        ("frames_jtime_blacklist", None),
    ]
    try:
        return WebKit2.UserScript(
            src,
            WebKit2.UserContentInjectedFrames.ALL_FRAMES,
            WebKit2.UserScriptInjectionTime.START,
            [],
        )
    except Exception:
        try:
            return WebKit2.UserScript(
                src,
                WebKit2.UserContentInjectedFrames.ALL_FRAMES,
                WebKit2.UserScriptInjectionTime.START,
                [],
                [],
            )
        except Exception as e:
            raise RuntimeError(f"UserScript API indisponível: {e}") from e


def _build_view(inject, port):
    manager = WebKit2.UserContentManager()
    if inject:
        manager.add_script(_make_user_script(inject))
    return WebKit2.WebView.new_with_user_content_manager(manager)


def run_window() -> None:
    with BoutiqueHandler.payload_lock:
        BoutiqueHandler.payload = build_payload()

    server = ThreadingHTTPServer(("127.0.0.1", 0), BoutiqueHandler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Loja de Software NLinux em {url}", flush=True)
    print("Feche a janela para encerrar.", flush=True)

    try:
        from nlinux.web_server import ADMIN_ENABLED, ensure_admin_shortcut
        if ADMIN_ENABLED:
            try:
                ensure_admin_shortcut()
            except Exception:
                pass
        if ADMIN_ENABLED:
            app_id = "io.github.nilsonlinux.NLinuxCuradoria"
            wm_class = "nlinuxcuradoria"
            icon_file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "icon-admin.svg")
        else:
            app_id = "io.github.nilsonlinux.NLinuxStore"
            wm_class = "nlinuxstore"
            icon_file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "icon-dist.svg")
        GLib.set_prgname(wm_class)
        try:
            from gi.repository import Gdk
            Gdk.set_program_class(wm_class)
        except Exception:
            pass
        app = Gtk.Application(
            application_id=app_id, flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.register(None)
        window = Gtk.ApplicationWindow(
            application=app, title="Loja de Software NLinux")
        window.set_wmclass(wm_class, wm_class)
        try:
            from gi.repository import GdkPixbuf
            window.set_icon(GdkPixbuf.Pixbuf.new_from_file_at_scale(
                icon_file, 128, 128, True))
        except Exception:
            pass
        window.set_default_size(1180, 820)
        window.set_position(Gtk.WindowPosition.CENTER)
        window.set_border_width(0)

        view = _build_view(_INJECT % {"port": port}, port)
        settings = view.get_settings()

        def _set(name, value):
            fn = getattr(settings, name, None)
            if fn:
                try:
                    fn(value)
                except Exception:
                    pass

        _set("set_enable_javascript", True)
        _set("set_enable_local_storage", True)
        _set("set_enable_webgl", False)
        _set("set_enable_developer_extras", True)
        _set("set_enable_plugins", False)

        # ---- diagnostics -------------------------------------------------
        def _diag(*parts):
            line = " ".join(str(p) for p in parts)
            print(line, flush=True)
            try:
                with open("/tmp/webkit.log", "a") as f:
                    f.write(line + "\n")
            except Exception:
                pass

        def _on_load_changed(webview, event):
            try:
                _diag("LOAD_EVENT", event.value_name, "uri=", webview.get_uri())
            except Exception as e:
                _diag("LOAD_EVENT err", repr(e))

        def _on_load_failed(webview, event, uri, error):
            _diag("LOAD_FAILED", uri, "->", repr(error))

        def _on_resource_fail(webview, resource, request, err):
            pass  # not used (signal may be unavailable)

        def _on_console(webview, message):
            _diag("JS_CONSOLE:", message.get_message())

        def _on_resource_start(webview, resource, request):
            try:
                _diag("RESOURCE_START", request.get_uri())
            except Exception as e:
                _diag("RESOURCE_START err", repr(e))

        view.connect("load-changed", _on_load_changed)
        view.connect("load-failed", _on_load_failed)
        try:
            view.connect("console-message", _on_console)
        except Exception:
            pass
        try:
            view.connect("resource-load-started", _on_resource_start)
        except Exception:
            pass

        def _open_external(uri):
            if uri.startswith(("http://127.0.0.1:", "about:")):
                return False
            try:
                webbrowser.open(uri)
            except Exception:
                pass
            return True

        def on_decide_policy(webview, decision, decision_type):
            if decision_type == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
                try:
                    uri = decision.get_navigation_action().get_request().get_uri()
                except Exception:
                    return False
                if _open_external(uri):
                    decision.ignore()
                    return True
                return False
            if decision_type != WebKit2.PolicyDecisionType.NAVIGATION_ACTION:
                return False
            try:
                nav = decision.get_navigation_action()
                uri = nav.get_request().get_uri()
            except Exception:
                return False
            if _open_external(uri):
                decision.ignore()
                return True
            return False

        view.connect("decide-policy", on_decide_policy)

        window.connect("destroy", lambda *_: app.quit())
        app.connect("activate", lambda *_: window.present())

        window.add(view)
        view.load_uri(url)
        window.show_all()
        window.present()

        def _raise():
            try:
                window.present()
            except Exception:
                pass

        GLib.timeout_add(500, _raise)

        app.run([])
    except Exception as e:  # window failed to open: report, do NOT spawn a browser
        print(f"Não foi possível abrir a janela nativa: {e}", flush=True)
        server.shutdown()
        server.server_close()
        return

    server.shutdown()
    server.server_close()
    print("Encerrado.", flush=True)


if __name__ == "__main__":
    run_window()