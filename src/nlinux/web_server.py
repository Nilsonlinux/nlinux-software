import base64
import errno
import fcntl
import hashlib
import json
import locale as l18n
import os
import pwd
import re
import pty
import select
import shlex
import shutil
import struct
import subprocess
import threading
import termios
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from nlinux import aur_db
from nlinux import pacman_db
from nlinux import resources
from nlinux.system_state import SystemState

SRC_ROOT = resources.SRC_ROOT
WEB_DIR = os.path.join(SRC_ROOT, "assets", "web")

# Idiomas suportados pela loja (mesmos do instalador web) com sua região.
_STORE_LANG_REGIONS = {
    "pt": "pt-BR",
    "en": "en-US",
    "es": "es-ES",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
    "ja": "ja-JP",
}


def _read_installed_lang() -> str:
    """Lê o idioma escolhido no instalador web (autoritativo)."""
    try:
        with open("/etc/locale.conf", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("LANG="):
                    val = line.split("=", 1)[1].strip()
                    return val.strip('"').strip("'")
    except OSError:
        pass
    return ""


def resolve_store_lang() -> tuple:
    """Retorna (código, região) do idioma da loja."""
    raw = _read_installed_lang()
    if not raw:
        try:
            raw = l18n.getlocale()[0] or ""
        except Exception:
            raw = ""
    code = raw.split(".")[0]
    if "_" in code:
        code = code.split("_")[0]
    code = code.lower()
    if code not in _STORE_LANG_REGIONS:
        code = "en"
    return code, _STORE_LANG_REGIONS[code]


def _resolve_dist_dir() -> str:
    """Pasta dos pacotes gerados.

    Sempre fica fora do checkout do projeto.
    """
    return resources.writable(os.path.join(resources.data_home(), "dist"))


DIST_DIR = _resolve_dist_dir()

# Desativada na versão de distribuição (gerada pela página de administração).
ADMIN_ENABLED = False

# Catálogo e mídia: no projeto de desenvolvimento fica em src/apps; instalado
# em /opt, em ~/.local/share/nlinux/<papel>/apps, gravável sem root.
# Definido aqui
# porque depende de ADMIN_ENABLED.
#
# NLINUX_APPS_DIR sobrescreve o caminho. Existe por causa de uma armadilha: rodar
# o projeto de dentro do $HOME faz a curadoria apontar para <projeto>/src/apps, o
# snapshot do repositório, em vez de ~/.local/share/nlinux/admin/apps, que é o
# catálogo que se está curando. Os dois são catálogos válidos e o servidor não
# tem como adivinhar qual é o certo — com a variável, dá para escolher.
APPS_DIR = os.environ.get("NLINUX_APPS_DIR") or \
    resources.apps_dir("admin" if ADMIN_ENABLED else "store")


def _assets_dir() -> str:
    """Pasta de mídia do catálogo — derivada de `APPS_DIR` a cada chamada.

    Não era uma constante `ASSETS_DIR` calculada na importação. O catálogo é lido
    por `_index_path()`, que resolve `APPS_DIR` na hora; a mídia, congelada no
    import. Quando os dois divergem — `NLINUX_APPS_DIR` mudado, ou o papel
    admin/loja trocado em tempo de execução — o GC comparava o catálogo de um
    lugar com a mídia de outro e apagava de mais: foi assim que 68 ícones do
    snapshot saíram quando se salvou no catálogo vivo. Derivando sempre do mesmo
    lugar, os dois não podem divergir.
    """
    return os.path.join(APPS_DIR, "assets")

# Publicação automática no GitHub quando o build da versão da loja terminar.
# Desligue com NLINUX_GIT_PUSH=0. O repositório local é clonado no GIT_PUSH_DIR
# na primeira vez (pede a senha do GitHub uma única vez).
GIT_PUSH_ENABLED = os.environ.get("NLINUX_GIT_PUSH", "1") not in ("0", "false", "no")
GIT_PUSH_URL = os.environ.get("NLINUX_GIT_URL") or \
    "https://github.com/Nilsonlinux/nlinux-software.git"
GIT_PUSH_BRANCH = os.environ.get("NLINUX_GIT_BRANCH") or "main"
GIT_PUSH_DIR = os.environ.get("NLINUX_GIT_DIR") or \
    os.path.join(os.path.expanduser("~"), "nlinux-software")
GIT_PUSH_USER = os.environ.get("NLINUX_GIT_USER") or "Nilsonlinux"
GIT_PUSH_EMAIL = os.environ.get("NLINUX_GIT_EMAIL") or "nilsonlinux@users.noreply.github.com"
# Projeto que constrói a ISO da distro. A subpasta <ISO_BUILD_DIR>/nlinux-software
# é a fonte que o build-iso.sh embute em /opt/nlinux-software na imagem, e ela é
# versionada no repositório do iso-build: sem espelhar o build para lá, a ISO
# continua embarcando a revisão que ficou commitada — uma loja velha, às vezes
# com programas que você já removeu do catálogo.
ISO_BUILD_DIR = os.environ.get("NLINUX_ISO_DIR") or \
    os.path.join(os.path.expanduser("~"), "nlinux-iso-build")
ISO_STORE_DIRNAME = "nlinux-software"
# A imagem é montada a partir do que está commitado: deixar a pasta da loja
# modificada faria o build da ISO usar uma loja diferente da do repositório.
# Por isso o espelho fecha com commit. Desligue com NLINUX_ISO_COMMIT=0; o
# push é opt-in com NLINUX_ISO_PUSH=1, para não escrever no remoto sem querer.
ISO_COMMIT_ENABLED = os.environ.get("NLINUX_ISO_COMMIT", "1") not in ("0", "false", "no")
ISO_PUSH_ENABLED = os.environ.get("NLINUX_ISO_PUSH", "0") not in ("0", "false", "no")
PACKAGE_DEPS = [
    "python",
    "python-gobject",
    "gtk3",
    "webkit2gtk-4.1",
    "polkit",
    "gnupg",
    "git",
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
    code, _ = resolve_store_lang()
    candidates = [code, "en"]
    try:
        locale = l18n.getlocale()[0]
    except Exception:
        locale = ""
    if locale:
        candidates.append(locale)
        if "_" in locale:
            candidates.append(locale.split("_")[0])
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
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


PACMAN_PACKAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9@._+-]*$")


def is_pacman_package(value) -> bool:
    return isinstance(value, str) and bool(PACMAN_PACKAGE_RE.fullmatch(value))


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
        self.progress = None
        self._progress_phase = "starting"
        self._step_group = 0
        self._step_current = 0
        self._step_total = 0
        # Extremidade de escrita do pty enquanto o processo roda. O pacman roda
        # interativo, e sem isto toda pergunta dele ficava sem resposta: o
        # processo esperava para sempre e a janela mostrava um travamento sem
        # saída. Vale para a instalação também, não só para a atualização — a
        # importação de uma chave PGP e a escolha de provedor acontecem nos dois.
        self._master_fd = None
        self.esperando_resposta = False
        self.reiniciar = False
        self.criticos: list = []
        self._espera_desde = 0.0

    def answer(self, text: str) -> bool:
        """Manda uma resposta ao processo que está esperando.

        Devolve False quando não há processo esperando: responder fora da hora
        é erro do cliente, não motivo para fingir que deu certo — senão a
        interface acreditaria que a resposta foi entregue e o pacman ficaria
        parado.

        A resposta **não** entra no log. O pty já devolve o que foi digitado,
        como num terminal de verdade, e repetir aqui deixaria duas cópias da
        mesma linha. Quem precisa do registro do que mandou é a janela, que
        acabou de ver a pessoa digitar.
        """
        if self._master_fd is None or self.done:
            return False
        try:
            os.write(self._master_fd, (text.rstrip("\n") + "\n").encode())
        except OSError:
            return False
        self.esperando_resposta = False
        self._espera_desde = 0.0
        return True

    def _record_output(self, raw_line: str) -> None:
        line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw_line).strip()
        if not line:
            return
        self.lines.append(line)
        self._update_progress(line)

    def _update_progress(self, line: str) -> None:
        text = line.lower()
        if "synchronizing package databases" in text or "sincronizando" in text:
            self._progress_phase = "database"
            self._set_progress(2)
        elif "resolving dependencies" in text or "resolvendo depend" in text:
            self._progress_phase = "dependencies"
            self._set_progress(8)
        elif ("looking for conflicting packages" in text
              or "pacotes conflitantes" in text or "pacotes em conflito" in text):
            self._progress_phase = "dependencies"
            self._set_progress(12)
        elif ("retrieving packages" in text or "downloading" in text
              or "baixando" in text or "transferindo" in text):
            self._progress_phase = "download"
            self._set_progress(15)

        phases = (
            ("checking keys in keyring", 55),
            ("checking package integrity", 61),
            ("loading package files", 67),
            ("checking for file conflicts", 73),
            ("checking available disk space", 79),
        )
        for label, start in phases:
            if label in text:
                self._progress_phase = "transaction"
                pct_match = re.search(r"(\d{1,3})%", text)
                pct = min(int(pct_match.group(1)), 100) if pct_match else 0
                self._set_progress(start + int(5 * pct / 100))
                return

        match = re.search(
            r"(?:\(|\[)\s*(\d+)\s*/\s*(\d+)\s*(?:\)|\])\s*"
            r"(installing|upgrading|reinstalling|removing|instalando|"
            r"atualizando|reinstalando|removendo)\b",
            text,
        )
        if match:
            self._update_counted_step(match, transaction=True)
            return

        count_match = re.search(
            r"(?:\(|\[)\s*(\d+)\s*/\s*(\d+)\s*(?:\)|\])", text)
        if count_match and (
            ".pkg.tar." in text or re.search(r"\btotal\s*\(", text)
        ):
            current, total = int(count_match.group(1)), int(count_match.group(2))
            if total > 0:
                pct_match = re.search(r"(\d{1,3})%", text)
                fraction = (
                    (current - 1 + min(int(pct_match.group(1)), 100) / 100) / total
                    if pct_match else current / total
                )
                database_download = (
                    self._progress_phase == "database" and ".pkg.tar." not in text
                )
                self._progress_phase = "database" if database_download else "download"
                start, span = (2, 6) if database_download else (15, 40)
                self._set_progress(start + int(span * min(fraction, 1)))
            return
        if count_match:
            package_change = re.search(
                r"\b(instalando|actualizando|reinstalando|removiendo|"
                r"installiere|aktualisiere|entferne|installazione|"
                r"aggiornamento|rimozione)\b",
                text,
            )
            self._update_counted_step(count_match, transaction=bool(package_change))
            return

        aur_stages = (
            ("making package:", 5),
            ("retrieving sources", 10),
            ("validating source files", 18),
            ("extracting sources", 25),
            ("starting prepare()", 30),
            ("starting build()", 35),
            ("starting check()", 65),
            ("starting package()", 70),
            ("finished making:", 75),
        )
        for label, estimate in aur_stages:
            if label in text:
                self._set_progress(estimate)
                return

        if ".pkg.tar." in text or self._progress_phase in ("download", "database"):
            pct_match = re.search(r"(\d{1,3})%", text)
            if pct_match:
                pct = min(int(pct_match.group(1)), 100)
                database_download = (
                    self._progress_phase == "database" and ".pkg.tar." not in text
                )
                if not database_download:
                    self._progress_phase = "download"
                if database_download:
                    estimate = 2 + int(6 * pct / 100)
                else:
                    estimate = 15 + int(40 * pct / 100)
                self._set_progress(estimate)

    def _set_progress(self, progress: int) -> None:
        self.progress = max(self.progress or 0, min(99, progress))

    def _update_counted_step(self, match, transaction: bool = False) -> None:
        current, total = int(match.group(1)), int(match.group(2))
        if total <= 0:
            return
        if transaction:
            self._step_group = max(self._step_group, 5)
        if current == 1 and (
            self._step_total == 0 or self._step_current >= self._step_total
        ):
            self._step_group += 1
        self._step_current = current
        self._step_total = total

        pct_match = re.search(r"(\d{1,3})%", match.string)
        within_step = (
            min(int(pct_match.group(1)), 100) / 100
            if pct_match else current / total
        )
        if self._step_group <= 5:
            self._progress_phase = "transaction-check"
            estimate = 55 + (self._step_group - 1) * 5 + int(4 * within_step)
        else:
            self._progress_phase = "transaction"
            fraction = min(
                (current - 1 + within_step) / total if pct_match
                else within_step,
                1,
            )
            estimate = 80 + int(19 * fraction)
        self._set_progress(estimate)

    def _aur_script(self) -> tuple[str, str, str]:
        """Instala pacotes AUR via paru OU yay (o que existir) como usuario,
        com NOPASSWD temporario apenas para o passo final de instalacao."""
        username = pwd.getpwuid(os.getuid()).pw_name
        inner_path = f"/tmp/boutique-aur-{self.id}-packages.sh"
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
            "printf '%s\\n' "
            + shlex.quote(f"{username} ALL=(ALL) NOPASSWD: /usr/bin/pacman")
            + " > \"$limite\"",
            "chmod 0440 \"$limite\"",
            "visudo -cf \"$limite\" >/dev/null 2>&1",
            f"cleanup() {{ rm -f \"$limite\" \"{inner_path}\"; }}",
            "trap cleanup EXIT INT TERM",
            "pacman -Sy --noconfirm || exit $?",
            f"su - -s /bin/bash {shlex.quote(username)} -c 'bash {inner_path}'",
            "exit $?",
        ])
        path = f"/tmp/boutique-aur-{self.id}-root.sh"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        return path, script, inner_path

    def _command(self):
        """(comando, arquivos temporários) a rodar neste job.

        Fica separado do `start` para que o caminho interativo possa ser
        testado de verdade, com um comando inofensivo que faz as mesmas
        perguntas — rodar `pacman -Syu` num teste atualizaria a máquina de quem
        está testando.
        """
        if self.mode == "sysupdate":
            return ["pkexec", "pacman", "-Syu", "--noconfirm"], []
        if self.mode == "remove":
            return ["pkexec", "pacman", "-Rns", "--noconfirm"] + self.packages, []
        if self.source == "aur":
            script_path, _, inner_path = self._aur_script()
            return ["pkexec", "bash", script_path], [script_path, inner_path]
        return (
            ["pkexec", "pacman", "-Sy", "--noconfirm", "--needed"]
            + self.packages
        ), []

    def start(self) -> None:
        def work() -> None:
            names = ", ".join(self.packages)
            removing = self.mode == "remove"
            if self.mode == "sysupdate":
                self.lines.append(f"Atualizando o sistema: {names} pacotes")
            else:
                self.lines.append(
                    f"{'Desinstalando' if removing else 'Instalando'} "
                    f"({self.source}): {names}")
            artifacts = []
            try:
                cmd, artifacts = self._command()
                master_fd, slave_fd = pty.openpty()
                self._master_fd = master_fd
                try:
                    fcntl.ioctl(
                        slave_fd,
                        termios.TIOCSWINSZ,
                        struct.pack("HHHH", 24, 80, 0, 0),
                    )
                    try:
                        process = subprocess.Popen(
                            cmd,
                            # O pty também como entrada, e não /dev/null. Com
                            # /dev/null qualquer pergunta do pacman recebia EOF
                            # e o processo abortava sozinho; sem canal de
                            # resposta, uma atualização parada numa pergunta
                            # ficava esperando para sempre.
                            stdin=slave_fd,
                            stdout=slave_fd,
                            stderr=slave_fd,
                            close_fds=True,
                        )
                    finally:
                        os.close(slave_fd)
                    pending = ""
                    self._espera_desde = 0.0
                    while True:
                        # `select` em vez de leitura bloqueante: sem um tempo
                        # limite na espera, um pacman parado numa pergunta que
                        # ninguém responde ficaria segurando a root em
                        # silêncio, para sempre. O prazo abaixo fecha o pty,
                        # e o pacman recebe EOF e desiste sozinho — que é a
                        # forma limpa de abortar, sem `kill` em um processo que
                        # está no meio de uma transação.
                        pronto, _, _ = select.select([master_fd], [], [], 1.0)
                        if not pronto:
                            if (self.esperando_resposta
                                    and time.time() - self._espera_desde
                                    > LIMITE_ESPERA):
                                self.lines.append(
                                    f"Sem resposta por {LIMITE_ESPERA // 60} "
                                    "minutos: a atualização foi cancelada. "
                                    "O pacman não desfez nada do que já "
                                    "instalou.")
                                self.esperando_resposta = False
                                os.close(master_fd)
                                master_fd = None
                                break
                            continue
                        try:
                            chunk = os.read(master_fd, 4096)
                        except OSError as exc:
                            if exc.errno == errno.EIO:
                                break
                            raise
                        if not chunk:
                            break
                        pending += chunk.decode("utf-8", "replace")
                        # Uma pergunta do pacman é uma linha **incompleta**: ela
                        # não fecha com \n nem com \r porque está esperando o que
                        # a pessoa digitar. Sem este passo, a pergunta ficava
                        # presa no acumulador e nunca aparecia no log — a
                        # janela acenderia a caixa de resposta sem mostrar
                        # nada para responder, que é o pior dos dois mundos.
                        # A barra de progresso também deixa linha pela metade,
                        # mas ela é encerrada por \r e chega aqui esvaziada.
                        if self.mode != "sysupdate" and PROMPT_RE.search(pending):
                            self.esperando_resposta = True
                            self._espera_desde = time.time()
                            self._record_output(pending)
                            pending = ""
                        while True:
                            delimiter = min(
                                (position for position in (
                                    pending.find("\n"), pending.find("\r")
                                ) if position >= 0),
                                default=-1,
                            )
                            if delimiter < 0:
                                break
                            self._record_output(pending[:delimiter])
                            pending = pending[delimiter + 1:]
                    self._record_output(pending)
                finally:
                    # Fecha a escrita antes de esperar o processo: sem isto, um
                    # pacman parado numa pergunta ficaria com o pty aberto e o
                    # `wait()` não teria como notar que a resposta nunca vem.
                    # Pode já estar fechado, quando o prazo de espera estourou
                    # dentro do laço — fechar duas vezes levanta EBADF e
                    # engoliria a falha real do comando.
                    if master_fd is not None:
                        os.close(master_fd)
                        master_fd = None
                    self._master_fd = None
                self.success = process.wait() == 0
            except Exception as e:
                self.success = False
                self.lines.append(f"Falha ao iniciar o instalador: {e}")
            finally:
                self._master_fd = None
                for a in artifacts:
                    try:
                        os.unlink(a)
                    except OSError:
                        pass
            self.state = "success" if self.success else "failed"
            if self.mode == "sysupdate":
                if self.success:
                    self.progress = 100
                    # Quem precisa reiniciar é decidido pelos pacotes que
                    # entraram, não por adivinhação: a lista foi montada antes do
                    # `pacman -Syu`, então é ela que diz o que mudou.
                    criticos = sorted(
                        p for p in self.packages
                        if p in REINICIO_PACOTES or
                        any(p.startswith(c + "-") for c in REINICIO_PACOTES))
                    self.reiniciar = bool(criticos)
                    self.criticos = criticos
                    self.lines.append(f"Sistema atualizado: {names} pacotes")
                    if self.reiniciar:
                        self.lines.append(
                            "Reinicie para que a atualização entre em vigor: "
                            + ", ".join(criticos))
                else:
                    self.lines.append(
                        "A atualização não foi concluída. O pacman não desfaz o "
                        "que já instalou, então o sistema continua íntegro — "
                        "mas confira o que ficou faltando na lista acima.")
                # O payload registra as versões instaladas. Sem invalidar, o
                # badge continuaria mostrando os mesmos pacotes por até
                # `ATUALIZACOES_TTL`, e a janela abriria sem nada para fazer.
                try:
                    invalidate_updates()
                except Exception as exc:
                    print(f"[nlinux] falha ao invalidar a lista de "
                          f"atualizações: {exc}")
                self.done = True
                return
            if self.success:
                self.progress = 100
                self.lines.append(f"{'Removido' if removing else 'Instalado'}: {names}")
            else:
                self.lines.append(
                    f"Falha ao {'desinstalar' if removing else 'instalar'}: {names}")
            # O payload guarda o estado "instalado" de cada app. Sem reconstruir
            # aqui, ele continua dizendo o que era verdade antes do trabalho, e o
            # app volta a aparecer como instalado (e a desinstalar de novo dá
            # erro, porque o pacote já não existe mais).
            try:
                rebuild_payload()
            except Exception as exc:
                print(f"[nlinux] falha ao reconstruir o payload: {exc}")
            self.done = True

        threading.Thread(target=work, daemon=True).start()

    def status(self) -> dict:
        return {
            "esperando_resposta": self.esperando_resposta,
            "reiniciar": self.reiniciar,
            "criticos": self.criticos,
            "id": self.id,
            "packages": self.packages,
            "mode": self.mode,
            "state": self.state,
            "done": self.done,
            "success": self.success,
            "progress": self.progress,
            "lines": self.lines[-12:],
        }


