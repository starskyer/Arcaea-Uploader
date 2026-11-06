"""Temporary batch management window; execution remains in the app worker."""

import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Thread
import tkinter as tk
from tkinter import messagebox, ttk

from .workspaces import delete_workspace, load_workspace
from .workspace_session import entries as session_entries


class WorkspaceManager:
    def __init__(self, app):
        self.app = app
        self.entries = {}
        self.loading = False
        self.poll_id = None
        self.queue = Queue()
        self.window = tk.Toplevel(app.root)
        self.window.title("未提交工作区管理")
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
        hint = ttk.Label(body, text="01 可累积准备，02 上传后仍保留；03 完成并确认提交后才清理。",
                         wraplength=app.px(700))
        hint.grid(row=0, column=0, sticky="ew", pady=(0, app.px(6)))
        self.location = tk.StringVar()
        location_label = ttk.Label(body, textvariable=self.location, style="Muted.TLabel", wraplength=app.px(700))
        location_label.grid(row=1, column=0, sticky="ew", pady=(0, app.px(8)))
        listing = ttk.Frame(body)
        listing.grid(row=2, column=0, sticky="nsew")
        columns = ("name", "state", "songs", "errors", "server")
        self.tree = ttk.Treeview(listing, columns=columns, show="headings", selectmode="browse")
        for key, label, width in zip(columns, ("批次", "状态", "曲目", "ERROR", "服务端"), (210, 180, 65, 60, 170)):
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
        self.resume_button = ttk.Button(actions, text="继续所选批次", command=self.resume)
        self.view_button = ttk.Button(actions, text="查看报告", command=self.view)
        self.open_button = ttk.Button(actions, text="打开目录", command=self.open_folder)
        self.delete_button = ttk.Button(actions, text="放弃工作区…", command=self.delete)
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
        if self.poll_id:
            self.window.after_cancel(self.poll_id)
            self.poll_id = None
        self.app.workspace_manager = None
        self.window.destroy()

    def refresh(self):
        if self.loading:
            return
        raw = self.app.variables[("local", "release_root")].get().strip()
        self.tree.delete(*self.tree.get_children())
        self.entries = {}
        path = Path(raw).expanduser() if raw else None
        self.release_root = ((self.app.config_path.parent / path).resolve() if not path.is_absolute() else path.resolve()) if path else None
        self.location.set("当前目录：" + str(self.release_root) + "；同时列出此前登记的工作区。" if path else "列出此前登记的未提交工作区。")
        self.detail.set("正在读取工作区列表…")
        self.loading = True
        self.select()
        # Capture Tk values above; the worker only reads filesystem records.
        session, release_root = self.app.session_path, self.release_root
        def scan():
            try:
                self.queue.put((session_entries(session, release_root), None))
            except Exception as error:
                self.queue.put(([], str(error)))
        Thread(target=scan, daemon=True).start()
        self.poll_id = self.window.after(25, self._poll_list)

    def _poll_list(self):
        self.poll_id = None
        try:
            rows, error = self.queue.get_nowait()
        except Empty:
            self.poll_id = self.window.after(25, self._poll_list)
            return
        self.loading = False
        try:
            if error:
                raise ValueError(error)
            for row in rows:
                report = row["report"] or {}
                counts = report.get("counts", {})
                item = self.tree.insert("", "end", values=(row["name"], row["state"],
                    counts.get("normalized", 0), len(report.get("errors", [])) + row.get("pending_errors", 0), row.get("server", "未绑定")))
                self.entries[item] = row
            self.detail.set("继续所选工作区会恢复它的服务器配置、暂存内容和操作进度。" if self.entries
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
                            "\n服务端：" + row.get("server", "未绑定") + "；恢复该工作区保存的配置与进度。")

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
            report = load_workspace(Path(row["path"]).parent, row["path"])
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
        if not messagebox.askyesno("放弃未提交工作区", "删除下列工作区的全部暂存内容与进度？\n\n" + row["path"] +
                                   "\n\n已上传资源和已执行的 API 操作不会撤销；原始曲目包保留。", parent=self.window, default="no"):
            return
        try:
            delete_workspace(Path(row["path"]).parent, row["path"])
            self.app.forget_workspace(Path(row["path"]))
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法清理工作区", str(exc), parent=self.window)
        self.refresh()
