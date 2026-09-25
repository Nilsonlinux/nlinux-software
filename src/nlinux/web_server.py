import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from nlinux import resources
from nlinux.system_state import SystemState

SRC_ROOT = resources.SRC_ROOT
APPS_DIR = os.path.join(SRC_ROOT, "apps")
WEB_DIR = os.path.join(SRC_ROOT, "assets", "web")
DIST_DIR = os.path.join(os.path.dirname(SRC_ROOT), "dist")

# Desativada na versão de distribuição (gerada pela página de administração).
ADMIN_ENABLED = False

# Publicação automática no GitHub quando o build da versão da loja terminar.
# Desligue com NLINUX_GIT_PUSH=0. O repositório local é clonado no GIT_PUSH_DIR
# na primeira vez (pede a senha do GitHub uma única vez).
GIT_PUSH_ENABLED = os.environ.get("NLINUX_GIT_PUSH", "1") not in ("0", "false", "no")
GIT_PUSH_URL = os.environ.get("NLINUX_GIT_URL") or \
    "https://github.com/Nilsonlinux/nlinux-software.git"
GIT_PUSH_BRANCH = os.environ.get("NLINUX_GIT_BRANCH") or "main"
GIT_PUSH_DIR = os.environ.get("NLINUX_GIT_DIR") or \
    os.path.join(os.path.expanduser("~"), "nlinux-repo")
GIT_PUSH_USER = os.environ.get("NLINUX_GIT_USER") or "Nilsonlinux"
GIT_PUSH_EMAIL = os.environ.get("NLINUX_GIT_EMAIL") or "nilsonlinux@users.noreply.github.com"
PACKAGE_DEPS = [
    "python",
    "python-gobject",
    "gtk3",
    "webkit2gtk-4.1",
    "polkit",
    "gnupg",
]
PACKAGE_DEPS_OPTIONAL = ("paru", "yay", "curl")

# Catálogo remoto (JSON raw do GitHub). APENAS a versão de distribuição sincroniza
# (a de curadoria é a fonte e não deve ser sobrescrita — ver start_remote_refresh).
# Ajuste a URL para o seu repositório após publicar o catálogo no GitHub.
REMOTE_CATALOG_URL = os.environ.get("NLINUX_CATALOG_URL") or \
    "https://raw.githubusercontent.com/nilsonlinux/nlinux-software/main/src/apps/applications-en.json"
# Arquivo-marcador minúsculo publicado junto com o catálogo. A loja lê só ele a
# cada REMOTE_CHECK_SECONDS e só baixa o catálogo inteiro (236 KB) quando a
# impressão digital muda. O CDN do raw.githubusercontent responde 304 obsoleto
# para requisições condicionais, então não dá para usar If-None-Match aqui.
REMOTE_MARKER_URL = os.environ.get("NLINUX_CATALOG_MARKER_URL") or \
    "https://raw.githubusercontent.com/nilsonlinux/nlinux-software/main/catalog-head.json"
try:
    REMOTE_CHECK_SECONDS = max(5, int(os.environ.get("NLINUX_CATALOG_CHECK", "15")))
except ValueError:
    REMOTE_CHECK_SECONDS = 15

STORE_AUTHOR = "Nilsonlinux"
STORE_REPO_URL = "https://github.com/Nilsonlinux/nlinux-software"
STORE_REPO_NAME = "Nilsonlinux/nlinux-software"

MIME = {
    "html": "text/html; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "ico": "image/x-icon",
    "txt": "text/plain; charset=utf-8",
}


def pick_index_file() -> str:
    try:
        locale = l18n.getlocale()[0]
    except Exception:
        locale = "en_US"
    candidates = [locale]
    if "_" in locale:
        candidates.append(locale.split("_")[0])
    candidates.append("en")
    for candidate in candidates:
        path = os.path.join(APPS_DIR, f"applications-{candidate}.json")
        if os.path.exists(path):
            return f"applications-{candidate}.json"
    return "applications-en.json"


def pacman_installed() -> set:
    try:
        out = subprocess.run(
            ["pacman", "-Qq"], capture_output=True, text=True, timeout=30
        )
        return set(out.stdout.split())
    except Exception:
        return set()


