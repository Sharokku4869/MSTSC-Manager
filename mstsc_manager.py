import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from collections import OrderedDict
from pathlib import Path
from tkinter import messagebox, ttk

APP_NAME = "MSTSC Manager"
# 判断是否是打包后的exe，获取exe真实所在文件夹
if getattr(sys, 'frozen', False):
    # 打包环境：exe所在目录
    APP_DIR = Path(sys.executable).resolve().parent
else:
    # 源码运行环境：py文件所在目录
    APP_DIR = Path(__file__).resolve().parent
PROFILES_PATH = APP_DIR / "profiles.json"
# 原测试代码
# APP_NAME = "MSTSC Manager"
# APP_DIR = Path(__file__).resolve().parent
# PROFILES_PATH = APP_DIR / "profiles.json"
TEMP_RDP_DIR = Path(tempfile.gettempdir()) / "MSTSC-Manager"
RDP_PORT = 3389
ONLINE_CHECK_TIMEOUT = 2.0
AUTO_REFRESH_INTERVAL_MS = 30_000

RESOLUTIONS = [
    ("3840 x 2160  (4K)",       3840, 2160),
    ("2560 x 1440  (2K)",       2560, 1440),
    ("1920 x 1080  (Full HD)",  1920, 1080),
    ("1680 x 1050",            1680, 1050),
    ("1600 x 900",             1600, 900),
    ("1440 x 900",             1440, 900),
    ("1366 x 768",             1366, 768),
    ("1280 x 1024",            1280, 1024),
    ("1280 x 720",             1280, 720),
    ("1024 x 768",             1024, 768),
    ("800 x 600",              800, 600),
]

STATUS_ONLINE  = "\u25cf"   # black circle (renders green via tag)
STATUS_OFFLINE = "\u25cb"   # white circle (renders grey via tag)


# --------------- data helpers ---------------

def encode_pw(plain):
    return base64.b64encode(plain.encode()).decode()

def decode_pw(encoded):
    return base64.b64decode(encoded.encode()).decode()

def load_profiles():
    if PROFILES_PATH.exists():
        return json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    return []

def save_profiles(profiles):
    PROFILES_PATH.write_text(
        json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# --------------- credential / rdp helpers ---------------

def set_cmdkey_credential(server, username, password):
    host = server.split(":")[0]
    target = f"TERMSRV/{host}"
    if "\\" in username:
        domain, user = username.split("\\", 1)
    subprocess.run(["cmdkey", "/delete", target], capture_output=True)
    result = subprocess.run(
        ["cmdkey", "/add", target, "/user", username, "/pass", password],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"cmdkey failed: {result.stderr.strip()}")

def generate_rdp_file(profile):
    TEMP_RDP_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"full address:s:{profile['server']}:{profile.get('port', RDP_PORT)}",
        f"username:s:{profile['username']}",
        f"screen mode id:i:{profile.get('screen_mode',0)}",
        f"desktopwidth:i:{profile.get('desktop_width',1920)}",
        f"desktopheight:i:{profile.get('desktop_height',1080)}",
        "session bpp:i:32",
        "redirectclipboard:i:1",
        "redirectprinters:i:1",
        "authentication level:i:2",
        "prompt for credentials:i:0",
        "negotiate security layer:i:1",
        "enablecredsspsupport:i:1",
        "disable wallpaper:i:0",
        "allow font smoothing:i:0",
        "allow desktop composition:i:0",
    ]
    rdp_path = TEMP_RDP_DIR / f"_{profile['name']}.rdp"
    rdp_path.write_text("\n".join(lines), encoding="utf-8")
    return rdp_path


# --------------- online checker (background thread) ---------------

class OnlineChecker:
    """Pings RDP port 3389 for every profile every N seconds."""

    def __init__(self, app):
        self._app = app
        self._lock = threading.Lock()
        self._status = {}          # profile_name -> bool
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while self._running:
            profiles = self._app.get_profiles_snapshot()
            for p in profiles:
                online = self._check_one(p['server'], p.get('port', RDP_PORT))
                with self._lock:
                    self._status[p["name"]] = online
            # schedule UI refresh on main thread
            try:
                self._app.after(0, self._app.refresh_online_status)
            except Exception:
                pass
            self._sleep_interruptible(AUTO_REFRESH_INTERVAL_MS / 1000.0)

    def _check_one(self, host, port):
        try:
            sock = socket.create_connection((host, port), timeout=ONLINE_CHECK_TIMEOUT)
            sock.close()
            return True
        except Exception:
            return False

    def _sleep_interruptible(self, seconds):
        """Sleep in small increments so shutdown is responsive."""
        for _ in range(int(seconds * 10)):
            if not self._running:
                return
            threading.Event().wait(0.1)

    def get_status(self, name):
        with self._lock:
            return self._status.get(name, False)

    def shutdown(self):
        self._running = False


