#!/usr/bin/env python3
"""Top bar icon for LAN Remote Desktop: turns the server on and off.

Always on the bar and off at login: gray icon = off, colored = on air (the port is listening).
Menu: status, On, LAN address, Show QR code, Open in the browser, View log. A middle click on the icon toggles it.
Starting it opens the QR code of the LAN address for the phone.
Only errors notify. There is no "Quit": unchecking "On" is how it turns off.

Runs on the system python3 with PyGObject + AyatanaAppIndicator3 from apt. GTK is imported
only when the icon starts, so the tests run anywhere.

    ./tray_icon.py             show the icon (a 2nd run exits: one icon per app)
    ./tray_icon.py --install   start the icon at login + app menu shortcut
    ./tray_icon.py --remove    undo --install
"""

import fcntl
import os
import signal
import socket
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------ this app
APP = {
    "slug": "devkit-lan-remote-desktop",
    "name": "LAN Remote Desktop",
    "cmd": ["./run.sh"],
    "env": {},
    "port": 8443,
    "lan": "https",  # scheme the phone opens over the LAN ("http"/"https"), or None
    "open": "https://localhost:8443",  # address for "Open in the browser", or None
    "lang_env": "LRD_LANG",
    "color": "#8e44ad",
    # 22x22 SVG body; {c} = main color, {d} = detail color
    "drawing": (
        '<rect x="1.5" y="3" width="19" height="12.5" rx="1.8" fill="{c}"/>'
        '<rect x="3.5" y="5" width="15" height="8.5" rx="0.8" fill="{d}"/>'
        '<rect x="9.5" y="15.5" width="3" height="2.5" fill="{c}"/>'
        '<rect x="6" y="18" width="10" height="2" rx="1" fill="{c}"/>'
    ),
}

CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), APP["slug"])
ICONS = os.path.join(CACHE, "icons")
LOG = os.path.join(CACHE, "server.log")
TICK_MS = 2000

# ------------------------------------------------------------------ i18n
# English is the source text and the key; PT_BR holds the Brazilian Portuguese line.
# tests/test_tray_icon.py fails on a missing line, on different {placeholders} or on an unused line.
PT_BR = {
    "{name}: on air (port {port})": "{name}: no ar (porta {port})",
    "{name}: starting…": "{name}: ligando…",
    "{name}: off": "{name}: desligado",
    "On": "Ligado",
    "On the phone: {url}": "No celular: {url}",
    "Address: {url}": "Endereço: {url}",
    "Show QR code": "Mostrar QR code",
    "Open in the browser": "Abrir no navegador",
    "View log": "Ver log",
    "{name} - QR code": "{name} - QR code",
    "Self-signed certificate: on the phone, accept the security warning.":
        "Certificado próprio: no celular, aceite o aviso de segurança.",
    "Install python3-segno to show the QR code.": "Instale o python3-segno pra mostrar o QR code.",
    "Close": "Fechar",
    "{name} did not start": "{name} não subiu",
    "exited with code {code}; see the log": "saiu com código {code}; veja o log",
}


def detect_language(env=None) -> str:
    env = os.environ if env is None else env
    for name in (APP["lang_env"], "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = env.get(name, "")
        if value:
            return "pt-BR" if value.split(":")[0].lower().startswith("pt") else "en"
    return "en"


LANG = detect_language()


def tr(text: str, lang: str | None = None, **values) -> str:
    lang = LANG if lang is None else lang
    line = PT_BR.get(text, text) if lang == "pt-BR" else text
    return line.format(**values) if values else line


# ------------------------------------------------------------------ pure helpers
def icon_svg(state: str) -> str:
    c, d = (APP["color"], "#ffffff") if state == "on" else ("#9a9a9a", "#3a3a3a")
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" '
            f'viewBox="0 0 22 22">{APP["drawing"].format(c=c, d=d)}</svg>')


def write_icons(folder=None):
    folder = folder or ICONS
    os.makedirs(folder, exist_ok=True)
    for state in ("on", "off"):
        with open(os.path.join(folder, f"{APP['slug']}-{state}.svg"), "w") as f:
            f.write(icon_svg(state))


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent: it only picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def lan_url(ip=None) -> str:
    return f"{APP['lan']}://{ip or lan_ip()}:{APP['port']}"


def parse_ss(output: str) -> list[int]:
    """PIDs in the output of `ss -ltnpH` (only the user's own processes show a pid)."""
    pids = []
    for chunk in output.split("pid=")[1:]:
        num = chunk.split(",")[0]
        if num.isdigit():
            pids.append(int(num))
    return pids


def pids_on_port(port: int) -> list[int]:
    try:
        out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_ss(out)


