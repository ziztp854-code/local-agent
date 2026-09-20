from datetime import datetime
import os
from pathlib import Path
from queue import Empty, Queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # Optional at source level; required by requirements.txt for native file drops.
    DND_FILES = "DND_Files"
    TkinterDnD = None

from agent import DEFAULT_BASE_URL, DEFAULT_MODEL
from approval_dialog import ApprovalDialog
from command_palette import CommandPalette
from desktop_core import (
    AgentController,
    AppSettings,
    DesktopConfig,
    build_workspace_briefing,
    describe_workspace,
    list_models,
    probe_model_server,
)
from mcp_client import MCPRegistry
from memory_panel import MemoryPanel
from session_store import SessionStore
from skill_panel import SkillsPanel
import theme
from theme import COLORS, DARK_COLORS
from workspace_tools import ToolError, safe_terminal_text


def _resource_path(*parts):
    """حُلّ مسار مورد مُجمَّع سواء من المصدر أو من حزمة PyInstaller."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


MODE_LABELS = {
    "قراءة فقط": "read",
    "برمجة": "coding",
    "برمجة + أوامر المضيف": "host",
}

HARNESS_LABELS = {
    "قياسي": "standard",
    "DeepSeek": "deepseek",
}
HARNESS_NAMES = {value: label for label, value in HARNESS_LABELS.items()}

QUICK_PROMPTS = (
    ("لخّص المشروع", "لخّص هذا المشروع واذكر أهم ملفاته ودور كل منها."),
    ("اقترح تحسينات", "اقرأ الكود واقترح تحسينات ملموسة مرتبة بالأولوية."),
    ("ابحث عن أخطاء", "ابحث في المشروع عن أخطاء أو مخاطر أمنية وأعط أمثلة."),
    ("اشرح ملفًا", "اشرح بنية الملف التالي ودوره في المشروع:"),
)

CONNECTION_GUIDANCE = (
    "تعذر الوصول إلى LM Studio ({detail}). خطوات التشغيل:\n"
    "1) افتح تطبيق LM Studio وحمّل النموذج:\n"
    "lms load qwen2.5-7b-instruct --gpu max --context-length 16384 --parallel 1 --ttl 900 -y\n"
    "2) شغّل الخادم المحلي:\n"
    "lms server start --port 1234 --bind 127.0.0.1\n"
    "3) اضغط «تطبيق وبدء» ثم أعد إرسال طلبك."
)


class Tooltip:
    """تلميح خفيف يظهر عند التحويم فوق عنصر؛ يشرح معنى الأيقونات والحالات."""

    def __init__(self, widget, text, colors):
        self.widget = widget
        self.text = text
        self.colors = colors
        self._tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None):
        if self._tip is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            self._tip,
            text=self.text,
            bg=self.colors["chip"],
            fg=self.colors["chip_fg"],
            relief="flat",
            justify="right",
            padx=10,
            pady=6,
            font=theme.ui_font(9),
            highlightthickness=1,
            highlightbackground=self.colors["line"],
        ).pack()

    def _hide(self, _event=None):
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class LocalAgentApp:
    def __init__(self, root, controller=None, skill_catalog=None, settings=None):
        self.root = root
        self.controller = controller or AgentController()
        theme.detect_fonts(root)
        self._settings = settings
        self._active_config = None
        self._closing = False
        self._destroyed = False
        self._approval_dialog = None
        self._approval_queue = []
        self._poll_job = None
        self._skill_catalog = skill_catalog
        self.colors = COLORS
        self._dark_theme = False
        self._streaming_answer_open = False
        self._stream_result = None
        self._thought_visible = False
        self._mcp_editor = None
        self._mcp_checks = []
        self._pending_mcp_toggle = None
        self._chat_log = []
        self._stream_parts = []
        self._stats = {"requests": 0, "completed": 0, "denied": 0, "errors": 0, "tools": 0}
        self._busy_since = None
        self._timer_job = None
        self._probe_results = Queue()
        self._probe_thread = None
        self._boot_results = Queue()
        self._boot_thread = None
        self._models_results = Queue()
        self._models_thread = None
        self._delegation_snapshot = None

        self.workspace_var = tk.StringVar(value=str(Path.cwd()))
        self.mode_var = tk.StringVar(value="قراءة فقط")
        self.session_var = tk.StringVar()
        self.model_var = tk.StringVar(value=DEFAULT_MODEL)
        self.harness_var = tk.StringVar(value="قياسي")
        self.base_url_var = tk.StringVar(value=DEFAULT_BASE_URL)
        self.mcp_config_var = tk.StringVar()
        self.semantic_memory_var = tk.BooleanVar(value=False)
        self.delegation_var = tk.BooleanVar(value=True)
        self.memory_enabled_var = tk.BooleanVar(value=True)
        self.experience_learning_var = tk.BooleanVar(value=True)
        self.reflection_var = tk.BooleanVar(value=True)
        self.skill_learning_var = tk.BooleanVar(value=True)
        self.knowledge_graph_var = tk.BooleanVar(value=True)
        self.memory_consolidation_var = tk.BooleanVar(value=True)
        self._memory_feature_checks = []
        self.container_engine_var = tk.StringVar()
        self.pending_images = []
        self.image_note_var = tk.StringVar(value="لا توجد صور مرفقة")
        self.status_var = tk.StringVar(value="جاهز")
        self.connection_var = tk.StringVar(value="LM Studio: لم يُفحص")
        self.trust_mode_var = tk.StringVar()
        self.write_state_var = tk.StringVar()
        self.skills_badge_var = tk.StringVar(value="المهارات: جارٍ الفحص")
        self.mode_note_var = tk.StringVar()
        self.stats_var = tk.StringVar(value="الجلسة: 0 طلب")

        saved = settings.load() if settings else {}
        if saved.get("workspace"):
            self.workspace_var.set(saved["workspace"])
        if saved.get("session"):
            self.session_var.set(saved["session"])
        if saved.get("model"):
            self.model_var.set(saved["model"])
        if saved.get("harness") in HARNESS_NAMES:
            self.harness_var.set(HARNESS_NAMES[saved["harness"]])
        if saved.get("base_url"):
            self.base_url_var.set(saved["base_url"])
        if saved.get("mode") in MODE_LABELS:
            self.mode_var.set(saved["mode"])
        self._saved_dark_theme = bool(saved.get("dark_theme", True))
        if "delegation_enabled" in saved:
            self.delegation_var.set(saved["delegation_enabled"])
        for key, variable in (
            ("memory_enabled", self.memory_enabled_var),
            ("experience_learning_enabled", self.experience_learning_var),
            ("reflection_enabled", self.reflection_var),
            ("skill_learning_enabled", self.skill_learning_var),
            ("knowledge_graph_enabled", self.knowledge_graph_var),
            ("memory_consolidation_enabled", self.memory_consolidation_var),
        ):
            if key in saved:
                variable.set(saved[key])

        self._configure_window()
        self._build_ui()
        self._install_hovers(self.root)
        if self._saved_dark_theme:
            self.toggle_theme()
        self._update_mode_ui()
        self._update_composer_state()
        self._install_drop_support()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Control-Key-1>", lambda event: self._select_tab(self.chat_page))
        self.root.bind("<Control-Key-2>", lambda event: self._select_tab(self.changes_page))
        self.root.bind("<Control-Key-3>", lambda event: self._select_tab(self.skills_page))
        self.root.bind(
            "<Control-Key-4>", lambda event: self._select_tab(self.integrations_page)
        )
        self.root.bind("<Control-Key-5>", lambda event: self._select_tab(self.memory_page))
        self.root.bind("<Control-o>", lambda event: self.choose_workspace())
        self.root.bind("<Control-Shift-O>", lambda event: self.choose_images())
        self.root.bind("<Control-f>", self._focus_skill_search)
        self.root.bind("<Control-k>", self.open_command_palette)
        self.root.bind("<Control-l>", lambda event: self.check_connection())
        self._poll_job = self.root.after(50, self._pump)
        self.root.after(150, self._startup_probe)

    def _startup_probe(self):
        if self._destroyed:
            return
        # شغّل خادم LM Studio تلقائيًا مع تفريغ النموذج على كرت الشاشة (GPU) إن لم
        # يكن يعمل، حتى تكون الاستجابة سريعة، ثم افحص الاتصال. يجري في خيط خلفي
        # حتى لا تتجمد الواجهة أثناء تحميل النموذج.
        base = self.base_url_var.get().strip()
        model = self.model_var.get().strip() or DEFAULT_MODEL
        self.connection_var.set("LM Studio: جارٍ تشغيل GPU…")

        def boot():
            try:
                from lmstudio_boot import ensure_server

                ensure_server(base, model)
            except Exception:  # لا يجوز أن يعطّل الإقلاع التلقائي التطبيق.
                pass
            self._boot_results.put(True)

        self._boot_thread = threading.Thread(
            target=boot, name="local-agent-boot", daemon=True
        )
        self._boot_thread.start()

    def _configure_window(self):
        self.root.title("الوكيل المحلي")
        # افتح بأطول نافذة يسمح بها ارتفاع الشاشة حتى تأخذ المحادثة مساحة قراءة كبيرة.
        try:
            screen_h = self.root.winfo_screenheight()
            screen_w = self.root.winfo_screenwidth()
        except tk.TclError:
            screen_h, screen_w = 900, 1240
        # عامل قياس الشاشة عالية الكثافة: نكبّر الأبعاد المنطقية بما يوافق DPI
        # حتى تبقى النسب صحيحة على شاشات الدقة العالية بعد تفعيل حِدّة العرض.
        try:
            scale = max(1.0, min(3.0, self.root.winfo_fpixels("1i") / 96.0))
        except tk.TclError:
            scale = 1.0
        width = min(int(1360 * scale), max(int(1040 * scale), int(screen_w * 0.9)))
        height = min(int(1320 * scale), max(int(760 * scale), int(screen_h * 0.9)))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(int(1040 * scale), int(740 * scale))
        # افتح بملء الشاشة لأقصى مساحة قراءة للمحادثة.
        try:
            self.root.state("zoomed")
        except tk.TclError:
            pass
        self.root.configure(bg=COLORS["fog"])
        self._set_app_icon()
        self.root.option_add("*Font", theme.ui_font(10))
        self.root.option_add("*Button.takeFocus", True)
        self.root.option_add("*Button.highlightThickness", 2)
        self.root.option_add("*Button.highlightBackground", COLORS["line"])
        self.root.option_add("*Button.highlightColor", COLORS["focus"])
        self.root.option_add("*Entry.highlightThickness", 1)
        self.root.option_add("*Entry.highlightColor", COLORS["focus"])
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TNotebook", background=COLORS["surface"], borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            padding=(20, 10),
            font=theme.ui_font(10, 'bold'),
            background=COLORS["fog"],
            foreground=COLORS["muted"],
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", COLORS["surface"])],
            foreground=[("selected", COLORS["ink"])],
        )
        # دفتر بلا تبويبات ظاهرة؛ نستبدلها بأزرار موسّطة مخصّصة.
        style.configure("Center.TNotebook", background=COLORS["surface"], borderwidth=0)
        style.layout("Center.TNotebook.Tab", [])
        style.configure("TCombobox", padding=7)

    def _set_app_icon(self):
        """اضبط أيقونة النافذة من assets؛ أي فشل لا يعطّل التطبيق."""
        try:
            ico = _resource_path("assets", "app_icon.ico")
            if ico.exists():
                self.root.iconbitmap(default=str(ico))
        except tk.TclError:
            pass
        try:
            png = _resource_path("assets", "app_icon.png")
            if png.exists():
                # الاحتفاظ بمرجع يمنع جمع الصورة تلقائيًا (يفرّغ الأيقونة).
                self._icon_image = tk.PhotoImage(file=str(png))
                self.root.iconphoto(True, self._icon_image)
        except tk.TclError:
            pass

    def _build_ui(self):
        self.root.grid_rowconfigure(2, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        header = tk.Frame(self.root, bg=COLORS["surface"], padx=26, pady=10)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        task_box = tk.Frame(header, bg=COLORS["surface"])
        task_box.grid(row=0, column=0, sticky="w")
        self.status_dot = tk.Label(
            task_box,
            text="●",
            bg=COLORS["soft_teal"],
            fg=COLORS["teal"],
            font=theme.ui_font(9),
        )
        self.status_dot.pack(side="left", padx=(12, 0), pady=7)
        self.status_label = tk.Label(
            task_box,
            textvariable=self.status_var,
            bg=COLORS["soft_teal"],
            fg=COLORS["teal"],
            padx=4,
            pady=7,
        )
        self.status_label.pack(side="left")
        self.theme_button = tk.Button(
            task_box,
            text="الوضع الداكن",
            command=self.toggle_theme,
            bg=COLORS["fog"],
            fg=COLORS["ink"],
            relief="flat",
            borderwidth=1,
            padx=10,
            pady=6,
            cursor="hand2",
        )
        self.theme_button.pack(side="left", padx=(10, 0))
        self.progress = ttk.Progressbar(task_box, mode="indeterminate", length=92)
        title_box = tk.Frame(header, bg=COLORS["surface"])
        title_box.grid(row=0, column=1, sticky="e")
        title_row = tk.Frame(title_box, bg=COLORS["surface"])
        title_row.pack(anchor="e")
        tk.Label(
            title_row,
            text="الوكيل المحلي",
            bg=COLORS["surface"],
            fg=COLORS["ink"],
            font=theme.display_font(22),
            anchor="e",
        ).pack(side="right")
        tk.Label(
            title_row,
            text="◆ ",
            bg=COLORS["surface"],
            fg=COLORS["teal"],
            font=theme.ui_font(16, 'bold'),
            anchor="e",
        ).pack(side="right")
        tk.Label(
            title_box,
            text="محطة تشغيل خاصة على جهازك",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=theme.ui_font(9),
            anchor="e",
        ).pack(anchor="e")

        trust_band = tk.Frame(self.root, bg=COLORS["band_bg"], padx=theme.space("xl"), pady=theme.space("sm"))
        trust_band.grid(row=1, column=0, sticky="ew")
        self.trust_band = trust_band
        self._trust_labels = []
        self._trust_chips = []
        self.trust_rail = tk.Frame(trust_band, width=4, bg=COLORS["teal"])
        self.trust_rail.pack(side="right", fill="y", padx=(theme.space("md"), 0))
        for variable, icon, dot, tip in (
            (self.trust_mode_var, "</>", COLORS["teal"], "وضع تشغيل الوكيل: قراءة، برمجة، أو أوامر المضيف."),
            (self.write_state_var, "📄", COLORS["info"], "حالة الكتابة: كل تعديل يعرض فرقًا ويحتاج موافقتك."),
            (self.connection_var, "🔌", COLORS["success"], "حالة اتصال خادم LM Studio المحلي."),
            (self.skills_badge_var, "🧩", COLORS["teal"], "عدد المهارات المدمجة المتاحة للوكيل."),
        ):
            self._trust_labels.append(
                self._build_trust_chip(trust_band, variable, icon, dot, tooltip=tip)
            )
        self.stats_chip = self._build_trust_chip(
            trust_band,
            self.stats_var,
            "⏱",
            COLORS["muted"],
            side="left",
            tooltip="ملخص نشاط الجلسة: الطلبات والأدوات والاكتمال.",
        )
        # يُبقي stats_label اسمًا متوافقًا مع بقية الشيفرة (نصّ رقاقة الإحصاءات).
        self.stats_label = self.stats_chip

        body = tk.Frame(self.root, bg=COLORS["fog"], padx=8, pady=8)
        body.grid(row=2, column=0, sticky="nsew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)

        footer = tk.Frame(self.root, bg=COLORS["surface"], padx=20, pady=8)
        footer.grid(row=3, column=0, sticky="ew")
        self.footer_hint = tk.Label(
            footer,
            text=(
                "Ctrl+Enter إرسال   ·   Ctrl+1…5 التبويبات   ·   Ctrl+F البحث   "
                "·   Ctrl+L فحص الاتصال   ·   Ctrl+K لوحة الأوامر"
            ),
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=theme.ui_font(9),
        )
        self.footer_hint.pack(side="right")

        self.navigation_panel = tk.Frame(
            body, bg=COLORS["surface"], width=228,
            highlightthickness=1, highlightbackground=COLORS["line"],
            padx=12, pady=16,
        )
        self.navigation_panel.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        self.navigation_panel.grid_propagate(False)

        main = tk.Frame(body, bg=COLORS["fog"])
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(0, weight=1)
        main.grid_columnconfigure(0, weight=1)
        sidebar = tk.Frame(
            body, bg=COLORS["surface"], width=292,
            highlightthickness=1, highlightbackground=COLORS["line"],
            padx=16, pady=18,
        )
        sidebar.grid(row=0, column=2, sticky="ns", padx=(10, 0))
        sidebar.grid_propagate(False)

        self._build_navigation(self.navigation_panel)
        self._build_sidebar(sidebar)
        self._build_main(main)

    def _build_navigation(self, parent):
        """تنقّل ثابت خفيف؛ الصفحات الفعلية تبقى في دفتر الواجهة المركزي."""
        brand = tk.Frame(parent, bg=COLORS["surface"])
        brand.pack(fill="x", pady=(0, 24))
        tk.Label(
            brand, text="◆", bg=COLORS["soft_teal"], fg=COLORS["teal"],
            font=theme.display_font(24), padx=9, pady=5,
        ).pack(side="left")
        brand_copy = tk.Frame(brand, bg=COLORS["surface"])
        brand_copy.pack(side="right", fill="x", expand=True, padx=(8, 0))
        tk.Label(
            brand_copy, text="الوكيل المحلي", bg=COLORS["surface"], fg=COLORS["ink"],
            font=theme.ui_font(12, "bold"), anchor="e",
        ).pack(fill="x")
        tk.Label(
            brand_copy, text="مساعدك الذكي على جهازك", bg=COLORS["surface"],
            fg=COLORS["muted"], font=theme.ui_font(8), anchor="e",
        ).pack(fill="x")

        self._navigation_buttons = []
        items = (
            ("المحادثة", "◯", lambda: self._select_tab(self.chat_page)),
            ("المهام", "☑", lambda: self._select_tab(self.changes_page)),
            ("التطبيقات", "▦", lambda: self._select_tab(self.skills_page)),
            ("الذاكرة", "◈", lambda: self._select_tab(self.memory_page)),
            ("التكاملات", "⌘", lambda: self._select_tab(self.integrations_page)),
            ("إعدادات", "⚙", lambda: self._select_tab(self.integrations_page)),
        )
        for index, (label, icon, command) in enumerate(items):
            button = tk.Button(
                parent, text=f"{icon}    {label}",
                command=command,
                bg=COLORS["soft_teal"] if index == 0 else COLORS["surface"],
                fg=COLORS["teal"] if index == 0 else COLORS["chip_fg"],
                activebackground=COLORS["elevated"], activeforeground=COLORS["teal"],
                relief="flat", borderwidth=0, anchor="e",
                padx=14, pady=11, font=theme.ui_font(11, "bold" if index == 0 else "normal"),
                cursor="hand2",
            )
            button.pack(fill="x", pady=(0, 4))
            self._navigation_buttons.append(button)

        spacer = tk.Frame(parent, bg=COLORS["surface"])
        spacer.pack(fill="both", expand=True)
        status = tk.Frame(
            parent, bg=COLORS["secondary_bg"], highlightthickness=1,
            highlightbackground=COLORS["line"], padx=12, pady=12,
        )
        status.pack(fill="x")
        tk.Label(
            status, text="●  جاهز للعمل", bg=COLORS["secondary_bg"],
            fg=COLORS["success"], font=theme.ui_font(10, "bold"), anchor="e",
        ).pack(fill="x")
        tk.Label(
            status, text="الوضع المحلي", bg=COLORS["secondary_bg"], fg=COLORS["muted"],
            font=theme.ui_font(8), anchor="e",
        ).pack(fill="x", pady=(4, 0))

    def _build_trust_chip(self, parent, variable, icon, dot, side="right", tooltip=""):
        """رقاقة حالة على هيئة حبّة (pill): نقطة حالة + نص + أيقونة + تلميح."""
        frame = tk.Frame(
            parent,
            bg=COLORS["chip"],
            highlightthickness=1,
            highlightbackground=COLORS["line"],
            padx=theme.space("md"),
            pady=theme.space("xs"),
        )
        frame.pack(side=side, padx=(theme.space("sm"), 0))
        icon_label = tk.Label(frame, text=icon, bg=COLORS["chip"], fg=COLORS["chip_fg"])
        icon_label.pack(side="right", padx=(theme.space("sm"), 0))
        text_label = tk.Label(
            frame, textvariable=variable, bg=COLORS["chip"], fg=COLORS["chip_fg"]
        )
        text_label.pack(side="right")
        dot_label = tk.Label(frame, text="●", bg=COLORS["chip"], fg=dot, font=theme.ui_font(7))
        dot_label.pack(side="left", padx=(theme.space("sm"), 0))
        # حالة تحويم لطيفة على الرقاقة كاملة + تلميح يشرح معناها.
        def _hover(color):
            for part in (frame, icon_label, text_label, dot_label):
                try:
                    part.configure(bg=color)
                except tk.TclError:
                    pass
        frame.bind("<Enter>", lambda e: _hover(self.colors["elevated"]), add="+")
        frame.bind("<Leave>", lambda e: _hover(self.colors["chip"]), add="+")
        if tooltip:
            for part in (frame, icon_label, text_label, dot_label):
                Tooltip(part, tooltip, self.colors)
        self._trust_chips.append((frame, icon_label, dot_label))
        return text_label

    def _build_sidebar(self, parent):
        section_head = tk.Frame(parent, bg=COLORS["surface"])
        section_head.pack(fill="x", pady=(0, 16))
        accent = tk.Frame(section_head, width=4, bg=COLORS["teal"])
        accent.pack(side="right", fill="y", padx=(8, 0))
        tk.Label(
            section_head,
            text="  حدود العمل",
            bg=COLORS["surface"],
            fg=COLORS["ink"],
            font=theme.ui_font(14, 'bold'),
            anchor="e",
        ).pack(side="right")
        self._field_label(parent, "مساحة العمل")
        workspace_row = tk.Frame(parent, bg=COLORS["surface"])
        workspace_row.pack(fill="x", pady=(0, 14))
        self.workspace_entry = tk.Entry(
            workspace_row,
            textvariable=self.workspace_var,
            justify="left",
            relief="solid",
            borderwidth=1,
        )
        self.workspace_entry.pack(side="left", fill="x", expand=True, ipady=7)
        Tooltip(self.workspace_entry, "المسار الكامل لمساحة عمل الوكيل.", self.colors)
        self.browse_button = tk.Button(
            workspace_row,
            text="اختيار",
            command=self.choose_workspace,
            bg=COLORS["fog"],
            fg=COLORS["ink"],
            relief="flat",
            padx=9,
            pady=7,
            cursor="hand2",
        )
        self.browse_button.pack(side="right", padx=(8, 0))
        self.open_folder_button = tk.Button(
            workspace_row,
            text="📂",
            command=self.open_workspace_folder,
            bg=COLORS["fog"],
            fg=COLORS["ink"],
            relief="flat",
            padx=8,
            pady=7,
            cursor="hand2",
        )
        self.open_folder_button.pack(side="right", padx=(8, 0))
        Tooltip(self.open_folder_button, "افتح مجلد مساحة العمل في مستكشف الملفات.", self.colors)

        self._field_label(parent, "الوضع")
        self.mode_combo = ttk.Combobox(
            parent,
            textvariable=self.mode_var,
            values=tuple(MODE_LABELS),
            state="readonly",
        )
        self.mode_combo.pack(fill="x", pady=(0, 8))
        self.mode_combo.bind("<<ComboboxSelected>>", self._update_mode_ui)
        tk.Label(
            parent,
            textvariable=self.mode_note_var,
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            justify="right",
            anchor="e",
            wraplength=245,
        ).pack(fill="x", pady=(0, 14))

        self.delegation_check = tk.Checkbutton(
            parent,
            text="مسار مفوّض: تنفيذ ← مراجعة ← اختبارات",
            variable=self.delegation_var,
            command=self._configuration_changed,
            bg=COLORS["surface"],
            fg=COLORS["ink"],
            activebackground=COLORS["surface"],
            anchor="e",
            justify="right",
        )
        self.delegation_check.pack(fill="x", pady=(0, 14))
        Tooltip(
            self.delegation_check,
            "ينشئ مراجعًا مستقلًا ويمنع الاعتماد حتى تنجح الفحوصات.",
            self.colors,
        )

        self._field_label(parent, "اسم الجلسة · اختياري (مثال: تحسين واجهة التطبيق)")
        self.session_entry = self._entry(parent, self.session_var)
        self.clear_session_button = tk.Button(
            parent,
            text="مسح ذاكرة الجلسة",
            command=self.clear_session_memory,
            bg=COLORS["secondary_bg"],
            fg=COLORS["muted"],
            relief="flat",
            padx=10,
            pady=6,
            cursor="hand2",
        )
        self.clear_session_button.pack(fill="x", pady=(0, 8))

        self.apply_button = tk.Button(
            parent,
            text="تطبيق وبدء",
            command=self.apply_configuration,
            bg=COLORS["teal"],
            fg="white",
            activebackground=COLORS["focus"],
            activeforeground="white",
            relief="flat",
            font=theme.ui_font(10, 'bold'),
            padx=14,
            pady=11,
            cursor="hand2",
        )
        self.apply_button.pack(fill="x", pady=(8, 14))
        tk.Frame(parent, bg=COLORS["surface"]).pack(fill="both", expand=True)
        self.privacy_card = tk.Frame(
            parent, bg=COLORS["soft_teal"], highlightthickness=1,
            highlightbackground=COLORS["line"], padx=14, pady=13,
        )
        self.privacy_card.pack(fill="x", pady=(0, 10))
        tk.Label(
            self.privacy_card, text="◈  خصوصيتك أولًا", bg=COLORS["soft_teal"],
            fg=COLORS["ink"], font=theme.ui_font(11, "bold"), anchor="e",
        ).pack(fill="x")
        tk.Label(
            self.privacy_card,
            text="تبقى المحادثة محليًا؛ وأي تكامل شبكي يحتاج موافقتك.",
            bg=COLORS["soft_teal"], fg=COLORS["muted"], justify="right",
            anchor="e", wraplength=235, font=theme.ui_font(9),
        ).pack(fill="x", pady=(7, 0))
        self.permissions_button = tk.Button(
            parent, text="⚙  إدارة الصلاحيات",
            command=lambda: self._select_tab(self.integrations_page),
            bg=COLORS["secondary_bg"], activebackground=COLORS["elevated"],
            fg=COLORS["ink"], padx=12, pady=10, anchor="center",
            font=theme.ui_font(9, "bold"), relief="flat", cursor="hand2",
        )
        self.permissions_button.pack(fill="x")

    @staticmethod
    def _field_label(parent, text):
        tk.Label(
            parent,
            text=text,
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            anchor="e",
            font=theme.ui_font(9, 'bold'),
        ).pack(fill="x", pady=(0, 5))

    @staticmethod
    def _entry(parent, variable, justify="right"):
        entry = tk.Entry(
            parent,
            textvariable=variable,
            justify=justify,
            relief="solid",
            borderwidth=1,
        )
        entry.pack(fill="x", ipady=7, pady=(0, 14))
        return entry

    def _build_main(self, parent):
        # إطار مؤطّر بخلفية اللوحة حتى تبدو التبويبات ومحتواها داخل مربع واحد.
        board = tk.Frame(
            parent,
            bg=COLORS["surface"],
            highlightthickness=1,
            highlightbackground=COLORS["line"],
        )
        board.grid(row=0, column=0, sticky="nsew")
        board.grid_rowconfigure(1, weight=1)
        board.grid_columnconfigure(0, weight=1)
        self.board = board

        # شريط تبويبات مخصّص موسّط: نُخفي تبويبات ttk الأصلية (المحاذاة لليمين
        # دائمًا) ونضع أزرارًا في منتصف الشريط تتحكم بصفحات الدفتر نفسها.
        tab_bar = tk.Frame(board, bg=COLORS["surface"])
        tab_bar.grid(row=0, column=0, sticky="ew", pady=(8, 0))
        tab_center = tk.Frame(tab_bar, bg=COLORS["surface"])
        tab_center.pack(anchor="e", padx=8)

        notebook = ttk.Notebook(board, style="Center.TNotebook")
        notebook.grid(row=1, column=0, sticky="nsew", padx=6, pady=(4, 6))
        self.chat_page = tk.Frame(notebook, bg=COLORS["surface"])
        self.changes_page = tk.Frame(notebook, bg=COLORS["surface"])
        self.skills_page = tk.Frame(notebook, bg=COLORS["surface"])
        self.memory_page = tk.Frame(notebook, bg=COLORS["surface"])
        self.integrations_page = tk.Frame(notebook, bg=COLORS["surface"])
        notebook.add(self.integrations_page, text="التكاملات  🗄")
        notebook.add(self.memory_page, text="الذاكرة  ◈")
        notebook.add(self.skills_page, text="المهارات  🧩")
        notebook.add(self.changes_page, text="التغييرات  🔀")
        notebook.add(self.chat_page, text="المحادثة  💬")
        self.notebook = notebook

        self._tab_buttons = {}
        self._tab_underlines = {}
        for page, label in (
            (self.chat_page, "المحادثة  💬"),
            (self.changes_page, "التغييرات  🔀"),
            (self.skills_page, "المهارات  🧩"),
            (self.memory_page, "الذاكرة  ◈"),
            (self.integrations_page, "التكاملات  🗄"),
        ):
            cell = tk.Frame(tab_center, bg=COLORS["surface"])
            cell.pack(side="right", padx=4)
            button = tk.Button(
                cell,
                text=label,
                command=lambda target=page: self._select_tab(target),
                bg=COLORS["surface"],
                fg=COLORS["muted"],
                relief="flat",
                borderwidth=0,
                font=theme.ui_font(10, "bold"),
                padx=16,
                pady=7,
                cursor="hand2",
            )
            button.pack()
            # مؤشّر خط سفلي بلون الأساسي يظهر تحت التبويب النشط فقط.
            underline = tk.Frame(cell, height=2, bg=COLORS["surface"])
            underline.pack(fill="x", pady=(3, 0))
            self._tab_buttons[str(page)] = button
            self._tab_underlines[str(page)] = underline
        notebook.bind("<<NotebookTabChanged>>", self._sync_tab_buttons)
        notebook.select(self.chat_page)
        self._sync_tab_buttons()
        chat_page = self.chat_page
        changes_page = self.changes_page

        chat_page.grid_rowconfigure(2, weight=1)
        chat_page.grid_columnconfigure(0, weight=1)
        self.thought_header = tk.Frame(chat_page, bg=COLORS["fog"], padx=14, pady=7)
        self.thought_header.grid(row=0, column=0, sticky="ew")
        self.thought_button = tk.Button(
            self.thought_header,
            text="تفكير الوكيل [عرض]",
            command=self.toggle_thought,
            bg=COLORS["fog"],
            fg=COLORS["muted"],
            relief="flat",
            cursor="hand2",
        )
        self.thought_button.pack(side="right")
        tk.Label(
            self.thought_header,
            text="✦",
            bg=COLORS["fog"],
            fg=COLORS["teal"],
            font=theme.ui_font(10, "bold"),
        ).pack(side="right", padx=(0, 8))
        self.thought_text = ScrolledText(
            chat_page,
            height=5,
            wrap="word",
            state="disabled",
            bg=COLORS["fog"],
            fg=COLORS["muted"],
            relief="flat",
            padx=18,
            pady=10,
            font=theme.thought_font(),
        )
        self.thought_text.tag_configure(
            "thought",
            foreground=COLORS["muted"],
            font=theme.thought_font(),
            lmargin1=14,
            lmargin2=14,
        )
        self.transcript = ScrolledText(
            chat_page,
            wrap="word",
            bg=COLORS["surface"],
            fg=COLORS["ink"],
            relief="flat",
            padx=32,
            pady=22,
            spacing3=9,
            font=theme.ui_font(15),  # خط يدعم العربية (mono لا يدعمها)؛ الكود يبقى mono عبر وسمه
        )
        self.transcript.grid(row=2, column=0, sticky="nsew")
        # قراءة فقط لكن قابلة للتحديد والنسخ: نمنع الكتابة عبر حاصر للمفاتيح
        # بدل تعطيل الودجت (المعطّل يمنع تحديد الفأرة فيتعذّر النسخ).
        self.transcript.bind("<Key>", self._block_transcript_edit)
        self.transcript.bind("<<Paste>>", lambda event: "break")
        self.transcript.bind("<Button-3>", lambda event: self._show_text_menu(event, self.transcript))
        self.transcript.bind("<Control-c>", self._copy_selection)
        self.transcript.bind("<Control-a>", self._select_all_transcript)
        self.transcript.tag_configure(
            "user",
            justify="right",
            lmargin1=40,
            lmargin2=40,
            rmargin=20,
            background=COLORS["soft_teal"],
            foreground=COLORS["teal"],
            spacing1=6,
            spacing3=8,
        )
        self.transcript.tag_configure(
            "code",
            font=theme.mono_font(10),
            background=COLORS["fog"],
            foreground=COLORS["ink"],
            rmargin=24,
            spacing1=4,
            spacing3=4,
        )
        self.transcript.tag_configure(
            "assistant",
            justify="right",
            lmargin1=20,
            lmargin2=20,
            rmargin=20,
            foreground=COLORS["ink"],
            spacing1=6,
            spacing3=8,
        )
        self.transcript.tag_configure(
            "error",
            justify="right",
            lmargin1=20,
            lmargin2=20,
            rmargin=20,
            background=COLORS["soft_danger"],
            foreground=COLORS["danger"],
            spacing1=6,
            spacing3=8,
        )
        self.transcript.tag_configure(
            "tool",
            foreground=COLORS["muted"],
            font=theme.tool_line_font(),
        )
        self.transcript.tag_configure(
            "msg_heading",
            font=theme.ui_font(11, "bold"),
            spacing1=10,
            spacing3=4,
        )
        self.transcript.tag_configure(
            "msg_bullet",
            lmargin1=20,
            lmargin2=32,
            rmargin=20,
            spacing1=3,
            spacing3=3,
        )
        # حالة فراغ في المنتصف تظهر ما دام لا توجد رسائل (تُخفى عند أول رسالة).
        self.empty_state = tk.Frame(chat_page, bg=COLORS["surface"])
        self.empty_state.grid(row=2, column=0, sticky="nsew")
        center = tk.Frame(self.empty_state, bg=COLORS["surface"])
        center.place(relx=0.5, rely=0.46, anchor="center")
        tk.Label(
            center, text="◆", bg=COLORS["surface"], fg=COLORS["teal"],
            font=theme.display_font(38),
        ).pack()
        tk.Label(
            center, text="كيف يمكنني مساعدتك؟", bg=COLORS["surface"], fg=COLORS["ink"],
            font=theme.display_font(24),
        ).pack(pady=(14, 6))
        tk.Label(
            center,
            text="اختر مساحة العمل والوضع، ثم اكتب المهمة التي تريد تنفيذها.",
            bg=COLORS["surface"], fg=COLORS["muted"], font=theme.ui_font(12),
            justify="center", wraplength=520,
        ).pack()
        cards = tk.Frame(center, bg=COLORS["surface"])
        cards.pack(pady=(28, 0))
        self.starter_cards = []
        for icon, title, detail, color in (
            ("⚙", "أتمتة المهام", "تنفيذ مهام متعددة\nعلى جهازك", COLORS["teal"]),
            ("☼", "بناء المعرفة", "البحث والتلخيص\nوتوليد الأفكار", COLORS["copper"]),
            ("</>", "مساعدة البرمجة", "كتابة وتعديل الأكواد\nوإصلاح الأخطاء", COLORS["info"]),
            ("▣", "تحليل الملفات", "قراءة وفهم الملفات\nداخل مشروعك", "#B96CFF"),
        ):
            card = tk.Frame(
                cards, width=168, height=190, bg=COLORS["secondary_bg"],
                highlightthickness=1, highlightbackground=COLORS["line"],
                padx=12, pady=10,
            )
            card.pack(side="right", padx=6)
            card.pack_propagate(False)
            tk.Label(
                card, text=icon, bg=COLORS["soft_teal"], fg=color,
                font=theme.ui_font(17, "bold"), padx=7, pady=3,
            ).pack()
            tk.Label(
                card, text=title, bg=COLORS["secondary_bg"], fg=COLORS["ink"],
                font=theme.ui_font(10, "bold"),
            ).pack(pady=(7, 3))
            tk.Label(
                card, text=detail, bg=COLORS["secondary_bg"], fg=COLORS["muted"],
                font=theme.ui_font(8), justify="center",
            ).pack()
            self.starter_cards.append(card)
        self._empty_state_visible = True

        composer = tk.Frame(chat_page, bg=COLORS["fog"], padx=14, pady=8)
        composer.grid(row=3, column=0, sticky="ew")
        composer.grid_columnconfigure(0, weight=1)
        # Composer محسّن: حقل متعدد الأسطر بحدّ مرتفع، Placeholder على طبقة
        # علوية لا تلوّث المحتوى، وزر إرسال أكبر مدمج معه.
        composer_box = tk.Frame(
            composer,
            bg=COLORS["surface"],
            highlightthickness=1,
            highlightbackground=COLORS["line"],
            highlightcolor=COLORS["focus"],
        )
        composer_box.grid(row=1, column=0, columnspan=2, sticky="ew")
        composer_box.grid_columnconfigure(0, weight=1)
        self.prompt_text = tk.Text(
            composer_box,
            height=3,
            wrap="word",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            bg=COLORS["surface"],
            fg=COLORS["ink"],
            insertbackground=COLORS["teal"],
            padx=16,
            pady=12,
            undo=True,
            font=theme.ui_font(13),  # يدعم العربية بوضوح
        )
        self.prompt_text.grid(row=0, column=0, sticky="ew")
        self.prompt_text.bind("<Control-Return>", self._send_shortcut)
        self.prompt_text.bind("<KeyRelease>", self._update_composer_state)
        self.prompt_text.bind("<FocusIn>", self._update_composer_state)
        self.prompt_text.bind("<FocusOut>", self._update_composer_state)
        self.prompt_text.bind("<Button-3>", lambda event: self._show_text_menu(event, self.prompt_text, editable=True))
        # Placeholder كطبقة علوية داخل الحقل (لا يدخل ضمن get()).
        self.prompt_placeholder = tk.Label(
            self.prompt_text,
            text="اكتب مهمة للوكيل…",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=theme.ui_font(13),
            anchor="e",
        )
        self.prompt_placeholder.place(relx=1.0, x=-16, y=12, anchor="ne")
        self.prompt_placeholder.bind("<Button-1>", lambda e: self.prompt_text.focus_set())
        # زر إرسال أكبر وأوضح مدمج مع صندوق الكتابة.
        self.send_button = tk.Button(
            composer_box,
            text="↑",
            command=self.send,
            bg=COLORS["teal"],
            fg=COLORS["fog"],
            activebackground=COLORS["focus"],
            activeforeground=COLORS["fog"],
            relief="flat",
            font=theme.ui_font(16, 'bold'),
            width=3,
            padx=6,
            pady=6,
            cursor="hand2",
        )
        self.send_button.grid(row=0, column=1, sticky="se", padx=8, pady=8)
        attachment_row = tk.Frame(composer, bg=COLORS["fog"])
        attachment_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        attachment_row.grid_columnconfigure(0, weight=1)
        tk.Label(
            attachment_row,
            textvariable=self.image_note_var,
            bg=COLORS["fog"],
            fg=COLORS["muted"],
            anchor="e",
        ).grid(row=0, column=0, sticky="ew")
        self.clear_images_button = tk.Button(
            attachment_row,
            text="مسح",
            command=self.clear_images,
            bg=COLORS["fog"],
            fg=COLORS["muted"],
            relief="flat",
            cursor="hand2",
        )
        self.clear_images_button.grid(row=0, column=1, padx=8)
        self.images_button = tk.Button(
            attachment_row,
            text="إرفاق صور",
            command=self.choose_images,
            bg=COLORS["soft_teal"],
            fg=COLORS["teal"],
            relief="flat",
            padx=12,
            pady=5,
            cursor="hand2",
        )
        self.images_button.grid(row=0, column=2)
        quick_row = tk.Frame(composer, bg=COLORS["fog"])
        quick_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for label, prompt in QUICK_PROMPTS:
            tk.Button(
                quick_row,
                text=label,
                command=lambda text=prompt: self.insert_quick_prompt(text),
                bg=COLORS["soft_teal"],
                fg=COLORS["teal"],
                activebackground=COLORS["teal"],
                activeforeground="white",
                relief="flat",
                borderwidth=0,
                padx=12,
                pady=5,
                cursor="hand2",
            ).pack(side="right", padx=(8, 0))

        changes_page.grid_rowconfigure(2, weight=1)
        changes_page.grid_columnconfigure(0, weight=1)

        delegation_band = tk.Frame(
            changes_page,
            bg=COLORS["fog"],
            padx=12,
            pady=12,
        )
        delegation_band.grid(row=0, column=0, sticky="ew")
        self.delegation_worker_var = tk.StringVar(value="بانتظار مهمة")
        self.delegation_review_var = tk.StringVar(value="لم تبدأ المراجعة")
        self.delegation_tests_var = tk.StringVar(value="لم تبدأ الاختبارات")
        self.delegation_accept_var = tk.StringVar(value="الاعتماد مقفل")
        for title, variable, marker in (
            ("التنفيذ", self.delegation_worker_var, "01"),
            ("المراجعة", self.delegation_review_var, "02"),
            ("الاختبارات", self.delegation_tests_var, "03"),
            ("الاعتماد", self.delegation_accept_var, "04"),
        ):
            card = tk.Frame(
                delegation_band,
                bg=COLORS["surface"],
                highlightthickness=1,
                highlightbackground=COLORS["line"],
                padx=12,
                pady=9,
            )
            card.pack(side="right", fill="x", expand=True, padx=4)
            tk.Label(
                card,
                text=marker,
                bg=COLORS["surface"],
                fg=COLORS["copper"],
                font=theme.mono_font(8),
            ).pack(side="right", padx=(8, 0))
            copy = tk.Frame(card, bg=COLORS["surface"])
            copy.pack(side="right", fill="x", expand=True)
            tk.Label(
                copy,
                text=title,
                bg=COLORS["surface"],
                fg=COLORS["ink"],
                font=theme.ui_font(9, "bold"),
                anchor="e",
            ).pack(fill="x")
            tk.Label(
                copy,
                textvariable=variable,
                bg=COLORS["surface"],
                fg=COLORS["muted"],
                font=theme.ui_font(8),
                anchor="e",
            ).pack(fill="x")

        changes_header = tk.Frame(changes_page, bg=COLORS["surface"], padx=18, pady=14)
        changes_header.grid(row=1, column=0, sticky="ew")
        tk.Label(
            changes_header,
            text="تعديلات هذه العملية",
            bg=COLORS["surface"],
            fg=COLORS["ink"],
            font=theme.ui_font(12, 'bold'),
        ).pack(side="right")
        self.refresh_button = tk.Button(
            changes_header,
            text="تحديث",
            command=self.refresh_changes,
            bg=COLORS["fog"],
            fg=COLORS["ink"],
            relief="flat",
            padx=14,
            pady=7,
            cursor="hand2",
        )
        self.refresh_button.pack(side="left")
        self.undo_button = tk.Button(
            changes_header,
            text="تراجع عن آخر تعديل",
            command=self.undo_last,
            bg=COLORS["soft_copper"],
            fg=COLORS["copper"],
            relief="flat",
            padx=14,
            pady=7,
            cursor="hand2",
        )
        self.undo_button.pack(side="left", padx=(8, 0))
        self.accept_task_button = tk.Button(
            changes_header,
            text="اعتماد النتيجة",
            command=self.accept_delegated_task,
            bg=COLORS["teal"],
            fg="white",
            activebackground=COLORS["focus"],
            activeforeground="white",
            relief="flat",
            padx=14,
            pady=7,
            state="disabled",
            cursor="hand2",
        )
        self.accept_task_button.pack(side="left", padx=(8, 0))
        self.changes_text = ScrolledText(
            changes_page,
            wrap="none",
            state="disabled",
            font=theme.mono_font(10),
            bg=COLORS["fog"],
            fg=COLORS["ink"],
            relief="flat",
            padx=16,
            pady=16,
        )
        self.changes_text.grid(row=2, column=0, sticky="nsew")
        self.changes_text.bind("<Button-3>", lambda event: self._show_text_menu(event, self.changes_text))
        self.changes_text.tag_configure(
            "diff_add", foreground="#137333", background="#E6F4EA"
        )
        self.changes_text.tag_configure(
            "diff_remove", foreground="#B3261E", background="#FCE8E6"
        )
        self.changes_text.tag_configure("diff_hunk", foreground="#3730A3")
        self._set_text(
            self.changes_text,
            "لا توجد تغييرات بعد. ستظهر هنا معاينة الملفات المعدلة بعد موافقتك.",
        )

        self.skill_panel = SkillsPanel(
            self.skills_page, catalog=self._skill_catalog, colors=self.colors
        )
        self.skills_badge_var.set(f"المهارات: {self.skill_panel.count} متاحة")
        self.memory_panel = MemoryPanel(
            self.memory_page,
            lambda: getattr(self.controller.agent, "cognitive_memory", None),
            self.colors,
        )
        self._build_integrations(self.integrations_page)

    def _build_integrations(self, parent):
        self.integrations_canvas = tk.Canvas(
            parent,
            bg=COLORS["surface"],
            highlightthickness=0,
            borderwidth=0,
        )
        self.integrations_scrollbar = ttk.Scrollbar(
            parent,
            orient="vertical",
            command=self.integrations_canvas.yview,
        )
        self.integrations_canvas.configure(
            yscrollcommand=self.integrations_scrollbar.set
        )
        self.integrations_scrollbar.pack(side="left", fill="y")
        self.integrations_canvas.pack(side="right", fill="both", expand=True)
        panel = tk.Frame(
            self.integrations_canvas,
            bg=COLORS["surface"],
            padx=28,
            pady=24,
        )
        self.integrations_panel = panel
        panel_window = self.integrations_canvas.create_window(
            (0, 0), window=panel, anchor="nw"
        )
        panel.bind(
            "<Configure>",
            lambda _event: self.integrations_canvas.configure(
                scrollregion=self.integrations_canvas.bbox("all")
            ),
        )
        self.integrations_canvas.bind(
            "<Configure>",
            lambda event: self.integrations_canvas.itemconfigure(
                panel_window, width=event.width
            ),
        )
        for widget in (self.integrations_canvas, panel):
            widget.bind(
                "<MouseWheel>",
                lambda event: self.integrations_canvas.yview_scroll(
                    -1 if event.delta > 0 else 1, "units"
                ),
            )
        tk.Label(
            panel,
            text="النموذج والتكاملات",
            bg=COLORS["surface"],
            fg=COLORS["ink"],
            font=theme.ui_font(15, 'bold'),
            anchor="e",
        ).pack(fill="x", pady=(0, 18))
        self._field_label(panel, "النموذج المحلي")
        model_row = tk.Frame(panel, bg=COLORS["surface"])
        model_row.pack(fill="x", pady=(0, 14))
        # قائمة منسدلة تُملأ تلقائيًا من نماذج LM Studio المثبتة، وتبقى قابلة
        # للكتابة لإدخال معرّف مخصّص. زر «تحديث» يعيد جلب القائمة.
        self.model_combo = ttk.Combobox(
            model_row, textvariable=self.model_var, values=(), state="normal"
        )
        self.model_combo.pack(side="left", fill="x", expand=True, ipady=3)
        self.model_combo.bind("<<ComboboxSelected>>", lambda event: self._configuration_changed())
        self.refresh_models_button = tk.Button(
            model_row,
            text="تحديث",
            command=self.refresh_models,
            bg=COLORS["fog"],
            fg=COLORS["ink"],
            relief="flat",
            padx=12,
            pady=7,
            cursor="hand2",
        )
        self.refresh_models_button.pack(side="right", padx=(8, 0))
        self._field_label(panel, "Harness النموذج")
        self.harness_combo = ttk.Combobox(
            panel,
            textvariable=self.harness_var,
            values=tuple(HARNESS_LABELS),
            state="readonly",
        )
        self.harness_combo.pack(fill="x", pady=(0, 4), ipady=3)
        self.harness_combo.bind(
            "<<ComboboxSelected>>", lambda event: self._configuration_changed()
        )
        tk.Label(
            panel,
            text="اختر DeepSeek عند تشغيل نموذج DeepSeek محليًا عبر LM Studio.",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            anchor="e",
            justify="right",
        ).pack(fill="x", pady=(0, 14))
        self._field_label(panel, "عنوان LM Studio")
        self.base_url_entry = self._entry(panel, self.base_url_var, justify="left")
        self._field_label(panel, "ملف إعداد MCP · اختياري")
        row = tk.Frame(panel, bg=COLORS["surface"])
        row.pack(fill="x")
        self.mcp_entry = tk.Entry(
            row,
            textvariable=self.mcp_config_var,
            justify="left",
            relief="solid",
            borderwidth=1,
        )
        self.mcp_entry.pack(side="left", fill="x", expand=True, ipady=7)
        self.mcp_button = tk.Button(
            row,
            text="اختيار",
            command=self.choose_mcp_config,
            bg=COLORS["fog"],
            fg=COLORS["ink"],
            relief="flat",
            padx=12,
            pady=7,
            cursor="hand2",
        )
        self.mcp_button.pack(side="right", padx=(8, 0))
        self.mcp_servers_frame = tk.Frame(panel, bg=COLORS["surface"])
        self.mcp_servers_frame.pack(fill="x", pady=(8, 0))
        tk.Label(
            panel,
            text="خادم MCP غير معزول وقد يقرأ أو يعدّل أو يحذف بيانات ويصل للشبكة؛ ستظهر موافقة قبل تشغيله وقبل كل أداة.",
            bg=COLORS["surface"],
            fg=COLORS["danger"],
            justify="right",
            anchor="e",
            wraplength=650,
        ).pack(fill="x", pady=(8, 24))
        self._field_label(panel, "عزل أوامر المضيف · اختياري")
        self.container_combo = ttk.Combobox(
            panel,
            textvariable=self.container_engine_var,
            values=("", "docker", "podman"),
            state="readonly",
        )
        self.container_combo.pack(fill="x", pady=(0, 8))
        self.container_combo.bind("<<ComboboxSelected>>", self._configuration_changed)
        self.memory_check = tk.Checkbutton(
            panel,
            text="تفعيل ذاكرة دلالية دائمة لهذه الجلسة",
            variable=self.semantic_memory_var,
            command=self._configuration_changed,
            bg=COLORS["surface"],
            fg=COLORS["ink"],
            activebackground=COLORS["surface"],
            anchor="e",
        )
        self.memory_check.pack(fill="x")
        tk.Label(
            panel,
            text="تتطلب اسم جلسة، وتحفظ نص المحادثة محليًا بصيغة قابلة للقراءة. لا تستخدمها للأسرار.",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            justify="right",
            anchor="e",
            wraplength=650,
        ).pack(fill="x", pady=(6, 0))
        self._field_label(panel, "ذاكرة المشروع والتعلّم المحلي")
        for text, variable in (
            ("تفعيل ذاكرة المشروع", self.memory_enabled_var),
            ("التعلّم من نتائج التنفيذ", self.experience_learning_var),
            ("إنشاء انعكاس منظم", self.reflection_var),
            ("ترقية الأنماط إلى مهارات", self.skill_learning_var),
            ("الرسم المعرفي المحلي", self.knowledge_graph_var),
            ("دمج الذكريات المتطابقة", self.memory_consolidation_var),
        ):
            check = tk.Checkbutton(
                panel,
                text=text,
                variable=variable,
                command=self._configuration_changed,
                bg=COLORS["surface"],
                fg=COLORS["ink"],
                activebackground=COLORS["surface"],
                anchor="e",
            )
            check.pack(fill="x", pady=(4, 0))
            self._memory_feature_checks.append(check)

    def _selected_mode(self):
        return MODE_LABELS.get(self.mode_var.get(), "")

    def _read_config(self):
        return DesktopConfig.parse(
            self.workspace_var.get(),
            self._selected_mode(),
            session=self.session_var.get(),
            model=self.model_var.get(),
            harness=HARNESS_LABELS.get(self.harness_var.get(), ""),
            base_url=self.base_url_var.get(),
            mcp_config=self.mcp_config_var.get(),
            semantic_memory=self.semantic_memory_var.get(),
            container_engine=self.container_engine_var.get(),
            memory_enabled=self.memory_enabled_var.get(),
            experience_learning_enabled=self.experience_learning_var.get(),
            reflection_enabled=self.reflection_var.get(),
            skill_learning_enabled=self.skill_learning_var.get(),
            knowledge_graph_enabled=self.knowledge_graph_var.get(),
            memory_consolidation_enabled=self.memory_consolidation_var.get(),
        )

    def apply_configuration(self):
        if self.controller.busy:
            self._set_status("انتظر انتهاء الطلب الحالي", "copper")
            return False
        try:
            config = self._read_config()
            history = self.controller.configure(config)
        except (OSError, RuntimeError, ToolError, ValueError) as error:
            self._set_status(safe_terminal_text(error), "danger")
            return False
        self._active_config = config
        self._clear_transcript()
        if history:
            for message in history:
                role = "user" if message.get("role") == "user" else "assistant"
                self._append_chat(role, message.get("content", ""))
        else:
            self._append_chat("assistant", self._smart_briefing(config.workspace))
        self._set_status("جاهز", "teal")
        self.memory_panel.refresh()
        self.save_settings()
        self.check_connection()
        self.prompt_text.focus_set()
        return True

    def choose_workspace(self):
        selected = filedialog.askdirectory(initialdir=self.workspace_var.get() or str(Path.cwd()))
        if selected:
            self.workspace_var.set(selected)
            self._active_config = None

    def clear_session_memory(self):
        if self.controller.busy:
            self._set_status("انتظر انتهاء الطلب الحالي", "copper")
            return False
        session_name = self.session_var.get().strip()
        workspace = self.workspace_var.get().strip()
        if not session_name or not workspace:
            self._set_status("اختر مساحة عمل واكتب اسم الجلسة أولًا", "copper")
            return False
        if not messagebox.askyesno(
            "مسح ذاكرة الجلسة",
            "سيُحذف سجل هذه الجلسة المحلي من التطبيق. متابعة؟",
            parent=self.root,
        ):
            return False
        try:
            store = SessionStore(workspace, session_name)
            store.clear()
            agent = self.controller.agent
            memory = getattr(agent, "memory", None)
            forget_source = getattr(memory, "forget_source", None)
            if callable(forget_source):
                forget_source(f"session:{session_name}")
            if agent is not None:
                agent.history = [agent.history[0]]
                agent._archive = []
            self._clear_transcript()
            self._append_chat("assistant", self._smart_briefing(workspace))
        except (OSError, RuntimeError, ToolError, ValueError) as error:
            self._set_status(safe_terminal_text(error), "danger")
            return False
        self._set_status("تم مسح ذاكرة الجلسة", "teal")
        return True

    def open_workspace_folder(self):
        """افتح مجلد مساحة العمل في مستكشف الملفات (إن كان موجودًا)."""
        path = self.workspace_var.get().strip()
        try:
            target = Path(path).expanduser()
            if not target.is_dir():
                self._set_status("مساحة العمل غير موجودة", "copper")
                return False
            os.startfile(str(target))  # ويندوز فقط
        except (OSError, AttributeError, ValueError) as error:
            self._set_status(f"تعذر فتح المجلد: {safe_terminal_text(error)}", "danger")
            return False
        return True

    def check_connection(self, quiet=False):
        base = self.base_url_var.get().strip()
        results = self._probe_results
        self.connection_var.set("LM Studio: جارٍ الفحص…")
        self._probe_thread = threading.Thread(
            target=lambda: results.put((*probe_model_server(base), quiet)),
            name="local-agent-probe",
            daemon=True,
        )
        self._probe_thread.start()
        return True

    def refresh_models(self):
        """اجلب نماذج LM Studio المتاحة في خيط خلفي واملأ القائمة المنسدلة."""
        base = self.base_url_var.get().strip()
        results = self._models_results
        self._models_thread = threading.Thread(
            target=lambda: results.put(list_models(base)),
            name="local-agent-models",
            daemon=True,
        )
        self._models_thread.start()
        return True

    def _drain_models_results(self):
        while True:
            try:
                models = self._models_results.get_nowait()
            except Empty:
                return
            if not models or self._closing or self._destroyed:
                continue
            self.model_combo.configure(values=tuple(models))
            # إن لم يكن النموذج الحالي ضمن القائمة، أبقِ اختيار المستخدم كما هو.
            current = self.model_var.get().strip()
            if current not in models and not current:
                self.model_var.set(models[0])

    def _drain_boot_results(self):
        while True:
            try:
                self._boot_results.get_nowait()
            except Empty:
                return
            if not self._closing and not self._destroyed:
                self.check_connection(quiet=True)
                self.refresh_models()

    def _drain_probe_results(self):
        while True:
            try:
                ok, detail, quiet = self._probe_results.get_nowait()
            except Empty:
                return
            if ok:
                self.connection_var.set("LM Studio: متصل")
            else:
                self.connection_var.set("LM Studio: لا استجابة")
                if not self._closing and not quiet:
                    self._append_chat(
                        "error", CONNECTION_GUIDANCE.format(detail=detail)
                    )

    def _smart_briefing(self, workspace):
        try:
            stats = describe_workspace(workspace)
            return build_workspace_briefing(stats)
        except (OSError, ValueError):
            return "الإعدادات جاهزة. اكتب طلبك الآن."

    def save_settings(self):
        if self._settings is None:
            return False
        return self._settings.save({
            "dark_theme": self._dark_theme,
            "workspace": self.workspace_var.get(),
            "mode": self.mode_var.get(),
            "session": self.session_var.get(),
            "model": self.model_var.get(),
            "harness": HARNESS_LABELS.get(self.harness_var.get(), "standard"),
            "base_url": self.base_url_var.get(),
            "delegation_enabled": self.delegation_var.get(),
            "memory_enabled": self.memory_enabled_var.get(),
            "experience_learning_enabled": self.experience_learning_var.get(),
            "reflection_enabled": self.reflection_var.get(),
            "skill_learning_enabled": self.skill_learning_var.get(),
            "knowledge_graph_enabled": self.knowledge_graph_var.get(),
            "memory_consolidation_enabled": self.memory_consolidation_var.get(),
        })

    def choose_images(self):
        selected = filedialog.askopenfilenames(
            title="اختر حتى أربع صور",
            filetypes=(("Images", "*.png *.jpg *.jpeg *.webp *.gif"), ("All files", "*.*")),
        )
        if selected:
            if len(selected) > 4:
                self._set_status("يمكن إرفاق أربع صور كحد أقصى", "danger")
                return
            self.pending_images = list(selected)
            names = "، ".join(Path(path).name for path in self.pending_images)
            self.image_note_var.set(names)

    def clear_images(self):
        self.pending_images = []
        self.image_note_var.set("لا توجد صور مرفقة")

    def _install_drop_support(self):
        register = getattr(self.root, "drop_target_register", None)
        bind = getattr(self.root, "dnd_bind", None)
        if callable(register) and callable(bind):
            try:
                register(DND_FILES)
                bind("<<Drop>>", self._handle_drop)
            except tk.TclError:
                pass

    def _handle_drop(self, event):
        raw = str(getattr(event, "data", ""))
        try:
            raw_paths = (raw,) if Path(raw).exists() else self.root.tk.splitlist(raw)
        except (AttributeError, tk.TclError):
            raw_paths = (raw,)
        paths = [Path(path).expanduser() for path in raw_paths if path]
        images = [
            path
            for path in paths
            if path.is_file() and path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        ]
        if images:
            combined = list(dict.fromkeys([*self.pending_images, *map(str, images)]))
            if len(combined) > 4:
                self._set_status("يمكن إرفاق أربع صور كحد أقصى", "danger")
            else:
                self.pending_images = combined
                self.image_note_var.set("، ".join(Path(path).name for path in combined))
        workspace = next((path for path in paths if path.is_dir()), None)
        if workspace is None:
            other = next((path for path in paths if path.is_file() and path not in images), None)
            workspace = other.parent if other is not None else None
        if workspace is not None:
            self.workspace_var.set(str(workspace.resolve()))
            self._active_config = None
        return "break"

    def choose_mcp_config(self):
        selected = filedialog.askopenfilename(
            title="اختر ملف إعداد MCP",
            filetypes=(("JSON", "*.json"), ("All files", "*.*")),
        )
        if selected:
            self.mcp_config_var.set(selected)
            self._load_mcp_servers()
            self._configuration_changed()

    def _load_mcp_servers(self):
        for child in self.mcp_servers_frame.winfo_children():
            child.destroy()
        self._mcp_checks = []
        if self._mcp_editor is not None:
            self._mcp_editor.close()
            self._mcp_editor = None
        path = self.mcp_config_var.get().strip()
        if not path:
            return
        try:
            workspace = Path(self.workspace_var.get()).expanduser().resolve(strict=True)
            self._mcp_editor = MCPRegistry(path, lambda *_args: True, workspace)
            servers = self._mcp_editor.list_servers()
            self._mcp_editor.close()
            self._mcp_editor = None
            for server in servers:
                variable = tk.BooleanVar(value=not server["disabled"])
                check = tk.Checkbutton(
                    self.mcp_servers_frame,
                    text=f"{server['name']} · {server['type']}",
                    variable=variable,
                    command=lambda name=server["name"], value=variable: self._toggle_mcp_server(name, value),
                    bg=self.colors["surface"],
                    fg=self.colors["ink"],
                    activebackground=self.colors["surface"],
                    anchor="e",
                )
                check.pack(fill="x")
                self._mcp_checks.append(check)
        except (OSError, RuntimeError, ValueError) as error:
            self._set_status(f"تعذر قراءة خوادم MCP: {safe_terminal_text(error)}", "danger")

    def _toggle_mcp_server(self, name, variable):
        try:
            config = self._read_config()
            self._pending_mcp_toggle = (variable, not variable.get())
            if not self.controller.update_mcp_server(name, variable.get(), config):
                raise RuntimeError("تعذر بدء تحديث إعداد MCP")
            self._set_busy(True)
            self._set_status("بانتظار الموافقة على حفظ إعداد MCP…", "copper")
        except (OSError, RuntimeError, ValueError) as error:
            variable.set(not variable.get())
            self._pending_mcp_toggle = None
            self._set_status(f"تعذر حفظ إعداد MCP: {safe_terminal_text(error)}", "danger")

    def _configuration_changed(self):
        self._active_config = None

    def send(self):
        prompt = self.prompt_text.get("1.0", "end-1c").strip()
        if not prompt or self.controller.busy:
            return False
        try:
            config = self._read_config()
        except ValueError as error:
            self._set_status(safe_terminal_text(error), "danger")
            return False
        if self._active_config != config and not self.apply_configuration():
            return False
        images = tuple(self.pending_images)
        delegated = self.delegation_var.get() and config.mode != "read"
        submitter = (
            getattr(self.controller, "submit_delegated", None)
            if delegated
            else self.controller.submit
        )
        if not callable(submitter) or not submitter(prompt, images):
            return False
        visible_prompt = prompt + (f"\n[صورة مرفقة: {len(images)}]" if images else "")
        self._append_chat("user", visible_prompt)
        self._record_stat("requests")
        self.prompt_text.delete("1.0", "end")
        self.clear_images()
        self._clear_thought()
        self._streaming_answer_open = False
        self._stream_result = "pending"
        self._set_busy(True)
        self._set_status(
            "بدأ مسار التنفيذ والمراجعة والاختبارات…" if delegated else "الوكيل يعمل…",
            "copper",
        )
        return True

    def _send_shortcut(self, _event=None):
        self.send()
        return "break"

    def _update_composer_state(self, _event=None):
        """يدير Placeholder وتفعيل زر الإرسال حسب المحتوى والتركيز والانشغال."""
        placeholder = getattr(self, "prompt_placeholder", None)
        if placeholder is None:
            return
        text = self.prompt_text.get("1.0", "end-1c")
        has_text = bool(text.strip())
        try:
            focused = self.prompt_text.focus_get() is self.prompt_text
        except (tk.TclError, KeyError):
            focused = False
        try:
            if not has_text and not focused:
                placeholder.place(relx=1.0, x=-16, y=12, anchor="ne")
            else:
                placeholder.place_forget()
        except tk.TclError:
            pass
        busy = bool(getattr(self.controller, "busy", False))
        self.send_button.configure(state="normal" if (has_text and not busy) else "disabled")

    def refresh_changes(self):
        try:
            changes = self.controller.review_changes()
        except (RuntimeError, ToolError, ValueError) as error:
            changes = f"تعذر عرض التغييرات: {safe_terminal_text(error)}"
        self._set_diff_text(safe_terminal_text(changes))
        self.notebook.select(self.changes_page)

    def _handle_delegation(self, snapshot):
        if not isinstance(snapshot, dict):
            return False
        self._delegation_snapshot = dict(snapshot)
        phase = snapshot.get("phase", "")
        review = snapshot.get("review", {})
        tests = snapshot.get("tests", {})
        worker_labels = {
            "working": "● العامل ينفّذ الآن",
            "reviewing": "✓ اكتمل التنفيذ",
            "testing": "✓ اكتمل التنفيذ",
            "ready": "✓ اكتمل التنفيذ",
            "blocked": "✓ انتهى التنفيذ",
            "failed": "تعذر التنفيذ",
            "accepted": "✓ اكتمل التنفيذ",
        }
        self.delegation_worker_var.set(worker_labels.get(phase, "بانتظار مهمة"))
        review_status = review.get("status", "pending")
        review_summary = safe_terminal_text(review.get("summary", "")).splitlines()
        review_detail = review_summary[-1][:80] if review_summary else ""
        if review_status == "passed":
            self.delegation_review_var.set(f"✓ نجحت · {review_detail}".rstrip(" ·"))
        elif review_status == "failed":
            self.delegation_review_var.set(f"توقفت · {review_detail}".rstrip(" ·"))
        elif phase == "reviewing":
            self.delegation_review_var.set("● مراجعة مستقلة جارية")
        else:
            self.delegation_review_var.set("لم تبدأ المراجعة")
        test_status = tests.get("status", "pending")
        test_summary = safe_terminal_text(tests.get("summary", "")).splitlines()
        test_detail = test_summary[-1][:80] if test_summary else ""
        if test_status == "passed":
            self.delegation_tests_var.set(f"✓ نجحت · {test_detail}".rstrip(" ·"))
        elif test_status == "failed":
            self.delegation_tests_var.set(f"فشلت · {test_detail}".rstrip(" ·"))
        elif test_status == "skipped":
            self.delegation_tests_var.set(f"لم تُشغّل · {test_detail}".rstrip(" ·"))
        elif phase == "testing":
            self.delegation_tests_var.set("● الفحوصات قيد التشغيل")
        else:
            self.delegation_tests_var.set("لم تبدأ الاختبارات")
        if snapshot.get("accepted"):
            self.delegation_accept_var.set("✓ اعتمدت النتيجة")
        elif snapshot.get("can_accept"):
            self.delegation_accept_var.set("جاهزة لقرارك")
        elif phase in {"blocked", "failed"}:
            self.delegation_accept_var.set("مقفلة بسبب بوابة الجودة")
        else:
            self.delegation_accept_var.set("الاعتماد مقفل")
        self.accept_task_button.configure(
            state="normal" if snapshot.get("can_accept") and not self.controller.busy else "disabled"
        )
        if snapshot.get("diff"):
            self._set_diff_text(safe_terminal_text(snapshot["diff"]))
        return True

    def accept_delegated_task(self):
        try:
            snapshot = self.controller.accept_delegated_task()
        except (RuntimeError, ValueError) as error:
            self._set_status(safe_terminal_text(error), "danger")
            return False
        self._handle_delegation(snapshot)
        self._set_status("اعتمدت نتيجة المهمة", "teal")
        return True

    def undo_last(self):
        if self.controller.rollback():
            self._set_busy(True)
            self._set_status("بانتظار الموافقة على التراجع…", "copper")
            return True
        self._set_status("لا توجد نقطة استعادة متاحة", "copper")
        return False

    def _record_stat(self, key):
        self._stats[key] += 1
        self._update_stats()

    def _update_stats(self):
        stats = self._stats
        parts = [f"{stats['requests']} طلب"]
        if stats["tools"]:
            parts.append(f"{stats['tools']} أداة")
        if stats["completed"]:
            parts.append(f"{stats['completed']} اكتمل")
        stopped = stats["denied"] + stats["errors"]
        if stopped:
            parts.append(f"{stopped} متوقف")
        self.stats_var.set("الجلسة: " + " · ".join(parts))

    def insert_quick_prompt(self, prompt):
        self.prompt_text.insert("end", prompt)
        self.prompt_text.focus_set()
        self._update_composer_state()
        return True

    def export_chat(self):
        if not self._chat_log:
            self._set_status("لا توجد محادثة للتصدير", "copper")
            return False
        selected = filedialog.asksaveasfilename(
            title="تصدير المحادثة",
            defaultextension=".md",
            filetypes=(("Markdown", "*.md"), ("All files", "*.*")),
        )
        if not selected:
            return False
        target = Path(selected).expanduser()
        labels = {"user": "أنت", "assistant": "الوكيل", "error": "تنبيه"}
        lines = ["# محادثة الوكيل المحلي", ""]
        lines.append(f"- التاريخ: {datetime.now():%Y-%m-%d %H:%M}")
        lines.append(f"- مساحة العمل: {self.workspace_var.get() or '—'}")
        lines.append(f"- النموذج: {self.model_var.get() or '—'}")
        lines.append(f"- الوضع: {self.mode_var.get() or '—'}")
        lines.append("")
        lines.append("---")
        for role, content in self._chat_log:
            lines.extend(("", f"## {labels[role]}", "", content))
        lines.append("")
        try:
            target.write_text("\n".join(lines), encoding="utf-8")
        except OSError as error:
            self._set_status(f"تعذر التصدير: {safe_terminal_text(error)}", "danger")
            return False
        self._set_status(f"تم تصدير المحادثة إلى {target.name}", "teal")
        return True

    def open_command_palette(self, _event=None):
        if self._closing:
            return "break"
        CommandPalette(self.root, self._palette_actions(), self.colors)
        return "break"

    def _palette_actions(self):
        return (
            ("فحص اتصال LM Studio", self.check_connection),
            ("نسخ آخر رد من الوكيل", self.copy_last_answer),
            ("تصدير المحادثة إلى Markdown", self.export_chat),
            ("تبديل الوضع الداكن والفاتح", self.toggle_theme),
            ("فتح تبويب المحادثة", lambda: self._select_tab(self.chat_page)),
            ("فتح تبويب التغييرات", lambda: self._select_tab(self.changes_page)),
            ("فتح تبويب المهارات", lambda: self._select_tab(self.skills_page)),
            ("فتح تبويب الذاكرة", lambda: self._select_tab(self.memory_page)),
            ("فتح تبويب التكاملات", lambda: self._select_tab(self.integrations_page)),
            ("تحديث عرض التغييرات", self.refresh_changes),
            ("تراجع عن آخر تعديل", self.undo_last),
            ("إظهار أو إخفاء تفكير الوكيل", self.toggle_thought),
            ("مسح الصور المرفقة", self.clear_images),
            ("تفريغ مربع الكتابة", self._clear_prompt),
            ("اختيار مساحة عمل", self.choose_workspace),
            ("إرفاق صور", self.choose_images),
            ("البحث في المهارات", self._focus_skill_search),
            ("تطبيق الإعدادات والبدء", self.apply_configuration),
        )

    def _clear_prompt(self):
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.focus_set()
        self._update_composer_state()

    def _pump(self):
        if self._destroyed:
            return
        while request := self.controller.approvals.poll():
            self._queue_approval(request)
        for event in self.controller.poll_events():
            self._handle_event(*event)
        self._drain_boot_results()
        self._drain_probe_results()
        self._drain_models_results()
        if self._closing and self.controller.ready_to_close:
            self._destroyed = True
            self.root.destroy()
            return
        self._poll_job = self.root.after(50, self._pump)

    def _handle_event(self, kind, content):
        if kind == "chunk":
            self._append_stream_chunk(content)
            self.connection_var.set("LM Studio: متصل")
        elif kind == "thought":
            self._append_thought(content)
        elif kind == "status":
            self._set_status(content, "copper")
            if content.startswith("تشغيل الأداة:"):
                self._record_stat("tools")
                self._append_tool_line(content)
            if "MCP" in content:
                self._active_config = None
                self._pending_mcp_toggle = None
                self._load_mcp_servers()
        elif kind == "answer":
            self._record_stat("completed")
            self._append_chat("assistant", content)
            self._stream_result = "success"
            self.connection_var.set("LM Studio: متصل")
            self._set_status("اكتمل الرد", "teal")
        elif kind == "delegation":
            self._handle_delegation(content)
        elif kind == "denied":
            self._record_stat("denied")
            self._finish_stream()
            if self._stream_result == "pending":
                self._stream_result = "failed"
            if self._pending_mcp_toggle is not None:
                variable, previous = self._pending_mcp_toggle
                variable.set(previous)
                self._pending_mcp_toggle = None
            self._append_chat("error", f"تم رفض العملية: {content}")
            self._set_status("تم رفض العملية", "copper")
        elif kind == "error":
            self._record_stat("errors")
            self._finish_stream()
            if self._stream_result == "pending":
                self._stream_result = "failed"
            if self._pending_mcp_toggle is not None:
                variable, previous = self._pending_mcp_toggle
                variable.set(previous)
                self._pending_mcp_toggle = None
            self._append_chat("error", f"تعذر إكمال الطلب: {content}")
            self._set_status("تعذر إكمال الطلب · راجع التنبيه", "danger")
        elif kind == "done":
            had_stream = self._streaming_answer_open
            self._finish_stream()
            if self._stream_result == "pending" and had_stream:
                self._stream_result = "success"
                self._record_stat("completed")
                self._set_status("اكتمل الرد", "teal")
            self._set_busy(False)
            self.memory_panel.refresh(announce=True)
            if not self._closing and self.status_var.get().startswith("الوكيل يعمل"):
                self._set_status("جاهز", "teal")
            if self._approval_dialog is None:
                self.prompt_text.focus_set()

    def _append_tool_line(self, content):
        clean = safe_terminal_text(content.replace("تشغيل الأداة:", "", 1).strip())
        if not clean:
            return
        self.transcript.configure(state="normal")
        self.transcript.insert("end", f"◆ أداة: {clean}\n", "tool")
        self.transcript.configure(state="normal")
        self.transcript.see("end")

    def _finish_stream(self):
        if not self._streaming_answer_open:
            return
        if self._stream_parts:
            self._chat_log.append(("assistant", "".join(self._stream_parts)))
            self._stream_parts = []
        self.transcript.configure(state="normal")
        self.transcript.insert("end", "\n\n", "assistant")
        self.transcript.configure(state="normal")
        self._streaming_answer_open = False
        self._highlight_transcript_code()

    def _append_stream_chunk(self, content):
        clean = safe_terminal_text(content)
        if not clean:
            return
        self._set_empty_state(False)
        self._stream_parts.append(clean)
        self.transcript.configure(state="normal")
        if not self._streaming_answer_open:
            self.transcript.insert("end", "الوكيل\n", "assistant")
            self._streaming_answer_open = True
        self.transcript.insert("end", clean, "assistant")
        self.transcript.configure(state="normal")
        self.transcript.see("end")

    def _append_thought(self, content):
        clean = safe_terminal_text(content)
        if not clean:
            return
        self.thought_text.configure(state="normal")
        self.thought_text.insert("end", clean, "thought")
        self.thought_text.configure(state="disabled")
        self.thought_text.see("end")
        if not self._thought_visible:
            self.toggle_thought()

    def _clear_thought(self):
        self.thought_text.configure(state="normal")
        self.thought_text.delete("1.0", "end")
        self.thought_text.configure(state="disabled")

    def toggle_thought(self):
        self._thought_visible = not self._thought_visible
        if self._thought_visible:
            self.thought_text.grid(row=1, column=0, sticky="ew")
            self.thought_button.configure(text="تفكير الوكيل [إخفاء]")
        else:
            self.thought_text.grid_remove()
            self.thought_button.configure(text="تفكير الوكيل [عرض]")

    def _highlight_transcript_code(self):
        content = self.transcript.get("1.0", "end-1c")
        self.transcript.tag_remove("code", "1.0", "end")
        start = 0
        while (opening := content.find("```", start)) >= 0:
            line_end = content.find("\n", opening)
            closing = content.find("```", line_end + 1) if line_end >= 0 else -1
            if closing < 0:
                break
            self.transcript.tag_add(
                "code",
                f"1.0+{line_end + 1}c",
                f"1.0+{closing}c",
            )
            start = closing + 3

    def _set_diff_text(self, content):
        self._set_text(self.changes_text, content)
        for tag in ("diff_add", "diff_remove", "diff_hunk"):
            self.changes_text.tag_remove(tag, "1.0", "end")
        for number, line in enumerate(content.splitlines(), start=1):
            tag = (
                "diff_add" if line.startswith("+")
                else "diff_remove" if line.startswith("-")
                else "diff_hunk" if line.startswith("@@")
                else None
            )
            if tag:
                self.changes_text.tag_add(tag, f"{number}.0", f"{number}.end")

    def _queue_approval(self, request):
        if self._closing:
            self.controller.approvals.resolve(request, False)
        elif self._approval_dialog is None:
            self._approval_dialog = ApprovalDialog(
                self.root,
                request,
                self._approval_result,
                self.colors,
            )
        else:
            self._approval_queue.append(request)

    def _approval_result(self, request, approved):
        self.controller.approvals.resolve(request, approved)
        self._approval_dialog = None
        if self._approval_queue and not self._closing:
            self._queue_approval(self._approval_queue.pop(0))

    def _update_mode_ui(self, _event=None):
        mode = self._selected_mode()
        if mode == "read":
            color = self.colors["teal"]
            note = "قراءة الملفات والبحث داخلها فقط."
            badge = "الوضع: قراءة فقط"
            write_state = "الكتابة: معطلة"
        elif mode == "coding":
            color = self.colors["copper"]
            note = "سياسة البرمجة نشطة؛ كل تعديل يعرض فرقًا ويطلب موافقة."
            badge = "الوضع: برمجة"
            write_state = "الكتابة: بموافقة"
        else:
            color = self.colors["danger"]
            note = (
                "سياسة البرمجة نشطة؛ أوامر المضيف معزولة مع Docker/Podman، "
                "وإلا فهي غير معزولة."
            )
            badge = "الوضع: مضيف"
            write_state = "الأوامر: بموافقة"
        self.trust_rail.configure(bg=color)
        self.trust_mode_var.set(badge)
        self.write_state_var.set(write_state)
        self.mode_note_var.set(note)
        self.delegation_check.configure(state="disabled" if mode == "read" else "normal")
        self._active_config = None

    def _set_busy(self, busy):
        normal = "disabled" if busy else "normal"
        self.send_button.configure(text="…" if busy else "↑")
        if busy:
            self.progress.pack(side="left", padx=(10, 0))
            self.progress.start(12)
            self._busy_since = time.monotonic()
            self._tick_busy_timer()
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self._busy_since = None
            if self._timer_job is not None:
                try:
                    self.root.after_cancel(self._timer_job)
                except tk.TclError:
                    pass
                self._timer_job = None
        self.send_button.configure(state=normal)
        self.apply_button.configure(state=normal)
        self.browse_button.configure(state=normal)
        self.open_folder_button.configure(state=normal)
        self.refresh_button.configure(state=normal)
        self.undo_button.configure(state=normal)
        can_accept = bool(
            self._delegation_snapshot and self._delegation_snapshot.get("can_accept")
        )
        self.accept_task_button.configure(
            state="normal" if can_accept and not busy else "disabled"
        )
        self.delegation_check.configure(
            state="disabled" if busy or self._selected_mode() == "read" else "normal"
        )
        self.workspace_entry.configure(state=normal)
        self.session_entry.configure(state=normal)
        self.clear_session_button.configure(state=normal)
        self.model_combo.configure(state="disabled" if busy else "normal")
        self.refresh_models_button.configure(state=normal)
        self.base_url_entry.configure(state=normal)
        self.mcp_entry.configure(state=normal)
        self.mcp_button.configure(state=normal)
        self.memory_check.configure(state=normal)
        for check in self._memory_feature_checks:
            check.configure(state=normal)
        self.images_button.configure(state=normal)
        self.clear_images_button.configure(state=normal)
        self.mode_combo.configure(state="disabled" if busy else "readonly")
        self.container_combo.configure(state="disabled" if busy else "readonly")
        for check in self._mcp_checks:
            check.configure(state=normal)
        self._update_composer_state()

    def _tick_busy_timer(self):
        if self._busy_since is None or self._closing:
            self._timer_job = None
            return
        seconds = int(time.monotonic() - self._busy_since)
        if self.status_var.get().startswith("الوكيل يعمل"):
            self.status_var.set(f"الوكيل يعمل… {seconds} ث" if seconds else "الوكيل يعمل…")
        self._timer_job = self.root.after(1000, self._tick_busy_timer)

    def _configure_theme_styles(self):
        style = ttk.Style(self.root)
        style.configure("TNotebook", background=self.colors["surface"], borderwidth=0)
        style.configure("Center.TNotebook", background=self.colors["surface"], borderwidth=0)
        style.layout("Center.TNotebook.Tab", [])
        style.configure(
            "TNotebook.Tab",
            background=self.colors["fog"],
            foreground=self.colors["muted"],
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", self.colors["surface"])],
            foreground=[("selected", self.colors["ink"])],
        )
        style.configure(
            "TCombobox",
            fieldbackground=self.colors["secondary_bg"],
            background=self.colors["secondary_bg"],
            foreground=self.colors["ink"],
            arrowcolor=self.colors["muted"],
            bordercolor=self.colors["line"],
            lightcolor=self.colors["line"],
            darkcolor=self.colors["line"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", self.colors["secondary_bg"])],
            foreground=[("readonly", self.colors["ink"])],
            selectbackground=[("readonly", self.colors["secondary_bg"])],
            selectforeground=[("readonly", self.colors["ink"])],
        )

    @staticmethod
    def _restyle_tree(widget, old, new):
        background = {
            old["fog"]: new["fog"],
            old["surface"]: new["surface"],
            old["ink"]: new["fog"],
            old["soft_teal"]: new["soft_teal"],
            old["soft_copper"]: new["soft_copper"],
            old["soft_danger"]: new["soft_danger"],
            old["teal"]: new["teal"],
            old["copper"]: new["copper"],
            old["danger"]: new["danger"],
            old["secondary_bg"]: new["secondary_bg"],
            old["elevated"]: new["elevated"],
            old["chip"]: new["chip"],
            old["band_bg"]: new["band_bg"],
            }
        foreground = {
            old["ink"]: new["ink"],
            old["muted"]: new["muted"],
            old["teal"]: new["teal"],
            old["copper"]: new["copper"],
            old["danger"]: new["danger"],
            old["chip_fg"]: new["chip_fg"],
        }
        if isinstance(widget, tk.Entry):
            try:
                widget.configure(
                    bg=new["secondary_bg"], fg=new["ink"],
                    insertbackground=new["teal"], readonlybackground=new["secondary_bg"],
                    highlightbackground=new["line"], highlightcolor=new["focus"],
                )
            except tk.TclError:
                pass
        try:
            current = str(widget.cget("background"))
            if current in background:
                widget.configure(background=background[current])
        except tk.TclError:
            pass
        try:
            current = str(widget.cget("foreground"))
            if current in foreground:
                widget.configure(foreground=foreground[current])
        except tk.TclError:
            pass
        try:
            current = str(widget.cget("highlightbackground"))
            if current == old["line"]:
                widget.configure(highlightbackground=new["line"])
            current = str(widget.cget("highlightcolor"))
            if current == old["focus"]:
                widget.configure(highlightcolor=new["focus"])
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            LocalAgentApp._restyle_tree(child, old, new)

    def _install_hovers(self, widget):
        """امنح كل زر تفاعلي ردّ فعل hover لطيفًا (إحساس مادي عند الاقتراب).

        يُحسب اللون لحظيًا من خلفية الزر الحالية، فيبقى صحيحًا بعد تبديل السمة
        دون الحاجة لمعرفة اللوحة مسبقًا. الأزرار المعطّلة لا تستجيب.
        """
        if isinstance(widget, tk.Button):
            self._bind_hover(widget)
        for child in widget.winfo_children():
            self._install_hovers(child)

    @staticmethod
    def _bind_hover(button):
        state = {"base": None}

        def on_enter(_event):
            if str(button.cget("state")) == "disabled":
                return
            base = str(button.cget("background"))
            state["base"] = base
            # تعتيم طفيف للألوان الفاتحة وتفتيح للداكنة لإبراز التمرير.
            luminance = theme.relative_luminance(base)
            button.configure(background=theme.shade(base, 0.10 if luminance < 0.5 else -0.06))

        def on_leave(_event):
            if state["base"] is not None:
                button.configure(background=state["base"])
                state["base"] = None

        button.bind("<Enter>", on_enter, add="+")
        button.bind("<Leave>", on_leave, add="+")

    def toggle_theme(self):
        old = self.colors
        self._dark_theme = not self._dark_theme
        self.colors = DARK_COLORS if self._dark_theme else COLORS
        self._restyle_tree(self.root, old, self.colors)
        self.root.configure(bg=self.colors["fog"])
        self.trust_band.configure(bg=self.colors["band_bg"])
        self.board.configure(
            bg=self.colors["surface"], highlightbackground=self.colors["line"]
        )
        for label in self._trust_labels:
            label.configure(bg=self.colors["chip"], fg=self.colors["chip_fg"])
        for frame, icon_label, dot_label in self._trust_chips:
            frame.configure(bg=self.colors["chip"], highlightbackground=self.colors["line"])
            icon_label.configure(bg=self.colors["chip"], fg=self.colors["chip_fg"])
            dot_label.configure(bg=self.colors["chip"])
        self.theme_button.configure(
            text="الوضع الفاتح" if self._dark_theme else "الوضع الداكن",
            bg=self.colors["fog"],
            fg=self.colors["ink"],
        )
        self.apply_button.configure(bg=self.colors["teal"], fg="white")
        self.transcript.configure(bg=self.colors["surface"], fg=self.colors["ink"])
        self.prompt_text.configure(
            bg=self.colors["surface"],
            fg=self.colors["ink"],
            insertbackground=self.colors["ink"],
            highlightbackground=self.colors["line"],
            highlightcolor=self.colors["focus"],
        )
        self.transcript.tag_configure(
            "assistant", foreground=self.colors["ink"]
        )
        self.transcript.tag_configure(
            "code", background=self.colors["fog"], foreground=self.colors["ink"]
        )
        self.thought_text.configure(bg=self.colors["fog"], fg=self.colors["muted"])
        self.thought_text.tag_configure("thought", foreground=self.colors["muted"])
        self.changes_text.configure(bg=self.colors["fog"], fg=self.colors["ink"])
        self.changes_text.tag_configure(
            "diff_add",
            foreground=self.colors["teal"] if self._dark_theme else "#137333",
            background=self.colors["soft_teal"] if self._dark_theme else "#E6F4EA",
        )
        self.changes_text.tag_configure(
            "diff_remove",
            foreground=self.colors["danger"] if self._dark_theme else "#B3261E",
            background=self.colors["soft_danger"] if self._dark_theme else "#FCE8E6",
        )
        self.changes_text.tag_configure(
            "diff_hunk", foreground=self.colors["copper"] if self._dark_theme else "#3730A3"
        )
        self._configure_theme_styles()
        self.memory_panel.apply_colors(self.colors)
        self._sync_tab_buttons()
        self._update_mode_ui()
        self.transcript.tag_configure("tool", foreground=self.colors["muted"])
        self.save_settings()
        return self._dark_theme

    def _set_status(self, text, tone):
        palette = {
            "teal": (self.colors["soft_teal"], self.colors["teal"]),
            "copper": (self.colors["soft_copper"], self.colors["copper"]),
            "danger": (self.colors["soft_danger"], self.colors["danger"]),
        }
        background, foreground = palette[tone]
        self.status_var.set(safe_terminal_text(text))
        self.status_label.configure(bg=background, fg=foreground)
        self.status_dot.configure(bg=background, fg=foreground)

    def _select_tab(self, page):
        self.notebook.select(page)
        return "break"

    def _sync_tab_buttons(self, _event=None):
        """أبرز زر التبويب المفتوح حاليًا في الشريط الموسّط."""
        buttons = getattr(self, "_tab_buttons", None)
        if not buttons:
            return
        try:
            current = self.notebook.select()
        except tk.TclError:
            return
        underlines = getattr(self, "_tab_underlines", {})
        for key, button in buttons.items():
            active = key == current
            button.configure(
                fg=self.colors["teal"] if active else self.colors["muted"],
                bg=self.colors["surface"],
                font=theme.ui_font(10, "bold"),
            )
            underline = underlines.get(key)
            if underline is not None:
                underline.configure(
                    bg=self.colors["teal"] if active else self.colors["surface"]
                )
        navigation_pages = (
            self.chat_page,
            self.changes_page,
            self.skills_page,
            self.memory_page,
            self.integrations_page,
            self.integrations_page,
        )
        for button, page in zip(self._navigation_buttons, navigation_pages, strict=True):
            active = str(page) == current
            button.configure(
                bg=self.colors["soft_teal"] if active else self.colors["surface"],
                fg=self.colors["teal"] if active else self.colors["chip_fg"],
            )

    def _focus_skill_search(self, _event=None):
        self.notebook.select(self.skills_page)
        self.skill_panel.search_entry.focus_set()
        return "break"

    def _set_empty_state(self, visible):
        state = getattr(self, "empty_state", None)
        if state is None or bool(getattr(self, "_empty_state_visible", False)) == visible:
            return
        if visible:
            state.grid()
        else:
            state.grid_remove()
        self._empty_state_visible = visible

    def _append_chat(self, role, content):
        self._set_empty_state(False)
        labels = {"user": "أنت", "assistant": "الوكيل", "error": "تنبيه"}
        clean = safe_terminal_text(content)
        self._chat_log.append((role, clean))
        self.transcript.configure(state="normal")
        self.transcript.insert("end", f"{labels[role]}\n", role)
        for line in clean.split("\n"):
            self.transcript.insert("end", line + "\n", (role, self._line_tag(line)))
        self.transcript.insert("end", "\n", role)
        self.transcript.configure(state="normal")
        self.transcript.see("end")

    @staticmethod
    def _line_tag(line):
        stripped = line.strip()
        if stripped.startswith("#"):
            return "msg_heading"
        if stripped.startswith("- ") or stripped.startswith("• "):
            return "msg_bullet"
        if len(stripped) > 1 and stripped[0].isdigit() and stripped[1] in ".)":
            return "msg_bullet"
        return "msg_body"

    def _clear_transcript(self):
        self._chat_log = []
        self._stream_parts = []
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="normal")
        self._set_empty_state(True)

    def copy_last_answer(self):
        for role, content in reversed(self._chat_log):
            if role == "assistant" and content.strip():
                self.root.clipboard_clear()
                self.root.clipboard_append(content)
                self._set_status("تم نسخ آخر رد إلى الحافظة", "teal")
                return True
        self._set_status("لا يوجد رد لنسخه", "copper")
        return False

    def _block_transcript_edit(self, event):
        """اجعل المحادثة للقراءة فقط: اسمح بالنسخ والتحديد والتنقّل، وامنع الكتابة."""
        if event.state & 0x4 and event.keysym.lower() in {"c", "a"}:
            return None  # Ctrl+C / Ctrl+A
        allowed = {
            "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next",
            "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
        }
        if event.keysym in allowed:
            return None
        return "break"

    def _copy_selection(self, _event=None):
        try:
            text = self.transcript.get("sel.first", "sel.last")
        except tk.TclError:
            return "break"
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        return "break"

    def _select_all_transcript(self, _event=None):
        self.transcript.tag_add("sel", "1.0", "end")
        self.transcript.focus_set()
        return "break"

    def _show_text_menu(self, event, widget, editable=False):
        menu = tk.Menu(self.root, tearoff=0, font=theme.menu_font())
        if editable:
            menu.add_command(label="قص", command=lambda: widget.event_generate("<<Cut>>"))
            menu.add_command(label="نسخ", command=lambda: widget.event_generate("<<Copy>>"))
            menu.add_command(label="لصق", command=lambda: widget.event_generate("<<Paste>>"))
        else:
            menu.add_command(label="نسخ المحدد", command=lambda: widget.event_generate("<<Copy>>"))
            menu.add_command(label="نسخ آخر رد", command=self.copy_last_answer)
        menu.add_separator()
        menu.add_command(
            label="تحديد الكل",
            command=lambda: widget.tag_add("sel", "1.0", "end"),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    @staticmethod
    def _set_text(widget, content):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def close(self):
        if self._closing:
            return
        self._closing = True
        self.save_settings()
        if self._probe_thread is not None:
            try:
                self._probe_thread.join(timeout=2)
            except RuntimeError:
                pass
        if self._boot_thread is not None:
            try:
                self._boot_thread.join(timeout=2)
            except RuntimeError:
                pass
        if self._models_thread is not None:
            try:
                self._models_thread.join(timeout=2)
            except RuntimeError:
                pass
        if self._mcp_editor is not None:
            self._mcp_editor.close()
        self.controller.request_close()
        if self._approval_dialog is not None:
            self._approval_dialog.reject()
        if self.controller.ready_to_close:
            self._destroyed = True
            self.root.destroy()
        else:
            self._set_busy(True)
            self._set_status("جارٍ الإغلاق الآمن بعد انتهاء العملية…", "copper")


def _enable_high_dpi():
    """فعّل الوعي بكثافة النقاط (DPI) على ويندوز لعرض حاد عالي الدقة.

    بدون ذلك يُمطّط ويندوز النافذة بكسليًا فتبدو ضبابية على الشاشات عالية
    الكثافة. نجرّب Per-Monitor أولًا ثم النظام كحل بديل؛ أي فشل غير مؤثّر.
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor Aware
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def main():
    _enable_high_dpi()
    root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    # طابق قياس Tk مع كثافة الشاشة حتى تُرسم الخطوط بحدّة بحجمها الصحيح.
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except tk.TclError:
        pass
    LocalAgentApp(root, settings=AppSettings())
    root.mainloop()


if __name__ == "__main__":
    main()
