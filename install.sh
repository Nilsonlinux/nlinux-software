#!/bin/sh
set -e
if [ "$(id -u)" -ne 0 ]; then
  echo "Executando com sudo..."
  exec sudo "$0" "$@"
fi
require() { pacman -Q "$1" >/dev/null 2>&1; }
# --- dependências de execução ----------------------------------------
MISSING=""
for p in python python-gobject gtk3 webkit2gtk-4.1 polkit; do
  require "$p" || MISSING="$MISSING $p"
done
if [ -n "$MISSING" ]; then
  echo "Instalando dependências:${MISSING}"
  pacman -S --noconfirm --needed $MISSING
fi
# paru (AUR) e curl são opcionais; apenas avisa se não estiverem aqui
for opt in paru curl; do
  command -v "$opt" >/dev/null 2>&1 || echo "Aviso: '$opt' não encontrado."
done
# --- instala a loja ----------------------------------------------------
SRC="$(cd "$(dirname "$0")" && pwd)"
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
echo "Instalado: NLinux Software v90 (/usr/local/bin/nlinux-software)"
