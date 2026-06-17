"""
App Launcher - Desktop Launcher
Starts the local backend silently, then opens the web app in the browser.
Double-click this script (or the compiled .exe) to use.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import urllib.request
import webbrowser

# App URL - update if Netlify site name changes
NETLIFY_URL = 'https://foundry-video-editor.netlify.app'
HEALTH_URL = 'http://localhost:5000/health'

# Fallback Dropbox path for server.py
DROPBOX_SERVER = os.path.join(
    os.path.expanduser('~'), 'Dropbox',
    'Scripts', 'Video Editor Downloads',
    'foundry-video-editor-backend', 'server.py'
)

# Colors
BG = '#1A1A1A'
LOG_BG = '#111111'
WHITE = '#FFFFFF'
MUTED = '#6B6B6B'
DOT_GRAY = '#6B6B6B'
DOT_GREEN = '#2D6A4F'
DOT_RED = '#CC2200'
ORANGE = '#E8541A'
ORANGE_HOV = '#C94516'
CREAM = '#FAFAF2'
AMBER = '#F5A623'
SKY = '#41B6E6'
NAVY = '#1E2530'


def _resource_path(name):
    """Path to a bundled resource (works for PyInstaller onefile and source)."""
    base = getattr(sys, '_MEIPASS', None)
    if base and os.path.isfile(os.path.join(base, name)):
        return os.path.join(base, name)
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
    return os.path.join(here, name)


class LauncherApp:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self._backend_log_path = self._get_backend_log_path()
        self._backend_log_handle = None
        self._build_window()
        self._build_ui()
        self.root.after(0, self._launch_sequence)

    def _build_window(self):
        self.root.title('App Launcher')
        try:
            _icon = _resource_path('rocket.ico')
            if os.path.isfile(_icon):
                self.root.iconbitmap(_icon)
        except Exception:
            pass
        self.root.geometry('380x600')
        self.root.resizable(False, False)
        self.root.configure(bg=BG)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    def _build_rocket(self):
        c = tk.Canvas(self.root, width=360, height=132, bg=BG, highlightthickness=0)
        c.pack(pady=(26, 0))
        cx = 180
        # flame
        c.create_polygon(cx, 130, cx - 13, 104, cx + 13, 104, fill=AMBER, outline='')
        c.create_polygon(cx, 121, cx - 6, 104, cx + 6, 104, fill=CREAM, outline='')
        # fins
        c.create_polygon(cx - 22, 70, cx - 22, 102, cx - 42, 104, fill=ORANGE, outline='')
        c.create_polygon(cx + 22, 70, cx + 22, 102, cx + 42, 104, fill=ORANGE, outline='')
        # body
        c.create_oval(cx - 22, 92, cx + 22, 116, fill=CREAM, outline='')
        c.create_rectangle(cx - 22, 56, cx + 22, 104, fill=CREAM, outline='')
        # nose cone
        c.create_polygon(cx, 16, cx - 22, 60, cx + 22, 60, fill=ORANGE, outline='')
        c.create_rectangle(cx - 22, 50, cx + 22, 62, fill=ORANGE, outline='')
        # window
        c.create_oval(cx - 13, 63, cx + 13, 89, fill=NAVY, outline='')
        c.create_oval(cx - 8, 68, cx + 8, 84, fill=SKY, outline='')
        # motion ticks
        for dy in (74, 86, 98):
            c.create_line(cx - 58, dy, cx - 44, dy, fill='#3A3A3A', width=3)
            c.create_line(cx + 44, dy, cx + 58, dy, fill='#3A3A3A', width=3)

    def _build_ui(self):
        self._build_rocket()
        tk.Label(
            self.root,
            text='App Launcher',
            bg=BG,
            fg=WHITE,
            font=('Arial', 18, 'bold')
        ).pack(pady=(8, 2))

        tk.Label(
            self.root,
            text='Mission control \u2014 prepping for launch',
            bg=BG,
            fg=MUTED,
            font=('Arial', 11)
        ).pack()

        status_row = tk.Frame(self.root, bg=BG)
        status_row.pack(pady=(40, 0))

        self._dot_canvas = tk.Canvas(
            status_row,
            width=12,
            height=12,
            bg=BG,
            highlightthickness=0
        )
        self._dot_canvas.pack(side=tk.LEFT, padx=(0, 8))
        self._dot = self._dot_canvas.create_oval(
            1, 1, 11, 11, fill=DOT_GRAY, outline=''
        )

        self._status_var = tk.StringVar(value='Fueling up\u2026')
        self._status_label = tk.Label(
            status_row,
            textvariable=self._status_var,
            bg=BG,
            fg=MUTED,
            font=('Arial', 12)
        )
        self._status_label.pack(side=tk.LEFT)

        self._btn_frame = tk.Frame(self.root, bg=BG)
        self._btn_frame.pack(pady=(44, 0), padx=20, fill=tk.X)
        btn_frame = self._btn_frame

        self._btn = tk.Button(
            btn_frame,
            text='Launch \U0001F680',
            bg=ORANGE,
            fg=WHITE,
            activebackground=ORANGE_HOV,
            font=('Arial', 14, 'bold'),
            height=2,
            relief=tk.FLAT,
            cursor='hand2',
            state=tk.DISABLED,
            command=self._open_browser,
        )
        self._btn.pack(fill=tk.X)

        log_frame = tk.Frame(self.root, bg=BG)
        log_frame.pack(pady=(16, 0), padx=20, fill=tk.X)

        self._log = tk.Text(
            log_frame,
            height=10,
            bg=LOG_BG,
            fg=MUTED,
            font=('Courier', 10),
            relief=tk.FLAT,
            borderwidth=0,
            highlightthickness=0,
            state=tk.DISABLED,
            wrap=tk.WORD,
        )
        self._log.pack(fill=tk.X)

    def _set_status(self, text, dot_color):
        label_color = WHITE if dot_color == DOT_GREEN else (
            '#FF6655' if dot_color == DOT_RED else MUTED
        )

        def _do():
            self._status_var.set(text)
            self._dot_canvas.itemconfig(self._dot, fill=dot_color)
            self._status_label.config(fg=label_color)

        self.root.after(0, _do)

    def _log_line(self, msg):
        def _do():
            self._log.config(state=tk.NORMAL)
            self._log.insert(tk.END, msg + '\n')
            self._log.see(tk.END)
            self._log.config(state=tk.DISABLED)

        self.root.after(0, _do)

    def _enable_btn(self):
        self.root.after(0, lambda: self._btn.config(state=tk.NORMAL))

    def _get_backend_log_path(self):
        local_app_data = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
        log_dir = os.path.join(local_app_data, 'Foundry Video Editor', 'logs')
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, 'backend.log')

    def _reset_backend_log(self):
        with open(self._backend_log_path, 'w', encoding='utf-8') as handle:
            handle.write('[launcher] Backend log initialized\n')

    def _read_backend_log_tail(self, max_lines=20):
        try:
            with open(self._backend_log_path, 'r', encoding='utf-8', errors='replace') as handle:
                lines = handle.readlines()
        except Exception:
            return ['(backend log unavailable)']
        tail = [line.rstrip() for line in lines[-max_lines:] if line.rstrip()]
        return tail or ['(no backend log output captured)']

    def _show_copy_error_btn(self):
        def _do():
            copy_btn = tk.Button(
                self._btn_frame,
                text='Copy Error',
                bg='#333333',
                fg=WHITE,
                activebackground='#444444',
                font=('Arial', 11),
                height=1,
                relief=tk.FLAT,
                cursor='hand2',
                command=self._copy_error_to_clipboard,
            )
            copy_btn.pack(fill=tk.X, pady=(8, 0))

        self.root.after(0, _do)

    def _copy_error_to_clipboard(self):
        text = '\n'.join(self._read_backend_log_tail())
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _launch_sequence(self):
        server_path = self._find_server()
        if not server_path:
            self._set_status('Error - see below', DOT_RED)
            self._log_line('server.py not found. Place this app in the')
            self._log_line('same folder as server.py, or ensure Dropbox is synced.')
            return

        python_exe = self._find_python()
        if not python_exe:
            self._set_status('Error - see below', DOT_RED)
            self._log_line('Python not found. Please install Python from python.org')
            return

        self._log_line(f'[launcher] Python: {python_exe}')
        self._log_line(f'[launcher] Using backend: {server_path}')
        self._log_line(f'[launcher] Backend log: {self._backend_log_path}')

        req_path = os.path.join(os.path.dirname(server_path), 'requirements.txt')
        if os.path.isfile(req_path):
            self._log_line('[launcher] Installing dependencies...')
            try:
                subprocess.run(
                    [python_exe, '-m', 'pip', 'install', '-r', req_path,
                     '-q', '--disable-pip-version-check'],
                    timeout=120,
                    capture_output=True,
                    creationflags=0x08000000,
                )
            except Exception as exc:
                self._log_line(f'[launcher] pip warning: {exc}')

        self._log_line('[launcher] Starting backend...')
        try:
            self._reset_backend_log()
            self._backend_log_handle = open(
                self._backend_log_path, 'a', encoding='utf-8', buffering=1
            )
            self.proc = subprocess.Popen(
                [python_exe, server_path],
                stdout=self._backend_log_handle,
                stderr=subprocess.STDOUT,
                creationflags=0x08000000,
            )
        except Exception as exc:
            self._set_status('Error - see below', DOT_RED)
            self._log_line(f'[launcher] Failed to start: {exc}')
            return

        time.sleep(2)
        if self.proc.poll() is not None:
            self._set_status('Backend failed to start - see error below', DOT_RED)
            for line in self._read_backend_log_tail():
                self._log_line(line)
            self._show_copy_error_btn()
            return

        # Whisper/torch imports can take 30-60s on a cold start — 15s caused
        # false "failed to start" errors while the backend was still loading.
        deadline = time.time() + 90
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self._set_status('Backend failed to start - see error below', DOT_RED)
                time.sleep(0.3)
                for line in self._read_backend_log_tail():
                    self._log_line(line)
                self._show_copy_error_btn()
                return

            try:
                with urllib.request.urlopen(HEALTH_URL, timeout=1) as resp:
                    if resp.status == 200:
                        self._set_status('Cleared for launch', DOT_GREEN)
                        self._log_line('[launcher] Backend ready')
                        self._enable_btn()
                        return
            except Exception:
                pass

            time.sleep(0.5)

        self._set_status('Backend failed to start - see error below', DOT_RED)
        self._log_line('[launcher] Backend did not respond within 90s.')
        for line in self._read_backend_log_tail():
            self._log_line(line)
        self._show_copy_error_btn()

    def _find_server(self):
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.abspath(__file__))

        same_folder = os.path.join(base, 'server.py')
        self._log_line(f'[launcher] Checking: {same_folder}')
        if os.path.isfile(same_folder):
            return same_folder

        self._log_line(f'[launcher] Checking: {DROPBOX_SERVER}')
        if os.path.isfile(DROPBOX_SERVER):
            return DROPBOX_SERVER

        return None

    def _find_python(self):
        candidates = []

        if not getattr(sys, 'frozen', False):
            candidates.append(sys.executable)

        candidates.extend([
            shutil.which('python'),
            shutil.which('python3'),
            'python',
            'python3',
        ])

        for exe in candidates:
            if not exe:
                continue
            try:
                result = subprocess.run(
                    [exe, '--version'],
                    capture_output=True,
                    timeout=5,
                    creationflags=0x08000000,
                )
                if result.returncode == 0:
                    return exe
            except Exception:
                continue
        return None

    def _open_browser(self):
        webbrowser.open(NETLIFY_URL)

    def _on_close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        if self._backend_log_handle:
            try:
                self._backend_log_handle.close()
            except Exception:
                pass
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()
