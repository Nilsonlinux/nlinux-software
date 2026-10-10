def _pick_runner():
    try:
        from nlinux.native_window import run_window

        return run_window
    except Exception as e:  # no introspection/Gtk available
        print(f"Janela nativa indisponível ({e}); tentando pywebview.")
    try:
        from nlinux.webview_launcher import run_webview

        return run_webview
    except Exception as e:
        print(f"pywebview indisponível ({e}).")
        from nlinux.web_server import run

        return lambda: run(open_browser=False)


if "__main__" == __name__:
    _pick_runner()()