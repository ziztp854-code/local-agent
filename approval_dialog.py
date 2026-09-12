import tkinter as tk
from tkinter.scrolledtext import ScrolledText
import theme

from workspace_tools import safe_terminal_text


class ApprovalDialog:
    def __init__(self, parent, request, on_result, colors):
        self.request = request
        self.on_result = on_result
        self.finished = False
        self.window = tk.Toplevel(parent)
        self.window.title("موافقة مطلوبة")
        self.window.geometry("820x560")
        self.window.minsize(620, 420)
        self.window.configure(bg=colors["surface"])
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.reject)
        self.window.bind("<Escape>", self.reject)
        self.window.bind("<Alt-a>", lambda _event: self.allow())
        self.window.bind("<Alt-r>", self.reject)
        self.window.grab_set()

        header = tk.Frame(
            self.window,
            bg=colors["soft_copper"],
            padx=24,
            pady=18,
        )
        header.pack(fill="x")
        tk.Label(
            header,
            text="موافقة مطلوبة",
            bg=colors["soft_copper"],
            fg=colors["copper"],
            font=theme.display_font(16),
            anchor="e",
        ).pack(fill="x")
        tk.Label(
            header,
            text=f"العملية: {safe_terminal_text(request.action)}",
            bg=colors["soft_copper"],
            fg=colors["ink"],
            anchor="e",
        ).pack(fill="x", pady=(6, 0))

        body = tk.Frame(self.window, bg=colors["surface"], padx=24, pady=18)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)
        tk.Label(
            body,
            text=(
                "راجع المعاينة كاملة؛ قد يكون الاقتراح من مهارة خارجية غير موثوقة. "
                "السماح يخص هذه العملية فقط."
            ),
            bg=colors["surface"],
            fg=colors["muted"],
            anchor="e",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 10))
        preview = ScrolledText(
            body,
            wrap="none",
            font=theme.mono_font(10),
            bg=colors["fog"],
            fg=colors["ink"],
            relief="solid",
            borderwidth=1,
            padx=12,
            pady=12,
        )
        preview.grid(row=1, column=0, sticky="nsew")
        preview.insert("1.0", safe_terminal_text(request.preview))
        preview.configure(state="disabled")

        actions = tk.Frame(body, bg=colors["surface"])
        actions.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        allow = tk.Button(
            actions,
            text="السماح لهذه العملية (Alt+A)",
            command=self.allow,
            bg=colors["copper"],
            fg="white",
            activebackground=colors["focus"],
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=10,
            cursor="hand2",
        )
        allow.pack(side="right")
        deny = tk.Button(
            actions,
            text="رفض (Alt+R)",
            command=self.reject,
            bg=colors["surface"],
            fg=colors["ink"],
            relief="solid",
            borderwidth=1,
            padx=24,
            pady=10,
            cursor="hand2",
        )
        deny.pack(side="right", padx=(0, 10))
        deny.focus_set()

    def allow(self):
        self._finish(True)

    def reject(self, _event=None):
        self._finish(False)

    def _finish(self, approved):
        if self.finished:
            return
        self.finished = True
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()
        self.on_result(self.request, approved)
