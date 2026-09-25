#!/usr/bin/env python3
"""Gera a versão de distribuição sob autorização do polkit.

A curadoria chama este script via `pkexec`, então o usuário vê exatamente a
mesma caixa de autenticação que aparece ao instalar ou remover um programa.

Depois de autenticado, o script **não fica como root**: ele volta para a conta
que autorizou (`PKEXEC_UID`) e gera o pacote com as permissões dela. Isso é
deliberado — a chave GPG, o clone do GitHub e as credenciais estão no HOME do
usuário, e o root usá-los direto causa conflito de lock no agente GPG.

Uso (chamado pela própria curadoria):
    pkexec /usr/bin/python3 .../src/nlinux/build_helper.py
"""
import json
import os
import pwd
import shutil
import subprocess
import sys

HELPER = os.path.abspath(__file__)


def run_as_user(uid: int, home: str) -> int:
    """Refaz o build dentro da conta do usuário que autorizou."""
    runuser = shutil.which("runuser")
    if not runuser:
        print("ERRO: runuser não encontrado; não é possível voltar para o "
              "usuário após a autorização.", flush=True)
        return 1
    env = {
        "HOME": home,
        "GNUPGHOME": os.path.join(home, ".gnupg"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "DISPLAY": os.environ.get("DISPLAY", ""),
    }
    proc = subprocess.run(
        [runuser, "-u", pwd.getpwuid(uid).pw_name, "--",
         "env", f"HOME={env['HOME']}", f"GNUPGHOME={env['GNUPGHOME']}",
         f"DISPLAY={env['DISPLAY']}", f"LANG={env['LANG']}",
         "/usr/bin/python3", HELPER, "--as-user"],
        env=env, text=True,
    )
    return proc.returncode


def build_as_user() -> int:
    """Gera o pacote com as permissões do usuário (sem elevação)."""
    src_root = os.path.dirname(os.path.dirname(HELPER))
    sys.path.insert(0, src_root)
    from nlinux import web_server as ws

    print(f"Gerando em {ws.DIST_DIR}", flush=True)
    result, _ = ws.admin_build(progress=lambda m: print(f"… {m}", flush=True))
    print("@@RESULT@@" + json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if isinstance(result, dict) and result.get("error") else 0


def main() -> int:
    if "--as-user" in sys.argv:
        return build_as_user()

    uid = os.environ.get("PKEXEC_UID")
    if not uid:
        print("ERRO: PKEXEC_UID ausente; o build precisa saber para qual "
              "usuário gerar.", flush=True)
        return 1
    try:
        info = pwd.getpwuid(int(uid))
    except KeyError:
        print(f"ERRO: usuário {uid} não encontrado.", flush=True)
        return 1

    print(f"Autorizado. Gerando como {info.pw_name} ({info.pw_dir})", flush=True)
    return run_as_user(info.pw_uid, info.pw_dir)


if __name__ == "__main__":
    sys.exit(main())
