"""Existência de pacotes na AUR, com cache em disco.

O `pacman_db` responde sobre o Arch a partir do banco local, e é por isso que
funciona sem rede. A AUR não tem banco local: a única fonte pública é a RPC v5.
Esta camada é a ponte, com três regras que vêm da lição de não inventar dado:

**Nunca afirmar o que não se sabe.** A função devolve três conjuntos, não um
booleano: os que existem, os que **não** existem e os que não deu para
verificar. Um pacote não conferido não vira "quebrado" — vira `desconhecido`, e
quem chama decide o que dizer. Confundir "não achei" com "não existe" foi
exatamente o que|Apagava apps bons do catálogo.

**Ausência só é afirmada com dado fresco.** O cache vale por `TTL`. Se a rede
falhou, só os pacotes cujo cache ainda está dentro do TTL contam como
verificados; o resto fica desconhecido. Um "não existe" de cache velho é
hipótese, não fato — e uma resposta errada aqui custa um app removido do
catálogo.

**Uma consulta, não uma por app.** A RPC aceita vários `arg[]` de uma vez. Fazer
uma chamada por pacote transformaria a abertura da loja em dezenas de
requisições, que é como se derruba o IP no archlinux.org.

O cache fica em `~/.local/share/nlinux/aur-cache.json`, fora de `src/apps`, e
portanto fora do pacote gerado.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from nlinux import resources

AUR_RPC = "https://aur.archlinux.org/rpc/v5/info"

# Six horas. A AUR muda devagar, e a consulta serve para pegar pacote removido,
# não para saber a versão da hora.
TTL = 6 * 3600

# Uma consulta só, mesmo com muitas apps AUR no catálogo.
LOTE = 100

# Rede lenta não pode travar a abertura da loja.
TIMEOUT = 6

_lock = threading.Lock()


def cache_path() -> str:
    return os.path.join(resources.data_home(), "aur-cache.json")


def _ler_cache():
    try:
        with open(cache_path(), encoding="utf-8") as fh:
            dados = json.load(fh)
        if not isinstance(dados, dict) or not isinstance(dados.get("pacotes"), dict):
            return {}
        return dados["pacotes"]
    except (OSError, ValueError):
        return {}


def _gravar_cache(pacotes):
    """Grava o cache. Falha de escrita não pode derrubar a consulta."""
    dados = {"quando": int(time.time()), "pacotes": pacotes}
    caminho = cache_path()
    try:
        os.makedirs(os.path.dirname(caminho), exist_ok=True)
        tmp = caminho + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(dados, fh, ensure_ascii=False)
        os.replace(tmp, caminho)
    except OSError:
        pass


def _consultar(nomes):
    """Uma ida à AUR. Devolve {nome: dict} só com o que veio."""
    achados = {}
    for i in range(0, len(nomes), LOTE):
        parte = nomes[i:i + LOTE]
        url = AUR_RPC + "?" + "&".join(
            "arg[]=" + urllib.parse.quote(n, safe="") for n in parte)
        req = urllib.request.Request(
            url, headers={"User-Agent": "nlinux-software-admin/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                dados = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            raise
        for p in dados.get("results") or []:
            nome = p.get("Name")
            if nome:
                achados[nome] = {
                    "versao": p.get("Version") or "",
                    "mantenedor": p.get("Maintainer") or "",
                    "votos": p.get("NumVotes") or 0,
                    "ausente": False,
                }
    return achados


def lookup_many(pacotes):
    """Confere vários pacotes de uma vez.

    Devolve `(existe, ausente, desconhecidos, erro)`, onde:

    - `existe`       nomes confirmados na AUR agora ou por cache fresco
    - `ausente`      nomes confirmados como **inexistentes** na AUR
    - `desconhecidos` nomes que não deu para decidir (rede fora, sem cache)
    - `erro`         texto explicando a falha, ou None se deu para decidir tudo

    Quem chama decide o que fazer com `desconhecidos`: acusar quebra por um
    pacote não conferido é o erro que esta função existe para evitar.
    """
    nomes = sorted({p for p in pacotes if p})
    if not nomes:
        return set(), set(), set(), None

    with _lock:
        cache = _ler_cache()
        agora = time.time()

        frescos = {n for n in nomes
                   if n in cache
                   and agora - (cache[n].get("quando") or 0) < TTL}
        a_consultar = [n for n in nomes if n not in frescos]

        erro = None
        if a_consultar:
            try:
                achados = _consultar(a_consultar)
                # Ausência confirmada vira entrada no cache, senão o pacote
                # removido seria re-consultado a cada abertura.
                for n in a_consultar:
                    achados.setdefault(n, {"ausente": True, "quando": agora})
                    achados[n]["quando"] = agora
                cache.update(achados)
                _gravar_cache(cache)
            except Exception as exc:                      # noqa: BLE001
                erro = f"não deu para consultar a AUR: {exc}"

        existe, ausente, desconhecidos = set(), set(), set()
        for n in nomes:
            entrada = cache.get(n)
            if entrada is None:
                desconhecidos.add(n)
                continue
            idade = agora - (entrada.get("quando") or 0)
            if idade >= TTL and erro:
                # Dado velho e sem rede para confirmar: hipótese, não fato.
                desconhecidos.add(n)
            elif entrada.get("ausente"):
                ausente.add(n)
            else:
                existe.add(n)

        return existe, ausente, desconhecidos, erro


def existe(package):
    """Atalho para um pacote só. Devolve True, False ou None (desconhecido).

    Nome vazio devolve `False`, e não `None`: `None` é "não deu para conferir",
    e quem chama costuma fazer `if not existe(pkg)` para acusar quebra — o que
    transformaria um campo em branco num app condenado. Nome vazio não é
    pacote, e um app sem pacote não instala.
    """
    if not package:
        return False
    achados, ausentes, _, _ = lookup_many([package])
    if package in achados:
        return True
    if package in ausentes:
        return False
    return None