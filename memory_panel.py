"""Tkinter dashboard for the project-scoped cognitive memory engine."""

from __future__ import annotations

from datetime import datetime
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import theme


class MemoryPanel:
    def __init__(self, parent, engine_getter, colors):
        self.engine_getter = engine_getter
        self.colors = colors
        self._memories = {}
        self._skills = {}
        self._last_verified = 0
        self.search_var = tk.StringVar()
        self.show_archived_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="طبّق إعدادات مساحة العمل لعرض ذاكرتها")
        self.notice_var = tk.StringVar(
            value="كل البيانات محفوظة محليًا داخل ذاكرة هذا المشروع"
        )
        self.stat_vars = {
            key: tk.StringVar(value="0")
            for key in ("memories", "verified_successes", "verified_failures", "learned_skills")
        }
        self._build(parent)
        self.apply_colors(colors)

    def _build(self, parent):
        shell = tk.Frame(parent, bg=self.colors["surface"], padx=22, pady=18)
        shell.pack(fill="both", expand=True)
        header = tk.Frame(shell, bg=self.colors["surface"])
        header.pack(fill="x", pady=(0, 12))
        tk.Button(
            header,
            text="تحديث",
            command=self.refresh,
            bg=self.colors["soft_teal"],
            fg=self.colors["teal"],
            relief="flat",
            padx=14,
            pady=7,
            cursor="hand2",
        ).pack(side="left")
        tk.Button(
            header,
            text="مسح ذاكرة المشروع",
            command=self.clear_project_memory,
            bg=self.colors["secondary_bg"],
            fg=self.colors["muted"],
            relief="flat",
            padx=14,
            pady=7,
            cursor="hand2",
        ).pack(side="left", padx=(8, 0))
        tk.Label(
            header,
            text="الذاكرة",
            bg=self.colors["surface"],
            fg=self.colors["ink"],
            font=theme.display_font(18, "bold"),
            anchor="e",
        ).pack(side="right")
        tk.Label(
            shell,
            textvariable=self.notice_var,
            bg=self.colors["soft_teal"],
            fg=self.colors["teal"],
            anchor="e",
            padx=12,
            pady=6,
        ).pack(fill="x", pady=(0, 10))

        stats = tk.Frame(shell, bg=self.colors["surface"])
        stats.pack(fill="x", pady=(0, 12))
        for key, label in (
            ("memories", "ذكريات"),
            ("verified_successes", "خبرات ناجحة"),
            ("verified_failures", "دروس من الأخطاء"),
            ("learned_skills", "مهارات مكتسبة"),
        ):
            card = tk.Frame(
                stats,
                bg=self.colors["secondary_bg"],
                highlightthickness=1,
                highlightbackground=self.colors["line"],
                padx=16,
                pady=10,
            )
            card.pack(side="right", fill="x", expand=True, padx=4)
            tk.Label(
                card,
                textvariable=self.stat_vars[key],
                bg=self.colors["secondary_bg"],
                fg=self.colors["teal"],
                font=theme.display_font(17, "bold"),
            ).pack()
            tk.Label(
                card,
                text=label,
                bg=self.colors["secondary_bg"],
                fg=self.colors["muted"],
                font=theme.ui_font(9),
            ).pack()

        self.notebook = ttk.Notebook(shell, style="Memory.TNotebook")
        notebook = self.notebook
        notebook.pack(fill="both", expand=True)
        overview = tk.Frame(notebook, bg=self.colors["surface"])
        memories = tk.Frame(notebook, bg=self.colors["surface"])
        experiences = tk.Frame(notebook, bg=self.colors["surface"])
        skills = tk.Frame(notebook, bg=self.colors["surface"])
        notebook.add(skills, text="المهارات")
        notebook.add(experiences, text="الخبرات")
        notebook.add(memories, text="ذكريات المشروع")
        notebook.add(overview, text="نظرة عامة")
        notebook.select(overview)

        tk.Label(
            overview,
            text=(
                "ذاكرة محلية معزولة لمساحة العمل. تُعامل الذكريات كسياق غير موثوق، "
                "ولا تُرقّى الخبرة إلا بدليل تنفيذ فعلي مثل كود خروج العملية."
            ),
            bg=self.colors["surface"],
            fg=self.colors["muted"],
            justify="right",
            anchor="ne",
            wraplength=720,
            padx=18,
            pady=22,
        ).pack(fill="both", expand=True)
        tk.Label(
            overview,
            textvariable=self.status_var,
            bg=self.colors["surface"],
            fg=self.colors["ink"],
            anchor="e",
            padx=18,
            pady=10,
        ).pack(fill="x")

        search = tk.Frame(memories, bg=self.colors["surface"])
        search.pack(fill="x", padx=8, pady=8)
        tk.Button(
            search,
            text="بحث",
            command=self.refresh,
            bg=self.colors["teal"],
            fg="white",
            relief="flat",
            padx=14,
            pady=6,
        ).pack(side="left")
        tk.Checkbutton(
            search,
            text="عرض المؤرشف",
            variable=self.show_archived_var,
            command=self.refresh,
            bg=self.colors["surface"],
            fg=self.colors["muted"],
            activebackground=self.colors["surface"],
            activeforeground=self.colors["ink"],
            selectcolor=self.colors["secondary_bg"],
        ).pack(side="left", padx=8)
        entry = tk.Entry(search, textvariable=self.search_var, justify="right")
        entry.pack(side="right", fill="x", expand=True, ipady=6)
        entry.bind("<Return>", lambda _event: self.refresh())
        self.memory_tree = self._tree(
            memories,
            (("kind", "النوع", 100), ("text", "المحتوى", 460), ("importance", "الأهمية", 75)),
        )
        self._actions(
            memories,
            (
                ("تثبيت/إلغاء", self.toggle_pin),
                ("تحرير", self.edit_memory),
                ("أرشفة/استعادة", self.archive_memory),
                ("حذف", self.delete_memory),
            ),
        )

        self.experience_tree = self._tree(
            experiences,
            (("status", "النتيجة", 90), ("task", "المهمة", 280), ("lesson", "الدرس", 360), ("confidence", "الثقة", 70)),
        )
        self.skill_tree = self._tree(
            skills,
            (("name", "المهارة", 250), ("version", "الإصدار", 70), ("status", "الحالة", 90), ("rate", "النجاح", 80), ("last", "آخر استخدام", 130)),
        )
        self._actions(
            skills,
            (("تفعيل/تعطيل", self.toggle_skill), ("حذف الإصدار", self.delete_skill)),
        )

    def _tree(self, parent, columns):
        tree = ttk.Treeview(
            parent,
            columns=[item[0] for item in columns],
            show="headings",
            style="Memory.Treeview",
        )
        for key, heading, width in columns:
            tree.heading(key, text=heading, anchor="e")
            tree.column(key, width=width, minwidth=55, anchor="e", stretch=key in {"text", "task", "lesson", "name"})
        tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        return tree

    def apply_colors(self, colors):
        self.colors = colors
        style = ttk.Style(self.notebook)
        style.configure(
            "Memory.TNotebook",
            background=colors["surface"],
            borderwidth=0,
        )
        style.configure(
            "Memory.TNotebook.Tab",
            background=colors["secondary_bg"],
            foreground=colors["muted"],
            padding=(14, 8),
        )
        style.map(
            "Memory.TNotebook.Tab",
            background=[("selected", colors["soft_teal"])],
            foreground=[("selected", colors["teal"])],
        )
        style.configure(
            "Memory.Treeview",
            background=colors["secondary_bg"],
            fieldbackground=colors["secondary_bg"],
            foreground=colors["ink"],
            rowheight=30,
            borderwidth=0,
        )
        style.configure(
            "Memory.Treeview.Heading",
            background=colors["surface"],
            foreground=colors["muted"],
            relief="flat",
        )
        style.map(
            "Memory.Treeview",
            background=[("selected", colors["soft_teal"])],
            foreground=[("selected", colors["teal"])],
        )

    def _actions(self, parent, actions):
        row = tk.Frame(parent, bg=self.colors["surface"])
        row.pack(fill="x", padx=8, pady=(0, 8))
        for label, command in actions:
            tk.Button(
                row,
                text=label,
                command=command,
                bg=self.colors["secondary_bg"],
                fg=self.colors["ink"],
                relief="flat",
                padx=12,
                pady=6,
                cursor="hand2",
            ).pack(side="right", padx=(6, 0))

    def _engine(self):
        try:
            return self.engine_getter()
        except (AttributeError, RuntimeError):
            return None

    @staticmethod
    def _clear(tree):
        for item in tree.get_children():
            tree.delete(item)

    @staticmethod
    def _date(timestamp):
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M") if timestamp else "—"

    def refresh(self, announce=False):
        engine = self._engine()
        if engine is None:
            self.status_var.set("الذاكرة غير مفعلة أو لم تُطبّق إعدادات المشروع بعد")
            return False
        try:
            stats = engine.stats()
            memories = engine.list_memories(
                query=self.search_var.get().strip(),
                include_archived=self.show_archived_var.get(),
            )
            experiences = engine.list_experiences()
            skills = engine.list_learned_skills()
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            self.status_var.set("تعذر قراءة الذاكرة؛ سيواصل الوكيل العمل دونها")
            return False
        for key, variable in self.stat_vars.items():
            variable.set(str(stats.get(key, 0)))
        verified = stats.get("verified_successes", 0) + stats.get("verified_failures", 0)
        if announce and verified > self._last_verified:
            self.notice_var.set("✓ تعلم الوكيل من هذه المهمة بنتيجة تنفيذ موثقة")
        elif not self.notice_var.get():
            self.notice_var.set("كل البيانات محفوظة محليًا داخل ذاكرة هذا المشروع")
        self._last_verified = verified
        self._fill_memories(memories)
        self._fill_experiences(experiences)
        self._fill_skills(skills)
        self.status_var.set(f"ذاكرة المشروع جاهزة · {engine.workspace}")
        return True

    def _fill_memories(self, rows):
        self._clear(self.memory_tree)
        self._memories = {str(row["id"]): row for row in rows}
        for key, row in self._memories.items():
            kind = f"📌 {row['kind']}" if row["pinned"] else row["kind"]
            if row["archived"]:
                kind = f"مؤرشف · {kind}"
            self.memory_tree.insert("", "end", iid=key, values=(kind, row["text"], row["importance"]))

    def _fill_experiences(self, rows):
        self._clear(self.experience_tree)
        labels = {"success": "نجحت", "failure": "فشلت", "unverified": "غير موثقة", "running": "قيد التنفيذ"}
        for row in rows:
            self.experience_tree.insert(
                "",
                "end",
                values=(labels.get(row["status"], row["status"]), row["task"], row["lesson"] or "—", f"{row['confidence']:.0%}"),
            )

    def _fill_skills(self, rows):
        self._clear(self.skill_tree)
        self._skills = {str(row["id"]): row for row in rows}
        for key, row in self._skills.items():
            total = row["success_count"] + row["failure_count"]
            rate = row["success_count"] / total if total else 0
            status = row["status"] if row["enabled"] else "معطلة"
            self.skill_tree.insert(
                "",
                "end",
                iid=key,
                values=(row["name"], f"v{row['version']}", status, f"{rate:.0%}", self._date(row["last_used_at"])),
            )

    def _selected(self, tree, rows):
        selected = tree.selection()
        return rows.get(selected[0]) if selected else None

    def toggle_pin(self):
        row = self._selected(self.memory_tree, self._memories)
        if row and self._engine():
            (self._engine().unpin if row["pinned"] else self._engine().pin)(row["id"])
            self.refresh()

    def edit_memory(self):
        row = self._selected(self.memory_tree, self._memories)
        if not row:
            return
        text = simpledialog.askstring("تحرير الذاكرة", "المحتوى", initialvalue=row["text"], parent=self.memory_tree)
        if text and text.strip() and self._engine():
            self._engine().edit_memory(row["id"], text.strip())
            self.refresh()

    def archive_memory(self):
        row = self._selected(self.memory_tree, self._memories)
        if row and self._engine():
            action = self._engine().unarchive if row["archived"] else self._engine().archive
            action(row["id"])
            self.refresh()

    def clear_project_memory(self):
        engine = self._engine()
        if engine and messagebox.askyesno(
            "مسح ذاكرة المشروع",
            "سيُحذف سجل الذاكرة والخبرات والمهارات لهذا المشروع. متابعة؟",
            parent=self.notebook,
        ):
            engine.clear_project_memory()
            self.notice_var.set("تم مسح ذاكرة هذا المشروع")
            self.refresh()

    def delete_memory(self):
        row = self._selected(self.memory_tree, self._memories)
        if row and messagebox.askyesno("حذف الذاكرة", "حذف هذه الذاكرة نهائيًا؟", parent=self.memory_tree):
            self._engine().forget(row["id"])
            self.refresh()

    def toggle_skill(self):
        row = self._selected(self.skill_tree, self._skills)
        if row and self._engine():
            self._engine().set_skill_enabled(row["id"], not row["enabled"])
            self.refresh()

    def delete_skill(self):
        row = self._selected(self.skill_tree, self._skills)
        if row and messagebox.askyesno("حذف المهارة", "حذف هذا الإصدار من المهارة؟", parent=self.skill_tree):
            self._engine().delete_skill(row["id"])
            self.refresh()
