import subprocess

class SystemState:
    
    def __init__(self) -> None:
        # Get current architecture of system.
        # Arch Linux uses the same names as the kernel/pacman (eg. x86_64, i686, aarch64).
        self.arch = self._get_architecture()

        # Get current version / codename of the distribution in use, from /etc/os-release.
        os_release = self._parse_os_release()
        self.distro = os_release.get('ID', 'arch').lower().strip('\n')
        self.name = os_release.get('NAME', 'Arch Linux')
        self.os_version = os_release.get('BUILD_ID', os_release.get('VERSION_ID', 'rolling')).strip('\n')
        self.codename = os_release.get('VERSION_CODENAME', 'rolling').strip('\n')

    def _parse_os_release(self) -> dict:
        values = {}
        try:
            with open('/etc/os-release', 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    key, _, value = line.partition('=')
                    values[key] = value.strip().strip('"\'')
        except Exception:
            pass
        return values

    def _get_architecture(self) -> str:
        # Prefer the architecture configured for pacman, fall back to the kernel's.
        try:
            arch = subprocess.run(['pacman-conf', 'Architecture'], stdout=subprocess.PIPE).stdout.decode('utf-8').strip()
            if arch:
                return arch
        except Exception:
            pass
        try:
            arch = subprocess.run(['uname', '-m'], stdout=subprocess.PIPE).stdout.decode('utf-8').strip('\n')
            return {
                'armv7l': 'armv7h',
                'aarch64': 'aarch64',
                'i686': 'i686',
                'x86_64': 'x86_64',
            }.get(arch, arch)
        except Exception:
            return 'x86_64'