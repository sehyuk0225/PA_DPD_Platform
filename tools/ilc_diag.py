# -*- coding: utf-8 -*-
"""
ILC 결과 (.npz) 진단 - 모델이 ILC 입력을 왜 못 따라가는지 숫자로만 보여 줌 (파형 데이터는 출력 안 함)

쓰는 법 (명령 프롬프트):
    python ilc_diag.py                      -> %APPDATA%\\PADPDTester\\ilc 의 가장 최근 .npz
    python ilc_diag.py "C:\\...\\ILC_xxx.npz"
    python ilc_diag.py 파일.npz --adj 200    -> 인접 채널 폭 (MHz, 기본: 신호 점유 폭의 절반)

필요: Python 3 + numpy (PA Bench Monitor 를 빌드 없이 돌리는 PC 면 이미 있음)
결과 화면 (숫자 표) 만 복사해서 공유하면 됨. 파일 자체는 밖으로 안 나감.
"""
import glob
import json
import os
import sys
import time

import numpy as np


# ------------------------------------------------------------------ 파일
def pick_file(argv):
    for a in argv[1:]:
        if a.lower().endswith(".npz"):
            return a
    d = os.path.join(os.environ.get("APPDATA", ""), "PADPDTester", "ilc")
    fs_ = sorted(glob.glob(os.path.join(d, "*.npz")), key=os.path.getmtime)
    if not fs_:
        sys.exit("ILC .npz 파일을 못 찾음 - 경로를 직접 주세요: python ilc_diag.py 파일.npz")
    return fs_[-1]


def arg_val(argv, key, default=None):
    if key in argv:
        i = argv.index(key)
        if i + 1 < len(argv):
            return float(argv[i + 1])
    return default


# ------------------------------------------------------------------ 기저 (GMP + 켤레)
def terms(K, M, L, conj=False, odd=False, mneg=0, fir=0):
    """mneg: 앞 샘플 (x(n+1) …) 메모리 수 · fir: 선형 x(n-d) 를 ±fir 탭까지 (메모리 밖만)"""
    ks = [k for k in range(1, K + 1) if (k % 2 == 1 or not odd)]
    ms = list(range(-mneg, M))
    t = [("mp", k, m, 0) for k in ks for m in ms]
    t += [("mp", 1, d, 0) for d in range(-fir, fir + 1) if d not in ms]
    for m in range(M):
        for l in list(range(1, L + 1)) + list(range(-L, 0)):
            t += [("x", k, m, l) for k in ks if k > 1]
    if conj:                                     # I/Q 불균형 · 이미지: conj(x(n-m)) |x(n-m)|^(k-1)
        t += [("cj", k, m, 0) for k in ks for m in range(M)]
    return t


def col(s, a, term):
    kind, k, m, l = term
    sm = np.roll(s, m)
    if kind == "mp":
        return sm * np.roll(a, m) ** (k - 1)
    if kind == "x":
        return sm * np.roll(a, m + l) ** (k - 1)
    return np.conj(sm) * np.roll(a, m) ** (k - 1)


def fit_apply(x, u, tlist, i0, nfit, lam=1e-6):
    """x -> u 를 tlist 기저로 학습 (x[i0:i0+nfit]) → 전체 x 에 적용한 출력"""
    a = np.abs(x)
    P = len(tlist)
    A = np.empty((nfit, P), complex)
    for j, t in enumerate(tlist):
        A[:, j] = col(x, a, t)[i0:i0 + nfit]
    sc = np.sqrt(np.mean(np.abs(A) ** 2, axis=0)) + 1e-30
    A /= sc
    R = A.conj().T @ A
    R += lam * np.trace(R).real / P * np.eye(P)
    w = np.linalg.solve(R, A.conj().T @ u[i0:i0 + nfit]) / sc
    out = np.zeros(len(x), complex)
    for j, t in enumerate(tlist):              # 열마다 더함 (메모리 적게)
        out += w[j] * col(x, a, t)
    return out


# ------------------------------------------------------------------ 스펙트럼
def psd(s, fs, nseg=4096):
    n = len(s) // nseg * nseg
    win = np.hanning(nseg)
    seg = s[:n].reshape(-1, nseg) * win
    P = np.mean(np.abs(np.fft.fftshift(np.fft.fft(seg, axis=1), axes=1)) ** 2, axis=0)
    f = np.fft.fftshift(np.fft.fftfreq(nseg, 1 / fs))
    return f, P


def bandp(f, P, lo, hi):
    m = (f >= lo) & (f < hi)
    return float(P[m].sum()) if m.any() else float("nan")


def db(v):
    return 10 * np.log10(max(v, 1e-30))