class BuildJob:
    """Gera o pacote de distribuição pedindo autorização ao polkit.

    Roda `pkexec` com `build_helper.py`, ou seja, o usuário vê a mesma caixa de
    autenticação de instalar/remover um programa. O processo filho escreve uma
    linha por etapa e, no fim, `@@RESULT@@<json>` com o retorno do build.
    Sem `pkexec` disponível, o build roda direto no processo atual.
    """

    def __init__(self, job_id: str) -> None:
        self.id = job_id
        self.state = "pending"
        self.lines: list = []
        self.done = False
        self.success = False
        self.result: dict = {}

    def log(self, msg: str) -> None:
        self.lines.append(msg)

    def status(self) -> dict:
        return {
            "id": self.id,
            "kind": "build",
            "state": self.state,
            "done": self.done,
            "success": self.success,
            "lines": self.lines[-20:],
            "result": self.result,
        }

    def _run_direct(self) -> None:
        def progress(msg: str) -> None:
            self.log(f"… {msg}")

        self.state = "running"
        result, _ = admin_build(progress=progress)
        self.result = result
        self.success = not bool(result.get("error"))
        self.done = True
        self.state = "done" if self.success else "error"

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        helper = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "build_helper.py")
        pkexec = shutil.which("pkexec")
        if not pkexec or not os.path.exists(helper):
            self.log("pkexec indisponível; gerando sem elevação de privilégio")
            try:
                self._run_direct()
            except Exception as exc:
                self.result = {"error": str(exc)}
                self.success = False
                self.done = True
                self.state = "error"
            return

        self.state = "running"
        self.log("aguardando autorização…")
        env = os.environ.copy()
        env["PKEXEC_UID"] = str(os.getuid())
        # O caminho do catálogo vai por argumento: o `pkexec` descarta o
        # ambiente do programa que executa, então só o que estiver na linha de
        # comando chega ao build. Sem isto, o helper recalcula o caminho a
        # partir do papel (admin/loja) em vez de usar o catálogo que esta
        # janela está curando — e o build empacota o snapshot do projeto sem
        # avisar ninguém. Ver `build_helper.run_as_user`.
        cmd = [pkexec, "/usr/bin/python3", helper, f"--apps-dir={APPS_DIR}"]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            if line.startswith("@@RESULT@@"):
                try:
                    self.result = json.loads(line[len("@@RESULT@@"):])
                except json.JSONDecodeError:
                    self.result = {"error": "resposta do build ilegível"}
            else:
                self.log(line)
        proc.wait()
        if not self.result:
            code = proc.returncode
            if code in (126, 127):
                self.result = {"error": "autorização cancelada ou negada"}
            elif code in (-2, -15, 143):
                self.result = {"error": "geração interrompida"}
            else:
                self.result = {"error": f"build falhou (código {code})"}
        self.success = not bool(self.result.get("error"))
        self.done = True
        self.state = "done" if self.success else "error"
        if not self.success:
            self.log(f"erro: {self.result.get('error')}")