def belongs(pid: int, folder: str = HERE, proc: str = "/proc") -> bool:
    """Is that process this app? Its cwd or its command line points into the folder."""
    try:
        if os.readlink(f"{proc}/{pid}/cwd").startswith(folder):
            return True
        with open(f"{proc}/{pid}/cmdline", "rb") as f:
            return folder.encode() in f.read()
    except OSError:
        return False


def status_text(on_air: bool, starting: bool) -> str:
    if on_air:
        return tr("{name}: on air (port {port})", name=APP["name"], port=APP["port"])
    if starting:
        return tr("{name}: starting…", name=APP["name"])
    return tr("{name}: off", name=APP["name"])


def failure_note(code, last_log_line: str) -> str:
    return last_log_line or tr("exited with code {code}; see the log", code=code)


def last_log_line(path=None) -> str:
    try:
        with open(path or LOG, errors="replace") as f:
            lines = [line.strip() for line in f if line.strip()]
        return lines[-1][-200:] if lines else ""
    except OSError:
        return ""


# ------------------------------------------------------------------ install
def desktop_paths(env=None):
    env = os.environ if env is None else env
    home = env.get("HOME") or os.path.expanduser("~")
    data = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    config = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    name = f"{APP['slug']}-tray.desktop"
    return os.path.join(data, "applications", name), os.path.join(config, "autostart", name)


def desktop_entry() -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP['name']}\n"
        f"Comment=Top bar icon that turns {APP['name']} on and off\n"
        f"Exec=/usr/bin/python3 {os.path.join(HERE, 'tray_icon.py')}\n"
        f"Icon={os.path.join(ICONS, APP['slug'] + '-on.svg')}\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "StartupNotify=false\n"
        "X-GNOME-Autostart-Delay=3\n"
    )


def install(env=None):
    write_icons()
    entry = desktop_entry()
    for path in desktop_paths(env):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(entry)
        print(f"written: {path}")


def remove(env=None):
    for path in desktop_paths(env):
        if os.path.exists(path):
            os.remove(path)
            print(f"removed: {path}")


def single_instance():
    """Holds a lock for the whole run; None when another icon of this app is already up."""
    os.makedirs(CACHE, exist_ok=True)
    handle = open(os.path.join(CACHE, "tray.lock"), "w")  # kept open on purpose: the lock lives with it
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def notify(title, body=""):
    try:
        subprocess.Popen(["notify-send", "-a", APP["name"], title, body])
    except OSError:
        pass


