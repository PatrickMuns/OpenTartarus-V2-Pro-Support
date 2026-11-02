Name:           opentartarus
Version:        0.3.0
Release:        1%{?dist}
Summary:        Razer Tartarus V2 Pro manager for Linux

License:        MIT
URL:            https://github.com/PatrickMuns/OpenTartarus-V2-Pro-Support
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
Requires:       python3 >= 3.8
Requires:       python3-pyqt6
Requires:       python3-evdev
Recommends:     openrazer-meta

%description
OpenTartarus is a Synapse-like tray application for the Razer
Tartarus V2 Pro keypad. It provides key remapping, macro support,
RGB lighting control via OpenRazer, analog stick modes, and
profile management. Runs as a system tray app.

Supports only the Tartarus V2 Pro (USB ID 1532:02eb).

%prep
%setup -q

%build
# Nothing to build - pure Python

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_datadir}/opentartarus
mkdir -p %{buildroot}%{_datadir}/applications
mkdir -p %{buildroot}/usr/lib/udev/rules.d

install -m 755 opentartarus.py %{buildroot}%{_datadir}/opentartarus/opentartarus.py

cat > %{buildroot}%{_bindir}/opentartarus << 'LAUNCHER'
#!/bin/bash
exec python3 %{_datadir}/opentartarus/opentartarus.py "$@"
LAUNCHER
chmod 755 %{buildroot}%{_bindir}/opentartarus

cat > %{buildroot}%{_datadir}/applications/opentartarus.desktop << 'DESKTOP'
[Desktop Entry]
Type=Application
Name=OpenTartarus V2 Pro
Comment=Razer Tartarus V2 Pro manager for Linux
Exec=/usr/bin/opentartarus
Icon=input-gaming
Categories=Settings;HardwareSettings;
Keywords=razer;tartarus;keypad;gaming;remap;v2pro;
Terminal=false
DESKTOP

cat > %{buildroot}/usr/lib/udev/rules.d/99-opentartarus.rules << 'UDEV'
# OpenTartarus - Razer Tartarus V2 Pro
SUBSYSTEM=="input", ATTRS{idVendor}=="1532", ATTRS{idProduct}=="02eb", MODE="0660", GROUP="input"
UDEV

%files
%{_bindir}/opentartarus
%{_datadir}/opentartarus/opentartarus.py
%{_datadir}/applications/opentartarus.desktop
/usr/lib/udev/rules.d/99-opentartarus.rules

%post
udevadm control --reload-rules >/dev/null 2>&1 || :
udevadm trigger >/dev/null 2>&1 || :
echo ""
echo "OpenTartarus V2 Pro installed."
echo ""
echo "IMPORTANT next steps:"
echo "  1. Add your user to the 'input' group:"
echo "       sudo usermod -aG input \$USER"
echo "  2. Log out and back in for group changes to take effect."
echo "  3. Launch OpenTartarus from your application menu, or run:"
echo "       opentartarus"
echo ""
echo "To launch automatically on login, open OpenTartarus and enable"
echo "'Launch on login' from the Settings tab."
echo ""

%postun
udevadm control --reload-rules >/dev/null 2>&1 || :

%changelog
* Sun Nov 02 2025 PatrickMuns <https://github.com/PatrickMuns> - 0.3.0-1
- Restrict support to Tartarus V2 Pro (1532:02eb)
- Full analog stick modes and key-binding support
- Updated device detection and status reporting
* Fri Jul 04 2025 Moparmaniac412 <https://github.com/Moparmaniac412> - 0.2.0-1
- Add Tartarus V2 support
- Dynamic device detection by USB ID
- Redesigned GUI
* Sun Jun 01 2025 Moparmaniac412 <https://github.com/Moparmaniac412> - 0.1.0-1
- Initial release
