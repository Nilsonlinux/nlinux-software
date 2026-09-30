#!/bin/sh
set -e
if [ "$(id -u)" -ne 0 ]; then
  echo "Executando com sudo..."
  exec sudo "$0" "$@"
fi
require() { pacman -Q "$1" >/dev/null 2>&1; }
# --- dependências de execução ----------------------------------------
MISSING=""
for p in python python-gobject gtk3 webkit2gtk-4.1 polkit gnupg git; do
  require "$p" || MISSING="$MISSING $p"
done
if [ -n "$MISSING" ]; then
  echo "Instalando dependências:${MISSING}"
  pacman -S --noconfirm --needed $MISSING
fi
# paru ou yay (AUR) e curl são opcionais; avisa só o que faltar
if ! command -v paru >/dev/null 2>&1 && ! command -v yay >/dev/null 2>&1; then
  echo "Aviso: nenhum auxiliar AUR encontrado (paru ou yay)."
fi
command -v curl >/dev/null 2>&1 || echo "Aviso: 'curl' não encontrado."
# --- autentica o pacote com GPG (assinatura da curadoria) ------------
SRC="$(cd "$(dirname "$0")" && pwd)"
if ! command -v gpg >/dev/null 2>&1; then
  echo "ERRO: gpg ausente; não é possível validar a assinatura." >&2
  exit 1
fi
TARBALL="$(ls "$SRC"/nlinux-software-v*.tar.gz "$SRC"/../nlinux-software-v*.tar.gz 2>/dev/null | head -n1)"
if [ -n "$TARBALL" ] && [ -f "$TARBALL.asc" ]; then
  gpg --batch --import "$SRC/nlinux-software_pub.asc" >/dev/null 2>&1
  if ! gpg --batch --verify "$TARBALL.asc" "$TARBALL" >/dev/null 2>&1; then
    echo "ERRO: assinatura GPG do pacote INVÁLIDA. Instalação abortada." >&2
    echo "O arquivo (ou o repositório) pode ter sido adulterado. Baixe de novo." >&2
    exit 1
  fi
  echo "Verificação GPG: OK (pacote autêntico da curadoria)."
else
  echo "Aviso: assinatura (.asc) não encontrada junto do pacote; sem validação."
fi
# --- instala a loja ----------------------------------------------------
DEST="/opt/nlinux-software"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$SRC/src" "$DEST/"
cp "$SRC/nlinux-software" "$DEST/"
chmod +x "$DEST/nlinux-software"
cat > /usr/local/bin/nlinux-software <<'EOF'
#!/bin/sh
exec /opt/nlinux-software/nlinux-software "$@"
EOF
chmod +x /usr/local/bin/nlinux-software
# --- dados gravaveis, fora do /opt ---------------------------------
# O catalogo e a midia mudam toda vez que a loja sincroniza com o
# GitHub. Em /opt isso pediria root, entao vao para o HOME do
# usuario que instalou, com a propriedade dele.
STORE_USER="${SUDO_USER:-root}"
STORE_HOME="$(getent passwd "$STORE_USER" | cut -d: -f6)"
if [ -n "$STORE_HOME" ] && [ "$STORE_USER" != "root" ]; then
  STORE_DATA="${XDG_DATA_HOME:-$STORE_HOME/.local/share}"
  APP_DATA="$STORE_DATA/nlinux"
  DATA_DIR="$APP_DATA/store"
  mkdir -p "$DATA_DIR"
  if [ ! -d "$DATA_DIR/apps" ]; then
    cp -a "$DEST/src/apps" "$DATA_DIR/apps"
  fi
  # o chown tem de pegtar a arvore inteira: um 'mkdir -p' como root
  # deixa os diretorios intermediarios do root, e sem dono o usuario
  # nao consegue criar mais nada dentro deles
  chown -R "$STORE_USER" "$APP_DATA"
  echo "Catalogo e midia: $DATA_DIR/apps"
fi
# --- ícone + atalho no menu de aplicativos --------------------------
mkdir -p /usr/share/pixmaps
cp "$DEST/src/apps/nlinux-logo.png" /usr/share/pixmaps/nlinux-software.png
rm -f /usr/share/icons/hicolor/scalable/apps/nlinux-software.svg
rm -f /usr/share/applications/nlinux-software.desktop
rm -f /usr/share/applications/nlinuxsoftware.desktop
cat > /usr/share/applications/nlinuxstore.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=NLinux Software
GenericName=Loja de aplicativos
Comment=Loja de aplicativos do NLinux (distribuição)
Exec=/usr/local/bin/nlinux-software
Icon=nlinux-software
Terminal=false
Categories=Network;Utility;
StartupNotify=true
StartupWMClass=nlinuxstore
X-GNOME-UsesNotifications=false
EOF
(command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database /usr/share/applications) || true
echo "Instalado: NLinux Software v198 (/usr/local/bin/nlinux-software)"
