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
# --- ícone + atalho no menu de aplicativos --------------------------
cat > "$DEST/icon.svg" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#1f6feb"/>
      <stop offset="1" stop-color="#0d3b8f"/>
    </linearGradient>
  </defs>
  <rect x="4" y="4" width="120" height="120" rx="26" fill="url(#g)"/>
  <rect x="4" y="4" width="120" height="120" rx="26" fill="none" stroke="#12233f" stroke-width="4"/>
  <path d="M32 86 L58 40 L74 72 L84 54 L98 86" stroke="#ffffff" stroke-width="10" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="58" cy="94" r="9" fill="#3fb950"/>
  <g transform="translate(90,90)">
    <path d="M-18 -8 L-18 18 Q-18 24 -12 24 L12 24 Q18 24 18 18 L18 -8 Z" fill="#3fb950" stroke="#12233f" stroke-width="3" stroke-linejoin="round"/>
    <path d="M-9 -8 L-9 -14 Q-9 -20 0 -20 Q9 -20 9 -14 L9 -8" fill="none" stroke="#12233f" stroke-width="3" stroke-linecap="round"/>
  </g>
</svg>
SVG
mkdir -p /usr/share/icons/hicolor/scalable/apps
cp "$DEST/icon.svg" /usr/share/icons/hicolor/scalable/apps/nlinux-software.svg
(command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -f -t /usr/share/icons/hicolor) || true
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
echo "Instalado: NLinux Software v105 (/usr/local/bin/nlinux-software)"
