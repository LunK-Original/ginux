
import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:  # The app still works; dropping files from outside is disabled.
    HAS_DND = False

try:
    import keyring
    HAS_KEYRING = True
except ImportError:
    HAS_KEYRING = False

APP_NAME = "Gitux"
CONFIG_PATH = Path.home() / ".gitpilot" / "config.json"
KEYRING_SERVICE = "gitpilot-github"

# ---------- Tema ----------
BG = "#0d1117"
PANEL = "#161b22"
PANEL_2 = "#1c2230"
BORDER = "#30363d"
ACCENT = "#2f81f7"
ACCENT_HOVER = "#1f6feb"
GREEN = "#238636"
GREEN_HOVER = "#2ea043"
TEXT_DIM = "#8b949e"
SELECT = "#264f78"

STATUS_COLORS = {
    "M": "#e3b341", "A": "#3fb950", "D": "#f85149", "?": "#58a6ff",
    "R": "#bc8cff", "C": "#bc8cff", "T": "#e3b341", "U": "#f85149",
}
STATUS_LABEL = {"?": "NEW"}

MAX_ROWS = 400


# =====================================================================
#  Helper: config, GitHub API, Git
# =====================================================================
def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


def gh_get(path, token):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": APP_NAME,
        },
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def gh_create_repo(name, token, private):
    data = json.dumps({"name": name, "private": private}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.github.com/user/repos",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": APP_NAME,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def gh_fetch_repos(token):
    repos = []
    for page in range(1, 11):
        chunk = gh_get(f"/user/repos?per_page=100&sort=updated&page={page}", token)
        if not chunk:
            break
        repos.extend(chunk)
        if len(chunk) < 100:
            break
    return repos


def _quiet_kwargs():
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def credential_fill():
    """Request GitHub credentials from Git Credential Manager."""
    p = subprocess.run(
        ["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, timeout=600,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, **_quiet_kwargs())
    if p.returncode != 0:
        return None
    d = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
    return d.get("password")


def credential_reject(token=None):
    data = "protocol=https\nhost=github.com\n"
    if token:
        data += f"password={token}\n"
    subprocess.run(["git", "credential", "reject"], input=data + "\n", capture_output=True,
                   text=True, timeout=30, **_quiet_kwargs())


def gh_cli_token():
    if not shutil.which("gh"):
        return None
    p = subprocess.run(["gh", "auth", "token", "--hostname", "github.com"],
                       capture_output=True, text=True, timeout=30, **_quiet_kwargs())
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def gh_cli_logout(username):
    if not username or not shutil.which("gh"):
        return
    p = subprocess.run(
        ["gh", "auth", "logout", "--hostname", "github.com", "--user", username],
        input="y\n", capture_output=True, text=True, timeout=30, **_quiet_kwargs())
    error = (p.stderr or p.stdout or "GitHub CLI logout failed.").strip()
    if p.returncode != 0 and error != "not logged in to any hosts":
        raise RuntimeError(error)


def acquire_github_token():
    """Get a validated token through GitHub sign-in."""
    for attempt in range(2):
        tok = credential_fill()
        if not tok:
            break
        try:
            gh_get("/user", tok)
            return tok
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                credential_reject(tok)  # Remove stale credentials and request sign-in again.
                continue
            raise
    tok = gh_cli_token()
    if tok:
        gh_get("/user", tok)
        return tok
    raise RuntimeError(
        "GitHub sign-in failed or was canceled. Make sure Git is fully installed "
        "(including Git Credential Manager), or sign in with GitHub CLI: gh auth login")


def run_git(args, cwd=None, token=None):
    """Run Git. Pass the token in a header without storing it in .git/config."""
    cmd = ["git"]
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]
    cmd += args
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    p = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, **kwargs,
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def parse_status(raw):
    """Parse `git status --porcelain=v1 -z` into unstaged and staged (code, path) pairs."""
    unstaged, staged = [], []
    parts = raw.split("\0")
    i = 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if len(e) < 4:
            continue
        x, y, path = e[0], e[1], e[3:]
        if x in "RC":
            i += 1  # Skip the original path for a rename.
        if x == "?" and y == "?":
            unstaged.append(("?", path))
            continue
        if x == "!":
            continue
        if x != " ":
            staged.append((x, path))
        if y != " ":
            unstaged.append((y, path))
    return unstaged, staged


# =====================================================================
#  File row (draggable)
# =====================================================================
class FileRow(ctk.CTkFrame):
    def __init__(self, master, app, side, path, code):
        selected = path in app.selected[side]
        super().__init__(master, fg_color=SELECT if selected else "transparent",
                         corner_radius=6, height=30)
        self.app, self.side, self.path = app, side, path
        self.pack(fill="x", padx=4, pady=1)

        color = STATUS_COLORS.get(code, TEXT_DIM)
        self.badge = ctk.CTkLabel(self, text=STATUS_LABEL.get(code, code), width=40,
                                  font=ctk.CTkFont(size=10, weight="bold"),
                                  text_color=color)
        self.badge.pack(side="left", padx=(6, 0))
        self.name = ctk.CTkLabel(self, text=path, anchor="w", font=ctk.CTkFont(size=12))
        self.name.pack(side="left", fill="x", expand=True, padx=6, pady=3)

        self._drag_start = None
        self._ghost = None
        self._dragging = False
        for w in (self, self.badge, self.name):
            w.bind("<ButtonPress-1>", self._press)
            w.bind("<B1-Motion>", self._motion)
            w.bind("<ButtonRelease-1>", self._release)
            w.bind("<Double-Button-1>", self._double)

    def _press(self, e):
        self._drag_start = (e.x_root, e.y_root)
        self._dragging = False

    def _motion(self, e):
        if not self._drag_start:
            return
        dx = abs(e.x_root - self._drag_start[0])
        dy = abs(e.y_root - self._drag_start[1])
        if not self._dragging and (dx > 6 or dy > 6):
            self._dragging = True
            self._make_ghost()
        if self._dragging and self._ghost:
            self._ghost.geometry(f"+{e.x_root + 14}+{e.y_root + 10}")

    def _make_ghost(self):
        n = len(self.app.selected[self.side]) if self.path in self.app.selected[self.side] else 1
        txt = os.path.basename(self.path) if n == 1 else f"{n} file"
        g = tk.Toplevel(self)
        g.overrideredirect(True)
        try:
            g.attributes("-topmost", True)
            g.attributes("-alpha", 0.9)
        except tk.TclError:
            pass
        tk.Label(g, text="  " + txt + "  ", bg=ACCENT, fg="white",
                 font=("Segoe UI", 10, "bold"), pady=4).pack()
        self._ghost = g

    def _release(self, e):
        if self._ghost:
            self._ghost.destroy()
            self._ghost = None
        if self._dragging:
            self._dragging = False
            target = self.app.panel_at(e.x_root, e.y_root)
            self.app.move_from_row(self, target)
        elif self._drag_start:
            self.app.toggle_select(self.side, self.path)
            sel = self.path in self.app.selected[self.side]
            self.configure(fg_color=SELECT if sel else "transparent")
        self._drag_start = None

    def _double(self, _e):
        self.app.move_paths(self.side, [self.path], "unstaged" if self.side == "staged" else "staged")


# =====================================================================
#  Aplikasi utama
# =====================================================================
_BaseWindow = ctk.CTk
if HAS_DND:
    class _BaseWindow(ctk.CTk, TkinterDnD.DnDWrapper):  # type: ignore
        pass


class App(_BaseWindow):
    LOGIN_HINT = "Click the button, then sign in to your\nGitHub account in the window that opens."

    def __init__(self):
        super().__init__()
        if HAS_DND:
            self.TkdndVersion = TkinterDnD._require(self)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title(f"{APP_NAME} - Git GUI")
        self.geometry("1220x780")
        self.minsize(1000, 640)
        self.configure(fg_color=BG)

        self.cfg = load_config()
        self.cfg.setdefault("repos", {})
        self.token = self._load_token()
        self.user = None
        self.repos = []
        self.repo = None  # Active local repository path.
        self.selected = {"unstaged": set(), "staged": set()}
        self.q = queue.Queue()
        self.busy = 0

        self._build_ui()
        self.after(60, self._poll)

        if HAS_DND:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_os_drop)

        if self.token:
            self._login_with_token(self.token, silent=True)
        last = self.cfg.get("last_repo")
        if last and Path(last).exists():
            self.set_repo(last)

    # ---------- util ----------
    def _load_token(self):
        if HAS_KEYRING:
            try:
                t = keyring.get_password(KEYRING_SERVICE, "token")
                if t:
                    return t
            except Exception:
                pass
        return self.cfg.get("token")

    def _store_token(self, token):
        if HAS_KEYRING:
            try:
                keyring.set_password(KEYRING_SERVICE, "token", token)
                self.cfg.pop("token", None)
                save_config(self.cfg)
                return
            except Exception:
                pass
        self.cfg["token"] = token
        save_config(self.cfg)

    def _clear_token(self):
        if HAS_KEYRING:
            try:
                keyring.delete_password(KEYRING_SERVICE, "token")
            except Exception:
                pass
        self.cfg.pop("token", None)
        save_config(self.cfg)

    def bg(self, fn, done=None):
        self._set_busy(+1)

        def run():
            try:
                res = fn()
            except Exception as ex:  # noqa
                res = ex
            self.q.put((done, res))

        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                done, res = self.q.get_nowait()
                self._set_busy(-1)
                if done:
                    done(res)
        except queue.Empty:
            pass
        self.after(60, self._poll)

    def _set_busy(self, d):
        self.busy = max(0, self.busy + d)
        self.status_lbl.configure(text="⏳ Working..." if self.busy else "● Ready")

    def git(self, args, auth=False):
        return run_git(args, cwd=self.repo, token=self.token if auth else None)

    def log(self, text):
        text = text.strip()
        if not text:
            return
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    # ---------- UI ----------
    def _build_ui(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main()

    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=300, corner_radius=0, fg_color=PANEL)
        sb.grid(row=0, column=0, sticky="nsw")
        sb.grid_propagate(False)
        sb.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(sb, text="⎇  Gitux", font=ctk.CTkFont(size=22, weight="bold")
                     ).grid(row=0, column=0, padx=20, pady=(22, 4), sticky="w")
        ctk.CTkLabel(sb, text="Git without the terminal", text_color=TEXT_DIM,
                     font=ctk.CTkFont(size=12)).grid(row=1, column=0, padx=20, sticky="w")

        acc = ctk.CTkFrame(sb, fg_color=PANEL_2, corner_radius=10)
        acc.grid(row=2, column=0, padx=14, pady=16, sticky="ew")
        acc.grid_columnconfigure(0, weight=1)
        self.user_lbl = ctk.CTkLabel(acc, text="Not signed in", anchor="w",
                                     font=ctk.CTkFont(size=13, weight="bold"))
        self.user_lbl.grid(row=0, column=0, padx=12, pady=(10, 0), sticky="ew")
        self.user_sub = ctk.CTkLabel(acc, text="Connect your GitHub account", anchor="w",
                                     text_color=TEXT_DIM, font=ctk.CTkFont(size=11))
        self.user_sub.grid(row=1, column=0, padx=12, sticky="ew")
        self.login_err = ctk.CTkLabel(acc, text=self.LOGIN_HINT,
                                      anchor="w", justify="left", text_color=TEXT_DIM,
                                      font=ctk.CTkFont(size=10), wraplength=240)
        self.login_err.grid(row=3, column=0, padx=12, pady=(4, 0), sticky="ew")
        self.login_btn = ctk.CTkButton(acc, text="Sign in with GitHub", height=32,
                                       fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                       command=self.open_login)
        self.login_btn.grid(row=4, column=0, padx=12, pady=10, sticky="ew")

        top = ctk.CTkFrame(sb, fg_color="transparent")
        top.grid(row=3, column=0, padx=14, sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._render_repos())
        ctk.CTkEntry(top, placeholder_text="🔍 Search repositories...", textvariable=self.search_var,
                     height=34, fg_color=PANEL_2, border_color=BORDER
                     ).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(top, text="⟳", width=34, height=34, fg_color=PANEL_2,
                      hover_color=BORDER, command=self.load_repos
                      ).grid(row=0, column=1, padx=(6, 0))
        ctk.CTkButton(top, text="+", width=34, height=34, fg_color=ACCENT,
                  hover_color=ACCENT_HOVER, command=self.create_repo
                  ).grid(row=0, column=2, padx=(6, 0))

        self.repo_list = ctk.CTkScrollableFrame(sb, fg_color="transparent",
                                                label_text="GitHub repositories",
                                                label_font=ctk.CTkFont(size=12),
                                                label_text_color=TEXT_DIM)
        self.repo_list.grid(row=4, column=0, padx=8, pady=8, sticky="nsew")

        ctk.CTkButton(sb, text="📂  Open local repository", height=36, fg_color=PANEL_2,
                      hover_color=BORDER, border_width=1, border_color=BORDER,
                      command=self.open_local_folder
                      ).grid(row=5, column=0, padx=14, pady=(0, 16), sticky="ew")

    def _build_main(self):
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=18, pady=14)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=4)
        main.grid_rowconfigure(4, weight=2)

        # Header
        head = ctk.CTkFrame(main, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        head.grid_columnconfigure(0, weight=1)
        self.repo_title = ctk.CTkLabel(head, text="No repository open", anchor="w",
                                       font=ctk.CTkFont(size=20, weight="bold"))
        self.repo_title.grid(row=0, column=0, sticky="w")
        self.repo_path_lbl = ctk.CTkLabel(head, text="Select a repository or open a local folder",
                                          anchor="w", text_color=TEXT_DIM,
                                          font=ctk.CTkFont(size=12))
        self.repo_path_lbl.grid(row=1, column=0, sticky="w")
        self.status_lbl = ctk.CTkLabel(head, text="● Ready", text_color=TEXT_DIM)
        self.status_lbl.grid(row=0, column=1, padx=10)
        ctk.CTkButton(head, text="⟳ Refresh", width=90, fg_color=PANEL_2, hover_color=BORDER,
                      command=self.refresh).grid(row=0, column=2)

        # Two panels
        panels = ctk.CTkFrame(main, fg_color="transparent")
        panels.grid(row=1, column=0, sticky="nsew")
        panels.grid_columnconfigure((0, 1), weight=1, uniform="p")
        panels.grid_rowconfigure(0, weight=1)
        self.panels, self.lists, self.count_lbls = {}, {}, {}
        specs = [
            ("unstaged", "Changes", "Changed or untracked files", 0),
            ("staged", "Staged", "Stage files here to prepare for commit", 1),
        ]
        for side, title, sub, col in specs:
            p = ctk.CTkFrame(panels, fg_color=PANEL, corner_radius=12,
                             border_width=2, border_color=BORDER)
            p.grid(row=0, column=col, sticky="nsew", padx=(0, 8) if col == 0 else (8, 0))
            p.grid_columnconfigure(0, weight=1)
            p.grid_rowconfigure(1, weight=1)
            hd = ctk.CTkFrame(p, fg_color="transparent")
            hd.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 0))
            hd.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(hd, text=title, font=ctk.CTkFont(size=14, weight="bold"),
                         anchor="w").grid(row=0, column=0, sticky="w")
            cl = ctk.CTkLabel(hd, text="0", fg_color=PANEL_2, corner_radius=10, width=34,
                              font=ctk.CTkFont(size=11, weight="bold"))
            cl.grid(row=0, column=1)
            ctk.CTkLabel(hd, text=sub, anchor="w", text_color=TEXT_DIM,
                         font=ctk.CTkFont(size=11)).grid(row=1, column=0, columnspan=2, sticky="w")
            lst = ctk.CTkScrollableFrame(p, fg_color="transparent")
            lst.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
            bar = ctk.CTkFrame(p, fg_color="transparent")
            bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
            bar.grid_columnconfigure(0, weight=1)
            if side == "unstaged":
                ctk.CTkButton(bar, text="＋ Stage all  (git add .)", height=32,
                              fg_color=ACCENT, hover_color=ACCENT_HOVER,
                              command=self.stage_all).grid(row=0, column=0, sticky="ew")
                ctk.CTkButton(bar, text="Stage selected →", height=32, width=130,
                              fg_color=PANEL_2, hover_color=BORDER,
                              command=lambda: self.move_selected("unstaged")
                              ).grid(row=0, column=1, padx=(6, 0))
            else:
                ctk.CTkButton(bar, text="－ Unstage all", height=32,
                              fg_color=PANEL_2, hover_color=BORDER,
                              command=self.unstage_all).grid(row=0, column=0, sticky="ew")
                ctk.CTkButton(bar, text="← Unstage selected", height=32, width=140,
                              fg_color=PANEL_2, hover_color=BORDER,
                              command=lambda: self.move_selected("staged")
                              ).grid(row=0, column=1, padx=(6, 0))
            self.panels[side], self.lists[side], self.count_lbls[side] = p, lst, cl

        # Commit
        cm = ctk.CTkFrame(main, fg_color=PANEL, corner_radius=12)
        cm.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        cm.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(cm, text="Commit message   (git commit -m \"...\")", anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")
                     ).grid(row=0, column=0, padx=14, pady=(10, 4), sticky="w")
        self.msg_box = ctk.CTkTextbox(cm, height=64, fg_color=BG, border_width=1,
                                      border_color=BORDER, font=ctk.CTkFont(size=13))
        self.msg_box.grid(row=1, column=0, padx=(14, 8), pady=(0, 12), sticky="ew")
        self.commit_btn = ctk.CTkButton(cm, text="✔  Commit", width=130, height=64,
                                        fg_color=GREEN, hover_color=GREEN_HOVER,
                                        font=ctk.CTkFont(size=14, weight="bold"),
                                        command=self.commit)
        self.commit_btn.grid(row=1, column=1, padx=(0, 14), pady=(0, 12))

        # Push bar
        pb = ctk.CTkFrame(main, fg_color=PANEL, corner_radius=12)
        pb.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ctk.CTkLabel(pb, text="Remote").grid(row=0, column=0, padx=(14, 4), pady=12)
        self.remote_menu = ctk.CTkOptionMenu(pb, values=["origin"], width=110, fg_color=PANEL_2,
                                             button_color=BORDER)
        self.remote_menu.grid(row=0, column=1, pady=12)
        ctk.CTkLabel(pb, text="Branch").grid(row=0, column=2, padx=(14, 4))
        self.branch_menu = ctk.CTkOptionMenu(pb, values=["main"], width=160, fg_color=PANEL_2,
                                             button_color=BORDER)
        self.branch_menu.grid(row=0, column=3)
        ctk.CTkButton(pb, text="+ Branch", width=80, fg_color=PANEL_2, hover_color=BORDER,
                      command=self.new_branch).grid(row=0, column=4, padx=(8, 0))
        ctk.CTkButton(pb, text="Checkout", width=80, fg_color=PANEL_2, hover_color=BORDER,
                      command=self.checkout).grid(row=0, column=5, padx=(6, 0))
        pb.grid_columnconfigure(6, weight=1)
        ctk.CTkButton(pb, text="⬇ Pull", width=90, fg_color=PANEL_2, hover_color=BORDER,
                      command=self.pull).grid(row=0, column=7, padx=6)
        ctk.CTkButton(pb, text="⬆  Push", width=130, height=36, fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, font=ctk.CTkFont(size=14, weight="bold"),
                      command=self.push).grid(row=0, column=8, padx=(0, 14))

        # Log
        self.logbox = ctk.CTkTextbox(main, fg_color="#010409", border_width=1, border_color=BORDER,
                                     font=ctk.CTkFont(family="Consolas", size=12), state="disabled")
        self.logbox.grid(row=4, column=0, sticky="nsew", pady=(10, 0))
        self.log("Welcome to Gitux. Sign in to GitHub and select a repository.")
        if not HAS_DND:
            self.log("Info: install 'tkinterdnd2' to drop files from Explorer/Finder. "
                     "Dragging files between panels still works.")

    # ---------- Panel helpers ----------
    def panel_at(self, x, y):
        for side, p in self.panels.items():
            px, py = p.winfo_rootx(), p.winfo_rooty()
            if px <= x <= px + p.winfo_width() and py <= y <= py + p.winfo_height():
                return side
        return None

    def toggle_select(self, side, path):
        s = self.selected[side]
        s.symmetric_difference_update({path})

    def move_from_row(self, row, target):
        if target is None or target == row.side:
            return
        paths = list(self.selected[row.side]) if row.path in self.selected[row.side] else [row.path]
        self.move_paths(row.side, paths, target)

    def move_selected(self, side):
        paths = list(self.selected[side])
        if not paths:
            self.log("Select a file first, or drag it directly.")
            return
        self.move_paths(side, paths, "staged" if side == "unstaged" else "unstaged")

    def move_paths(self, side, paths, target):
        if not self.repo or not paths or side == target:
            return
        if target == "staged":
            self._run_and_refresh(["add", "-A", "--"] + paths, f"git add ({len(paths)} files)")
        else:
            def work():
                rc, out = self.git(["reset", "-q", "HEAD", "--"] + paths)
                if rc != 0:  # The repository has no commits yet.
                    rc, out = self.git(["rm", "--cached", "-r", "-q", "--"] + paths)
                return rc, out
            self._run_and_refresh(work, f"unstage ({len(paths)} files)")

    def stage_all(self):
        if self.repo:
            self._run_and_refresh(["add", "."], "git add .")

    def unstage_all(self):
        if not self.repo:
            return

        def work():
            rc, out = self.git(["reset", "-q"])
            if rc != 0:
                rc, out = self.git(["rm", "--cached", "-r", "-q", "."])
            return rc, out
        self._run_and_refresh(work, "unstage all")

    def _run_and_refresh(self, cmd, label, auth=False):
        def work():
            return cmd() if callable(cmd) else self.git(cmd, auth=auth)

        def done(res):
            if isinstance(res, Exception):
                self.log(f"✗ {label}: {res}")
            else:
                rc, out = res
                self.log(f"{'✓' if rc == 0 else '✗'} {label}\n{out}")
            self.refresh()

        self.bg(work, done)

    # ---------- Render ----------
    def render_files(self, unstaged, staged):
        for side, items in (("unstaged", unstaged), ("staged", staged)):
            self.selected[side] &= {p for _, p in items}
            lst = self.lists[side]
            for w in lst.winfo_children():
                w.destroy()
            self.count_lbls[side].configure(text=str(len(items)))
            if not items:
                ctk.CTkLabel(lst, text="— empty —" if self.repo else "Open a repository first",
                             text_color=TEXT_DIM).pack(pady=30)
                continue
            for code, path in items[:MAX_ROWS]:
                FileRow(lst, self, side, path, code)
            if len(items) > MAX_ROWS:
                ctk.CTkLabel(lst, text=f"... and {len(items) - MAX_ROWS} more files "
                                       "(use 'Stage all')", text_color=TEXT_DIM
                             ).pack(pady=8)

    def refresh(self):
        if not self.repo:
            self.render_files([], [])
            return

        def work():
            rc, raw = self.git(["status", "--porcelain=v1", "-z", "-uall"])
            if rc != 0:
                raise RuntimeError(raw.strip())
            _, cur = self.git(["branch", "--show-current"])
            _, brs = self.git(["branch", "--format=%(refname:short)"])
            _, rem = self.git(["remote"])
            return raw, cur.strip(), [b for b in brs.split() if b], rem.split()

        def done(res):
            if isinstance(res, Exception):
                self.log(f"✗ Failed to read status: {res}")
                return
            raw, cur, branches, remotes = res
            unstaged, staged = parse_status(raw)
            self.render_files(unstaged, staged)
            if cur and cur not in branches:
                branches.append(cur)
            branches = branches or ["main"]
            self.branch_menu.configure(values=branches)
            self.branch_menu.set(cur if cur else branches[0])
            self.remote_menu.configure(values=remotes or ["origin"])
            if remotes and self.remote_menu.get() not in remotes:
                self.remote_menu.set(remotes[0])
            self.repo_title.configure(text=f"{Path(self.repo).name}   ⎇ {cur or '(detached)'}")

        self.bg(work, done)

    # ---------- Commit / push / pull ----------
    def commit(self):
        if not self.repo:
            return
        msg = self.msg_box.get("1.0", "end").strip()
        if not msg:
            messagebox.showwarning(APP_NAME, "Enter a commit message first.")
            return

        def done(res):
            if isinstance(res, Exception):
                self.log(f"✗ commit: {res}")
            else:
                rc, out = res
                self.log(f"{'✓' if rc == 0 else '✗'} git commit -m \"{msg.splitlines()[0]}\"\n{out}")
                if rc == 0:
                    self.msg_box.delete("1.0", "end")
            self.refresh()

        self.bg(lambda: self.git(["commit", "-m", msg]), done)

    def push(self):
        if not self.repo:
            return
        if not self.token:
            messagebox.showinfo(APP_NAME, "Sign in to GitHub before pushing.")
            return
        remote, branch = self.remote_menu.get(), self.branch_menu.get()
        if not messagebox.askyesno(APP_NAME, f"Push branch '{branch}' to {remote}?"):
            return
        self._run_and_refresh(["push", "-u", remote, branch],
                              f"git push -u {remote} {branch}", auth=True)

    def pull(self):
        if not self.repo or not self.token:
            messagebox.showinfo(APP_NAME, "Open a repository and sign in to GitHub first.")
            return
        remote, branch = self.remote_menu.get(), self.branch_menu.get()
        self._run_and_refresh(["pull", remote, branch], f"git pull {remote} {branch}", auth=True)

    def checkout(self):
        if self.repo:
            b = self.branch_menu.get()
            self._run_and_refresh(["checkout", b], f"git checkout {b}")

    def new_branch(self):
        if not self.repo:
            return
        dlg = ctk.CTkInputDialog(text="New branch name:", title="Create branch")
        name = (dlg.get_input() or "").strip().replace(" ", "-")
        if name:
            self._run_and_refresh(["checkout", "-b", name], f"git checkout -b {name}")

    # ---------- Drop files from the OS ----------
    def _on_os_drop(self, event):
        if not self.repo:
            self.log("Open a repository before dropping files.")
            return
        paths = self.tk.splitlist(event.data)
        target = self.panel_at(self.winfo_pointerx(), self.winfo_pointery()) or "staged"
        repo_root = Path(self.repo).resolve()

        def work():
            rels = []
            for p in paths:
                src = Path(p)
                try:
                    rel = src.resolve().relative_to(repo_root)
                except ValueError:  # Copy files from outside the repository to its root.
                    dst = repo_root / src.name
                    if src.is_dir():
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    else:
                        shutil.copy2(src, dst)
                    rel = Path(src.name)
                rels.append(rel.as_posix())
            if target == "staged":
                return self.git(["add", "-A", "--"] + rels)
            return 0, f"Copied to repository: {', '.join(rels)}"

        def done(res):
            if isinstance(res, Exception):
                self.log(f"✗ drop: {res}")
            else:
                self.log(f"{'✓' if res[0] == 0 else '✗'} drop file\n{res[1]}")
            self.refresh()

        self.bg(work, done)

    # ---------- GitHub sign-in and repositories ----------
    def open_login(self):
        if self.user:
            if messagebox.askyesno(
                    APP_NAME, f"Sign out from @{self.user['login']}?\n\n"
                              "The saved GitHub login on this computer will also be removed "
                              "so you can switch accounts."):
                tok = self.token
                username = self.user["login"]
                self.token, self.user, self.repos = None, None, []
                self._clear_token()

                def clear_credentials():
                    credential_reject(tok)
                    gh_cli_logout(username)

                def logout_done(res):
                    if isinstance(res, Exception):
                        self.log(f"✗ GitHub CLI logout: {res}")

                self.bg(clear_credentials, logout_done)
                self._update_account()
                self._render_repos()
            return

        self.login_btn.configure(state="disabled", text="Waiting for sign-in...")
        self.login_err.configure(
            text="The GitHub sign-in window will open.\nComplete sign-in there.",
            text_color=TEXT_DIM)

        def done(res):
            self._reset_login_ui()
            if isinstance(res, Exception):
                self.login_err.configure(text=str(res), text_color="#f85149")
                return
            self._login_with_token(res, on_fail=lambda m: self.login_err.configure(
                text=m, text_color="#f85149"))

        self.bg(acquire_github_token, done)

    def _reset_login_ui(self):
        self.login_btn.configure(text="Sign in with GitHub", fg_color=ACCENT,
                                 hover_color=ACCENT_HOVER, border_width=0, state="normal")
        self.login_err.configure(text=self.LOGIN_HINT, text_color=TEXT_DIM)

    def _login_with_token(self, token, silent=False, on_ok=None, on_fail=None):
        def work():
            return gh_get("/user", token)

        def done(res):
            if isinstance(res, Exception):
                msg = "Invalid token" if isinstance(res, urllib.error.HTTPError) \
                    else f"Connection failed: {res}"
                if not silent:
                    self.log(f"✗ GitHub sign-in: {msg}")
                if on_fail:
                    on_fail(msg)
                return
            self.token, self.user = token, res
            self._store_token(token)
            self._update_account()
            self.log(f"✓ Signed in as @{res['login']}")
            self.load_repos()
            if self.repo:
                self._ensure_identity()
            if on_ok:
                on_ok()

        self.bg(work, done)

    def _update_account(self):
        if self.user:
            self.user_lbl.configure(text=self.user.get("name") or self.user["login"])
            self.user_sub.configure(text=f"@{self.user['login']}")
            self.login_err.grid_remove()
            self.login_btn.configure(text="Logout", fg_color=PANEL, hover_color=BORDER,
                                     border_width=1, border_color=BORDER)
        else:
            self.user_lbl.configure(text="Not signed in")
            self.user_sub.configure(text="Connect your GitHub account")
            self.login_err.grid()
            self._reset_login_ui()

    def load_repos(self):
        if not self.token:
            return

        def done(res):
            if isinstance(res, Exception):
                self.log(f"✗ Failed to load repositories: {res}")
                return
            self.repos = res
            self.log(f"✓ Loaded {len(res)} repositories")
            self._render_repos()

        self.bg(lambda: gh_fetch_repos(self.token), done)

    def create_repo(self):
        if not self.token or not self.user:
            messagebox.showinfo(APP_NAME, "Sign in to GitHub before creating a repository.")
            return

        dlg = ctk.CTkInputDialog(text="Repository name:", title="Create GitHub repository")
        name = (dlg.get_input() or "").strip()
        if not name:
            return
        private = messagebox.askyesno(
            APP_NAME, "Create this repository as private? Select No to make it public.")
        source = None
        destination = None
        if messagebox.askyesno(
                APP_NAME, "Add files and folders from an existing local project?"):
            source = filedialog.askdirectory(title="Choose the project folder to upload")
            if not source:
                return
            parent = filedialog.askdirectory(title="Choose where to clone the new repository")
            if not parent:
                return
            destination = Path(parent) / name
            if destination.exists():
                messagebox.showwarning(
                    APP_NAME, f"The destination already exists:\n{destination}")
                return
            try:
                destination.resolve().relative_to(Path(source).resolve())
            except ValueError:
                pass
            else:
                messagebox.showwarning(
                    APP_NAME, "Choose a clone location outside the project folder.")
                return

        token, user = self.token, self.user

        def done(res):
            if isinstance(res, Exception):
                error = str(res)
                if destination and destination.exists() and str(destination) not in error:
                    error += f"\nLocal repository: {destination}"
                self.log(f"✗ Failed to create repository: {error}")
                messagebox.showerror(APP_NAME, f"Failed to create repository:\n{error}")
                self.load_repos()
                return
            repo_data, local_path = res
            self.log(f"✓ Created repository: {repo_data['full_name']}")
            if local_path:
                self.cfg["repos"][repo_data["full_name"]] = local_path
                save_config(self.cfg)
                self._render_repos()
                self.set_repo(local_path)
            self.load_repos()

        def work():
            repo_data = gh_create_repo(name, token, private)
            if not source:
                return repo_data, None

            rc, output = run_git(["clone", repo_data["clone_url"], str(destination)], token=token)
            if rc != 0:
                raise RuntimeError(
                    f"The GitHub repository was created, but cloning failed:\n{output}")

            try:
                shutil.copytree(
                    source, destination, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git"),
                )
            except OSError as ex:
                raise RuntimeError(
                    f"The repository was created and cloned, but files could not be copied: {ex}\n"
                    f"Local clone: {destination}") from ex

            for key, value in (
                    ("user.name", user.get("name") or user["login"]),
                    ("user.email", user.get("email") or
                     f"{user['id']}+{user['login']}@users.noreply.github.com")):
                rc, current = run_git(["config", key], cwd=destination)
                if rc != 0 or not current.strip():
                    rc, output = run_git(["config", key, value], cwd=destination)
                    if rc != 0:
                        raise RuntimeError(f"Could not configure {key}: {output}")

            rc, output = run_git(["add", "-A"], cwd=destination)
            if rc != 0:
                raise RuntimeError(f"Could not stage project files:\n{output}")
            rc, status = run_git(["status", "--porcelain"], cwd=destination)
            if rc != 0:
                raise RuntimeError(f"Could not read repository status:\n{status}")
            if not status.strip():
                raise RuntimeError(
                    "The repository was created and cloned, but the project folder has no files "
                    f"to commit. Local clone: {destination}")

            rc, output = run_git(["commit", "-m", "Initial commit"], cwd=destination)
            if rc != 0:
                raise RuntimeError(f"Could not create the initial commit:\n{output}")
            branch = run_git(["branch", "--show-current"], cwd=destination)[1].strip() or "main"
            rc, output = run_git(["push", "-u", "origin", branch], cwd=destination, token=token)
            if rc != 0:
                raise RuntimeError(
                    f"The initial commit was created, but push failed:\n{output}\n"
                    f"Local repository: {destination}")
            return repo_data, str(destination)

        self.bg(work, done)

    def _render_repos(self):
        for w in self.repo_list.winfo_children():
            w.destroy()
        if not self.repos:
            ctk.CTkLabel(self.repo_list, text="Sign in to view repositories",
                         text_color=TEXT_DIM).pack(pady=20)
            return
        q = self.search_var.get().strip().lower()
        for r in self.repos:
            if q and q not in r["full_name"].lower():
                continue
            local = self.cfg["repos"].get(r["full_name"])
            icon = "🔒" if r.get("private") else "📘"
            dot = "  ●" if local and Path(local).exists() else ""
            ctk.CTkButton(
                self.repo_list, text=f"{icon}  {r['full_name']}{dot}", anchor="w", height=34,
                fg_color="transparent", hover_color=PANEL_2, font=ctk.CTkFont(size=12),
                command=lambda repo=r: self.open_repo(repo),
            ).pack(fill="x", pady=1)

    # ---------- Open or clone a repository ----------
    def open_repo(self, r):
        local = self.cfg["repos"].get(r["full_name"])
        if local and Path(local).exists():
            self.set_repo(local)
            return
        if not messagebox.askyesno(APP_NAME, f"Repository '{r['full_name']}' is not on this computer.\n"
                             "Clone it now?"):
            return
        parent = filedialog.askdirectory(title="Choose a destination folder for the clone")
        if not parent:
            return
        dest = str(Path(parent) / r["name"])
        self.log(f"Cloning {r['full_name']} ...")

        def done(res):
            if isinstance(res, Exception):
                self.log(f"✗ clone: {res}")
                return
            rc, out = res
            self.log(f"{'✓' if rc == 0 else '✗'} git clone\n{out}")
            if rc == 0:
                self.cfg["repos"][r["full_name"]] = dest
                save_config(self.cfg)
                self._render_repos()
                self.set_repo(dest)

        self.bg(lambda: run_git(["clone", r["clone_url"], dest], token=self.token), done)

    def open_local_folder(self):
        d = filedialog.askdirectory(title="Choose a Git repository folder")
        if not d:
            return
        rc, out = run_git(["rev-parse", "--show-toplevel"], cwd=d)
        if rc != 0:
            if messagebox.askyesno(APP_NAME, "This folder is not a Git repository. Run 'git init'?"):
                rc2, out2 = run_git(["init", "-b", "main"], cwd=d)
                self.log(out2)
                if rc2 != 0:
                    return
                top = d
            else:
                return
        else:
            top = out.strip()
        self.set_repo(top)

    def set_repo(self, path):
        self.repo = str(Path(path))
        self.cfg["last_repo"] = self.repo
        save_config(self.cfg)
        self.repo_path_lbl.configure(text=self.repo)
        self.repo_title.configure(text=Path(self.repo).name)
        self.selected = {"unstaged": set(), "staged": set()}
        self.log(f"Opened repository: {self.repo}")
        self._ensure_identity()
        self.refresh()

    def _ensure_identity(self):
        """Set local user.name and user.email from GitHub when not already configured."""
        if not (self.repo and self.user):
            return
        repo, user = self.repo, self.user

        def work():
            _, n = run_git(["config", "user.name"], cwd=repo)
            _, m = run_git(["config", "user.email"], cwd=repo)
            if not n.strip():
                run_git(["config", "user.name", user.get("name") or user["login"]], cwd=repo)
            if not m.strip():
                run_git(["config", "user.email",
                         user.get("email") or f"{user['id']}+{user['login']}@users.noreply.github.com"],
                        cwd=repo)
            return 0, ""

        self.bg(work)


if __name__ == "__main__":
    if shutil.which("git") is None:
        print("Git is not installed or is not available on PATH.")
        sys.exit(1)
    App().mainloop()
