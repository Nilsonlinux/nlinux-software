# SPDX-License-Identifier: GPL-3.0-or-later
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
    manager.register_script_message_handler("nlinuxCloseWindow")
    if inject:
        manager.add_script(_make_user_script(inject))
    return WebKit2.WebView.new_with_user_content_manager(manager), manager


def _create_tray(app_id, window, icon_file, quit_app):
    """Ícone na bandeja, para a loja continuar viva com a janela fechada.

    Devolve o indicador criado, ou None quando a sessão não tem bandeja. Nesse
    caso quem chama mantém o comportamento antigo: fechar a janela encerra.
    """
    def mostrar(*_args):
        window.show_all()
        window.present()

    menu = Gtk.Menu()
    abrir = Gtk.MenuItem(label="Abrir loja")
    abrir.connect("activate", mostrar)
    sair = Gtk.MenuItem(label="Sair da loja")
    sair.connect("activate", lambda *_: quit_app())
    menu.append(abrir)
    menu.append(Gtk.SeparatorMenuItem())
    menu.append(sair)
    menu.show_all()

    # StatusNotifier é o caminho das sessões moderne (GNOME, KDE, Noctalia...).
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3

        indicador = AppIndicator3.Indicator.new(
            app_id, icon_file, AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
        indicador.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        indicador.set_title("Loja de Software NLinux")
        indicador.set_menu(menu)
        print("Bandeja: ícone de notificação (StatusNotifier).", flush=True)
        return indicador
    except Exception as e:
        print(f"StatusNotifier indisponível ({e}); tentando Gtk.StatusIcon.",
              flush=True)

    # Sem SNI sobra o ícone antigo da GTK 3, que depende de um painel com bandeja.
    try:
        icone = Gtk.StatusIcon.new_from_file(icon_file)
        icone.set_tooltip_text("Loja de Software NLinux")
        icone.connect("activate", mostrar)

        def _popup(_icon, button, _time):
            menu.popup(None, None, None, None, button, _time)

        icone.connect("popup-menu", _popup)
        print("Bandeja: Gtk.StatusIcon.", flush=True)
        return icone
    except Exception as e:
        print(f"Sem bandeja disponível ({e}); a janela encerra o programa.", flush=True)
        return None


def _hide_tray(tray):
    if tray is None:
        return
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3

        if isinstance(tray, AppIndicator3.Indicator):
            tray.set_status(AppIndicator3.IndicatorStatus.PASSIVE)
            return
    except Exception:
        pass
    try:
        tray.set_visible(False)
    except Exception:
        pass


def run_window() -> None:
    with BoutiqueHandler.payload_lock:
        BoutiqueHandler.payload = build_payload()

    server = ThreadingHTTPServer(("127.0.0.1", 0), BoutiqueHandler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Loja de Software NLinux em {url}", flush=True)

    try:
        from nlinux.web_server import (ADMIN_ENABLED, ensure_admin_shortcut,
                                        start_remote_refresh)
        if ADMIN_ENABLED:
            try:
                ensure_admin_shortcut()
            except Exception:
                pass
        else:
            start_remote_refresh()
        if ADMIN_ENABLED:
            app_id = "io.github.nilsonlinux.NLinuxCuradoria"
            wm_class = "nlinuxcuradoria"
        else:
            app_id = "io.github.nilsonlinux.NLinuxStore"
            wm_class = "nlinuxstore"
        # ícone único do projeto: a loja e a curadoria usam o mesmo arquivo
        icon_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "apps", "nlinux-logo.png")
        GLib.set_prgname(wm_class)
        try:
            from gi.repository import Gdk
            Gdk.set_program_class(wm_class)
        except Exception:
            pass
        try:
            app = Gtk.Application(
                application_id=app_id, flags=Gio.ApplicationFlags.FLAGS_NONE)
            app.register(None)
            if app.get_is_remote():
                # Já existe uma loja rodando: mostra a dela e encerra esta.
                print("A loja já está aberta; mostrando a janela existente.",
                      flush=True)
                app.activate()
                server.shutdown()
                server.server_close()
                return
        except Exception as e:  # sessão sem barramento: aceitamos duas janelas
            print(f"Instância única indisponível ({e}).", flush=True)
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

        view, content_manager = _build_view(_INJECT % {"port": port}, port)

        tray = None

        def _quit_app():
            _hide_tray(tray)
            try:
                window.destroy()
            except Exception:
                pass
            app.quit()

        def _hide_or_quit():
            if tray is not None:
                window.hide()  # continua na bandeja
            else:
                GLib.idle_add(window.destroy)

        def _on_delete(_window, _event):
            if tray is not None:
                window.hide()
                return True  # segura o fechamento: quem encerra é o menu
            return False

        tray = _create_tray(app_id, window, icon_file, _quit_app)
        window.connect("delete-event", _on_delete)
        if tray is not None:
            print("Fechar a janela deixa a loja na bandeja; use 'Sair da loja' "
                  "para encerrar.", flush=True)
        else:
            print("Feche a janela para encerrar.", flush=True)

        def _close_window(_manager, message):
            try:
                command = message.get_js_value().to_string()
            except Exception as exc:
                print(f"Mensagem de fechamento inválida: {exc}", flush=True)
                return
            if command == "close":
                GLib.idle_add(_hide_or_quit)

        content_manager.connect(
            "script-message-received::nlinuxCloseWindow", _close_window)
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

        def _activate(*_args):  # loja na bandeja, aberta pelo ícone do sistema
            window.show_all()
            window.present()

        app.connect("activate", _activate)

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