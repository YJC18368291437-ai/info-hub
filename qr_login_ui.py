# -*- coding: utf-8 -*-
"""info-hub QQ 采集号扫码登录窗口。

- 轮询 NapCat 的本地 OneBot HTTP 接口判断是否已登录。
- 登录成功自动关闭；离线时显示 cache/qrcode*.png 中最新的二维码。
- 提供“重启 NapCat 生成新码”按钮（只处理 1000000001，不碰其他 QQ）。
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import threading
import time
import tkinter as tk
import urllib.request

QQ = "1000000001"
QR_DIR = r"D:\napcat-toolkit\napcat\cache"
TOKEN_PATH = r"D:\info-hub\data\napcat_http_token.txt"
API = "http://127.0.0.1:8083/"
GUARD = r"D:\info-hub\infohub_login_guard.ps1"
SNOOZE = r"D:\info-hub\data\qr_ui_snooze"
SNOOZE_SECONDS = 600
QR_MAX_AGE = 300
PREVIEW = os.environ.get("INFOHUB_QR_PREVIEW") == "1"
CREATE_NO_WINDOW = 0x08000000


def _token() -> str:
    try:
        return open(TOKEN_PATH, encoding="utf-8").read().strip()
    except OSError:
        return ""


def _api(action: str) -> dict:
    req = urllib.request.Request(
        API + action, headers={"Authorization": "Bearer " + _token()}
    )
    with urllib.request.urlopen(req, timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _login() -> tuple[bool, str]:
    try:
        data = _api("get_login_info")
        info = data.get("data") or {}
        if data.get("retcode") == 0 and str(info.get("user_id") or "") == QQ:
            return True, str(info.get("nickname") or "")
    except Exception:
        pass
    return False, ""


def _newest_qr() -> str:
    files = glob.glob(os.path.join(QR_DIR, "qrcode*.png"))
    files = [f for f in files if os.path.isfile(f) and os.path.getsize(f) > 0]
    return max(files, key=os.path.getmtime) if files else ""


def _to_gif(src: str, dst: str) -> bool:
    ps = (
        "Add-Type -AssemblyName System.Drawing; "
        "$img = [System.Drawing.Image]::FromFile('{src}'); "
        "$bmp = New-Object System.Drawing.Bitmap($img.Width, $img.Height); "
        "$g = [System.Drawing.Graphics]::FromImage($bmp); "
        "$g.Clear([System.Drawing.Color]::White); "
        "$g.DrawImage($img, 0, 0, $img.Width, $img.Height); "
        "$bmp.Save('{dst}', [System.Drawing.Imaging.ImageFormat]::Gif); "
        "$g.Dispose(); $bmp.Dispose(); $img.Dispose()"
    ).format(src=src, dst=dst)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW,
        )
        return r.returncode == 0 and os.path.exists(dst)
    except Exception:
        return False


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._stop = False
        self._lock = threading.Lock()
        self._snap = {"online": False, "nick": "", "qr": "", "mt": 0.0}
        self._photo = None
        self._shown_mt = 0.0
        self._closed = False

        root.title("info-hub 采集号 · 扫码登录")
        root.geometry("520x640")
        root.resizable(False, False)
        root.configure(bg="#f4f4f4")
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        tk.Label(
            root, text="请用手机 QQ 扫码登录", bg="#f4f4f4",
            font=("Microsoft YaHei", 16, "bold"),
        ).pack(pady=(16, 2))
        tk.Label(
            root, text=f"账号 {QQ}（info-hub 采集号）", bg="#f4f4f4",
            fg="#777", font=("Microsoft YaHei", 10),
        ).pack()
        self.status = tk.Label(
            root, text="正在检查登录状态…", bg="#f4f4f4", fg="#666",
            font=("Microsoft YaHei", 12, "bold"),
        )
        self.status.pack(pady=(12, 4))
        self.img = tk.Label(root, bg="#f4f4f4")
        self.img.pack(pady=8)
        self.hint = tk.Label(
            root, text="登录成功后本窗口会自动关闭", bg="#f4f4f4",
            fg="#999", font=("Microsoft YaHei", 9),
        )
        self.hint.pack()

        btns = tk.Frame(root, bg="#f4f4f4")
        btns.pack(side="bottom", pady=16)
        tk.Button(
            btns, text="重启 NapCat 生成新码", font=("Microsoft YaHei", 10),
            command=self._restart, width=20,
        ).pack(side="left", padx=8)
        tk.Button(
            btns, text="稍后再说", font=("Microsoft YaHei", 10),
            command=self._on_close, width=10,
        ).pack(side="left", padx=8)

        threading.Thread(target=self._worker, daemon=True).start()
        self.root.after(500, self._tick)

    # ---------- 后台轮询 ----------
    def _worker(self) -> None:
        while not self._stop:
            online, nick = (False, "") if PREVIEW else _login()
            qr = _newest_qr()
            try:
                mt = os.path.getmtime(qr) if qr else 0.0
            except OSError:
                mt = 0.0
            with self._lock:
                self._snap = {"online": online, "nick": nick, "qr": qr, "mt": mt}
            for _ in range(20):
                if self._stop:
                    return
                time.sleep(0.1)

    def _snapshot(self) -> dict:
        with self._lock:
            return dict(self._snap)

    # ---------- UI ----------
    def _tick(self) -> None:
        if self._closed:
            return
        snap = self._snapshot()
        if snap["online"]:
            self.status.config(text="已登录 ✓  " + snap["nick"], fg="#1a7f37",
                               font=("Microsoft YaHei", 13, "bold"))
            self.img.config(image="")
            self._photo = None
            self.root.after(1500, self._on_close_auto)
            return
        self._show_qr(snap)
        self.root.after(1000, self._tick)

    def _show_qr(self, snap: dict) -> None:
        path, mt = snap["qr"], snap["mt"]
        if path and mt != self._shown_mt:
            self._shown_mt = mt
            photo = None
            try:
                photo = tk.PhotoImage(file=path)
            except Exception:
                view = os.path.join(QR_DIR, "qrcode_login_view.gif")
                if _to_gif(path, view):
                    try:
                        photo = tk.PhotoImage(file=view)
                    except Exception:
                        photo = None
            if photo is not None:
                try:
                    photo = photo.zoom(2, 2)
                except Exception:
                    pass
                self._photo = photo
                self.img.config(image=photo)
            else:
                self.img.config(image="")
                self._photo = None
        age = time.time() - mt if mt else 99999
        if not path:
            self.status.config(text="正在等待 NapCat 生成二维码…", fg="#666",
                               font=("Microsoft YaHei", 11, "bold"))
            self.hint.config(text="若长时间没有二维码，点下方按钮重启 NapCat")
        elif age > QR_MAX_AGE:
            self.status.config(text=f"二维码已过期（{int(age)} 秒前）", fg="#c0392b",
                               font=("Microsoft YaHei", 12, "bold"))
            self.hint.config(text="点“重启 NapCat 生成新码”后扫新码")
        else:
            self.status.config(text="未登录，请扫码", fg="#d9534f",
                               font=("Microsoft YaHei", 13, "bold"))
            self.hint.config(text=f"二维码生成于 {time.strftime('%H:%M:%S', time.localtime(mt))}")

    def _restart(self) -> None:
        self.status.config(text="正在重启 NapCat，请稍候…", fg="#666",
                           font=("Microsoft YaHei", 12, "bold"))
        self.hint.config(text="重启后会自动刷新二维码")

        def run() -> None:
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", GUARD, "-RestartNow"],
                    capture_output=True, timeout=90, creationflags=CREATE_NO_WINDOW,
                )
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _on_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            with open(SNOOZE, "w", encoding="utf-8") as f:
                f.write(str(int(time.time())))
        except OSError:
            pass
        self._stop = True
        self.root.destroy()

    def _on_close_auto(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop = True
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
