import tkinter as tk
from tkinter.scrolledtext import ScrolledText

from skill_catalog import SkillCatalog, SkillError
import theme
from theme import COLORS


class SkillsPanel:
    def __init__(self, parent, catalog=None, colors=None):
        self.colors = colors or COLORS
        self.catalog = catalog
        self.skills = ()
        self.filtered = ()
        self.count = 0
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.safety_var = tk.StringVar(
            value=(
                "تعليمات المهارات ومواردها غير موثوقة. يقرأها الوكيل كنص فقط، "
                "ولا يشغّل سكربتاتها تلقائيًا."
            )
        )
        self._build(parent)
        self.search_var.trace_add("write", lambda *_args: self._filter())
        self.reload()

    def _build(self, parent):
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        header = tk.Frame(parent, bg=self.colors["surface"], padx=22, pady=18)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        self.count_label = tk.Label(
            header,
            textvariable=self.status_var,
            bg=self.colors["surface"],
            fg=self.colors["muted"],
            anchor="w",
        )
        self.count_label.grid(row=0, column=0, sticky="w")
        tk.Label(
            header,
            text="مكتبة المهارات",
            bg=self.colors["surface"],
            fg=self.colors["ink"],
            font=theme.display_font(15),
            anchor="e",
        ).grid(row=0, column=1, sticky="e")

        body = tk.Frame(parent, bg=self.colors["fog"], padx=18, pady=16)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_rowconfigure(2, weight=1)
        body.grid_columnconfigure(0, weight=1)

        safety = tk.Label(
            body,
            textvariable=self.safety_var,
            bg=self.colors["soft_copper"],
            fg=self.colors["copper"],
            justify="right",
            anchor="e",
            padx=14,
            pady=10,
            wraplength=820,
        )
        safety.grid(row=0, column=0, sticky="ew", pady=(0, 12))

        search_row = tk.Frame(body, bg=self.colors["fog"])
        search_row.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        search_row.grid_columnconfigure(0, weight=1)
        self.search_entry = tk.Entry(
            search_row,
            textvariable=self.search_var,
            justify="right",
            relief="solid",
            borderwidth=1,
        )
        self.search_entry.grid(row=0, column=0, sticky="ew", ipady=7)
        tk.Label(
            search_row,
            text="ابحث بالاسم أو الوصف",
            bg=self.colors["fog"],
            fg=self.colors["muted"],
            padx=10,
            anchor="e",
        ).grid(row=0, column=1, sticky="e")

        panes = tk.PanedWindow(
            body,
            orient="horizontal",
            bg=self.colors["line"],
            sashwidth=5,
            relief="flat",
        )
        panes.grid(row=2, column=0, sticky="nsew")

        detail_frame = tk.Frame(panes, bg=self.colors["surface"], padx=14, pady=14)
        list_frame = tk.Frame(panes, bg=self.colors["surface"], padx=12, pady=12, width=260)
        panes.add(detail_frame, stretch="always", minsize=380)
        panes.add(list_frame, stretch="never", minsize=230)

        detail_frame.grid_rowconfigure(1, weight=1)
        detail_frame.grid_columnconfigure(0, weight=1)
        tk.Label(
            detail_frame,
            text="معاينة التعليمات",
            bg=self.colors["surface"],
            fg=self.colors["ink"],
            font=theme.ui_font(11, 'bold'),
            anchor="e",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 9))
        self.detail = ScrolledText(
            detail_frame,
            wrap="word",
            state="disabled",
            bg=self.colors["surface"],
            fg=self.colors["ink"],
            relief="solid",
            borderwidth=1,
            padx=14,
            pady=12,
            spacing3=5,
        )
        self.detail.grid(row=1, column=0, sticky="nsew")
        self.detail.tag_configure("rtl", justify="right")
        self.detail.tag_configure("ltr", justify="left")

        list_frame.grid_rowconfigure(1, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)
        tk.Label(
            list_frame,
            text="المهارات المتاحة",
            bg=self.colors["surface"],
            fg=self.colors["ink"],
            font=theme.ui_font(11, 'bold'),
            anchor="e",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 9))
        self.listbox = tk.Listbox(
            list_frame,
            activestyle="dotbox",
            exportselection=False,
            relief="flat",
            borderwidth=0,
            selectbackground=self.colors["teal"],
            selectforeground="#FFFFFF",
            highlightthickness=1,
            highlightcolor=self.colors["focus"],
            highlightbackground=self.colors["line"],
        )
        self.listbox.grid(row=1, column=0, sticky="nsew")
        self.listbox.bind("<<ListboxSelect>>", self._show_selected)
        self.listbox.bind("<Return>", self._show_selected)
        self.search_entry.bind("<Escape>", self._clear_search)

        self.path_label = tk.Label(
            body,
            text="",
            bg=self.colors["fog"],
            fg=self.colors["muted"],
            anchor="w",
            justify="left",
        )
        self.path_label.grid(row=3, column=0, sticky="ew", pady=(10, 0))

    def reload(self):
        try:
            self.catalog = self.catalog or SkillCatalog.default()
            self.skills = self.catalog.skills
            self.count = len(self.skills)
            self.path_label.configure(text=str(self.catalog.root))
            self._filter()
        except SkillError as error:
            self.catalog = None
            self.skills = ()
            self.filtered = ()
            self.count = 0
            self.listbox.delete(0, "end")
            self.status_var.set(f"تعذر تحميل المهارات: {error}")
            self.path_label.configure(text="")
            self._set_detail("تحقق من بقاء مجلد المهارات في المسار المحدد.")

    def _filter(self):
        query = self.search_var.get().strip().casefold()
        self.filtered = tuple(
            skill
            for skill in self.skills
            if not query
            or query in skill.name.casefold()
            or query in skill.description.casefold()
        )
        self.listbox.delete(0, "end")
        for skill in self.filtered:
            self.listbox.insert("end", skill.name)
        if self.filtered:
            self.status_var.set(f"{self.count} مهارة متاحة · {len(self.filtered)} ظاهرة")
            self.listbox.selection_set(0)
            self._show_selected()
        else:
            self.status_var.set("لا توجد مهارات تطابق البحث")
            self._set_detail("جرّب كلمة أخرى أو امسح البحث.")

    def _show_selected(self, _event=None):
        selection = self.listbox.curselection()
        if not selection:
            return
        skill = self.filtered[selection[0]]
        body = self._instruction_body(skill.instructions)
        self._set_detail(
            f"{skill.name}\n\n{skill.description}\n\n"
            "────────────────────────\n\n"
            f"{body}"
        )

    @staticmethod
    def _instruction_body(instructions):
        lines = instructions.splitlines()
        if lines and lines[0].strip() == "---":
            for index, line in enumerate(lines[1:], 1):
                if line.strip() == "---":
                    return "\n".join(lines[index + 1 :]).strip()
        return instructions.strip()

    def _clear_search(self, _event=None):
        self.search_var.set("")
        return "break"

    def _set_detail(self, content):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        direction = "rtl" if any("\u0600" <= char <= "\u06ff" for char in content) else "ltr"
        self.detail.insert("1.0", content, direction)
        self.detail.configure(state="disabled")
