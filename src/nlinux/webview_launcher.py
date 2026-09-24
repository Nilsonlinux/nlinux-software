import os
import threading
import webbrowser
from http.server import ThreadingHTTPServer

# WebKitGTK's inner bubblewrap sandbox fails on this system (user namespaces
# are blocked -> blank page + crash). Disable it: it only runs localhost
# content served by our own server, so the risk is negligible.
os.environ.setdefault("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS", "1")
# Software compositing: avoids GPU/DMA-BUF rendering crashes and blank pages
# on compositors/drivers with quirks (Umbriel included).
os.environ.setdefault("WEBKIT_DISABLE_COMPOSITING_MODE", "1")
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
# Force the X server (XWayland) backend: reliable window handling/decorations
# under the Umbriel compositor.
os.environ.setdefault("GDK_BACKEND", "x11")

from nlinux.web_server import BoutiqueHandler, build_payload


class Api:
    """Bridging object exposed to the WebView page (window.pywebview.api)."""

    def open_external(self, url: str) -> None:
        try:
            webbrowser.open(url)
        except Exception:
            pass


def run_webview() -> None:
    try:
        import webview
    except ImportError:
        print("pywebview não está instalado; abrindo no navegador padrão.")
        from nlinux.web_server import run

        run(open_browser=True)
        return

    with BoutiqueHandler.payload_lock:
        BoutiqueHandler.payload = build_payload()

    server = ThreadingHTTPServer(("127.0.0.1", 0), BoutiqueHandler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"Loja de Software NLinux em {url}")
    print("Feche a janela para encerrar.")

    try:
        window = webview.create_window(
            "Loja de Software NLinux",
            url,
            width=1120,
            height=780,
            min_size=(900, 620),
            background_color="#070722",
            js_api=Api(),
        )
        window.events.loaded += lambda: print("WEBVIEW_PAGELOADED", flush=True)
        window.events.shown += lambda: print("WEBVIEW_SHOWN", flush=True)

        def _focus():
            try:
                window.show(focus=True)
                window.focus()
            except Exception:
                pass

        window.events.loaded += _focus
        window.events.shown += _focus
        window.events.closed += server.shutdown
        webview.start(gui="gtk")
    except Exception as e:
        print(f"Não foi possível abrir a janela nativa ({e}); tentando o navegador padrão.")
        server.shutdown()
        server.server_close()
        from nlinux.web_server import run

        run(open_browser=True)
        return

    server.server_close()


if __name__ == "__main__":
    run_webview()