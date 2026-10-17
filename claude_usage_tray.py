#!/usr/bin/env python3
"""KDE/Wayland system tray showing Claude plan usage for one or more Claude Code accounts.

Each account is a Claude Code config dir (CLAUDE_CONFIG_DIR). ~/.claude is always shown as
"default"; any ~/.claude-<name> dir is picked up automatically.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PyQt6.QtCore import QFileSystemWatcher, QLockFile, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (QApplication, QGridLayout, QLabel, QMenu, QProgressBar,
                             QPushButton, QSystemTrayIcon, QVBoxLayout, QWidget)

HOME = Path.home()
DEFAULT_DIR = HOME / ".claude"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "claude-usage-tray" / "config.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_CONFIG = {
    "refresh_minutes": 5,
    # Optional explicit list: [{"name": "work", "dir": "~/.claude-work"}]. Empty = auto-discover.
    "accounts": [],
}
WINDOWS = [("five_hour", "5-hour"), ("seven_day", "Week"),
           ("seven_day_opus", "Week (Opus)"), ("seven_day_sonnet", "Week (Sonnet)")]
LOGGED_OUT = "logged out"


class LoggedOut(Exception):
    pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except ValueError:
            pass
    else:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
    return cfg


def discover_accounts(cfg):
    if cfg["accounts"]:
        return [{"name": a["name"], "dir": Path(a["dir"]).expanduser()} for a in cfg["accounts"]]
    # Always list the default profile so a logged-out state is visible rather than "no accounts".
    accounts = [{"name": "default", "dir": DEFAULT_DIR}]
    for d in sorted(HOME.glob(".claude-*")):
        if d.is_dir():
            accounts.append({"name": d.name.removeprefix(".claude-"), "dir": d})
    return accounts


def account_email(cfg_dir):
    # Default profile keeps metadata in ~/.claude.json; CLAUDE_CONFIG_DIR profiles keep it inside the dir.
    for p in (cfg_dir / ".claude.json", HOME / ".claude.json" if cfg_dir == DEFAULT_DIR else None):
        if p and p.exists():
            try:
                return json.loads(p.read_text()).get("oauthAccount", {}).get("emailAddress")
            except (OSError, ValueError):
                pass
    return None


def creds_mtime(cfg_dir):
    try:
        return (cfg_dir / ".credentials.json").stat().st_mtime
    except OSError:
        return None


def read_oauth(cfg_dir):
    try:
        oauth = json.loads((cfg_dir / ".credentials.json").read_text()).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        raise LoggedOut() from None
    if not oauth.get("accessToken"):
        raise LoggedOut()
    return oauth


def profile_env(cfg_dir):
    env = dict(os.environ)
    if cfg_dir != DEFAULT_DIR:
        env["CLAUDE_CONFIG_DIR"] = str(cfg_dir)
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    return env


def detached(cmd, **kw):
    """Spawn fully detached: no inherited TTY, own session (a child touching the TTY would
    otherwise SIGTTIN/SIGTTOU-stop the whole tray when it was launched from a shell)."""
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True, **kw)


def nudge_cli_refresh(cfg_dir):
    """Let the Claude CLI refresh its own token rather than rotating refresh tokens behind its back."""
    try:
        subprocess.run(["claude", "auth", "status"], env=profile_env(cfg_dir), stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=30, start_new_session=True)
    except (OSError, subprocess.TimeoutExpired):
        pass


def fetch_usage(cfg_dir):
    oauth = read_oauth(cfg_dir)
    if oauth.get("expiresAt", 0) / 1000 < time.time() + 60:
        nudge_cli_refresh(cfg_dir)
        oauth = read_oauth(cfg_dir)
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {oauth['accessToken']}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-usage-tray/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise LoggedOut() from None
        raise
    data["_plan"] = oauth.get("subscriptionType")
    return data


def open_login(cfg_dir):
    """Run `claude auth login` for this profile in a terminal window."""
    script = "claude auth login; echo; read -rp 'Press Enter to close…'"
    for term in (["konsole", "-e"], ["ptyxis", "--"], ["gnome-terminal", "--"], ["kgx", "--"],
                 ["xfce4-terminal", "-x"], ["x-terminal-emulator", "-e"], ["xterm", "-e"]):
        if shutil.which(term[0]):
            detached(term + ["bash", "-c", script], env=profile_env(cfg_dir))
            return


def fmt_reset(iso):
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso)
    secs = int((dt - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "now"
    d, rem = divmod(secs, 86400)
    h, m = divmod(rem // 60, 60)
    rel = f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")
    return f"{rel} ({dt.astimezone().strftime('%a %H:%M')})"


def color_for(pct):
    if pct is None:
        return QColor("#888888")
    if pct >= 90:
        return QColor("#e5484d")
    if pct >= 70:
        return QColor("#f5a524")
    return QColor("#30a46c")


def make_icon(pct, text=None):
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#2b2b2b"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, size, size, 12, 12)
    if pct is not None:
        h = int(size * min(pct, 100) / 100)
        p.setBrush(color_for(pct))
        p.drawRoundedRect(0, size - h, size, h, 12, 12)
    p.setPen(QColor("#e5484d") if text == "!" else QColor("white"))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(34 if pct is None or pct < 100 else 26)
    p.setFont(f)
    label = text or ("?" if pct is None else str(int(round(pct))))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, label)
    p.end()
    return QIcon(pm)


class Fetcher(QObject):
    done = pyqtSignal(str, object, str)  # account name, data or None, error

    def __init__(self):
        super().__init__()
        self.inflight = set()

    def run(self, accounts):
        for a in accounts:
            if a["name"] not in self.inflight:
                self.inflight.add(a["name"])
                threading.Thread(target=self._one, args=(a,), daemon=True).start()

    def _one(self, a):
        try:
            self.done.emit(a["name"], fetch_usage(a["dir"]), "")
        except LoggedOut:
            self.done.emit(a["name"], None, LOGGED_OUT)
        except urllib.error.HTTPError as e:
            self.done.emit(a["name"], None, "rate limited" if e.code == 429 else f"HTTP {e.code}")
        except Exception as e:  # noqa: BLE001 - show anything in the UI
            self.done.emit(a["name"], None, str(e))


class Popup(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("Claude Usage")
        self.setMinimumWidth(420)
        self.layout_ = QVBoxLayout(self)
        self.body = QWidget()
        self.layout_.addWidget(self.body)
        btn = QPushButton("Refresh")
        btn.clicked.connect(app.refresh)
        self.layout_.addWidget(btn)

    def rebuild(self):
        old = self.body
        self.body = QWidget()
        grid = QGridLayout(self.body)
        row = 0
        for a in self.app.accounts:
            st = self.app.state.get(a["name"], {})
            plan = st["data"].get("_plan") if st.get("data") else None
            title = QLabel(f"<b>{a['name']}</b> <span style='color:gray'>{a.get('email') or ''}"
                           f"{' · ' + plan if plan else ''}</span>")
            grid.addWidget(title, row, 0, 1, 3)
            row += 1
            if st.get("error") == LOGGED_OUT:
                grid.addWidget(QLabel("<span style='color:#e5484d'>Logged out</span>"), row, 0, 1, 2)
                btn = QPushButton("Log in…")
                btn.clicked.connect(lambda _, d=a["dir"]: open_login(d))
                grid.addWidget(btn, row, 2)
                row += 1
            elif st.get("error"):
                grid.addWidget(QLabel(f"<span style='color:#e5484d'>{st['error']}</span>"), row, 0, 1, 3)
                row += 1
            elif not st:
                grid.addWidget(QLabel("<span style='color:gray'>loading…</span>"), row, 0, 1, 3)
                row += 1
            for key, label, pct, reset in self.app.windows_for(a["name"]):
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setValue(int(round(pct)))
                bar.setFormat(f"{pct:.0f}%")
                bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color_for(pct).name()}; }}")
                grid.addWidget(QLabel(label), row, 0)
                grid.addWidget(bar, row, 1)
                grid.addWidget(QLabel(f"<span style='color:gray'>resets {fmt_reset(reset)}</span>"), row, 2)
                row += 1
            row += 1
        if not self.app.accounts:
            grid.addWidget(QLabel("No accounts configured."), 0, 0)
        self.layout_.replaceWidget(old, self.body)
        old.deleteLater()
        self.adjustSize()


class TrayApp:
    def __init__(self):
        self.qapp = QApplication(sys.argv)
        self.qapp.setQuitOnLastWindowClosed(False)
        self.qapp.setDesktopFileName("claude-usage-tray")
        self.cfg = load_config()
        self.accounts = []
        self.state = {}
        self.mtimes = {}
        self.last_update = None

        self.tray = QSystemTrayIcon(make_icon(None))
        self.tray.activated.connect(self.on_activated)
        self.popup = Popup(self)

        # The menu is exported over D-Bus (StatusNotifierItem). Plasma caches action ids, so actions
        # are created once and only updated in place; rebuilding them on every render left Plasma
        # holding ids of deleted actions and clicks did nothing.
        self.menu = QMenu()
        self.account_actions = {}  # name -> (status, line1, line2, login)
        self.static_sep = self.menu.addSeparator()
        for text, slot in (("Show details", self.show_popup), ("Refresh now", self.refresh),
                           ("Open config", lambda: detached(["xdg-open", str(CONFIG_FILE)])),
                           ("Quit", self.qapp.quit)):
            act = QAction(text, self.menu)
            act.triggered.connect(slot)
            self.menu.addAction(act)
        self.tray.setContextMenu(self.menu)

        self.fetcher = Fetcher()
        self.fetcher.done.connect(self.on_result)
        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(int(self.cfg["refresh_minutes"] * 60_000))
        # Re-render relative reset times every minute without hitting the API.
        self.tick = QTimer()
        self.tick.timeout.connect(self.render)
        self.tick.start(60_000)

        # Pick up logins/logouts and new ~/.claude-* profiles immediately.
        self.watcher = QFileSystemWatcher()
        self.watcher.directoryChanged.connect(lambda _: self.debounce.start())
        self.debounce = QTimer(singleShot=True, interval=1500)
        self.debounce.timeout.connect(self.check_changes)

        self.tray.show()
        self.refresh()

    def sync_accounts(self):
        known = {a["name"]: a for a in self.accounts}
        self.accounts = discover_accounts(self.cfg)
        for a in self.accounts:
            a["email"] = account_email(a["dir"]) or known.get(a["name"], {}).get("email")
        names = {a["name"] for a in self.accounts}
        for name in list(self.account_actions):
            if name not in names:
                for act in self.account_actions.pop(name):
                    self.menu.removeAction(act)
                self.state.pop(name, None)
        for a in self.accounts:
            if a["name"] not in self.account_actions:
                acts = []
                for i in range(4):
                    act = QAction("", self.menu)
                    act.setEnabled(i == 3)
                    act.setVisible(False)
                    self.menu.insertAction(self.static_sep, act)
                    acts.append(act)
                acts[3].setText(f"    Log in {a['name']}…")
                acts[3].triggered.connect(lambda _, d=a["dir"]: open_login(d))
                self.account_actions[a["name"]] = acts
        dirs = [str(HOME)] + [str(a["dir"]) for a in self.accounts if a["dir"].is_dir()]
        if self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())
        self.watcher.addPaths(dirs)
        self.mtimes = {a["name"]: creds_mtime(a["dir"]) for a in self.accounts}

    def refresh(self):
        self.cfg = load_config()
        self.sync_accounts()
        self.render()
        self.fetcher.run(self.accounts)

    def check_changes(self):
        """Directory changed: refetch only accounts whose credentials file appeared/changed/vanished."""
        before = [a["name"] for a in self.accounts]
        old = dict(self.mtimes)
        self.sync_accounts()
        changed = [a for a in self.accounts
                   if a["name"] not in before or self.mtimes.get(a["name"]) != old.get(a["name"])]
        if changed or len(before) != len(self.accounts):
            self.render()
            self.fetcher.run(changed)

    def on_result(self, name, data, error):
        self.fetcher.inflight.discard(name)
        prev = self.state.get(name, {})
        # Keep last good data on transient errors, but never show stale numbers once logged out.
        keep = None if error == LOGGED_OUT else prev.get("data")
        self.state[name] = {"data": data or keep, "error": error}
        self.last_update = datetime.now()
        self.render()

    def windows_for(self, name):
        data = (self.state.get(name) or {}).get("data") or {}
        out = []
        for key, label in WINDOWS:
            w = data.get(key)
            if w and w.get("utilization") is not None:
                out.append((key, label, float(w["utilization"]), w.get("resets_at")))
        return out

    def show_popup(self):
        self.popup.rebuild()
        self.popup.show()
        self.popup.raise_()
        self.popup.activateWindow()

    def on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.popup.isVisible():
                self.popup.hide()
            else:
                self.show_popup()

    def render(self):
        # Icon shows the highest 5-hour usage across accounts (the limit you'll hit first),
        # or "!" when nothing has data and some account is logged out.
        five = [p for a in self.accounts for k, _, p, _ in self.windows_for(a["name"]) if k == "five_hour"]
        logged_out = [a["name"] for a in self.accounts if self.state.get(a["name"], {}).get("error") == LOGGED_OUT]
        self.tray.setIcon(make_icon(max(five) if five else None, None if five or not logged_out else "!"))

        tip = []
        for a in self.accounts:
            st = self.state.get(a["name"], {})
            wins = self.windows_for(a["name"])
            parts = [f"{label} {pct:.0f}%" for _, label, pct, _ in wins[:2]]
            if st.get("error") == LOGGED_OUT:
                status = "logged out"
            else:
                status = " · ".join(parts) or st.get("error") or "loading…"
            line = f"{a['name']}: {status}"
            tip.append(line)
            acts = self.account_actions[a["name"]]
            acts[0].setText(line)
            acts[0].setVisible(True)
            for act, w in zip(acts[1:3], wins[:2] if st.get("error") != LOGGED_OUT else []):
                act.setText(f"    {w[1]} resets {fmt_reset(w[3])}")
            for i, act in enumerate(acts[1:3]):
                act.setVisible(st.get("error") != LOGGED_OUT and i < len(wins))
            acts[3].setVisible(st.get("error") == LOGGED_OUT)

        if self.last_update:
            tip.append(f"updated {self.last_update.strftime('%H:%M')}")
        self.tray.setToolTip("Claude usage\n" + "\n".join(tip))
        if self.popup.isVisible():
            self.popup.rebuild()

    def exec(self):
        return self.qapp.exec()


if __name__ == "__main__":
    lock = QLockFile(str(Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "claude-usage-tray.lock"))
    if not lock.tryLock(100):
        sys.exit("claude-usage-tray is already running")
    sys.exit(TrayApp().exec())
