"""
Buff Watcher v3 — 자동 아이콘 감지 + 강건한 매칭
================================================
- 창 선택 → ROI 지정 (한 번)
- ROI 내부에서 아이콘을 '자동 분리' (행 분산 기반)
- 매칭은 HSV 색상 히스토그램 — 쿨타임 어둠/배경 변화에 강건
- 빠진 버프는 메인 창 안에 32x32 썸네일로 인라인 표시
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

FALLBACK_ICON_SIZE = 32
MATCH_THRESHOLD = 0.70
WATCH_INTERVAL = 0.5
CONFIRM_TICKS = 2
THUMB_SIZE = 32
AUTO_GAME_KEYWORDS = ["ragnarok"]  # 자동 탐색할 게임 창 키워드


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


def auto_find_game_window():
    """AUTO_GAME_KEYWORDS에 매칭되는 창을 자동 탐색. 없으면 None."""
    for w in gw.getAllWindows():
        try:
            if not w.title or not w.visible:
                continue
            if w.width < 100 or w.height < 100:
                continue
            title_low = w.title.lower()
            for kw in AUTO_GAME_KEYWORDS:
                if kw in title_low:
                    return w.title
        except Exception:
            continue
    return None


def auto_detect_buff_roi(bgr_img):
    """게임 스크린샷 우측에서 버프 바를 자동 감지.

    방법: 우측 가장자리의 좁은 세로 스트립에서 행별 std 프로파일의
    자기상관(autocorrelation)을 계산하여 '일정 간격으로 반복되는
    아이콘 격자' 패턴을 탐지한다.
    나무/돌 등 게임 텍스처는 이런 규칙적 주기가 없어 구분됨.

    반환: (x, y, w, h) 창 내부 상대좌표 또는 None
    """
    h, w = bgr_img.shape[:2]

    # 우측 가장자리만 스캔 (RO 버프 바는 맨 오른쪽에 위치)
    scan_w = min(100, max(50, int(w * 0.10)))
    right = bgr_img[:, w - scan_w:]
    gray = cv2.cvtColor(right, cv2.COLOR_BGR2GRAY)

    bs = FALLBACK_ICON_SIZE  # 32
    best_x, best_score, best_strip_w = -1, 0.0, bs

    # 1열(bs) 및 2열(bs*2+gap) 스트립을 슬라이딩하며 주기 패턴 탐색
    for strip_w in [bs, bs * 2 + 2, bs * 2 + 4, bs * 2]:
        if strip_w > scan_w:
            continue
        for x in range(max(0, scan_w - strip_w - 10),
                       min(scan_w - strip_w + 1, scan_w), 2):
            strip = gray[:, x:x + strip_w]
            # 행별 표준편차 = 세로 컨텐츠 프로파일
            prof = strip.std(axis=1).astype(np.float64)
            centered = prof - prof.mean()
            norm = np.sqrt(np.sum(centered ** 2))
            if norm < 1e-6:
                continue
            normed = centered / norm

            # 예상 아이콘 피치(24~32px)에서 자기상관 최대값 탐색
            for lag in range(bs - 2, bs + 10):
                n = len(normed) - lag
                if n < lag * 3:      # 최소 3주기 필요
                    continue
                corr = float(np.dot(normed[:n], normed[lag:lag + n]))
                # 주기 수에 비례한 가중 (긴 버프 바 우선)
                n_periods = n / lag
                score = corr * min(n_periods, 10) / 10
                if score > best_score:
                    best_score = score
                    best_x = x
                    best_strip_w = strip_w

    if best_score < 0.15 or best_x < 0:
        return None

    # 주기 패턴이 있는 세로 구간 찾기
    strip = gray[:, best_x:best_x + best_strip_w]
    prof = strip.std(axis=1)
    content_thresh = max(12, prof.mean() * 0.8)
    is_content = prof > content_thresh

    # 연속 구간 찾기
    runs = []
    in_run, start = False, 0
    for y in range(len(is_content)):
        if is_content[y] and not in_run:
            start = y; in_run = True
        elif not is_content[y] and in_run:
            runs.append((start, y)); in_run = False
    if in_run:
        runs.append((start, len(is_content)))

    if not runs:
        return None

    # 가까운 구간 병합 (아이콘 간 갭은 짧으므로)
    merged = [list(runs[0])]
    for s, e in runs[1:]:
        if s - merged[-1][1] <= bs:
            merged[-1][1] = e
        else:
            merged.append([s, e])

    # 가장 긴 구간 선택
    best_run = max(merged, key=lambda b: b[1] - b[0])
    y_start, y_end = best_run

    if (y_end - y_start) < bs * 2:
        return None

    roi_x = w - scan_w + best_x
    roi_y = y_start
    roi_w = best_strip_w
    roi_h = y_end - y_start

    # 세로가 가로보다 길어야 버프 바 (미니맵 등 제외)
    if roi_h < roi_w * 1.5:
        return None

    # 정밀 보정: ROI 내부에서 실제 컨텐츠 경계 재탐색
    rough = bgr_img[roi_y:roi_y + roi_h, roi_x:roi_x + roi_w]
    g = cv2.cvtColor(rough, cv2.COLOR_BGR2GRAY)
    row_blocks = _find_content_blocks(g.std(axis=1), min_length=8)
    col_blocks = _find_content_blocks(g.std(axis=0), min_length=8)
    if row_blocks and col_blocks:
        ry1 = row_blocks[0][0]
        ry2 = row_blocks[-1][0] + row_blocks[-1][1]
        rx1 = col_blocks[0][0]
        rx2 = col_blocks[-1][0] + col_blocks[-1][1]
        roi_x += rx1
        roi_y += ry1
        roi_w = rx2 - rx1
        roi_h = ry2 - ry1

    # 최종 검증: 아이콘이 3개 이상 분리되는지 확인
    final_roi = bgr_img[roi_y:roi_y + roi_h, roi_x:roi_x + roi_w]
    icons = auto_split_icons(final_roi)
    if len(icons) < 3:
        return None

    return (roi_x, roi_y, roi_w, roi_h)


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


# --- 자동 아이콘 분리 (흰색 테두리 + NMS) ------------------------------------
def _find_content_blocks(std_signal, min_length=8):
    """1D 표준편차 신호에서 컨텐츠 구간(시작, 길이)을 찾는다."""
    if std_signal.max() == 0:
        return []
    norm = std_signal / std_signal.max()
    threshold = max(0.15, norm.mean() * 0.4)
    is_content = norm > threshold

    blocks = []
    in_blk = False
    start = 0
    for i in range(len(std_signal)):
        if is_content[i] and not in_blk:
            start = i
            in_blk = True
        elif not is_content[i] and in_blk:
            if i - start >= min_length:
                blocks.append((start, i - start))
            in_blk = False
    if in_blk and len(std_signal) - start >= min_length:
        blocks.append((start, len(std_signal) - start))
    return blocks


def _estimate_grid_params(std_signal, expected_size=None, min_length=8):
    """컨텐츠 구간에서 셀 크기, 스텝(피치), 시작 오프셋을 추정.

    반환: (cell_size, step, offset) 또는 (None, None, 0)
    - cell_size: 아이콘 픽셀 크기
    - step: 아이콘 시작점 간 거리 (아이콘 + 간격)
    - offset: 첫 번째 아이콘 시작 위치

    expected_size: 이미 알려진 아이콘 크기. 가로 분석 시 세로에서 구한
                   크기를 넘기면 합쳐진 블록을 세분화할 수 있다.
    """
    blocks = _find_content_blocks(std_signal, min_length)
    if not blocks:
        return None, None, 0

    cell_size = int(np.median([length for _, length in blocks]))

    # 이미 알려진 크기보다 블록이 훨씬 크면 → 간격이 좁아 합쳐진 것, 세분화
    if expected_size and expected_size >= 12 and cell_size > expected_size * 1.3:
        refined = []
        for bstart, blength in blocks:
            n = max(1, round(blength / expected_size))
            if n == 1:
                refined.append((bstart, blength))
            else:
                # 블록 내부 std 골(valley)을 찾아 정확한 분리점 결정
                seg = std_signal[bstart:bstart + blength]
                sub_pitch = blength / n
                splits = [0]
                for k in range(1, n):
                    approx = int(round(k * sub_pitch))
                    lo = max(0, approx - int(sub_pitch * 0.25))
                    hi = min(blength, approx + int(sub_pitch * 0.25))
                    if lo < hi and hi <= len(seg):
                        valley = lo + int(np.argmin(seg[lo:hi]))
                        splits.append(valley)
                    else:
                        splits.append(approx)
                splits.append(blength)
                for k in range(len(splits) - 1):
                    s = bstart + splits[k]
                    l = splits[k + 1] - splits[k]
                    if l >= min_length:
                        refined.append((s, l))
        blocks = refined
        cell_size = expected_size

    use_size = expected_size if expected_size and expected_size >= 12 else cell_size

    # 블록 중심 기반으로 아이콘 시작 위치 계산 (경계 감지 오차 보정)
    starts = []
    for bstart, blength in blocks:
        center = bstart + blength // 2
        icon_start = center - use_size // 2
        starts.append(max(0, icon_start))

    offset = starts[0] if starts else 0

    if len(starts) >= 2:
        pitches = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
        step = int(np.median(pitches))
        step = max(step, use_size)
    else:
        step = use_size

    return cell_size, step, offset


def _assign_grid(positions, roi_img, bs):
    """감지된 위치를 열/행으로 분류하고 아이콘 이미지 추출."""
    positions.sort(key=lambda s: s[0])
    columns = [[positions[0]]]
    for item in positions[1:]:
        if abs(item[0] - columns[-1][-1][0]) < bs * 0.5:
            columns[-1].append(item)
        else:
            columns.append([item])

    icons = []
    for c_idx, col in enumerate(columns):
        col.sort(key=lambda item: item[1])
        for r_idx, (x, y, _) in enumerate(col):
            icon = roi_img[y:y + bs, x:x + bs]
            icons.append((c_idx, r_idx, icon))
    return icons


def _detect_pitch_autocorr(signal, min_lag, max_lag):
    """1D 신호의 자기상관으로 주기(피치) 감지.

    반환: 감지된 피치 (정수) 또는 None
    """
    signal = np.asarray(signal, dtype=np.float64)
    centered = signal - signal.mean()
    norm_val = np.sqrt(np.sum(centered ** 2))
    if norm_val < 1e-6:
        return None
    normed = centered / norm_val

    best_lag, best_corr = None, 0.2
    for lag in range(max(1, min_lag), min(max_lag + 1, len(normed) // 2)):
        n = len(normed) - lag
        if n < lag:
            continue
        corr = float(np.dot(normed[:n], normed[lag:lag + n]))
        if corr > best_corr:
            best_corr = corr
            best_lag = lag
    return best_lag


def _find_grid_offset(bright_frac, pitch, cell_size, length):
    """격자 시작 오프셋을 결정.

    각 격자 셀의 첫/마지막 행(또는 열)이 밝은(테두리) 위치에
    오도록 오프셋을 최적화한다.

    반환: (offset, score)
    """
    best_off, best_score = 0, -1.0
    search_range = min(pitch, max(1, length - cell_size + 1))
    for off in range(search_range):
        score = 0.0
        count = 0
        pos = off
        while pos + cell_size <= length:
            score += bright_frac[pos]
            score += bright_frac[pos + cell_size - 1]
            count += 2
            pos += pitch
        if count >= 2:
            avg = score / count
            if avg > best_score:
                best_score = avg
                best_off = off
    return best_off, best_score


def _grid_split(roi_img, gray, bs, empty_std_threshold):
    """격자 구조 감지 기반 아이콘 분리.

    1) 세로/가로 std 프로파일의 자기상관으로 피치(아이콘 간격) 감지
    2) 흰색 테두리 밝기를 기반으로 격자 시작 오프셋 결정
    3) 피치 미세 보정 (±2 px)
    4) 격자 위치에서 유효한 아이콘만 추출

    반환: [(col, row, bgr_img), ...]
    """
    H, W = gray.shape

    # --- 1) 피치 감지 (자기상관) ---
    row_std = gray.std(axis=1)
    y_pitch = _detect_pitch_autocorr(row_std, bs - 10, bs + 12)
    if y_pitch is None:
        y_pitch = bs

    if W >= bs * 1.5:
        col_std = gray.std(axis=0)
        x_pitch = _detect_pitch_autocorr(col_std, bs - 10, bs + 12)
        if x_pitch is None:
            x_pitch = bs
    else:
        x_pitch = bs

    # 추출 크기 (피치 이하로 — 겹침 방지)
    cell_h = min(bs, y_pitch)
    cell_w = min(bs, x_pitch)

    # --- 2) 밝은 테두리 기반 오프셋 ---
    bright = (gray > 180).astype(np.float64)
    row_bright = bright.mean(axis=1)
    col_bright = bright.mean(axis=0)

    y_off, y_score = _find_grid_offset(row_bright, y_pitch, cell_h, H)
    x_off, x_score = _find_grid_offset(col_bright, x_pitch, cell_w, W)

    if y_score < 0.15:
        return []

    # --- 3) 피치 미세 보정 (±2) ---
    for dp in range(-2, 3):
        tp = y_pitch + dp
        if tp < bs - 10 or tp > bs + 12 or tp < 12:
            continue
        ch = min(bs, tp)
        off, sc = _find_grid_offset(row_bright, tp, ch, H)
        if sc > y_score:
            y_pitch, y_off, y_score, cell_h = tp, off, sc, ch

    for dp in range(-2, 3):
        tp = x_pitch + dp
        if tp < bs - 10 or tp > bs + 12 or tp < 12:
            continue
        cw = min(bs, tp)
        off, sc = _find_grid_offset(col_bright, tp, cw, W)
        if sc > x_score:
            x_pitch, x_off, x_score, cell_w = tp, off, sc, cw

    # --- 4) 격자에서 아이콘 추출 ---
    margin = min(2, cell_h // 4, cell_w // 4)

    icons = []
    col_idx = 0
    x = x_off
    while x + cell_w <= W:
        row_idx = 0
        y = y_off
        while y + cell_h <= H:
            patch = gray[y:y + cell_h, x:x + cell_w]
            inner = patch[margin:-margin, margin:-margin] if margin > 0 else patch
            if inner.std() > empty_std_threshold:
                icon = roi_img[y:y + cell_h, x:x + cell_w]
                icons.append((col_idx, row_idx, icon))
            row_idx += 1
            y += y_pitch
        col_idx += 1
        x += x_pitch

    return icons


def auto_split_icons(roi_img, empty_std_threshold=15.0):
    """ROI 안에서 버프 아이콘을 개별 감지.

    방법 0 — 격자 기반: 자기상관으로 피치 감지 + 테두리 오프셋 정렬.
    방법 1 — 흰색 테두리(32×32) 박스를 앵커로 정확한 위치 결정.
    방법 2 (폴백) — 슬라이딩 윈도우 + NMS 스코어링.

    반환: [(col, row, bgr_img), ...]"""
    if roi_img is None or roi_img.size == 0:
        return []
    gray = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY)
    H, W = gray.shape
    bs = FALLBACK_ICON_SIZE  # 32

    if H < bs or W < bs:
        return []

    # ===== 방법 0: 격자 기반 (자기상관 + 테두리 오프셋) =====
    grid_icons = _grid_split(roi_img, gray, bs, empty_std_threshold)
    if len(grid_icons) >= 3:
        return grid_icons

    # ===== 방법 1: 흰색 테두리 감지 =====
    # 32×32 박스의 외곽 1px 링을 마스크로 사용
    border_mask = np.zeros((bs, bs), dtype=bool)
    border_mask[0, :] = True    # 상단
    border_mask[-1, :] = True   # 하단
    border_mask[:, 0] = True    # 좌측
    border_mask[:, -1] = True   # 우측
    n_border = int(border_mask.sum())

    bright_thresh = 180
    step = max(2, bs // 8)  # 4px
    border_cands = []

    for y in range(0, H - bs + 1, step):
        for x in range(0, W - bs + 1, step):
            patch = gray[y:y + bs, x:x + bs]
            ratio = float((patch[border_mask] > bright_thresh).sum()) / n_border
            if ratio > 0.5:
                inner = patch[2:-2, 2:-2]
                if inner.std() > empty_std_threshold:
                    border_cands.append((x, y, ratio))

    if len(border_cands) >= 3:
        # NMS
        border_cands.sort(key=lambda c: -c[2])
        sel = []
        for x, y, sc in border_cands:
            if not any(abs(x - sx) < bs * 0.6 and abs(y - sy) < bs * 0.6
                       for sx, sy, _ in sel):
                sel.append((x, y, sc))

        # 미세 보정 ±3px (테두리 점수 최대화)
        ref = []
        for x, y, sc in sel:
            bx, by, bsc = x, y, sc
            for dy in range(-3, 4):
                for dx in range(-3, 4):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx <= W - bs and 0 <= ny <= H - bs:
                        p = gray[ny:ny + bs, nx:nx + bs]
                        r = float((p[border_mask] > bright_thresh).sum()) / n_border
                        if r > bsc:
                            bx, by, bsc = nx, ny, r
            ref.append((bx, by, bsc))

        if len(ref) >= 3:
            return _assign_grid(ref, roi_img, bs)

    # ===== 방법 2: NMS 스코어링 (폴백) =====
    hsv = cv2.cvtColor(roi_img, cv2.COLOR_BGR2HSV)
    step2 = max(2, bs // 6)
    nms_cands = []

    for y in range(0, H - bs + 1, step2):
        for x in range(0, W - bs + 1, step2):
            pg = gray[y:y + bs, x:x + bs]
            var = pg.std()
            if var < empty_std_threshold:
                continue
            sat = float(hsv[y:y + bs, x:x + bs, 1].mean())
            edges = cv2.Canny(pg, 40, 120)
            edge_r = float(edges.sum()) / (bs * bs * 255)
            score = (min(var / 50, 1) * 0.3
                     + min(sat / 60, 1) * 0.4
                     + min(edge_r / 0.12, 1) * 0.3)
            if score > 0.3:
                nms_cands.append((x, y, score))

    if not nms_cands:
        return []

    nms_cands.sort(key=lambda c: -c[2])
    sel2 = []
    for x, y, sc in nms_cands:
        if not any(abs(x - sx) < bs * 0.6 and abs(y - sy) < bs * 0.6
                   for sx, sy, _ in sel2):
            sel2.append((x, y, sc))

    if not sel2:
        return []

    ref2 = []
    for x, y, sc in sel2:
        bx, by, bsc = x, y, sc
        for dy in range(-step2, step2 + 1):
            for dx in range(-step2, step2 + 1):
                nx, ny = x + dx, y + dy
                if 0 <= nx <= W - bs and 0 <= ny <= H - bs:
                    pg = gray[ny:ny + bs, nx:nx + bs]
                    var = pg.std()
                    if var < empty_std_threshold:
                        continue
                    sat = float(hsv[ny:ny + bs, nx:nx + bs, 1].mean())
                    edges = cv2.Canny(pg, 40, 120)
                    er = float(edges.sum()) / (bs * bs * 255)
                    s2 = (min(var / 50, 1) * 0.3
                          + min(sat / 60, 1) * 0.4
                          + min(er / 0.12, 1) * 0.3)
                    if s2 > bsc:
                        bx, by, bsc = nx, ny, s2
        ref2.append((bx, by, bsc))

    return _assign_grid(ref2, roi_img, bs)


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

    # 아이콘 표시 크기 결정 (최소 48px로 확대)
    display_px = 56
    grid_cols = min(10, len(saved_icons))
    cell_px = display_px + 14  # border + padding
    need_w = max(500, grid_cols * cell_px + 80)
    need_w = min(need_w, parent.winfo_screenwidth() - 100)
    win.geometry(f"{need_w}x600")

    tk.Label(win,
             text=(f"총 {len(saved_icons)}개 감지됨. "
                   "잘못 잡힌 항목은 클릭(빨강)해서 제외하세요."),
             fg="gray", wraplength=need_w - 40, justify="left").pack(pady=6, padx=10, anchor="w")

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
    cols = grid_cols
    for i, (name, img) in enumerate(saved_icons):
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        k = max(1, display_px // max(rgb.shape[0], rgb.shape[1], 1))
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
        self.last_positions = {}  # name → (x, y) 이전 매칭 위치 캐시
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
                    h_roi, w_roi = roi_img.shape[:2]
                    icon_size = self.saved[0][1].shape[0] if self.saved \
                        else FALLBACK_ICON_SIZE

                    # --- 슬라이딩 윈도우 매칭 (위치 캐싱 최적화) ---
                    missing_detail = []

                    if h_roi < icon_size or w_roi < icon_size:
                        missing_detail = [(n, i) for n, i, _ in self.saved]
                    else:
                        m = max(1, icon_size // 5)
                        coarse = max(2, icon_size // 3)  # 전체 스캔용
                        fine = 2                          # 캐시 근처 탐색용

                        def _match(y, x, sig):
                            """인라인 히스토그램 비교 (함수 호출 오버헤드 제거)."""
                            core = roi_img[y + m:y + icon_size - m,
                                           x + m:x + icon_size - m]
                            if core.size == 0:
                                return 0.0
                            cr = cv2.resize(core, (16, 16),
                                            interpolation=cv2.INTER_AREA)
                            hsv = cv2.cvtColor(cr, cv2.COLOR_BGR2HSV)
                            h = cv2.calcHist([hsv], [0, 1], None,
                                             [12, 12], [0, 180, 0, 256])
                            cv2.normalize(h, h, 0, 1, cv2.NORM_MINMAX)
                            return float(cv2.compareHist(
                                sig, h.astype(np.float32),
                                cv2.HISTCMP_CORREL))

                        for name, img, sig in self.saved:
                            found = False

                            # 1) 이전 위치 근처 먼저 확인 (대부분 여기서 끝남)
                            if name in self.last_positions:
                                lx, ly = self.last_positions[name]
                                for dy in range(-fine * 2, fine * 2 + 1, fine):
                                    for dx in range(-fine * 2, fine * 2 + 1, fine):
                                        ny, nx = ly + dy, lx + dx
                                        if (0 <= ny <= h_roi - icon_size and
                                                0 <= nx <= w_roi - icon_size):
                                            if _match(ny, nx, sig) >= MATCH_THRESHOLD:
                                                self.last_positions[name] = (nx, ny)
                                                found = True
                                                break
                                    if found:
                                        break

                            # 2) 못 찾으면 전체 ROI 스캔 (큰 스텝)
                            if not found:
                                for y in range(0, h_roi - icon_size + 1, coarse):
                                    for x in range(0, w_roi - icon_size + 1, coarse):
                                        if _match(y, x, sig) >= MATCH_THRESHOLD:
                                            self.last_positions[name] = (x, y)
                                            found = True
                                            break
                                    if found:
                                        break

                            if not found:
                                self.last_positions.pop(name, None)
                                missing_detail.append((name, img))

                    present_count = len(self.saved) - len(missing_detail)
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
                            self.update_display(missing_detail, present_count)
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

    missing_frame = tk.Frame(root, bg="#2b2b2b", height=42)
    missing_frame.pack(fill="x", padx=20, pady=(0, 10))
    missing_frame.pack_propagate(False)

    def refresh_info():
        w = state["window_title"] or "(미지정)"
        r = state["roi_rel"] or "(미지정)"
        t = len(list(ICONS_DIR.glob("*.png")))
        info.config(text=f"창: {w}   |   ROI: {r}   |   템플릿: {t}개")
    refresh_info()

    # --- 시작 시 자동 탐색 ---------------------------------------------------
    if not state["window_title"]:
        auto_title = auto_find_game_window()
        if auto_title:
            state["window_title"] = auto_title
            save_config(state); refresh_info()
            status.config(text=f"자동 감지: {auto_title}", fg="blue")

    if state["window_title"] and not state["roi_rel"]:
        rect = get_window_rect(state["window_title"])
        if rect:
            img = grab_rect(rect)
            roi = auto_detect_buff_roi(img)
            if roi:
                state["roi_rel"] = roi
                save_config(state); refresh_info()
                status.config(text=f"창+ROI 자동 감지 완료", fg="blue")

    def do_pick_window():
        # 자동 탐색 시도 → 실패 시 수동 선택
        t = auto_find_game_window()
        if t:
            state["window_title"] = t
            save_config(state); refresh_info()
            status.config(text=f"자동 감지: {t}", fg="blue")
        else:
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
        # 자동 감지 시도 → 실패 시 수동 드래그
        roi = auto_detect_buff_roi(img)
        if roi:
            state["roi_rel"] = roi
            save_config(state); refresh_info()
            status.config(text=f"ROI 자동 감지됨 {roi}", fg="blue")
        else:
            roi = pick_roi_on_image(img, root)
            if roi:
                state["roi_rel"] = roi
                save_config(state); refresh_info()
                status.config(text="ROI 수동 지정됨", fg="blue")

    def do_save():
        # --- 원클릭 흐름: 창 찾기 → ROI 감지 → 아이콘 분리 전부 자동 ---

        # 1) 창 자동 탐색
        if not state["window_title"]:
            t = auto_find_game_window()
            if t:
                state["window_title"] = t
                save_config(state); refresh_info()
            else:
                t = pick_window_dialog(root)
                if not t:
                    return
                state["window_title"] = t
                save_config(state); refresh_info()

        rect = get_window_rect(state["window_title"])
        if rect is None:
            messagebox.showerror("에러", "창을 찾지 못했어요.")
            return

        full = grab_rect(rect)

        # 2) ROI 자동 감지
        if not state["roi_rel"]:
            roi = auto_detect_buff_roi(full)
            if roi:
                state["roi_rel"] = roi
                save_config(state); refresh_info()
            else:
                # ROI 자동 실패 → 수동 드래그
                roi = pick_roi_on_image(full, root)
                if not roi:
                    return
                state["roi_rel"] = roi
                save_config(state); refresh_info()

        # 3) 템플릿 저장 (auto_split_icons가 NMS 기반으로 개별 감지)
        n, saved = save_templates(rect, state["roi_rel"])
        if n == -1:
            messagebox.showerror("에러", "ROI가 창 밖으로 나갔습니다.")
        elif n == 0:
            messagebox.showerror("에러",
                                 "아이콘 감지 실패.\n"
                                 "ROI를 수동으로 다시 선택하세요.")
            state["roi_rel"] = None
            save_config(state); refresh_info()
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
