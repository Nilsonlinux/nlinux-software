"""Metadados de pacotes: repositório, tamanho e versão vinda do Arch.

A fonte é o **banco de sincronização do próprio pacman**
(`/var/lib/pacman/sync/*.db`), e não a API do archlinux.org. A API traz os
mesmos campos, mas derruba o IP depois de ~150 requisições e não aceita
listagem por repositório nem paginação — o que tornaria a loja dependente de
uma chamada por app, no momento em que ela abre. O banco local é a mesma fonte
que o instalador usa: se o pacote não está lá, o `pacman` não instala.

Cada `.db` é um tar.gz cujas entradas terminam em `/desc` e trazem campos no
formato `%CHAVE%\\nvalor\\n\\n` — o valor fica na linha seguinte, nunca na
mesma. Os campos que importam aqui:

    %NAME%     nome do pacote (o do diretório traz a versão com epoch)
    %VERSION%  versão
    %CSIZE%    tamanho comprimido, em bytes (o download)
    %ISIZE%    tamanho instalado, em bytes
    %DESC%     descrição de uma linha
    %URL%      site do projeto
    %PROVIDES% provides separados por espaço

A leitura é feita uma vez e memorizada: 15 mil pacotes em ~2 s. Como o banco só
muda quando o usuário sincroniza, não há ganho em reler a cada requisição.
"""

import os
import re
import tarfile
import threading

# Repositórios lidos, na ordem de preferência. `core` vem primeiro de propósito:
# quando um pacote aparece em dois repositórios, o do core é o que o pacman
# instala por padrão.
SYNC_DIR = "/var/lib/pacman/sync"
REPO_FILES = ("core", "extra", "multilib")

# Um pacote do catálogo pode ter sido renomeado. Não é um dicionário de
# sinônimos para consulta (o nome antigo simplesmente não existe), é a lista
# do queKnown-good: quando o nome do catálogo não está no banco, ainda vale
# tentar o sucessor, para o card não ficar sem tamanho à toa.
#   nome no catálogo -> (sucessor, data em que o Arch renomeou)
RENAMED = {
    "freeciv": ("freeciv-gtk3", "2026-05"),
    "libreoffice-core": ("libreoffice-fresh", "2026-08"),
    "libreoffice-base": ("libreoffice-fresh", "2026-08"),
    "libreoffice-math": ("libreoffice-fresh", "2026-08"),
    "libreoffice-writer": ("libreoffice-fresh", "2026-08"),
    "libreoffice-calc": ("libreoffice-fresh", "2026-08"),
    "libreoffice-draw": ("libreoffice-fresh", "2026-08"),
    "libreoffice-impress": ("libreoffice-fresh", "2026-08"),
    "libreoffice-gnome": ("libreoffice-fresh", "2026-08"),
    "libreoffice-gtk2": ("libreoffice-fresh", "2026-08"),
    "aisleriot": ("gnome-games", "2024-10"),
    "gnome-cards": ("gnome-games", "2024-10"),
    "gnome-sudoku": ("gnome-games", "2024-10"),
    "gnome-mines": ("gnome-games", "2024-10"),
    "gnome-mahjongg": ("gnome-games", "2024-10"),
    "gnome-cards-data": ("gnome-games", "2024-10"),
}

# Nomes que o pacman rejeita antes mesmo de olhar no banco. `pacman -Si steam:i386`
# dá "package not found" porque o `:` não faz parte de um nome de pacote: é a
# sintaxe de repositório (`pacman -S repo/pkg`), não de arquitetura. Sem esta
# lista o sufixo seria descartado, `steam` seria encontrado em multilib e o
# app pareceria saudável — mas `pacman -S steam:i386` continua falhando.
KNOWN_INVALID = {
    "steam:i386": "o pacote não existe; o Steam vive em multilib, sem sufixo de arquitetura",
}

# O mesmo formato que `PACMAN_PACKAGE_RE` do servidor, replicado aqui para o
# módulo não depender do web_server (que o importa). Manter os dois em acordo:
# um nome rejeito aqui também é rejeitado pelo instalador.
VALID_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9@._+-]*$")


def valid_name(package):
    """Diz se o pacman aceitaria esse nome como pacote."""
    return bool(package) and bool(VALID_NAME.fullmatch(package))

_lock = threading.Lock()
_cache = None          # dict nome -> info
_cache_stamp = None    # (mtime, size) de cada .db, para invalidar