# ------------------------------------------------------------------ the icon (GTK)
def run_icon():
    import warnings

    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
    from gi.repository import GLib, Gtk

    warnings.filterwarnings("ignore", category=DeprecationWarning)

    class QrWindow(Gtk.Window):
        """QR code of the LAN address, to scan with the phone."""

        def __init__(self, url):
            super().__init__(title=tr("{name} - QR code", name=APP["name"]))
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            box.set_border_width(16)
            try:
                import segno  # only when needed; python3-segno from apt
                png = os.path.join(CACHE, "qr.png")
                segno.make(url, error="m").save(png, kind="png", scale=8, border=3)
                box.pack_start(Gtk.Image.new_from_file(png), False, False, 0)
            except ImportError:
                box.pack_start(Gtk.Label(label=tr("Install python3-segno to show the QR code.")), False, False, 0)
            label = Gtk.Label()
            label.set_markup(f"<big><b>{GLib.markup_escape_text(url)}</b></big>")
            label.set_selectable(True)
            box.pack_start(label, False, False, 0)
            if url.startswith("https"):
                box.pack_start(Gtk.Label(label=tr(
                    "Self-signed certificate: on the phone, accept the security warning.")), False, False, 0)
            close = Gtk.Button(label=tr("Close"))
            close.connect("clicked", lambda *_: self.destroy())
            box.pack_start(close, False, False, 0)
            self.add(box)
            self.set_position(Gtk.WindowPosition.CENTER)
            self.set_keep_above(True)
            self.set_resizable(False)
            self.show_all()
            self.present()

    class Tray:
        def __init__(self):
            write_icons()
            self.proc = None
            self.on_air = False
            self._changing = False
            self._qr_on_start = False
            self.qr = None
            self.ind = AppIndicator.Indicator.new(
                f"{APP['slug']}-tray", f"{APP['slug']}-off", AppIndicator.IndicatorCategory.APPLICATION_STATUS)
            self.ind.set_icon_theme_path(ICONS)
            self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.ind.set_title(APP["name"])
            self._build_menu()
            self.refresh()
            GLib.timeout_add(TICK_MS, lambda: self.refresh() or True)

        def _build_menu(self):
            m = Gtk.Menu()
            self.it_status = Gtk.MenuItem(label="")
            self.it_status.set_sensitive(False)
            m.append(self.it_status)
            self.it_on = Gtk.CheckMenuItem(label=tr("On"))
            self.it_on.connect("toggled", self._toggled)
            m.append(self.it_on)
            self.it_lan = self.it_qr = None
            if APP["lan"]:
                self.it_lan = Gtk.MenuItem(label="")
                self.it_lan.set_sensitive(False)
                m.append(self.it_lan)
                self.it_qr = Gtk.MenuItem(label=tr("Show QR code"))
                self.it_qr.connect("activate", lambda *_: self.show_qr())
                m.append(self.it_qr)
            self.it_open = None
            if APP["open"]:
                self.it_open = Gtk.MenuItem(label=tr("Open in the browser"))
                self.it_open.connect("activate", lambda *_: subprocess.Popen(["xdg-open", APP["open"]]))
                m.append(self.it_open)
            it_log = Gtk.MenuItem(label=tr("View log"))
            it_log.connect("activate", self._view_log)
            m.append(it_log)
            # no "Quit" on purpose: the icon always stays on the bar; unchecking "On" turns it off
            m.show_all()
            self.ind.set_menu(m)
            self.ind.set_secondary_activate_target(self.it_on)  # middle click

        def _view_log(self, *_):
            os.makedirs(CACHE, exist_ok=True)
            open(LOG, "a").close()
            subprocess.Popen(["xdg-open", LOG])

        def _app_pids(self):
            return [p for p in pids_on_port(APP["port"]) if belongs(p)]

        def refresh(self):
            alive = self.proc is not None and self.proc.poll() is None
            if self.proc is not None and not alive:
                code = self.proc.returncode
                self.proc = None
                if code not in (0, -signal.SIGTERM, -signal.SIGKILL) and not self.on_air:
                    notify(tr("{name} did not start", name=APP["name"]), failure_note(code, last_log_line()))
            self.on_air = bool(self._app_pids())
            starting = alive and not self.on_air
            text = status_text(self.on_air, starting)
            self.it_status.set_label(text)
            self.ind.set_icon_full(f"{APP['slug']}-{'on' if self.on_air else 'off'}", text)
            self.ind.set_title(text)
            self._changing = True  # set_active fires "toggled"
            self.it_on.set_active(self.on_air or starting)
            self._changing = False
            if self.it_open:
                self.it_open.set_sensitive(self.on_air)
            if self.it_lan:
                url = lan_url()
                self.it_lan.set_label(tr("On the phone: {url}", url=url) if self.on_air
                                      else tr("Address: {url}", url=url))
                self.it_qr.set_sensitive(self.on_air)
                if self.on_air and self._qr_on_start:
                    self._qr_on_start = False
                    self.show_qr()
            if not alive and not self.on_air:
                self._qr_on_start = False

        def show_qr(self):
            if self.qr:
                self.qr.destroy()
            self.qr = QrWindow(lan_url())
            self.qr.connect("destroy", lambda *_: setattr(self, "qr", None))

        def _toggled(self, item):
            if self._changing:
                return
            if item.get_active():
                self.start()
            else:
                self.stop()
            GLib.timeout_add(600, lambda: self.refresh() and False)

        def start(self):
            if self.on_air or (self.proc and self.proc.poll() is None):
                return
            os.makedirs(CACHE, exist_ok=True)
            self._qr_on_start = bool(APP["lan"])
            env = dict(os.environ, **APP["env"])
            with open(LOG, "w") as out:
                self.proc = subprocess.Popen(
                    APP["cmd"], cwd=HERE, env=env, stdin=subprocess.DEVNULL, stdout=out,
                    stderr=subprocess.STDOUT, start_new_session=True)  # own group: stop takes the children

        def stop(self):
            self._qr_on_start = False
            if self.qr:
                self.qr.destroy()
            if self.proc and self.proc.poll() is None:
                _kill_group(self.proc.pid)
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    _kill_group(self.proc.pid, signal.SIGKILL)
                self.proc = None
            # started outside the icon (terminal): bring down whoever listens on the port
            for pid in self._app_pids():
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass

        def quit(self):
            if self.proc and self.proc.poll() is None:
                self.stop()
            Gtk.main_quit()

    tray = Tray()
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: tray.quit() or False)
    Gtk.main()


def _kill_group(pid, sig=signal.SIGTERM):
    try:
        os.killpg(pid, sig)
    except OSError:
        pass


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--install" in argv:
        install()
        return 0
    if "--remove" in argv:
        remove()
        return 0
    lock = single_instance()
    if lock is None:
        print(f"{APP['name']}: the icon is already on the bar")
        return 0
    run_icon()
    return 0


if __name__ == "__main__":
    sys.exit(main())
