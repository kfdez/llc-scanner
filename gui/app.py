# -*- coding: utf-8 -*-
"""
Tkinter desktop GUI for the Pokemon Card Identifier.
"""

import re
import threading
import webbrowser
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk

from db.database import card_count, hash_count, embedding_count
from identifier.matcher import identify_card, reload_index
from identifier.embedding_matcher import identify_card_embedding, reload_embedding_index


CONF_COLORS = {
    "high": "#2ecc71",
    "medium": "#f39c12",
    "low": "#e74c3c",
}

# Minimum and aspect ratio constants for dynamic image scaling
CARD_ASPECT = 336 / 240          # height / width  (standard Pokemon card ~1.4)
SCAN_MIN_W = 180
REF_MIN_W = 140

# Batch grid thumbnail size (width, height)
THUMB_W, THUMB_H = 70, 98


# ── Batch data model ──────────────────────────────────────────────────────────

@dataclass
class BatchRow:
    image_path: str                    # original scan file path (front)
    candidates: list                   # top-k result dicts (same schema as single mode)
    current_idx: int = 0               # which candidate is active (0 = best guess)
    row_number: int = 0                # 1-based position in the batch (for label generation)
    widgets: dict = field(default_factory=dict)  # tk widget refs for in-place updates
    back_image_path: str = ""          # optional back-of-card scan path


# ── App ───────────────────────────────────────────────────────────────────────

class CardIdentifierApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("LLC Scanner - v1-beta2")
        self.configure(bg="#1a1a2e")
        self.geometry("1440x900")
        self.minsize(900, 600)

        # ── Windows App User Model ID — must be set before any window is shown ──
        # This tells Windows to group the taskbar button under our app identity
        # rather than under pythonw.exe, and makes iconbitmap() apply to the
        # taskbar button as well as the title bar.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "LowLatencyCards.LLCScanner.1"
            )
        except Exception:
            pass

        # ── App icon — taskbar + title bar ──
        _assets = Path(__file__).resolve().parent / "assets"
        try:
            _ico = _assets / "logo.ico"
            if _ico.exists():
                self.iconbitmap(default=str(_ico))
        except Exception:
            pass  # icon is optional
        try:
            _icon = Image.open(_assets / "logo_black.png").convert("RGBA")
            self._app_icon = ImageTk.PhotoImage(_icon)
            self.iconphoto(True, self._app_icon)
        except Exception:
            pass  # icon is optional — missing file is fine

        self._scan_photo: ImageTk.PhotoImage | None = None
        self._ref_photo: ImageTk.PhotoImage | None = None
        self._scan_pil: Image.Image | None = None
        self._ref_pil: Image.Image | None = None

        # Matcher mode (shared between single and batch tabs)
        self._matcher_mode = tk.StringVar(value="ML (GPU)")

        # Batch state
        self._front_back_mode = tk.BooleanVar(value=False)  # pair every 2 scans as front+back
        self._batch_rows: list[BatchRow] = []
        self._batch_running = False
        self._thumb_cache: dict[tuple, ImageTk.PhotoImage] = {}
        self._preview_cache: dict[str, ImageTk.PhotoImage] = {}
        self._hover_after_id: str | None = None
        self._hover_toplevel: tk.Toplevel | None = None
        self._batch_name_var  = tk.StringVar()
        self._price_mult_var  = tk.StringVar(value="1.00")

        # Load saved column widths (must happen before _build_ui)
        self._COL_W = self._load_col_widths()
        # Header cell frames keyed by column name — populated in _build_batch_tab
        self._hdr_cells: dict[str, tk.Frame] = {}

        # Flush pending geometry/theme events before attaching the menu.
        # Without this, Tk 8.6 on Windows 11 (Python 3.13) creates the native
        # menu bar at zero height when the window bg is a dark colour, so the
        # menu is invisible even though self.config(menu=...) succeeds.
        self.update_idletasks()
        self._build_menu()
        self._build_ui()
        self._check_first_run()

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self)
        setup_menu = tk.Menu(menubar, tearoff=0)
        setup_menu.add_command(label="Download / Update Card Database", command=self._run_setup)
        setup_menu.add_command(label="Refresh Card Metadata (set names, rarities...)", command=self._run_refresh_metadata)
        setup_menu.add_command(label="Rehash All Cards (fixes accuracy)", command=self._run_rehash)
        setup_menu.add_separator()
        setup_menu.add_command(label="Build Embeddings (ML, GPU)", command=self._run_build_embeddings)
        setup_menu.add_command(label="Rebuild Embeddings (ML, GPU)", command=self._run_rebuild_embeddings)
        setup_menu.add_separator()
        setup_menu.add_command(label="Change Data Directory...", command=self._change_data_dir)
        setup_menu.add_command(label="Relink Images from Folder...", command=self._relink_images)
        menubar.add_cascade(label="Setup", menu=setup_menu)

        export_menu = tk.Menu(menubar, tearoff=0)
        export_menu.add_command(label="Export Batch to eBay CSV...", command=self._export_ebay_csv)
        export_menu.add_separator()
        export_menu.add_command(label="eBay Export Settings...", command=self._open_ebay_settings)
        menubar.add_cascade(label="Export", menu=export_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="LLC Scanner Help...", command=self._open_help)
        help_menu.add_separator()
        help_menu.add_command(label="About LLC Scanner...", command=self._open_about)
        help_menu.add_command(label="Support Development (Donate)...", command=self._open_donate)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)
        # Keep a reference so _reattach_menu() can re-apply it after the
        # event loop starts (belt-and-suspenders for the Tk/Win11 timing bug).
        self._menubar = menubar
        self.after(1, self._reattach_menu)

    def _reattach_menu(self):
        """Re-apply the menu bar once the event loop is running.

        On Python 3.13 + Windows 11, self.config(menu=...) called inside
        __init__ (before mainloop) sometimes produces a zero-height native
        menu bar. Reattaching after the first event-loop tick forces Windows
        to resize the frame and show the menu correctly.
        """
        self.config(menu=self._menubar)

    # ------------------------------------------------------------------
    # UI layout
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Root grid: row 0 = notebook (expands), row 1 = bottom bar
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        # ── Main frame — batch only (single card tab hidden for now) ──
        batch_tab = tk.Frame(self, bg="#1a1a2e")
        batch_tab.grid(row=0, column=0, sticky="nsew", padx=6, pady=(6, 0))

        self._build_batch_tab(batch_tab)

        # ── Bottom bar ──
        bottom = tk.Frame(self, bg="#16213e")
        bottom.grid(row=1, column=0, sticky="ew", padx=10, pady=6)

        self._db_info_var = tk.StringVar(value="")
        tk.Label(bottom, textvariable=self._db_info_var, bg="#16213e", fg="#555577",
                 font=("Helvetica", 9)).pack(side="left")

        tk.Label(bottom, text="LLC Scanner  •  v1-beta2  •  vibe coded with Claude AI  •  © 2026 Kyle Fernandez",
                 bg="#16213e", fg="#555577",
                 font=("Helvetica", 9, "italic")).pack(side="right")

        self._update_db_info()

    # ── Single Card tab ────────────────────────────────────────────────

    def _build_single_tab(self, parent):
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        # ---- Top bar ----
        top = tk.Frame(parent, bg="#16213e")
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 4))

        self._open_btn = tk.Button(
            top,
            text="Open Card Image",
            command=self._open_image,
            bg="#0f3460",
            fg="white",
            activebackground="#533483",
            font=("Helvetica", 12, "bold"),
            relief="flat",
            padx=16,
            pady=8,
            cursor="hand2",
        )
        self._open_btn.pack(side="left")

        self._status_var = tk.StringVar(value="Ready — open an image to identify a card.")
        tk.Label(
            top,
            textvariable=self._status_var,
            bg="#16213e",
            fg="#a0a0b0",
            font=("Helvetica", 10),
        ).pack(side="left", padx=12)

        # Matcher mode selector (right-aligned)
        tk.Label(
            top, text="Matcher:",
            bg="#16213e", fg="#a0a0b0",
            font=("Helvetica", 10),
        ).pack(side="right", padx=(0, 4))

        mode_selector = ttk.Combobox(
            top,
            textvariable=self._matcher_mode,
            values=["Hash", "ML (GPU)", "Hybrid (both)"],
            state="readonly",
            width=13,
            font=("Helvetica", 10),
        )
        mode_selector.pack(side="right", padx=(0, 8))

        # ---- Main body — PanedWindow ----
        self._pane = tk.PanedWindow(
            parent, orient=tk.HORIZONTAL,
            bg="#1a1a2e", sashwidth=6, sashrelief="flat",
            handlesize=0,
        )
        self._pane.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)

        # Left pane: scanned card
        left = tk.LabelFrame(self._pane, text="Scanned Card", bg="#16213e", fg="#a0a0b0",
                              font=("Helvetica", 10))
        self._pane.add(left, minsize=SCAN_MIN_W + 20, stretch="always")

        self._scan_canvas = tk.Label(left, bg="#0d0d1a", text="No image loaded",
                                     fg="#444466", font=("Helvetica", 10))
        self._scan_canvas.pack(fill="both", expand=True, padx=8, pady=8)
        left.bind("<Configure>", self._on_left_resize)

        # Right pane: match results
        right = tk.LabelFrame(self._pane, text="Match Results", bg="#16213e", fg="#a0a0b0",
                               font=("Helvetica", 10))
        self._pane.add(right, minsize=320, stretch="always")

        top_match = tk.Frame(right, bg="#16213e")
        top_match.pack(fill="x", padx=8, pady=8)
        top_match.columnconfigure(1, weight=1)

        self._ref_canvas = tk.Label(top_match, bg="#0d0d1a", text="—",
                                    fg="#444466", font=("Helvetica", 10))
        self._ref_canvas.grid(row=0, column=0, padx=(0, 10), sticky="nw")
        right.bind("<Configure>", self._on_right_resize)

        info_frame = tk.Frame(top_match, bg="#16213e")
        info_frame.grid(row=0, column=1, sticky="nw")

        self._name_var = tk.StringVar(value="")
        tk.Label(info_frame, textvariable=self._name_var, bg="#16213e", fg="white",
                 font=("Helvetica", 14, "bold"), anchor="w").pack(anchor="w")

        self._set_var = tk.StringVar(value="")
        tk.Label(info_frame, textvariable=self._set_var, bg="#16213e", fg="#a0a0b0",
                 font=("Helvetica", 10), anchor="w").pack(anchor="w")

        self._details_var = tk.StringVar(value="")
        tk.Label(info_frame, textvariable=self._details_var, bg="#16213e", fg="#a0a0b0",
                 font=("Helvetica", 10), anchor="w").pack(anchor="w", pady=(4, 0))

        self._conf_var = tk.StringVar(value="")
        self._conf_label = tk.Label(info_frame, textvariable=self._conf_var, bg="#16213e",
                                    font=("Helvetica", 11, "bold"), anchor="w")
        self._conf_label.pack(anchor="w", pady=(8, 0))

        ttk.Separator(right, orient="horizontal").pack(fill="x", padx=8, pady=4)
        tk.Label(right, text="Other candidates:", bg="#16213e", fg="#a0a0b0",
                 font=("Helvetica", 9)).pack(anchor="w", padx=8)
        self._alts_frame = tk.Frame(right, bg="#16213e")
        self._alts_frame.pack(fill="x", padx=8, pady=(2, 8))

    # ── Batch tab ──────────────────────────────────────────────────────

    def _build_batch_tab(self, parent):
        parent.rowconfigure(5, weight=1)
        parent.columnconfigure(0, weight=1)

        # ---- Toolbar row 1: buttons + matcher ----
        toolbar = tk.Frame(parent, bg="#16213e")
        toolbar.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))

        # ── Logo (white version) ──
        _assets = Path(__file__).parent / "assets"
        self._toolbar_logo_photo = None
        try:
            _logo = Image.open(_assets / "logo_white.png").convert("RGBA")
            _logo.thumbnail((48, 48), Image.LANCZOS)
            self._toolbar_logo_photo = ImageTk.PhotoImage(_logo)
            tk.Label(toolbar, image=self._toolbar_logo_photo,
                     bg="#16213e").pack(side="left", padx=(0, 10))
        except Exception:
            pass  # logo is optional

        btn_kw = dict(bg="#0f3460", fg="white", activebackground="#533483",
                      font=("Helvetica", 11, "bold"), relief="flat",
                      padx=14, pady=7, cursor="hand2")

        self._batch_files_btn = tk.Button(toolbar, text="Open Files…",
                                          command=self._open_batch_files, **btn_kw)
        self._batch_files_btn.pack(side="left", padx=(0, 6))

        self._batch_folder_btn = tk.Button(toolbar, text="Open Folder…",
                                           command=self._open_batch_folder, **btn_kw)
        self._batch_folder_btn.pack(side="left")

        tk.Button(toolbar, text="Export CSV…",
                  command=self._export_ebay_csv,
                  bg="#145214", fg="white", activebackground="#1e7a1e",
                  font=("Helvetica", 11, "bold"), relief="flat",
                  padx=14, pady=7, cursor="hand2").pack(side="left", padx=(12, 0))

        tk.Label(toolbar, text="Matcher:", bg="#16213e", fg="#a0a0b0",
                 font=("Helvetica", 10)).pack(side="right", padx=(0, 4))
        ttk.Combobox(toolbar, textvariable=self._matcher_mode,
                     values=["Hash", "ML (GPU)", "Hybrid (both)"],
                     state="readonly", width=13,
                     font=("Helvetica", 10)).pack(side="right", padx=(0, 8))

        # ---- Toolbar row 1: front/back checkbox ----
        _cb_style = ttk.Style()
        _cb_style.configure("Toolrow.TCheckbutton",
                            background="#16213e", foreground="#a0a0b0",
                            font=("Helvetica", 10))
        fb_row = tk.Frame(parent, bg="#16213e")
        fb_row.grid(row=1, column=0, sticky="w", padx=10, pady=(2, 0))
        ttk.Checkbutton(fb_row, text="Front + Back scans",
                        variable=self._front_back_mode,
                        style="Toolrow.TCheckbutton"
                        ).pack(side="left")

        # ---- Toolbar row 2: batch name ----
        name_row = tk.Frame(parent, bg="#16213e")
        name_row.grid(row=2, column=0, sticky="ew", padx=10, pady=(2, 4))
        name_row.columnconfigure(1, weight=1)   # batch name entry expands
        # columns 2 & 3 are fixed-width (× label and multiplier entry)

        tk.Label(name_row, text="Batch Name:", bg="#16213e", fg="#a0a0b0",
                 font=("Helvetica", 10)).grid(row=0, column=0, sticky="w", padx=(0, 8))
        batch_name_entry = tk.Entry(name_row, textvariable=self._batch_name_var,
                                    bg="#0d0d1a", fg="white", insertbackground="white",
                                    relief="flat", font=("Helvetica", 10))
        batch_name_entry.grid(row=0, column=1, sticky="ew", ipady=4)

        tk.Label(name_row, text="Price ×", bg="#16213e", fg="#a0a0b0",
                 font=("Helvetica", 10)).grid(row=0, column=2, sticky="w", padx=(14, 4))
        mult_entry = tk.Entry(name_row, textvariable=self._price_mult_var,
                              bg="#0d0d1a", fg="white", insertbackground="white",
                              relief="flat", font=("Helvetica", 10),
                              justify="center", width=6)
        mult_entry.grid(row=0, column=3, ipady=4)

        def _on_batch_name_change(*_):
            """Re-compute the custom label for every row when the batch name changes."""
            batch = self._batch_name_var.get().strip()
            for br in self._batch_rows:
                lbl = f"{batch}-{br.row_number}" if batch else str(br.row_number)
                lw = br.widgets.get("label_var")
                if lw:
                    lw.set(lbl)

        self._batch_name_var.trace_add("write", _on_batch_name_change)

        def _on_mult_change(*_):
            """Re-apply the multiplier to all non-manually-edited price fields."""
            for br in self._batch_rows:
                if br.widgets and not br.widgets.get("price_user_edited", [False])[0]:
                    self._refresh_price(br)

        self._price_mult_var.trace_add("write", _on_mult_change)

        # ---- Toolbar row 3: status + progress bar ----
        prog_row = tk.Frame(parent, bg="#16213e")
        prog_row.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 4))
        prog_row.columnconfigure(1, weight=1)

        self._batch_status_var = tk.StringVar(value="Open files or a folder to begin.")
        tk.Label(prog_row, textvariable=self._batch_status_var,
                 bg="#16213e", fg="#a0a0b0", font=("Helvetica", 9),
                 anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 10))

        self._batch_bar_var = tk.IntVar(value=0)
        self._batch_bar = ttk.Progressbar(prog_row, mode="determinate", maximum=100,
                                          variable=self._batch_bar_var)
        self._batch_bar.grid(row=0, column=1, sticky="ew")

        # ---- Column header row (resizable) ----
        # Column key order must stay in sync with _build_batch_row cell order.
        _HDR_COLS = [
            ("scan",    "Scan"),    ("ref",     "Ref"),
            ("title",   "Title"),   ("label",   "Label"),
            ("qty",     "Qty"),     ("price",   "Price $"),
            ("name",    "Name"),    ("set",     "Set"),
            ("num",     "#/Total"), ("rarity",  "Rarity"),
            ("finish",  "Finish"),  ("edition", "Edition"),
            ("conf",    "Conf"),    ("cond",    "Cond"),
            ("game",    "Game"),    ("desc",    "Desc"),
            ("actions", "Actions"),
        ]
        # Header lives in a canvas so it can scroll horizontally in sync with the grid.
        hdr_canvas = tk.Canvas(parent, bg="#0d0d1a", highlightthickness=0, height=22)
        hdr_canvas.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 1))
        hdr = tk.Frame(hdr_canvas, bg="#0d0d1a")
        hdr_canvas.create_window((0, 0), window=hdr, anchor="nw")
        # Update header scroll region when its content changes size
        hdr.bind("<Configure>",
                 lambda e: hdr_canvas.configure(scrollregion=hdr_canvas.bbox("all")))
        # Store reference so the horizontal scrollbar can drive it
        self._hdr_canvas = hdr_canvas

        def _make_resizable_header(key, label_text):
            """Build one header cell with a right-edge drag handle."""
            cell = tk.Frame(hdr, bg="#0d0d1a",
                            width=self._COL_W[key], height=22)
            cell.pack_propagate(False)
            cell.pack(side="left")
            self._hdr_cells[key] = cell

            tk.Label(cell, text=label_text, bg="#0d0d1a", fg="#6666aa",
                     font=("Helvetica", 9, "bold"), anchor="w").place(
                x=2, y=0, relwidth=1.0, relheight=1.0)

            # Drag handle — a narrow strip on the right edge
            handle = tk.Frame(cell, bg="#3a3a6a", width=4, cursor="sb_h_double_arrow")
            handle.place(relx=1.0, x=-4, y=0, width=4, relheight=1.0)

            _drag = {"x": 0, "w": self._COL_W[key]}

            def _on_press(e, k=key):
                _drag["x"] = e.x_root
                _drag["w"] = self._COL_W[k]

            def _on_drag(e, k=key):
                delta = e.x_root - _drag["x"]
                new_w = max(self._COL_MIN_W, _drag["w"] + delta)
                self._COL_W[k] = new_w
                self._hdr_cells[k].config(width=new_w)
                # Resize the matching cell in every existing row
                for br in self._batch_rows:
                    cf = br.widgets.get(f"_cell_{k}")
                    if cf:
                        cf.config(width=new_w)

            def _on_release(e):
                self._save_col_widths()

            handle.bind("<ButtonPress-1>",   _on_press)
            handle.bind("<B1-Motion>",        _on_drag)
            handle.bind("<ButtonRelease-1>",  _on_release)

        for key, label_text in _HDR_COLS:
            _make_resizable_header(key, label_text)

        # ---- Scrollable grid ----
        grid_frame = tk.Frame(parent, bg="#1a1a2e")
        grid_frame.grid(row=5, column=0, sticky="nsew", padx=10, pady=(0, 6))
        grid_frame.rowconfigure(0, weight=1)
        grid_frame.columnconfigure(0, weight=1)

        self._batch_canvas = tk.Canvas(grid_frame, bg="#1a1a2e", highlightthickness=0)
        vscrollbar = ttk.Scrollbar(grid_frame, orient="vertical",
                                   command=self._batch_canvas.yview)

        def _hscroll(*args):
            """Drive both the grid canvas and header canvas together."""
            self._batch_canvas.xview(*args)
            self._hdr_canvas.xview(*args)

        hscrollbar = ttk.Scrollbar(grid_frame, orient="horizontal", command=_hscroll)

        def _xview_set(*args):
            """Keep header in sync when grid canvas x-position changes."""
            hscrollbar.set(*args)
            self._hdr_canvas.xview_moveto(args[0])

        self._batch_canvas.configure(
            yscrollcommand=vscrollbar.set,
            xscrollcommand=_xview_set,
        )

        self._batch_canvas.grid(row=0, column=0, sticky="nsew")
        vscrollbar.grid(row=0, column=1, sticky="ns")
        hscrollbar.grid(row=1, column=0, sticky="ew")

        self._batch_inner = tk.Frame(self._batch_canvas, bg="#1a1a2e")
        self._batch_canvas_window = self._batch_canvas.create_window(
            (0, 0), window=self._batch_inner, anchor="nw"
        )

        self._batch_inner.bind(
            "<Configure>",
            lambda e: self._batch_canvas.configure(
                scrollregion=self._batch_canvas.bbox("all")
            )
        )

        # Bind mousewheel scrolling
        def _on_mousewheel(event):
            self._batch_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self._batch_canvas.bind("<MouseWheel>", _on_mousewheel)
        self._batch_inner.bind("<MouseWheel>", _on_mousewheel)

    # ------------------------------------------------------------------
    # Single-card actions
    # ------------------------------------------------------------------

    def _open_image(self):
        path = filedialog.askopenfilename(
            title="Select card image",
            filetypes=[("Image files", "*.jpg *.jpeg *.png *.webp *.bmp *.tiff"), ("All files", "*.*")],
        )
        if not path:
            return

        self._status_var.set("Identifying card...")
        self._open_btn.config(state="disabled")
        self.update_idletasks()

        self._show_scan_preview(path)

        threading.Thread(target=self._identify_worker, args=(path,), daemon=True).start()

    def _identify_worker(self, path: str):
        try:
            results = self._run_identify(path)
        except Exception as e:
            self.after(0, self._show_error, str(e))
            return
        self.after(0, self._show_results, results)

    def _run_identify(self, path: str) -> list[dict]:
        """Dispatch identification to the active matcher. Used by both single and batch."""
        mode = self._matcher_mode.get()

        if mode == "Hash":
            return identify_card(path)

        if mode == "ML (GPU)":
            results = identify_card_embedding(path)
            return results if results else identify_card(path)

        # Hybrid
        from config import EMBEDDING_CONFIDENCE_MED
        ml_results   = identify_card_embedding(path)
        hash_results = identify_card(path)
        if ml_results and ml_results[0]["distance"] >= EMBEDDING_CONFIDENCE_MED:
            return ml_results
        return hash_results if hash_results else ml_results

    # ------------------------------------------------------------------
    # Dynamic resize handlers (single-card tab)
    # ------------------------------------------------------------------

    def _on_left_resize(self, event):
        if self._scan_pil is not None:
            self._render_scan(event.width, event.height)

    def _on_right_resize(self, event):
        if self._ref_pil is not None:
            self._render_ref(event.width, event.height)

    def _render_scan(self, panel_w: int, panel_h: int):
        if self._scan_pil is None:
            return
        avail_w = max(SCAN_MIN_W, panel_w - 20)
        avail_h = max(int(SCAN_MIN_W * CARD_ASPECT), panel_h - 20)
        img = self._scan_pil.copy()
        img.thumbnail((avail_w, avail_h), Image.LANCZOS)
        self._scan_photo = ImageTk.PhotoImage(img)
        self._scan_canvas.config(image=self._scan_photo, text="")

    def _render_ref(self, panel_w: int, panel_h: int):
        if self._ref_pil is None:
            return
        avail_w = max(REF_MIN_W, min(int(panel_w * 0.30), 280))
        avail_h = max(int(REF_MIN_W * CARD_ASPECT), int(panel_h * 0.55))
        img = self._ref_pil.copy()
        img.thumbnail((avail_w, avail_h), Image.LANCZOS)
        self._ref_photo = ImageTk.PhotoImage(img)
        self._ref_canvas.config(image=self._ref_photo, text="")

    def _annotate_ref_image(self, img: Image.Image, top: dict) -> Image.Image:
        """
        Draw a semi-transparent dark bar at the bottom of the reference image
        containing the card name (line 1) and set / number / rarity (line 2).
        """
        img = img.copy().convert("RGBA")
        w, h = img.size

        bar_h = max(44, int(h * 0.14))

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw_ov = ImageDraw.Draw(overlay)
        draw_ov.rectangle([(0, h - bar_h), (w, h)], fill=(0, 0, 0, 185))
        img = Image.alpha_composite(img, overlay)

        draw = ImageDraw.Draw(img)

        line1 = top.get("name", "")
        line2 = f"{top.get('set_name', '')}  #{top.get('number', '')}"
        if top.get("rarity"):
            line2 += f"  ·  {top['rarity']}"

        size_large = max(12, bar_h // 3)
        size_small  = max(9,  bar_h // 4)

        try:
            font_large = ImageFont.truetype("arial.ttf", size_large)
            font_small = ImageFont.truetype("arial.ttf", size_small)
        except Exception:
            font_large = ImageFont.load_default()
            font_small = font_large

        y_top = h - bar_h + 5
        draw.text((6, y_top),                    line1, fill=(255, 255, 255, 255), font=font_large)
        draw.text((6, y_top + size_large + 3), line2, fill=(180, 180, 180, 255), font=font_small)

        return img.convert("RGB")

    def _show_scan_preview(self, path: str):
        try:
            self._scan_pil = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
            w = self._scan_canvas.winfo_width() or 240
            h = self._scan_canvas.winfo_height() or 336
            self._render_scan(w, h)
        except Exception:
            self._scan_pil = None
            self._scan_canvas.config(image="", text="Preview unavailable")

    def _show_results(self, results: list[dict]):
        self._open_btn.config(state="normal")

        if not results:
            self._status_var.set("No match found. Is the database populated?")
            self._clear_results()
            return

        top = results[0]
        self._status_var.set(f"Best match: {top['name']} ({top['set_name']})")

        self._name_var.set(top["name"])
        self._set_var.set(f"{top['set_name']}  #{top['number']}")
        details = []
        if top["rarity"]:
            details.append(top["rarity"])
        if top["category"]:
            details.append(top["category"])
        if top["hp"]:
            details.append(f"HP {top['hp']}")
        self._details_var.set("  ·  ".join(details))

        conf = top["confidence"]
        mode = self._matcher_mode.get()
        score_label = "dist" if mode == "Hash" else "sim"
        self._conf_var.set(f"Confidence: {conf.upper()}  ({score_label}={top['distance']})")
        self._conf_label.config(fg=CONF_COLORS.get(conf, "white"))

        # Reference image thumbnail (with card info overlaid at the bottom)
        ref_img_path = top.get("local_image_path")
        if ref_img_path and Path(ref_img_path).exists():
            try:
                self._ref_pil = Image.open(ref_img_path).convert("RGB")
                self._ref_pil = self._annotate_ref_image(self._ref_pil, top)
                w = self._ref_canvas.winfo_width() or 180
                h = self._ref_canvas.winfo_height() or 252
                self._render_ref(w, h)
            except Exception:
                self._ref_pil = None
                self._ref_canvas.config(image="", text="No image")
        else:
            self._ref_pil = None
            self._ref_canvas.config(image="", text="No image")

        for widget in self._alts_frame.winfo_children():
            widget.destroy()

        score_key = "dist" if mode == "Hash" else "sim"
        for alt in results[1:]:
            conf_color = CONF_COLORS.get(alt["confidence"], "#888")
            text = (
                f"{alt['name']}  ·  {alt['set_name']}  "
                f"#{alt['number']}  ({score_key}={alt['distance']})"
            )
            tk.Label(
                self._alts_frame,
                text=text,
                bg="#16213e",
                fg=conf_color,
                font=("Helvetica", 9),
                anchor="w",
            ).pack(anchor="w")

    def _show_error(self, msg: str):
        self._open_btn.config(state="normal")
        self._status_var.set(f"Error: {msg}")
        messagebox.showerror("Identification Error", msg)

    def _clear_results(self):
        self._name_var.set("")
        self._set_var.set("")
        self._details_var.set("")
        self._conf_var.set("")
        self._ref_pil = None
        self._ref_canvas.config(image="", text="—")
        for widget in self._alts_frame.winfo_children():
            widget.destroy()

    # ------------------------------------------------------------------
    # Batch tab actions
    # ------------------------------------------------------------------

    def _open_batch_files(self):
        paths = filedialog.askopenfilenames(
            title="Select card images",
            filetypes=[("Image files", "*.jpg *.jpeg *.png *.webp *.bmp *.tiff"),
                       ("All files", "*.*")],
        )
        if paths:
            self._start_batch(list(paths))

    def _open_batch_folder(self):
        folder = filedialog.askdirectory(title="Select folder of card images")
        if not folder:
            return
        extensions = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}
        paths = sorted(
            str(p) for p in Path(folder).iterdir()
            if p.suffix.lower() in extensions
        )
        if not paths:
            messagebox.showinfo("No Images", "No image files found in that folder.")
            return
        self._start_batch(paths)

    def _start_batch(self, paths: list[str]):
        if self._batch_running:
            return

        # Clear previous results
        self._batch_rows.clear()
        self._thumb_cache.clear()
        for widget in self._batch_inner.winfo_children():
            widget.destroy()

        self._batch_bar_var.set(0)
        self._batch_bar.config(mode="indeterminate")
        self._batch_bar.start(12)   # pulse every 12 ms
        self._batch_status_var.set(f"Loading {len(paths)} image{'s' if len(paths) != 1 else ''}…")
        self._batch_files_btn.config(state="disabled")
        self._batch_folder_btn.config(state="disabled")
        self._batch_running = True

        threading.Thread(
            target=self._batch_identify_worker,
            args=(paths, self._front_back_mode.get()),
            daemon=True,
        ).start()

    def _batch_identify_worker(self, paths: list[str], front_back: bool = False):
        if front_back:
            # Pair paths: [front0, back0, front1, back1, ...]
            # Only identify front images; backs are paired after.
            fronts = paths[0::2]
            backs  = paths[1::2]
            total  = len(fronts)
            for i, front_path in enumerate(fronts):
                back_path = backs[i] if i < len(backs) else ""
                try:
                    results = self._run_identify(front_path)
                except Exception:
                    results = []
                self.after(0, self._on_batch_result, front_path, results,
                           i + 1, total, back_path)
        else:
            total = len(paths)
            for i, path in enumerate(paths):
                try:
                    results = self._run_identify(path)
                except Exception:
                    results = []
                self.after(0, self._on_batch_result, path, results, i + 1, total, "")
        self.after(0, self._on_batch_complete, total)

    def _on_batch_result(self, path: str, results: list[dict], done: int, total: int,
                         back_path: str = ""):
        # Switch from indeterminate pulse → determinate fill on first result
        if done == 1:
            self._batch_bar.stop()
            self._batch_bar.config(mode="determinate")
        pct = int(done / total * 100) if total else 100
        self._batch_bar_var.set(pct)
        self._batch_status_var.set(f"Identified {done} / {total}")

        row = BatchRow(image_path=path, candidates=results,
                       row_number=len(self._batch_rows) + 1,
                       back_image_path=back_path)
        self._batch_rows.append(row)
        self._build_batch_row(self._batch_inner, row)

    def _on_batch_complete(self, total: int):
        self._batch_running = False
        self._batch_bar.stop()
        self._batch_bar.config(mode="determinate")
        self._batch_bar_var.set(100)
        self._batch_status_var.set(f"Done — {total} card{'s' if total != 1 else ''} identified.")
        self._batch_files_btn.config(state="normal")
        self._batch_folder_btn.config(state="normal")

    # ------------------------------------------------------------------
    # Batch row builder
    # ------------------------------------------------------------------

    # Default pixel widths for each batch column.
    # These are overwritten at runtime from settings.json if the user has
    # previously resized columns.
    _COL_W_DEFAULTS = {
        "scan": 78, "ref": 78, "title": 260, "label": 140, "name": 175, "set": 135,
        "num": 80, "rarity": 90, "finish": 115, "edition": 95, "conf": 90,
        "qty": 55, "price": 80, "cond": 120, "game": 100, "desc": 120,
        "actions": 120,
    }

    _COND_SHORT = {
        "Near Mint":         "NM",
        "Lightly Played":    "LP",
        "Moderately Played": "MP",
        "Heavily Played":    "HP",
        "Damaged":           "DMG",
    }
    # Minimum pixel width for any column (prevents accidental collapse)
    _COL_MIN_W = 24

    @classmethod
    def _load_col_widths(cls) -> dict:
        """Return column widths from settings.json, falling back to defaults."""
        import config as _cfg
        from config import _load_settings
        saved = _load_settings().get("batch_col_widths", {})
        widths = dict(cls._COL_W_DEFAULTS)
        for key in widths:
            if key in saved and isinstance(saved[key], int) and saved[key] >= cls._COL_MIN_W:
                widths[key] = saved[key]
        return widths

    def _save_col_widths(self):
        """Persist current _COL_W to settings.json."""
        import config as _cfg
        _cfg.save_settings(extra={"batch_col_widths": dict(self._COL_W)})

    _CONDITION_OPTIONS = ["Near Mint", "Lightly Played", "Moderately Played",
                          "Heavily Played", "Damaged"]

    def _build_batch_row(self, parent: tk.Frame, row: BatchRow):
        """Create all widgets for one batch result row and store refs in row.widgets."""
        row_idx = len(parent.winfo_children())
        bg = "#16213e" if row_idx % 2 == 0 else "#1a1a2e"
        cw = self._COL_W

        frame = tk.Frame(parent, bg=bg, pady=3)
        frame.pack(fill="x", padx=0, pady=1)

        def _mw(event):
            self._batch_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_mw(w):
            w.bind("<MouseWheel>", _mw)
            return w

        ROW_H = THUMB_H + 8  # total row height driven by thumbnail

        def _cell(width, height=ROW_H):
            """Fixed-size cell frame — explicit height ensures images render."""
            f = tk.Frame(frame, bg=bg, width=width, height=height)
            f.pack_propagate(False)
            f.pack(side="left")
            _bind_mw(f)
            return f

        top = row.candidates[row.current_idx] if row.candidates else {}

        # Helper that also registers the cell frame for column-resize updates
        _cells: dict[str, tk.Frame] = {}
        def _named_cell(key, height=ROW_H):
            f = _cell(cw[key], height)
            _cells[key] = f
            return f

        # ── Scan thumbnail (front overlaid on back when both exist) ──
        scan_cell = _named_cell("scan")
        if row.back_image_path:
            # Back rendered first (lower Z-order), front placed on top offset slightly
            # so both are visible as a "fanned" stack.
            thumb_scan_back = _bind_mw(tk.Label(scan_cell, bg=bg))
            thumb_scan_back.place(relx=0.56, rely=0.54, anchor="center")
            self._set_thumb(thumb_scan_back, row.back_image_path, bg)
            self._attach_hover_preview(thumb_scan_back, row.back_image_path)

            thumb_scan = _bind_mw(tk.Label(scan_cell, bg=bg))
            thumb_scan.place(relx=0.44, rely=0.46, anchor="center")
            self._set_thumb(thumb_scan, row.image_path, bg)
            self._attach_hover_preview(thumb_scan, row.image_path)
        else:
            thumb_scan = _bind_mw(tk.Label(scan_cell, bg=bg))
            thumb_scan.place(relx=0.5, rely=0.5, anchor="center")
            self._set_thumb(thumb_scan, row.image_path, bg)
            self._attach_hover_preview(thumb_scan, row.image_path)

        # ── Reference thumbnail (updates when candidate changes) ──
        ref_cell = _named_cell("ref")
        thumb_ref = _bind_mw(tk.Label(ref_cell, bg=bg))
        thumb_ref.place(relx=0.5, rely=0.5, anchor="center")
        ref_path = top.get("local_image_path", "") if top else ""
        if ref_path and Path(ref_path).exists():
            self._set_thumb(thumb_ref, ref_path, bg)
            self._attach_hover_preview(thumb_ref, ref_path)

        # ── Title (auto-generated: Name · #/Total · Set · Rarity · Cond) ──
        title_cell = _named_cell("title")
        title_lbl = _bind_mw(tk.Label(title_cell, text="",
                                       bg=bg, fg="#d0d0e8",
                                       font=("Helvetica", 10),
                                       anchor="w", justify="left",
                                       wraplength=cw["title"] - 8))
        title_lbl.place(x=4, y=0, relwidth=1.0, relheight=1.0)

        # Character counter — hidden until title approaches 80 chars
        _TITLE_WARN  = 65   # show counter from here
        _TITLE_LIMIT = 80   # turn red from here
        title_counter = tk.Label(title_cell, text="",
                                  bg=bg, fg="#f0a500",
                                  font=("Helvetica", 7, "bold"),
                                  anchor="e")
        title_counter.place(relx=1.0, rely=0.0, anchor="ne", x=-2, y=2)

        def _update_title_counter(text: str):
            n = len(text)
            if n >= _TITLE_WARN:
                title_counter.config(
                    text=f"{n}/80",
                    fg="#e03030" if n > _TITLE_LIMIT else "#f0a500",
                )
                title_counter.lift()
            else:
                title_counter.config(text="")

        # Finish values that are noteworthy enough to appear in the title
        _HOLO_FINISHES = {
            "Holo", "Reverse Holo", "Poke Ball Holo", "Master Ball Holo",
        }

        def _build_title(candidate: dict, cond: str, edition: str = "",
                         finish: str = "", set_name_override: str | None = None) -> str:
            parts = [candidate.get("name", "") or ""]
            num = self._fmt_number(candidate)
            if num:
                parts.append(num)
            s = set_name_override if set_name_override is not None else (candidate.get("set_name", "") or "")
            sid = candidate.get("set_id", "") or ""
            # Always start with full set name
            parts.append(s) if s else None
            # Include edition only when it's meaningful (WotC era, 1st Edition selected)
            if edition and edition != "Unlimited":
                parts.append(edition)
            r = candidate.get("rarity", "") or ""
            if r and r.lower() != "none":
                parts.append(r)
            # Include finish only for notable holo variants
            if finish and finish in _HOLO_FINISHES:
                parts.append(finish)
            short_cond = self._COND_SHORT.get(cond, cond)
            if short_cond:
                parts.append(short_cond)
            title = " - ".join(p for p in parts if p)
            # If title exceeds 80 chars and the set is swsh/sv era, swap set name for set_id
            _modern_prefixes = ("swsh", "sv")
            if (len(title) > 80 and s and sid
                    and sid.lower().startswith(_modern_prefixes)):
                parts[parts.index(s)] = sid
                title = " - ".join(p for p in parts if p)
            return title

        # _edition_ref, _finish_ref, _set_var_ref are filled in after those vars are created below
        _edition_ref:  list = [""]
        _finish_ref:   list = [""]
        _set_var_ref:  list = [None]   # holds set_var once created

        def _update_title(*_):
            sv = _set_var_ref[0]
            t = _build_title(
                row.candidates[row.current_idx] if row.candidates else {},
                cond_var.get(),
                _edition_ref[0].get() if _edition_ref[0] else "",
                _finish_ref[0].get()  if _finish_ref[0]  else "",
                set_name_override=sv.get() if sv else None,
            )
            title_lbl.config(text=t)
            _update_title_counter(t)

        # ── Custom Label (Batch Name + row number, editable) ──
        label_cell = _named_cell("label")
        batch = self._batch_name_var.get().strip()
        initial_label = f"{batch}-{row.row_number}" if batch else str(row.row_number)
        label_var = tk.StringVar(value=initial_label)
        label_entry = tk.Entry(label_cell, textvariable=label_var,
                               bg="#0d0d1a", fg="#d0d0e8",
                               insertbackground="white",
                               relief="flat", font=("Helvetica", 10))
        label_entry.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.92)

        # ── Qty spinbox ──
        qty_cell = _named_cell("qty")
        qty_var = tk.StringVar(value="1")
        qty_spin = ttk.Spinbox(qty_cell, from_=1, to=999, textvariable=qty_var,
                                font=("Helvetica", 10))
        qty_spin.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.90)

        # ── Price entry + source label ──
        price_cell = _named_cell("price")
        price_var = tk.StringVar()
        price_entry = tk.Entry(price_cell, textvariable=price_var,
                               bg="#0d0d1a", fg="white",
                               insertbackground="white",
                               relief="flat", font=("Helvetica", 10),
                               justify="right")
        price_entry.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.92)

        # source_label_var holds the tooltip text (e.g. "TCGPlayer (USD->CAD)")
        # shown on hover — no persistent label in the cell
        source_label_var = tk.StringVar(value="")
        self._attach_price_tooltip(price_entry, source_label_var)

        # Track whether the user has manually edited the price (suppress auto-fill if so)
        _price_user_edited = [False]

        def _on_price_manual_edit(*_):
            # Only flag as user-edited when the entry widget itself has focus
            if price_entry == price_cell.focus_get() if hasattr(price_cell, "focus_get") else False:
                _price_user_edited[0] = True
                source_label_var.set("")

        price_var.trace_add("write", _on_price_manual_edit)
        price_entry.bind("<FocusIn>",  lambda e: None)  # mark entry as active target
        price_entry.bind("<Key>",      lambda e: _price_user_edited.__setitem__(0, True) or source_label_var.set(""))

        # ── Name ──
        name_cell = _named_cell("name")
        name_lbl = _bind_mw(tk.Label(name_cell, text=top.get("name", "—"),
                                      bg=bg, fg="white",
                                      font=("Helvetica", 11, "bold"),
                                      anchor="w", justify="left",
                                      wraplength=cw["name"] - 8))
        name_lbl.place(x=4, y=0, relwidth=1.0, relheight=1.0)

        # ── Set (editable) ──
        set_cell = _named_cell("set")
        set_var = tk.StringVar(value=top.get("set_name", "") or "")
        _set_var_ref[0] = set_var
        set_var.trace_add("write", _update_title)
        set_entry = _bind_mw(tk.Entry(set_cell, textvariable=set_var,
                                      bg=bg, fg="#a0a0b0",
                                      insertbackground="white",
                                      relief="flat", font=("Helvetica", 10)))
        set_entry.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.96)

        # ── Number / Total (e.g. "58/102") ──
        num_cell = _named_cell("num")
        num_str = self._fmt_number(top)
        num_lbl = _bind_mw(tk.Label(num_cell, text=num_str,
                                     bg=bg, fg="#a0a0b0",
                                     font=("Helvetica", 10), anchor="w"))
        num_lbl.place(x=4, y=0, relwidth=1.0, relheight=1.0)

        # ── Rarity ──
        rarity_cell = _named_cell("rarity")
        rarity_lbl = _bind_mw(tk.Label(rarity_cell, text=top.get("rarity", "—"),
                                        bg=bg, fg="#a0a0b0",
                                        font=("Helvetica", 10), anchor="w",
                                        wraplength=cw["rarity"] - 8))
        rarity_lbl.place(x=4, y=0, relwidth=1.0, relheight=1.0)

        # ── Finish dropdown ──
        finish_cell = _named_cell("finish")
        finish_opts = self._finish_options(top)
        finish_var = tk.StringVar(value=finish_opts[0] if finish_opts else "Non-Holo")
        finish_cb = ttk.Combobox(finish_cell, textvariable=finish_var,
                                  values=finish_opts, state="readonly",
                                  font=("Helvetica", 10))
        finish_cb.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.92)
        # Wire finish into title updates
        _finish_ref[0] = finish_var
        finish_var.trace_add("write", _update_title)

        # Auto-fetch price in background now that finish_var exists
        def _auto_fetch_price(candidate,
                               _finish_var=finish_var,
                               _price_var=price_var,
                               _source_var=source_label_var,
                               _edited=_price_user_edited):
            if _edited[0]:
                return
            from prices.fetcher import fetch_price
            card_id = candidate.get("card_id", "")
            price, source = fetch_price(card_id, _finish_var.get())
            if price is not None and not _edited[0]:
                mult  = self._get_price_mult()
                final = price * mult
                self.after(0, lambda p=final: _price_var.set(f"{p:.2f}"))
                self.after(0, lambda: _source_var.set(source))

        if top:
            threading.Thread(
                target=_auto_fetch_price, args=(top,), daemon=True
            ).start()

        # ── Edition dropdown (WotC era only — hidden for modern sets) ──
        edition_cell = _named_cell("edition")
        edition_var = tk.StringVar(value="Unlimited")
        edition_cb = ttk.Combobox(edition_cell, textvariable=edition_var,
                                   values=["Unlimited", "1st Edition"],
                                   state="readonly", font=("Helvetica", 10))
        if self._is_wotc_era(top):
            edition_cb.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.92)
        # Wire edition into title updates now that edition_var exists
        _edition_ref[0] = edition_var
        edition_var.trace_add("write", _update_title)

        # ── Confidence badge ──
        conf_cell = _named_cell("conf")
        conf = top.get("confidence", "low") if top else "low"
        conf_lbl = _bind_mw(tk.Label(conf_cell, text=f"● {conf.upper()}",
                                      bg=bg, fg=CONF_COLORS.get(conf, "#888"),
                                      font=("Helvetica", 10, "bold"), anchor="w"))
        conf_lbl.place(x=4, y=0, relwidth=1.0, relheight=1.0)

        def _tab_to_next_row(widget_key: str, event=None):
            """On Tab, move focus to the same column widget in the next batch row."""
            current_idx = next(
                (i for i, r in enumerate(self._batch_rows) if r.widgets.get("frame") is frame),
                None,
            )
            if current_idx is not None and current_idx + 1 < len(self._batch_rows):
                nxt = self._batch_rows[current_idx + 1].widgets.get(widget_key)
                if nxt:
                    nxt.focus_set()
                    return "break"  # suppress default Tab behaviour

        qty_spin.bind("<Tab>",   lambda e: _tab_to_next_row("qty_spin", e))
        price_entry.bind("<Tab>", lambda e: _tab_to_next_row("price_entry", e))

        # ── Condition combobox ──
        cond_cell = _named_cell("cond")
        cond_var = tk.StringVar(value="Near Mint")
        cond_cb = ttk.Combobox(cond_cell, textvariable=cond_var,
                                values=self._CONDITION_OPTIONS,
                                state="readonly", font=("Helvetica", 10))
        cond_cb.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.92)
        # Rebuild title whenever condition changes
        cond_var.trace_add("write", _update_title)
        # Set initial title now that cond_var exists
        _update_title()

        # ── Game label ──
        game_cell = _named_cell("game")
        _bind_mw(tk.Label(game_cell, text="Pokémon TCG",
                           bg=bg, fg="#a0a0b0",
                           font=("Helvetica", 10), anchor="w")).place(
            x=4, y=0, relwidth=1.0, relheight=1.0)

        # ── Description entry ──
        desc_cell = _named_cell("desc")
        desc_var = tk.StringVar()

        def _render_desc(candidate, cond):
            """Render the settings template for this candidate+condition."""
            try:
                from ebay.exporter import _build_description
                from config import _load_settings, _EBAY_DEFAULTS
                s = _load_settings()
                tmpl = s.get("ebay_description_template") or _EBAY_DEFAULTS.get(
                    "ebay_description_template", "")
                return _build_description(candidate, cond, tmpl)
            except Exception:
                return ""

        # Pre-populate with the rendered template
        _initial_desc = _render_desc(top, "Near Mint") if top else ""
        desc_var.set(_initial_desc)
        # Track whether the user has manually edited this field
        _desc_user_edited = [False]

        def _on_desc_edit(*_):
            _desc_user_edited[0] = True
        desc_var.trace_add("write", _on_desc_edit)

        # After setting the initial value the trace fires — reset the flag
        _desc_user_edited[0] = False

        # ── Popout editor ──────────────────────────────────────────────────
        _desc_popout: list = [None]   # holds the open Toplevel so we don't open two

        def _open_desc_popout(event=None):
            if _desc_popout[0] and _desc_popout[0].winfo_exists():
                _desc_popout[0].lift()
                return

            card_name = (row.candidates[row.current_idx].get("name", "")
                         if row.candidates else "")
            pop = tk.Toplevel(self)
            pop.title(f"Description — {card_name}" if card_name else "Description")
            pop.geometry("700x420")
            pop.configure(bg="#1a1a2e")
            pop.resizable(True, True)
            _desc_popout[0] = pop

            # ── Toolbar ──
            toolbar_f = tk.Frame(pop, bg="#0d0d1a")
            toolbar_f.pack(fill="x", side="top")

            def _insert(tag):
                tags = {
                    "b":  ("<strong>", "</strong>"),
                    "i":  ("<em>",     "</em>"),
                    "u":  ("<u>",      "</u>"),
                    "p":  ("<p>",      "</p>"),
                    "h2": ("<h2>",     "</h2>"),
                    "h3": ("<h3>",     "</h3>"),
                    "ul": ("<ul>\n  <li>", "</li>\n</ul>"),
                    "li": ("<li>",     "</li>"),
                    "br": ("<br>",     ""),
                }
                open_tag, close_tag = tags.get(tag, ("", ""))
                try:
                    sel = txt.get(tk.SEL_FIRST, tk.SEL_LAST)
                    txt.delete(tk.SEL_FIRST, tk.SEL_LAST)
                    txt.insert(tk.INSERT, f"{open_tag}{sel}{close_tag}")
                except tk.TclError:
                    txt.insert(tk.INSERT, f"{open_tag}{close_tag}")

            tbtn_kw = dict(bg="#0f3460", fg="white", relief="flat",
                           font=("Helvetica", 9, "bold"), cursor="hand2",
                           activebackground="#533483", padx=6, pady=3)
            for lbl, tag in [("B", "b"), ("I", "i"), ("U", "u"),
                              ("H2", "h2"), ("H3", "h3"), ("P", "p"),
                              ("UL", "ul"), ("LI", "li"), ("BR", "br")]:
                tk.Button(toolbar_f, text=lbl,
                          command=lambda t=tag: _insert(t),
                          **tbtn_kw).pack(side="left", padx=(2, 0), pady=2)

            # ── Placeholder buttons ──
            tk.Label(toolbar_f, text="|", bg="#0d0d1a", fg="#444").pack(
                side="left", padx=4)
            for ph in ["{name}", "{set}", "{number}", "{rarity}", "{condition}"]:
                tk.Button(toolbar_f, text=ph,
                          command=lambda p=ph: txt.insert(tk.INSERT, p),
                          bg="#1a1a2e", fg="#7777cc", relief="flat",
                          font=("Helvetica", 8), cursor="hand2",
                          activebackground="#2a2a4e", padx=4, pady=3).pack(
                    side="left", padx=(1, 0), pady=2)

            # ── HTML / Preview toggle (right side of toolbar) ──
            _mode = ["html"]  # "html" or "preview"
            toggle_btn = tk.Button(toolbar_f, text="[ ] Preview",
                                   bg="#0d0d1a", fg="#aaaacc", relief="flat",
                                   font=("Helvetica", 9), cursor="hand2",
                                   activebackground="#1a1a2e", padx=8, pady=3)
            toggle_btn.pack(side="right", padx=(0, 4), pady=2)

            # ── Content area (editor + preview stacked, only one visible at a time) ──
            content_frame = tk.Frame(pop, bg="#1a1a2e")
            content_frame.pack(fill="both", expand=True, padx=8, pady=(4, 0))
            content_frame.rowconfigure(0, weight=1)
            content_frame.columnconfigure(0, weight=1)

            # Editor pane
            txt_frame = tk.Frame(content_frame, bg="#1a1a2e")
            txt_frame.grid(row=0, column=0, sticky="nsew")
            txt_vsb = ttk.Scrollbar(txt_frame, orient="vertical")
            txt = tk.Text(txt_frame, bg="#0d0d1a", fg="white",
                          insertbackground="white",
                          font=("Courier New", 10), relief="flat",
                          wrap="word", undo=True,
                          yscrollcommand=txt_vsb.set)
            txt_vsb.config(command=txt.yview)
            txt_vsb.pack(side="right", fill="y")
            txt.pack(fill="both", expand=True)

            # Preview pane (tkinterweb)
            preview_frame = tk.Frame(content_frame, bg="#0d0d1a")
            preview_frame.grid(row=0, column=0, sticky="nsew")
            preview_frame.grid_remove()   # hidden initially
            _html_widget: list = [None]
            try:
                from tkinterweb import HtmlFrame
                hw = HtmlFrame(preview_frame, messages_enabled=False)
                hw.pack(fill="both", expand=True)
                _html_widget[0] = hw
            except ImportError:
                tk.Label(preview_frame,
                         text="Install tkinterweb for live preview\npip install tkinterweb",
                         bg="#0d0d1a", fg="#666699",
                         font=("Helvetica", 10)).pack(expand=True)

            def _switch_mode():
                if _mode[0] == "html":
                    # Switch to preview
                    _mode[0] = "preview"
                    toggle_btn.config(text="< Edit HTML")
                    # Render current text into preview
                    if _html_widget[0]:
                        _html_widget[0].load_html(txt.get("1.0", "end-1c"))
                    txt_frame.grid_remove()
                    preview_frame.grid()
                else:
                    # Switch back to editor
                    _mode[0] = "html"
                    toggle_btn.config(text="[ ] Preview")
                    preview_frame.grid_remove()
                    txt_frame.grid()
                    txt.focus_set()

            toggle_btn.config(command=_switch_mode)

            # Populate with current value
            txt.insert("1.0", desc_var.get())
            txt.focus_set()

            # ── Bottom bar ──
            btn_row = tk.Frame(pop, bg="#1a1a2e")
            btn_row.pack(fill="x", padx=8, pady=6)

            def _reset_to_template():
                candidate = row.candidates[row.current_idx] if row.candidates else {}
                rendered = _render_desc(candidate, cond_var.get())
                txt.delete("1.0", "end")
                txt.insert("1.0", rendered)

            def _save_and_close():
                new_val = txt.get("1.0", "end-1c")
                _desc_user_edited[0] = False
                desc_var.set(new_val)
                # Only mark as user-edited if they changed it from the template
                candidate = row.candidates[row.current_idx] if row.candidates else {}
                auto = _render_desc(candidate, cond_var.get())
                _desc_user_edited[0] = (new_val != auto)
                pop.destroy()

            tk.Button(btn_row, text="Reset to Template",
                      command=_reset_to_template,
                      bg="#333355", fg="#aaaacc", relief="flat",
                      font=("Helvetica", 10), cursor="hand2",
                      activebackground="#444477",
                      padx=10, pady=5).pack(side="left")
            tk.Button(btn_row, text="Save & Close",
                      command=_save_and_close,
                      bg="#145214", fg="white", relief="flat",
                      font=("Helvetica", 10, "bold"), cursor="hand2",
                      activebackground="#1e7a1e",
                      padx=10, pady=5).pack(side="right")
            tk.Button(btn_row, text="Cancel",
                      command=pop.destroy,
                      bg="#0f3460", fg="white", relief="flat",
                      font=("Helvetica", 10), cursor="hand2",
                      activebackground="#533483",
                      padx=10, pady=5).pack(side="right", padx=(0, 6))

            pop.protocol("WM_DELETE_WINDOW", pop.destroy)

        # ── Inline preview label (click to open popout) ────────────────────
        def _short_preview(*_):
            """Strip HTML tags for a plain-text inline preview (3-line max)."""
            import re
            raw = desc_var.get()
            plain = re.sub(r"<[^>]+>", " ", raw)
            plain = " ".join(plain.split())
            # Truncate to ~180 chars so it doesn't overflow 3 wrapped lines
            return plain[:180] + ("…" if len(plain) > 180 else "")

        desc_preview_var = tk.StringVar(value=_short_preview())
        desc_var.trace_add("write", lambda *_: desc_preview_var.set(_short_preview()))

        desc_entry = tk.Label(desc_cell, textvariable=desc_preview_var,
                              bg="#0d0d1a", fg="#aaaacc",
                              font=("Helvetica", 9), anchor="nw",
                              justify="left",
                              wraplength=cw["desc"] - 10,
                              cursor="hand2", relief="flat")
        desc_entry.place(x=4, y=4, relwidth=1.0, relheight=1.0)
        desc_entry.bind("<Button-1>", _open_desc_popout)

        def _update_desc_on_cond(*_):
            """Re-render description when condition changes, unless user edited it."""
            if not _desc_user_edited[0]:
                rendered = _render_desc(
                    row.candidates[row.current_idx] if row.candidates else {},
                    cond_var.get(),
                )
                _desc_user_edited[0] = False
                desc_var.set(rendered)
                _desc_user_edited[0] = False

        cond_var.trace_add("write", _update_desc_on_cond)

        # ── Action buttons (prev / next / search / delete) ──
        action_cell = _named_cell("actions")
        action_frame = _bind_mw(tk.Frame(action_cell, bg=bg))
        action_frame.place(relx=0.5, rely=0.5, anchor="center")

        abtn_kw = dict(bg="#0f3460", fg="white", relief="flat",
                       font=("Helvetica", 10), cursor="hand2",
                       activebackground="#533483", padx=5, pady=3)

        _bind_mw(tk.Button(action_frame, text="<",
                            command=lambda r=row: self._cycle_match(r, -1),
                            **abtn_kw)).pack(side="left", padx=(0, 2))
        _bind_mw(tk.Button(action_frame, text=">",
                            command=lambda r=row: self._cycle_match(r, +1),
                            **abtn_kw)).pack(side="left", padx=(0, 2))
        _bind_mw(tk.Button(action_frame, text="?",
                            command=lambda r=row: self._open_search_dialog(r),
                            **abtn_kw)).pack(side="left", padx=(0, 2))
        _bind_mw(tk.Button(action_frame, text="$",
                            command=lambda r=row: self._refresh_price(r),
                            **abtn_kw)).pack(side="left", padx=(0, 2))
        _bind_mw(tk.Button(action_frame, text="X",
                            command=lambda r=row, f=frame: self._delete_row(r, f),
                            bg="#3a1a1a", fg="#ff6666", relief="flat",
                            font=("Helvetica", 10), cursor="hand2",
                            activebackground="#5a2a2a", padx=5, pady=3)
                 ).pack(side="left")

        frame.bind("<MouseWheel>", _mw)

        # Store widget refs for in-place refresh
        row.widgets = {
            "frame":        frame,
            "thumb_scan":   thumb_scan,
            "thumb_ref":    thumb_ref,
            "title":               title_lbl,
            "build_title":         _build_title,
            "update_title_counter": _update_title_counter,
            "label_var":    label_var,
            "name":         name_lbl,
            "set_var":      set_var,
            "number":     num_lbl,
            "rarity":     rarity_lbl,
            "finish_var":   finish_var,
            "finish_cb":    finish_cb,
            "edition_cell": edition_cell,
            "edition_var":  edition_var,
            "edition_cb":   edition_cb,
            "conf":       conf_lbl,
            "qty_var":    qty_var,
            "qty_spin":   qty_spin,
            "price_var":         price_var,
            "price_entry":       price_entry,
            "source_label_var":  source_label_var,
            "price_user_edited": _price_user_edited,
            "cond_var":   cond_var,
            "desc_var":          desc_var,
            "desc_render":       _render_desc,
            "desc_user_edited":  _desc_user_edited,
            "bg":         bg,
            # Cell frame refs keyed as "_cell_<colkey>" for column-resize updates
            **{f"_cell_{k}": v for k, v in _cells.items()},
        }

    @staticmethod
    def _fmt_number(top: dict) -> str:
        """Format number as '58/102' or just '58' if set_total is unknown."""
        num = top.get("number", "") or ""
        total = top.get("set_total", "") or ""
        if num and total:
            return f"{num}/{total}"
        return num or "—"

    @staticmethod
    def _finish_options(top: dict) -> list[str]:
        """Build finish dropdown options from variants dict."""
        variants = top.get("variants") if top else None
        if variants is None:
            return ["Non-Holo", "Reverse Holo", "Holo",
                    "Poke Ball Holo", "Master Ball Holo"]
        opts = []
        if variants.get("normal"):
            opts.append("Non-Holo")
        if variants.get("reverse"):
            opts.append("Reverse Holo")
        if variants.get("holo"):
            opts.append("Holo")
        # Poke Ball / Master Ball holos are special print variants not tracked
        # separately in TCGdex variants — always offer them as manual overrides.
        opts += ["Poke Ball Holo", "Master Ball Holo"]
        return opts or ["Non-Holo", "Reverse Holo", "Holo",
                        "Poke Ball Holo", "Master Ball Holo"]

    @staticmethod
    def _is_wotc_era(top: dict) -> bool:
        """Return True only when TCGdex reports firstEdition=True for this card.
        That flag is exclusively set on WotC-era sets (Base Set through Neo/Team Rocket/Gym).
        """
        variants = top.get("variants") if top else None
        return bool(variants and variants.get("firstEdition"))

    def _set_thumb(self, label: tk.Label, image_path: str, bg: str):
        """Load a thumbnail into a Label, using the cache to avoid redundant disk reads."""
        key = (image_path, THUMB_W, THUMB_H)
        if key not in self._thumb_cache:
            try:
                img = ImageOps.exif_transpose(Image.open(image_path)).convert("RGB")
                img.thumbnail((THUMB_W, THUMB_H), Image.LANCZOS)
                self._thumb_cache[key] = ImageTk.PhotoImage(img)
            except Exception:
                self._thumb_cache[key] = None
        photo = self._thumb_cache[key]
        if photo:
            label.config(image=photo, text="", bg=bg)
        else:
            label.config(image="", text="?", fg="#666", bg=bg,
                         font=("Helvetica", 8))

    # ── Hover image preview ────────────────────────────────────────────────────

    _PREVIEW_W, _PREVIEW_H = 280, 392   # ~4× thumbnail, card aspect ratio

    def _attach_hover_preview(self, widget: tk.Widget, image_path: str):
        """Bind <Enter>/<Leave> on widget to show a large pop-up preview after 0.6 s.

        Safe to call multiple times on the same widget (e.g. after candidate cycling):
        the path is stored on the widget and the lambda reads it at trigger time, so
        re-calling with a new path simply updates the stored value without re-binding.
        """
        if not image_path:
            return

        # Store current path on the widget so cycles update the preview target
        widget._hover_image_path = image_path  # type: ignore[attr-defined]

        # Only bind once — check a sentinel attribute
        if getattr(widget, "_hover_bound", False):
            return
        widget._hover_bound = True  # type: ignore[attr-defined]

        def _on_enter(event):
            if self._hover_after_id:
                self.after_cancel(self._hover_after_id)
            # Read path at trigger time (not capture time) so candidate cycles work
            self._hover_after_id = self.after(
                600,
                lambda w=widget: self._show_hover_preview(
                    w, getattr(w, "_hover_image_path", "")
                )
            )

        def _on_leave(event):
            if self._hover_after_id:
                self.after_cancel(self._hover_after_id)
                self._hover_after_id = None
            self._hide_hover_preview()

        widget.bind("<Enter>", _on_enter)
        widget.bind("<Leave>", _on_leave)

    def _show_hover_preview(self, widget: tk.Widget, image_path: str):
        """Display a large floating preview window near the widget."""
        self._hover_after_id = None
        self._hide_hover_preview()   # close any existing one

        # Load / cache the preview-sized image
        if image_path not in self._preview_cache:
            try:
                img = ImageOps.exif_transpose(Image.open(image_path)).convert("RGB")
                img.thumbnail((self._PREVIEW_W, self._PREVIEW_H), Image.LANCZOS)
                self._preview_cache[image_path] = ImageTk.PhotoImage(img)
            except Exception:
                self._preview_cache[image_path] = None

        photo = self._preview_cache[image_path]
        if not photo:
            return

        # Position the popup to the right of the widget (or left if near screen edge)
        try:
            wx = widget.winfo_rootx()
            wy = widget.winfo_rooty()
            ww = widget.winfo_width()
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
        except Exception:
            return

        popup_w = photo.width() + 8
        popup_h = photo.height() + 8
        x = wx + ww + 6
        if x + popup_w > sw:
            x = wx - popup_w - 6          # flip to left side
        y = wy
        if y + popup_h > sh:
            y = sh - popup_h - 10

        top = tk.Toplevel(self)
        top.overrideredirect(True)         # no title bar / border
        top.attributes("-topmost", True)
        top.geometry(f"+{x}+{y}")

        # Dark border frame
        border = tk.Frame(top, bg="#0f3460", padx=3, pady=3)
        border.pack()
        lbl = tk.Label(border, image=photo, bg="#0f3460")
        lbl.pack()
        # Keep a reference so GC doesn't collect the photo
        lbl._photo_ref = photo

        self._hover_toplevel = top

    def _hide_hover_preview(self):
        """Destroy the floating preview if it exists."""
        if self._hover_toplevel:
            try:
                self._hover_toplevel.destroy()
            except Exception:
                pass
            self._hover_toplevel = None

    # ------------------------------------------------------------------
    # Price-source tooltip
    # ------------------------------------------------------------------

    def _attach_price_tooltip(self, widget: tk.Widget, source_var: tk.StringVar):
        """Show a small dark tooltip with the price source after 0.6 s of hovering.

        The tooltip reads from source_var at display time so it always reflects
        the current value (updated by the background fetch thread).
        Only binds once — safe to call again if the widget is reused.
        """
        if getattr(widget, "_price_tip_bound", False):
            return
        widget._price_tip_bound = True          # type: ignore[attr-defined]
        widget._price_source_var = source_var   # type: ignore[attr-defined]

        _after_id: list[str | None] = [None]
        _tip_win:  list             = [None]

        def _show():
            _after_id[0] = None
            text = getattr(widget, "_price_source_var", source_var).get()
            if not text or text == "Fetching...":
                return

            # Destroy any existing tip
            if _tip_win[0]:
                try:
                    _tip_win[0].destroy()
                except Exception:
                    pass
                _tip_win[0] = None

            try:
                wx = widget.winfo_rootx()
                wy = widget.winfo_rooty()
                wh = widget.winfo_height()
            except Exception:
                return

            tip = tk.Toplevel(self)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            tip.configure(bg="#1a1a2e")

            lbl = tk.Label(tip, text=text,
                           bg="#1a1a2e", fg="#aaaadd",
                           font=("Helvetica", 8),
                           padx=6, pady=3,
                           relief="solid", bd=1)
            lbl.pack()
            tip.update_idletasks()
            tw = tip.winfo_width()

            # Centre below the widget
            x = wx + widget.winfo_width() // 2 - tw // 2
            y = wy + wh + 4
            tip.geometry(f"+{x}+{y}")
            _tip_win[0] = tip

        def _hide():
            if _after_id[0]:
                try:
                    self.after_cancel(_after_id[0])
                except Exception:
                    pass
                _after_id[0] = None
            if _tip_win[0]:
                try:
                    _tip_win[0].destroy()
                except Exception:
                    pass
                _tip_win[0] = None

        def _on_enter(_event):
            _hide()
            _after_id[0] = self.after(600, _show)

        def _on_leave(_event):
            _hide()

        widget.bind("<Enter>", _on_enter, add="+")
        widget.bind("<Leave>", _on_leave, add="+")

    def _refresh_row(self, row: BatchRow):
        """Update all text and image widgets in a batch row from the current candidate."""
        w = row.widgets
        if not w:
            return

        top = row.candidates[row.current_idx] if row.candidates else {}
        bg  = w["bg"]

        w["name"].config(text=top.get("name", "—"))
        w["set_var"].set(top.get("set_name", "") or "")
        w["number"].config(text=self._fmt_number(top))
        w["rarity"].config(text=top.get("rarity", "—") or "—")
        _t = w["build_title"](top, w["cond_var"].get(),
                              w["edition_var"].get(), w["finish_var"].get(),
                              set_name_override=w["set_var"].get())
        w["title"].config(text=_t)
        w["update_title_counter"](_t)

        conf = top.get("confidence", "low") if top else "low"
        w["conf"].config(text=f"● {conf.upper()}", fg=CONF_COLORS.get(conf, "#888"))

        # Update reference thumbnail
        ref_path = top.get("local_image_path", "") if top else ""
        if ref_path and Path(ref_path).exists():
            self._set_thumb(w["thumb_ref"], ref_path, bg)
            self._attach_hover_preview(w["thumb_ref"], ref_path)
        else:
            w["thumb_ref"].config(image="", text="—", fg="#555", bg=bg,
                                   font=("Helvetica", 10))

        # Update finish dropdown options
        finish_opts = self._finish_options(top)
        w["finish_cb"].config(values=finish_opts)
        if w["finish_var"].get() not in finish_opts:
            w["finish_var"].set(finish_opts[0])

        # Re-render description if the user hasn't manually edited it
        if not w["desc_user_edited"][0]:
            rendered = w["desc_render"](top, w["cond_var"].get())
            w["desc_user_edited"][0] = False   # keep flag clear while we set
            w["desc_var"].set(rendered)
            w["desc_user_edited"][0] = False   # trace fired — reset again

        # Show edition dropdown only for WotC-era cards (firstEdition flag)
        if self._is_wotc_era(top):
            w["edition_cb"].place(relx=0.5, rely=0.5, anchor="center", relwidth=0.92)
        else:
            w["edition_cb"].place_forget()
            w["edition_var"].set("Unlimited")

        # Refresh price for new candidate (unless user has manually edited it)
        if not w.get("price_user_edited", [False])[0]:
            self._refresh_price(row)

    def _cycle_match(self, row: BatchRow, delta: int):
        """Step through candidates (±1) with wraparound and refresh the row."""
        if not row.candidates:
            return
        row.current_idx = (row.current_idx + delta) % len(row.candidates)
        self._refresh_row(row)

    def _delete_row(self, row: BatchRow, frame: tk.Frame):
        """Remove a batch row from the UI and internal list."""
        frame.destroy()
        if row in self._batch_rows:
            self._batch_rows.remove(row)

    def _get_price_mult(self) -> float:
        """Return the current price multiplier, clamped to [0.01, 100]. Defaults to 1.0."""
        try:
            v = float(self._price_mult_var.get())
            return max(0.01, min(v, 100.0))
        except (ValueError, tk.TclError):
            return 1.0

    def _refresh_price(self, row: BatchRow):
        """Fetch the market price for the current candidate and update the price cell."""
        w = row.widgets
        if not w:
            return
        top = row.candidates[row.current_idx] if row.candidates else {}
        card_id = top.get("card_id", "")
        finish  = w["finish_var"].get()

        # Reset user-edited flag so we can update the price
        w["price_user_edited"][0] = False
        w["source_label_var"].set("Fetching...")

        def worker():
            from prices.fetcher import fetch_price
            price, source = fetch_price(card_id, finish)
            if price is not None:
                mult = self._get_price_mult()
                final = price * mult
                self.after(0, lambda p=final: w["price_var"].set(f"{p:.2f}"))
                self.after(0, lambda: w["source_label_var"].set(source))
            else:
                self.after(0, lambda: w["source_label_var"].set("No price found"))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Batch search dialog
    # ------------------------------------------------------------------

    def _open_search_dialog(self, row: BatchRow):
        from db.database import get_all_cards

        dialog = tk.Toplevel(self)
        dialog.title("Find Card")
        dialog.configure(bg="#1a1a2e")
        dialog.geometry("560x420")
        dialog.grab_set()
        dialog.transient(self)

        tk.Label(dialog, text="Search by name (type at least 2 characters):", bg="#1a1a2e", fg="#a0a0b0",
                 font=("Helvetica", 10)).pack(anchor="w", padx=12, pady=(12, 2))

        search_var = tk.StringVar()
        entry = ttk.Entry(dialog, textvariable=search_var, font=("Helvetica", 11))
        entry.pack(fill="x", padx=12, pady=(0, 8))
        entry.focus_set()

        tree_frame = tk.Frame(dialog, bg="#1a1a2e")
        tree_frame.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        cols = ("name", "set", "number", "rarity")
        tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=14)
        tree.column("name",   width=190, anchor="w")
        tree.column("set",    width=160, anchor="w")
        tree.column("number", width=50,  anchor="center")
        tree.column("rarity", width=100, anchor="w")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)

        vsb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)

        # Load all cards once
        all_cards = get_all_cards()

        # Sort state: (column_key, reverse)
        _sort_state: list = ["name", False]

        # Map auto-assigned treeview iid → sqlite3.Row for safe retrieval.
        _card_map: dict[str, object] = {}

        # col_index maps column id → index in the values tuple
        _col_idx = {"name": 0, "set": 1, "number": 2, "rarity": 3}

        def _sort_key(iid):
            val = tree.item(iid)["values"][_col_idx[_sort_state[0]]]
            # Sort card number numerically where possible
            if _sort_state[0] == "number":
                try:
                    return (0, int(str(val).split("/")[0]))
                except (ValueError, IndexError):
                    return (1, str(val).lower())
            return str(val).lower()

        def _apply_sort():
            items = list(tree.get_children())
            items.sort(key=_sort_key, reverse=_sort_state[1])
            for i, iid in enumerate(items):
                tree.move(iid, "", i)
            # Update heading arrows
            for col in cols:
                arrow = ""
                if col == _sort_state[0]:
                    arrow = " ▲" if not _sort_state[1] else " ▼"
                tree.heading(col, text=_col_labels[col] + arrow)

        def _on_heading_click(col):
            if _sort_state[0] == col:
                _sort_state[1] = not _sort_state[1]
            else:
                _sort_state[0] = col
                _sort_state[1] = False
            _apply_sort()

        _col_labels = {"name": "Name", "set": "Set", "number": "#", "rarity": "Rarity"}
        for col in cols:
            tree.heading(col, text=_col_labels[col],
                         command=lambda c=col: _on_heading_click(c))

        def _populate(filter_text: str = ""):
            tree.delete(*tree.get_children())
            _card_map.clear()
            ft = filter_text.lower().strip()
            # Require at least 2 characters to avoid inserting 22k rows at once
            if len(ft) < 2:
                return
            for card in all_cards:
                if ft not in (card["name"] or "").lower():
                    continue
                # Let Tk assign the iid; capture it from the return value
                auto_iid = tree.insert("", "end", values=(
                    card["name"] or "",
                    card["set_name"] or "",
                    card["number"] or "",
                    card["rarity"] or "",
                ))
                _card_map[auto_iid] = card
            _apply_sort()

        def _on_search(*_):
            _populate(search_var.get())

        search_var.trace_add("write", _on_search)

        def _on_select():
            sel = tree.selection()
            if not sel:
                return
            card = _card_map.get(sel[0])
            if not card:
                return

            # Build a synthetic candidate dict from the chosen card
            synthetic = {
                "card_id":          card["id"],
                "name":             card["name"] or "",
                "set_id":           card["set_id"] if "set_id" in card.keys() else "",
                "set_name":         card["set_name"] or "",
                "number":           card["number"] or "",
                "rarity":           card["rarity"] or "",
                "category":         card["category"] or "",
                "hp":               card["hp"] or "",
                "types":            card["types"] if "types" in card.keys() else "",
                "image_url":        card["image_url"] or "",
                "local_image_path": card["local_image_path"] or "",
                "variants":         card["variants"] if "variants" in card.keys() else None,
                "set_total":        card["set_total"] if "set_total" in card.keys() else None,
                "distance":         0.0,
                "confidence":       "high",   # user manually confirmed
            }
            # Decode variants JSON if it came back as a string
            if isinstance(synthetic["variants"], str):
                import json as _json
                try:
                    synthetic["variants"] = _json.loads(synthetic["variants"])
                except Exception:
                    synthetic["variants"] = None

            # Insert at the front of candidates so ◀/▶ still works
            row.candidates.insert(0, synthetic)
            row.current_idx = 0
            self._refresh_row(row)
            dialog.destroy()

        btn_frame = tk.Frame(dialog, bg="#1a1a2e")
        btn_frame.pack(pady=(0, 12))

        tk.Button(btn_frame, text="Select", command=_on_select,
                  bg="#0f3460", fg="white", activebackground="#533483",
                  font=("Helvetica", 11, "bold"), relief="flat",
                  padx=14, pady=6, cursor="hand2").pack(side="left", padx=6)

        tk.Button(btn_frame, text="Cancel", command=dialog.destroy,
                  bg="#2a2a4a", fg="#a0a0b0", relief="flat",
                  padx=10, pady=6, font=("Helvetica", 10),
                  cursor="hand2").pack(side="left", padx=6)

        # Double-click also selects
        tree.bind("<Double-1>", lambda e: _on_select())

    # ------------------------------------------------------------------
    # DB info
    # ------------------------------------------------------------------

    def _update_db_info(self):
        import config
        try:
            cards    = card_count()
            hashed   = hash_count()
            embedded = embedding_count()
            self._db_info_var.set(
                f"DB: {cards:,} cards  |  {hashed:,} hashed  |  "
                f"{embedded:,} embedded  |  {config.DATA_DIR}"
            )
        except Exception:
            import config as _c
            self._db_info_var.set(f"DB: not initialised  |  {_c.DATA_DIR}")

    # ------------------------------------------------------------------
    # Setup / first run
    # ------------------------------------------------------------------

    def _change_data_dir(self):
        import config
        current = str(config.DATA_DIR)
        chosen = filedialog.askdirectory(
            title="Choose data directory (database + images will be stored here)",
            initialdir=current,
        )
        if not chosen:
            return

        from pathlib import Path
        new_dir = Path(chosen)
        config.save_settings(new_dir)
        config.DATA_DIR = new_dir
        config.IMAGES_DIR = new_dir / "images"
        config.DB_PATH = new_dir / "cards.db"
        new_dir.mkdir(parents=True, exist_ok=True)
        (new_dir / "images").mkdir(exist_ok=True)
        self._update_db_info()
        messagebox.showinfo(
            "Data Directory Updated",
            f"Data directory set to:\n{new_dir}\n\n"
            "Your existing database and images are NOT moved automatically.",
        )

    def _relink_images(self):
        """Let the user point to a folder of images and bulk-update local_image_path in the DB."""
        import config
        chosen = filedialog.askdirectory(
            title="Select folder containing card images (e.g. data/images)",
            initialdir=str(config.IMAGES_DIR) if config.IMAGES_DIR.exists() else str(config.DATA_DIR),
        )
        if not chosen:
            return

        folder = Path(chosen)

        # Progress window
        win = tk.Toplevel(self)
        win.title("Relinking Images")
        win.resizable(False, False)
        win.configure(bg="#1a1a2e")
        win.grab_set()

        tk.Label(win, text="Scanning folder and updating image paths...",
                 bg="#1a1a2e", fg="white", font=("Helvetica", 11)).pack(padx=20, pady=(16, 6))

        log_var = tk.StringVar(value="Scanning...")
        tk.Label(win, textvariable=log_var, bg="#1a1a2e", fg="#a0a0b0",
                 font=("Helvetica", 9), wraplength=380).pack(padx=20, pady=4)

        bar = ttk.Progressbar(win, mode="indeterminate", length=380)
        bar.pack(padx=20, pady=8)
        bar.start(12)

        close_btn = tk.Button(win, text="Close", state="disabled", bg="#0f3460", fg="white",
                              relief="flat", padx=12, pady=6, command=win.destroy)
        close_btn.pack(pady=(4, 16))

        rehash_var = tk.BooleanVar(value=True)
        rehash_chk = tk.Checkbutton(win, text="Rehash all cards after relinking",
                                    variable=rehash_var, bg="#1a1a2e", fg="#a0a0b0",
                                    selectcolor="#0f3460", activebackground="#1a1a2e",
                                    state="disabled")
        rehash_chk.pack(pady=(0, 12))

        def worker():
            try:
                from db.database import relink_images_from_folder
                matched, total = relink_images_from_folder(folder)
                self.after(0, lambda: log_var.set(
                    f"Done. {matched:,} cards relinked from {total:,} image files found."
                ))
                self.after(0, self._update_db_info)
                self.after(0, lambda: bar.stop())
                self.after(0, lambda: close_btn.config(state="normal"))
                self.after(0, lambda: rehash_chk.config(state="normal"))

                if matched == 0:
                    self.after(0, lambda: log_var.set(
                        f"No matches found in {total:,} files. "
                        "Make sure filenames match card IDs (e.g. swsh1-1.png)."
                    ))
                    return

                # Auto-trigger rehash if checkbox was checked when worker finishes
                def _maybe_rehash():
                    if rehash_var.get():
                        win.destroy()
                        self._run_rehash_silent()

                self.after(500, _maybe_rehash)

            except Exception as e:
                self.after(0, lambda: log_var.set(f"Error: {e}"))
                self.after(0, lambda: bar.stop())
                self.after(0, lambda: close_btn.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _run_rehash_silent(self):
        """Run a full rehash without the confirmation dialog (called automatically after relink)."""
        win = tk.Toplevel(self)
        win.title("Rehashing Cards")
        win.resizable(False, False)
        win.configure(bg="#1a1a2e")
        win.grab_set()

        tk.Label(win, text="Recomputing perceptual hashes...", bg="#1a1a2e", fg="white",
                 font=("Helvetica", 11)).pack(padx=20, pady=(16, 6))

        log_var = tk.StringVar(value="Clearing old hashes...")
        tk.Label(win, textvariable=log_var, bg="#1a1a2e", fg="#a0a0b0",
                 font=("Helvetica", 9), wraplength=380).pack(padx=20, pady=4)

        bar = ttk.Progressbar(win, mode="indeterminate", length=380)
        bar.pack(padx=20, pady=8)
        bar.start(12)

        close_btn = tk.Button(win, text="Close", state="disabled", bg="#0f3460", fg="white",
                              relief="flat", padx=12, pady=6, command=win.destroy)
        close_btn.pack(pady=(4, 16))

        def progress(msg: str):
            self.after(0, lambda: log_var.set(msg))

        def worker():
            from db.database import clear_all_hashes
            from cards.hasher import compute_all_hashes
            try:
                clear_all_hashes()
                progress("Old hashes cleared. Computing new hashes...")
                compute_all_hashes(progress_callback=progress)
                reload_index()
                self.after(0, self._update_db_info)
                self.after(0, lambda: progress("Rehash complete!"))
            except Exception as e:
                self.after(0, lambda: progress(f"Error: {e}"))
            finally:
                self.after(0, lambda: bar.stop())
                self.after(0, lambda: close_btn.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # eBay export
    # ------------------------------------------------------------------

    def _export_ebay_csv(self):
        """Export the current batch to an eBay bulk-upload CSV file."""
        if not self._batch_rows:
            messagebox.showwarning("No Data", "No batch rows to export. Run a batch scan first.")
            return

        out_path = filedialog.asksaveasfilename(
            title="Save eBay CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"{self._batch_name_var.get().strip() or 'batch'}.csv",
        )
        if not out_path:
            return

        from config import _load_settings
        from ebay.exporter import export_csv
        settings = _load_settings()

        # Check if imgbb auto-upload is enabled so we can show a progress dialog
        api_key    = (settings.get("ebay_imgbb_api_key") or "").strip()
        auto_upload = settings.get("ebay_imgbb_auto_upload") in (True, "true", "True", 1, "1")
        scan_count  = sum(1 for br in self._batch_rows
                          if br.image_path and br.candidates)

        if auto_upload and api_key and scan_count:
            # Show a small progress window during the upload phase
            prog_win = tk.Toplevel(self)
            prog_win.title("Uploading Images…")
            prog_win.resizable(False, False)
            prog_win.grab_set()
            prog_win.configure(bg="#1a1a2e")
            tk.Label(prog_win, text="Uploading scan images to imgbb…",
                     bg="#1a1a2e", fg="white",
                     font=("Helvetica", 11)).pack(padx=20, pady=(16, 6))
            prog_lbl = tk.Label(prog_win, text=f"0 / {scan_count}",
                                bg="#1a1a2e", fg="#888899",
                                font=("Helvetica", 10))
            prog_lbl.pack(padx=20, pady=(0, 16))
            prog_win.update()

            def _on_progress(done: int, total: int):
                prog_lbl.config(text=f"{done} / {total}")
                prog_win.update()

            try:
                n = export_csv(self._batch_rows, out_path, settings,
                               progress_callback=_on_progress)
            except Exception as exc:
                prog_win.destroy()
                messagebox.showerror("Export Failed", str(exc))
                return
            prog_win.destroy()
        else:
            try:
                n = export_csv(self._batch_rows, out_path, settings)
            except Exception as exc:
                messagebox.showerror("Export Failed", str(exc))
                return

        messagebox.showinfo("Export Complete",
                            f"Exported {n} listing{'s' if n != 1 else ''} to:\n{out_path}")

    # ------------------------------------------------------------------
    # Help & About
    # ------------------------------------------------------------------

    _HELP_TEXT = """\
LLC SCANNER  --  User Guide
===================================================================

OVERVIEW
--------
LLC Scanner automatically identifies Pokemon cards from scan images and
generates ready-to-upload eBay CSV files. It matches your scans against a
local database of ~22,000 cards using perceptual hashing and a GPU-accelerated
ML embedding model (DINOv2), so results are fast and work fully offline after
the initial setup.


FIRST-TIME SETUP
----------------
1.  Go to  Setup -> Download / Update Card Database.
    This fetches card metadata and images from TCGdex (may take 30-60 min
    on first run; fully resumable if interrupted).

2.  Go to  Setup -> Rehash All Cards (fixes accuracy).
    Computes perceptual hashes for every downloaded card image.

3.  Optionally run  Setup -> Build Embeddings (ML, GPU)  for the highest
    accuracy. Requires a CUDA-capable GPU. Takes ~10-20 min on an RTX 30-series.

After setup, the app is ready to scan.


SCANNING CARDS  --  BATCH MODE
-------------------------------
1.  Click  Open Files...  or  Open Folder...  to load your scan images.
    Supported formats: JPEG, PNG, TIFF, BMP.

2.  Select a Matcher mode in the toolbar:
      - ML (GPU)  -- most accurate, uses DINOv2 embeddings (recommended)
      - Hash      -- fastest, uses perceptual hashing (no GPU required)

3.  Identification starts automatically once files are loaded.
    A progress bar shows activity while cards are being matched.

4.  Results appear as rows -- each showing:
      - Scan thumbnail  (your image)
      - Ref thumbnail   (matched card from database)
      - Card name, set, number, rarity, confidence level

5.  Cycle through alternative matches using  <  /  >  in the row's
    action buttons if the first match isn't correct.

6.  Click  ?  to open a search dialog and manually assign a card.


FRONT + BACK SCANNING
---------------------
Enable  Front+Back  mode in the toolbar before loading files.
Select scans in interleaved order: front0, back0, front1, back1, ...

Ensure your scanner names files sequentially so they sort in this order
naturally (e.g. scan_001.jpg = front, scan_002.jpg = back, etc.).

Both images are uploaded to imgbb and pipe-joined in the eBay PicURL field
so buyers see the front and back photos on the listing.


EDITING ROWS
------------
Each row has editable fields:

  Custom Label   -- Your internal SKU / reference (auto-generated from
                    batch name + row number, zero-padded to 3 digits)
  Title          -- Auto-built from card name, set, number, rarity, finish,
                    and condition. Updates live as you change other fields.
  Finish         -- Non-Holo / Reverse Holo / Holo / Poke Ball Holo /
                    Master Ball Holo
  Edition        -- Unlimited / 1st Edition (shown for WotC-era cards only)
  Qty            -- Quantity to list (Tab key moves to the next row)
  Price          -- Listing price in CAD (Tab key moves to the next row)
  Condition      -- Near Mint / Lightly Played / Moderately Played /
                    Heavily Played / Damaged
  Description    -- Click the description cell to open the full HTML editor.
                    Toggle between raw HTML and a live preview with the
                    [ ] Preview / < Edit HTML button.

Hover over any scan or reference thumbnail to display a pop-up preview.


DESCRIPTION TEMPLATE
--------------------
A default HTML description is pre-filled for every row using the template
in  Export -> eBay Export Settings.  Available placeholders:

    {name}       Card name
    {set}        Set name
    {number}     Card number / total (e.g. 58/102)
    {rarity}     Rarity string
    {condition}  Selected condition

Once you manually edit a description in a row the auto-fill is disabled for
that row so your edits are never overwritten.  Press  Reset  in the editor
to restore the rendered template.


EXPORTING TO EBAY
-----------------
1.  Fill in  Export -> eBay Export Settings  once:
      - Shipping / Return / Payment profile names (from your eBay account)
      - Location (city, province -- e.g. "Toronto, ON")
      - Dispatch days, Best Offer toggle

2.  To include scan photos in the listing you need a free imgbb account:
      a.  Sign up at  https://imgbb.com/  and go to  About -> API  to get
          your API key.
      b.  Paste the key into  eBay Export Settings -> imgbb API Key  and
          enable  Auto-upload scans to imgbb.
    Images are hosted for 24 hours -- long enough for eBay to transload them.

3.  Click  Export CSV...  in the toolbar (or  Export -> Export Batch to eBay CSV...).
    If imgbb is enabled, scans are uploaded automatically before the CSV
    is written.  A progress dialog tracks upload progress.

4.  Import the CSV in your eBay Seller Hub under
    "Listings -> Bulk listing tool -> Upload a file".


COLUMN WIDTHS
-------------
Drag the column header dividers to resize columns. Widths are saved to
settings.json automatically and restored on next launch.


KEYBOARD SHORTCUTS
------------------
  Tab (in Qty / Price)   -- Move focus to the same field in the next row
  Scroll wheel           -- Scroll the batch grid vertically


DATA STORAGE
------------
All card data and images are stored locally in the configured data folder
(default: data/ next to the application).

  cards.db       SQLite database -- cards, hashes, embeddings
  images/        Downloaded card reference images
  settings.json  User preferences and eBay export settings

To change where data is stored, go to  Setup -> Change Data Directory.
The database and images are NOT moved automatically -- copy them manually
then use  Setup -> Relink Images from Folder  to update the paths.


SETUP MENU REFERENCE
--------------------
  Download / Update Card Database
      Fetches metadata and images from TCGdex. Skips cards that are already
      downloaded. Safe to re-run to pick up new sets.

  Refresh Card Metadata
      Re-fetches set names, rarities, and variant flags for all cards without
      re-downloading images.

  Rehash All Cards (fixes accuracy)
      Clears all stored perceptual hashes and recomputes them from the local
      image files. Required after moving images or changing PHASH_SIZE.

  Build / Rebuild Embeddings (ML, GPU)
      Computes DINOv2 embeddings for ML matching. Requires a CUDA GPU.
      "Build" adds missing embeddings; "Rebuild" recomputes all from scratch.

  Change Data Directory...
      Moves the active database and images path to a new folder. Does NOT
      physically move any files -- copy them manually first.

  Relink Images from Folder...
      Scans a folder for image files and updates local_image_path in the
      database for every card whose ID matches a filename (e.g. swsh1-1.png).
      Use this after copying images to a new location. Optionally triggers a
      full rehash automatically once relinking is complete.


EXCLUDING CARD SETS
-------------------
Pokemon TCG Pocket sets (mobile game) are excluded from matching by default
since they are not physical cards. This is controlled by
EXCLUDED_SET_ID_PREFIXES in config.py (currently: A, B, P-A).


TROUBLESHOOTING
---------------
  Poor match accuracy   -> Run  Setup -> Rehash All Cards, or build ML
                           embeddings for the best results.

  Wrong card matched    -> Use  <  /  >  to cycle candidates, or  ?  to
                           search manually.

  imgbb URL missing     -> Ensure your API key is saved in eBay Export
                           Settings and Auto-upload is enabled.

  Slow first identify   -> The ML index loads on first use (~5-10 s).
                           Subsequent identifies are instant.

  Card images missing   -> If images were deleted or moved:
                           1. Copy images to the correct folder.
                           2. Run  Setup -> Relink Images from Folder
                              and select that folder (rehash runs after).
                           Or re-run  Setup -> Download / Update Card Database
                           to re-download any missing images from TCGdex.

  0 hashed after rehash -> Images are missing from the images folder.
                           Use  Setup -> Relink Images from Folder  if you
                           have the files elsewhere, or re-download them.


===================================================================
LLC Scanner  v1-beta2  -  (c) 2026 Kyle Fernandez  -  LowLatencyCards
===================================================================
"""

    def _open_about(self):
        """Show the About dialog with version info and credits."""
        win = tk.Toplevel(self)
        win.title("About LLC Scanner")
        win.configure(bg="#1a1a2e")
        win.resizable(False, False)
        win.grab_set()

        BG   = "#1a1a2e"
        BG2  = "#0d0d1a"
        FG   = "#d0d0e8"
        DIM  = "#7777aa"
        LINK = "#5599ff"

        # ── Logo + title ──
        top_f = tk.Frame(win, bg=BG2, padx=24, pady=18)
        top_f.pack(fill="x")
        try:
            from PIL import Image, ImageTk
            logo_path = Path(__file__).parent / "assets" / "logo_white.png"
            img = Image.open(logo_path).convert("RGBA")
            img.thumbnail((48, 48), Image.LANCZOS)
            _logo_photo = ImageTk.PhotoImage(img)
            logo_lbl = tk.Label(top_f, image=_logo_photo, bg=BG2)
            logo_lbl.image = _logo_photo
            logo_lbl.pack(side="left", padx=(0, 14))
        except Exception:
            pass

        title_f = tk.Frame(top_f, bg=BG2)
        title_f.pack(side="left")
        tk.Label(title_f, text="LLC Scanner", bg=BG2, fg=FG,
                 font=("Helvetica", 16, "bold")).pack(anchor="w")
        tk.Label(title_f, text="v1-beta2  \u2022  \u00a9 2026 Kyle Fernandez",
                 bg=BG2, fg=DIM, font=("Helvetica", 9)).pack(anchor="w")
        tk.Label(title_f, text="LowLatencyCards",
                 bg=BG2, fg=DIM, font=("Helvetica", 9, "italic")).pack(anchor="w")

        # ── Credits table ──
        credits_f = tk.Frame(win, bg=BG, padx=24, pady=16)
        credits_f.pack(fill="x")

        tk.Label(credits_f, text="Credits & Open-Source Resources",
                 bg=BG, fg=FG, font=("Helvetica", 10, "bold")).pack(anchor="w", pady=(0, 10))

        _CREDITS = [
            ("TCGdex",         "Card data & images (tcgdex.net)"),
            ("TCGPlayer",      "Market pricing data via TCGdex"),
            ("CardMarket",     "EU pricing fallback via TCGdex"),
            ("Frankfurter",    "Forex rates — api.frankfurter.app"),
            ("Pillow",         "Image loading & processing"),
            ("imagehash",      "Perceptual hashing (phash/ahash/dhash/whash)"),
            ("OpenCV",         "Card edge detection & preprocessing"),
            ("NumPy",          "Vectorised hash matching"),
            ("requests",       "HTTP client for API calls"),
            ("tkinterweb",     "HTML preview in description editor"),
            ("Inno Setup",     "Windows installer builder"),
            ("PyInstaller",    "Launcher executable packaging"),
            ("Claude AI",      "Vibe-coded with Anthropic Claude"),
        ]

        for name, desc in _CREDITS:
            row = tk.Frame(credits_f, bg=BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=name, bg=BG, fg=LINK,
                     font=("Helvetica", 9, "bold"),
                     width=14, anchor="w").pack(side="left")
            tk.Label(row, text=desc, bg=BG, fg=DIM,
                     font=("Helvetica", 9), anchor="w").pack(side="left")

        # ── Close button ──
        btn_f = tk.Frame(win, bg=BG, pady=10)
        btn_f.pack(fill="x")
        tk.Button(btn_f, text="Close", command=win.destroy,
                  bg="#0f3460", fg="white", relief="flat",
                  font=("Helvetica", 10), cursor="hand2",
                  activebackground="#533483",
                  padx=20, pady=5).pack()

        win.update_idletasks()
        # Centre over the main window
        mw = self.winfo_width();  mh = self.winfo_height()
        mx = self.winfo_rootx(); my = self.winfo_rooty()
        ww = win.winfo_width();  wh = win.winfo_height()
        win.geometry(f"+{mx + (mw - ww)//2}+{my + (mh - wh)//2}")

    def _open_help(self):
        """Open the Help documentation window."""
        win = tk.Toplevel(self)
        win.title("LLC Scanner — Help")
        win.configure(bg="#1a1a2e")
        win.geometry("780x680")
        win.resizable(True, True)

        # Header
        tk.Label(win, text="LLC Scanner  —  User Guide",
                 bg="#0f3460", fg="white",
                 font=("Helvetica", 13, "bold"),
                 anchor="w", padx=16, pady=10).pack(fill="x")

        # Scrollable text body
        frame = tk.Frame(win, bg="#1a1a2e")
        frame.pack(fill="both", expand=True, padx=0, pady=0)

        vsb = ttk.Scrollbar(frame, orient="vertical")
        vsb.pack(side="right", fill="y")

        txt = tk.Text(frame, wrap="word", bg="#0d1b2a", fg="#dce1e7",
                      font=("Courier New", 10), relief="flat",
                      padx=20, pady=16, cursor="arrow",
                      yscrollcommand=vsb.set,
                      state="normal")
        txt.pack(fill="both", expand=True)
        vsb.config(command=txt.yview)

        # Insert content and lock editing
        txt.insert("1.0", self._HELP_TEXT)
        txt.config(state="disabled")

        # Close button
        tk.Button(win, text="Close", command=win.destroy,
                  bg="#0f3460", fg="white", relief="flat",
                  font=("Helvetica", 10), padx=20, pady=6,
                  cursor="hand2", activebackground="#533483"
                  ).pack(pady=(0, 12))

    def _open_donate(self):
        """Placeholder — will open the PayPal donation link."""
        # TODO: replace with actual PayPal.me URL when ready
        # import webbrowser; webbrowser.open("https://paypal.me/LowLatencyCards")
        messagebox.showinfo(
            "Support Development",
            "Thank you for considering a donation!\n\n"
            "Donation link coming soon.\n"
            "— LowLatencyCards",
        )

    def _open_ebay_settings(self):
        """Open the eBay Export Settings dialog."""
        import config as _cfg
        from config import _load_settings, save_settings, _EBAY_DEFAULTS

        current = _load_settings()

        def _get(key):
            return current.get(key, _EBAY_DEFAULTS.get(key, ""))

        dialog = tk.Toplevel(self)
        dialog.title("eBay Export Settings")
        dialog.configure(bg="#1a1a2e")
        dialog.geometry("900x820")
        dialog.grab_set()
        dialog.transient(self)

        # Scrollable content
        canvas = tk.Canvas(dialog, bg="#1a1a2e", highlightthickness=0)
        vsb = ttk.Scrollbar(dialog, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg="#1a1a2e")
        canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(
            canvas_window, width=e.width))
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(
            int(-1 * (e.delta / 120)), "units"))

        lbl_kw  = dict(bg="#1a1a2e", fg="#a0a0b0", font=("Helvetica", 10), anchor="w")
        entry_kw = dict(bg="#0d0d1a", fg="white", insertbackground="white",
                        relief="flat", font=("Helvetica", 10))

        def _row(parent, label, row_idx):
            tk.Label(parent, text=label, **lbl_kw).grid(
                row=row_idx, column=0, sticky="w", padx=(12, 6), pady=(6, 0))
            var = tk.StringVar(value=str(_get(label.lower().replace(" ", "_").replace("/", "_"))))
            return var

        def _field(parent, key, label, row_idx, width=52):
            tk.Label(parent, text=label, **lbl_kw).grid(
                row=row_idx, column=0, sticky="w", padx=(12, 6), pady=(6, 0))
            var = tk.StringVar(value=str(_get(key)))
            tk.Entry(parent, textvariable=var, width=width, **entry_kw).grid(
                row=row_idx, column=1, sticky="ew", padx=(0, 12), pady=(6, 0))
            return var

        inner.columnconfigure(1, weight=1)

        section_kw = dict(bg="#1a1a2e", fg="#7777cc",
                          font=("Helvetica", 10, "bold"), anchor="w")

        r = 0
        tk.Label(inner, text="Listing Defaults", **section_kw).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 2)); r += 1

        v_location   = _field(inner, "ebay_location",        "Seller Location",      r); r += 1
        v_category   = _field(inner, "ebay_category_id",     "eBay Category ID",     r, 20); r += 1
        v_store_cat  = _field(inner, "ebay_store_category",  "Store Category ID",    r, 20); r += 1
        v_dispatch   = _field(inner, "ebay_dispatch_days",   "Handling Time (days)", r, 10); r += 1
        v_site_params = _field(inner, "ebay_site_params",    "Site Parameters",      r); r += 1

        tk.Label(inner, text="Best Offer", **section_kw).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 2)); r += 1
        v_best_offer = tk.BooleanVar(value=bool(int(_get("ebay_best_offer_enabled") or 1)))
        tk.Checkbutton(inner, text="Enable Best Offer on all listings",
                       variable=v_best_offer, bg="#1a1a2e", fg="white",
                       selectcolor="#0d0d1a", activebackground="#1a1a2e",
                       font=("Helvetica", 10)).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12); r += 1

        tk.Label(inner, text="eBay Business Policies", **section_kw).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 2)); r += 1
        v_shipping_profile = _field(inner, "ebay_shipping_profile", "Shipping Profile Name", r); r += 1
        v_return_profile   = _field(inner, "ebay_return_profile",   "Return Profile Name",   r); r += 1
        v_payment_profile  = _field(inner, "ebay_payment_profile",  "Payment Profile Name",  r); r += 1

        # Hidden vars kept so save() doesn't KeyError on old settings keys
        v_pic_base = tk.StringVar(value=_get("ebay_pic_url_base") or "")

        # ── imgbb auto-upload ──────────────────────────────────────────────
        tk.Label(inner, text="imgbb Auto-Upload", **section_kw).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 2)); r += 1
        tk.Label(inner,
                 text="Scans are uploaded to imgbb on CSV export and the URL is used as PicURL.\n"
                      "Images expire after 24 hours — long enough for eBay to transload them.\n"
                      "Get a free API key at: https://api.imgbb.com/",
                 bg="#1a1a2e", fg="#666699", font=("Helvetica", 9),
                 justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12); r += 1
        v_imgbb_key = _field(inner, "ebay_imgbb_api_key", "imgbb API Key", r); r += 1
        v_imgbb_auto = tk.BooleanVar(
            value=bool(_get("ebay_imgbb_auto_upload") in (True, "true", "True", 1, "1")))
        tk.Checkbutton(inner,
                       text="Auto-upload scans to imgbb when exporting CSV",
                       variable=v_imgbb_auto,
                       bg="#1a1a2e", fg="white", selectcolor="#0d0d1a",
                       activebackground="#1a1a2e", font=("Helvetica", 10)).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12); r += 1
        v_tcgdex_fallback = tk.BooleanVar(
            value=bool(_get("ebay_tcgdex_pic_fallback") in (True, "true", "True", 1, "1")))
        tk.Checkbutton(inner,
                       text="Fall back to TCGdex reference image for rows with no scan"
                            " (note: eBay may not reliably fetch TCGdex URLs)",
                       variable=v_tcgdex_fallback,
                       bg="#1a1a2e", fg="#888899", selectcolor="#0d0d1a",
                       activebackground="#1a1a2e", font=("Helvetica", 10),
                       wraplength=500, justify="left").grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12); r += 1

        tk.Label(inner, text="Listing Description Template", **section_kw).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 2)); r += 1
        tk.Label(inner,
                 text="Placeholders: {name}  {set}  {number}  {rarity}  {condition}"
                      "       Emoji: Win+. to open OS picker  (preview shows colour)",
                 bg="#1a1a2e", fg="#666699", font=("Helvetica", 9)).grid(
            row=r, column=0, columnspan=2, sticky="w", padx=12); r += 1

        # ── HTML editor: formatting toolbar + split editor/preview ──
        editor_outer = tk.Frame(inner, bg="#1a1a2e")
        editor_outer.grid(row=r, column=0, columnspan=2, sticky="ew",
                          padx=12, pady=(4, 8)); r += 1
        editor_outer.columnconfigure(0, weight=1)

        # Formatting toolbar
        fmt_bar = tk.Frame(editor_outer, bg="#0d0d1a")
        fmt_bar.grid(row=0, column=0, sticky="ew", pady=(0, 2))

        fmt_btn_kw = dict(bg="#1a1a3a", fg="white", relief="flat",
                          activebackground="#2a2a5a", cursor="hand2",
                          font=("Helvetica", 10), padx=6, pady=2)

        def _wrap_selection(before: str, after: str):
            try:
                sel = desc_text.get(tk.SEL_FIRST, tk.SEL_LAST)
                desc_text.delete(tk.SEL_FIRST, tk.SEL_LAST)
                desc_text.insert(tk.INSERT, f"{before}{sel}{after}")
            except tk.TclError:
                desc_text.insert(tk.INSERT, f"{before}{after}")
                desc_text.mark_set(tk.INSERT,
                    f"{desc_text.index(tk.INSERT)}-{len(after)}c")

        def _insert_at_line_start(tag_open: str, tag_close: str):
            idx = desc_text.index(tk.INSERT + " linestart")
            line_text = desc_text.get(idx, idx + " lineend")
            desc_text.delete(idx, idx + " lineend")
            desc_text.insert(idx, f"{tag_open}{line_text}{tag_close}")

        def _insert_snippet(text: str):
            desc_text.insert(tk.INSERT, text)
            desc_text.focus_set()

        toolbar_buttons = [
            ("B",   lambda: _wrap_selection("<strong>", "</strong>")),
            ("I",   lambda: _wrap_selection("<em>", "</em>")),
            ("U",   lambda: _wrap_selection("<u>", "</u>")),
            ("H2",  lambda: _insert_at_line_start("<h2>", "</h2>")),
            ("H3",  lambda: _insert_at_line_start("<h3>", "</h3>")),
            ("P",   lambda: _wrap_selection("<p>", "</p>")),
            ("UL",  lambda: _insert_snippet("<ul>\n  <li></li>\n</ul>\n")),
            ("LI",  lambda: _insert_snippet("<li></li>")),
            ("BR",  lambda: _insert_snippet("<br>")),
        ]
        # Placeholder insert buttons
        placeholders = ["{name}", "{set}", "{number}", "{rarity}", "{condition}"]

        for label_text, cmd in toolbar_buttons:
            tk.Button(fmt_bar, text=label_text, command=cmd, **fmt_btn_kw).pack(
                side="left", padx=(0, 2))

        tk.Label(fmt_bar, text="|", bg="#0d0d1a", fg="#444466").pack(side="left", padx=4)

        for ph in placeholders:
            tk.Button(fmt_bar, text=ph,
                      command=lambda p=ph: _insert_snippet(p),
                      **{**fmt_btn_kw, "fg": "#88aaff"}).pack(side="left", padx=(0, 2))

        # Split pane: editor left, preview right
        pane = tk.PanedWindow(editor_outer, orient="horizontal",
                              bg="#0d0d1a", sashwidth=5, sashrelief="flat")
        pane.grid(row=1, column=0, sticky="nsew")
        editor_outer.rowconfigure(1, weight=1)

        # Left: raw HTML editor
        editor_frame = tk.Frame(pane, bg="#0d0d1a")
        # Note: Python's bundled Tk on Windows uses GDI rendering which shows
        # emoji as monochrome glyphs in the editor. They will appear in full
        # colour in the preview pane (rendered by tkinterweb) and export
        # correctly to the CSV. Use Win+. to insert emoji via the OS picker.
        import tkinter.font as tkfont
        _editor_font = tkfont.Font(family="Segoe UI Emoji", size=11)
        desc_text = tk.Text(editor_frame, height=18,
                            bg="#0d0d1a", fg="white", insertbackground="white",
                            relief="flat", font=_editor_font, wrap="word",
                            undo=True)
        desc_scroll = ttk.Scrollbar(editor_frame, command=desc_text.yview)
        desc_text.configure(yscrollcommand=desc_scroll.set)
        desc_text.pack(side="left", fill="both", expand=True)
        desc_scroll.pack(side="right", fill="y")
        desc_text.insert("1.0", _get("ebay_description_template"))
        pane.add(editor_frame, minsize=200, stretch="always")

        # Right: live HTML preview using tkinterweb
        preview_frame = tk.Frame(pane, bg="#ffffff")
        try:
            from tkinterweb import HtmlFrame
            html_preview = HtmlFrame(preview_frame, messages_enabled=False)
            html_preview.pack(fill="both", expand=True)

            def _update_preview(*_):
                html = desc_text.get("1.0", "end-1c")
                # Simple token substitution — avoids str.format() choking on
                # any { } braces used in CSS or other HTML contexts.
                sample = {"name": "Charizard", "set": "Base Set",
                          "number": "4/102", "rarity": "Rare Holo",
                          "condition": "Near Mint"}
                preview_html = html
                for key, val in sample.items():
                    preview_html = preview_html.replace(f"{{{key}}}", val)
                html_preview.load_html(
                    f"<html><body style='font-family:sans-serif;padding:8px'>"
                    f"{preview_html}</body></html>"
                )

            # Update preview on every keystroke (debounced via after())
            _preview_job = [None]
            def _on_desc_change(*_):
                if _preview_job[0]:
                    desc_text.after_cancel(_preview_job[0])
                _preview_job[0] = desc_text.after(400, _update_preview)

            desc_text.bind("<<Modified>>", _on_desc_change)
            desc_text.bind("<KeyRelease>", _on_desc_change)
            _update_preview()   # initial render

            tk.Label(preview_frame, text="Preview (sample values)",
                     bg="#dddddd", fg="#333333",
                     font=("Helvetica", 8)).pack(side="bottom", fill="x")
        except ImportError:
            tk.Label(preview_frame, text="Install tkinterweb for live preview",
                     bg="#ffffff", fg="#666666",
                     font=("Helvetica", 9)).pack(expand=True)

        pane.add(preview_frame, minsize=180, stretch="always")

        # Buttons
        btn_frame = tk.Frame(inner, bg="#1a1a2e")
        btn_frame.grid(row=r, column=0, columnspan=2, pady=(4, 16))

        def _save():
            save_settings(extra={
                "ebay_location":             v_location.get().strip(),
                "ebay_category_id":          v_category.get().strip(),
                "ebay_store_category":       v_store_cat.get().strip(),
                "ebay_dispatch_days":        v_dispatch.get().strip(),
                "ebay_site_params":          v_site_params.get().strip(),
                "ebay_best_offer_enabled":   "1" if v_best_offer.get() else "0",
                "ebay_shipping_profile":     v_shipping_profile.get().strip(),
                "ebay_return_profile":       v_return_profile.get().strip(),
                "ebay_payment_profile":      v_payment_profile.get().strip(),
                "ebay_pic_url_base":         v_pic_base.get().strip(),
                "ebay_tcgdex_pic_fallback":  v_tcgdex_fallback.get(),
                "ebay_imgbb_api_key":        v_imgbb_key.get().strip(),
                "ebay_imgbb_auto_upload":    v_imgbb_auto.get(),
                "ebay_description_template": desc_text.get("1.0", "end-1c"),
            })
            messagebox.showinfo("Saved", "eBay export settings saved.", parent=dialog)
            dialog.destroy()

        tk.Button(btn_frame, text="Save", command=_save,
                  bg="#0f3460", fg="white", activebackground="#533483",
                  font=("Helvetica", 11, "bold"), relief="flat",
                  padx=14, pady=6, cursor="hand2").pack(side="left", padx=6)
        tk.Button(btn_frame, text="Cancel", command=dialog.destroy,
                  bg="#2a2a4a", fg="#a0a0b0", relief="flat",
                  padx=10, pady=6, font=("Helvetica", 10),
                  cursor="hand2").pack(side="left", padx=6)

    def _check_first_run(self):
        try:
            from db.database import init_db, get_connection
            init_db()
            count = card_count()
        except Exception:
            count = 0

        if count == 0:
            self._prompt_first_run()
            return

        # Detect the "cards exist but set_name is universally NULL" state that
        # happens when the DB was built with the old upsert that skipped metadata.
        try:
            from db.database import get_connection
            with get_connection() as conn:
                missing = conn.execute(
                    "SELECT COUNT(*) FROM cards WHERE set_name IS NULL"
                ).fetchone()[0]
            # Only prompt when a significant fraction (>5%) is missing —
            # a handful of promo/special cards legitimately have no set name
            # even after a complete backfill, so don't nag on every launch.
            if missing > 0 and missing > count * 0.05:
                self._prompt_refresh_metadata(missing, count)
        except Exception:
            pass

    def _prompt_refresh_metadata(self, missing: int, total: int):
        """Offer to re-fetch metadata when set_name/rarity are blank in the DB."""
        if not messagebox.askyesno(
            "Metadata Missing",
            f"{missing:,} of {total:,} cards are missing set name and rarity data.\n\n"
            "This usually means the database was built with an older version of the app.\n\n"
            "Run 'Refresh Card Metadata' now to fix this? "
            "(downloads metadata only, no images — takes ~1–2 minutes)",
            icon="warning",
        ):
            return
        self._run_refresh_metadata()

    def _prompt_first_run(self):
        import config

        dialog = tk.Toplevel(self)
        dialog.title("Welcome — Database Setup")
        dialog.configure(bg="#1a1a2e")
        dialog.resizable(False, False)
        dialog.grab_set()
        dialog.transient(self)

        tk.Label(
            dialog,
            text="The card database is empty.",
            bg="#1a1a2e", fg="white",
            font=("Helvetica", 12, "bold"),
        ).pack(padx=24, pady=(20, 4))

        tk.Label(
            dialog,
            text="Choose where to store the database and card images,\n"
                 "then download the data to get started.",
            bg="#1a1a2e", fg="#a0a0b0",
            font=("Helvetica", 10),
            justify="center",
        ).pack(padx=24, pady=(0, 12))

        dir_frame = tk.Frame(dialog, bg="#1a1a2e")
        dir_frame.pack(fill="x", padx=24, pady=4)

        tk.Label(dir_frame, text="Data directory:", bg="#1a1a2e", fg="#a0a0b0",
                 font=("Helvetica", 10)).pack(side="left")

        dir_var = tk.StringVar(value=str(config.DATA_DIR))
        dir_entry = tk.Entry(dir_frame, textvariable=dir_var, width=40,
                             bg="#0d0d1a", fg="white", insertbackground="white",
                             relief="flat", font=("Helvetica", 9))
        dir_entry.pack(side="left", padx=(8, 4))

        def browse():
            chosen = filedialog.askdirectory(
                title="Choose data directory",
                initialdir=dir_var.get(),
            )
            if chosen:
                dir_var.set(chosen)

        tk.Button(
            dir_frame, text="Browse…", command=browse,
            bg="#0f3460", fg="white", relief="flat", padx=8, pady=2,
            font=("Helvetica", 9), cursor="hand2",
        ).pack(side="left")

        btn_frame = tk.Frame(dialog, bg="#1a1a2e")
        btn_frame.pack(pady=(16, 20))

        def on_download():
            chosen = Path(dir_var.get())
            config.save_settings(chosen)
            config.DATA_DIR = chosen
            config.IMAGES_DIR = chosen / "images"
            config.DB_PATH = chosen / "cards.db"
            chosen.mkdir(parents=True, exist_ok=True)
            (chosen / "images").mkdir(exist_ok=True)
            dialog.destroy()
            self._run_setup()

        def on_skip():
            dialog.destroy()

        tk.Button(
            btn_frame, text="Download Now", command=on_download,
            bg="#0f3460", fg="white", activebackground="#533483",
            font=("Helvetica", 11, "bold"), relief="flat", padx=16, pady=8,
            cursor="hand2",
        ).pack(side="left", padx=8)

        tk.Button(
            btn_frame, text="Skip for Now", command=on_skip,
            bg="#2a2a4a", fg="#a0a0b0", relief="flat", padx=12, pady=8,
            font=("Helvetica", 10), cursor="hand2",
        ).pack(side="left", padx=8)

    def _run_setup(self):
        win = tk.Toplevel(self)
        win.title("Database Setup")
        win.resizable(False, False)
        win.configure(bg="#1a1a2e")
        win.grab_set()

        tk.Label(win, text="Downloading card data from TCGdex...", bg="#1a1a2e", fg="white",
                 font=("Helvetica", 11)).pack(padx=20, pady=(16, 6))

        log_var = tk.StringVar(value="Starting...")
        tk.Label(win, textvariable=log_var, bg="#1a1a2e", fg="#a0a0b0",
                 font=("Helvetica", 9), wraplength=380).pack(padx=20, pady=4)

        bar = ttk.Progressbar(win, mode="indeterminate", length=380)
        bar.pack(padx=20, pady=8)
        bar.start(12)

        close_btn = tk.Button(win, text="Close", state="disabled", bg="#0f3460", fg="white",
                              relief="flat", padx=12, pady=6,
                              command=win.destroy)
        close_btn.pack(pady=(4, 16))

        def progress(msg: str):
            self.after(0, lambda: log_var.set(msg))

        def worker():
            from cards.downloader import download_all
            from cards.hasher import compute_all_hashes
            from cards.embedding_computer import compute_all_embeddings

            try:
                download_all(progress_callback=progress)
                progress("Computing perceptual hashes...")
                compute_all_hashes(progress_callback=progress)
                reload_index()
                progress("Computing ML embeddings (GPU)...")
                compute_all_embeddings(progress_callback=progress)
                reload_embedding_index()
                self.after(0, self._update_db_info)
                self.after(0, lambda: progress("Setup complete!"))
            except Exception as e:
                self.after(0, lambda: progress(f"Error: {e}"))
            finally:
                self.after(0, lambda: bar.stop())
                self.after(0, lambda: close_btn.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _run_refresh_metadata(self):
        """Re-fetch card metadata from TCGdex (no images). Fixes blank set names / rarities."""
        win = tk.Toplevel(self)
        win.title("Refreshing Card Metadata")
        win.resizable(False, False)
        win.configure(bg="#1a1a2e")
        win.grab_set()

        tk.Label(win, text="Downloading card metadata from TCGdex…",
                 bg="#1a1a2e", fg="white",
                 font=("Helvetica", 11)).pack(padx=20, pady=(16, 6))

        log_var = tk.StringVar(value="Starting…")
        tk.Label(win, textvariable=log_var, bg="#1a1a2e", fg="#a0a0b0",
                 font=("Helvetica", 9), wraplength=400).pack(padx=20, pady=4)

        bar = ttk.Progressbar(win, mode="indeterminate", length=400)
        bar.pack(padx=20, pady=8)
        bar.start(12)

        close_btn = tk.Button(win, text="Close", state="disabled",
                              bg="#0f3460", fg="white", relief="flat",
                              padx=12, pady=6, command=win.destroy)
        close_btn.pack(pady=(4, 16))

        def progress(msg: str):
            self.after(0, lambda: log_var.set(msg))

        def worker():
            from cards.downloader import backfill_metadata
            try:
                count = backfill_metadata(progress_callback=progress)
                self.after(0, self._update_db_info)
                self.after(0, lambda: progress(
                    f"Done — {count:,} cards updated with set names and rarities."
                ))
            except Exception as e:
                self.after(0, lambda: progress(f"Error: {e}"))
            finally:
                self.after(0, lambda: bar.stop())
                self.after(0, lambda: close_btn.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _run_rehash(self):
        if not messagebox.askyesno(
            "Rehash All Cards",
            "This will delete all stored hashes and recompute them.\n\n"
            "This is required when the hash size has changed (e.g. upgrading from\n"
            "8-bit to 16-bit hashes for better accuracy).\n\n"
            "It may take several minutes. Continue?",
        ):
            return

        win = tk.Toplevel(self)
        win.title("Rehashing Cards")
        win.resizable(False, False)
        win.configure(bg="#1a1a2e")
        win.grab_set()

        tk.Label(win, text="Recomputing perceptual hashes...", bg="#1a1a2e", fg="white",
                 font=("Helvetica", 11)).pack(padx=20, pady=(16, 6))

        log_var = tk.StringVar(value="Clearing old hashes...")
        tk.Label(win, textvariable=log_var, bg="#1a1a2e", fg="#a0a0b0",
                 font=("Helvetica", 9), wraplength=380).pack(padx=20, pady=4)

        bar = ttk.Progressbar(win, mode="indeterminate", length=380)
        bar.pack(padx=20, pady=8)
        bar.start(12)

        close_btn = tk.Button(win, text="Close", state="disabled", bg="#0f3460", fg="white",
                              relief="flat", padx=12, pady=6, command=win.destroy)
        close_btn.pack(pady=(4, 16))

        def progress(msg: str):
            self.after(0, lambda: log_var.set(msg))

        def worker():
            from db.database import clear_all_hashes
            from cards.hasher import compute_all_hashes

            try:
                clear_all_hashes()
                progress("Old hashes cleared. Computing new hashes...")
                compute_all_hashes(progress_callback=progress)
                reload_index()
                self.after(0, self._update_db_info)
                self.after(0, lambda: progress("Rehash complete! Identification accuracy improved."))
            except Exception as e:
                self.after(0, lambda: progress(f"Error: {e}"))
            finally:
                self.after(0, lambda: bar.stop())
                self.after(0, lambda: close_btn.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _run_build_embeddings(self):
        """Compute embeddings for any cards that don't have them yet (incremental)."""
        win = tk.Toplevel(self)
        win.title("Building ML Embeddings")
        win.resizable(False, False)
        win.configure(bg="#1a1a2e")
        win.grab_set()

        tk.Label(win, text="Computing ML embeddings (GPU)...", bg="#1a1a2e", fg="white",
                 font=("Helvetica", 11)).pack(padx=20, pady=(16, 6))

        log_var = tk.StringVar(value="Starting...")
        tk.Label(win, textvariable=log_var, bg="#1a1a2e", fg="#a0a0b0",
                 font=("Helvetica", 9), wraplength=380).pack(padx=20, pady=4)

        bar_var = tk.IntVar(value=0)
        bar = ttk.Progressbar(win, mode="determinate", maximum=100,
                              variable=bar_var, length=380)
        bar.pack(padx=20, pady=8)

        close_btn = tk.Button(win, text="Close", state="disabled", bg="#0f3460", fg="white",
                              relief="flat", padx=12, pady=6, command=win.destroy)
        close_btn.pack(pady=(4, 16))

        def progress(msg: str):
            self.after(0, lambda: log_var.set(msg))
            m = re.search(r"Embedded (\d+)/(\d+)", msg)
            if m:
                done, total = int(m.group(1)), int(m.group(2))
                pct = int(done / total * 100) if total else 0
                self.after(0, lambda p=pct: bar_var.set(p))

        def worker():
            from cards.embedding_computer import compute_all_embeddings

            try:
                compute_all_embeddings(progress_callback=progress)
                reload_embedding_index()
                self.after(0, self._update_db_info)
                self.after(0, lambda: bar_var.set(100))
                self.after(0, lambda: progress("Embeddings complete!"))
            except Exception as e:
                self.after(0, lambda: progress(f"Error: {e}"))
            finally:
                self.after(0, lambda: close_btn.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _run_rebuild_embeddings(self):
        """Clear all stored embeddings and recompute from scratch."""
        if not messagebox.askyesno(
            "Rebuild Embeddings",
            "This will delete all stored ML embeddings and recompute them.\n\n"
            "Useful if the model or preprocessing has changed.\n"
            "May take 10–20 minutes on GPU. Continue?",
        ):
            return

        win = tk.Toplevel(self)
        win.title("Rebuilding ML Embeddings")
        win.resizable(False, False)
        win.configure(bg="#1a1a2e")
        win.grab_set()

        tk.Label(win, text="Recomputing ML embeddings...", bg="#1a1a2e", fg="white",
                 font=("Helvetica", 11)).pack(padx=20, pady=(16, 6))

        log_var = tk.StringVar(value="Clearing old embeddings...")
        tk.Label(win, textvariable=log_var, bg="#1a1a2e", fg="#a0a0b0",
                 font=("Helvetica", 9), wraplength=380).pack(padx=20, pady=4)

        bar_var = tk.IntVar(value=0)
        bar = ttk.Progressbar(win, mode="determinate", maximum=100,
                              variable=bar_var, length=380)
        bar.pack(padx=20, pady=8)

        close_btn = tk.Button(win, text="Close", state="disabled", bg="#0f3460", fg="white",
                              relief="flat", padx=12, pady=6, command=win.destroy)
        close_btn.pack(pady=(4, 16))

        def progress(msg: str):
            self.after(0, lambda: log_var.set(msg))
            m = re.search(r"Embedded (\d+)/(\d+)", msg)
            if m:
                done, total = int(m.group(1)), int(m.group(2))
                pct = int(done / total * 100) if total else 0
                self.after(0, lambda p=pct: bar_var.set(p))

        def worker():
            from db.database import clear_all_embeddings
            from cards.embedding_computer import compute_all_embeddings

            try:
                clear_all_embeddings()
                progress("Old embeddings cleared. Computing new embeddings...")
                compute_all_embeddings(progress_callback=progress)
                reload_embedding_index()
                self.after(0, self._update_db_info)
                self.after(0, lambda: bar_var.set(100))
                self.after(0, lambda: progress("Rebuild complete!"))
            except Exception as e:
                self.after(0, lambda: progress(f"Error: {e}"))
            finally:
                self.after(0, lambda: close_btn.config(state="normal"))

        threading.Thread(target=worker, daemon=True).start()


def launch():
    app = CardIdentifierApp()
    app.mainloop()