def main():
    path = pick_file(sys.argv)
    d = np.load(path, allow_pickle=False)
    u = d["u"].astype(complex)
    x = d["x"].astype(complex)
    fs = float(d["fs"])
    meta = json.loads(str(d["meta"])) if "meta" in d.files else {}
    print("=" * 78)
    print("파일: %s" % os.path.basename(path))
    print("샘플레이트 %.2f MSps · 길이 %d 샘플 (%.1f us) · ILC 최고 ACLR %.1f dBc · SG %.2f dBm"
          % (fs / 1e6, len(x), len(x) / fs * 1e6, float(d["best"]), float(d["sg"])))
    if meta:
        print("정보: 샘플 %s · %.3f GHz · %s · 반복 %s%s" % (meta.get("stype"), float(meta.get("freq") or 0), meta.get("when"),
                                                      meta.get("iters"), " · 중지" if meta.get("partial") else ""))

    # 같은 크기로 (u 는 x 와 같은 스케일이어야)
    g = np.vdot(x, u) / np.vdot(x, x)
    print("u / x 평균 이득: %.2f dB · %.1f°" % (20 * np.log10(abs(g)), np.degrees(np.angle(g))))
    # x 와 u 의 시간 어긋남 (교차 상관 최고점 · 소수점 지연)
    cc = np.fft.ifft(np.fft.fft(u) * np.conj(np.fft.fft(x)))
    k0 = int(np.argmax(np.abs(cc)))
    k0s = k0 if k0 < len(cc) // 2 else k0 - len(cc)
    c_m, c_0, c_p = np.abs(cc[(k0 - 1) % len(cc)]), np.abs(cc[k0]), np.abs(cc[(k0 + 1) % len(cc)])
    frac = 0.5 * (c_m - c_p) / (c_m - 2 * c_0 + c_p) if (c_m - 2 * c_0 + c_p) != 0 else 0.0
    dly = k0s + frac
    print("x → u 시간 어긋남: %+.3f 샘플 (%+.3f ns) %s" % (dly, dly / fs * 1e9,
          "← u 가 x 보다 앞섬 (지금 모델은 앞 샘플 못 씀)" if dly < -0.02 else ("← u 가 x 보다 늦음" if dly > 0.02 else "(거의 없음)")))

    # 대역 정하기: x 점유 대역 → 인접 채널
    f, Px = psd(x, fs)
    occ = f[Px > Px.max() * 10 ** (-30 / 10)]
    f_lo, f_hi = float(occ.min()), float(occ.max())
    occ_w = f_hi - f_lo
    adj = arg_val(sys.argv, "--adj", None)
    adj = adj * 1e6 if adj else occ_w / 2
    obs = 0.4 * fs                               # FSW 가 보는 대략 끝
    bands = [("아래 인접", max(f_lo - adj, -fs / 2), f_lo), ("위 인접", f_hi, min(f_hi + adj, fs / 2))]
    print("신호 점유 %.0f ~ %.0f MHz (폭 %.0f) · 인접 채널 폭 %.0f MHz · FSW 관측 끝 약 ±%.0f MHz"
          % (f_lo / 1e6, f_hi / 1e6, occ_w / 1e6, adj / 1e6, obs / 1e6))

    _, Pu = psd(u, fs)
    pin = bandp(f, Pu, f_lo, f_hi)
    cor = u - g * x                               # ILC 가 더한 보정 성분
    _, Pc = psd(cor, fs)

    # 학습 구간
    n = len(x)
    nfit = int(min(60000, n // 2))
    i0 = (n - nfit) // 2

    cases = [
        ("GMP K5 M4 L1 (지금과 비슷)", terms(5, 4, 1)),
        ("GMP K7 M4 L1", terms(7, 4, 1)),
        ("GMP K5 M12 L2 (긴 메모리)", terms(5, 12, 2)),
        ("GMP K5 M4 L1 + 앞 샘플 3 (m −3~3)", terms(5, 4, 1, mneg=3)),
        ("GMP K5 M4 L1 + 선형 FIR ±32 탭", terms(5, 4, 1, fir=32)),
        ("GMP K5 M4 L1 + 켤레 항 (I/Q 불균형)", terms(5, 4, 1, conj=True)),
        ("GMP K5 M12 L2 + 켤레 항", terms(5, 12, 2, conj=True)),
    ]
    print("-" * 78)
    print("① ILC 가 더한 보정 성분 (u - x) 의 인접 채널 레벨 = 모델이 만들어 내야 하는 것")
    for nm_, lo, hi in bands:
        print("   %-6s %7.1f dBc" % (nm_, db(bandp(f, Pc, lo, hi) / pin)))
    print("-" * 78)
    print("② 모델별 '모델 출력 − ILC 입력' (못 따라간 나머지) - 작을수록 좋음")
    print("   %-36s %8s %8s %8s %6s %5s" % ("모델", "아래인접", "위인접", "전체오차", "계수", "초"))
    res_keep = {}
    for nm_, tl in cases:
        t0 = time.time()
        out = fit_apply(x, u, tl, i0, nfit)
        r = u - out
        _, Pr = psd(r, fs)
        lo_ = db(bandp(f, Pr, bands[0][1], bands[0][2]) / pin)
        hi_ = db(bandp(f, Pr, bands[1][1], bands[1][2]) / pin)
        nm_all = db(np.mean(np.abs(r) ** 2) / np.mean(np.abs(u) ** 2))
        print("   %-36s %8.1f %8.1f %8.1f %6d %5.1f" % (nm_, lo_, hi_, nm_all, len(tl), time.time() - t0))
        res_keep[nm_] = (r, Pr)

    r, Pr = res_keep[cases[0][0]]
    print("-" * 78)
    print("③ 못 따라간 나머지 (GMP K5 M4 L1) 의 주파수 모양 (50 MHz 구간 · 신호 대비 dBc/50MHz)")
    edges = np.arange(np.floor(-fs / 2 / 50e6) * 50e6, fs / 2, 50e6)
    row = []
    for a_ in edges:
        b_ = a_ + 50e6
        v = bandp(f, Pr, a_, b_)
        if v == v:
            mark = "*" if (b_ <= f_lo or a_ >= f_hi) else " "
            row.append("%+5.0f%s:%6.1f" % (a_ / 1e6 + 25, mark, db(v / pin)))
    for i in range(0, len(row), 6):
        print("   " + "  ".join(row[i:i + 6]))
    print("   (숫자 = 구간 가운데 MHz · * = 신호 밖)")

    # 대칭 / 거울상: 아래 인접 나머지 vs 위 인접 나머지 의 거울 상관
    R = np.fft.fft(r)
    ff = np.fft.fftfreq(len(r), 1 / fs)
    mU = (ff >= f_hi) & (ff < f_hi + adj)
    idx = np.where(mU)[0]
    mir = (-idx) % len(r)                         # -f 칸
    Ru, Rl = R[idx], R[mir]
    coh_m = abs(np.vdot(np.conj(Rl), Ru)) / np.sqrt(np.vdot(Rl, Rl).real * np.vdot(Ru, Ru).real + 1e-30)
    # 나머지가 x 의 켤레 (이미지) 와 얼마나 닮았나: 신호 안 나머지 vs conj(X(-f))
    X = np.fft.fft(x)
    mIn = (ff >= f_lo) & (ff <= f_hi)
    ii = np.where(mIn)[0]
    img = np.conj(X[(-ii) % len(x)])
    coh_i = abs(np.vdot(img, R[ii])) / np.sqrt(np.vdot(img, img).real * np.vdot(R[ii], R[ii]).real + 1e-30)
    lin = X[ii]
    coh_l = abs(np.vdot(lin, R[ii])) / np.sqrt(np.vdot(lin, lin).real * np.vdot(R[ii], R[ii]).real + 1e-30)
    print("-" * 78)
    print("④ 나머지의 성격 (0 ~ 1, 1 에 가까울수록 그 성격)")
    print("   위 · 아래 인접 나머지가 서로 거울상 (켤레 대칭)     : %.2f" % coh_m)
    print("   신호 안 나머지가 원래 신호의 거울상 (I/Q 이미지)   : %.2f" % coh_i)
    print("   신호 안 나머지가 원래 신호 그대로 (선형 이득 · 위상): %.2f" % coh_l)

    # 크기별: 나머지가 피크에 몰리나
    a = np.abs(x)
    qs = np.percentile(a, [0, 50, 90, 99, 99.9, 100])
    er = np.abs(r) ** 2
    ref = np.mean(np.abs(u) ** 2)
    print("-" * 78)
    print("⑤ 원래 신호 크기별 나머지 전력 (평균 신호 전력 대비 dB)")
    for lo_q, hi_q, nm_ in zip(qs[:-1], qs[1:], ("하위 50%", "50~90%", "90~99%", "99~99.9%", "최상위 0.1%")):
        m = (a >= lo_q) & (a <= hi_q)
        print("   %-11s %7.1f dB  (샘플 %d)" % (nm_, db(er[m].mean() / ref), int(m.sum())))

    # 스퍼: 나머지 PSD 에서 주변보다 15 dB 넘게 튀는 선
    med = np.convolve(Pr, np.ones(101) / 101, "same")
    pk = np.where(Pr > med * 10 ** 1.5)[0]
    print("-" * 78)
    if len(pk):
        top = pk[np.argsort(-Pr[pk])][:6]
        print("⑥ 나머지에 튀는 선 (스퍼 후보): " + ", ".join("%.1f MHz (+%.0f dB)" % (f[i] / 1e6, db(Pr[i] / med[i])) for i in sorted(top)))
    else:
        print("⑥ 나머지에 튀는 선 (스퍼): 없음")

    # 앞 / 뒤 반: 시간에 따라 변하나 (같은 모델로 앞 반 학습 → 뒤 반 평가)
    h = n // 2
    tl = cases[0][1]
    out_a = fit_apply(x, u, tl, max(0, h // 2 - nfit // 2), min(nfit, h))
    out_b = fit_apply(x, u, tl, min(n - nfit, h + h // 2 - nfit // 2), min(nfit, h))
    ea = db(np.mean(np.abs(u[h:] - out_a[h:]) ** 2) / ref)
    eb = db(np.mean(np.abs(u[h:] - out_b[h:]) ** 2) / ref)
    print("⑦ 뒤 반 오차: 앞 반으로 학습 %.1f dB · 뒤 반으로 학습 %.1f dB (차이 크면 학습 구간 밖에서 성질이 다름)" % (ea, eb))
    print("=" * 78)
    print("위 표를 복사해서 보내 주세요 (파형 데이터는 없음).")


if __name__ == "__main__":
    main()
