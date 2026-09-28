"use strict";

const closeButton = document.getElementById("close-window");
if (closeButton) {
  closeButton.addEventListener("click", () => {
    const api = window.pywebview && window.pywebview.api;
    if (api && typeof api.close_window === "function") {
      api.close_window();
      return;
    }

    const handler = window.webkit && window.webkit.messageHandlers &&
      window.webkit.messageHandlers.nlinuxCloseWindow;
    if (handler) {
      handler.postMessage("close");
      return;
    }

    window.close();
  });
}