ATUALIZACOES_TTL = 90.0
_atualizacoes_lock = threading.Lock()
_atualizacoes_cache: dict = {"quando": 0.0, "pacotes": [], "erro": None}

# Perguntas que o pacman faz em modo interativo. Serve para acender a caixa de
# resposta, não para decidir o que é pergunta: por isso o padrão é largo e o
# pior erro possível — não reconhecer uma — não trava nada, porque a caixa
# fica disponível durante a execução inteira.
PROMPT_RE = re.compile(
    r"(\[\?\]|\[[YySs]/[Nn]\]|\[[Nn]/[YySs]\]|do you want|"
    r"would you like|\[y/N\]|enter |proceed with|import pgp|replacing|"
    r"choose |select |(?:deseja|prosseguir|continuar|substituir|importar)"
    r".{0,120}\?)",
    re.IGNORECASE,
)

# Quanto tempo uma pergunta pode ficar sem resposta antes de o pacman desistir.
# A janela é respondida por uma pessoa que está olhando para ela, então 15
# minutos é folgado; o que importa é não haver um `pacman` de root esperando
# para sempre caso a janela seja fechada sem responder.
LIMITE_ESPERA = 900.0

# Pacotes cuja atualização só entra em vigor depois de reiniciar. Sem este
# aviso, a pessoa acha que a atualização terminou, o serviço antigo continua em
# memória, e a causa do comportamento estranho vira um mistério. Não é uma
# lista oficial do Arch: é o conjunto que costuma mudar o que já está em
# execução — kernel, init, biblioteca de C e os drivers de GPU, que trocam a
# interface em uso.
REINICIO_PACOTES = {
    "linux", "linux-lts", "linux-zen", "linux-hardened", "linux-rt",
    "linux-firmware", "linux-firmware-x86_64", "linux-firmware-intel",
    "linux-firmware-nvidia", "systemd", "systemd-libs", "systemd-ukify",
    "glibc", "nvidia", "nvidia-390xx", "nvidia-470xx", "nvidia-550xx",
    "nvidia-dkms", "mesa", "lib32-mesa", "lib32-glibc", "gcc-libs",
    "glib2", "dbus", "polkit",
}


def invalidate_updates() -> None:
    """Joga fora a lista de atualizações em cache.

    Depois de uma atualização, a lista consultada antes é história: sem isto o
    badge continuaria mostrando os mesmos pacotes por até `ATUALIZACOES_TTL`, e
    a pessoa iria clicar numa janela que não tem mais nada para fazer.
    """
    with _atualizacoes_lock:
        _atualizacoes_cache.update(
            {"quando": 0.0, "pacotes": None, "erro": None})


def precisa_reiniciar(pacote: str) -> bool:
    """Diz se a versão nova deste pacote só vale depois de reiniciar.

    Casa o nome exato e o prefixo com hífen, para pegAR tanto `linux` quanto
    `linux-zen` e `nvidia-550xx`, sem confundir `glibc` com `glib2`.
    """
    if not pacote:
        return False
    return (pacote in REINICIO_PACOTES
            or any(pacote.startswith(c + "-") for c in REINICIO_PACOTES))


def _tamanhos_de_download(nomes) -> dict:
    """Tamanho de download de cada pacote, lido do banco do pacman.

    O `.db` já tem o %CSIZE% de tudo, então não é preciso chamar o pacman: ler
    o banco é o caminho mais rápido e não depende de a base estar sincronizada
    com o momento da consulta.
    """
    out = {}
    for nome in nomes:
        info = pacman_db.lookup(nome)
        if info and info.get("csize"):
            out[nome] = info["csize"]
    return out