class InstallJob:
    def __init__(self, job_id: str, packages: list, source: str = "arch",
                 mode: str = "install") -> None:
        self.id = job_id
        self.packages = packages
        self.source = source
        self.mode = mode
        self.state = "pending"
        self.lines = []
        self.done = False
        self.success = False

    def _aur_script_by_pkg(packages) -> None:
        """Instala pacotes AUR via paru OU yay (o que existir) como usuario,
        com NOPASSWD temporario apenas para o passo final de instalacao."""
        inner_path = f"/tmp/boutique-aur-{self.id}.sh"
        with open(inner_path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/bash\n")
            fh.write("AUR_HELPER=\"$(command -v paru || command -v yay)\"\n")
            fh.write("[ -n \"$AUR_HELPER\" ] || { echo \"AUR helper ausente (instale paru ou yay).\" >&2; exit 2; }\n")
            fh.write("\"$AUR_HELPER\" -S --noconfirm --needed ")
            fh.write(" ".join(shlex.quote(p) for p in self.packages))
            fh.write("\n")
        os.chmod(inner_path, 0o700)
        script = "\n".join([
            "set +e",
            "umask 077",
            "limite='/etc/sudoers.d/zz-boutique'",
            "printf '%s\\n' 'nilsonlinux ALL=(ALL) NOPASSWD: /usr/bin/pacman' > \"$limite\"",
            "chmod 0440 \"$limite\"",
            "visudo -cf \"$limite\" >/dev/null 2>&1",
            f"cleanup() {{ rm -f \"$limite\" \"{inner_path}\"; }}",
            "trap cleanup EXIT INT TERM",
            f"su - -s /bin/bash nilsonlinux -c 'bash {inner_path}'",
            "exit $?",
        ])
        path = f"/tmp/boutique-aur-{self.id}.sh"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        return path, script, inner_path

    def start(self) -> None:
        def work() -> None:
            names = ", ".join(self.packages)
            removing = self.mode == "remove"
            self.lines.append(
                f"{'Desinstalando' if removing else 'Instalando'} "
                f"({self.source}): {names}")
            artifacts = []
            try:
                if removing:
                    cmd = ["pkexec", "pacman", "-Rns", "--noconfirm"] + self.packages
                elif self.source == "aur":
                    script_path, _, inner_path = self._aur_script()
                    artifacts = [script_path, inner_path]
                    cmd = ["pkexec", "bash", script_path]
                else:
                    cmd = ["pkexec", "pacman", "-S", "--noconfirm", "--needed"] + self.packages
                process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
                )
                if process.stdout is not None:
                    for line in iter(process.stdout.readline, ""):
                        line = line.rstrip()
                        if line:
                            self.lines.append(line)
                self.success = process.wait() == 0
            except Exception as e:
                self.success = False
                self.lines.append(f"Falha ao iniciar o instalador: {e}")
            finally:
                for a in artifacts:
                    try:
                        os.unlink(a)
                    except OSError:
                        pass
            self.state = "success" if self.success else "failed"
            if self.success:
                self.lines.append(f"{'Removido' if removing else 'Instalado'}: {names}")
            else:
                self.lines.append(
                    f"Falha ao {'desinstalar' if removing else 'instalar'}: {names}")
            self.done = True

        threading.Thread(target=work, daemon=True).start()

    def status(self) -> dict:
        return {
            "id": self.id,
            "packages": self.packages,
            "mode": self.mode,
            "state": self.state,
            "done": self.done,
            "success": self.success,
            "lines": self.lines[-12:],
        }


