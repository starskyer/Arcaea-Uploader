"""Inline constant editor and explicit, optional stage-03 execution."""

from copy import deepcopy
import json
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .api_client import ApiClient
from .import_rules import actual_chart_classes, portable_id
from .planner import PlanError, digest
from .server_tasks import (OPTIONS, confirm_uploaded, english_name, existing_task_path, fingerprint, finish_task, load_task,
    make_task, missing_constants, parse_constant, analyze_purchases, purchase_skips, purchase_skip_signature, confirm_purchase_skips,
    read_reviewed, run_task, set_purchase_override, save_task, task_complete, task_lock, task_path, update_purchase_candidates,
    create_followup, archive_followup)
from .workspaces import (bind_workspace_api, commit_workspace, verify_workspace_api, verify_workspace_publication, workspace_lock)


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
        self.edit_buttons = []
        for label, command in (("审核总表…", self.choose_source), ("选项…", self.options),
                               ("保存定数", self.save), ("载入任务…", self.open_task)):
            button = ttk.Button(toolbar, text=label, command=command)
            button.pack(side="left", padx=(0, 4))
            if command != self.open_task:
                self.edit_buttons.append(button)
            if command == self.choose_source:
                self.source_button = button
        self.commit_button = ttk.Button(toolbar, text="提交并清理…", command=self.commit, state="disabled")
        self.commit_button.pack(side="right")
        self.execute_button = ttk.Button(toolbar, text="执行 03…", style="Accent.TButton", command=self.execute)
        self.execute_button.pack(side="right")
        self.hint = tk.StringVar(value="定数双击填写；0 / -1 不计评分，留空保留已有值；空难度固定 -1。")
        hint_row = ttk.Frame(self.frame)
        hint_row.pack(fill="x", padx=8, pady=(0, 3))
        self.followup_button = ttk.Button(hint_row, text="创建后续任务…", command=self.new_followup, state="disabled")
        self.followup_button.pack(side="right", padx=(8, 0))
        hint_label = ttk.Label(hint_row, textvariable=self.hint, style="Muted.TLabel", wraplength=app.px(600))
        hint_label.pack(side="left", fill="x", expand=True)
        self.frame.bind("<Configure>", lambda event: hint_label.configure(
            wraplength=max(app.px(40), event.width - self.followup_button.winfo_reqwidth() - app.px(32))))
        self.review_tabs = ttk.Notebook(self.frame)
        self.review_tabs.pack(fill="both", expand=True)
        listing = ttk.Frame(self.review_tabs)
        self.review_tabs.add(listing, text="曲目定数")
        self.purchase_frame = ttk.Frame(self.review_tabs)
        self.review_tabs.add(self.purchase_frame, text="购买项预览")
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
        purchase_toolbar = ttk.Frame(self.purchase_frame)
        purchase_toolbar.pack(fill="x", padx=6, pady=4)
        self.purchase_summary = tk.StringVar(value="01 后即可分析，无需连接服务器。")
        ttk.Label(purchase_toolbar, textvariable=self.purchase_summary, style="Muted.TLabel").pack(side="left")
        ttk.Button(purchase_toolbar, text="导出清单…", command=self.export_analysis).pack(side="right")
        ttk.Button(purchase_toolbar, text="重新分析总表", command=self.refresh_analysis).pack(side="right", padx=4)
        self.purchase_edit_button = ttk.Button(purchase_toolbar, text="编辑所选…", command=self.edit_purchase)
        self.purchase_edit_button.pack(side="right", padx=4)
        self.purchase_details = tk.StringVar(value="候选由最终 songlist 推导；执行 03 时查询远端，仅补缺少的购买项，已有项保留。")
        ttk.Label(self.purchase_frame, textvariable=self.purchase_details, style="Muted.TLabel",
                  wraplength=app.px(730)).pack(side="bottom", fill="x", padx=8, pady=5)
        purchase_listing = ttk.Frame(self.purchase_frame)
        purchase_listing.pack(fill="both", expand=True)
        self.purchase_tree = ttk.Treeview(purchase_listing,
            columns=("name", "kind", "price", "orig", "item", "songs", "state"), show="headings", selectmode="browse")
        for key, label, width in zip(self.purchase_tree["columns"],
                ("购买项", "类别", "价格", "原价", "关联物品", "引用曲目", "状态"), (150, 60, 60, 60, 150, 180, 110)):
            self.purchase_tree.heading(key, text=label)
            self.purchase_tree.column(key, width=app.px(width), minwidth=app.px(50), stretch=key in {"name", "songs"})
        vertical = ttk.Scrollbar(purchase_listing, command=self.purchase_tree.yview)
        horizontal = ttk.Scrollbar(purchase_listing, orient="horizontal", command=self.purchase_tree.xview)
        self.purchase_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        self.purchase_tree.pack(fill="both", expand=True)
        self.purchase_tree.bind("<<TreeviewSelect>>", lambda event: self.show_purchase_detail())
        self.purchase_tree.bind("<Double-1>", self.edit_purchase)
        self.review_tabs.bind("<<NotebookTabChanged>>", lambda event: self.refresh_analysis()
            if self.review_tabs.select() == str(self.purchase_frame) else None)
        self.purchase_rows = {}
        self.purchase_row_ids = {}
        self.purchase_analysis = None
        self.purchase_editor = None

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
                actual = deepcopy(report.get("actual", {}))
                for song in report["songs"]:
                    sid = song["id"]
                    if sid in actual:
                        continue
                    folders = {Path(f["source"]).parent for f in report["files"]
                               if f["area"] == "database" and f["path"].startswith("songs/" + sid + "/")
                               and f["path"].endswith(".aff") and not f["placeholder"]}
                    actual[sid] = actual_chart_classes(database / sid, *folders)
            batch = report.get("batch_root") or (str(Path(report["preprocessing_report"]).parent)
                                                  if report.get("preprocessing_report") else None)
            task_id = Path(batch).name if batch else "plan-" + fingerprint(
                [local["input_root"], settings["import"]["version"], sorted(actual)])[:16]
            self.path = task_path(local["release_root"], task_id, create=False)
            prior = existing_task_path(local["release_root"], task_id)
            previous = load_task(prior) if prior.is_file() else None
            self.task = make_task(task_id, rows, actual, previous)
            self.task["input_errors"] = report.get("errors", []).copy()
            self.context_signature = self.binding(settings)
            self.context_expired = False
            self.render()
        except (ValueError, OSError, KeyError, TypeError) as error:
            self.task = self.path = None
            self.render()
            self.hint.set("03 尚未准备：" + str(error))

    @staticmethod
    def binding(settings):
        return fingerprint([settings["import"]["version"],
            {key: settings["local"][key] for key in ("input_root", "assets_root", "server_project_root", "database_relative_path")}])

    @property
    def readonly(self):
        return bool(self.task and self.task.get("archive"))

    def render(self):
        self.tree.delete(*self.tree.get_children())
        self.purchase_tree.delete(*self.purchase_tree.get_children())
        self.purchase_rows = {}
        self.purchase_row_ids = {}
        self.purchase_analysis = None
        self.commit_button.configure(state="disabled")
        editable = bool(self.task) and not self.readonly and not self.app.busy
        for button in (*self.edit_buttons, self.purchase_edit_button, self.execute_button):
            button.configure(state="normal" if editable else "disabled")
        if self.task and self.task.get("followup"):
            self.source_button.configure(state="disabled")
        self.followup_button.configure(state="normal" if self.readonly and not self.app.busy else "disabled")
        if self.readonly:
            self.cancel_editor()
            if self.purchase_editor:
                self.purchase_editor["window"].destroy()
                self.purchase_editor = None
        if not self.task:
            self.purchase_summary.set("01 后即可分析，无需连接服务器。")
            self.purchase_details.set("候选由审核总表推导；选择工作区或完成 01 后可预览。")
            return
        update_purchase_candidates(self.task)
        for row in self.task["songs"]:
            sid = row["id"]
            values = [sid, english_name(row)]
            for c in range(5):
                value = parse_constant(self.task["constants"].get(sid, {}).get(str(c)))
                values.append("-1" if c not in self.task["actual"][sid] or value == -1
                              else "" if value is None else f"{value:.1f}")
            self.tree.insert("", "end", iid=sid, values=values)
        source = (self.task.get("source") or {}).get("path")
        state = (" · 已归档，只读；修改请创建后续任务。" if self.readonly else
                 " · 后续任务：修改后执行，成功后自动归档。" if self.task.get("followup") else
                 " · 03 完成，待确认提交。" if task_complete(self.task) and self.path and self.path.name == "server-task.json" else
                 " · 独立任务已完成。" if task_complete(self.task) else " · 双击录入；0 / -1 不计评分；空难度 -1。")
        if self.readonly and self.task["archive"].get("modified_after_commit"):
            state += "旧版归档有提交后的编辑痕迹，现有内容保留。"
        self.hint.set(("审核表：" + Path(source).name if source else "尚未选择最终审核总表") +
                      f" · 未填 {len(missing_constants(self.task))} 项 · 购买候选 {len(self.task.get('purchase_candidates', []))} 项" +
                      state)
        if self.path and self.path.name == "server-task.json" and task_complete(self.task) and not self.app.busy:
            self.commit_button.configure(state="normal")
        self.render_purchases()

    def render_purchases(self):
        analysis = self.purchase_analysis = deepcopy(self.task["purchase_preview"])
        skipped = purchase_skips(analysis)
        confirmed = bool(skipped) and self.task.get("purchase_skip_confirmation") == purchase_skip_signature(self.task)
        skip_label = "已确认跳过" if confirmed else "待跳过"
        self.purchase_summary.set(f"候选 {len(analysis['purchases'])} · 免费 {len(analysis['free_songs'])} · {skip_label} {len(skipped)}")
        for entry in analysis["purchases"]:
            spec, item = entry["spec"], entry["item"]
            ids = ", ".join(song["id"] for song in entry["songs"])
            state = "归档候选" if self.readonly else "待远端核对" if self.task["options"]["purchases"] else "本次已跳过"
            iid = self.purchase_tree.insert("", "end", values=(spec["name"], "单曲" if spec["kind"] == "single" else "曲包",
                spec["price"], spec["orig_price"], item["type"] + "/" + item["item_id"], ids, state))
            self.purchase_rows[iid] = (f"购买项 {spec['name']}；关联 {item['type']}/{item['item_id']}，数量 1，物品可用。"
                f"无折扣、不允许兑换券。引用曲目：{ids}。若服务器已有此购买项，保留其价格与全部物品配置。")
            self.purchase_row_ids[iid] = [song["id"] for song in entry["songs"]]
        for song in analysis["free_songs"]:
            iid = self.purchase_tree.insert("", "end", values=("—", "免费", "—", "—", "—", song["id"], "无需创建"))
            self.purchase_rows[iid] = f"{song['id']}：purchase 为空或 JSON null，不创建购买项。"
            self.purchase_row_ids[iid] = [song["id"]]
        for song in analysis["invalid_songs"]:
            iid = self.purchase_tree.insert("", "end", values=(str(song["purchase"]), "未推断", "—", "—", "—", song["id"], "已确认跳过" if confirmed else "确认后跳过"))
            self.purchase_rows[iid] = f"{song['error']} 当前 set={song['set']!r}，purchase={song['purchase']!r}。可双击编辑本批安排，或执行 03 时确认跳过。"
            self.purchase_row_ids[iid] = [song["id"]]
        for song in analysis["skipped_songs"]:
            iid = self.purchase_tree.insert("", "end", values=(str(song["purchase"] or "—"), "跳过", "—", "—", "—", song["id"], "已确认跳过" if confirmed else "人工跳过"))
            self.purchase_rows[iid] = f"{song['id']}：人工选择跳过购买项；不影响本曲定数或其他 03 操作，执行前仍会列出确认。"
            self.purchase_row_ids[iid] = [song["id"]]
        self.purchase_details.set("归档只读，可查看或导出；修改购买安排请创建后续任务。" if self.readonly else
            "双击或编辑所选可修改本批购买安排，不改 songlist。已有购买项保留，执行前确认待跳过项。")

    def show_purchase_detail(self):
        selected = self.purchase_tree.selection()
        if selected and selected[0] in self.purchase_rows:
            self.purchase_details.set(self.purchase_rows[selected[0]])

    def edit_purchase(self, event=None):
        if self.app.busy or not self.task or self.readonly or not self.commit_editor():
            return
        if self.purchase_editor and self.purchase_editor["window"].winfo_exists():
            self.purchase_editor["window"].lift()
            return
        if event is not None:
            selected = self.purchase_tree.identify_row(event.y)
            if not selected:
                return
            self.purchase_tree.selection_set(selected)
        selected = self.purchase_tree.selection()
        if not selected or selected[0] not in self.purchase_row_ids:
            messagebox.showinfo("选择购买项", "请先选择一条候选、免费或待跳过记录。", parent=self.app.root)
            return
        ids = self.purchase_row_ids[selected[0]]
        source_row = next(row for row in self.task["songs"] if row["id"] == ids[0])
        overrides = self.task.get("purchase_overrides", {})
        override = next((overrides[sid] for sid in ids if sid in overrides), {})
        entry = next((entry for entry in self.task["purchase_preview"]["purchases"]
                      if {song["id"] for song in entry["songs"]} == set(ids)), None)
        spec = entry["spec"] if entry else {"name": source_row.get("purchase") if portable_id(source_row.get("purchase")) else ids[0],
                                          "kind": "single", "price": 100}
        window = tk.Toplevel(self.app.root)
        window.title("编辑本批购买项")
        window.transient(self.app.root)
        body = ttk.Frame(window, padding=self.app.px(14))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="应用到曲目：" + ", ".join(ids), wraplength=self.app.px(500)).grid(row=0, column=0, columnspan=2, sticky="w", pady=5)
        labels = {"auto": "自动推导", "custom": "自定义", "skip": "跳过购买项"}
        mode = tk.StringVar(value=labels[override.get("mode", "auto")])
        name = tk.StringVar(value=spec["name"])
        kind = tk.StringVar(value="单曲" if spec["kind"] == "single" else "曲包")
        price = tk.StringVar(value=str(spec["price"]))
        ttk.Label(body, text="处理方式").grid(row=1, column=0, sticky="w", pady=5)
        selector = ttk.Combobox(body, textvariable=mode, values=list(labels.values()), state="readonly")
        selector.grid(row=1, column=1, sticky="ew", pady=5)
        fields = []
        for index, (label, variable) in enumerate((("购买项 ID", name), ("类别", kind), ("价格", price)), 2):
            ttk.Label(body, text=label).grid(row=index, column=0, sticky="w", padx=(0, 12), pady=5)
            field = (ttk.Combobox(body, textvariable=variable, values=("单曲", "曲包"), state="readonly")
                     if variable is kind else ttk.Entry(body, textvariable=variable))
            field.grid(row=index, column=1, sticky="ew", pady=5)
            fields.append(field)
        def mode_changed(*_):
            for index, field in enumerate(fields):
                field.configure(state=("readonly" if index == 1 else "normal") if mode.get() == labels["custom"] else "disabled")
        mode.trace_add("write", mode_changed)
        mode_changed()
        ttk.Label(body, text="仅保存本批 03 任务，不改审核总表、不立即写服务器。购买项 ID 请与客户端引用核对。\n"
            "价格与原价相同，无折扣、不允许兑换券；关联同 ID 的物品，数量 1。已有远端项仍保留。",
            style="Muted.TLabel", wraplength=self.app.px(500)).grid(row=5, column=0, columnspan=2, sticky="w", pady=10)
        revision = fingerprint([self.task["id"], self.task["revision"], self.task.get("source"),
                                self.task["songs"], self.task.get("purchase_overrides", {})])
        def dismiss():
            self.purchase_editor = None
            window.destroy()
        def apply():
            if self.app.busy:
                return
            try:
                current = fingerprint([self.task["id"], self.task["revision"], self.task.get("source"),
                                       self.task["songs"], self.task.get("purchase_overrides", {})])
                if current != revision:
                    raise PlanError("购买清单已改变，请关闭此窗口后重新选择。")
                previous = deepcopy(self.task)
                set_purchase_override(self.task, ids, next(key for key, label in labels.items() if label == mode.get()),
                                      name=name.get(), kind="single" if kind.get() == "单曲" else "pack", price=price.get())
                if not self.save():
                    self.task = previous
                    return
                self.render()
                dismiss()
            except (ValueError, OSError) as error:
                messagebox.showerror("无法保存购买项", str(error), parent=window)
        actions = ttk.Frame(body)
        actions.grid(row=6, column=0, columnspan=2, sticky="e")
        ttk.Button(actions, text="取消", command=dismiss).pack(side="left", padx=6)
        ttk.Button(actions, text="保存本批修改", command=apply).pack(side="left")
        body.columnconfigure(1, weight=1)
        self.purchase_editor = {"window": window, "mode": mode, "name": name, "kind": kind, "price": price, "apply": apply}
        window.protocol("WM_DELETE_WINDOW", dismiss)
        window.bind("<Escape>", lambda event: dismiss())
        window.update_idletasks()
        window.grab_set()

    def refresh_analysis(self):
        if self.app.busy or not self.task or not self.commit_editor():
            return False
        try:
            source = (self.task.get("source") or {}).get("path")
            if source and not self.readonly and not self.task.get("followup"):
                self.task = read_reviewed(self.task, source)
            self.render()
            return True
        except (ValueError, OSError) as error:
            self.purchase_tree.delete(*self.purchase_tree.get_children())
            self.purchase_analysis = None
            self.purchase_summary.set("分析未完成，请检查总表")
            messagebox.showerror("无法分析购买项", str(error), parent=self.app.root)
            return False

    def export_analysis(self):
        if not self.refresh_analysis():
            return
        selected = filedialog.asksaveasfilename(parent=self.app.root, title="导出购买项审核清单",
            initialfile="purchase-preview.json", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if selected:
            try:
                document = {"task_id": self.task["id"], "source": self.task.get("source"),
                    "purchases_enabled": self.task["options"]["purchases"],
                    "purchase_overrides": self.task.get("purchase_overrides", {}), **self.purchase_analysis}
                Path(selected).write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            except (ValueError, OSError) as error:
                messagebox.showerror("无法导出清单", str(error), parent=self.app.root)

    def save(self):
        if self.readonly:
            return True  # Closing/switching views must not rewrite the archive.
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
        if self.app.busy or not self.task or self.readonly or not self.commit_editor():
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
        if self.readonly:
            self.cancel_editor()
            return True
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
        if self.app.busy or not self.task or self.readonly or self.task.get("followup") or not self.commit_editor():
            return
        selected = filedialog.askopenfilename(parent=self.app.root, title="选择已经审核并用于上传的最终 songlist",
                                              filetypes=[("Songlist", "*")])
        if selected:
            try:
                if self.path and self.path.name == "server-task.json" and (self.path.parent / "receipt.json").is_file():
                    raise PlanError("02 已固定本工作区的审核总表；更正资源需另建工作区，不能替换已上传的总表。")
                updated = read_reviewed(self.task, selected)
                self.task = updated
                self.save()
                self.render()
            except (ValueError, OSError) as error:
                messagebox.showerror("无法载入审核总表", str(error), parent=self.app.root)

    def open_task(self):
        if self.app.busy or not self.commit_editor():
            return
        selected = filedialog.askopenfilename(parent=self.app.root, title="载入 03 任务或查看归档",
                                              filetypes=[("03 任务", "*.json")])
        if selected:
            try:
                if Path(selected).name == "server-task.json" and (Path(selected).parent / "publish.json").is_file():
                    self.app.resume_workspace(Path(selected).parent)
                    self.app.results.select(self.frame)
                    return
                self.task, self.path = load_task(selected), Path(selected).resolve()
                self.context_signature, self.context_expired = None, False
                self.render()
            except (ValueError, OSError, KeyError, TypeError) as error:
                messagebox.showerror("无法载入 03 任务", str(error), parent=self.app.root)

    def options(self):
        if self.app.busy or not self.task or self.readonly or not self.commit_editor():
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
        ttk.Label(body, text="购买默认无折扣、不允许兑换券；已有购买项保留。\n定数接受正数、0 或 -1，整数或一位小数；不从 rating 推断。\n0 / -1 不计评分，留空保留已有值，新建暂用 -1。\n界面/API 使用真实定数；数据库存十倍整数（-1 → -10）。",
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

    def new_followup(self):
        if self.app.busy or not self.readonly or not self.path:
            return
        try:
            root = self.path.parent.parent if self.path.parent.name == ".server-tasks" else Path(self.app._collect()["local"]["release_root"])
            self.path, self.task = create_followup(self.path, root)
            self.context_signature, self.context_expired = None, False
            self.render()
            self.app.summary.set("已创建后续 03 任务 · 修改后再执行")
            self.app.status.set("原归档保留；默认不刷新资源。后续任务执行成功后自动归档。")
        except (ValueError, OSError) as error:
            messagebox.showerror("无法创建后续任务", str(error), parent=self.app.root)

    def execute(self):
        if self.readonly:
            messagebox.showinfo("归档只读", "请点击“创建后续任务…”，再修改或执行。", parent=self.app.root)
            return
        if self.app.busy or not self.task or not self.commit_editor():
            return
        try:
            if self.task.get("input_errors"):
                raise PlanError("原批次仍有 ERROR，请修复并重新检查后再执行 03。")
            if self.path and self.path.name == "server-task.json":
                verify_workspace_publication(self.path.parent.parent, self.path.parent, self.task)
            if not self.task.get("source"):
                self.choose_source()
                if not self.task.get("source"):
                    return
            source = Path(self.task["source"]["path"])
            if source.is_file() and not self.task.get("followup"):
                self.task = read_reviewed(self.task, source)
                self.render()
            elif not self.task.get("publication"):
                raise PlanError("审核总表已不存在，且尚未确认上传。")
            settings = self.app._collect()
            if self.context_expired or self.context_signature and self.binding(settings) != self.context_signature:
                raise PlanError("曲目来源或资源配置已改变，请重新检查后再执行 03。")
            if self.path.name == "server-task.json":
                verify_workspace_publication(self.path.parent.parent, self.path.parent, self.task)
                if any(self.task["options"].values()):
                    verify_workspace_api(self.path.parent.parent, self.path.parent, settings)
            analysis = analyze_purchases(self.task) if self.task["options"]["purchases"] else None
            specs = [entry["spec"] for entry in analysis["purchases"]] if analysis else []
            skipped = purchase_skips(analysis) if analysis else []
            missing = missing_constants(self.task)
            details = ["将调用：" + settings["api"]["base_url"],
                       "审核表：" + self.task["source"]["path"],
                       "请确认本批资源已上传，且使用的正是当前审核 songlist。",
                       "购买检查（已有项保留）：" + (", ".join(s["name"] + f" ({s['price']})" for s in specs) or "无")]
            if self.task.get("followup"):
                details[1:3] = ["后续任务来源：" + self.task["followup"]["source_task_id"],
                               "沿用原上传确认及归档元数据；本次调整服务端配置，成功后自动归档。"]
            if skipped:
                details.append("以下购买项将跳过，songlist 原值保留：\n" + "\n".join(
                    f"{song['id']}（purchase={song['purchase']!r}）：{song['reason']}" for song in skipped) +
                    "\n确认后仅跳过这些购买项，其余合法购买项、定数及已选择的刷新操作继续执行。")
            if missing:
                details.append("以下真实谱面尚未填写定数：\n" + ", ".join(missing) +
                               "\n继续将保留已有值；新增记录的未填难度暂用 -1。")
            if any(p.get("state") in {"running", "uncertain"} for p in self.task["progress"].values()):
                details.append("此前有请求结果未确认。请确认服务端处理已结束后继续；程序将先核对已有数据。")
            if not messagebox.askyesno("确认执行可选的 03 操作", "\n\n".join(details) + "\n\n继续执行？",
                                       parent=self.app.root, default="no"):
                return
            if skipped:
                confirm_purchase_skips(self.task)
            publication = self.task.get("publication") or {}
            if not publication or (not self.task.get("followup") and publication.get("kind") == "manual_confirmed" and self.task.get("state") == "complete"):
                confirm_uploaded(self.task)
            if not self.save():
                return
            if self.path.name == "server-task.json" and any(self.task["options"].values()):
                bind_workspace_api(self.path.parent.parent, self.path.parent, settings)
            self.set_busy(True)
            self.app._set_details("03 服务端操作", [str(self.path)])
            self.app.results.select(2)
            Thread(target=self.worker, args=(settings, self.path), daemon=True).start()
            self.poll_id = self.app.root.after(80, self.poll)
        except (ValueError, OSError) as error:
            messagebox.showerror("无法执行 03", str(error), parent=self.app.root)

    def set_busy(self, busy):
        self.app.busy = busy
        for button in (self.app.check_button, self.app.preprocess_button, self.app.workspace_button, self.app.new_batch_button, self.execute_button):
            button.configure(state="disabled" if busy else "normal")
        for button in self.app.stage_buttons:
            button.configure(state="disabled" if busy else "normal")
        if busy:
            self.app.progress.start(12)
            self.app.status.set("03 正在执行；每一步完成后保存进度。")
        else:
            self.app.progress.stop()
        if busy:
            self.commit_button.configure(state="disabled")
        else:
            self.render()

    def worker(self, settings, path):
        task = None
        try:
            if path.name == "server-task.json":
                with workspace_lock(path.parent.parent, path.parent):
                    task = self.run_locked(settings, path)
            else:
                task = self.run_locked(settings, path)
            self.queue.put(("done", (task, None)))
        except Exception as error:
            try:
                task = load_task(path)
            except (OSError, ValueError):
                pass
            self.queue.put(("done", (task, str(error))))

    def run_locked(self, settings, path):
        with task_lock(path):
            task = load_task(path)
            if path.name == "server-task.json":
                verify_workspace_publication(path.parent.parent, path.parent, task)
                if any(task["options"].values()):
                    verify_workspace_api(path.parent.parent, path.parent, settings)
            if not any(task["options"].values()):
                finish_task(task, skipped=True)
                save_task(path, task, locked=True)
            else:
                run_task(task, ApiClient.from_settings(settings), persist=lambda task: save_task(path, task, locked=True),
                         on_log=lambda level, message: self.queue.put(("log", (level, message))), allow_missing=True)
            if task.get("followup") and task_complete(task):
                archive_followup(path, task, locked=True)
            return task

    def commit(self):
        if self.app.busy or not self.task or not self.commit_editor():
            return
        try:
            if not self.path or self.path.name != "server-task.json":
                raise PlanError("独立任务没有待提交工作区。")
            if not self.save():
                return
            batch = self.path.parent
            verify_workspace_publication(batch.parent, batch, self.task)
            if not task_complete(self.task):
                raise PlanError("03 仍需执行，或定数 / 选项已改变。")
            note = "全部 API 操作已明确跳过。" if self.task["state"] == "skipped" else "本次选择的 03 操作已成功完成。"
            omitted = [label for key, label in zip(OPTIONS, ("刷新资源", "购买项", "曲目定数", "刷新评分")) if not self.task["options"][key]]
            if omitted:
                note += "\n已跳过：" + "、".join(omitted) + "。"
            if self.task.get("endpoint"):
                note += "\n目标 API：" + self.task["endpoint"]
            if not messagebox.askyesno("确认提交本工作区", note + "\n\n请确认本批文件、购买项和定数均符合预期。\n\n" +
                str(batch) + "\n\n提交后清理整个临时工作区，保留完成记录与 03 任务归档。确认提交？",
                parent=self.app.root, default="no"):
                return
            self.set_busy(True)
            Thread(target=self.commit_worker, args=(batch, self.task["revision"]), daemon=True).start()
            self.poll_id = self.app.root.after(80, self.poll)
        except (ValueError, OSError) as error:
            messagebox.showerror("无法提交工作区", str(error), parent=self.app.root)

    def commit_worker(self, batch, revision):
        try:
            history = commit_workspace(batch.parent, batch, expected_revision=revision)
            archive = batch.parent / ".server-tasks" / (batch.name + ".json")
            self.queue.put(("committed", (batch, history, archive, None)))
        except Exception as error:
            self.queue.put(("committed", (batch, None, None, str(error))))

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
            elif kind == "committed":
                batch, history, archive, error = value
                if not error:
                    self.path, self.task = archive, load_task(archive)
                    self.app.workspace_committed(batch, history)
                self.set_busy(False)
                if error:
                    self.app._append_log({"level": "error", "message": error, "song_id": None})
                    self.app.summary.set("提交未完成 · 工作区保留供继续")
                return
            else:
                task, error = value
                if task:
                    self.task = task
                    self.render()
                self.set_busy(False)
                if error:
                    self.app._append_log({"level": "error", "message": error, "song_id": None})
                managed = self.path and self.path.name == "server-task.json"
                self.app.summary.set("03 未完成 · 进度保留" if error else "后续 03 已完成 · 已归档，只读" if self.readonly else
                                     "03 已完成 · 工作区等待确认提交" if managed else "独立 03 任务已完成")
                self.app.status.set("查看 ERROR 后，可载入同一任务继续。" if error else
                    "归档保留；再次修改请创建后续任务。" if self.readonly else
                    "请验收结果，确认无误后点击“提交并清理…”。" if managed else "任务进度已保存。")
                return
        self.poll_id = self.app.root.after(20, self.poll)

    def close(self):
        if self.poll_id:
            self.app.root.after_cancel(self.poll_id)
        self.cancel_editor()
        if self.purchase_editor:
            self.purchase_editor["window"].destroy()
            self.purchase_editor = None
