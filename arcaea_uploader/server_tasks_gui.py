"""Inline constant editor and explicit, optional stage-03 execution."""

from copy import deepcopy
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .api_client import ApiClient
from .import_rules import actual_chart_classes
from .planner import PlanError, digest
from .server_tasks import (OPTIONS, confirm_uploaded, english_name, fingerprint, load_task,
    make_task, missing_constants, parse_constant, purchase_specs, read_reviewed, run_task,
    save_task, task_lock, task_path)


class ServerOperationsPanel:
    def __init__(self, app):
        self.app = app
        self.task = self.path = self.editor = None
        self.context_signature = None
        self.context_expired = False
        self.queue, self.poll_id = Queue(), None
        self.frame = ttk.Frame(app.results)
        app.results.add(self.frame, text="03 · 定数与服务端")
        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill="x", padx=6, pady=4)
        for label, command in (("审核总表…", self.choose_source), ("选项…", self.options),
                               ("保存定数", self.save), ("载入任务…", self.open_task)):
            ttk.Button(toolbar, text=label, command=command).pack(side="left", padx=(0, 4))
        self.execute_button = ttk.Button(toolbar, text="执行 03…", style="Accent.TButton", command=self.execute)
        self.execute_button.pack(side="right")
        self.hint = tk.StringVar(value="先预处理或检查本批曲目；定数双击填写，空难度固定 -1。03 可跳过。")
        hint_label = ttk.Label(self.frame, textvariable=self.hint, style="Muted.TLabel", wraplength=app.px(760))
        hint_label.pack(anchor="w", padx=8, pady=(0, 3))
        self.frame.bind("<Configure>", lambda event: hint_label.configure(
            wraplength=max(app.px(40), event.width - app.px(16))))
        listing = ttk.Frame(self.frame)
        listing.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(listing, columns=("sid", "name", "0", "1", "2", "3", "4"),
                                 show="headings", selectmode="browse")
        for key, label, width in zip(self.tree["columns"], ("SID", "English name", "0", "1", "2", "3", "4"),
                                      (165, 240, 70, 70, 70, 70, 70)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=app.px(width), minwidth=app.px(55), stretch=key in {"sid", "name"})
        vertical = ttk.Scrollbar(listing, command=self.tree.yview)
        horizontal = ttk.Scrollbar(listing, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", self.edit_cell)

    def set_context(self, report):
        self.cancel_editor()
        try:
            settings = self.app._collect()
            local = settings["local"]
            database = Path(local["server_project_root"]) / local["database_relative_path"] / "songs"
            if report.get("mode") == "preprocess":
                packages = [p for p in report["packages"] if p["status"] == "normalized"]
                rows = {"songs": [p["songlist"] for p in packages]}
                actual = {p["id"]: actual_chart_classes(Path(p["output"]), database / p["id"]) for p in packages}
            else:
                rows = report["merged_songlist"]
                actual = {}
                for song in report["songs"]:
                    sid = song["id"]
                    folders = {Path(f["source"]).parent for f in report["files"]
                               if f["area"] == "database" and f["path"].startswith("songs/" + sid + "/")
                               and f["path"].endswith(".aff") and not f["placeholder"]}
                    actual[sid] = actual_chart_classes(database / sid, *folders)
            batch = report.get("batch_root") or (str(Path(report["preprocessing_report"]).parent)
                                                  if report.get("preprocessing_report") else None)
            task_id = Path(batch).name if batch else "plan-" + fingerprint(
                [local["input_root"], settings["import"]["version"], sorted(actual)])[:16]
            self.path = Path(local["release_root"]) / ".server-tasks" / (task_id + ".json")
            previous = load_task(self.path) if self.path.is_file() else None
            self.task = make_task(task_id, rows, actual, previous)
            self.task["input_errors"] = report.get("errors", []).copy()
            self.context_signature = self.binding(settings)
            self.context_expired = False
            self.render()
        except (ValueError, OSError, KeyError, TypeError) as error:
            self.task = self.path = None
            self.tree.delete(*self.tree.get_children())
            self.hint.set("03 尚未准备：" + str(error))

    @staticmethod
    def binding(settings):
        return fingerprint([settings["import"]["version"],
            {key: settings["local"][key] for key in ("input_root", "assets_root", "server_project_root", "database_relative_path")}])

    def render(self):
        self.tree.delete(*self.tree.get_children())
        if not self.task:
            return
        for row in self.task["songs"]:
            sid = row["id"]
            values = [sid, english_name(row)]
            for c in range(5):
                value = parse_constant(self.task["constants"].get(sid, {}).get(str(c)))
                values.append("-1" if c not in self.task["actual"][sid] else "" if value is None else f"{value:.1f}")
            self.tree.insert("", "end", iid=sid, values=values)
        source = (self.task.get("source") or {}).get("path")
        self.hint.set(("审核表：" + Path(source).name if source else "尚未选择最终审核总表") +
                      f" · 未填 {len(missing_constants(self.task))} 项 · 双击录入；空难度 -1。")

    def save(self):
        if self.app.busy or not self.task or not self.commit_editor():
            return False
        try:
            if self.context_signature is not None:
                local = self.app._collect()["local"]
                root = self.path.parent.parent.resolve()
                for key in ("input_root", "assets_root", "server_project_root"):
                    if local[key] and root.is_relative_to(Path(local[key]).resolve()):
                        raise PlanError("发布工作区必须放在原始曲目、active 和服务端项目目录之外。")
            if not self.path.parent.exists():
                root = self.path.parent.parent.resolve()
                self.path = task_path(root, self.task["id"])
            save_task(self.path, self.task)
            return True
        except (ValueError, OSError) as error:
            messagebox.showerror("无法保存 03 任务", str(error), parent=self.app.root)
            return False

    def edit_cell(self, event):
        if self.app.busy or not self.task or not self.commit_editor():
            return
        sid, column = self.tree.identify_row(event.y), self.tree.identify_column(event.x)
        if not sid or not column or int(column[1:]) < 3:
            return
        difficulty = int(column[1:]) - 3
        if difficulty not in self.task["actual"][sid]:
            return
        box = self.tree.bbox(sid, column)
        if not box:
            return
        entry = ttk.Entry(self.tree)
        entry.place(x=box[0], y=box[1], width=box[2], height=box[3])
        entry.insert(0, self.tree.set(sid, str(difficulty)))
        self.editor = (entry, sid, difficulty)
        entry.bind("<Return>", lambda event: self.commit_editor())
        entry.bind("<Escape>", lambda event: self.cancel_editor())
        entry.focus_set()
        entry.select_range(0, "end")

    def cancel_editor(self):
        if self.editor:
            self.editor[0].destroy()
            self.editor = None

    def commit_editor(self):
        if not self.editor:
            return True
        entry, sid, difficulty = self.editor
        try:
            value = parse_constant(entry.get())
        except ValueError as error:
            messagebox.showerror("定数格式无效", str(error), parent=self.app.root)
            entry.focus_set()
            return False
        self.cancel_editor()
        previous = self.task["constants"].setdefault(sid, {}).get(str(difficulty))
        self.task["constants"][sid][str(difficulty)] = value
        if not self.save():
            self.task["constants"][sid][str(difficulty)] = previous
            return False
        self.render()
        return True

    def choose_source(self):
        if self.app.busy or not self.task or not self.commit_editor():
            return
        selected = filedialog.askopenfilename(parent=self.app.root, title="选择已经审核并用于上传的最终 songlist",
                                              filetypes=[("Songlist", "*")])
        if selected:
            try:
                updated = read_reviewed(self.task, selected)
                self.task = updated
                self.save()
                self.render()
            except (ValueError, OSError) as error:
                messagebox.showerror("无法载入审核总表", str(error), parent=self.app.root)

    def open_task(self):
        if self.app.busy or not self.commit_editor():
            return
        selected = filedialog.askopenfilename(parent=self.app.root, title="载入可继续执行的 03 任务",
                                              filetypes=[("03 任务", "*.json")])
        if selected:
            try:
                self.task, self.path = load_task(selected), Path(selected).resolve()
                self.context_signature, self.context_expired = None, False
                self.render()
            except (ValueError, OSError, KeyError, TypeError) as error:
                messagebox.showerror("无法载入 03 任务", str(error), parent=self.app.root)

    def options(self):
        if self.app.busy or not self.task or not self.commit_editor():
            return
        window = tk.Toplevel(self.app.root)
        window.title("03 操作选项")
        window.transient(self.app.root)
        body = ttk.Frame(window, padding=self.app.px(14))
        body.pack(fill="both", expand=True)
        variables = {}
        labels = ("1. 刷新 bundle → songs → world map", "2. 仅补充缺少的购买项（单曲 100，曲包 500）",
                  "3. 写入本批已指定定数（真实谱面可留空）", "4. 刷新曲目定数对应的成绩评分")
        for key, label in zip(OPTIONS, labels):
            variable = variables[key] = tk.BooleanVar(value=self.task["options"][key])
            ttk.Checkbutton(body, text=label, variable=variable).pack(anchor="w", pady=5)
        ttk.Label(body, text="购买默认无折扣、不允许兑换券；已有购买项保留。\n定数只接受正数及一位小数，不从 rating 推断。",
                  style="Muted.TLabel").pack(anchor="w", pady=8)
        source = (self.task.get("source") or {}).get("path")
        if source:
            ttk.Label(body, text="审核表：" + source, style="Muted.TLabel",
                      wraplength=self.app.px(480)).pack(anchor="w", pady=4)
        def apply():
            if self.app.busy:
                return
            self.task["options"] = {key: variable.get() for key, variable in variables.items()}
            if self.save():
                window.destroy()
        ttk.Button(body, text="保存选项", command=apply).pack(anchor="e")

    def execute(self):
        if self.app.busy or not self.task or not self.commit_editor():
            return
        try:
            if self.task.get("input_errors"):
                raise PlanError("原批次仍有 ERROR，请修复并重新检查后再执行 03。")
            if not self.task.get("source"):
                self.choose_source()
                if not self.task.get("source"):
                    return
            source = Path(self.task["source"]["path"])
            if source.is_file():
                self.task = read_reviewed(self.task, source)
                self.render()
            elif not self.task.get("publication"):
                raise PlanError("审核总表已不存在，且尚未确认上传。")
            settings = self.app._collect()
            if self.context_expired or self.context_signature and self.binding(settings) != self.context_signature:
                raise PlanError("曲目来源或资源配置已改变，请重新检查后再执行 03。")
            specs = purchase_specs(self.task) if self.task["options"]["purchases"] else []
            missing = missing_constants(self.task)
            details = ["将调用：" + settings["api"]["base_url"],
                       "审核表：" + self.task["source"]["path"],
                       "请确认本批资源已上传，且使用的正是当前审核 songlist。",
                       "购买检查（已有项保留）：" + (", ".join(s["name"] + f" ({s['price']})" for s in specs) or "无")]
            if missing:
                details.append("以下真实谱面尚未填写定数：\n" + ", ".join(missing) +
                               "\n继续将保留已有值；新增记录的未填难度暂用 -1。")
            if any(p.get("state") in {"running", "uncertain"} for p in self.task["progress"].values()):
                details.append("此前有请求结果未确认。请确认服务端处理已结束后继续；程序将先核对已有数据。")
            if not messagebox.askyesno("确认执行可选的 03 操作", "\n\n".join(details) + "\n\n继续执行？",
                                       parent=self.app.root, default="no"):
                return
            publication = self.task.get("publication") or {}
            if not publication or publication.get("kind") == "manual_confirmed" and self.task.get("state") == "complete":
                confirm_uploaded(self.task)
            if not self.save():
                return
            self.set_busy(True)
            self.app._set_details("03 服务端操作", [str(self.path)])
            self.app.results.select(2)
            Thread(target=self.worker, args=(settings, self.path), daemon=True).start()
            self.poll_id = self.app.root.after(80, self.poll)
        except (ValueError, OSError) as error:
            messagebox.showerror("无法执行 03", str(error), parent=self.app.root)

    def set_busy(self, busy):
        self.app.busy = busy
        for button in (self.app.check_button, self.app.preprocess_button, self.app.workspace_button, self.execute_button):
            button.configure(state="disabled" if busy else "normal")
        if busy:
            self.app.progress.start(12)
            self.app.status.set("03 正在执行；每一步完成后保存进度。")
        else:
            self.app.progress.stop()

    def worker(self, settings, path):
        task = None
        try:
            with task_lock(path):
                task = load_task(path)
                if not any(task["options"].values()):
                    task["state"] = "skipped"
                    save_task(path, task, locked=True)
                else:
                    run_task(task, ApiClient.from_settings(settings), persist=lambda task: save_task(path, task, locked=True),
                             on_log=lambda level, message: self.queue.put(("log", (level, message))), allow_missing=True)
            self.queue.put(("done", (task, None)))
        except Exception as error:
            self.queue.put(("done", (task, str(error))))

    def poll(self):
        self.poll_id = None
        for _ in range(100):
            try:
                kind, value = self.queue.get_nowait()
            except Empty:
                self.poll_id = self.app.root.after(80, self.poll)
                return
            if kind == "log":
                level, message = value
                self.app._append_log({"level": level, "message": message, "song_id": None})
            else:
                task, error = value
                if task:
                    self.task = task
                    self.render()
                self.set_busy(False)
                if error:
                    self.app._append_log({"level": "error", "message": error, "song_id": None})
                self.app.summary.set("03 未完成 · 进度已保留" if error else "03 已完成 · 可后续补填定数")
                self.app.status.set("查看 ERROR 后，可载入同一 03 任务继续。" if error else "03 操作完成。")
                return
        self.poll_id = self.app.root.after(20, self.poll)

    def close(self):
        if self.poll_id:
            self.app.root.after_cancel(self.poll_id)
        self.cancel_editor()