# --------------- main application ---------------

class MSTSCManager(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("900x520")
        self.minsize(760, 440)
        self.resizable(True, True)

        self._profiles = []
        self._checker = None  # set after UI init                # list[dict]
        self._profile_map = {}             # name -> index
        self._selected_name = None         # currently selected profile name
        self._group_order = []             # ordered list of group names
        self._tree_iid_map = {}            # profile_name -> treeview iid
        self._group_iid_map = {}           # group_name -> treeview iid

        # styles
        self._setup_styles()
        self._build_ui()
        self._load_and_populate()

        # start online checker
        self._checker = OnlineChecker(self)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- styles ----------

    def _setup_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")

        # colours
        BG  = "#f0f0f0"
        FG  = "#1e1e1e"
        ACC = "#0078d4"
        self.configure(bg=BG)

        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 9))
        style.configure("TLabelframe", background=BG, foreground=FG)
        style.configure("TLabelframe.Label", background=BG, foreground=FG, font=("Segoe UI", 9, "bold"))
        style.configure("TButton", font=("Segoe UI", 9), padding=(10, 4))
        style.configure("Accent.TButton", font=("Segoe UI", 9, "bold"), padding=(14, 4))
        style.configure("TEntry", font=("Segoe UI", 9), padding=4)
        style.configure("TCombobox", font=("Segoe UI", 9), padding=4)
        style.configure("Treeview", font=("Segoe UI", 9), rowheight=24)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("TStatusbar.TLabel", background="#e0e0e0", foreground="#555",
                        font=("Segoe UI", 8), padding=(8, 3), anchor=tk.W)

        # tree tags
        self._tree = None  # set in _build_ui
        # will configure tags after tree is created

    # ---------- UI construction ----------

    def _build_ui(self):
        # ---- outer split: tree | settings ----
        outer = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        outer.pack(fill=tk.BOTH, expand=True, padx=(8,8), pady=(8,4))

        # LEFT panel -------------------------------------------------
        left = ttk.Frame(outer, width=280)
        outer.add(left, weight=0)

        # toolbar row
        toolbar = ttk.Frame(left)
        toolbar.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(toolbar, text="New", command=self._new_profile).pack(side=tk.LEFT, padx=(0,4))
        ttk.Button(toolbar, text="Delete", command=self._delete_profile).pack(side=tk.LEFT, padx=(0,4))
        ttk.Button(toolbar, text="\u25b2", width=3, command=lambda: self._move_profile(-1)).pack(side=tk.RIGHT, padx=(2,0))
        ttk.Button(toolbar, text="\u25bc", width=3, command=lambda: self._move_profile(1)).pack(side=tk.RIGHT, padx=(2,0))

        # treeview
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("status", "name")
        self._tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings",
                                  selectmode="browse")
        self._tree.heading("status", text="")
        self._tree.heading("name", text="Profiles")
        self._tree.column("status", width=28, stretch=False, anchor=tk.CENTER)
        self._tree.column("name", width=200, stretch=True, anchor=tk.W)
        self._tree.column("#0", width=24, stretch=False)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self._tree.yview)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree.configure(yscrollcommand=tree_scroll.set)

        # tree tags
        self._tree.tag_configure("online",  foreground="#107c10")
        self._tree.tag_configure("offline", foreground="#a0a0a0")
        self._tree.tag_configure("group",   font=("Segoe UI", 9, "bold"))

        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # RIGHT panel ------------------------------------------------
        right = ttk.Frame(outer)
        outer.add(right, weight=1)

        # use a single grid; two columns
        right.columnconfigure(0, weight=0)
        right.columnconfigure(1, weight=1)

        row = 0
        # section header
        ttk.Label(right, text="Profile Settings", font=("Segoe UI", 9, "bold")) \
            .grid(row=row, column=0, columnspan=2, sticky=tk.W, pady=(0,6))
        row += 1

        self._entries = {}
        fields = [
            ("Name:",     "name"),
            ("Server:",   "server"),
            ("Username:", "username"),
            ("Password:", "password"),
        ]
        for lbl, key in fields:
            ttk.Label(right, text=lbl, width=9, anchor=tk.E) \
                .grid(row=row, column=0, sticky=tk.E, padx=(0,6), pady=2)
            show = "*" if key == "password" else ""
            w = ttk.Entry(right, show=show)
            w.grid(row=row, column=1, sticky=tk.EW, pady=2)
            self._entries[key] = w
            row += 1

        # Port field
        ttk.Label(right, text="Port:", width=9, anchor=tk.E)             .grid(row=row, column=0, sticky=tk.E, padx=(0,6), pady=2)
        self._port_entry = ttk.Entry(right, width=7)
        self._port_entry.grid(row=row, column=1, sticky=tk.W, pady=2)
        self._port_entry.insert(0, str(RDP_PORT))
        row += 1

        # resolution + fullscreen combo row
        ttk.Label(right, text="Resolution:", width=9, anchor=tk.E) \
            .grid(row=row, column=0, sticky=tk.E, padx=(0,6), pady=2)

        res_row = ttk.Frame(right)
        res_row.grid(row=row, column=1, sticky=tk.EW, pady=2)
        self._res_var = tk.StringVar()
        self._res_combo = ttk.Combobox(res_row, textvariable=self._res_var,
                                       values=[r[0] for r in RESOLUTIONS],
                                       state="readonly", width=28)
        self._res_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._res_combo.set(RESOLUTIONS[2][0])

        self._fullscreen_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(res_row, text="Full", variable=self._fullscreen_var,
                        command=self._on_fullscreen_toggle) \
            .pack(side=tk.LEFT, padx=(8,0))
        row += 1

        # group field
        ttk.Label(right, text="Group:", width=9, anchor=tk.E) \
            .grid(row=row, column=0, sticky=tk.E, padx=(0,6), pady=2)
        self._group_var = tk.StringVar(value="Default")
        self._group_combo = ttk.Combobox(right, textvariable=self._group_var, width=28)
        self._group_combo.grid(row=row, column=1, sticky=tk.EW, pady=2)
        row += 1

        # button bar
        btn_row = ttk.Frame(right)
        btn_row.grid(row=row, column=0, columnspan=2, sticky=tk.EW, pady=(10,0))
        ttk.Button(btn_row, text="Connect", command=self._connect) \
            .pack(side=tk.LEFT, padx=(0,6))
        ttk.Button(btn_row, text="Save", command=self._save_profile) \
            .pack(side=tk.LEFT, padx=(0,6))
        ttk.Button(btn_row, text="Export .rdp", command=self._export_rdp) \
            .pack(side=tk.LEFT)

        # status bar
        self._status = ttk.Label(self, text="Ready", style="TStatusbar.TLabel")
        self._status.pack(fill=tk.X, side=tk.BOTTOM)

    # ---------- data management ----------

    def _load_and_populate(self):
        self._profiles = load_profiles()
        self._rebuild_profile_map()
        self._rebuild_tree()
        self._clear_form()
        self._refresh_group_combo()

    def _rebuild_profile_map(self):
        self._profile_map = {p["name"]: i for i, p in enumerate(self._profiles)}

    def _rebuild_tree(self):
        """Rebuild the whole tree from self._profiles."""
        tree = self._tree
        self._tree_iid_map.clear()
        self._group_iid_map.clear()

        for item in tree.get_children():
            tree.delete(item)

        # determine group order from profile order (first-seen)
        seen_groups = []
        for p in self._profiles:
            g = p.get("group", "Default") or "Default"
            if g not in seen_groups:
                seen_groups.append(g)
        self._group_order = seen_groups

        # create group nodes
        for g in seen_groups:
            g_iid = tree.insert("", tk.END, values=("", g), tags=("group",), open=True)
            self._group_iid_map[g] = g_iid

        # create profile nodes under groups
        for p in self._profiles:
            g = p.get("group", "Default") or "Default"
            g_iid = self._group_iid_map.get(g)
            if g_iid is None:
                g_iid = tree.insert("", tk.END, values=("", g), tags=("group",), open=True)
                self._group_iid_map[g] = g_iid
                if g not in self._group_order:
                    self._group_order.append(g)

            online = self._checker and self._checker.get_status(p["name"])
            status_char = STATUS_ONLINE if online else STATUS_OFFLINE
            tag = "online" if online else "offline"
            iid = tree.insert(g_iid, tk.END, values=(status_char, p["name"]), tags=(tag,))
            self._tree_iid_map[p["name"]] = iid

    def get_profiles_snapshot(self):
        """Thread-safe snapshot for the online checker."""
        return list(self._profiles)

    def refresh_online_status(self):
        """Called on main thread by OnlineChecker."""
        tree = self._tree
        for p in self._profiles:
            iid = self._tree_iid_map.get(p["name"])
            if iid is None:
                continue
            online = self._checker.get_status(p["name"])
            status_char = STATUS_ONLINE if online else STATUS_OFFLINE
            tag = "online" if online else "offline"
            tree.item(iid, values=(status_char, p["name"]), tags=(tag,))

    def _refresh_group_combo(self):
        groups = sorted(set(
            p.get("group", "Default") or "Default" for p in self._profiles
        ))
        self._group_combo["values"] = groups

    # ---------- form helpers ----------

    def _clear_form(self):
        for k in ("name", "server", "username", "password"):
            self._entries[k].delete(0, tk.END)
        self._res_combo.set(RESOLUTIONS[2][0])
        self._fullscreen_var.set(False)
        self._group_var.set("Default")
        self._port_entry.delete(0, tk.END)
        self._port_entry.insert(0, str(RDP_PORT))
        self._on_fullscreen_toggle()
        self._selected_name = None

    def _form_to_profile(self):
        res_label = self._res_var.get()
        width, height = 1920, 1080
        for lbl, w, h in RESOLUTIONS:
            if lbl == res_label:
                width, height = w, h
                break

        fullscreen = self._fullscreen_var.get()
        if fullscreen:
            width = self.winfo_screenwidth()
            height = self.winfo_screenheight()

        port_str = self._port_entry.get().strip()
        try:
            port = int(port_str)
            if port < 1 or port > 65535:
                raise ValueError
        except ValueError:
            port = RDP_PORT

        return {
            "name": self._entries["name"].get().strip(),
            "server": self._entries["server"].get().strip(),
            "username": self._entries["username"].get().strip(),
            "password_encoded": encode_pw(self._entries["password"].get()),
            "screen_mode": 1 if fullscreen else 0,
            "desktop_width": width,
            "desktop_height": height,
            "group": self._group_var.get().strip() or "Default",
            "port": port,
        }

    def _profile_to_form(self, profile):
        self._entries["name"].delete(0, tk.END)
        self._entries["name"].insert(0, profile.get("name", ""))
        self._entries["server"].delete(0, tk.END)
        self._entries["server"].insert(0, profile.get("server", ""))
        self._entries["username"].delete(0, tk.END)
        self._entries["username"].insert(0, profile.get("username", ""))

        pw = ""
        if profile.get("password_encoded"):
            try:
                pw = decode_pw(profile["password_encoded"])
            except Exception:
                pw = ""
        self._entries["password"].delete(0, tk.END)
        self._entries["password"].insert(0, pw)

        w = profile.get("desktop_width", 1920)
        h = profile.get("desktop_height", 1080)
        matched = False
        for lbl, rw, rh in RESOLUTIONS:
            if rw == w and rh == h:
                self._res_combo.set(lbl)
                matched = True
                break
        if not matched:
            self._res_combo.set(RESOLUTIONS[2][0])

        self._fullscreen_var.set(profile.get("screen_mode", 0) == 1)
        self._group_var.set(profile.get("group", "Default") or "Default")
        port = profile.get("port", RDP_PORT)
        self._port_entry.delete(0, tk.END)
        self._port_entry.insert(0, str(port))
        self._on_fullscreen_toggle()

    # ---------- events ----------

    def _on_tree_select(self, event=None):
        sel = self._tree.selection()
        if not sel:
            return
        iid = sel[0]
        values = self._tree.item(iid, "values")
        if not values or not values[1]:
            return
        name = values[1]
        # check if it matches a known profile
        idx = self._profile_map.get(name)
        if idx is None:
            return
        self._selected_name = name
        self._profile_to_form(self._profiles[idx])

    def _on_fullscreen_toggle(self):
        pass

    def _move_profile(self, direction):
        """direction: -1 for up, +1 for down."""
        sel = self._tree.selection()
        if not sel:
            return
        iid = sel[0]
        name = self._tree.item(iid, "values")[1]
        idx = self._profile_map.get(name)
        if idx is None:
            return

        profile = self._profiles[idx]
        group = profile.get("group", "Default") or "Default"

        # find siblings in the same group (ordered by _profiles)
        siblings = [(i, p) for i, p in enumerate(self._profiles)
                    if (p.get("group", "Default") or "Default") == group]
        if len(siblings) < 2:
            return

        pos_in_group = next((j for j, (i, p) in enumerate(siblings) if i == idx), None)
        if pos_in_group is None:
            return

        new_pos = pos_in_group + direction
        if new_pos < 0 or new_pos >= len(siblings):
            return

        # swap in _profiles
        other_idx = siblings[new_pos][0]
        self._profiles[idx], self._profiles[other_idx] = self._profiles[other_idx], self._profiles[idx]
        self._rebuild_profile_map()
        save_profiles(self._profiles)
        self._rebuild_tree()

        # re-select
        new_iid = self._tree_iid_map.get(name)
        if new_iid:
            self._tree.selection_set(new_iid)
            self._tree.see(new_iid)

    def _new_profile(self):
        self._tree.selection_remove(self._tree.selection())
        self._clear_form()
        self._entries["name"].focus_set()
        self._set_status("Creating new profile ...")

    def _save_profile(self):
        profile = self._form_to_profile()
        if not profile["name"] or not profile["server"] or not profile["username"]:
            messagebox.showwarning("Validation", "Name, Server, and Username are required.")
            return

        old_name = self._selected_name
        if old_name and old_name in self._profile_map:
            # update existing
            idx = self._profile_map[old_name]
            self._profiles[idx] = profile
            self._set_status(f"Profile '{profile['name']}' updated.")
        else:
            self._profiles.append(profile)
            self._set_status(f"Profile '{profile['name']}' created.")

        save_profiles(self._profiles)
        self._rebuild_profile_map()
        self._rebuild_tree()
        self._refresh_group_combo()

        # select the saved profile
        iid = self._tree_iid_map.get(profile["name"])
        if iid:
            self._tree.selection_set(iid)
            self._tree.see(iid)
            self._selected_name = profile["name"]

    def _delete_profile(self):
        if not self._selected_name or self._selected_name not in self._profile_map:
            messagebox.showinfo("Delete", "Select a profile to delete first.")
            return
        ok = messagebox.askyesno("Delete Profile",
                                 f"Delete profile '{self._selected_name}'?")
        if not ok:
            return
        idx = self._profile_map[self._selected_name]
        del self._profiles[idx]
        save_profiles(self._profiles)
        self._rebuild_profile_map()
        self._rebuild_tree()
        self._refresh_group_combo()
        self._clear_form()
        self._set_status(f"Profile deleted.")

    # ---------- connect / export ----------

    def _connect(self):
        profile = self._form_to_profile()
        if not profile["server"] or not profile["username"]:
            messagebox.showwarning("Validation", "Server and Username are required to connect.")
            return
        password = self._entries["password"].get()
        if not password:
            messagebox.showwarning("Validation", "Password is required to connect.")
            return

        self._set_status(f"Connecting to {profile['server']} ...")
        try:
            set_cmdkey_credential(profile["server"], profile["username"], password)
        except RuntimeError as exc:
            messagebox.showerror("Credential Error", str(exc))
            self._set_status("Connection failed - credential error.")
            return

        rdp_path = generate_rdp_file(profile)
        mstsc_args = ["mstsc"]
        if profile.get("screen_mode", 0) == 1:
            mstsc_args.append("/f")
        mstsc_args.append(str(rdp_path))

        try:
            subprocess.Popen(mstsc_args)
            self._set_status(f"Launched RDP to {profile['server']}.")
        except FileNotFoundError:
            messagebox.showerror("MSTSC Not Found",
                "mstsc.exe not found. Ensure Remote Desktop is available on this system.")
            self._set_status("Connection failed - mstsc not available.")

    def _export_rdp(self):
        profile = self._form_to_profile()
        if not profile["name"] or not profile["server"]:
            messagebox.showwarning("Validation", "Name and Server are required to export.")
            return

        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            defaultextension=".rdp",
            filetypes=[("RDP files", "*.rdp")],
            initialfile=f"{profile['name']}.rdp",
        )
        if not path:
            return

        password = self._entries["password"].get()
        if password:
            try:
                set_cmdkey_credential(profile["server"], profile["username"], password)
            except RuntimeError as exc:
                messagebox.showerror("Credential Error", str(exc))
                return

        rdp_temp = generate_rdp_file(profile)
        content = rdp_temp.read_text(encoding="utf-8")
        Path(path).write_text(content, encoding="utf-8")
        self._set_status(f"Exported .rdp to {path}")

    # ---------- helpers ----------

    def _set_status(self, text):
        self._status.configure(text=text)

    def _on_close(self):
        self._checker.shutdown()
        self.destroy()


if __name__ == "__main__":
    app = MSTSCManager()
    app.mainloop()
