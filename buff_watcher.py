"""
Buff Watcher v3 — 자동 아이콘 감지 + 강건한 매칭
================================================
- 창 선택 → ROI 지정 (한 번)
- ROI 내부에서 아이콘을 '자동 분리' (행 분산 기반)
- 매칭은 HSV 색상 히스토그램 — 쿨타임 어둠/배경 변화에 강건
- 빠진 버프는 메인 창 안에 24x24 썸네일로 인라인 표시
- 2틱 연속 missing이어야 확정 (깜빡임 방지)

설치: pip install mss opencv-python numpy pillow pygetwindow
Windows, 창모드 전용.
"""

import json
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import cv2
import mss
import numpy as np
import pygetwindow as gw
from PIL import Image, ImageTk

CONFIG_DIR = Path.home() / ".buff_watcher"
ICONS_DIR = CONFIG_DIR / "icons"
CONFIG_FILE = CONFIG_DIR / "config.json"
CONFIG_DIR.mkdir(exist_ok=True)
ICONS_DIR.mkdir(exist_ok=True)

FALLBACK_ICON_SIZE = 24
MATCH_THRESHOLD = 0.70
WATCH_INTERVAL = 1.5
CONFIRM_TICKS = 2
THUMB_SIZE = 24


# --- 창 헬퍼 -----------------------------------------------------------------
def list_visible_windows():
    out, seen = [], set()
    for w in gw.getAllWindows():
        try:
            if not w.title or not w.visible:
                continue
            if w.width < 100 or w.height < 100:
                continue
            key = (w.title, w.left, w.top)
            if key in seen:
                continue
            seen.add(key)
            out.append(w)
        except Exception:
            pass
    return out


def get_window_rect(title_substr):
    for w in gw.getAllWindows():
        try:
            if not w.title or not w.visible:
                continue
            if title_substr.lower() not in w.title.lower():
                continue
            if w.isMinimized or w.width < 10 or w.height < 10:
                return None
            return (w.left, w.top, w.width, w.height)
        except Exception:
            continue
    return None


def grab_rect(rect):
    x, y, w, h = rect
    with mss.mss() as sct:
        shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    return np.array(shot)[:, :, :3]


# --- 창 선택 다이얼로그 ------------------------------------------------------
def pick_window_dialog(parent):
    result = {"title": None}
    win = tk.Toplevel(parent)
    win.title("감시할 창 선택")
    win.geometry("560x420")
    win.attributes("-topmost", True)

    tk.Label(win, text="창모드 게임 창을 선택하세요", fg="gray").pack(pady=6)

    frame = tk.Frame(win)
    frame.pack(fill="both", expand=True, padx=10)
    lb = tk.Listbox(frame, font=("맑은 고딕", 10))
    sb = tk.Scrollbar(frame, command=lb.yview)
    lb.config(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    lb.pack(side="left", fill="both", expand=True)

    windows = list_visible_windows()
    for w in windows:
        try:
            lb.insert(tk.END, f"{w.title}  [{w.width}x{w.height}]")
        except Exception:
            lb.insert(tk.END, "(?)")

    def do_ok():
        sel = lb.curselection()
        if sel:
            result["title"] = windows[sel[0]].title
            win.destroy()

    def do_refresh():
        nonlocal windows
        lb.delete(0, tk.END)
        windows = list_visible_windows()
        for w in windows:
            try:
                lb.insert(tk.END, f"{w.title}  [{w.width}x{w.height}]")
            except Exception:
                lb.insert(tk.END, "(?)")

    btns = tk.Frame(win); btns.pack(pady=8)
    tk.Button(btns, text="선택", width=10, command=do_ok).pack(side="left", padx=5)
    tk.Button(btns, text="새로고침", width=10, command=do_refresh).pack(side="left", padx=5)
    tk.Button(btns, text="취소", width=10, command=win.destroy).pack(side="left", padx=5)
    lb.bind("<Double-Button-1>", lambda e: do_ok())

    win.wait_window()
    return result["title"]


# --- ROI 선택 ----------------------------------------------------------------
def pick_roi_on_image(bgr_img, parent):
    result = {"roi": None}
    h, w = bgr_img.shape[:2]
    scale = min((parent.winfo_screenwidth() - 120) / w,
                (parent.winfo_screenheight() - 180) / h, 1.0)
    dw, dh = int(w * scale), int(h * scale)
    disp = cv2.resize(bgr_img, (dw, dh)) if scale < 1.0 else bgr_img.copy()
    photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)))

    win = tk.Toplevel(parent)
    win.title(f"ROI 선택 — 드래그로 버프창 영역 (배율 {scale:.0%})")
    win.attributes("-topmost", True)

    canvas = tk.Canvas(win, width=dw, height=dh, cursor="cross", highlightthickness=0)
    canvas.pack()
    canvas.create_image(0, 0, image=photo, anchor="nw")
    canvas.image = photo

    st = {"x0": 0, "y0": 0, "rid": None}

    def on_press(e):
        st["x0"], st["y0"] = e.x, e.y
        if st["rid"]:
            canvas.delete(st["rid"])
        st["rid"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=2)

    def on_drag(e):
        if st["rid"]:
            canvas.coords(st["rid"], st["x0"], st["y0"], e.x, e.y)

    def on_release(e):
        x1, y1, x2, y2 = st["x0"], st["y0"], e.x, e.y
        rx, ry = min(x1, x2), min(y1, y2)
        rw, rh = abs(x2 - x1), abs(y2 - y1)
        if rw < 5 or rh < 5:
            return
        result["roi"] = (int(rx / scale), int(ry / scale),
                         int(rw / scale), int(rh / scale))

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)

    def ok():
        if result["roi"]:
            win.destroy()
        else:
            messagebox.showwarning("안내", "영역을 드래그하세요.")

    def cancel():
        result["roi"] = None
        win.destroy()

    btns = tk.Frame(win); btns.pack(pady=6)
    tk.Button(btns, text="확인", width=10, command=ok).pack(side="left", padx=5)
    tk.Button(btns, text="취소", width=10, command=cancel).pack(side="left", padx=5)

    win.wait_window()
    return result["roi"]


