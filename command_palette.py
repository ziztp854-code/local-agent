import tkinter as tk
import theme


class CommandPalette(tk.Toplevel):
    def __init__(self, master, actions, colors, on_close=None):
        super().__init__(master)
        self.title("لوحة الأوامر")
        self.configure(bg=colors["surface"])
        self.transient(master)
        self.resizable(False, False)
        self._actions = list(actions)
        self._colors = colors
        self._on_close = on_close
        self._filtered = []

        self.entry = tk.Entry(
            self,
            justify="right",
            relief="solid",
            borderwidth=1,
            width=48,
            bg=colors["fog"],
            fg=colors["ink"],
            insertbackground=colors["ink"],
        )
        self.entry.pack(fill="x", padx=14, pady=(14, 8), ipady=7)
        self.entry.bind("<KeyRelease>", self._refresh)
        self.entry.bind("<Down>", self._move)
        self.entry.bind("<Up>", self._move)
        self.entry.bind("<Return>", self._run)
        self.entry.bind("<Escape>", self._cancel)

        self.listbox = tk.Listbox(
            self,
            activestyle="none",
            relief="flat",
            bg=colors["surface"],
            fg=colors["ink"],
            selectbackground=colors["soft_teal"],
            selectforeground=colors["teal"],
            font=theme.ui_font(10),
            borderwidth=0,
            highlightthickness=0,
        )
        self.listbox.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.listbox.bind("<Return>", self._run)
        self.listbox.bind("<Double-Button-1>", self._run)
        self.listbox.bind("<Escape>", self._cancel)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._refresh()
        self.entry.focus_set()

    def _matches(self):
        query = self.entry.get().strip().casefold()
        return [
            (label, callback)
            for label, callback in self._actions
            if query in label.casefold()
        ]

    def _refresh(self, _event=None):
        self._filtered = self._matches()
        self.listbox.delete(0, "end")
        for label, _callback in self._filtered:
            self.listbox.insert("end", label)
        self.listbox.configure(height=min(max(len(self._filtered), 1), 12))
        if self._filtered:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set(0)
            self.listbox.activate(0)

    def _move(self, event):
        if not self._filtered:
            return "break"
        current = self.listbox.curselection()
        index = current[0] if current else 0
        step = 1 if event.keysym == "Down" else -1
        index = max(0, min(len(self._filtered) - 1, index + step))
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(index)
        self.listbox.activate(index)
        self.listbox.see(index)
        return "break"

    def _selected(self):
        if not self._filtered:
            return None
        current = self.listbox.curselection()
        index = current[0] if current else 0
        return self._filtered[index]

    def _run(self, _event=None):
        selected = self._selected()
        if selected is None:
            return "break"
        _label, callback = selected
        self._close()
        callback()
        return "break"

    def _cancel(self, _event=None):
        self._close()
        return "break"

    def _close(self):
        if self._on_close is not None:
            self._on_close()
        try:
            self.master.focus_set()
        except tk.TclError:
            pass
        self.destroy()
