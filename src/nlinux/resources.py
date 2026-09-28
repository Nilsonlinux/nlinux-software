import getpass
import os
import shutil
import sys

# Resolves packaged resources (apps index, assets, translations) relative to
# the source tree, avoiding a dependency on the deprecated pkg_resources module.
SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# Snapshot do catálogo que vem junto do pacote. Instalado em /opt ele é do root,
# por isso nunca deve ser usado para escrever.
BUNDLED_APPS_DIR = os.path.join(SRC_ROOT, 'apps')

# Subpasta do app. Não pode ser 'nlinux-software': esse nome pertence ao
# WebKitGTK, que cria ~/.local/share/nlinux-software para os dados do site.
APP_DIRNAME = 'nlinux'


def resource_path(resource: str) -> str:
    return os.path.join(SRC_ROOT, resource)


def resource_exists(resource: str) -> bool:
    return os.path.exists(resource_path(resource))


def _running_from_home() -> bool:
    """True quando o código roda no projeto de desenvolvimento (dentro do HOME)."""
    home = os.path.realpath(os.path.expanduser('~'))
    root = os.path.realpath(SRC_ROOT)
    return root == home or root.startswith(home + os.sep)


def data_home() -> str:
    """Base dos dados graváveis, numa pasta oculta (XDG).

    `NLINUX_DATA_DIR` troca a base inteira (útil para testes).
    """
    base = os.environ.get('NLINUX_DATA_DIR')
    if not base:
        base = os.environ.get('XDG_DATA_HOME') \
            or os.path.join(os.path.expanduser('~'), '.local', 'share')
    return os.path.join(base, APP_DIRNAME)


def data_dir(role: str) -> str:
    """Pasta de dados graváveis do papel `role` (admin ou store)."""
    return os.path.join(data_home(), role)


def writable(path: str) -> str:
    """Devolve `path` garantindo que dá para escrever nela.

    Sem isso, uma pasta que sobrou do root vira um `Permission denied` no meio
    do build, sem dizer de onde veio. A pasta pode ainda não existir (o build
    cria), então confere o ancestral existente mais próximo.
    """
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if not os.access(probe, os.W_OK):
        user = os.environ.get('SUDO_USER') or getpass.getuser()
        raise PermissionError(
            f"sem permissão de escrita em {probe} "
            f"(criado por outra conta?) — rode: "
            f"sudo chown -R {user} {os.path.dirname(probe) or probe}")
    return path


def seed_apps_dir(role: str) -> str:
    """Copia o catálogo do pacote para a pasta gravável, uma única vez.

    A cópia vai para um nome temporário e só então é renomeada, para nunca
    deixar a pasta pela metade se o processo for interrompido.
    """
    target = os.path.join(data_dir(role), 'apps')
    if os.path.isdir(target) or not os.path.isdir(BUNDLED_APPS_DIR):
        return writable(target)
    writable(os.path.dirname(target))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + '.seed'
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        shutil.copytree(BUNDLED_APPS_DIR, tmp)
        try:
            os.replace(tmp, target)
        except OSError:
            # outra instância semeou antes: o resultado dela vale
            if os.path.isdir(target):
                return target
            raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return target


def apps_dir(role: str) -> str:
    """Pasta do catálogo e da mídia, sempre gravável pelo usuário.

    No projeto de desenvolvimento continua em `<projeto>/src/apps`, para
    editar no lugar. Instalado em /opt, semeia e usa
    `~/.local/share/nlinux/<role>/apps`: assim salvar um programa,
    trocar um ícone ou sincronizar o catálogo do GitHub não pede root.
    """
    if _running_from_home():
        return BUNDLED_APPS_DIR
    try:
        return seed_apps_dir(role)
    except OSError as exc:
        # seguir so com leitura silenciosamente esconde o problema: avisa e
        # deixa claro que nada pode ser gravado nesta sessao
        print(f"[nlinux] {exc}", file=sys.stderr, flush=True)
        print(f"[nlinux] usando o catalogo somente-leitura de "
              f"{BUNDLED_APPS_DIR} (rode a curadoria com um usuario com "
              f"permissao de escrita)", file=sys.stderr, flush=True)
        return BUNDLED_APPS_DIR
