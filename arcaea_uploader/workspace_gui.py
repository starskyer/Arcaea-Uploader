"""Temporary batch management window; execution remains in the app worker."""

import os
from pathlib import Path
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from .workspaces import delete_workspace, list_workspaces, load_workspace


class WorkspaceManager:
    def __init__(self, app):
        self.app = app
        self.entries = {}
        self.window = tk.Toplevel(app.root)
        self.window.title("临时工作区管理")
        self.window.transient(app.root)
        width, height = app.px(780), app.px(440)
        x = max(0, min(app.root.winfo_rootx() + (app.root.winfo_width() - width) // 2,
                       app.root.winfo_screenwidth() - width))
        y = max(0, min(app.root.winfo_rooty() + (app.root.winfo_height() - height) // 2,
                       app.root.winfo_screenheight() - height - app.px(40)))
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.window.minsize(app.px(620), app.px(340))
        body = ttk.Frame(self.window, padding=app.px(14))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        hint = ttk.Label(body, text="预处理也会创建临时批次。失败批次保留；修复原始包后可在同一批次继续。",
                         wraplength=app.px(700))
        hint.grid(row=0, column=0, sticky="ew", pady=(0, app.px(6)))
        self.location = tk.StringVar()
        location_label = ttk.Label(body, textvariable=self.location, style="Muted.TLabel", wraplength=app.px(700))
        location_label.grid(row=1, column=0, sticky="ew", pady=(0, app.px(8)))
        listing = ttk.Frame(body)
        listing.grid(row=2, column=0, sticky="nsew")
        columns = ("name", "state", "songs", "errors")
        self.tree = ttk.Treeview(listing, columns=columns, show="headings", selectmode="browse")
        for key, label, width in zip(columns, ("批次", "状态", "已标准化", "ERROR"), (260, 180, 85, 65)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=app.px(width), minwidth=app.px(60), stretch=key in {"name", "state"})
        scrollbar = ttk.Scrollbar(listing, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.detail = tk.StringVar(value="选择批次可查看报告、继续处理或手动删除。")
        detail_label = ttk.Label(body, textvariable=self.detail, style="Muted.TLabel", wraplength=app.px(700))
        detail_label.grid(row=3, column=0, sticky="ew", pady=app.px(8))
        def resized(event):
            if event.widget == self.window:
                for label in (hint, location_label, detail_label):
                    label.configure(wraplength=max(app.px(40), event.width - app.px(28)))
        self.window.bind("<Configure>", resized)
        actions = ttk.Frame(body)
        actions.grid(row=4, column=0, sticky="ew")
        self.resume_button = ttk.Button(actions, text="继续预处理", command=self.resume)
        self.view_button = ttk.Button(actions, text="查看报告", command=self.view)
        self.open_button = ttk.Button(actions, text="打开目录", command=self.open_folder)
        self.delete_button = ttk.Button(actions, text="删除批次…", command=self.delete)
        for button in (self.resume_button, self.view_button, self.open_button, self.delete_button):
            button.pack(side="left", padx=(0, app.px(6)))
        ttk.Button(actions, text="刷新列表", command=self.refresh).pack(side="right")
        self.tree.bind("<<TreeviewSelect>>", lambda event: self.select())
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda event: self.close())
        self.refresh()
        self.window.update_idletasks()
        self.window.minsize(max(app.px(620), actions.winfo_reqwidth() + app.px(28)), app.px(340))

    def close(self):
        self.app.workspace_manager = None
        self.window.destroy()

    def refresh(self):
        raw = self.app.variables[("local", "release_root")].get().strip()
        self.tree.delete(*self.tree.get_children())
        self.entries = {}
        if not raw:
            self.detail.set("请先在配置页指定发布工作区。")
            self.select()
            return
        path = Path(raw).expanduser()
        self.release_root = (self.app.config_path.parent / path).resolve() if not path.is_absolute() else path.resolve()
        self.location.set(str(self.release_root))
        try:
            for row in list_workspaces(self.release_root):
                report = row["report"] or {}
                counts = report.get("counts", {})
                item = self.tree.insert("", "end", values=(row["name"], row["state"],
                    counts.get("normalized", 0), len(report.get("errors", []))))
                self.entries[item] = row
            self.detail.set("选择批次后继续将重新扫描原始包，仅复用校验通过的副本。" if self.entries
                            else "暂无临时批次。只查看此列表不会创建 releases 目录。")
        except (OSError, ValueError) as exc:
            self.detail.set(str(exc))
        self.select()

    def selected(self):
        selection = self.tree.selection()
        return self.entries.get(selection[0]) if selection else None

    def select(self):
        row = self.selected()
        ready = row is not None and row["report"] is not None and not self.app.busy
        self.resume_button.configure(state="normal" if ready else "disabled")
        self.view_button.configure(state="normal" if ready else "disabled")
        self.open_button.configure(state="normal" if row else "disabled")
        self.delete_button.configure(state="normal" if row and row["managed"] and not self.app.busy else "disabled")
        if row:
            self.detail.set(row["error"] or "输入：" + row["report"]["input_root"] +
                            "\n继续使用该批次的输入目录和 version，其余采用当前配置；变更项会重新核验。")

    def resume(self):
        row = self.selected()
        if row and not self.app.busy:
            path = Path(row["path"])
            self.close()
            self.app.resume_workspace(path)

    def view(self):
        row = self.selected()
        if not row or self.app.busy:
            return
        try:
            report = load_workspace(self.release_root, row["path"])
            self.app.show_workspace_report(report)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            messagebox.showerror("无法载入报告", str(exc), parent=self.window)

    def open_folder(self):
        row = self.selected()
        if not row:
            return
        try:
            if os.name == "nt":
                os.startfile(row["path"])
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", row["path"]],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self.window)

    def delete(self):
        row = self.selected()
        if not row or not row["managed"] or self.app.busy:
            return
        if not messagebox.askyesno("删除临时批次", "删除下列批次的全部工作副本和报告？\n\n" + row["path"] +
                                   "\n\n删除后无法继续此批次，原始曲目包保留。", parent=self.window, default="no"):
            return
        try:
            delete_workspace(self.release_root, row["path"])
            self.app.forget_workspace(Path(row["path"]))
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法清理工作区", str(exc), parent=self.window)
        self.refresh()