def _read_db(path):
    """Lê um .db do pacman e devolve {nome: {version, csize, isize, desc, url}}."""
    out = {}
    with tarfile.open(path, "r:gz") as tf:
        for member in tf:
            if not member.name.endswith("/desc"):
                continue
            handle = tf.extractfile(member)
            if handle is None:
                continue
            data = {}
            for block in handle.read().decode("utf-8", "replace").split("\n\n"):
                if "\n" not in block:
                    continue
                # A chave vem como "%NAME%" e o valor na linha seguinte.
                # Cortar os dois "%" é obrigatório: cortar só o inicial deixa
                # "NAME%", que nunca casa com nenhuma busca.
                key = block.partition("\n")[0].strip().strip("%")
                if key:
                    data[key] = block.partition("\n")[2].strip()
            name = data.get("NAME")
            if not name:
                continue
            out[name] = {
                "version": data.get("VERSION") or "",
                "csize": _int(data.get("CSIZE")),
                "isize": _int(data.get("ISIZE")),
                "desc": data.get("DESC") or "",
                "url": data.get("URL") or "",
            }
    return out


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _db_stamp():
    """Identidade dos .db atuais, para invalidar o cache quando sincronizar."""
    stamp = []
    for repo in REPO_FILES:
        path = os.path.join(SYNC_DIR, repo + ".db")
        try:
            st = os.stat(path)
            stamp.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            stamp.append((path, None, None))
    return tuple(stamp)


def _load():
    """Lê todos os .db. Repositório anterior sobrescreve o seguinte na ordem
    inversa, para que o `core` tenha a última palavra."""
    merged = {}
    for repo in reversed(REPO_FILES):
        path = os.path.join(SYNC_DIR, repo + ".db")
        if not os.path.exists(path):
            continue
        try:
            data = _read_db(path)
        except (OSError, tarfile.TarError):
            continue
        for name, info in data.items():
            info = dict(info)
            info["repo"] = repo
            merged[name] = info
    return merged


def _ensure():
    global _cache, _cache_stamp
    stamp = _db_stamp()
    if _cache is not None and _cache_stamp == stamp:
        return _cache
    with _lock:
        if _cache is not None and _cache_stamp == stamp:
            return _cache
        _cache = _load()
        _cache_stamp = stamp
        return _cache


def available() -> bool:
    """Diz se há banco de sincronização para consultar."""
    return any(os.path.exists(os.path.join(SYNC_DIR, r + ".db"))
               for r in REPO_FILES)


def lookup(package):
    """Metadados de um pacote dos repositórios oficiais.

    Devolve `None` quando o pacote não existe — aí o app está quebrado, e o
    chamador decide como avisar. Quando o nome foi renomeado pelo Arch, devolve
    o do sucessor com `renamed_from` preenchido, para a interface poder dizer
    que o nome mudou em vez de mostrar um número qualquer.
    """
    if not package:
        return None
    # O nome tem que ser um nome de pacote antes de qualquer coisa: `steam:i386`
    # não é, e procurar só pelo `steam` acharia um pacote saudável e esconderia
    # que a instalação vai falhar.
    if package in KNOWN_INVALID or not valid_name(package):
        return None
    db = _ensure()
    info = db.get(package)
    if info:
        return dict(info, renamed_from=None)
    sucessor = RENAMED.get(package)
    if sucessor:
        info = db.get(sucessor[0])
        if info:
            return dict(info, renamed_from=package, renamed_to=sucessor[0])
    return None


def renamed_hint(package):
    """Texto curto explicando um nome inválido ou renomeado, ou None."""
    if not package:
        return None
    if package in KNOWN_INVALID:
        return KNOWN_INVALID[package]
    if not valid_name(package):
        return ("“%s” não é um nome de pacote válido para o pacman "
                "(o “:” é sintaxe de repositório, não de arquitetura)" % package)
    sucessor = RENAMED.get(package)
    if sucessor:
        return "renomeado para %s no Arch (%s)" % (sucessor[0], sucessor[1])
    return None


def search(term, limit=40):
    """Busca pacotes por nome, para o preenchimento automático da curadoria.

    A pontuação favorece o que começa com o termo e o que tem nome curto, para
    que `firefox` apareça antes de `firefox-i18n-af`.
    """
    term = (term or "").strip().lower()
    if len(term) < 2:
        return []
    db = _ensure()
    scored = []
    for name, info in db.items():
        pos = name.lower().find(term)
        if pos < 0:
            continue
        # início do nome > substring; nome curto > nome longo
        score = (0 if pos == 0 else 1, pos, len(name))
        scored.append((score, name, info))
    scored.sort(key=lambda item: item[0])
    out = []
    for _, name, info in scored[:limit]:
        out.append({
            "name": name,
            "version": info["version"],
            "repo": info["repo"],
            "csize": info["csize"],
            "isize": info["isize"],
            "desc": info["desc"],
            "url": info["url"],
        })
    return out


def human_size(num_bytes):
    """Bytes -> texto curto, no estilo que o resto da loja usa."""
    if not num_bytes or num_bytes < 0:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(num_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return "%d %s" % (int(value), unit)
            return ("%.1f" % value).rstrip("0").rstrip(".") + " " + unit
        value /= 1024
    return ""
