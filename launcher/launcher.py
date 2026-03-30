"""
Foundry Video Editor — Desktop Launcher
Starts the local backend silently, then opens the web app in the browser.
Double-click this script (or the compiled .exe) to use.
"""

import os
import sys
import shutil
import subprocess
import threading
import time
import urllib.request
import webbrowser
import tkinter as tk

# ── App URL — update if Netlify site name changes ──
NETLIFY_URL = 'https://foundry-video-editor.netlify.app'
HEALTH_URL  = 'http://localhost:5000/health'

# Fallback Dropbox path for server.py
DROPBOX_SERVER = os.path.join(
    os.path.expanduser('~'), 'Dropbox',
    'Scripts', 'Video Editor Downloads',
    'foundry-video-editor-backend', 'server.py'
)

# ── Colors ──
BG         = '#1A1A1A'
LOG_BG     = '#111111'
WHITE      = '#FFFFFF'
MUTED      = '#6B6B6B'
DOT_GRAY   = '#6B6B6B'
DOT_GREEN  = '#2D6A4F'
DOT_RED    = '#CC2200'
ORANGE     = '#E8541A'
ORANGE_HOV = '#C94516'


class LauncherApp:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self._stderr_lines = []
        self._build_window()
        self._build_ui()
        threading.Thread(target=self._launch_sequence, daemon=True).start()

    def _build_window(self):
        self.root.title('Foundry Video Editor')
        self.root.geometry('360x500')
        self.root.resizable(False, False)
        self.root.configure(bg=BG)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

    def _build_ui(self):
        tk.Label(
            self.root,
            text='Foundry Video Editor',
            bg=BG,
            fg=WHITE,
            font=('Arial', 16, 'bold')
        ).pack(pady=(40, 2))

        tk.Label(
            self.root,
            text='The Candler Foundry',
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

        self._status_var = tk.StringVar(value='Starting backend...')
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
            text='Open Video Editor',
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

    def _read_stderr(self, proc):
        """Daemon thread: read stderr line by line into buffer and log widget."""
        try:
            for raw_line in proc.stderr:
                line = raw_line.decode('utf-8', errors='replace').rstrip()
                if line:
                    self._stderr_lines.append(line)
                    self._log_line(line)
        except Exception:
            pass

    def _show_copy_error_btn(self):
        """Dynamically add a Copy Error button below the main action button."""
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
        text = '\n'.join(self._stderr_lines[-20:])
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _launch_sequence(self):
        server_path = self._find_server()
        if not server_path:
            self._set_status('Error — see below', DOT_RED)
            self._log_line('server.py not found. Place this app in the')
            self._log_line('same folder as server.py, or ensure Dropbox is synced.')
            return

        python_exe = self._find_python()
        if not python_exe:
            self._set_status('Error — see below', DOT_RED)
            self._log_line('Python not found. Please install Python from python.org')
            return

        self._log_line(f'[launcher] Python: {python_exe}')

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
            except Exception as e:
                self._log_line(f'[launcher] pip warning: {e}')

        self._log_line('[launcher] Starting backend...')
        try:
            self.proc = subprocess.Popen(
                [python_exe, server_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=0x08000000,
            )
        except Exception as e:
            self._set_status('Error — see below', DOT_RED)
            self._log_line(f'[launcher] Failed to start: {e}')
            return

        # Start daemon thread to capture stderr continuously
        threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True).start()

        # Wait 2 seconds then check if process is still alive
        time.sleep(2)
        if self.proc.poll() is not None:
            self._set_status('Backend failed to start — see error below', DOT_RED)
            tail = self._stderr_lines[-20:] or ['(no stderr output captured)']
            for line in tail:
                self._log_line(line)
            self._show_copy_error_btn()
            return

        # Process still alive — poll health endpoint for up to 15 seconds
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self._set_status('Backend failed to start — see error below', DOT_RED)
                time.sleep(0.3)  # let stderr reader catch up
                tail = self._stderr_lines[-20:] or ['(no stderr output captured)']
                for line in tail:
                    self._log_line(line)
                self._show_copy_error_btn()
                return

            try:
                with urllib.request.urlopen(HEALTH_URL, timeout=1) as resp:
                    if resp.status == 200:
                        self._set_status('Backend running', DOT_GREEN)
                        self._log_line('[launcher] Backend ready')
                        self._enable_btn()
                        return
            except Exception:
                pass

            time.sleep(0.5)

        self._set_status('Backend failed to start — see error below', DOT_RED)
        self._log_line('[launcher] Backend did not respond within 15s.')
        tail = self._stderr_lines[-20:] or ['(no stderr output captured)']
        for line in tail:
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
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()