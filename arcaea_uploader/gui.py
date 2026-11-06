"""Compact native desktop interface for Arcaea-Uploader."""

from __future__ import annotations

from copy import deepcopy
from importlib.resources import files as package_files
import json
import os
from pathlib import Path
from queue import Empty, Queue
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .planner import build_plan, resolve_config
from .preprocess import preprocess_settings
from .import_rules import require_version
from .backgrounds import normalize_pairs, pair_lookup
from .diagnostics import summarize
from .field_help import FIELD_HELP
from .workspaces import load_workspace
from .bundle_versions import latest_record, read_history, resolve_bundle_versions, validate_version
from .settings import (
    default_config_path, default_settings, effective_settings, load_settings,
    normalize_settings, save_settings,
)


# All configuration lives on one scrollable page. The type drives the picker.
FIELDS = (
    ("本次导入", "import", (("version", "本次导入 version", "text"),)),
    ("Bundle 应用版本", "bundle", (
        ("follow_app_version", "沿用当前应用版本", "check"),
        ("app_version", "应用版本", "app_version"),
    )),
    ("本地资源", "local", (
        ("server_project_root", "Arcaea-server 项目", "dir"),
        ("assets_root", "待打包资源（active）", "dir"),
        ("input_root", "待更新曲目目录", "dir"),
        ("release_root", "发布工作区", "dir"),
    )),
    ("VPS · SSH", "ssh", (
        ("host", "服务器 IP / 主机名", "text"),
        ("port", "SSH 端口", "text"),
        ("username", "SSH 用户名", "text"),
        ("server_project_root", "服务器上的项目路径", "text"),
        ("private_key_path", "SSH 私钥", "file"),
        ("known_hosts_path", "已知主机文件", "file"),
    )),
    ("服务端管理 API", "api", (
        ("base_url", "管理 API 地址", "text"),
        ("token_env", "Token 环境变量名", "text"),
    )),
    ("工具与目录细节", "local", (
        ("database_relative_path", "本地 database 相对路径", "text"),
        ("placeholder_aff", "占位谱面（可选覆盖）", "file"),
        ("ffmpeg_path", "FFmpeg（留空自动查找）", "file"),
    )),
    ("远端目录细节", "ssh", (
        ("database_relative_path", "远端 database 相对路径", "text"),
    )),
)


def inspect_settings(data: dict, origin: Path, preprocessed: dict | None = None) -> dict:
    """Bridge unsaved GUI values to the existing read-only planner."""
    config = effective_settings(data, origin)
    if preprocessed:
        config["local"]["input_root"] = preprocessed["normalized_root"]
    # The planner accepts a config file; a temporary one avoids changing user settings.
    with TemporaryDirectory(prefix="arcaea-preflight-") as temp:
        path = Path(temp) / "config.json"
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        plan = build_plan(resolve_config(path))
    if preprocessed:
        plan["preprocessing_report"] = preprocessed["report_file"]
        plan["diagnostics"] = preprocessed.get("diagnostics", []) + plan.get("diagnostics", [])
        summarize(plan)
        plan["valid"] = not plan["errors"]
    return plan


