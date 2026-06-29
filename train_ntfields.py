#!/usr/bin/env python3
"""
train_ntfields.py – GUI trainer for NTFields models.

Usage:
    python3 train_ntfields.py

Discovers all SLAM maps in src/argo_mini/maps/, lets you pick one,
configure training, run it with live log output, and automatically
updates ntfields.yaml so the next launch uses the new model.
"""

import os
import sys
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox

# ── Paths ─────────────────────────────────────────────────────────────────────
WS           = os.path.dirname(os.path.abspath(__file__))
MAPS_DIR     = os.path.join(WS, 'src', 'argo_mini', 'maps')
MODELS_DIR   = os.path.expanduser('~/ntfields_models')
TRAIN_SCRIPT = os.path.join(WS, 'src', 'argo_mini', 'argo_mini',
                             'ntfields_offline_train.py')
NTFIELDS_CFG = os.path.join(WS, 'src', 'argo_mini', 'config', 'ntfields.yaml')


def available_maps() -> dict:
    maps = {}
    for f in sorted(os.listdir(MAPS_DIR)):
        if f.endswith('.yaml'):
            name = f[:-5]
            maps[name] = os.path.join(MAPS_DIR, f)
    return maps


def update_ntfields_yaml(model_path: str):
    with open(NTFIELDS_CFG) as f:
        lines = f.readlines()
    with open(NTFIELDS_CFG, 'w') as f:
        for line in lines:
            if line.strip().startswith('model_path:'):
                indent = len(line) - len(line.lstrip())
                f.write(' ' * indent + f'model_path: {model_path}\n')
            else:
                f.write(line)


def detect_device() -> str:
    try:
        import torch
        return 'cuda' if torch.cuda.is_available() else 'cpu'
    except ImportError:
        return 'cpu'


# ── Epoch presets ─────────────────────────────────────────────────────────────
EPOCH_PRESETS = [
    ('300  – Quick test',    300,    'grey60'),
    ('1 000 – Draft',       1000,   '#74c7ec'),
    ('3 000 – Standard',    3000,   '#89b4fa'),
    ('5 000 – High quality',5000,   '#cba6f7'),
    ('10 000 – Production', 10000,  '#a6e3a1'),
]


# ── GUI ───────────────────────────────────────────────────────────────────────