def pacotes_com_atualizacao(agora: bool = False) -> tuple:
    """Pacotes instalados com versão nova no repositório.

    `pacman -Qu` roda como usuário comum e só compara a base de sincronização,
    então serve para avisar na tela que há versões novas. O resultado fica em
    cache por `ATUALIZACOES_TTL`: a tela pergunta a cada minuto e não faz
    sentido repetir a consulta a cada 4 segundos. Com `agora`, ignora o cache.

    Cada item leva também `download` (bytes) e `repo`, para a interface poder
    dizer quanto vai ser baixado no total.
    """
    momento = time.time()
    with _atualizacoes_lock:
        if (not agora and _atualizacoes_cache["pacotes"] is not None
                and momento - _atualizacoes_cache["quando"] < ATUALIZACOES_TTL):
            return _atualizacoes_cache["pacotes"], _atualizacoes_cache["erro"]

    pacotes, erro = [], None
    try:
        proc = subprocess.run(
            ["pacman", "-Qu"], capture_output=True, text=True, timeout=25,
            env={**os.environ, "LC_ALL": "C", "LANG": "C", "COLUMNS": "400"})
        # "pacote 1.0-1 -> 1.1-1" (LC_ALL=C garante esse formato)
        for linha in proc.stdout.splitlines():
            if "->" not in linha:
                continue
            nome, _, resto = linha.strip().partition(" ")
            antes, _, depois = resto.partition("->")
            if not nome or not antes.strip() or not depois.strip():
                continue
            pacotes.append({"pacote": nome, "de": antes.strip(), "para": depois.strip()})
        if proc.returncode not in (0, 1):  # 1 = há atualizações, sem erro
            erro = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else None
    except Exception as exc:  # sem pacman, sem permissão, tempo esgotado...
        erro = str(exc)

    # Tamanho e repositório vêm do banco, que falha com elegância: sem ele, os
    # pacotes continuam listados, só sem o número de download.
    if pacotes:
        try:
            tamanhos = _tamanhos_de_download([p["pacote"] for p in pacotes])
        except Exception:
            tamanhos = {}
        for item in pacotes:
            info = pacman_db.lookup(item["pacote"]) or {}
            item["download"] = tamanhos.get(item["pacote"])
            item["repo"] = info.get("repo")

    with _atualizacoes_lock:
        _atualizacoes_cache.update({"quando": momento, "pacotes": pacotes, "erro": erro})
    return pacotes, erro


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

    def _serve_file(self, path: str, ctype: str = None, html_lang: str = None) -> None:
        if not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        if ctype is None:
            ext = path.rsplit(".", 1)[-1].lower()
            ctype = MIME.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        if html_lang:
            text = body.decode("utf-8", "replace")
            text, _ = re.subn(
                r'<html lang="[^"]*"', f'<html lang="{html_lang}"', text
            )
            body = text.encode("utf-8")
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

    def _broken_apps(self) -> tuple:
        """Apps do catálogo cujo pacote principal não existe mais.

        Cobre as duas fontes, porque as duas quebram:

        - `arch` é conferido no banco local do pacman, sem rede;
        - `aur` é conferido na AUR (RPC v5), que exige rede — e por isso devolve
          três respostas, não duas. Um pacote AUR que não deu para conferir
          (offline, sem cache) vai para `sem_conferencia`, **não** para
          `apps`: acusar quebra por falta de informação foi o que deixou
          `atom`, `corebird` e os dois apps do Minecraft invisíveis, marcados
          como `aur` e com pacote que não existe em lugar nenhum.

        Apps `manual` não usam pacote, e ficam de fora.

        Em ambos os casos o verificado é o **pacote principal**, que é o que o
        botão instalar baixa. Extras que sumiram (um `-data`, um plugin) não
        impedem a instalação e virariam ruído no aviso.
        """
        try:
            raw = _load_raw()
        except Exception as exc:
            return {"apps": [], "sem_conferencia": [], "erro": str(exc)}, 200

        # app_id -> o que precisa ser decidido, separado por fonte para que a
        # AUR seja consultada uma única vez, no fim.
        arch_alvos, aur_alvos = [], []
        for categoria, items in raw.items():
            if categoria in SPECIAL_KEYS or not isinstance(items, dict):
                continue
            for app_id, app in items.items():
                if not isinstance(app, dict) or app.get("listed") is False:
                    continue
                details = (app.get("pacman") or {}).get("default") or {}
                fonte = details.get("source")
                if fonte not in ("arch", "aur"):
                    continue
                principal = details.get("main-package")

                # Nome que o pacman rejeita (`steam:i386`) também é quebra: o
                # instalador nem chega a consultar o banco. Sem este caminho o
                # app sumiria da lista, porque `is_pacman_package` descarta o
                # nome antes de qualquer verificação — quebrado e invisível.
                #
                # Isso vale para as duas fontes: um nome rejeitado pelo pacman
                # também não existe na AUR.
                if not is_pacman_package(principal or ""):
                    if not principal:
                        continue
                    arch_alvos.append({
                        "category": categoria, "id": app_id,
                        "name": app.get("name") or app_id,
                        "pacote": principal, "fonte": fonte,
                        "hint": pacman_db.renamed_hint(principal),
                    })
                    continue

                (arch_alvos if fonte == "arch" else aur_alvos).append({
                    "category": categoria, "id": app_id,
                    "name": app.get("name") or app_id,
                    "pacote": principal, "fonte": fonte, "hint": None,
                })

        erro_aur = None
        aur_existe, aur_ausente, aur_desconhecidos, erro_aur_consulta = \
            aur_db.lookup_many([a["pacote"] for a in aur_alvos])
        if erro_aur_consulta:
            erro_aur = erro_aur_consulta

        quebrados = []
        sem_conferencia = []
        if not pacman_db.available():
            # Sem banco local, `lookup` devolve None para **todo** pacote, e
            # acusar o catálogo inteiro seria pior que não dizer nada. Os apps
            # do Arch entram como não conferidos, junto com os da AUR.
            erro_aur = ("sem banco de sincronização do pacman "
                        "(rode pacman -Sy)")
            sem_conferencia = [{"category": a["category"], "id": a["id"],
                                "name": a["name"], "package": a["pacote"]}
                               for a in arch_alvos]
        else:
            for alvo in arch_alvos:
                if pacman_db.lookup(alvo["pacote"]):
                    continue
                quebrados.append({
                    "category": alvo["category"], "id": alvo["id"],
                    "name": alvo["name"], "packages": [alvo["pacote"]],
                    "source": alvo["fonte"],
                    "hint": alvo["hint"] or pacman_db.renamed_hint(alvo["pacote"]),
                })

        for alvo in aur_alvos:
            pacote = alvo["pacote"]
            if pacote in aur_existe:
                continue
            if pacote in aur_desconhecidos:
                sem_conferencia.append({
                    "category": alvo["category"], "id": alvo["id"],
                    "name": alvo["name"], "package": pacote,
                })
                continue
            quebrados.append({
                "category": alvo["category"], "id": alvo["id"],
                "name": alvo["name"], "packages": [pacote],
                "source": "aur",
                # A AUR não guarda histórico de renomeação como o Arch; a RPC
                # só diz que o nome não existe. Dizer isso é melhor que
                # inventar um sucessor.
                "hint": None,
            })

        sem_conferencia.sort(key=lambda a: a["name"])
        quebrados.sort(key=lambda a: a["name"])
        return {"apps": quebrados, "total": len(quebrados),
                "sem_conferencia": sem_conferencia,
                "erro": erro_aur}, 200

    # ---- routes -----------------------------------------------------------

    def do_GET(self) -> None:
        from urllib.parse import parse_qs, urlparse

        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            _, region = resolve_store_lang()
            self._serve_file(os.path.join(WEB_DIR, "index.html"), html_lang=region)
            return

        if path == "/admin":
            if not ADMIN_ENABLED:
                self._send_json({"error": "not found"}, 404)
                return
            self._serve_file(os.path.join(WEB_DIR, "admin.html"))
            return

        if path == "/api/admin/published":
            if not ADMIN_ENABLED:
                self._send_json({"error": "not found"}, 404)
                return
            self._send_json(admin_published_info())
            return

        if path == "/api/admin/catalog":
            if not ADMIN_ENABLED:
                self._send_json({"error": "not found"}, 404)
                return
            self._send_json(admin_catalog())
            return

        if path == "/api/admin/search":
            if not ADMIN_ENABLED:
                self._send_json({"error": "not found"}, 404)
                return
            from urllib.parse import parse_qs
            query = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            if len(query.strip()) < 2:
                self._send_json({"results": [], "error": None})
                return
            if not pacman_db.available():
                self._send_json({
                    "results": [],
                    "error": "sem banco de sincronização do pacman "
                             "(rode pacman -Sy para atualizar os repositórios)",
                })
                return
            self._send_json({"results": pacman_db.search(query), "error": None})
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

        if path == "/api/updates":
            pacotes, erro = pacotes_com_atualizacao()
            conhecidos = [p["download"] for p in pacotes if p.get("download")]
            criticos = [p["pacote"] for p in pacotes
                        if precisa_reiniciar(p["pacote"])]
            self._send_json({
                "pacotes": pacotes,
                "total": len(pacotes),
                # Soma do que é possível medir. `total_bytes` só vale quando
                # `sized` == `total`: sem o banco do pacman, alguns pacotes
                # ficam sem número e a soma seria enganosa.
                "total_bytes": sum(conhecidos) or None,
                "sized": len(conhecidos),
                # Dizido antes de atualizar, e não só depois: quem decide
                # atualizar já consegue ver que vai ter de reiniciar, e não
                # descobre isso quando a janela já está fechada.
                "reiniciar": bool(criticos),
                "criticos": criticos,
                "erro": erro,
            })
            return

        if path == "/api/broken":
            # Apps cujo pacote não existe mais nos repositórios: o botão
            # instalar vai falhar, e a curadoria precisa saber para corrigir.
            data, code = self._broken_apps()
            self._send_json(data, code)
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

        if path == "/api/sysupdate":
            self._start_sysupdate()
            return

        if path == "/api/answer":
            self._answer_job()
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

        if path == "/api/admin/sync":
            if not ADMIN_ENABLED:
                self._send_json({"error": "not found"}, 404)
                return
            data, code = admin_sync_published()
            self._send_json(data, code)
            return

        if path == "/api/admin/build":
            if not ADMIN_ENABLED:
                self._send_json({"error": "not found"}, 404)
                return
            job_id = uuid4().hex[:12]
            job = BuildJob(job_id)
            with self.jobs_lock:
                self.jobs[job_id] = job
            job.start()
            self._send_json({"id": job_id, "state": "pending"})
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
        if any(not is_pacman_package(package) for package in packages):
            self._send_json({"error": "invalid package name"}, 400)
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

    def _start_sysupdate(self) -> None:
        """Atualiza o sistema inteiro com `pacman -Syu`.

        A lista de pacotes vai no job pelo mesmo motivo do aviso de reinício:
        quem decide precisa saber o que entrou, e o pacman não devolve a lista do
        que atualizou — só o que ele tinha para atualizar, que é o que foi lido
        antes. Sem isso, o aviso de reinício seria adivinhação.

        Recusa começar se já houver um job de sistema em andamento: dois
        `pacman` com o lock do banco ao mesmo tempo não terminam, e a segunda
        janela ficaria olhando um processo que só espera.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._send_json({"error": "corpo inválido"}, 400)
            return

        pacotes, erro = pacotes_com_atualizacao(agora=True)
        if not pacotes:
            # Recusa de verdade, e não uma janela vazia: o pedido veio da
            # janela, que já mostrou a lista, e ela some entre o clique e
            # aqui. Sem esta conferência a pessoa veria "nada a fazer" sem
            # entender por quê.
            self._send_json(
                {"error": "não há pacotes para atualizar",
                 "detalhe": erro}, 409)
            return

        with self.jobs_lock:
            for job in self.jobs.values():
                if getattr(job, "mode", None) == "sysupdate" and not job.done:
                    self._send_json(
                        {"error": "já há uma atualização em andamento",
                         "id": job.id}, 409)
                    return
            job_id = uuid4().hex[:12]
            job = InstallJob(job_id, [p["pacote"] for p in pacotes],
                             "arch", "sysupdate")
            self.jobs[job_id] = job
        job.start()
        self._send_json({"id": job_id, "total": len(pacotes)})

    def _answer_job(self) -> None:
        """Entrega uma resposta ao processo que está esperando por ela.

        Vale 409 quando não há ninguém esperando: assim o campo de resposta não
        fica fingindo que enviou, e a pessoa percebe que precisa rolar o log
        para ver a pergunta.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._send_json({"error": "corpo inválido"}, 400)
            return
        job_id = str(body.get("id") or "")
        texto = str(body.get("text") or "")
        if not job_id or not texto.strip():
            self._send_json({"error": "falta id ou texto"}, 400)
            return
        with self.jobs_lock:
            job = self.jobs.get(job_id)
        if job is None or not hasattr(job, "answer"):
            self._send_json({"error": "job desconhecido"}, 404)
            return
        if not job.answer(texto):
            self._send_json(
                {"error": "nenhum processo esperando resposta"}, 409)
            return
        self._send_json({"ok": True})

    do_PUT = do_POST  # convenience