class UploaderApp:
    def __init__(self, root: tk.Tk, config_path: Path | None = None):
        self.root = root
        self.config_path = (config_path or default_config_path()).resolve()
        self.settings = default_settings()
        self.result = None
        self.result_signature = None
        self.preprocessed = None
        self.preprocessed_signature = None
        self.job_kind = "check"
        self.log_items = []
        self.busy = False
        self.closed = False
        self.poll_id = None
        self.queue = Queue()
        self.variables: dict[tuple[str, str], tk.Variable] = {}
        self._bundle_history_cache = None
        self.app_version_entries = []
        self.help_buttons = {}
        self.help_window = None
        self.help_text = None
        self.workspace_manager = None
        self.error_on_load = None
        if self.config_path.is_file():
            try:
                self.settings = load_settings(self.config_path)
            except (ValueError, OSError) as exc:
                self.error_on_load = str(exc)
        try:
            self.settings = effective_settings(self.settings, self.config_path.parent)
        except (ValueError, OSError) as exc:
            self.error_on_load = str(exc)
        self._style()
        self._build()
        self._fill(self.settings)
        # Font and theme metrics vary by DPI; retain room for the result rows.
        self.root.update_idletasks()
        min_width, min_height = self.root.minsize()
        required = self.root.winfo_reqheight() - self.results.winfo_reqheight() + self.px(220)
        self.root.minsize(min_width, min(max(min_height, required),
                                        self.root.winfo_screenheight() - self.px(80)))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if self.error_on_load:
            self._set_details("配置读取失败", [self.error_on_load])
            self.status.set("配置未载入，请在配置页检查文件。")
            self.pages.select(self.config_page)

    def _style(self):
        self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96)
        self.px = lambda value: round(value * self.scale)
        self.root.title("Arcaea-Uploader")
        width = min(self.px(920), self.root.winfo_screenwidth() - self.px(40))
        height = min(self.px(700), self.root.winfo_screenheight() - self.px(80))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(self.px(780), width), min(self.px(620), height))
        self.root.configure(bg="#f3f5f8")
        self.style = ttk.Style(self.root)
        self.style.theme_use("clam")
        self.style.configure(".", font=("Microsoft YaHei UI", 10), background="#f3f5f8", foreground="#1e293b")
        self.style.configure("TFrame", background="#f3f5f8")
        self.style.configure("Card.TFrame", background="#ffffff")
        self.style.configure("TLabel", background="#f3f5f8")
        self.style.configure("Muted.TLabel", foreground="#64748b")
        self.style.configure("Title.TLabel", font=("Segoe UI", 21, "bold"))
        self.style.configure("Card.TLabel", background="#ffffff")
        self.style.configure("CardTitle.TLabel", background="#ffffff", font=("Microsoft YaHei UI", 11, "bold"))
        self.style.configure("CardMuted.TLabel", background="#ffffff", foreground="#64748b")
        self.style.configure("Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        self.style.configure("TButton", padding=(self.px(12), self.px(6)))
        self.style.configure("Help.TButton", foreground="#2563eb", padding=self.px(2),
                             font=("Segoe UI", 12), anchor="center")
        self.style.map("TButton", foreground=[("disabled", "#8491a4")])
        self.style.configure("Accent.TButton", background="#2563eb", foreground="#ffffff", padding=(self.px(18), self.px(7)))
        self.style.map("Accent.TButton", background=[("disabled", "#cbd5e1"), ("active", "#1d4ed8")],
                       foreground=[("disabled", "#64748b"), ("!disabled", "#ffffff")])
        self.style.configure("TNotebook", borderwidth=0)
        self.style.configure("TNotebook.Tab", padding=(self.px(18), self.px(7)))
        self.style.map("TNotebook.Tab", background=[("selected", "#ffffff")])
        self.style.configure("Treeview", rowheight=self.px(28), background="#ffffff", fieldbackground="#ffffff")
        self.style.configure("Treeview.Heading", padding=self.px(5), font=("Microsoft YaHei UI", 9, "bold"))
        self.style.configure("TEntry", padding=self.px(5), fieldbackground="#ffffff")
        self.style.configure("TCheckbutton", indicatorsize=self.px(14), indicatormargin=self.px(3))
        self.style.map("TCheckbutton", indicatorbackground=[("selected", "#2563eb"), ("!selected", "#ffffff")],
                       indicatorforeground=[("selected", "#ffffff"), ("!selected", "#1e293b")])
        self.style.configure("TProgressbar", background="#2563eb")

    def _build(self):
        shell = ttk.Frame(self.root, padding=tuple(self.px(x) for x in (22, 14, 22, 10)))
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(1, weight=1)
        header = ttk.Frame(shell)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(header, text="Arcaea-Uploader", style="Title.TLabel").pack(side="left")
        ttk.Button(header, text="关于与授权", command=self.show_licenses).pack(side="right")
        ttk.Label(header, text="桌面预览版 · " + __version__, style="Muted.TLabel").pack(side="right", padx=12, pady=9)
        self.pages = ttk.Notebook(shell)
        self.pages.grid(row=1, column=0, sticky="nsew")
        self.home_page = ttk.Frame(self.pages, padding=(self.px(16), self.px(14)))
        self.config_page = ttk.Frame(self.pages, padding=(self.px(16), self.px(14)))
        self.pages.add(self.home_page, text="更新")
        self.pages.add(self.config_page, text="配置")
        self.status = tk.StringVar(value="01/02 待接入 · 预处理、检查与可选 03 已可用 · ⓘ 查看说明。")
        self._home()
        self._config()
        footer = ttk.Frame(shell)
        footer.grid(row=2, column=0, sticky="ew", pady=(9, 0))
        ttk.Label(footer, textvariable=self.status, style="Muted.TLabel", wraplength=self.px(850)).pack(anchor="w")

    def _home(self):
        page = self.home_page
        heading = ttk.Frame(page)
        heading.pack(fill="x")
        ttk.Label(heading, text="本次更新", style="Section.TLabel").pack(side="left")
        self._help_button(heading, "workflow", "stages").pack(side="left", padx=(4, 8))
        ttk.Label(heading, text="01 → 保存审核结果 → 02 → 可选 03", style="Muted.TLabel").pack(side="left")
        directory = ttk.Frame(page)
        directory.pack(fill="x", pady=(6, 10))
        self.input_var = tk.StringVar()
        self.variables[("local", "input_root")] = self.input_var
        self.input_entry = ttk.Entry(directory, textvariable=self.input_var)
        self.input_entry.pack(side="left", fill="x", expand=True)
        self._help_button(directory, "local", "input_root").pack(side="left", padx=(4, 0))
        ttk.Button(directory, text="选择目录…", command=lambda: self._browse("local", "input_root", "dir")).pack(side="left", padx=(8, 0))
        self.preprocess_button = ttk.Button(directory, text="预处理曲目", command=self.start_preprocess)
        self.preprocess_button.pack(side="left", padx=(8, 0))
        self.check_button = ttk.Button(directory, text="检查资源", command=self.start_check)
        self.check_button.pack(side="left", padx=(8, 0))
        version_row = ttk.Frame(page)
        version_row.pack(fill="x", pady=(0, 10))
        ttk.Label(version_row, text="本次导入 version").pack(side="left")
        self._help_button(version_row, "import", "version").pack(side="left", padx=(4, 0))
        self.import_version_var = tk.StringVar()
        self.variables[("import", "version")] = self.import_version_var
        ttk.Entry(version_row, textvariable=self.import_version_var, width=18).pack(side="left", padx=8)
        ttk.Label(version_row, text="覆盖本次曲目及各难度的 version", style="Muted.TLabel").pack(side="left")
        app_row = ttk.Frame(page)
        app_row.pack(fill="x", pady=(0, 10))
        self.follow_app_version_var = tk.BooleanVar(value=True)
        self.manual_app_version_var = tk.StringVar()
        self.auto_app_version_var = tk.StringVar()
        self.bundle_preview = tk.StringVar()
        self.variables[("bundle", "follow_app_version")] = self.follow_app_version_var
        self.variables[("bundle", "app_version")] = self.manual_app_version_var
        self.follow_app_checkbox = ttk.Checkbutton(app_row, text="沿用当前应用版本", variable=self.follow_app_version_var)
        self.follow_app_checkbox.pack(side="left")
        self._help_button(app_row, "bundle", "follow_app_version").pack(side="left", padx=(4, 0))
        entry = ttk.Entry(app_row, textvariable=self.auto_app_version_var, width=18, state="disabled")
        entry.pack(side="left", padx=8)
        self.app_version_entries.append(entry)
        ttk.Label(app_row, textvariable=self.bundle_preview, style="Muted.TLabel", wraplength=self.px(420)).pack(side="left")
        stages = ttk.Frame(page)
        stages.pack(fill="x", pady=(0, 10))
        stages.columnconfigure((0, 1, 2), weight=1, uniform="stages")
        self.stage_buttons = []
        for column, title, detail, button in (
            (0, "01  准备与审核", "自动预处理、检查并准备资源，打开 songlist 审核。", "准备并审核"),
            (1, "02  打包与上传", "打包并同步本地、VPS；完成文件核验。", "打包并上传"),
            (2, "03  服务端操作", "刷新资源、补购买项和定数；可选执行。", "服务端操作"),
        ):
            card = ttk.Frame(stages, style="Card.TFrame", padding=self.px(12))
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 5) if column == 0 else (5, 0))
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
            description = ttk.Label(card, text=detail, style="CardMuted.TLabel", wraplength=self.px(340))
            description.pack(fill="x", pady=(4, 9))
            card.bind("<Configure>", lambda event, label=description: label.configure(
                wraplength=max(self.px(40), event.width - self.px(24))))
            row = ttk.Frame(card, style="Card.TFrame")
            row.pack(side="bottom", fill="x")
            action = ttk.Button(row, text=button, state="disabled" if column < 2 else "normal",
                                command=(lambda: self.results.select(self.server_ops.frame)) if column == 2 else None)
            action.pack(side="left")
            ttk.Label(row, text="待接入" if column < 2 else "可选", style="CardMuted.TLabel").pack(side="left", padx=6)
            self.stage_buttons.append(action)
        self.summary = tk.StringVar(value="辅助操作：预处理曲目 / 检查资源 · 原始目录保持不变")
        ttk.Label(page, textvariable=self.summary, style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        self.progress = ttk.Progressbar(page, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 10))
        # Reserve the footer before allocating the expandable results area.
        toolbar = ttk.Frame(page)
        toolbar.pack(side="bottom", fill="x", pady=(10, 0))
        self.export_button = ttk.Button(toolbar, text="导出检查报告…", state="disabled", command=self.export_report)
        self.export_button.pack(side="left")
        self.workspace_button = ttk.Button(toolbar, text="工作区管理…", command=self.manage_workspaces)
        self.workspace_button.pack(side="left", padx=8)
        ttk.Button(toolbar, text="打开配置页", command=lambda: self.pages.select(self.config_page)).pack(side="right")
        self.results = ttk.Notebook(page)
        self.results.pack(fill="both", expand=True)
        self.song_tree = self._tree("曲目", ("id", "kind", "idx"), ("曲目 ID", "更新类型", "idx"), (390, 130, 90))
        self.file_tree = self._tree("资源变更", ("area", "path", "action"), ("目标", "相对路径", "处理"), (100, 420, 120))
        detail_frame = ttk.Frame(self.results)
        self.results.add(detail_frame, text="信息日志")
        self.log_summary = tk.StringVar(value="INFO 0 · WARNING 0 · ERROR 0")
        ttk.Label(detail_frame, textvariable=self.log_summary, style="Muted.TLabel").pack(anchor="w", padx=10, pady=4)
        self.details = tk.Text(detail_frame, wrap="word", relief="flat", padx=12, pady=9,
                               background="#ffffff", foreground="#334155", font=("Microsoft YaHei UI", 10), state="disabled")
        scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self.details.yview)
        self.details.configure(yscrollcommand=scroll.set)
        self.details.tag_configure("info", foreground="#334155")
        self.details.tag_configure("warning", foreground="#996300")
        self.details.tag_configure("error", foreground="#b42318")
        scroll.pack(side="right", fill="y")
        self.details.pack(fill="both", expand=True)
        from .server_tasks_gui import ServerOperationsPanel
        self.server_ops = ServerOperationsPanel(self)
        self._set_details("检查结果", [
            "预处理和检查是可单独运行的辅助操作；当前预览版尚未接入 01、02。",
            "接入后可直接执行 01，内部自动预处理、检查并准备资源。审核保存工作区 songlist 后，再执行 02。",
            "03 可选执行：刷新资源、购买项、人工定数和成绩评分；也支持已手动上传的批次。",
        ])

    def _tree(self, title, columns, headings, widths):
        frame = ttk.Frame(self.results)
        self.results.add(frame, text=title)
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for name, label, width in zip(columns, headings, widths):
            tree.heading(name, text=label)
            tree.column(name, width=self.px(width), minwidth=self.px(65), stretch=name in {"id", "path"})
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        scroll.pack(side="right", fill="y")
        horizontal.pack(side="bottom", fill="x")
        tree.pack(fill="both", expand=True)
        return tree

    def _config(self):
        self.config_page.columnconfigure(0, weight=1)
        self.config_page.rowconfigure(2, weight=1)
        ttk.Label(self.config_page, text="路径和连接设置", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self.config_page, text="点击各项旁的 ⓘ 查看用途和填写示例；SSH 传文件，API 刷新资源索引。",
                  style="Muted.TLabel", wraplength=self.px(790)).grid(row=1, column=0, sticky="w", pady=(4, 9))
        body = ttk.Frame(self.config_page)
        body.grid(row=2, column=0, sticky="nsew")
        self.config_canvas = tk.Canvas(body, bg="#f3f5f8", highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.config_canvas.yview)
        self.config_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.config_canvas.pack(fill="both", expand=True)
        self.form = ttk.Frame(self.config_canvas, padding=(0, 0, 10, 0))
        item = self.config_canvas.create_window((0, 0), window=self.form, anchor="nw")
        self.form.bind("<Configure>", lambda event: self.config_canvas.configure(scrollregion=self.config_canvas.bbox("all")))
        self.config_canvas.bind("<Configure>", lambda event: self.config_canvas.itemconfigure(item, width=event.width))
        self.form.columnconfigure(1, weight=1)
        row = 0
        for title, section, fields in FIELDS:
            ttk.Label(self.form, text=title, style="Section.TLabel").grid(row=row, column=0, columnspan=3, sticky="w", pady=(12 if row else 0, 6))
            row += 1
            for key, label, kind in fields:
                variable = self.variables.setdefault((section, key), tk.StringVar())
                label_row = ttk.Frame(self.form)
                label_row.grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
                if kind == "check":
                    label_row.grid_configure(columnspan=3)
                    ttk.Checkbutton(label_row, text=label, variable=variable).pack(side="left")
                    self._help_button(label_row, section, key).pack(side="left", padx=(4, 0))
                    row += 1
                    continue
                ttk.Label(label_row, text=label).pack(side="left")
                self._help_button(label_row, section, key).pack(side="left", padx=(4, 0))
                entry = ttk.Entry(self.form, textvariable=variable)
                entry.grid(row=row, column=1, sticky="ew", pady=4)
                if kind == "app_version":
                    self.app_version_entries.append(entry)
                elif kind != "text":
                    ttk.Button(self.form, text="浏览…", command=lambda s=section, k=key, t=kind: self._browse(s, k, t)).grid(row=row, column=2, padx=(8, 0), pady=4)
                row += 1
                if kind == "app_version":
                    ttk.Label(self.form, textvariable=self.bundle_preview, style="Muted.TLabel", wraplength=self.px(550)).grid(row=row, column=1, columnspan=2, sticky="w", pady=(0, 5))
                    row += 1
        ttk.Label(self.form, text="Bundler 和图片 / 预览处理已内置。网络配置当前仅保存，预处理和检查无需 API Token。",
                  style="Muted.TLabel", wraplength=self.px(730)).grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 12))
        self.root.bind_all("<MouseWheel>", self._wheel, add="+")
        actions = ttk.Frame(self.config_page)
        actions.grid(row=3, column=0, sticky="ew", pady=(12, 4))
        ttk.Button(actions, text="载入配置…", command=self.import_config).pack(side="left")
        ttk.Button(actions, text="另存为…", command=self.save_as).pack(side="left", padx=8)
        ttk.Button(actions, text="背景对应表…", command=self.edit_background_pairs).pack(side="left")
        self._help_button(actions, "import", "background_pairs").pack(side="left", padx=(4, 0))
        ttk.Button(actions, text="保存配置", style="Accent.TButton", command=self.save).pack(side="right")
        self.config_label = tk.StringVar(value=str(self.config_path))
        ttk.Label(self.config_page, textvariable=self.config_label, style="Muted.TLabel", wraplength=self.px(790)).grid(row=4, column=0, sticky="w")

    def _wheel(self, event):
        if event.widget.winfo_toplevel() == self.root and self.pages.select() == str(self.config_page):
            self.config_canvas.yview_scroll(-int(event.delta / 120), "units")

    def _help_button(self, parent, section, key):
        # A keyboard-focusable button also works where hover tooltips cannot.
        topic = (section, key)
        button = ttk.Button(parent, text="ⓘ", width=2, style="Help.TButton", takefocus=True,
                            cursor="hand2", command=lambda: self.show_help(topic))
        self.help_buttons.setdefault(topic, []).append(button)
        return button

    def show_help(self, topic):
        content = FIELD_HELP[topic]
        if self.help_window is not None:
            self.help_window.destroy()
        previous_focus = self.root.focus_get()
        window = self.help_window = tk.Toplevel(self.root)
        window.title(content.title + " · 填写说明")
        window.transient(self.root)
        width = min(self.px(640), self.root.winfo_screenwidth() - self.px(40))
        height = min(self.px(470), self.root.winfo_screenheight() - self.px(80))
        x = max(0, min(self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2,
                       self.root.winfo_screenwidth() - width))
        y = max(0, min(self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2,
                       self.root.winfo_screenheight() - height - self.px(40)))
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.minsize(min(self.px(420), width), min(self.px(280), height))
        body = ttk.Frame(window, padding=self.px(16))
        body.pack(fill="both", expand=True)
        title = ttk.Label(body, text=content.title, style="Section.TLabel")
        title.pack(fill="x", pady=(0, self.px(10)))
        window.bind("<Configure>", lambda event: title.configure(
            wraplength=max(self.px(40), event.width - self.px(32))) if event.widget == window else None)

        def dismiss():
            self.help_window = None
            self.help_text = None
            window.destroy()
            if previous_focus is not None and previous_focus.winfo_exists():
                previous_focus.focus_set()

        footer = ttk.Frame(body)
        footer.pack(side="bottom", fill="x", pady=(self.px(10), 0))
        ttk.Button(footer, text="关闭", command=dismiss).pack(side="right")
        ttk.Label(footer, text="可选中文本复制 · Esc 关闭", style="Muted.TLabel").pack(side="left")
        reader = ttk.Frame(body)
        reader.pack(fill="both", expand=True)
        text = self.help_text = tk.Text(reader, wrap="char", relief="flat", background="#ffffff",
                                       foreground="#334155", font=("Microsoft YaHei UI", 10),
                                       padx=self.px(12), pady=self.px(10), width=1, height=1)
        scroll = ttk.Scrollbar(reader, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.tag_configure("heading", font=("Microsoft YaHei UI", 10, "bold"), foreground="#1e293b")
        text.insert("end", content.description + "\n\n")
        text.insert("end", "示例\n", "heading")
        text.insert("end", content.example)
        text.configure(state="disabled")
        window.protocol("WM_DELETE_WINDOW", dismiss)
        window.bind("<Escape>", lambda event: dismiss())
        text.focus_set()

    def _fill(self, values):
        self.settings = deepcopy(values)
        for (section, key), variable in self.variables.items():
            variable.set(values[section][key])
        self.config_label.set(str(self.config_path))
        for variable in self.variables.values():
            if not variable.trace_info():
                variable.trace_add("write", self._edited)
        self._refresh_bundle_preview()

    def _edited(self, *_):
        self.config_label.set(str(self.config_path) + "  · 未保存")
        self._refresh_bundle_preview()
        if self.result is not None:
            self.export_button.configure(state="disabled")
            self.summary.set("配置已变更 · 请重新检查资源")

    def _refresh_bundle_preview(self):
        follow = self.follow_app_version_var.get()
        variable = self.auto_app_version_var if follow else self.manual_app_version_var
        for entry in self.app_version_entries:
            entry.configure(textvariable=variable, state="disabled" if follow else "normal")
        try:
            raw = self.variables[("local", "assets_root")].get().strip()
            if not raw:
                self.auto_app_version_var.set("")
                if not follow:
                    validate_version(self.manual_app_version_var.get().strip())
                self.bundle_preview.set("配置资源目录后可预览包名。")
                return
            assets = Path(raw).expanduser()
            if not assets.is_absolute():
                assets = self.config_path.parent / assets
            history = assets / "metadata.oldjson"
            stat = history.stat() if history.is_file() else None
            signature = (str(history), stat.st_mtime_ns if stat else None, stat.st_size if stat else None)
            if self._bundle_history_cache is None or self._bundle_history_cache[0] != signature:
                self._bundle_history_cache = (signature, read_history(assets))
            records = self._bundle_history_cache[1]
            latest = latest_record(records)
            self.auto_app_version_var.set(latest["applicationVersionNumber"] if latest else "")
            plan = resolve_bundle_versions(records, follow_app_version=follow, app_version=self.manual_app_version_var.get())
            self.bundle_preview.set(("承接包：" if plan.transition else "下一包：") + plan.filename_prefix + ".cb / .json")
        except (ValueError, OSError) as exc:
            if follow:
                self.auto_app_version_var.set("")
            self.bundle_preview.set(str(exc))

    def _collect(self):
        values = deepcopy(self.settings)
        for (section, key), variable in self.variables.items():
            values[section][key] = variable.get()
        return normalize_settings(values, self.config_path.parent)

    def _browse(self, section, key, kind):
        current = Path(self.variables[(section, key)].get() or str(Path.home())).expanduser()
        initial = current if current.is_dir() else current.parent
        if kind == "dir":
            selected = filedialog.askdirectory(parent=self.root, initialdir=str(initial), title="选择目录")
        else:
            selected = filedialog.askopenfilename(parent=self.root, initialdir=str(initial), title="选择文件")
        if selected:
            self.variables[(section, key)].set(selected)

    def save(self):
        try:
            self.settings = save_settings(self.config_path, self._collect())
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法保存配置", str(exc), parent=self.root)
            return False
        self.status.set("配置已保存。")
        self.config_label.set(str(self.config_path))
        return True

    def save_as(self):
        selected = filedialog.asksaveasfilename(parent=self.root, title="保存配置", defaultextension=".json", filetypes=[("JSON 配置", "*.json")])
        if not selected:
            return
        old_path = self.config_path
        try:
            # Resolve relative values against their old file before changing origins.
            values = self._collect()
            path = Path(selected).resolve()
            values = save_settings(path, values)
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法保存配置", str(exc), parent=self.root)
            return
        self.config_path = path
        self._fill(values)
        self.status.set("配置已另存。" if old_path != path else "配置已保存。")

    def import_config(self):
        selected = filedialog.askopenfilename(parent=self.root, title="载入配置", filetypes=[("JSON 配置", "*.json")])
        if not selected:
            return
        try:
            path = Path(selected).resolve()
            values = effective_settings(load_settings(path), path.parent)
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法载入配置", str(exc), parent=self.root)
            return
        self.config_path = path
        self._fill(values)
        self.status.set("配置已载入。")

    def start_check(self):
        self._start_job("check")

    def start_preprocess(self):
        self._start_job("preprocess")

    def _start_job(self, kind, resume_from=None):
        if self.busy:
            return
        if not self.server_ops.commit_editor():
            return
        try:
            values = self._collect()
            require_version(values["import"]["version"])
            if not values["bundle"]["follow_app_version"]:
                validate_version(values["bundle"]["app_version"])
        except (ValueError, OSError) as exc:
            self._set_details("无法开始", [])
            self._append_log({"level": "error", "code": "config.invalid", "message": str(exc), "song_id": None})
            self.results.select(2)
            self.summary.set("ERROR · 请检查本次导入配置")
            return
        self._refresh_bundle_preview()
        self.busy = True
        self.job_kind = kind
        if kind == "preprocess":
            self.preprocessed = None
            self.preprocessed_signature = None
        self.result = None
        self.result_signature = json.dumps(values, sort_keys=True)
        self.check_button.configure(state="disabled")
        self.preprocess_button.configure(state="disabled")
        self.workspace_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        for tree in (self.song_tree, self.file_tree):
            tree.delete(*tree.get_children())
        self._set_details("正在检查", ["正在扫描本次曲目和比较资源，请稍候。"])
        self.results.select(2)
        self.summary.set("正在整理曲目、生成标准副本…" if kind == "preprocess" else "正在扫描曲目和比较资源…")
        self.status.set("正在预处理，原始文件保留。" if kind == "preprocess" else "正在检查资源，窗口可继续操作。")
        self.progress.start(12)
        origin = self.config_path.parent
        previous = self.preprocessed if self.preprocessed_signature == self.result_signature else None
        Thread(target=self._worker, args=(values, origin, kind, previous, resume_from), daemon=True).start()
        self.poll_id = self.root.after(80, self._poll)

    def _worker(self, values, origin, kind, previous, resume_from=None):
        # Only queue plain data. All Tk operations stay on the main thread.
        try:
            if kind == "preprocess":
                result = preprocess_settings(values, origin, resume_from=resume_from,
                                             on_diagnostic=lambda entry: self.queue.put(("log", entry)))
            else:
                result = inspect_settings(values, origin, previous)
                for entry in result.get("diagnostics", []):
                    self.queue.put(("log", entry))
            self.queue.put((True, result))
        except Exception as exc:
            self.queue.put((False, str(exc)))

    def _poll(self):
        self.poll_id = None
        if self.closed:
            return
        for _ in range(200):
            try:
                success, value = self.queue.get_nowait()
            except Empty:
                self.poll_id = self.root.after(80, self._poll)
                return
            if success != "log":
                break
            self._append_log(value)
        else:
            self.poll_id = self.root.after(20, self._poll)
            return
        self.busy = False
        self.progress.stop()
        self.check_button.configure(state="normal")
        self.preprocess_button.configure(state="normal")
        self.workspace_button.configure(state="normal")
        if not success:
            self.summary.set("检查未完成 · 请查看待处理与提示")
            self._set_details("检查未完成", [])
            self._append_log({"level": "error", "code": "job.failed", "message": value, "song_id": None})
            self.results.select(2)
            self.status.set("请检查路径和资源后重试。")
            return
        self.result = value
        if self.job_kind == "preprocess":
            self.preprocessed = value
            self.preprocessed_signature = self.result_signature
            self._show_preprocess(value)
        else:
            self._show_result(value)
        try:
            current = json.dumps(self._collect(), sort_keys=True)
        except (ValueError, OSError):
            current = None
        if current != self.result_signature:
            self.server_ops.context_expired = True
            self._edited()
            self.status.set("检查期间配置已变更，请重新检查。")

    def manage_workspaces(self):
        if self.workspace_manager is not None:
            self.workspace_manager.window.lift()
            self.workspace_manager.refresh()
            return
        from .workspace_gui import WorkspaceManager
        self.workspace_manager = WorkspaceManager(self)

    def resume_workspace(self, batch):
        if self.busy:
            return
        try:
            values = self._collect()
            report = load_workspace(values["local"]["release_root"], batch)
            self.input_var.set(report["input_root"])
            self.import_version_var.set(report["import_version"])
        except (ValueError, OSError, KeyError) as exc:
            messagebox.showerror("无法继续批次", str(exc), parent=self.root)
            return
        self.pages.select(self.home_page)
        self._start_job("preprocess", resume_from=batch)

    def show_workspace_report(self, report):
        self.preprocessed = self.preprocessed_signature = None
        self.result, self.result_signature = report, None
        for tree in (self.song_tree, self.file_tree):
            tree.delete(*tree.get_children())
        self._show_preprocess(report)
        self.server_ops.context_expired = True
        self.status.set("正在查看历史报告；继续处理后会重新核验，再供本次资源检查使用。")
        self.pages.select(self.home_page)

    def forget_workspace(self, batch):
        references = [self.preprocessed or {}, self.result or {}]
        if not any(r.get("batch_root") == str(batch) or r.get("preprocessing_report") == str(batch / "report.json")
                   for r in references):
            return
        self.preprocessed = self.preprocessed_signature = None
        self.result = self.result_signature = None
        self.export_button.configure(state="disabled")
        for tree in (self.song_tree, self.file_tree):
            tree.delete(*tree.get_children())
        self.summary.set("批次已清理 · 请重新预处理或检查")
        self._set_details("工作区已清理", [str(batch)])

    def _set_details(self, title, lines):
        self.log_items = []
        self.log_summary.set("INFO 0 · WARNING 0 · ERROR 0")
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("end", title + "\n\n" + "\n\n".join(str(line) for line in lines))
        self.details.configure(state="disabled")

    def _append_log(self, entry):
        self.log_items.append(entry)
        counts = {level: sum(d["level"] == level for d in self.log_items) for level in ("info", "warning", "error")}
        self.log_summary.set(f"INFO {counts['info']} · WARNING {counts['warning']} · ERROR {counts['error']}")
        level = entry["level"]
        subject = entry.get("song_id") or (Path(entry["source"]).name if entry.get("source") else "")
        prefix = f"[{level.upper()}]" + (f" [{subject}]" if subject else "")
        self.details.configure(state="normal")
        self.details.insert("end", prefix + " " + entry["message"] + "\n", level)
        self.details.see("end")
        self.details.configure(state="disabled")

    def _render_diagnostics(self, report, title, extra=()):
        self._set_details(title, list(extra))
        for entry in report.get("diagnostics", []):
            self._append_log(entry)

    def _show_result(self, plan):
        for tree in (self.song_tree, self.file_tree):
            tree.delete(*tree.get_children())
        for song in plan["songs"]:
            self.song_tree.insert("", "end", values=(song["id"], "新曲目" if song["kind"] == "new" else "增量更新", song["idx"]))
        labels = {"add": "新增", "replace": "覆盖冲突", "unchanged": "相同", "prepare_media": "待处理媒体"}
        for entry in plan["files"]:
            self.file_tree.insert("", "end", values=("资源包" if entry["area"] == "assets" else "下载资源",
                                  entry["path"], "补占位" if entry["placeholder"] else labels[entry["action"]]))
        new = sum(song["kind"] == "new" for song in plan["songs"])
        self.summary.set(f"共 {len(plan['songs'])} 首 · 新曲 {new} · 增量 {len(plan['songs']) - new} · 待处理 {len(plan['errors'])}")
        self._render_diagnostics(plan, "资源检查结果")
        self.export_button.configure(state="normal")
        self.results.select(2 if plan["errors"] or plan.get("warnings") else 0)
        self.status.set("预检完成，查看 ERROR。" if plan["errors"] else "预检完成，请审核 WARNING；当前未执行发布。")
        self.server_ops.set_context(plan)

    def _show_preprocess(self, report):
        statuses = {"normalized": "已标准化", "blocked": "待人工处理", "ignored": "跳过空谱"}
        actions = {"copy": "复制", "thumbnail": "生成缩略图", "transcode": "音频转码"}
        for package in report["packages"]:
            sid = package["id"] or Path(package["source"]).name
            self.song_tree.insert("", "end", values=(sid, statuses.get(package["status"], package["status"]), "—"))
            for action in package["actions"]:
                self.file_tree.insert("", "end", values=("标准副本", f"{sid}/{action['target']}", actions[action["kind"]]))
        counts = report["counts"]
        self.summary.set(f"标准化 {counts.get('normalized', 0)} 首 · 待处理 {counts.get('blocked', 0)} 首 · 跳过 {counts.get('ignored', 0)} 首")
        self._render_diagnostics(report, "预处理结果", ["标准副本：" + report["normalized_root"] + "\n报告：" + report["report_file"] + "\n"])
        self.export_button.configure(state="normal")
        self.results.select(2 if report["errors"] or report.get("warnings") else 0)
        self.status.set("预处理完成；点击“检查资源”使用本次标准副本，原来源目录保持不变。")
        self.server_ops.set_context(report)

    def export_report(self):
        if self.result is None:
            return
        selected = filedialog.asksaveasfilename(parent=self.root, title="导出检查报告", initialfile="plan.local.json",
                                               defaultextension=".json", filetypes=[("JSON 报告", "*.json")])
        if selected:
            try:
                Path(selected).write_text(json.dumps(self.result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
                self.status.set("检查报告已导出。")
            except (ValueError, OSError) as exc:
                messagebox.showerror("无法导出报告", str(exc), parent=self.root)

    def edit_background_pairs(self):
        try:
            policy = effective_settings(self._collect(), self.config_path.parent)["import"]
        except (ValueError, OSError) as exc:
            messagebox.showerror("无法读取背景对应表", str(exc), parent=self.root)
            return
        window = tk.Toplevel(self.root)
        window.title("背景翻转对应表")
        width, height = self.px(570), self.px(400)
        x = max(0, self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2)
        y = max(0, self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.minsize(self.px(420), self.px(280))
        window.transient(self.root)
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill="both", expand=True)
        hint = ('已从当前总表初始化；每组写一次即可双向推导，例如 "A": "B"。'
                if policy["background_pairs_initialized"] else '配置 active 后可读取总表；每组写一次即可双向推导，例如 "A": "B"。')
        ttk.Label(frame, text=hint, wraplength=self.px(530)).pack(anchor="w", pady=(0, 8))
        table_status = tk.StringVar()
        ttk.Label(frame, textvariable=table_status, style="Muted.TLabel", wraplength=self.px(530)).pack(anchor="w", pady=(0, 8))
        save_row = ttk.Frame(frame)
        save_row.pack(side="bottom", fill="x", pady=(8, 0))
        text = tk.Text(frame, wrap="word", font=("Consolas", 11))
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", json.dumps(policy["background_pairs"], ensure_ascii=False, indent=2))
        pending_conflicts = policy["background_pair_conflicts"].copy()
        def update_table_status(pairs):
            unresolved = sorted(set(pending_conflicts) - set(pair_lookup(pairs)))
            table_status.set(f"{len(pairs)} 组双向对应；{len(unresolved)} 个背景待确认。" +
                             (" 待确认：" + "、".join(unresolved) if unresolved else ""))
        update_table_status(policy["background_pairs"])
        def reload_pairs():
            nonlocal pending_conflicts
            from .backgrounds import learn_pairs
            from .planner import read_json
            try:
                values = self._collect()
                learned, conflicts = learn_pairs(read_json(Path(values["local"]["assets_root"]) / "songs" / "songlist"))
            except (ValueError, OSError) as exc:
                messagebox.showerror("无法读取总表", str(exc), parent=window)
                return
            text.delete("1.0", "end")
            text.insert("1.0", json.dumps(learned, ensure_ascii=False, indent=2))
            pending_conflicts = conflicts
            update_table_status(learned)
        def save_pairs():
            try:
                pairs = normalize_pairs(json.loads(text.get("1.0", "end")))
            except ValueError as exc:
                messagebox.showerror("对应表无效", str(exc), parent=window)
                return
            self.settings["import"]["background_pairs"] = pairs
            self.settings["import"]["background_pair_conflicts"] = pending_conflicts
            self.settings["import"]["background_pairs_initialized"] = True
            self._edited()
            self.status.set("背景对应表已更新，请保存配置。")
            window.destroy()
        ttk.Button(save_row, text="应用对应表", command=save_pairs).pack(side="right")
        ttk.Button(save_row, text="重新读取当前总表", command=reload_pairs).pack(side="left")

    def show_licenses(self):
        window = tk.Toplevel(self.root)
        window.title("关于与授权 · Arcaea-Uploader")
        window.geometry(f"{self.px(650)}x{self.px(520)}")
        window.transient(self.root)
        body = ttk.Frame(window, padding=16)
        body.pack(fill="both", expand=True)
        license_text = package_files("arcaea_uploader._vendor.arcaea_bundler").joinpath("LICENSE").read_text(encoding="utf-8")
        text = tk.Text(body, wrap="word", relief="flat", font=("Microsoft YaHei UI", 10), padx=12, pady=12)
        scroll = ttk.Scrollbar(body, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", "Arcaea-Uploader\n\n内置 Arcaea-Bundler · Lost-MSth · MIT\n"
                    "https://github.com/Lost-MSth/Arcaea-Bundler\n\n"
                    "图片与预览处理来自维护者提供的原手动更新脚本。\n"
                    "FFmpeg 为外部程序，本项目未捆绑分发。\n\n" + license_text)
        text.configure(state="disabled")

    def close(self):
        self.closed = True
        self.server_ops.close()
        if self.poll_id:
            self.root.after_cancel(self.poll_id)
        self.root.destroy()


def launch(config_path: Path | None = None):
    if os.name == "nt":
        # Opt in before creating widgets; let Tk choose point-size scaling.
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    UploaderApp(root, config_path)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch(Path(sys.argv[1]) if len(sys.argv) > 1 else None))
