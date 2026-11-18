# -*- coding: utf-8 -*-
"""PA Bench Monitor 설정 · 기록 읽기 (읽기만, 아무것도 바꾸지 않음)

- 케이블 손실: Bench 설정의 손실 표 (주파수별 In / Out) 또는 고정값
- 샘플 종류 · 연결된 .pam (가장 최근 날짜_종류.pam) 의 측정 기록 → SG 레벨 ↔ DUT Pout 곡선
"""
import glob
import json
import math
import os
import re

BENCH_SET = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PABenchMonitor", "settings.json")
BENCH_LIVE = os.path.join(os.path.dirname(BENCH_SET), "live.json")


DPD_BUSY = os.path.join(os.path.dirname(BENCH_SET), "dpd_busy.json")
BENCH_ACK = os.path.join(os.path.dirname(BENCH_SET), "bench_ack.json")


def set_busy(on):
    """DPD 측정 중 표시 (PA Bench Monitor v2.3.24+ 는 이게 2초 안에 갱신되는 동안 SMW · FSW 읽기를 멈춤)"""
    import time
    try:
        if on:
            os.makedirs(os.path.dirname(DPD_BUSY), exist_ok=True)
            tmp = DPD_BUSY + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"t": time.time(), "pid": os.getpid()}, f)
            os.replace(tmp, DPD_BUSY)
        elif os.path.exists(DPD_BUSY):
            os.remove(DPD_BUSY)
    except Exception:
        pass


def read_ack(max_age=2.0):
    """PA Bench Monitor 의 SMW · FSW 연결 · 멈춤 상태 → dict 또는 None (Bench 가 안 켜져 있으면)"""
    import time
    try:
        with open(BENCH_ACK, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if time.time() - float(d.get("t") or 0) > max_age:
        return None
    return d


def read_live(max_age=3.0):
    """PA Bench Monitor 가 0.5초마다 남기는 Vdd 전원 값 (켜져 있을 때만) → dict(t, vdd, id, ...) 또는 None"""
    import time
    try:
        with open(BENCH_LIVE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if time.time() - float(d.get("t") or 0) > max_age:
        return None
    return d


def fnum(s, d=float("nan")):
    try:
        return float(str(s).strip())
    except (ValueError, TypeError):
        return d


def load_settings(path=BENCH_SET):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def losses_at(s, freq_ghz):
    """(In loss, Out loss, 설명). Bench 와 같은 규칙: 표에서 가장 가까운 주파수 행, 표가 없으면 고정값"""
    if not s:
        return None
    tbl = [r for r in (s.get("loss_table") or []) if len(r) >= 3]
    if tbl and freq_ghz == freq_ghz:
        r = min(tbl, key=lambda r: abs(float(r[0]) - freq_ghz))
        return float(r[1]), float(r[2]), "Bench 손실 표 %.6g GHz 행" % float(r[0])
    return fnum(s.get("in_loss"), 0.0), fnum(s.get("out_loss"), 0.0), "Bench 고정값"


def sample_types(s):
    return [t.get("name", "") for t in (s or {}).get("sample_types", []) if t.get("name")]


def latest_pam(s, stype):
    """Bench 와 같은 규칙: 연결된 .pam 과 같은 폴더의 날짜_종류.pam 중 가장 최근에 저장된 것"""
    t = next((t for t in (s or {}).get("sample_types", []) if t.get("name") == stype), None)
    cur = (t or {}).get("pam") or ""
    folder = os.path.dirname(cur) if cur else ((s or {}).get("pam_dir") or
                                               os.path.join(os.path.expanduser("~"), "Documents", "PA-Bench-pam"))
    safe = "".join(ch if ch not in '\\/:*?"<>|' else "_" for ch in (stype or "")).strip() or "샘플"
    cands = [cur] if (cur and os.path.isfile(cur)) else []
    try:
        pat = re.compile(r"^\d{8}_%s\.pam$" % re.escape(safe), re.IGNORECASE)
        cands += [f for f in glob.glob(os.path.join(glob.escape(folder), "*.pam")) if pat.match(os.path.basename(f))]
    except (OSError, re.error):
        pass
    cands = [c for c in cands if os.path.isfile(c)]
    return max(cands, key=os.path.getmtime) if cands else None


def read_records(pam_path):
    """.pam 의 데이터셋 → [dict(name, display, mod, freq_ghz, sg[], pout[], inl, outl)]"""
    with open(pam_path, encoding="utf-8") as f:
        doc = json.load(f)
    out = []
    for sh in doc.get("sheets", []):
        rows = sh.get("rows") or []
        fq = inl = outl = float("nan")
        hi = None
        for i, r in enumerate(rows):
            if not r:
                continue
            k = str(r[0]).strip().upper()
            if k == "FREQ" and len(r) > 1:
                m = re.search(r"([\d.]+)\s*(GHZ|MHZ)?", str(r[1]).upper())
                if m:
                    fq = float(m.group(1)) / (1000.0 if m.group(2) == "MHZ" else 1.0)
            elif k == "INPUT LOSS" and len(r) > 1:
                inl = fnum(r[1])
            elif k == "OUTPUT LOSS" and len(r) > 1:
                outl = fnum(r[1])
            heads = [str(c).upper() for c in r]
            if any(h.startswith("SG POUT") for h in heads) and any(h.startswith("DUT_POUT") for h in heads):
                hi = i
                break
        if hi is None:
            continue
        heads = [str(c).upper() for c in rows[hi]]
        isg = next(j for j, h in enumerate(heads) if h.startswith("SG POUT"))
        ipo = next(j for j, h in enumerate(heads) if h.startswith("DUT_POUT"))
        sg, po = [], []
        for r in rows[hi + 1:]:
            if len(r) > max(isg, ipo):
                a, b = fnum(r[isg]), fnum(r[ipo])
                if math.isfinite(a) and math.isfinite(b):
                    sg.append(a)
                    po.append(b)
        if len(sg) < 2:
            continue
        name = sh.get("name") or ""
        out.append(dict(name=name, display=sh.get("display") or name, mod=(" Mod " in " %s " % name) or " Mod" in name,
                        freq_ghz=fq, sg=sg, pout=po, inl=inl, outl=outl,
                        when=sh.get("modified_at") or sh.get("added_at") or ""))
    return out


def interp(xs, ys, x):
    """정렬된 xs 위 선형 보간 (밖이면 양 끝 두 점으로 연장)"""
    pts = sorted(zip(xs, ys))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if len(xs) < 2:
        return float("nan")
    if x <= xs[0]:
        i = 0
    elif x >= xs[-1]:
        i = len(xs) - 2
    else:
        i = max(j for j in range(len(xs) - 1) if xs[j] <= x)
    x0, x1, y0, y1 = xs[i], xs[i + 1], ys[i], ys[i + 1]
    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def pout_at_sg(rec, sg):
    return interp(rec["sg"], rec["pout"], sg)


def sg_for_pout(rec, pout):
    """목표 Pout 에 필요한 SG 레벨 (Pout 이 계속 올라가는 구간만 써서 역보간)"""
    pts = sorted(zip(rec["sg"], rec["pout"]))
    mono = [pts[0]]
    for p in pts[1:]:
        if p[1] > mono[-1][1]:
            mono.append(p)
    return interp([p[1] for p in mono], [p[0] for p in mono], pout)