def build_payload() -> dict:
    index_path = os.path.join(APPS_DIR, pick_index_file())
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
            install_packages = [
                item for item in (details.get("install-packages") or [])
                if is_pacman_package(item)
            ]
            main_package = details.get("main-package")
            if is_pacman_package(main_package) and main_package not in install_packages:
                install_packages.insert(0, main_package)
            primary_package = (
                main_package if is_pacman_package(main_package)
                else next(iter(install_packages), None)
            )
            source = details.get("source", "arch")
            # Repositório e tamanho vêm do banco do pacman, que é a mesma fonte
            # do instalador. `pacman_db.lookup` devolve None quando o pacote
            # não existe mais no Arch — aí a loja mostra o aviso em vez de um
            # número inventado, porque o botão instalar vai falhar.
            #
            # A consulta é feita pelo pacote **declarado**, e não pelo
            # `primary_package`: um nome que o pacman rejeita (`steam:i386`)
            # nunca chega a ser um `primary_package`, e consultando por ele o
            # app apareceria saudável enquanto a instalação falha.
            #
            # `declarado` é o nome **como está escrito no catálogo**, mesmo
            # quando o pacman recusa: é ele que o aviso precisa mostrar para a
            # pessoa saber o que corrigir. `consultado` é o mesmo nome quando
            # é válido, senão o primeiro da lista de instalação.
            declarado = str(main_package).strip() if main_package else None
            consultado = declarado if is_pacman_package(declarado) else (
                next(iter(install_packages), None))
            meta = None
            if source == "arch":
                meta = pacman_db.lookup(consultado)
            products.append(
                {
                    "key": f"{key}/{name}",
                    "category": key,
                    "id": name,
                    "name": package.get("name") or name,
                    "summary": package.get("summary", ""),
                    "description": package.get("description", ""),
                    "developer": package.get("developer-name"),
                    "icon": f"/media/{package.get('icon')}" if package.get("icon") else "",
                    "screenshots": [
                        f"/media/{s}" for s in package.get("screenshots", []) if s
                    ],
                    "packages": install_packages,
                    # O pacote como está escrito no catálogo, mesmo quando o
                    # pacman não aceita o nome. `packages` é a lista que o
                    # instalador vai rodar, e um nome inválido nunca chega lá:
                    # sem este campo o aviso do Steam diria "o pacote “” não
                    # existe", que não ajuda ninguém a consertar.
                    "declared": declarado,
                    "source": source,
                    "repo": (meta or {}).get("repo"),
                    "size": (meta or {}).get("isize"),
                    "download": (meta or {}).get("csize"),
                    "pkgversion": (meta or {}).get("version"),
                    # Renomeado pelo Arch não é falha: o sucessor existe e o
                    # app instala normalmente, então entra com a informação do
                    # novo nome em vez do aviso de pacote quebrado.
                    "missing": bool(source == "arch" and not meta),
                    # Por que está quebrado: renomeado, nome inválido, ou nada.
                    # "não existe mais" sozinho é falso para `steam:i386`, que
                    # nunca existiu com esse nome — o `:` é sintaxe de
                    # repositório, não de arquitetura.
                    "missing_hint": (
                        pacman_db.renamed_hint(declarado)
                        if source == "arch" and not meta else None
                    ),
                    "renamed": (meta or {}).get("renamed_from"),
                    "renamed_to": (meta or {}).get("renamed_to"),
                    "arches": package.get("arch", []),
                    "proprietary": bool(package.get("proprietary")),
                    "website": (package.get("urls") or {}).get("info"),
                    "launch": package.get("launch-cmd"),
                    "installed": bool(
                        primary_package and primary_package in installed
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
    return os.path.join(APPS_DIR, pick_index_file())


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


def _media_referenced(raw: dict) -> set:
    """Caminhos (relativos a src/apps) de icones e screenshots usados."""
    out = set()

    def walk(node):
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "icon" and isinstance(val, str):
                    out.add(val)
                elif key == "screenshots" and isinstance(val, list):
                    out.update(x for x in val if isinstance(x, str))
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(raw)
    return out


def _fetch_media(sha: str, wanted: set) -> list:
    """Baixa do repositório publicado os arquivos de mídia que faltam aqui."""
    import urllib.request

    got = []
    base = f"{REMOTE_REPO_RAW}/{sha}/src/apps/"
    for rel in sorted(wanted):
        destino = os.path.join(APPS_DIR, rel)
        if os.path.exists(destino):
            continue
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        tmp = destino + ".dl"
        try:
            req = urllib.request.Request(
                _cache_busted(base + urllib.parse.quote(rel)),
                headers={"User-Agent": "nlinux-software"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
        except Exception as exc:
            print(f"[nlinux] mídia não baixada ({rel}): {exc}")
            continue
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, destino)
        got.append(rel)
    return got


def admin_published_info() -> dict:
    """Revisão do catálogo publicado no GitHub (para comparar com o local)."""
    sha = remote_head_sha()
    if not sha:
        return {"error": "GitHub indisponível"}
    publicado = remote_marker_revision(sha)
    local = _load_raw().get("stats", {}).get("revision")
    if publicado is None or local is None:
        return {"sha": sha, "revision": publicado, "local_revision": local,
                "ahead": False}
    return {"sha": sha, "revision": publicado, "local_revision": local,
            "ahead": publicado > local}


def remote_marker_revision(sha: str):
    """revision do catálogo publicado, lida do catalog-head.json do commit."""
    import urllib.request

    url = f"{REMOTE_REPO_RAW}/{sha}/catalog-head.json"
    try:
        req = urllib.request.Request(
            _cache_busted(url), headers={"User-Agent": "nlinux-software"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")).get("revision")
    except Exception:
        return None


def admin_sync_published() -> tuple:
    """Adota o catálogo publicado, para a curadoria seguir o mesmo catálogo.

    Usado quando o catálogo muda em outra máquina (ou seja, quando a versão da
    distribuição foi gerada lá): baixa o JSON do commit publicado, adota as
    alterações, busca a mídia que faltar e reconstrói o payload.
    """
    sha = remote_head_sha()
    if not sha:
        return {"error": "não foi possível consultar o GitHub"}, 502
    try:
        remote = fetch_remote_catalog(sha)
    except Exception as exc:
        return {"error": f"catálogo publicado indisponível: {exc}"}, 502
    if not isinstance(remote, dict) or not remote:
        return {"error": "catálogo publicado vazio ou inválido"}, 502

    with _ADMIN_LOCK:
        current = _load_raw()
        antes = current.get("stats", {}).get("revision")
        if json.dumps(remote, sort_keys=True, ensure_ascii=False) == \
                json.dumps(current, sort_keys=True, ensure_ascii=False):
            return {"ok": True, "unchanged": True, "revision": antes,
                    "media": []}, 200
        antes_ids = _catalog_ids(current)
        media = _fetch_media(sha, _media_referenced(remote))
        _write_raw(remote)
        _gc_assets(remote)
        exportados = _export_to_source(remote)
        rebuild_payload()
        depois = remote.get("stats", {}).get("revision")
        novos = sorted(_catalog_ids(remote) - antes_ids)
        saida = sorted(antes_ids - _catalog_ids(remote))
    print(f"[nlinux] catálogo da curadoria atualizado do publicado "
          f"(revision {antes} -> {depois}; {len(saida)} removidos, "
          f"{len(novos)} novos, {len(media)} midia baixada)", flush=True)
    return {"ok": True, "unchanged": False, "sha": sha, "de": antes,
            "revision": depois, "media": media, "removidos": saida,
            "novos": novos, "exportados": exportados}, 200


def _catalog_ids(raw: dict) -> set:
    return {f"{cat}/{ident}" for cat, val in raw.items()
            if cat not in SPECIAL_KEYS and isinstance(val, dict)
            for ident in val}


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


def _registered_source() -> str | None:
    """Projeto de curadoria registrado, para o git enxergar o catálogo vivo.

    A curadoria instalada grava em ~/.local/share/nlinux/admin/apps (o /opt é do
    sistema). Sem isto, o src/apps do projeto — que é o que está no git —
    ficaria defasado a cada edição salva.
    """
    marker = os.path.join(os.path.dirname(APPS_DIR), "source-path")
    try:
        with open(marker, encoding="utf-8") as fh:
            path = fh.read().strip()
    except OSError:
        path = ""
    if path and os.path.isdir(os.path.join(path, "src", "apps")) \
            and os.path.exists(os.path.join(path, ".git")):
        return path
    fallback = os.path.join(os.path.expanduser("~"), "nlinux-software-admin")
    if os.path.isdir(os.path.join(fallback, "src", "apps")) \
            and os.path.exists(os.path.join(fallback, ".git")):
        return fallback
    return None


def _export_to_source(raw: dict) -> list:
    """Copia catálogo e mídia para o projeto registrado (o que o git versiona)."""
    src = _registered_source()
    if not src or os.path.realpath(src) == os.path.realpath(SRC_ROOT):
        return []
    dest = os.path.join(src, "src", "apps")
    os.makedirs(dest, exist_ok=True)
    shutil.copyfile(_index_path(), os.path.join(dest, "applications-en.json"))
    copiados = []
    for rel in _media_referenced(raw):
        origem = os.path.join(APPS_DIR, rel)
        alvo = os.path.join(dest, rel)
        if not os.path.exists(origem):
            continue
        if os.path.exists(alvo) and \
                os.path.getmtime(alvo) >= os.path.getmtime(origem):
            continue
        os.makedirs(os.path.dirname(alvo), exist_ok=True)
        shutil.copyfile(origem, alvo)
        copiados.append(rel)

    # O projeto é o que o git versiona: mídia que saiu do catálogo não pode
    # ficar lá órfã, senão cada app removido deixa arquivo para sempre.
    dest_assets = os.path.join(dest, "assets")
    if os.path.isdir(dest_assets):
        referenciados = set(_media_referenced(raw))
        removidos = 0
        for raiz, _pastas, arquivos in os.walk(dest_assets, topdown=False):
            for arquivo in arquivos:
                caminho = os.path.join(raiz, arquivo)
                rel = os.path.relpath(caminho, dest)
                if rel in referenciados:
                    continue
                try:
                    os.remove(caminho)
                    removidos += 1
                except OSError:
                    pass
            if raiz != dest_assets and not os.listdir(raiz):
                try:
                    os.rmdir(raiz)
                except OSError:
                    pass
        if removidos:
            print(f"[nlinux] {removidos} midia orfa removida do projeto", flush=True)

    print(f"[nlinux] catalogo exportado para {dest}"
          + (f" ({len(copiados)} midia)" if copiados else ""), flush=True)
    return copiados


def _gc_assets(raw: dict) -> None:
    referenced = _media_referenced(raw)
    assets_dir = _assets_dir()
    if not os.path.isdir(assets_dir):
        return
    for root, dirs, files in os.walk(assets_dir, topdown=False):
        for fname in files:
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, APPS_DIR)
            if rel == os.path.join("assets", "nlinux-logo.png") or rel in referenced:
                continue
            try:
                os.remove(path)
            except OSError:
                pass
        for dirname in dirs:
            path = os.path.join(root, dirname)
            try:
                os.rmdir(path)
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
                assets_dir = _assets_dir()
                os.makedirs(assets_dir, exist_ok=True)
                with open(os.path.join(assets_dir, os.path.basename(rel)), "wb") as fh:
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
                assets_dir = _assets_dir()
                os.makedirs(assets_dir, exist_ok=True)
                with open(os.path.join(assets_dir, os.path.basename(rel)), "wb") as fh:
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
            _export_to_source(raw)
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
            _export_to_source(raw)
        except OSError:
            return {"error": "falha ao gravar o catálogo"}, 500
        _gc_assets(raw)
        rebuild_payload()
    return {"ok": True}, 200


def _write_assets_manifest(pkg_root: str, revision: int) -> None:
    assets_dir = os.path.join(pkg_root, "src", "apps", "assets")
    files = {}
    for root, _, names in os.walk(assets_dir):
        for name in names:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, os.path.join(pkg_root, "src", "apps"))
            rel = rel.replace(os.sep, "/")
            if not ASSET_RE.fullmatch(rel):
                continue
            digest = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            files[rel] = digest.hexdigest()

    manifest = {"revision": revision, "files": files}
    path = os.path.join(pkg_root, "catalog-assets.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def admin_build(progress=None):
    """Gera o pacote de distribuição da loja (sem a opção de administração).

    `progress` recebe uma função de log usada pela execução via polkit, para
    mostrar cada etapa na interface enquanto o processo roda.
    """
    import shutil
    import tarfile

    def _step(msg):
        if progress:
            progress(msg)

    with _ADMIN_LOCK:
        raw = _load_raw()
        rev = int(raw.get("stats", {}).get("revision", 0))
        name = f"nlinux-software-v{rev}"
        pkg_root = os.path.join(DIST_DIR, "nlinux-software")
        tar_path = os.path.join(DIST_DIR, f"{name}.tar.gz")
        if os.path.exists(pkg_root):
            shutil.rmtree(pkg_root)

        try:
            _step(f"preparando {pkg_root}")
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

            # Fora do projeto de desenvolvimento o catálogo e a mídia não moram
            # em src/apps, e sim na pasta gravável do usuário. O build tem que
            # levar o catálogo vivo, não o snapshot que veio no pacote.
            if os.path.realpath(APPS_DIR) != os.path.realpath(
                    os.path.join(SRC_ROOT, "apps")):
                    apps_target = os.path.join(pkg_root, "src", "apps")
                    shutil.rmtree(apps_target)
                    shutil.copytree(APPS_DIR, apps_target)

            src_ws = os.path.join(pkg_root, "src", "nlinux", "web_server.py")
            with open(src_ws, encoding="utf-8") as fh:
                content = fh.read()
            # Ancora no começo da linha, e não na primeira ocorrência do texto.
            # Com `replace(..., 1)` o alvo era a primeira vez que a string
            # aparecia no arquivo, e essa era a própria linha do `replace` —
            # enquanto o `True` de verdade, na atribuição, continuava. Bastava
            # um comentário ou docstring citando `ADMIN_ENABLED = True` antes
            # dela para a distro passar a embarcar a curadoria, e o build não
            # reclama de nada: ele só geraria um pacote com o `/admin` ligado.
            content, trocou = re.subn(r"^(ADMIN_ENABLED\s*=\s*)True",
                                      r"\1False", content, count=1,
                                      flags=re.MULTILINE)
            if not trocou:
                raise RuntimeError(
                    "a atribuição de ADMIN_ENABLED não foi encontrada em "
                    f"{src_ws}; a distro sairia com a curadoria ligada")
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
                    "# ls ordena alfabeticamente, e 'v115' < 'v211': com varios\n"
                    "# tarballs na pasta, head -n1 pegava o MAIS ANTIGO, nao o\n"
                    "# mais novo. sort -V ordena por numero de versao.\n"
                    "TARBALL=\"$(ls \"$SRC\"/nlinux-software-v*.tar.gz \"$SRC\"/../nlinux-software-v*.tar.gz 2>/dev/null | sort -V | tail -n1)\"\n"
                    "if [ -z \"$TARBALL\" ]; then\n"
                    "  echo \"Aviso: nenhum nlinux-software-v<N>.tar.gz encontrado em $SRC.\"\n"
                    "  echo \"         Instalando a loja a partir do src/ sem validar assinatura.\"\n"
                    "elif [ -f \"$TARBALL.asc\" ] && [ -f \"$SRC/nlinux-software_pub.asc\" ]; then\n"
                    "  # set -e + redirecionamento silencioso aqui ja matou o\n"
                    "  # instalador sem mensagem alguma quando a chave publica faltava.\n"
                    "  if ! gpg --batch --import \"$SRC/nlinux-software_pub.asc\" >/dev/null 2>&1; then\n"
                    "    echo \"ERRO: nlinux-software_pub.asc não pôde ser importada.\" >&2\n"
                    "    echo \"O pacote está sem a chave pública: baixe o repositório inteiro.\" >&2\n"
                    "    exit 1\n"
                    "  fi\n"
                    "  if ! gpg --batch --verify \"$TARBALL.asc\" \"$TARBALL\" >/dev/null 2>&1; then\n"
                    "    echo \"ERRO: assinatura GPG do pacote INVÁLIDA. Instalação abortada.\" >&2\n"
                    "    echo \"O arquivo (ou o repositório) pode ter sido adulterado. Baixe de novo.\" >&2\n"
                    "    exit 1\n"
                    "  fi\n"
                    "  echo \"Verificação GPG: OK (pacote autêntico da curadoria).\"\n"
                    "else\n"
                    "  echo \"ERRO: assinatura ou chave pública ausente; sem validação.\" >&2\n"
                    "  echo \"Esperado junto de $(basename \"$TARBALL\"):\" >&2\n"
                    "  echo \"  $(basename \"$TARBALL\").asc  e  nlinux-software_pub.asc\" >&2\n"
                    "  echo \"O pacote está incompleto. Baixe o repositório inteiro.\" >&2\n"
                    "  exit 1\n"
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
                    "# --- dados gravaveis, fora do /opt ---------------------------------\n"
                    "# O catalogo e a midia mudam toda vez que a loja sincroniza com o\n"
                    "# GitHub. Em /opt isso pediria root, entao vao para o HOME do\n"
                    "# usuario que instalou, com a propriedade dele.\n"
                    "STORE_USER=\"${SUDO_USER:-root}\"\n"
                    "STORE_HOME=\"$(getent passwd \"$STORE_USER\" | cut -d: -f6)\"\n"
                    "if [ -n \"$STORE_HOME\" ] && [ \"$STORE_USER\" != \"root\" ]; then\n"
                    "  STORE_DATA=\"${XDG_DATA_HOME:-$STORE_HOME/.local/share}\"\n"
                    "  APP_DATA=\"$STORE_DATA/nlinux\"\n"
                    "  DATA_DIR=\"$APP_DATA/store\"\n"
                    "  mkdir -p \"$DATA_DIR\"\n"
                    "  if [ ! -d \"$DATA_DIR/apps\" ]; then\n"
                    "    cp -a \"$DEST/src/apps\" \"$DATA_DIR/apps\"\n"
                    "  fi\n"
                    "  # o chown tem de pegtar a arvore inteira: um 'mkdir -p' como root\n"
                    "  # deixa os diretorios intermediarios do root, e sem dono o usuario\n"
                    "  # nao consegue criar mais nada dentro deles\n"
                    "  chown -R \"$STORE_USER\" \"$APP_DATA\"\n"
                    "  echo \"Catalogo e midia: $DATA_DIR/apps\"\n"
                    "fi\n"
                    "# --- ícone + atalho no menu de aplicativos --------------------------\n"
                    "mkdir -p /usr/share/pixmaps\n"
                    "cp \"$DEST/src/apps/nlinux-logo.png\" /usr/share/pixmaps/nlinux-software.png\n"
                    "rm -f /usr/share/icons/hicolor/scalable/apps/nlinux-software.svg\n"
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
                    "Os bancos do pacman são atualizados com `pacman -Sy` ao iniciar\n"
                    "uma instalação, não ao abrir a loja.\n"
                )

            _step(f"empacotando {os.path.basename(tar_path)}")
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(pkg_root, arcname=f"{name}/nlinux-software")
            size = os.path.getsize(tar_path)

            # Marcador de versão do catálogo: a loja lê só este arquivo (~100
            # bytes) a cada 15 s para saber se precisa baixar o catálogo inteiro.
            marker = {
                "revision": rev,
                "compiled": raw.get("stats", {}).get("compiled"),
                "apps": raw.get("stats", {}).get("apps"),
                "sha1": _catalog_fingerprint(raw),
                "published": int(time.time()),
            }
            with open(os.path.join(pkg_root, "catalog-head.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(marker, fh, ensure_ascii=False, indent=2)
            _write_assets_manifest(pkg_root, rev)

            # Assinatura GPG real (detached, armadura ASCII) pela curadoria.
            _step("assinando com GPG")
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
            else:
                # Sem a chave publica no pacote, o install.sh da outra
                # maquina nao consegue validar a assinatura: ou morre em
                # silencio (set -e) ou instala sem verificar. Gerava um
                # pacote inutilizavel sem ninguem avisar -- v204 em diante.
                print("[nlinux] AVISO: ~/nlinux-software_pub.asc não existe; "
                      "o pacote vai sair sem a chave pública e o instalador "
                      "não conseguirá validar a assinatura.", flush=True)
                print("[nlinux] Gere com: "
                      "gpg --armor --export <FINGERPRINT> > ~/nlinux-software_pub.asc",
                      flush=True)

            _step("publicando no GitHub")
            publish = git_publish(pkg_root, tar_path, rev)
            _step("publicação concluída" if publish.get("pushed")
                  else f"publicação falhou: {publish.get('reason', '?')}")

            _step("sincronizando a loja do iso-build")
            iso = sync_iso_store(pkg_root, rev)
            _iso_note = iso.get("commit", {})
            if not iso.get("synced"):
                _step(f"iso-build não sincronizado: {iso.get('reason', '?')}")
            elif not _iso_note.get("committed"):
                _step(f"iso-build copiado, sem commit: {_iso_note.get('reason', '?')}")
            else:
                _step("loja do iso-build commitada em " + _iso_note["head"])
                if iso.get("push", {}).get("pushed"):
                    _step("iso-build publicado no GitHub")
                elif "push" in iso:
                    _step(f"iso-build não publicado: {iso['push'].get('reason', '?')}")

            _step("publicando alterações da curadoria no GitHub")
            admin_source = git_publish_admin_source(rev)
            _step("repositório da curadoria publicado" if admin_source.get("pushed")
                  else f"repositório da curadoria não publicado: {admin_source.get('reason', '?')}")
        except OSError as e:
            return {"error": f"falha ao gerar o pacote: {e}"}, 500

    return {"ok": True, "revision": rev, "path": tar_path, "run": pkg_root,
            "size": size, "publish": publish, "iso": iso,
            "admin_source": admin_source,
            # De qual catálogo o pacote saiu. O build roda em processo separado
            # (pkexec), que recalcula o caminho a partir do papel; sem este
            # campo, um caminho errado só apareceria quando a Loja mostrasse o
            # catálogo antigo — e já teria sido publicado.
            "apps_dir": APPS_DIR}, 200


_ISO_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git")


def sync_iso_store(build_dir: str, rev: int) -> dict:
    """Espelha o build em `<iso-build>/nlinux-software/` e commita.

    É a pasta que o `build-iso.sh` copia para `/opt/nlinux-software` dentro da
    imagem, e ela é versionada no repositório do iso-build. Sem este espelho
    ela congela na revisão commitada: a imagem sai com a loja antiga, com o
    catálogo antigo e sem as correções do build.

    O commit é o que fecha o ciclo: a imagem é construída a partir do que está
    commitado, então deixar a pasta modificada faria a ISO montar uma loja
    diferente da que está no repositório — irreprodutível. Só a subpasta da loja
    entra no commit, para não varrer junto o que estiver em edição no projeto
    da ISO.

    Não propaga erro — falhar aqui não invalida o pacote já gerado e
    publicado, que é a parte importante.
    """
    dest = os.path.join(ISO_BUILD_DIR, ISO_STORE_DIRNAME)
    if not os.path.isdir(ISO_BUILD_DIR):
        return {"synced": False,
                "reason": f"{ISO_BUILD_DIR} não existe (ajuste com NLINUX_ISO_DIR)"}
    try:
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        shutil.copytree(build_dir, dest, ignore=_ISO_IGNORE)
        launcher = os.path.join(dest, "nlinux-software")
        if os.path.exists(launcher):
            os.chmod(launcher, 0o755)
    except OSError as exc:
        return {"synced": False, "reason": str(exc)}
    print(f"[nlinux] loja do iso-build sincronizada: {dest} (v{rev})", flush=True)
    result = {"synced": True, "path": dest, "revision": rev}
    result["commit"] = _iso_commit(ISO_STORE_DIRNAME, rev)
    if ISO_PUSH_ENABLED and result["commit"].get("committed"):
        result["push"] = _iso_push()
    return result


def _git(repo: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args],
                          capture_output=True, text=True, timeout=timeout)


def git_publish_admin_source(rev: int) -> dict:
    """Commita e envia as alterações do checkout privado da curadoria."""
    if os.environ.get("NLINUX_ADMIN_GIT_PUSH", "1") in ("0", "false", "no"):
        return {"pushed": False, "reason": "publicação desativada (NLINUX_ADMIN_GIT_PUSH=0)"}

    repo = _registered_source() or os.path.dirname(SRC_ROOT)
    root = _git(repo, "rev-parse", "--show-toplevel")
    if root.returncode != 0:
        return {"pushed": False, "reason": f"checkout Git não encontrado em {repo}"}
    repo = os.path.realpath(root.stdout.strip())
    upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if upstream.returncode != 0:
        return {"pushed": False, "reason": "branch da curadoria sem upstream configurado"}

    try:
        status = _git(repo, "status", "--porcelain")
        if status.returncode != 0:
            return {"pushed": False, "reason": status.stderr.strip() or "git status falhou"}
        committed = False
        if status.stdout.strip():
            add = _git(repo, "add", "-A")
            if add.returncode != 0:
                return {"pushed": False, "reason": add.stderr.strip() or "git add falhou"}
            commit_env = os.environ.copy()
            commit_env.setdefault("GIT_AUTHOR_NAME", GIT_PUSH_USER)
            commit_env.setdefault("GIT_AUTHOR_EMAIL", GIT_PUSH_EMAIL)
            commit_env.setdefault("GIT_COMMITTER_NAME", GIT_PUSH_USER)
            commit_env.setdefault("GIT_COMMITTER_EMAIL", GIT_PUSH_EMAIL)
            commit = subprocess.run(
                ["git", "-C", repo, "commit", "-m",
                 f"NLinux Software Admin: publicação v{rev}"],
                capture_output=True, text=True, env=commit_env,
            )
            if commit.returncode != 0:
                return {"pushed": False,
                        "reason": commit.stderr.strip() or "git commit falhou"}
            committed = True

        push = _git(repo, "push")
        if push.returncode != 0:
            return {"pushed": False, "committed": committed,
                    "reason": (push.stderr or push.stdout).strip() or "git push falhou"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"pushed": False, "reason": str(exc)}
    return {"pushed": True, "committed": committed,
            "branch": upstream.stdout.strip(), "repo": repo}


def _iso_commit(subdir: str, rev: int) -> dict:
    """Commita a subpasta da loja no repositório da ISO.

    Versionado=False deixa só a cópia no disco. O `add` é restrito à subpasta
    da loja: o resto do projeto da ISO pode estar com edição em andamento, e
    isso não é da conta da curadoria.
    """
    if not ISO_COMMIT_ENABLED:
        return {"committed": False, "reason": "desligado (NLINUX_ISO_COMMIT=0)"}
    if not os.path.isdir(os.path.join(ISO_BUILD_DIR, ".git")):
        return {"committed": False, "reason": f"{ISO_BUILD_DIR} não é um repositório git"}
    try:
        if _git(ISO_BUILD_DIR, "add", "-A", "--", subdir).returncode != 0:
            raise OSError("git add falhou")
        if _git(ISO_BUILD_DIR, "diff", "--cached", "--quiet",
                "--", subdir).returncode == 0:
            return {"committed": False, "reason": "nada a commitar (já no repositório)"}
        msg = (f"Loja embarcada: revisão {rev}\n\n"
               f"Espelhado de {ISO_STORE_DIRNAME} pela curadoria ao gerar a versão "
               f"da loja (revisão {rev}). Esta pasta é o que o build-iso.sh "
               f"embute em /opt/nlinux-software na imagem.")
        res = _git(ISO_BUILD_DIR, "commit", "-m", msg)
        if res.returncode != 0:
            return {"committed": False, "reason": res.stderr.strip() or "git commit falhou"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"committed": False, "reason": str(exc)}
    head = _git(ISO_BUILD_DIR, "rev-parse", "--short", "HEAD").stdout.strip()
    print(f"[nlinux] loja do iso-build commitada: {head}", flush=True)
    return {"committed": True, "head": head}


def _iso_push() -> dict:
    """Sobe o commit da loja para o repositório da ISO. Nunca levanta."""
    try:
        res = _git(ISO_BUILD_DIR, "push")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"pushed": False, "reason": str(exc)}
    if res.returncode != 0:
        reason = (res.stderr or res.stdout).strip().splitlines()
        return {"pushed": False, "reason": reason[-1] if reason else "git push falhou"}
    print("[nlinux] iso-build publicado no GitHub", flush=True)
    return {"pushed": True}


def _prepare_git_clone() -> str:
    """Garante o repositório local (~/nlinux-software) clonado do GitHub e o retorna."""
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
        # Atualiza a referência de origem ANTES de mexer no clone. O
        # --force-with-lease do push compara o remoto com
        # refs/remotes/origin/<branch>: se ela estiver velha (o remoto andou
        # de outra máquina, ou por web), o push é recusado com "stale info".
        fetch = subprocess.run(
            ["git", "-C", clone_dir, "fetch", "--quiet", "origin",
             f"+refs/heads/{GIT_PUSH_BRANCH}"
             f":refs/remotes/origin/{GIT_PUSH_BRANCH}"],
            capture_output=True, text=True,
        )
        if fetch.returncode != 0:
            return {"pushed": False,
                    "reason": fetch.stderr.strip() or "git fetch falhou"}

        # Trava anti-retrocesso. A revisão sai do catálogo local, e o mesmo
        # repositório pode ser publicado por mais de uma máquina: se o remoto
        # já estiver à frente, o push de aqui sobrescreveria um catálogo mais
        # novo com um mais velho. Melhor recusar e pedir a sincronização.
        if not os.environ.get("NLINUX_PUBLISH_FORCE"):
            head = subprocess.run(
                ["git", "-C", clone_dir, "show",
                 f"refs/remotes/origin/{GIT_PUSH_BRANCH}:catalog-head.json"],
                capture_output=True, text=True,
            )
            if head.returncode == 0 and head.stdout.strip():
                try:
                    remoto_rev = int(
                        json.loads(head.stdout).get("revision", 0))
                except (ValueError, TypeError):
                    remoto_rev = None
                if remoto_rev is not None and remoto_rev > rev:
                    return {"pushed": False, "reason": (
                        f"o GitHub está na revisão {remoto_rev} e o catálogo "
                        f"local está na {rev}. Publicar agora sobrescreveria "
                        f"um catálogo mais novo com um mais antigo — "
                        f"sincronize o catálogo com o GitHub e gere de novo. "
                        f"Para publicar mesmo assim, gere com "
                        f"NLINUX_PUBLISH_FORCE=1.")}

    except Exception as exc:
        return {"pushed": False, "reason": str(exc)}

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
        if status.stdout.strip():
            commit = _git(
                "commit", "-q",
                "-m", f"NLinux Software v{rev}: catálogo atualizado",
                env=author_env,
            )
            if commit.returncode != 0:
                return {"pushed": False,
                        "reason": commit.stderr.strip() or "git commit falhou"}
        # O push acontece mesmo sem commit novo: um build anterior pode ter
        # commitado e falhado só na publicação, e nesse caso a árvore está
        # limpa mas o remoto continua desatualizado.
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

# O CDN do raw.githubusercontent serve versões velhas por vários minutos (inclusive
# 304 obsoletos), então a loja não confia nele para saber se houve mudança. A
# detecção usa `git ls-remote`, que responde ao vivo em menos de 1s, e o catálogo
# é lido de uma URL travada no commit — imutável, portanto sempre correta.
REMOTE_REPO_RAW = os.environ.get("NLINUX_CATALOG_RAW_BASE") or \
    "https://raw.githubusercontent.com/nilsonlinux/nlinux-software"
REMOTE_FALLBACK_SECONDS = 300


def _cache_busted(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}cb={int(time.time())}"


def _remote_get(url: str, timeout: int = 20):
    """GET sem cache: o parâmetro cb= força o CDN a buscar no servidor."""
    return json.loads(_remote_get_bytes(url, timeout).decode("utf-8"))


def _remote_get_bytes(url: str, timeout: int = 20) -> bytes:
    import urllib.request

    req = urllib.request.Request(
        _cache_busted(url),
        headers={"User-Agent": "nlinux-software", "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def remote_head_sha() -> str | None:
    """Commit atual da branch main no GitHub (via git, sem cache de CDN)."""
    try:
        out = subprocess.run(
            ["git", "ls-remote", GIT_PUSH_URL, GIT_PUSH_BRANCH],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout.split():
        return None
    return out.stdout.split()[0]


def catalog_url_at(sha: str) -> str:
    return f"{REMOTE_REPO_RAW}/{sha}/src/apps/{pick_index_file()}"


def fetch_remote_catalog(sha: str | None = None) -> dict:
    """Baixa o catálogo. Com sha, lê a URL travada no commit (sempre atual)."""
    return _remote_get(catalog_url_at(sha) if sha else REMOTE_CATALOG_URL)


def _catalog_fingerprint(raw: dict) -> str:
    return hashlib.sha1(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


_REMOTE_SYNC = {"applied": None, "sha": None, "last_full": 0.0}


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
        # o arquivo local mudou por fora (curadoria editando, merge, restauração
        # de backup): rebaixa. Não conta como pedido explícito do usuário, então
        # a trava de revisão abaixo ainda vale.
        rebaixar = True
    else:
        rebaixar = force

    sha = remote_head_sha()
    if not rebaixar:
        if sha is None:
            # Sem git disponível: usa o marcador minúsculo; se nem ele responder,
            # rebaixa o catálogo inteiro no máximo a cada 5 minutos.
            try:
                marker = _remote_get(REMOTE_MARKER_URL, timeout=15)
                _REMOTE_SYNC["last_full"] = time.time()
                if _local_matches_marker(local, marker):
                    assets_key = marker.get("sha1")
                    if assets_key and _REMOTE_SYNC.get("assets") != assets_key:
                        if _sync_remote_assets(local, None):
                            _REMOTE_SYNC["assets"] = assets_key
                    return False
            except Exception:
                if time.time() - _REMOTE_SYNC["last_full"] < REMOTE_FALLBACK_SECONDS:
                    return False
                _REMOTE_SYNC["last_full"] = time.time()
        elif sha == _REMOTE_SYNC["sha"]:
            if _REMOTE_SYNC.get("assets") != sha:
                if _sync_remote_assets(local, sha):
                    _REMOTE_SYNC["assets"] = sha
            return False  # nada novo publicado no repositório
    else:
        _REMOTE_SYNC["last_full"] = time.time()

    try:
        remote = fetch_remote_catalog(sha)
    except Exception as exc:
        print(f"[nlinux] catálogo remoto indisponível: {exc}")
        return False
    if not isinstance(remote, dict) or not remote:
        print("[nlinux] catálogo remoto vazio ou inválido; mantido o atual")
        return False

    # Trava de recuo, espelhando a da publicação. A sincronização só comparava
    # conteúdo: qualquer edição local que ainda não foi publicada era apagada
    # assim que o remoto devolvesse um catálogo diferente — inclusive na
    # partida, quando a referência de commit é desconhecida. Sem isso não há
    # como curatejar e publicar: o trabalho some antes de chegar ao GitHub.
    # `force` explícito (pedido do usuário) continua prevalecendo.
    rev_local = int((local.get("stats") or {}).get("revision") or 0)
    rev_remoto = int((remote.get("stats") or {}).get("revision") or 0)
    if not force and rev_remoto and rev_local > rev_remoto:
        print(f"[nlinux] catálogo local na revisão {rev_local} é mais novo que o "
              f"remoto ({rev_remoto}); mantido o local", flush=True)
        _REMOTE_SYNC["applied"] = _catalog_fingerprint(local)
        _REMOTE_SYNC["sha"] = sha
        return False

    assets_synced = _sync_remote_assets(remote, sha)
    assets_key = sha or _catalog_fingerprint(remote)
    with _ADMIN_LOCK:
        current = _load_raw()
        same = json.dumps(remote, sort_keys=True, ensure_ascii=False) == \
            json.dumps(current, sort_keys=True, ensure_ascii=False)
        if same:
            _REMOTE_SYNC["applied"] = _catalog_fingerprint(remote)
            _REMOTE_SYNC["sha"] = sha
            if assets_synced:
                _REMOTE_SYNC["assets"] = assets_key
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
        _gc_assets(remote)
        rebuild_payload()
        revision = remote.get("stats", {}).get("revision")
        _REMOTE_SYNC["applied"] = _catalog_fingerprint(remote)
        _REMOTE_SYNC["sha"] = sha
        if assets_synced:
            _REMOTE_SYNC["assets"] = assets_key
    print(f"[nlinux] catálogo atualizado do repositório remoto "
          f"(revision {revision}, commit {sha[:8] if sha else 'local'})", flush=True)
    return True


def _sync_remote_assets(raw: dict, sha: str | None) -> bool:
    """Atualiza a mídia referenciada pelo catálogo a partir da mesma revisão."""
    manifest_url = (
        f"{REMOTE_REPO_RAW}/{sha}/catalog-assets.json" if sha
        else f"{REMOTE_REPO_RAW}/main/catalog-assets.json"
    )
    try:
        manifest = _remote_get(manifest_url)
    except (OSError, ValueError) as exc:
        print(f"[nlinux] manifesto de mídia remoto indisponível: {exc}")
        return False

    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict):
        print("[nlinux] manifesto de mídia remoto inválido")
        return False

    referenced = set()
    for category, items in raw.items():
        if category in SPECIAL_KEYS or not isinstance(items, dict):
            continue
        for app in items.values():
            if not isinstance(app, dict):
                continue
            icon = app.get("icon")
            if isinstance(icon, str) and ASSET_RE.fullmatch(icon):
                referenced.add(icon)
            for shot in app.get("screenshots") or []:
                if isinstance(shot, str) and ASSET_RE.fullmatch(shot):
                    referenced.add(shot)

    updated = 0
    complete = True
    for rel in sorted(referenced):
        expected = files.get(rel)
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected):
            print(f"[nlinux] mídia ausente ou inválida no manifesto: {rel}")
            complete = False
            continue

        destination = os.path.join(APPS_DIR, rel)
        try:
            with open(destination, "rb") as fh:
                local_hash = hashlib.sha256(fh.read()).hexdigest()
        except FileNotFoundError:
            local_hash = ""
        except OSError as exc:
            print(f"[nlinux] não foi possível ler a mídia local {rel}: {exc}")
            complete = False
            continue
        if local_hash == expected:
            continue

        asset_url = (
            f"{REMOTE_REPO_RAW}/{sha}/src/apps/{rel}" if sha
            else f"{REMOTE_REPO_RAW}/main/src/apps/{rel}"
        )
        try:
            data = _remote_get_bytes(asset_url)
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("hash diferente do manifesto")
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            tmp = destination + f".sync-{os.getpid()}"
            try:
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, destination)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            updated += 1
        except (OSError, ValueError) as exc:
            print(f"[nlinux] falha ao sincronizar mídia {rel}: {exc}")
            complete = False

    if updated:
        print(f"[nlinux] {updated} arquivo(s) de mídia atualizado(s)", flush=True)
    return complete


def remote_refresh_loop() -> None:
    """Ciclo de checagem do catálogo remoto.

    A cada REMOTE_CHECK_SECONDS pergunta ao GitHub qual é o commit da main
    (`git ls-remote`, ~0,6 s). Só quando o commit muda é que o catálogo é
    baixado, pela URL travada nesse commit. A loja, que consulta o payload a cada
    poucos segundos, recarrega sozinha.
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
        os.path.dirname(os.path.abspath(__file__)),
        "..", "apps", "nlinux-logo.png")
    icon_name = "nlinux-software-admin"
    icons_root = os.path.join(
        os.path.expanduser("~"), ".local", "share", "icons", "hicolor")
    icons_dir = os.path.join(icons_root, "128x128", "apps")
    try:
        os.makedirs(icons_dir, exist_ok=True)
        shutil.copy2(icon_src, os.path.join(icons_dir, icon_name + ".png"))
        # ícone antigo em SVG sobrepunha o PNG no tema (scalable tem prioridade)
        stale = os.path.join(icons_root, "scalable", "apps", icon_name + ".svg")
        if os.path.exists(stale):
            os.remove(stale)
        updater = shutil.which("gtk-update-icon-cache")
        if updater:
            subprocess.run([updater, "-f", "-t", icons_root],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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