# --- 자동 아이콘 분리 (격자 방식) --------------------------------------------
def _estimate_icon_size(gray):
    """행 단위 컨텐츠 구간 길이들의 중앙값으로 아이콘 높이 추정."""
    H = gray.shape[0]
    row_std = gray.std(axis=1)
    if row_std.max() == 0:
        return None
    norm = row_std / row_std.max()
    threshold = max(0.15, norm.mean() * 0.4)
    is_c = norm > threshold
    lengths = []
    in_blk = False; start = 0
    for y in range(H):
        if is_c[y] and not in_blk:
            start = y; in_blk = True
        elif not is_c[y] and in_blk:
            if y - start >= 8:
                lengths.append(y - start)
            in_blk = False
    if in_blk and H - start >= 8:
        lengths.append(H - start)

    if not lengths:
        return None
    med = int(np.median(lengths))
    return med if med >= 12 else None


def auto_split_icons(roi_img, empty_std_threshold=15.0):
    """아이콘이 정사각형이라고 가정하고 ROI를 격자로 분할.
    1) 행 분석으로 아이콘 한 변 추정
    2) 그 크기로 격자 생성
    3) 편차 낮은 셀(빈 칸)은 제외

    반환: [(col, row, bgr_img), ...]"""
    if roi_img is None or roi_img.size == 0:
        return []
    gray = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape

    size = _estimate_icon_size(gray) or FALLBACK_ICON_SIZE
    # ROI가 너무 작으면 포기
    if H < size or W < size:
        return []

    cols = max(1, W // size)
    rows = max(1, H // size)

    # 가운데 정렬: 남는 픽셀이 양쪽에 균등 분배되도록 오프셋
    x_off = (W - cols * size) // 2
    y_off = (H - rows * size) // 2

    icons = []
    for r in range(rows):
        for c in range(cols):
            y1 = y_off + r * size
            y2 = y1 + size
            x1 = x_off + c * size
            x2 = x1 + size
            if y2 > H or x2 > W or y1 < 0 or x1 < 0:
                continue
            cell = roi_img[y1:y2, x1:x2]
            if cell.std() < empty_std_threshold:
                continue  # 빈 칸
            icons.append((c, r, cell))
    return icons


# --- 강건한 매칭: HSV 히스토그램 --------------------------------------------
def icon_signature(bgr_img):
    """H, S 히스토그램 (V 무시 → 쿨타임 어둠 불변)."""
    if bgr_img is None or bgr_img.size == 0:
        return None
    h, w = bgr_img.shape[:2]
    m = max(1, min(h, w) // 5)
    core = bgr_img[m:h - m, m:w - m] if (h > 2 * m and w > 2 * m) else bgr_img
    if core.size == 0:
        core = bgr_img
    core = cv2.resize(core, (16, 16), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(core, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [12, 12], [0, 180, 0, 256])
    cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
    return hist.astype(np.float32)


def sig_score(a, b):
    if a is None or b is None:
        return 0.0
    return float(cv2.compareHist(a, b, cv2.HISTCMP_CORREL))


# --- 저장/로드 ---------------------------------------------------------------
def save_templates(window_rect, roi_rel):
    for f in ICONS_DIR.glob("*.png"):
        f.unlink()
    full = grab_rect(window_rect)
    rx, ry, rw, rh = roi_rel
    if rx < 0 or ry < 0 or rx + rw > full.shape[1] or ry + rh > full.shape[0]:
        return -1, []
    roi_img = full[ry:ry + rh, rx:rx + rw]
    icons = auto_split_icons(roi_img)
    if not icons:
        return 0, []
    saved = []
    # col, row 순으로 정렬해서 일관된 인덱스 부여 (행 우선)
    icons_sorted = sorted(icons, key=lambda t: (t[1], t[0]))
    for i, (_c, _r, img) in enumerate(icons_sorted):
        path = ICONS_DIR / f"buff_{i:02d}.png"
        cv2.imwrite(str(path), img)
        saved.append((path.stem, img))
    cv2.imwrite(str(CONFIG_DIR / "reference_roi.png"), roi_img)
    return len(saved), saved


def load_templates():
    out = []
    for f in sorted(ICONS_DIR.glob("*.png")):
        img = cv2.imread(str(f))
        if img is None:
            continue
        out.append((f.stem, img, icon_signature(img)))
    return out


def load_config():
    if not CONFIG_FILE.exists():
        return None
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_config(state):
    CONFIG_FILE.write_text(json.dumps({
        "window_title": state["window_title"],
        "roi_rel": list(state["roi_rel"]) if state["roi_rel"] else None,
    }, indent=2, ensure_ascii=False), encoding="utf-8")


# --- 리뷰 다이얼로그 ---------------------------------------------------------
def review_dialog(parent, saved_icons):
    if not saved_icons:
        return 0
    win = tk.Toplevel(parent)
    win.title("자동 분리 결과 확인")
    win.attributes("-topmost", True)
    win.geometry("700x520")

    tk.Label(win,
             text=(f"총 {len(saved_icons)}개 감지됨. "
                   "잘못 잡힌 항목은 클릭(빨강)해서 제외하세요."),
             fg="gray", wraplength=660, justify="left").pack(pady=6, padx=10, anchor="w")

    area = tk.Canvas(win, highlightthickness=0)
    sb = tk.Scrollbar(win, orient="vertical", command=area.yview)
    area.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    area.pack(side="left", fill="both", expand=True, padx=10)
    inner = tk.Frame(area)
    area.create_window((0, 0), window=inner, anchor="nw")

    def wheel(e):
        area.yview_scroll(int(-e.delta / 120), "units")
    area.bind_all("<MouseWheel>", wheel)

    keep, frames, photos = {}, {}, []
    cols = 10
    for i, (name, img) in enumerate(saved_icons):
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        k = max(1, 40 // max(rgb.shape[0], 1))
        if k > 1:
            rgb = cv2.resize(rgb, None, fx=k, fy=k, interpolation=cv2.INTER_NEAREST)
        ph = ImageTk.PhotoImage(Image.fromarray(rgb))
        photos.append(ph)
        r, c = i // cols, i % cols
        keep[name] = True
        fr = tk.Frame(inner, bd=3, bg="#2da44e")
        fr.grid(row=r, column=c, padx=2, pady=2)
        lb = tk.Label(fr, image=ph, bg="#2da44e", cursor="hand2")
        lb.pack(padx=1, pady=1)

        def mk(n=name, f=fr, l=lb):
            def toggle(_e=None):
                keep[n] = not keep[n]
                c = "#2da44e" if keep[n] else "#cf222e"
                f.config(bg=c); l.config(bg=c)
            return toggle

        t = mk()
        lb.bind("<Button-1>", t)
        fr.bind("<Button-1>", t)
        frames[name] = fr

    inner.update_idletasks()
    area.config(scrollregion=area.bbox("all"))

    result = {"n": -1}

    def do_ok():
        removed = 0
        for name, v in keep.items():
            if not v:
                try:
                    (ICONS_DIR / f"{name}.png").unlink()
                    removed += 1
                except Exception:
                    pass
        result["n"] = len(saved_icons) - removed
        area.unbind_all("<MouseWheel>")
        win.destroy()

    def do_cancel():
        area.unbind_all("<MouseWheel>")
        win.destroy()

    btns = tk.Frame(win); btns.pack(side="bottom", pady=8)
    tk.Button(btns, text="확정", width=12, command=do_ok).pack(side="left", padx=10)
    tk.Button(btns, text="취소", width=12, command=do_cancel).pack(side="left", padx=3)

    win.wait_window()
    return result["n"]


# --- Watcher -----------------------------------------------------------------
class Watcher:
    def __init__(self, root, status_label, missing_frame, missing_count_label,
                 window_title, roi_rel, saved):
        self.root = root
        self.status = status_label
        self.mframe = missing_frame
        self.mcount = missing_count_label
        self.title = window_title
        self.roi_rel = roi_rel
        self.saved = saved  # [(name, img, sig)]
        self.running = False
        self.pending_missing = None
        self.stable_count = 0
        self.current_missing = set()
        self.thumb_photos = []

    def start(self):
        self.running = True
        self.tick()

    def stop(self):
        self.running = False

    def tick(self):
        if not self.running:
            return
        try:
            rect = get_window_rect(self.title)
            if rect is None:
                self.status.config(text=f"⚠ '{self.title}' 창 없음", fg="orange")
            else:
                full = grab_rect(rect)
                rx, ry, rw, rh = self.roi_rel
                if (rx < 0 or ry < 0
                        or rx + rw > full.shape[1]
                        or ry + rh > full.shape[0]):
                    self.status.config(text="⚠ ROI 범위 초과", fg="orange")
                else:
                    roi_img = full[ry:ry + rh, rx:rx + rw]
                    current = auto_split_icons(roi_img)
                    current_sigs = [icon_signature(img) for _c, _r, img in current]

                    missing_detail = []
                    for name, img, sig in self.saved:
                        if not current_sigs:
                            missing_detail.append((name, img))
                            continue
                        best = max(sig_score(sig, c) for c in current_sigs)
                        if best < MATCH_THRESHOLD:
                            missing_detail.append((name, img))
                    missing_set = frozenset(n for n, _ in missing_detail)

                    # 디바운싱
                    if missing_set == self.pending_missing:
                        self.stable_count += 1
                    else:
                        self.pending_missing = missing_set
                        self.stable_count = 1

                    if self.stable_count >= CONFIRM_TICKS:
                        if missing_set != self.current_missing:
                            self.current_missing = missing_set
                            self.update_display(missing_detail, len(current))
                        else:
                            self.update_timestamp()
                    else:
                        self.update_timestamp()
        except Exception as e:
            self.status.config(text=f"에러: {e}", fg="orange")
        self.root.after(int(WATCH_INTERVAL * 1000), self.tick)

    def update_timestamp(self):
        ts = time.strftime("%H:%M:%S")
        txt = self.status.cget("text").split(" | ")[0]
        self.status.config(text=f"{txt} | {ts}")

    def update_display(self, missing_detail, current_count):
        ts = time.strftime("%H:%M:%S")
        if missing_detail:
            self.status.config(
                text=f"⚠ 빠진 {len(missing_detail)}개 "
                     f"(감지 {current_count}/저장 {len(self.saved)}) | {ts}",
                fg="red")
        else:
            self.status.config(
                text=f"✓ 풀버프 ({current_count}/{len(self.saved)}) | {ts}",
                fg="green")

        for w in self.mframe.winfo_children():
            w.destroy()
        self.thumb_photos = []
        for name, img in missing_detail:
            thumb = cv2.resize(img, (THUMB_SIZE, THUMB_SIZE), interpolation=cv2.INTER_AREA)
            rgb = cv2.cvtColor(thumb, cv2.COLOR_BGR2RGB)
            ph = ImageTk.PhotoImage(Image.fromarray(rgb))
            self.thumb_photos.append(ph)
            lbl = tk.Label(self.mframe, image=ph, bd=1, relief="solid", bg="#2b2b2b")
            lbl.pack(side="left", padx=1, pady=1)
        self.mcount.config(text=f"({len(missing_detail)})")


# --- 메인 UI -----------------------------------------------------------------
def main():
    root = tk.Tk()
    root.title("Buff Watcher v3")
    root.geometry("520x360")
    root.attributes("-topmost", True)
    root.configure(bg="#f5f5f5")

    state = {"window_title": None, "roi_rel": None, "watcher": None}
    cfg = load_config()
    if cfg:
        state["window_title"] = cfg.get("window_title")
        roi = cfg.get("roi_rel")
        if roi:
            state["roi_rel"] = tuple(roi)

    status = tk.Label(root, text="대기 중", fg="gray",
                      font=("Arial", 11), bg="#f5f5f5")
    status.pack(pady=(10, 4))

    info = tk.Label(root, text="", fg="gray",
                    font=("맑은 고딕", 9), justify="left", bg="#f5f5f5")
    info.pack()

    missing_header = tk.Frame(root, bg="#f5f5f5")
    missing_header.pack(pady=(8, 2))
    tk.Label(missing_header, text="빠진 버프:",
             font=("맑은 고딕", 10, "bold"), bg="#f5f5f5").pack(side="left")
    missing_count = tk.Label(missing_header, text="(0)",
                             font=("맑은 고딕", 10), fg="gray", bg="#f5f5f5")
    missing_count.pack(side="left", padx=4)

    missing_frame = tk.Frame(root, bg="#2b2b2b", height=34)
    missing_frame.pack(fill="x", padx=20, pady=(0, 10))
    missing_frame.pack_propagate(False)

    def refresh_info():
        w = state["window_title"] or "(미지정)"
        r = state["roi_rel"] or "(미지정)"
        t = len(list(ICONS_DIR.glob("*.png")))
        info.config(text=f"창: {w}   |   ROI: {r}   |   템플릿: {t}개")
    refresh_info()

    def do_pick_window():
        t = pick_window_dialog(root)
        if t:
            state["window_title"] = t
            save_config(state); refresh_info()
            status.config(text=f"창 연결: {t}", fg="blue")

    def do_pick_roi():
        if not state["window_title"]:
            messagebox.showwarning("안내", "먼저 창을 선택하세요.")
            return
        rect = get_window_rect(state["window_title"])
        if rect is None:
            messagebox.showerror("에러", "창을 찾지 못했어요. 창모드 확인.")
            return
        img = grab_rect(rect)
        roi = pick_roi_on_image(img, root)
        if roi:
            state["roi_rel"] = roi
            save_config(state); refresh_info()
            status.config(text="ROI 지정됨", fg="blue")

    def do_save():
        if not (state["window_title"] and state["roi_rel"]):
            messagebox.showwarning("안내", "창과 ROI를 먼저 지정하세요.")
            return
        rect = get_window_rect(state["window_title"])
        if rect is None:
            messagebox.showerror("에러", "창을 찾지 못했어요.")
            return
        n, saved = save_templates(rect, state["roi_rel"])
        if n == -1:
            messagebox.showerror("에러", "ROI가 창 밖으로 나갔습니다.")
        elif n == 0:
            messagebox.showerror("에러", "아이콘 감지 실패. ROI를 다시 잡아보세요.")
        else:
            final = review_dialog(root, saved)
            if final == -1:
                status.config(text=f"자동감지 {n}개 (리뷰 취소)", fg="orange")
            else:
                status.config(text=f"템플릿 {final}개 확정", fg="green")
            refresh_info()

    def do_watch():
        if state["watcher"] and state["watcher"].running:
            state["watcher"].stop()
            status.config(text="감시 중지", fg="gray")
            watch_btn.config(text="감시 시작")
            return
        if not (state["window_title"] and state["roi_rel"]):
            messagebox.showwarning("안내", "창과 ROI를 지정하세요.")
            return
        tmpl = load_templates()
        if not tmpl:
            messagebox.showwarning("안내", "저장된 템플릿이 없습니다.")
            return
        w = Watcher(root, status, missing_frame, missing_count,
                    state["window_title"], state["roi_rel"], tmpl)
        state["watcher"] = w
        w.start()
        watch_btn.config(text="감시 중지")
        status.config(text="감시 시작", fg="blue")

    btns = tk.Frame(root, bg="#f5f5f5")
    btns.pack(pady=8)
    tk.Button(btns, text="1. 창 선택", width=14, command=do_pick_window)\
        .grid(row=0, column=0, padx=3, pady=3)
    tk.Button(btns, text="2. ROI 선택", width=14, command=do_pick_roi)\
        .grid(row=0, column=1, padx=3, pady=3)
    tk.Button(btns, text="3. 풀버프 저장", width=14, command=do_save)\
        .grid(row=0, column=2, padx=3, pady=3)
    watch_btn = tk.Button(btns, text="감시 시작", width=46, command=do_watch)
    watch_btn.grid(row=1, column=0, columnspan=3, padx=3, pady=6)

    root.mainloop()


if __name__ == "__main__":
    main()
