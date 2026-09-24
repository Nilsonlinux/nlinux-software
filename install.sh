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
# paru/yay (AUR) e curl são opcionais; apenas avisa se não estiverem aqui
for opt in paru yay curl; do
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
# --- ícone + atalho no menu de aplicativos --------------------------
cat > "$DEST/icon.svg" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
<rect width="128" height="128" rx="24" fill="#0077cc"/>
<text x="50%" y="54%" font-family="DejaVu Sans, sans-serif"
      font-size="72" font-weight="bold" fill="#ffffff"
      text-anchor="middle" dominant-baseline="middle">N</text>
<circle cx="92" cy="96" r="14" fill="#22cc88"/>
</svg>
SVG
cat > /usr/share/applications/nlinux-software.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=NLinux Software
GenericName=Loja de aplicativos
Comment=Loja de aplicativos do NLinux (distribuição)
Exec=/usr/local/bin/nlinux-software
Icon=/opt/nlinux-software/icon.svg
Terminal=false
Categories=Network;Utility;
StartupNotify=false
EOF
echo "Instalado: NLinux Software v91 (/usr/local/bin/nlinux-software)"