class BoutiqueHandler(BaseHTTPRequestHandler):
    payload = None
    payload_lock = threading.Lock()
    jobs = {}
    jobs_lock = threading.Lock()
    systemState = SystemState()
    js_log = []
    js_log_lock = threading.Lock()
    server_version = "NLinuxBoutique/1.0"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # ---- helpers ----------------------------------------------------------

    def _send_json(self, data: dict, code: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", MIME["json"])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: str, ctype: str = None) -> None:
        if not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        if ctype is None:
            ext = path.rsplit(".", 1)[-1].lower()
            ctype = MIME.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _safe_media(self, rel: str) -> str:
        if not rel or rel.startswith("/") or ".." in rel:
            return ""
        return os.path.join(APPS_DIR, rel)

    # ---- routes -----------------------------------------------------------

    def do_GET(self) -> None:
        from urllib.parse import parse_qs, urlparse

        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self._serve_file(os.path.join(WEB_DIR, "index.html"))
            return

        if path == "/admin":
            if not ADMIN_ENABLED:
                self._send_json({"error": "not found"}, 404)
                return
            self._serve_file(os.path.join(WEB_DIR, "admin.html"))
            return

        if path == "/api/admin/catalog":
            if not ADMIN_ENABLED:
                self._send_json({"error": "not found"}, 404)
                return
            self._send_json(admin_catalog())
            return

        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            if ".." in rel or rel.startswith("/"):
                self._send_json({"error": "bad path"}, 400)
                return
            self._serve_file(os.path.join(WEB_DIR, rel))
            return

        if path.startswith("/media/"):
            media = self._safe_media(path[len("/media/"):])
            if not media:
                self._send_json({"error": "bad path"}, 400)
                return
            self._serve_file(media)
            return

        if path == "/api/index":
            with self.payload_lock:
                if self.payload is None:
                    self.payload = build_payload()
            self._send_json(self.payload)
            return

        if path == "/api/log":
            self._send_json({"logs": list(self.js_log[-60:])})
            return

        if path == "/api/status":
            from urllib.parse import parse_qs

            job_id = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            with self.jobs_lock:
                job = self.jobs.get(job_id)
            self._send_json(job.status() if job else {"state": "unknown"})
            return

        self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        from urllib.parse import urlparse

        path = urlparse(self.path).path

        if path == "/api/log":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                msg = str(body.get("msg", ""))[:500]
                if msg:
                    with self.js_log_lock:
                        self.js_log.append(msg)
            except Exception:
                pass
            self._send_json({"ok": True})
            return

        if path == "/api/admin/save":
            if not ADMIN_ENABLED:
                self._send_json({"error": "not found"}, 404)
                return
            data, code = admin_save(self)
            self._send_json(data, code)
            return

        if path == "/api/admin/delete":
            if not ADMIN_ENABLED:
                self._send_json({"error": "not found"}, 404)
                return
            data, code = admin_delete(self)
            self._send_json(data, code)
            return

        if path == "/api/admin/build":
            if not ADMIN_ENABLED:
                self._send_json({"error": "not found"}, 404)
                return
            data, code = admin_build()
            self._send_json(data, code)
            return

        if path != "/api/install":
            self._send_json({"error": "not found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send_json({"error": "invalid body"}, 400)
            return

        packages = body.get("packages") or []
        if not isinstance(packages, list) or not packages:
            self._send_json({"error": "no packages"}, 400)
            return

        source = body.get("source", "arch")
        mode = body.get("action", "install")
        if mode not in ("install", "remove"):
            self._send_json({"error": "invalid action"}, 400)
            return
        job_id = uuid4().hex[:12]
        job = InstallJob(job_id, packages, source, mode)
        with self.jobs_lock:
            self.jobs[job_id] = job
        job.start()
        self._send_json({"id": job_id})

    do_PUT = do_POST  # convenience


def build_payload() -> dict:
    index_path = resources.resource_path(f"apps/{pick_index_file()}")
    with open(index_path) as f:
        raw = json.load(f)

    installed = pacman_installed()
    state = BoutiqueHandler.systemState

    categories = []
    products = []
    for key, value in raw.items():
        if key in ("stats", "distro", "supported") or not isinstance(value, dict):
            continue
        count = len(value)
        categories.append({"id": key, "count": count})
        for name, package in value.items():
            if package.get("listed") is False:
                continue
            methods = package.get("methods", [])
            if "pacman" not in methods:
                continue
            details = package.get("pacman", {}).get("default", {}) or {}
            install_packages = list(details.get("install-packages") or [])
            main_package = details.get("main-package")
            if main_package and main_package not in install_packages:
                install_packages.insert(0, main_package)
            source = details.get("source", "arch")
            products.append(
                {
                    "key": f"{key}/{name}",
                    "category": key,
                    "name": name,
                    "summary": package.get("summary", ""),
                    "description": package.get("description", ""),
                    "developer": package.get("developer-name"),
                    "icon": f"/media/{package.get('icon')}" if package.get("icon") else "",
                    "screenshots": [
                        f"/media/{s}" for s in package.get("screenshots", []) if s
                    ],
                    "packages": install_packages,
                    "source": source,
                    "arches": package.get("arch", []),
                    "proprietary": bool(package.get("proprietary")),
                    "website": (package.get("urls") or {}).get("info"),
                    "launch": package.get("launch-cmd"),
                    "installed": bool(
                        main_package and main_package in installed
                    ),
                }
            )

    stats = dict(raw.get("stats", {}))
    stats["catalog_hash"] = hashlib.sha1(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]

    return {
        "system": {
            "name": state.name,
            "id": state.distro,
            "arch": state.arch,
            "version": state.os_version,
        },
        "stats": stats,
        "store": {
            "author": STORE_AUTHOR,
            "repo": STORE_REPO_URL,
            "repo_name": STORE_REPO_NAME,
            "version": raw.get("stats", {}).get("version"),
            "revision": raw.get("stats", {}).get("revision"),
            "updated": raw.get("stats", {}).get("compiled"),
        },
        "categories": categories,
        "products": products,
        "total": len(products),
        "admin": ADMIN_ENABLED,
    }


def mark_installed(packages: list) -> None:
    with BoutiqueHandler.payload_lock:
        if BoutiqueHandler.payload is None:
            return
        for product in BoutiqueHandler.payload["products"]:
            if set(product["packages"]) & set(packages):
                product["installed"] = True


# ============================= Administração ================================

_ADMIN_LOCK = threading.Lock()
SPECIAL_KEYS = ("stats", "distro", "supported")
ASSETS_DIR = os.path.join(APPS_DIR, "assets")
IMG_RE = re.compile(r"^data:image/(png|jpeg|webp);base64,", re.IGNORECASE)
ASSET_RE = re.compile(r"^assets/[a-z0-9][a-z0-9._-]*\.(png|jpe?g|webp)$", re.IGNORECASE)
APP_KEY_ORDER = [
    "name", "summary", "description", "icon", "screenshots",
    "developer-name", "developer-url", "urls", "launch-cmd",
    "arch", "releases", "methods", "pacman", "proprietary",
    "alternate-to", "listed",
]
_ASSET_TMP = ".admin"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")[:64]


def _index_path() -> str:
    return resources.resource_path(f"apps/{pick_index_file()}")


def _load_raw() -> dict:
    with open(_index_path(), encoding="utf-8") as fh:
        return json.load(fh)


def _write_raw(raw: dict) -> None:
    out = {}
    for k, v in raw.items():
        if k not in SPECIAL_KEYS:
            out[k] = v
    for k in SPECIAL_KEYS:
        out[k] = raw.get(k, {})
    path = _index_path()
    tmp = path + _ASSET_TMP
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _canonical_app(app: dict) -> dict:
    out = {}
    for k in APP_KEY_ORDER:
        if k in app:
            out[k] = app[k]
    for k, v in app.items():
        if k not in out:
            out[k] = v
    pacman = out.get("pacman")
    if isinstance(pacman, dict):
        defaults = pacman.get("default")
        if not isinstance(defaults, dict):
            defaults = {}
            pacman["default"] = defaults
        for key in ("install-packages", "remove-packages"):
            if key not in defaults:
                defaults[key] = []
        if "main-package" not in defaults:
            defaults["main-package"] = ""
        out["methods"] = ["pacman"]
    return out


def _decode_image(data_url: str, max_bytes: int = 8 * 1024 * 1024):
    m = IMG_RE.match(data_url)
    if not m:
        raise ValueError("imagem inválida (esperava data:image/*;base64)")
    img_type = m.group(1).lower()
    payload = base64.b64decode(data_url.split(",", 1)[1])
    if len(payload) > max_bytes:
        raise ValueError("imagem muito grande (máx 8 MB)")
    ext = {"png": "png", "jpeg": "jpg", "webp": "webp"}[img_type]
    return ext, payload


def _bump_stats(raw: dict) -> None:
    stats = raw.get("stats")
    if not isinstance(stats, dict):
        stats = {}
        raw["stats"] = stats
    cats = [k for k, v in raw.items() if isinstance(v, dict) and k not in SPECIAL_KEYS]
    stats["categories"] = len(cats)
    stats["apps"] = sum(len(raw[c]) for c in cats)
    stats["revision"] = int(stats.get("revision", 0)) + 1
    stats["compiled"] = int(time.time())


def rebuild_payload() -> None:
    with BoutiqueHandler.payload_lock:
        BoutiqueHandler.payload = build_payload()


def _gc_assets(raw: dict) -> None:
    referenced = set()
    for key, items in raw.items():
        if not isinstance(items, dict) or key in SPECIAL_KEYS:
            continue
        for app in items.values():
            if not isinstance(app, dict):
                continue
            if app.get("icon"):
                referenced.add(os.path.basename(str(app.get("icon"))))
            for shot in app.get("screenshots") or []:
                referenced.add(os.path.basename(str(shot)))
    if not os.path.isdir(ASSETS_DIR):
        return
    for fname in os.listdir(ASSETS_DIR):
        if fname == "nlinux-logo.png" or fname in referenced:
            continue
        try:
            os.remove(os.path.join(ASSETS_DIR, fname))
        except OSError:
            pass


def _admin_body(handler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", 0))
        return json.loads(handler.rfile.read(length) or b"{}")
    except Exception:
        return None


def admin_catalog() -> dict:
    raw = _load_raw()
    cats = [k for k, v in raw.items() if isinstance(v, dict) and k not in SPECIAL_KEYS]
    apps = []
    for cat in cats:
        for app_id, app in raw[cat].items():
            if not isinstance(app, dict):
                continue
            icon = app.get("icon") or ""
            apps.append(
                {
                    "category": cat,
                    "id": app_id,
                    "name": app.get("name"),
                    "developer": app.get("developer-name"),
                    "source": (app.get("pacman") or {}).get("default", {}).get("source"),
                    "icon": f"/media/{icon}" if icon else "",
                    "listed": app.get("listed") is not False,
                    "app": app,
                }
            )
    return {"categories": cats, "apps": apps, "stats": dict(raw.get("stats", {}),
            catalog_hash=hashlib.sha1(
                json.dumps(raw, sort_keys=True,
                           ensure_ascii=False).encode("utf-8")).hexdigest()[:12])}


def admin_save(handler):
    body = _admin_body(handler)
    if body is None:
        return {"error": "corpo inválido"}, 400
    old = body.get("old") or {}
    new = body.get("new") or {}
    category = _slug(new.get("category", ""))
    app_id = _slug(new.get("id", ""))
    app = new.get("app") or {}
    name = str(app.get("name", "")).strip()
    if not category or not app_id or not name:
        return {"error": "informe categoria, identificador e nome"}, 400
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", app_id):
        return {"error": "identificador inválido (use letras, números e hífens)"}, 400

    with _ADMIN_LOCK:
        raw = _load_raw()

        for c, i in ((category, app_id), (_slug(old.get("category", "")), _slug(old.get("id", "")))):
            if not c or not i:
                continue
            items = raw.get(c)
            if isinstance(items, dict) and i in items:
                del items[i]
                if not items:
                    del raw[c]

        icon_up = body.get("icon_upload")
        if isinstance(icon_up, dict) and icon_up.get("data"):
            try:
                ext, data = _decode_image(icon_up["data"])
            except ValueError as e:
                return {"error": str(e)}, 400
            rel = f"assets/{app_id}.{ext}"
            try:
                os.makedirs(ASSETS_DIR, exist_ok=True)
                with open(os.path.join(ASSETS_DIR, os.path.basename(rel)), "wb") as fh:
                    fh.write(data)
            except OSError:
                return {"error": "não foi possível salvar o ícone"}, 500
            app["icon"] = rel
        else:
            cur = str(app.get("icon") or "")
            if cur and not ASSET_RE.match(cur):
                return {"error": "caminho do ícone inválido"}, 400
            app["icon"] = cur

        shots = []
        for s in app.get("screenshots") or []:
            s = str(s)
            if not ASSET_RE.match(s):
                return {"error": "caminho de screenshot inválido"}, 400
            shots.append(s)
        n = len(shots) + 1
        for up in body.get("screenshots_upload") or []:
            if not (isinstance(up, dict) and up.get("data")):
                continue
            try:
                ext, data = _decode_image(up["data"])
            except ValueError as e:
                return {"error": str(e)}, 400
            rel = f"assets/{app_id}-{n}.{ext}"
            try:
                with open(os.path.join(ASSETS_DIR, os.path.basename(rel)), "wb") as fh:
                    fh.write(data)
            except OSError:
                return {"error": "não foi possível salvar o screenshot"}, 500
            shots.append(rel)
            n += 1
        app["screenshots"] = shots

        pacman = app.get("pacman") or {}
        defaults = pacman.get("default") or {}
        source = defaults.get("source") or "manual"
        pkgs = [
            p for p in ([defaults.get("main-package")] + list(defaults.get("install-packages") or []))
            if p
        ]
        if not pkgs:
            return {"error": "informe ao menos um pacote (main-package ou install-packages)"}, 400
        defaults["main-package"] = str(defaults.get("main-package") or pkgs[0])
        defaults.setdefault("install-packages", [defaults["main-package"]])
        defaults.setdefault("remove-packages", [defaults["main-package"]])
        defaults["source"] = source

        app = _canonical_app(app)

        if category not in raw or not isinstance(raw[category], dict):
            raw[category] = {}
        raw[category][app_id] = app

        _bump_stats(raw)
        try:
            _write_raw(raw)
        except OSError:
            return {"error": "falha ao gravar o catálogo"}, 500
        _gc_assets(raw)
        rebuild_payload()
    return {"ok": True, "revision": raw.get("stats", {}).get("revision")}, 200


def admin_delete(handler):
    body = _admin_body(handler)
    if body is None:
        return {"error": "corpo inválido"}, 400
    category = _slug(body.get("category", ""))
    app_id = _slug(body.get("id", ""))
    if not category or not app_id:
        return {"error": "faltando categoria e identificador"}, 400
    with _ADMIN_LOCK:
        raw = _load_raw()
        items = raw.get(category)
        if not isinstance(items, dict) or app_id not in items:
            return {"error": "aplicativo não encontrado"}, 404
        del items[app_id]
        if not items:
            del raw[category]
        _bump_stats(raw)
        try:
            _write_raw(raw)
        except OSError:
            return {"error": "falha ao gravar o catálogo"}, 500
        _gc_assets(raw)
        rebuild_payload()
    return {"ok": True}, 200


def admin_build():
    """Gera o pacote de distribuição da loja (sem a opção de administração)."""
    import shutil
    import tarfile

    with _ADMIN_LOCK:
        raw = _load_raw()
        rev = int(raw.get("stats", {}).get("revision", 0))
        name = f"nlinux-software-v{rev}"
        pkg_root = os.path.join(DIST_DIR, "nlinux-software")
        tar_path = os.path.join(DIST_DIR, f"{name}.tar.gz")
        if os.path.exists(pkg_root):
            shutil.rmtree(pkg_root)

        try:
            os.makedirs(pkg_root, exist_ok=True)

            def ignore(d, files):
                out = []
                for f in files:
                    if f in ("__pycache__", "admin.html", "admin.css", "admin.js",
                             "dist", ".git"):
                        out.append(f)
                return out

            shutil.copytree(SRC_ROOT, os.path.join(pkg_root, "src"),
                            ignore=ignore, dirs_exist_ok=True)

            src_ws = os.path.join(pkg_root, "src", "nlinux", "web_server.py")
            with open(src_ws, encoding="utf-8") as fh:
                content = fh.read()
            content = content.replace("ADMIN_ENABLED = True", "ADMIN_ENABLED = False", 1)
            with open(src_ws, "w", encoding="utf-8") as fh:
                fh.write(content)

            launch = os.path.join(pkg_root, "nlinux-software")
            with open(launch, "w") as fh:
                fh.write("#!/bin/sh\n"
                         "cd \"$(dirname \"$0\")\"\n"
                         "exec /usr/bin/python3 src/main.py \"$@\"\n")
            os.chmod(launch, 0o755)

            deps = " ".join(PACKAGE_DEPS)
            with open(os.path.join(pkg_root, "install.sh"), "w") as fh:
                fh.write(
                    "#!/bin/sh\n"
                    "set -e\n"
                    "if [ \"$(id -u)\" -ne 0 ]; then\n"
                    "  echo \"Executando com sudo...\"\n"
                    "  exec sudo \"$0\" \"$@\"\n"
                    "fi\n"
                    "require() { pacman -Q \"$1\" >/dev/null 2>&1; }\n"
                    "# --- dependências de execução ----------------------------------------\n"
                    "MISSING=\"\"\n"
                    f"for p in {deps}; do\n"
                    "  require \"$p\" || MISSING=\"$MISSING $p\"\n"
                    "done\n"
                    "if [ -n \"$MISSING\" ]; then\n"
                    "  echo \"Instalando dependências:${MISSING}\"\n"
                    "  pacman -S --noconfirm --needed $MISSING\n"
                    "fi\n"
                    "# paru ou yay (AUR) e curl são opcionais; avisa só o que faltar\n"
                    "if ! command -v paru >/dev/null 2>&1 && ! command -v yay >/dev/null 2>&1; then\n"
                    "  echo \"Aviso: nenhum auxiliar AUR encontrado (paru ou yay).\"\n"
                    "fi\n"
                    "command -v curl >/dev/null 2>&1 || echo \"Aviso: 'curl' não encontrado.\"\n"
                    "# --- autentica o pacote com GPG (assinatura da curadoria) ------------\n"
                    "SRC=\"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
                    "if ! command -v gpg >/dev/null 2>&1; then\n"
                    "  echo \"ERRO: gpg ausente; não é possível validar a assinatura.\" >&2\n"
                    "  exit 1\n"
                    "fi\n"
                    "TARBALL=\"$(ls \"$SRC\"/nlinux-software-v*.tar.gz \"$SRC\"/../nlinux-software-v*.tar.gz 2>/dev/null | head -n1)\"\n"
                    "if [ -n \"$TARBALL\" ] && [ -f \"$TARBALL.asc\" ]; then\n"
                    "  gpg --batch --import \"$SRC/nlinux-software_pub.asc\" >/dev/null 2>&1\n"
                    "  if ! gpg --batch --verify \"$TARBALL.asc\" \"$TARBALL\" >/dev/null 2>&1; then\n"
                    "    echo \"ERRO: assinatura GPG do pacote INVÁLIDA. Instalação abortada.\" >&2\n"
                    "    echo \"O arquivo (ou o repositório) pode ter sido adulterado. Baixe de novo.\" >&2\n"
                    "    exit 1\n"
                    "  fi\n"
                    "  echo \"Verificação GPG: OK (pacote autêntico da curadoria).\"\n"
                    "else\n"
                    "  echo \"Aviso: assinatura (.asc) não encontrada junto do pacote; sem validação.\"\n"
                    "fi\n"
                    "# --- instala a loja ----------------------------------------------------\n"
                    f"DEST=\"/opt/nlinux-software\"\n"
                    "rm -rf \"$DEST\"\n"
                    "mkdir -p \"$DEST\"\n"
                    "cp -a \"$SRC/src\" \"$DEST/\"\n"
                    "cp \"$SRC/nlinux-software\" \"$DEST/\"\n"
                    "chmod +x \"$DEST/nlinux-software\"\n"
                    "cat > /usr/local/bin/nlinux-software <<'EOF'\n"
                    "#!/bin/sh\n"
                    "exec /opt/nlinux-software/nlinux-software \"$@\"\n"
                    "EOF\n"
                    "chmod +x /usr/local/bin/nlinux-software\n"
                    "# --- ícone + atalho no menu de aplicativos --------------------------\n"
                    "cat > \"$DEST/icon.svg\" <<'SVG'\n"
                    "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"128\" height=\"128\" viewBox=\"0 0 128 128\">\n"
                    "  <defs>\n"
                    "    <linearGradient id=\"g\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\">\n"
                    "      <stop offset=\"0\" stop-color=\"#1f6feb\"/>\n"
                    "      <stop offset=\"1\" stop-color=\"#0d3b8f\"/>\n"
                    "    </linearGradient>\n"
                    "  </defs>\n"
                    "  <rect x=\"4\" y=\"4\" width=\"120\" height=\"120\" rx=\"26\" fill=\"url(#g)\"/>\n"
                    "  <rect x=\"4\" y=\"4\" width=\"120\" height=\"120\" rx=\"26\" fill=\"none\" stroke=\"#12233f\" stroke-width=\"4\"/>\n"
                    "  <path d=\"M32 86 L58 40 L74 72 L84 54 L98 86\" stroke=\"#ffffff\" stroke-width=\"10\" fill=\"none\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/>\n"
                    "  <circle cx=\"58\" cy=\"94\" r=\"9\" fill=\"#3fb950\"/>\n"
                    "  <g transform=\"translate(90,90)\">\n"
                    "    <path d=\"M-18 -8 L-18 18 Q-18 24 -12 24 L12 24 Q18 24 18 18 L18 -8 Z\" fill=\"#3fb950\" stroke=\"#12233f\" stroke-width=\"3\" stroke-linejoin=\"round\"/>\n"
                    "    <path d=\"M-9 -8 L-9 -14 Q-9 -20 0 -20 Q9 -20 9 -14 L9 -8\" fill=\"none\" stroke=\"#12233f\" stroke-width=\"3\" stroke-linecap=\"round\"/>\n"
                    "  </g>\n"
                    "</svg>\n"
                    "SVG\n"
                    "mkdir -p /usr/share/icons/hicolor/scalable/apps\n"
                    "cp \"$DEST/icon.svg\" /usr/share/icons/hicolor/scalable/apps/nlinux-software.svg\n"
                    "(command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -f -t /usr/share/icons/hicolor) || true\n"
                    "rm -f /usr/share/applications/nlinux-software.desktop\n"
                    "rm -f /usr/share/applications/nlinuxsoftware.desktop\n"
                    "cat > /usr/share/applications/nlinuxstore.desktop <<'EOF'\n"
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=NLinux Software\n"
                    "GenericName=Loja de aplicativos\n"
                    "Comment=Loja de aplicativos do NLinux (distribuição)\n"
                    "Exec=/usr/local/bin/nlinux-software\n"
                    "Icon=nlinux-software\n"
                    "Terminal=false\n"
                    "Categories=Network;Utility;\n"
                    "StartupNotify=true\n"
                    "StartupWMClass=nlinuxstore\n"
                    "X-GNOME-UsesNotifications=false\n"
                    "EOF\n"
                    "(command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database /usr/share/applications) || true\n"
                    f"echo \"Instalado: NLinux Software v{rev} (/usr/local/bin/nlinux-software)\"\n"
                )
            os.chmod(os.path.join(pkg_root, "install.sh"), 0o755)

            with open(os.path.join(pkg_root, "DEPENDENCIES.txt"), "w") as fh:
                fh.write(
                    "Dependencias de execucao do NLinux Software\n"
                    "(nomes de pacote Arch Linux)\n\n"
                    "Obrigatorias (instaladas automaticamente pelo install.sh):\n" +
                    "\n".join(f"  - {p}" for p in PACKAGE_DEPS) +
                    "\n\nOpcionais:\n"
                    "  - paru  (instalacao de pacotes AUR pela loja)\n"
                    "  - curl  (diagnostico/testes)\n"
                )

            with open(os.path.join(pkg_root, "README.txt"), "w") as fh:
                fh.write(
                    f"NLinux Software v{rev} - loja de aplicativos (distribuicao)\n"
                    "Sem opcao de administracao.\n\n"
                    "Dependencias (veja DEPENDENCIES.txt):\n" +
                    "\n".join(f"  - {p}" for p in PACKAGE_DEPS) +
                    "\n\nInstalar (o instalador verifica e instala as dependencias faltantes):\n"
                    "  sudo ./install.sh\n"
                    "Executar:\n"
                    "  nlinux-software\n"
                )

            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(pkg_root, arcname=f"{name}/nlinux-software")
            size = os.path.getsize(tar_path)

            # Marcador de versão do catálogo: a loja lê só este arquivo (~100
            # bytes) a cada 15 s para saber se precisa baixar o catálogo inteiro.
            marker = {
                "revision": rev,
                "compiled": raw.get("stats", {}).get("compiled"),
                "apps": len(raw.get("apps", [])),
                "sha1": _catalog_fingerprint(raw),
                "published": int(time.time()),
            }
            with open(os.path.join(pkg_root, "catalog-head.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(marker, fh, ensure_ascii=False, indent=2)

            # Assinatura GPG real (detached, armadura ASCII) pela curadoria.
            enc = os.environ.copy()
            enc["GNUPGHOME"] = os.path.expanduser("~/.gnupg")
            asc_path = tar_path + ".asc"
            sign = subprocess.run(
                ["gpg", "--batch", "--yes", "--armor", "--detach-sign",
                 "--digest-algo", "SHA256", "--output", asc_path, tar_path],
                capture_output=True, text=True, env=enc)
            if sign.returncode != 0 or not os.path.exists(asc_path):
                raise OSError("falha ao assinar o pacote com GPG: "
                              + (sign.stderr.strip() or "código desconhecido"))
            pub_asc = os.path.expanduser("~/nlinux-software_pub.asc")
            if os.path.exists(pub_asc):
                shutil.copy2(pub_asc, os.path.join(pkg_root, "nlinux-software_pub.asc"))

            publish = git_publish(pkg_root, tar_path, rev)
        except OSError as e:
            return {"error": f"falha ao gerar o pacote: {e}"}, 500

    return {"ok": True, "revision": rev, "path": tar_path, "run": pkg_root,
            "size": size, "publish": publish}, 200


def _prepare_git_clone() -> str:
    """Garante o repositório local (~/nlinux-repo) clonado do GitHub e o retorna."""
    clone_dir = GIT_PUSH_DIR
    if os.path.isdir(os.path.join(clone_dir, ".git")):
        return clone_dir
    parent = os.path.dirname(clone_dir)
    os.makedirs(parent, exist_ok=True)
    print(f"[nlinux] clonando catálogo do GitHub pela primeira vez ({GIT_PUSH_URL})...")
    result = subprocess.run(
        ["git", "clone", GIT_PUSH_URL, clone_dir],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git clone falhou")
    return clone_dir


def git_publish(build_dir: str, tar_path: str, rev: int) -> dict:
    """Publica o build no GitHub: copia o conteúdo, comita e faz push.

    Retorna {"pushed": True} ou {"pushed": False, "reason": ...} — nunca lança
    exceção (a falha de publicação não deve impedir a loja de continuar)."""
    if not GIT_PUSH_ENABLED:
        return {"pushed": False, "reason": "publicação desativada (NLINUX_GIT_PUSH=0)"}

    try:
        clone_dir = _prepare_git_clone()
    except Exception as exc:
        return {"pushed": False, "reason": f"clone: {exc}"}

    try:
        # Substitui o conteúdo do repositório (mantém apenas .git e .gitignore).
        keep = {".git", ".gitignore"}
        for entry in os.listdir(clone_dir):
            if entry in keep:
                continue
            full = os.path.join(clone_dir, entry)
            if os.path.isdir(full) and not os.path.islink(full):
                shutil.rmtree(full)
            else:
                os.remove(full)

        for entry in os.listdir(build_dir):
            src = os.path.join(build_dir, entry)
            dst = os.path.join(clone_dir, entry)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)

        shutil.copy2(tar_path, os.path.join(clone_dir, os.path.basename(tar_path)))
        asc = tar_path + ".asc"
        if os.path.exists(asc):
            shutil.copy2(asc, os.path.join(clone_dir, os.path.basename(asc)))

        # .gitignore do próprio repositório, para não subir lixo.
        gi = os.path.join(clone_dir, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w") as fh:
                fh.write("__pycache__/\n*.py[cod]\n")

        def _git(*args, env=None):
            return subprocess.run(
                ["git", "-C", clone_dir, *args],
                capture_output=True, text=True,
                env=env or author_env,
            )

        author_env = os.environ.copy()
        author_env.setdefault("GIT_AUTHOR_NAME", GIT_PUSH_USER)
        author_env.setdefault("GIT_AUTHOR_EMAIL", GIT_PUSH_EMAIL)
        author_env.setdefault("GIT_COMMITTER_NAME", GIT_PUSH_USER)
        author_env.setdefault("GIT_COMMITTER_EMAIL", GIT_PUSH_EMAIL)

        add = _git("add", "-A")
        if add.returncode != 0:
            return {"pushed": False, "reason": add.stderr.strip() or "git add falhou"}

        status = _git("status", "--porcelain")
        if not status.stdout.strip():
            return {"pushed": True, "clean": True}

        commit = _git(
            "commit", "-q",
            "-m", f"NLinux Software v{rev}: catálogo atualizado",
            env=author_env,
        )
        if commit.returncode != 0:
            return {"pushed": False, "reason": commit.stderr.strip() or "git commit falhou"}

        push = subprocess.run(
            ["git", "-C", clone_dir, "push", "origin",
             f"HEAD:{GIT_PUSH_BRANCH}", "--force-with-lease"],
            capture_output=True, text=True, env=author_env,
        )
        if push.returncode != 0:
            return {"pushed": False, "reason": push.stderr.strip() or "git push falhou"}
        return {"pushed": True, "branch": GIT_PUSH_BRANCH}
    except Exception as exc:
        return {"pushed": False, "reason": str(exc)}


# FIM da seção de publicação no GitHub ------------------------------------------


# ===================== Catálogo remoto (GitHub) ==============================

def _cache_busted(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}cb={int(time.time())}"


def _remote_get(url: str, timeout: int = 20):
    """GET sem cache: o parâmetro cb= força o CDN a buscar no servidor."""
    import urllib.request

    req = urllib.request.Request(
        _cache_busted(url),
        headers={"User-Agent": "nlinux-software", "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_remote_catalog() -> dict:
    """Baixa o json do catálogo do repositório remoto (raw GitHub)."""
    return _remote_get(REMOTE_CATALOG_URL)


def fetch_remote_marker() -> dict:
    """Lê o arquivo-marcador (~100 bytes) que diz se o catálogo mudou."""
    return _remote_get(REMOTE_MARKER_URL, timeout=15)


def _catalog_fingerprint(raw: dict) -> str:
    return hashlib.sha1(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


_REMOTE_SYNC = {"applied": None, "marker_ok": None, "last_full": 0.0}

# Se o marcador não estiver publicado (repo antigo), rebaixa o catálogo inteiro
# no máximo a cada 5 minutos em vez de Download de 236 KB a cada 15 s.
REMOTE_FALLBACK_SECONDS = 300


def _local_matches_marker(local: dict, marker: dict) -> bool:
    """O catálogo local já é o que o marcador descreve?"""
    if _catalog_fingerprint(local) == marker.get("sha1"):
        return True
    stats = local.get("stats")
    if not isinstance(stats, dict):
        return False
    return (stats.get("revision") == marker.get("revision")
            and stats.get("compiled") == marker.get("compiled"))


def apply_remote_catalog(force: bool = False) -> bool:
    """Confere o catálogo remoto e, se houver diferenças, grava localmente e
    reconstrói o payload. A loja detecta a mudança e recarrega sozinha.
    Retorna True quando o catálogo foi atualizado."""
    try:
        local = _load_raw()
    except Exception:
        local = {}

    if not force and _REMOTE_SYNC["applied"] is not None \
            and local and _catalog_fingerprint(local) != _REMOTE_SYNC["applied"]:
        force = True  # arquivo local divergiu do que foi aplicado: rebaixa

    if not force:
        try:
            marker = fetch_remote_marker()
            _REMOTE_SYNC["marker_ok"] = True
        except Exception as exc:
            if _REMOTE_SYNC["marker_ok"] is None:
                print(f"[nlinux] marcador remoto indisponível "
                      f"({exc}); usando verificação completa a cada "
                      f"{REMOTE_FALLBACK_SECONDS}s")
            _REMOTE_SYNC["marker_ok"] = False
            marker = None
        if marker is not None:
            if _local_matches_marker(local, marker):
                return False
        else:
            if time.time() - _REMOTE_SYNC["last_full"] < REMOTE_FALLBACK_SECONDS:
                return False
            _REMOTE_SYNC["last_full"] = time.time()

    try:
        remote = fetch_remote_catalog()
    except Exception as exc:
        print(f"[nlinux] catálogo remoto indisponível: {exc}")
        return False
    if not isinstance(remote, dict) or not remote:
        print("[nlinux] catálogo remoto vazio ou inválido; mantido o atual")
        return False

    with _ADMIN_LOCK:
        current = _load_raw()
        same = json.dumps(remote, sort_keys=True, ensure_ascii=False) == \
            json.dumps(current, sort_keys=True, ensure_ascii=False)
        if same:
            _REMOTE_SYNC["applied"] = _catalog_fingerprint(remote)
            return False
        new_stats = remote.get("stats")
        if not (isinstance(new_stats, dict)
                and isinstance(new_stats.get("revision"), int)):
            remote["stats"] = current.get("stats", {})
        try:
            _write_raw(remote)
        except OSError as exc:
            print(f"[nlinux] falha ao gravar o catálogo remoto: {exc}")
            return False
        rebuild_payload()
        revision = remote.get("stats", {}).get("revision")
        _REMOTE_SYNC["applied"] = _catalog_fingerprint(remote)
    print(f"[nlinux] catálogo atualizado do repositório remoto (revision {revision})",
          flush=True)
    return True


def remote_refresh_loop() -> None:
    """Ciclo de checagem do catálogo remoto.

    A cada REMOTE_CHECK_SECONDS lê o marcador de ~100 bytes; quando a impressão
    digital muda, baixa o catálogo e reconstrói o payload. A loja, que pergunta o
    payload a cada poucos segundos, recarrega sozinha.
    """
    while True:
        time.sleep(REMOTE_CHECK_SECONDS)
        try:
            apply_remote_catalog()
        except Exception as exc:
            print(f"[nlinux] erro no ciclo de atualização remota: {exc}")


def start_remote_refresh() -> None:
    """Sincroniza na inicialização e agenda o ciclo periódico.

    Somente a versão de distribuição (sem administração) sincroniza; a versão
    de curadoria é a fonte do catálogo e nunca deve ser sobrescrita."""
    if ADMIN_ENABLED:
        return

    def _boot():
        try:
            apply_remote_catalog()
        except Exception as exc:
            print(f"[nlinux] erro na atualização inicial remota: {exc}")
        remote_refresh_loop()

    threading.Thread(target=_boot, daemon=True).start()


def ensure_admin_shortcut() -> None:
    """Cria o atalho 'NLinux Software Admin' apontando para o projeto real.

    Só roda na curadoria (ADMIN_ENABLED). Usa o caminho do próprio projeto,
    detectado via __file__, para não fixar o home do usuário no repositório.
    """
    desktop_dir = os.path.join(
        os.path.expanduser("~"), ".local", "share", "applications")
    try:
        os.makedirs(desktop_dir, exist_ok=True)
    except OSError:
        return

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    installed_launcher = "/usr/local/bin/nlinux-software-admin"
    source_launcher = os.path.join(project_root, "nlinux-software-admin")
    if os.path.isfile(installed_launcher):
        launcher = installed_launcher
    elif os.path.isfile(source_launcher):
        launcher = source_launcher
    else:
        return

    icon_src = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "icon-admin.svg")
    if not os.path.exists(icon_src):
        icon_src = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "icon.svg")
    icon_name = "nlinux-software-admin"
    icons_dir = os.path.join(
        os.path.expanduser("~"), ".local", "share", "icons", "hicolor",
        "scalable", "apps")
    try:
        os.makedirs(icons_dir, exist_ok=True)
        shutil.copy2(icon_src, os.path.join(icons_dir, icon_name + ".svg"))
    except OSError:
        icon_name = icon_src

    entry = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=NLinux Software Admin\n"
        "GenericName=Curadoria da loja\n"
        "Comment=Administração da NLinux Software (com curadoria)\n"
        f"Exec={launcher}\n"
        f"Icon={icon_name}\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "StartupNotify=true\n"
        "StartupWMClass=nlinuxcuradoria\n"
    )
    apps_dir = desktop_dir
    path = os.path.join(apps_dir, "nlinuxcuradoria.desktop")
    for old_name in ("nlinuxsoftware.desktop", "nlinux-software-admin.desktop"):
        try:
            old_path = os.path.join(apps_dir, old_name)
            if os.path.exists(old_path):
                os.remove(old_path)
        except OSError:
            pass
    try:
        with open(path, "w") as fh:
            fh.write(entry)
        os.chmod(path, 0o755)
    except OSError:
        pass


def run(open_browser: bool = True) -> None:
    import webbrowser

    if ADMIN_ENABLED:
        ensure_admin_shortcut()

    with BoutiqueHandler.payload_lock:
        BoutiqueHandler.payload = build_payload()

    start_remote_refresh()

    server = ThreadingHTTPServer(("127.0.0.1", 0), BoutiqueHandler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"Loja de Software NLinux em {url}")
    print("Pressione Ctrl+C para encerrar.")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    run()