class TrainerApp(tk.Tk):

    BG   = '#1e1e2e'
    CARD = '#313244'
    FG   = '#cdd6f4'
    ACC  = '#89b4fa'

    def __init__(self):
        super().__init__()
        self.title('NTFields Map Trainer')
        self.resizable(False, False)
        self.configure(bg=self.BG)

        self.maps      = available_maps()
        self.device    = detect_device()
        self._proc     = None
        self._training = False
        self._epoch_var = tk.IntVar(value=3000)

        self._build_ui()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _card(self, row, title=None):
        """Create a card frame and optionally add a title label."""
        fr = tk.Frame(self, bg=self.CARD, padx=12, pady=10)
        fr.grid(row=row, column=0, padx=16, pady=4, sticky='ew')
        if title:
            tk.Label(fr, text=title, bg=self.CARD, fg=self.ACC,
                     font=('Inter', 9, 'bold')).grid(
                         row=0, column=0, columnspan=3, sticky='w', pady=(0, 6))
        return fr

    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────────────
        tk.Label(self, text='NTFields Map Trainer', bg=self.BG, fg=self.ACC,
                 font=('Inter', 14, 'bold')).grid(
                     row=0, column=0, pady=(16, 2), padx=16)
        tk.Label(self, text=f'Device: {self.device.upper()}',
                 bg=self.BG, fg='#6c7086',
                 font=('Inter', 9)).grid(row=1, column=0, pady=(0, 6))

        # ── Map selector ──────────────────────────────────────────────────────
        mcard = self._card(2, 'Select Map')
        self._map_var = tk.StringVar()
        map_names = list(self.maps.keys())
        cb = ttk.Combobox(mcard, textvariable=self._map_var,
                          values=map_names, state='readonly', width=38)
        if map_names:
            cb.current(0)
        cb.grid(row=1, column=0, sticky='w')
        cb.bind('<<ComboboxSelected>>', self._on_map_change)

        # ── Epoch presets ─────────────────────────────────────────────────────
        ecard = self._card(3, 'Training Quality (Epochs)')

        for col, (label, value, colour) in enumerate(EPOCH_PRESETS):
            btn = tk.Radiobutton(
                ecard,
                text=label,
                variable=self._epoch_var,
                value=value,
                bg=self.CARD,
                fg=colour,
                selectcolor='#45475a',
                activebackground=self.CARD,
                activeforeground=colour,
                font=('Inter', 10),
                indicatoron=False,
                relief='flat',
                overrelief='flat',
                bd=0,
                padx=10,
                pady=6,
                cursor='hand2',
                command=lambda c=colour, v=value: self._on_epoch_select(c, v),
            )
            btn.grid(row=1, column=col, padx=4, pady=2, sticky='ew')
            ecard.columnconfigure(col, weight=1)

        # Custom epoch entry
        tk.Label(ecard, text='or custom:', bg=self.CARD, fg=self.FG,
                 font=('Inter', 9)).grid(row=2, column=0, sticky='w', pady=(6, 0))
        self._custom_epoch = tk.StringVar()
        custom_e = tk.Entry(ecard, textvariable=self._custom_epoch,
                            width=10, bg='#45475a', fg=self.FG,
                            insertbackground=self.FG, relief='flat')
        custom_e.grid(row=2, column=1, sticky='w', padx=(4, 0), pady=(6, 0))
        custom_e.bind('<FocusOut>', self._on_custom_epoch)
        custom_e.bind('<Return>',   self._on_custom_epoch)

        self._epoch_label = tk.Label(ecard, bg=self.CARD, fg='#a6e3a1',
                                     font=('Inter', 9, 'italic'))
        self._epoch_label.grid(row=2, column=2, columnspan=3, sticky='w',
                               padx=(16, 0), pady=(6, 0))
        self._update_epoch_label(3000)

        # ── Advanced params ───────────────────────────────────────────────────
        pcard = self._card(4, 'Advanced Parameters')
        params = [
            ('Training pairs', 'pairs', '200000'),
            ('Batch size',     'batch', '2000'),
            ('d_min (m)',      'd_min', '0.07'),
            ('d_max (m)',      'd_max', '0.70'),
        ]
        self._pvars = {}
        for i, (label, key, default) in enumerate(params):
            col = (i % 2) * 2
            row = 1 + i // 2
            tk.Label(pcard, text=label, bg=self.CARD, fg=self.FG,
                     font=('Inter', 9)).grid(row=row, column=col, sticky='w',
                                             pady=3, padx=(0, 4))
            var = tk.StringVar(value=default)
            tk.Entry(pcard, textvariable=var, width=12, bg='#45475a', fg=self.FG,
                     insertbackground=self.FG, relief='flat').grid(
                         row=row, column=col + 1, sticky='w', padx=(0, 20), pady=3)
            self._pvars[key] = var

        # ── Output path ───────────────────────────────────────────────────────
        ocard = self._card(5, 'Model Output Path')
        self._out_var = tk.StringVar()
        tk.Entry(ocard, textvariable=self._out_var, width=52,
                 bg='#45475a', fg=self.FG, insertbackground=self.FG,
                 relief='flat').grid(row=1, column=0, sticky='ew')
        self._on_map_change()

        # ── Train button ──────────────────────────────────────────────────────
        self._btn = tk.Button(
            self, text='▶  Train', font=('Inter', 12, 'bold'),
            bg=self.ACC, fg='#1e1e2e', activebackground='#74c7ec',
            activeforeground='#1e1e2e', relief='flat', bd=0,
            padx=24, pady=10, cursor='hand2',
            command=self._start_training,
        )
        self._btn.grid(row=6, column=0, pady=14)

        # ── Log output ────────────────────────────────────────────────────────
        lcard = self._card(7)
        self._log = tk.Text(lcard, height=16, width=70,
                            bg='#11111b', fg='#a6e3a1',
                            font=('JetBrains Mono', 9), relief='flat',
                            state='disabled')
        scroll = tk.Scrollbar(lcard, command=self._log.yview)
        self._log.configure(yscrollcommand=scroll.set)
        self._log.grid(row=0, column=0)
        scroll.grid(row=0, column=1, sticky='ns')

        # Status bar
        self._status = tk.StringVar(value='Ready')
        tk.Label(self, textvariable=self._status, bg=self.BG, fg='#6c7086',
                 font=('Inter', 9)).grid(row=8, column=0, pady=(0, 10))

    def _update_epoch_label(self, epochs: int):
        minutes = round(epochs * 0.012)   # rough estimate
        self._epoch_label.config(text=f'≈ {minutes} min on Jetson GPU')

    def _on_epoch_select(self, colour, value):
        self._update_epoch_label(value)
        self._custom_epoch.set('')

    def _on_custom_epoch(self, *_):
        val = self._custom_epoch.get().strip()
        if val.isdigit() and int(val) > 0:
            self._epoch_var.set(int(val))
            self._update_epoch_label(int(val))

    def _on_map_change(self, *_):
        name = self._map_var.get()
        if name:
            self._out_var.set(os.path.join(MODELS_DIR, f'{name}.pt'))

    # ── Training ──────────────────────────────────────────────────────────────

    def _log_write(self, text: str):
        self._log.configure(state='normal')
        self._log.insert('end', text)
        self._log.see('end')
        self._log.configure(state='disabled')

    def _start_training(self):
        if self._training:
            return

        map_name = self._map_var.get()
        if not map_name:
            messagebox.showerror('No map', 'Please select a map first.')
            return

        map_yaml   = self.maps[map_name]
        model_path = self._out_var.get().strip()
        os.makedirs(os.path.dirname(model_path), exist_ok=True)

        try:
            epochs = self._epoch_var.get()
            pairs  = int(self._pvars['pairs'].get())
            batch  = int(self._pvars['batch'].get())
            d_min  = float(self._pvars['d_min'].get())
            d_max  = float(self._pvars['d_max'].get())
        except ValueError as e:
            messagebox.showerror('Bad parameter', str(e))
            return

        # -u = unbuffered stdout so log lines appear immediately in the GUI
        print_every = max(1, epochs // 60)   # ~60 updates regardless of epoch count
        cmd = [
            sys.executable, '-u', TRAIN_SCRIPT,
            '--map',         map_yaml,
            '--output',      model_path,
            '--epochs',      str(epochs),
            '--pairs',       str(pairs),
            '--batch',       str(batch),
            '--d-min',       str(d_min),
            '--d-max',       str(d_max),
            '--device',      self.device,
            '--print-every', str(print_every),
        ]

        self._log.configure(state='normal')
        self._log.delete('1.0', 'end')
        self._log.configure(state='disabled')
        self._log_write(f'Map:    {map_name}\n')
        self._log_write(f'Output: {model_path}\n')
        self._log_write(f'Device: {self.device.upper()} | '
                        f'Epochs: {epochs:,} | Pairs: {pairs:,}\n')
        self._log_write('─' * 60 + '\n')

        self._training = True
        self._btn.configure(state='disabled', text='Training…',
                            bg='#585b70', fg='#cdd6f4')
        self._status.set('Training in progress — do not close this window')

        threading.Thread(target=self._run_training,
                         args=(cmd, model_path), daemon=True).start()

    def _run_training(self, cmd: list, model_path: str):
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in self._proc.stdout:
                self.after(0, self._log_write, line)
            self._proc.wait()
            rc = self._proc.returncode
        except Exception as e:
            self.after(0, self._log_write, f'\n[ERROR] {e}\n')
            rc = 1
        finally:
            self._proc = None

        self.after(0, self._on_training_done, rc, model_path)

    def _on_training_done(self, returncode: int, model_path: str):
        self._training = False
        self._btn.configure(state='normal', text='▶  Train',
                            bg=self.ACC, fg='#1e1e2e')

        if returncode == 0:
            update_ntfields_yaml(model_path)
            self._log_write('\n✓ ntfields.yaml updated.\n')
            self._log_write('  Restart the navigation stack to use the new model.\n')
            self._status.set(f'Done — model saved to {model_path}')
            messagebox.showinfo('Training complete',
                                f'Model saved to:\n{model_path}\n\n'
                                'ntfields.yaml updated.\n'
                                'Restart the nav stack to use it.')
        else:
            self._log_write(f'\n✗ Training failed (exit code {returncode}).\n')
            self._status.set('Training failed — see log above')
            messagebox.showerror('Training failed',
                                 'Check the log for details.')

    def destroy(self):
        if self._proc:
            self._proc.terminate()
        super().destroy()


if __name__ == '__main__':
    app = TrainerApp()
    app.mainloop()
