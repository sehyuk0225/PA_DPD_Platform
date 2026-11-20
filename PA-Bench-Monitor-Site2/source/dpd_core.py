# -*- coding: utf-8 -*-
"""PA DPD Tester - 신호 · DPD 계산 (numpy 만 사용)

- 5G NR 100 MHz (SCS 30 kHz, 273 RB) 1 캐리어 OFDM 신호 만들기 + CFR (PAPR 줄이기)
- R&S .wv 파일 쓰기 (SMW200A ARB 용, 암호화 없음)
- 받은 IQ 정렬 (지연 · 위상 · 크기)
- DPD: 메모리 다항식 (MP) / GMP, 간접 학습 (ILA), 정규화 최소제곱
- 성능: ACLR (±100 MHz, 98.28 MHz 측정 대역), EVM (서브캐리어별 등화), AM-AM / AM-PM
"""
import math
import time

import numpy as np

# ---------------------------------------------------------------- NR 100 MHz 1C
NR = dict(scs=30e3, nrb=273, nfft=4096, fs=122.88e6, cp=288, bw=100e6, meas_bw=98.28e6)
QAM_BITS = {"QPSK": 2, "16QAM": 4, "64QAM": 6, "256QAM": 8}


def qam_symbols(n, mod, rng):
    b = QAM_BITS[mod]
    m = int(round(math.sqrt(2 ** b)))
    lv = np.arange(m) * 2 - (m - 1)
    i = rng.choice(lv, n)
    q = rng.choice(lv, n)
    s = (i + 1j * q).astype(np.complex128)
    return s / math.sqrt(np.mean(np.abs(s) ** 2))


class NRSignal:
    """NR 100 MHz 1 캐리어 OFDM. os = 오버샘플링 (샘플레이트 = 122.88 MHz × os)"""

    def __init__(self, os=4, n_sym=28, mod="256QAM", seed=1, carriers=1):
        self.os = int(os)
        self.n_sym = int(n_sym)
        self.mod = mod
        self.seed = seed
        self.carriers = carriers                  # 지금은 1 캐리어만 (2 캐리어는 다음 버전)
        self.nfft = NR["nfft"] * self.os
        self.cp = NR["cp"] * self.os
        self.fs = NR["fs"] * self.os
        self.nsc = NR["nrb"] * 12                 # 3276 서브캐리어
        k = np.arange(self.nsc) - self.nsc // 2   # DC 포함 가운데 정렬 (DC 도 사용 - 테스트용)
        self.bins = np.mod(k, self.nfft)
        rng = np.random.default_rng(seed)
        self.X = qam_symbols(self.n_sym * self.nsc, mod, rng).reshape(self.n_sym, self.nsc)
        self.x = self._modulate(self.X)
        self.x_orig = self.x.copy()

    def _modulate(self, X):
        F = np.zeros((X.shape[0], self.nfft), complex)
        F[:, self.bins] = X
        t = np.fft.ifft(F, axis=1) * math.sqrt(self.nfft)
        t = np.concatenate([t[:, -self.cp:], t], axis=1)
        x = self.chan_filter(t.reshape(-1))
        return x / rms(x)

    def chan_filter(self, x):
        """채널 필터: 전체 주기 신호를 FFT 로 걸러 ±49.2 MHz 밖을 없앰 (49.2~50.4 MHz 코사인 전이).
        필터가 없으면 OFDM 심볼 경계 때문에 기준 신호 자체 ACLR 이 -43 dBc 정도라서 DPD 효과를 못 봄"""
        n = len(x)
        f = np.abs(np.fft.fftfreq(n, 1 / self.fs))
        f1, f2 = 49.2e6, 50.4e6
        h = np.where(f <= f1, 1.0, np.where(f >= f2, 0.0, 0.5 * (1 + np.cos(np.pi * (f - f1) / (f2 - f1)))))
        return np.fft.ifft(np.fft.fft(x) * h)

    @property
    def sym_len(self):
        return self.nfft + self.cp

    def cfr(self, papr_db=8.0, iters=4):
        """클리핑 + 대역 밖 제거를 반복해서 PAPR 을 papr_db 근처로 (EVM 이 조금 나빠짐)"""
        x = self.x_orig.copy()
        for _ in range(iters):
            r = rms(x)
            lim = r * 10 ** (papr_db / 20)
            a = np.abs(x)
            g = np.where(a > lim, lim / np.maximum(a, 1e-12), 1.0)
            x = self.chan_filter(x * g)              # 클리핑으로 생긴 대역 밖 성분 제거
        self.x = x / rms(x)
        return papr(self.x)


class FileSignal:
    """불러온 .wv 신호 (QAM 심볼을 모름 → EVM 은 파형 비교)"""

    def __init__(self, path, max_fs=None, up=1):
        x, fs = read_wv(path)
        if not fs or fs <= 0:
            raise ValueError(".wv 에 샘플레이트 (CLOCK) 가 없습니다")
        if len(x) < 1000:
            raise ValueError(".wv 샘플이 너무 적습니다 (%d)" % len(x))
        self.path = path
        self.fs_file = float(fs)
        self.n_file = len(x)
        self.drop_db = None
        fs = float(fs)
        # 샘플레이트가 너무 높으면 (FSW 캡처 · 계산 부담) 반씩 줄임: 주기 신호라 FFT 로 잘라내면 정확
        while max_fs and fs > max_fs * 1.0001 and len(x) % 2 == 0:
            X = np.fft.fft(x)
            n = len(x)
            h = n // 4
            keep = np.concatenate([X[:h], X[n - h:]])
            tot = np.sum(np.abs(X) ** 2)
            drop = 1 - np.sum(np.abs(keep) ** 2) / tot
            self.drop_db = 10 * np.log10(max(drop, 1e-20))
            x = np.fft.ifft(keep) / 2
            fs /= 2
        # 샘플레이트 올리기 (×up): 주기 신호라 FFT 가운데에 0 을 끼우면 정확 → SMW 가 더 빠르게 보내고
        # FSW 가 더 넓게 받음 (FSW I/Q 쓸 수 있는 대역 ≈ 0.8 × 샘플레이트 → 반송파 끝 · 바깥 인접 채널까지)
        self.up = 1
        if up and int(up) > 1:
            k = int(up)
            n = len(x)
            X = np.fft.fft(x)
            h = n // 2
            Xu = np.concatenate([X[:h], np.zeros(n * (k - 1), complex), X[h:]])
            x = np.fft.ifft(Xu) * k
            fs *= k
            self.up = k
        self.fs = fs
        self.x = x / rms(x)
        self.x_orig = self.x
        self.X = None
        self.n_sym = None

    def cfr(self, papr_db=8.0, iters=4):
        return papr(self.x)               # 불러온 신호는 그대로 씀


def evm_any(sig, y_al):
    """EVM (%): 만든 NR 신호는 서브캐리어 등화 EVM, 불러온 신호는 파형 EVM (복소 이득만 맞춘 오차 / 신호)"""
    if getattr(sig, "X", None) is not None:
        return evm(sig, y_al)
    x = sig.x
    g = np.vdot(x, y_al) / np.vdot(x, x)
    e = y_al / g - x
    return float(100 * math.sqrt(np.mean(np.abs(e) ** 2) / np.mean(np.abs(x) ** 2)))


def rms(x):
    return float(np.sqrt(np.mean(np.abs(x) ** 2)))


def papr(x):
    return float(20 * np.log10(np.max(np.abs(x)) / rms(x)))


# ---------------------------------------------------------------- .wv (R&S ARB)
def write_wv(path, x, fs, comment="PA DPD Tester"):
    """R&S SMU-WV 파일 (int16 I/Q). 최고값 = 풀스케일, LEVEL OFFS = (RMS 오프셋, 0) → SMW 는 RMS 를 설정 레벨에 맞춤"""
    x = np.asarray(x, complex)
    pk = float(np.max(np.abs(x))) or 1.0
    s = x / pk * 32767.0
    iq = np.empty(2 * len(s), np.int16)
    iq[0::2] = np.clip(np.round(s.real), -32767, 32767).astype(np.int16)
    iq[1::2] = np.clip(np.round(s.imag), -32767, 32767).astype(np.int16)
    rms_off = -20 * math.log10(rms(x) / pk)
    comment = "".join(ch if 32 <= ord(ch) < 127 and ch not in "{}" else "_" for ch in str(comment))   # 헤더는 ASCII 만
    hdr = ("{TYPE: SMU-WV,0}{COMMENT: %s}{DATE: %s}{CLOCK: %.0f}{LEVEL OFFS: %.6f,0.000000}{SAMPLES: %d}"
           % (comment, time.strftime("%Y-%m-%d;%H:%M:%S"), fs, rms_off, len(s)))
    body = iq.astype("<i2").tobytes()
    data = hdr.encode("ascii") + ("{WAVEFORM-%d:#" % (len(body) + 1)).encode("ascii") + body + b"}"
    if path:
        with open(path, "wb") as f:
            f.write(data)
    return data


def read_wv(path):
    """암호화 안 된 .wv 읽기 → (x, fs). 암호화된 파일이면 ValueError"""
    raw = open(path, "rb").read()
    i = raw.find(b"{WAVEFORM-")
    head = raw[:4096].upper()
    if i < 0 or b"ENCRYPT" in head or b"SMU-WV" not in head:
        raise ValueError("암호화된 .wv 이거나 R&S 파형 파일이 아닙니다 (IQ 값을 읽을 수 없음).\n"
                         "SMW · WinIQSIM 표준 옵션으로 만든 파일은 암호화돼 있을 수 있어요.")
    hdr = raw[:i].decode("latin-1")
    j = raw.find(b"#", i) + 1
    n = int(raw[i + 10:raw.find(b":", i)]) - 1
    iq = np.frombuffer(raw[j:j + n], "<i2").astype(float)
    fs = 0.0
    k = hdr.find("{CLOCK:")
    if k >= 0:
        fs = float(hdr[k + 7:hdr.find("}", k)].strip())
    return (iq[0::2] + 1j * iq[1::2]) / 32767.0, fs


# ---------------------------------------------------------------- 정렬
def frac_delay(x, d):
    """주기 신호 x 를 d 샘플 (실수) 만큼 늦춤 (FFT 위상)"""
    n = len(x)
    f = np.fft.fftfreq(n)
    return np.fft.ifft(np.fft.fft(x) * np.exp(-2j * np.pi * f * d))


def align(x, y):
    """y (받은 신호, 길이 ≥ len(x)) 를 x (보낸 주기 신호) 에 맞춤.
    → (y_al: x 와 같은 길이 · 같은 시간, gain: 복소 이득 y ≈ gain·x, delay 샘플)"""
    n = len(x)
    if len(y) < n:
        raise ValueError("받은 IQ 가 보낸 신호보다 짧음 (%d < %d)" % (len(y), n))
    seg = y[:n]
    # 지연은 크기(envelope)끼리 상관으로 찾음 → 위상 · 작은 주파수 차이에 영향 안 받음
    ex = np.abs(x) - np.mean(np.abs(x))
    ey = np.abs(seg) - np.mean(np.abs(seg))
    a = np.real(np.fft.ifft(np.fft.fft(ey) * np.conj(np.fft.fft(ex))))
    k = int(np.argmax(a))
    ym1, y0, yp1 = a[(k - 1) % n], a[k], a[(k + 1) % n]
    den = ym1 - 2 * y0 + yp1
    frac = 0.5 * (ym1 - yp1) / den if den != 0 else 0.0
    d = k + frac                                    # seg[n] ≈ x[n - d]
    # 정수 지연만큼 잘라냄 (받은 IQ 가 충분히 길면 이어진 구간을 그대로 → 주파수 차이가 있어도 끊김 없음)
    z = y[k:k + n] if len(y) >= k + n else np.roll(seg, -k)
    # 작은 주파수 차이 (SMW · FSW 기준 클럭이 따로일 때) → 구간별 위상 기울기를 빼 줌
    nb = 64
    L = n // nb
    if L > 64:
        ph = np.unwrap(np.angle([np.vdot(x[i * L:(i + 1) * L], z[i * L:(i + 1) * L]) for i in range(nb)]))
        slope = np.polyfit((np.arange(nb) + 0.5) * L, ph, 1)[0]
        if abs(slope) * n > 0.02:                   # 한 주기 동안 1도 이상 돌 때만
            z = z * np.exp(-1j * slope * np.arange(n))
    y_al = frac_delay(z, -frac)                     # 남은 소수 지연 (주기 신호로 보고 FFT 위상)
    # 정밀 지연: 포물선 보간은 0.01 ~ 0.02 샘플 틀림 → 오차 바닥 -45 dB 근처 (ILC · DLA 가 거기서 멈춤)
    # → 신호 대역 안 교차 스펙트럼 위상 기울기 (가중 최소제곱) 로 0.001 샘플 이하까지 맞춤
    y_al, t = fine_delay(x, y_al)
    g = np.vdot(x, y_al) / np.vdot(x, x)
    return y_al, g, d + t


def fine_delay(x, z, iters=3):
    """z 를 x 에 소수 지연까지 정밀하게 맞춤 → (맞춘 z, 고친 지연 샘플)"""
    n = len(x)
    X = np.fft.fft(x)
    w = np.abs(X) ** 2
    m = w > w.max() * 1e-3                           # 신호 대역 안 빈만
    if m.sum() < 16:
        return z, 0.0
    f = np.fft.fftfreq(n)
    sw = np.sqrt(w[m])
    A = np.stack([np.ones(m.sum()), -2 * np.pi * f[m]], 1) * sw[:, None]
    Z = np.fft.fft(z)
    tot = 0.0
    for _ in range(iters):
        cr = Z * np.conj(X)
        ph = np.angle(cr[m] * np.exp(-1j * np.angle(np.sum(cr[m]))))
        tau = np.linalg.lstsq(A, ph * sw, rcond=None)[0][1]
        if not np.isfinite(tau) or abs(tau) > 0.5:
            break
        Z = Z * np.exp(2j * np.pi * f * tau)
        tot += tau
        if abs(tau) < 1e-5:
            break
    return np.fft.ifft(Z), tot


def small_signal_gain(x, y_al, frac=0.3):
    """크기가 작은 샘플 (하위 frac) 로 구한 선형 이득 - DPD 가 맞춰야 할 목표 이득"""
    a = np.abs(x)
    m = a <= np.quantile(a, frac)
    return np.vdot(x[m], y_al[m]) / np.vdot(x[m], x[m])


# ---------------------------------------------------------------- DPD (MP / GMP)
FIT_SEGS = 1          # 학습 조각 수 (기본 1 = 예전처럼 한 구간): n_fit 샘플을 신호 전체에 고르게 이 개수로 나눠 뽑음 (1 = 한 구간)


def fit_segs(n, n_fit, k=None, rng=None):
    """학습 구간 [(시작, 길이)]: 신호 전체 (길이 n) 에 같은 간격으로 k 조각 · 합 n_fit. 늘 같은 자리 (고정 · 재현 가능)
    k = 1 이면 예전 프로그램과 똑같은 한 구간 (시드 고정 시작점)"""
    n_fit = min(int(n_fit or n), n)
    k = max(1, int(FIT_SEGS if k is None else k))
    if n_fit >= n:
        return [(0, n)]
    if k == 1:
        rng = rng or np.random.default_rng(0)
        return [(int(rng.integers(0, n - n_fit + 1)), n_fit)]
    k = min(k, max(1, n_fit // 2000))                # 조각이 너무 짧으면 (2000 샘플 미만) 개수를 줄임
    ln = n_fit // k
    step = n / k
    out = []
    for i in range(k):
        L_ = ln + (n_fit - ln * k if i == k - 1 else 0)
        s0 = int(i * step + (step - L_) / 2) if step > L_ else int(i * step)
        out.append((s0 % n, L_))
    return out



class DPDModel:
    """메모리 다항식 (GMP 는 앞뒤 envelope 교차항 추가).
    K = 비선형 차수 (홀수만 쓰기 옵션), M = 메모리 깊이, L = GMP 교차 지연 (0 = MP)"""

    def __init__(self, K=7, M=4, L=0, odd_only=False, lam=1e-6):
        self.K, self.M, self.L, self.odd, self.lam = int(K), int(M), int(L), bool(odd_only), float(lam)
        self.w = None

    def orders(self):
        return [k for k in range(1, self.K + 1) if (not self.odd or k % 2 == 1)]

    def basis(self, u):
        n = len(u)
        cols = []
        a = np.abs(u)
        for m in range(self.M):
            um = np.roll(u, m)
            am = np.roll(a, m)
            for k in self.orders():
                cols.append(um * am ** (k - 1))
        if self.L > 0:                                  # GMP: x(n-m)|x(n-m-l)|^(k-1), l = ±1..L
            for m in range(self.M):
                um = np.roll(u, m)
                for l in list(range(1, self.L + 1)) + list(range(-self.L, 0)):
                    al = np.roll(a, m + l)
                    for k in self.orders():
                        if k > 1:
                            cols.append(um * al ** (k - 1))
        return np.stack(cols, axis=1)

    def term_names(self):
        """계수 이름 (basis 순서와 같음)"""
        out = []
        for m in range(self.M):
            for k in self.orders():
                out.append("x(n-%d)|x(n-%d)|^%d" % (m, m, k - 1))
        if self.L > 0:
            for m in range(self.M):
                for l in list(range(1, self.L + 1)) + list(range(-self.L, 0)):
                    for k in self.orders():
                        if k > 1:
                            out.append("x(n-%d)|x(n-%d)|^%d" % (m, m + l, k - 1))
        return out

    def n_coef(self):
        return self.basis(np.zeros(4, complex)).shape[1]

    def identity(self):
        w = np.zeros(self.n_coef(), complex)
        w[0] = 1.0                                     # k=1, m=0
        self.w = w
        return w

    def fit(self, u, d, n_fit=40000, rng=None):
        """postdistorter 학습: basis(u) · w ≈ d (정규화 최소제곱). n_fit 샘플만 사용"""
        n = len(u)
        n_fit = min(int(n_fit or n), n)
        segs = fit_segs(n, n_fit, rng=rng)               # 신호 전체에 고르게 여러 조각 (늘 같은 자리)
        Phi = np.concatenate([self._basis_seg(u, s0, L_) for s0, L_ in segs], axis=0)
        d = np.concatenate([d[np.arange(s0, s0 + L_) % n] for s0, L_ in segs])
        n_fit = len(d)
        idx = np.arange(n_fit)
        sc = np.sqrt(np.mean(np.abs(Phi) ** 2, axis=0))      # 열마다 크기를 맞춤 (|u|^k 차수별 크기 차이로 계산이 불안정해지지 않게)
        sc[sc == 0] = 1.0
        P = Phi / sc
        A = P.conj().T @ P
        b = P.conj().T @ d[idx]
        lam = self.lam * np.trace(A).real / A.shape[0]
        w = np.linalg.solve(A + lam * np.eye(A.shape[0]), b) / sc
        resid = d[idx] - Phi @ w
        nmse = 10 * np.log10(np.mean(np.abs(resid) ** 2) / np.mean(np.abs(d[idx]) ** 2))
        return w, float(nmse)

    def pad(self):
        return self.M + self.L + 1

    def _basis_seg(self, u, s0, n):
        """주기 신호 u 의 [s0, s0+n) 구간 basis (앞쪽 지연 샘플은 주기로 이어 붙임)"""
        p = self.pad()
        idx = np.arange(s0 - p, s0 + n + p) % len(u)
        B = self.basis(u[idx])
        return B[p:p + n]

    def apply(self, x, w=None, chunk=65536):
        """DPD 적용 (구간을 나눠 계산 - 49만 샘플도 메모리 적게)"""
        w = self.w if w is None else w
        if w is None:
            return x.copy()
        out = np.empty(len(x), complex)
        for s0 in range(0, len(x), chunk):
            n = min(chunk, len(x) - s0)
            out[s0:s0 + n] = self._basis_seg(x, s0, n) @ w
        return out



class LinGMPModel(DPDModel):
    """GMP + 긴 선형 등화 (앞뒤 ±nlin 탭 FIR) - 넓은 대역 (400 MHz) 의 이득 리플 · 지연 (케이블 반사 · 필터) 을
    선형 항으로 따로 맞추고 비선형은 GMP 로. 계수는 한 번에 같이 학습 (ILA · 적용 모두 그대로)"""

    def __init__(self, K=7, M=4, L=0, odd_only=False, lam=1e-6, nlin=64):
        super().__init__(K, M, L, odd_only, lam)
        self.nlin = int(nlin)

    def lin_delays(self):
        return [d for d in range(-self.nlin, self.nlin + 1) if not (0 <= d < self.M)]

    def basis(self, u):
        B = super().basis(u)
        if self.nlin <= 0:
            return B
        lin = np.stack([np.roll(u, d) for d in self.lin_delays()], axis=1)
        return np.concatenate([B, lin], axis=1)

    def term_names(self):
        return super().term_names() + ["x(n%+d) 선형" % (-d) for d in self.lin_delays()]

    def pad(self):
        return max(super().pad(), self.nlin + 1)


class TrapGMPModel(LinGMPModel):
    """GMP (+ 긴 선형 등화) + 긴 메모리 (GaN 트래핑 · 열) 항.
    느린 평균 전력 s_τ(n) = |x|² 의 지수 평균 (시정수 τ = 0.1 · 1 · 10 µs 등, 주기 신호라 원형으로 정확히) 을 만들어
    x(n)·s_τ(n) · x(n)·|x(n)|·s_τ(n) 항을 더함 → 수 µs 전까지의 전력에 따라 바뀌는 이득 · 위상을 표현"""

    def __init__(self, K=7, M=4, L=0, odd_only=False, lam=1e-6, nlin=0, fs=491.52e6, taus_us=(0.1, 1.0, 10.0)):
        super().__init__(K, M, L, odd_only, lam, nlin)
        self.fs = float(fs)
        self.taus_us = tuple(float(t) for t in taus_us)
        self._cache = None

    def slow_feats(self, u):
        """주기 신호 u 전체의 느린 전력 성분들 (len(u) × τ 개) - 평균 전력으로 나눔"""
        key = (len(u), complex(u[0]), complex(u[len(u) // 3]), complex(u[-1]))
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1]
        n = len(u)
        p = np.abs(u) ** 2
        pm = float(np.mean(p)) or 1.0
        Pf = np.fft.fft(p / pm)
        k = np.arange(n)
        out = []
        for t_us in self.taus_us:
            nt = max(1.0, t_us * 1e-6 * self.fs)
            a = 1.0 - math.exp(-1.0 / nt)
            h = a * (1 - a) ** k / (1 - (1 - a) ** n)          # 주기로 감은 지수 커널 (원형 = 정상 상태)
            s_ = np.real(np.fft.ifft(Pf * np.fft.fft(h)))
            out.append(s_ - 1.0)                                # 평균을 빼서 선형 항과 겹치지 않게
        F = np.stack(out, axis=1)
        self._cache = (key, F)
        return F

    def _basis_seg(self, u, s0, n):
        B = super()._basis_seg(u, s0, n)
        F = self.slow_feats(u)
        idx = np.arange(s0, s0 + n) % len(u)
        xs, a = u[idx], np.abs(u[idx])
        cols = []
        for j in range(F.shape[1]):
            cols.append(xs * F[idx, j])
            cols.append(xs * a * F[idx, j])
        return np.concatenate([B, np.stack(cols, axis=1)], axis=1)

    def n_coef(self):
        return super().n_coef() + 2 * len(self.taus_us)

    def term_names(self):
        out = super().term_names()
        for t in self.taus_us:
            out += ["x·s(%gµs)" % t, "x|x|·s(%gµs)" % t]
        return out


class SegGMPModel(DPDModel):
    """구간 GMP (Doherty 용): 피크 앰프가 켜지는 레벨 t 를 기준으로 GMP 두 벌을 부드럽게 이어 붙임
    basis = [Φ·(1-s), Φ·s],  s = 1 / (1 + exp(-(|x| - t) / w)).  t 는 처음 학습 때 자동으로 찾음 (최고값의 30 ~ 70 %)"""

    def __init__(self, K=7, M=4, L=1, odd_only=False, lam=1e-6):
        super().__init__(K, M, L, odd_only, lam)
        self.t = None          # 경계 레벨 (|x| 절대값)
        self.amax = None

    def _gate(self, a):
        w = 0.04 * self.amax
        return 1.0 / (1.0 + np.exp(-np.clip((a - self.t) / w, -40, 40)))

    def basis(self, u):
        B = DPDModel.basis(self, u)
        if self.t is None:
            return np.concatenate([B, B * 0], axis=1)
        sgate = self._gate(np.abs(u))[:, None]
        return np.concatenate([B * (1 - sgate), B * sgate], axis=1)

    def term_names(self):
        nm = DPDModel.term_names(self)
        return ["피크 전 · " + t for t in nm] + ["피크 후 · " + t for t in nm]

    def identity(self):
        n = self.n_coef()
        w = np.zeros(n, complex)
        w[0] = w[n // 2] = 1.0
        self.w = w
        return w

    def fit(self, u, d, n_fit=40000, rng=None):
        if self.t is None:                           # 경계 레벨 찾기: 후보마다 학습 → 오차가 가장 작은 것
            self.amax = float(np.percentile(np.abs(u), 99.99))
            best = None
            for f in (0.3, 0.4, 0.5, 0.6, 0.7):
                self.t = f * self.amax
                w, nm = DPDModel.fit(self, u, d, n_fit=min(int(n_fit or len(u)), 20000), rng=np.random.default_rng(3))
                if best is None or nm < best[0]:
                    best = (nm, self.t)
            self.t = best[1]
            self.t_frac = self.t / self.amax
        return DPDModel.fit(self, u, d, n_fit, rng)


class DVRModel(DPDModel):
    """DVR (Decomposed Vector Rotation, Doherty 에 강함): 비선형을 입력 크기 구간마다 다른 기울기의 직선으로
    항 = ||x(n-m)| - β_k| · e^{jθ(n-m)}  (+ 교차: ||x(n-m)| - β_k| · e^{jθ(n)},  ||x(n)| - β_k| · e^{jθ(n-m)})
    β_k = 최고값 × k / 구간 수.  최소제곱으로 바로 풀림 (GMP 와 같은 속도)"""

    def __init__(self, nseg=8, M=4, lam=1e-6):
        super().__init__(K=int(nseg), M=M, L=0, odd_only=False, lam=lam)
        self.nseg = int(nseg)
        self.amax = None

    def betas(self):
        return [self.amax * k / self.nseg for k in range(self.nseg)]

    def basis(self, u):
        a = np.abs(u) + 1e-12
        e = u / a
        bs = self.betas() if self.amax else [0.0]
        cols = []
        for m in range(self.M):
            am, em = np.roll(a, m), np.roll(e, m)
            for b in bs:
                cols.append(np.abs(am - b) * em)                       # k = 0 (β = 0) 이면 x(n-m) 그대로
        for m in range(1, self.M):
            am, em = np.roll(a, m), np.roll(e, m)
            for b in bs[1:]:
                cols.append(np.abs(am - b) * e)
                cols.append(np.abs(a - b) * em)
        return np.stack(cols, axis=1)

    def term_names(self):
        out = []
        for m in range(self.M):
            for k in range(self.nseg):
                out.append("||x(n-%d)|-b%d| e^jθ(n-%d)" % (m, k, m))
        for m in range(1, self.M):
            for k in range(1, self.nseg):
                out.append("||x(n-%d)|-b%d| e^jθ(n)" % (m, k))
                out.append("||x(n)|-b%d| e^jθ(n-%d)" % (k, m))
        return out

    def n_coef(self):
        return self.M * self.nseg + 2 * (self.M - 1) * (self.nseg - 1)

    def identity(self):
        w = np.zeros(self.n_coef(), complex)
        w[0] = 1.0
        self.w = w
        return w

    def fit(self, u, d, n_fit=40000, rng=None):
        if self.amax is None:
            self.amax = float(np.percentile(np.abs(u), 99.99))
        return DPDModel.fit(self, u, d, n_fit, rng)

class NNModel:
    """신경망 DPD (ARVTDNN): 지금 + 이전 M 샘플의 I · Q · |u| · |u|² · |u|³ → (d - u) 의 I · Q
    은닉층 2개 (tanh). numpy 만 사용 (Adam). w = 가중치 dict"""

    def __init__(self, M=4, hidden=24, epochs=30, lr=2e-3, seed=0, pn=False):
        self.M, self.H, self.epochs, self.lr = int(M), int(hidden), int(epochs), float(lr)
        self.pn = bool(pn)     # 위상 정규화: 지금 샘플 위상을 0 으로 돌려서 넣고 · 결과를 다시 돌림 (진폭 왜곡을 먼저 배움)
        self.seed = seed
        self.w = None
        self.K, self.L = "NN", 0                      # 표시용

    def n_in(self):
        return 5 * (self.M + 1)

    def n_coef(self):
        a, h = self.n_in(), self.H
        return a * h + h + h * h + h + h * 2 + 2

    def _feat(self, u, s0, n):
        p = self.M
        idx = np.arange(s0 - p, s0 + n) % len(u)
        v = u[idx]
        rot = np.ones(n, complex)
        if self.pn:
            cur = v[p:p + n]
            rot = np.conj(cur) / (np.abs(cur) + 1e-12)          # e^{-jθ(n)}: 모든 탭을 지금 샘플 위상 기준으로
        a = np.abs(v)
        F = np.empty((n, self.n_in()), np.float64)
        for m in range(self.M + 1):
            seg = slice(p - m, p - m + n)
            vm = v[seg] * rot if self.pn else v[seg]
            F[:, 5 * m] = vm.real
            F[:, 5 * m + 1] = vm.imag
            F[:, 5 * m + 2] = a[seg]
            F[:, 5 * m + 3] = a[seg] ** 2
            F[:, 5 * m + 4] = a[seg] ** 3
        self._rot = rot
        return F, u[idx][p:p + n]

    def _init(self, rng):
        a, h = self.n_in(), self.H
        return dict(W1=rng.standard_normal((a, h)) * np.sqrt(1.0 / a), b1=np.zeros(h),
                    W2=rng.standard_normal((h, h)) * np.sqrt(1.0 / h), b2=np.zeros(h),
                    W3=rng.standard_normal((h, 2)) * 0.01, b3=np.zeros(2))

    @staticmethod
    def _fwd(w, F):
        h1 = np.tanh(F @ w["W1"] + w["b1"])
        h2 = np.tanh(h1 @ w["W2"] + w["b2"])
        return h1, h2, h2 @ w["W3"] + w["b3"]

    def fit(self, u, d, n_fit=40000, rng=None, init=None, cancelled=None):
        """postdistorter 학습 (u → d). init = 이전 가중치 (이어서 학습) → (w, nmse dB)"""
        n = len(u)
        n_fit = min(int(n_fit or n), n)
        rng = rng or np.random.default_rng(self.seed)
        Fs, Us, Rs, Ds = [], [], [], []
        for s0, L_ in fit_segs(n, n_fit, rng=rng):                 # 신호 전체에 고르게 여러 조각 (늘 같은 자리)
            F_, uu_ = self._feat(u, s0, L_)
            Fs.append(F_); Us.append(uu_); Rs.append(self._rot); Ds.append(d[np.arange(s0, s0 + L_) % n])
        F, uu, dd = np.concatenate(Fs), np.concatenate(Us), np.concatenate(Ds)
        self._rot = np.concatenate(Rs)
        n_fit = len(dd)
        t = (dd - uu) * self._rot      # 바로 통과 (u) 와의 차이만 배움 (위상 정규화면 돌린 좌표에서)
        T = np.stack([t.real, t.imag], 1)
        w = {k: v.copy() for k, v in (init or self._init(rng)).items()}
        mo = {k: np.zeros_like(v) for k, v in w.items()}
        ve = {k: np.zeros_like(v) for k, v in w.items()}
        b1, b2, eps, step = 0.9, 0.999, 1e-8, 0
        bs = 1024
        ep = self.epochs if init is None else max(self.epochs // 2, 5)
        for e in range(ep):
            if cancelled and cancelled():
                break
            order = rng.permutation(n_fit)
            lr = self.lr * (0.5 if e > ep * 0.7 else 1.0)
            for i in range(0, n_fit - bs + 1, bs):
                j = order[i:i + bs]
                f, tt = F[j], T[j]
                h1, h2, o = self._fwd(w, f)
                g = 2 * (o - tt) / len(j)
                gr = dict(W3=h2.T @ g, b3=g.sum(0))
                d2 = (g @ w["W3"].T) * (1 - h2 ** 2)
                gr["W2"], gr["b2"] = h1.T @ d2, d2.sum(0)
                d1 = (d2 @ w["W2"].T) * (1 - h1 ** 2)
                gr["W1"], gr["b1"] = f.T @ d1, d1.sum(0)
                step += 1
                for k in w:
                    mo[k] = b1 * mo[k] + (1 - b1) * gr[k]
                    ve[k] = b2 * ve[k] + (1 - b2) * gr[k] ** 2
                    w[k] -= lr * (mo[k] / (1 - b1 ** step)) / (np.sqrt(ve[k] / (1 - b2 ** step)) + eps)
        o = self._fwd(w, F)[2]
        r = (o[:, 0] + 1j * o[:, 1]) * np.conj(self._rot) + uu - dd
        nmse = 10 * np.log10(np.mean(np.abs(r) ** 2) / np.mean(np.abs(dd) ** 2))
        return w, float(nmse)

    def apply(self, x, w=None, chunk=65536):
        w = self.w if w is None else w
        if w is None:
            return x.copy()
        out = np.empty(len(x), complex)
        for s0 in range(0, len(x), chunk):
            n = min(chunk, len(x) - s0)
            F, uu = self._feat(x, s0, n)
            o = self._fwd(w, F)[2]
            out[s0:s0 + n] = uu + (o[:, 0] + 1j * o[:, 1]) * np.conj(self._rot)
        return out


def model_search(u, d, odd_only=False, lam=1e-6, n_fit=30000, Ks=(5, 7, 9), Ms=(2, 3, 4, 6), Ls=(0, 1, 2),
                 cancelled=None):
    """DPD 모델 (K, M, L) 자동 찾기 - 계산만으로. postdistorter (u → d) 를 한 구간으로 학습하고
    다른 구간 (안 본 데이터) 에서 오차 (검증 NMSE) 를 잼 → [(검증 NMSE, 계수 수, K, M, L)] 좋은 순"""
    n = len(u)
    rng = np.random.default_rng(1)
    n_fit = min(n_fit, n // 3)
    out = []
    for K in Ks:
        for M in Ms:
            for L in Ls:
                if cancelled and cancelled():
                    return out
                if L > 0 and M < 2:
                    continue
                m = DPDModel(K, M, L, odd_only, lam)
                s_fit = int(rng.integers(0, n // 2 - n_fit))
                s_val = n // 2 + int(rng.integers(0, n // 2 - n_fit))
                Phi = m._basis_seg(u, s_fit, n_fit)
                sc = np.sqrt(np.mean(np.abs(Phi) ** 2, axis=0))
                sc[sc == 0] = 1.0
                P = Phi / sc
                A = P.conj().T @ P
                b = P.conj().T @ d[s_fit:s_fit + n_fit]
                lm = lam * np.trace(A).real / A.shape[0]
                try:
                    w = np.linalg.solve(A + lm * np.eye(A.shape[0]), b) / sc
                except np.linalg.LinAlgError:
                    continue
                dv = d[s_val:s_val + n_fit]
                r = dv - m._basis_seg(u, s_val, n_fit) @ w
                nm = 10 * np.log10(np.mean(np.abs(r) ** 2) / np.mean(np.abs(dv) ** 2))
                out.append((float(nm), Phi.shape[1], K, M, L))
    out.sort()
    return out



def dvr_search(u, d, lam=1e-6, n_fit=30000, segs=(4, 6, 8, 10, 12), Ms=(2, 3, 4, 6), cancelled=None):
    """DVR 모델 (구간 수 · M) 자동 찾기 - 계산만으로 (model_search 와 같은 방식: 한 구간 학습 · 다른 구간 검증)
    → [(검증 NMSE, 계수 수, 구간 수, M, 0)] 좋은 순 (pick_models 에 그대로)"""
    n = len(u)
    rng = np.random.default_rng(1)
    n_fit = min(n_fit, n // 3)
    amax = float(np.percentile(np.abs(u), 99.99))
    out = []
    for ns in segs:
        for M in Ms:
            if cancelled and cancelled():
                return out
            m = DVRModel(ns, M, lam)
            m.amax = amax
            s_fit = int(rng.integers(0, n // 2 - n_fit))
            s_val = n // 2 + int(rng.integers(0, n // 2 - n_fit))
            Phi = m._basis_seg(u, s_fit, n_fit)
            sc = np.sqrt(np.mean(np.abs(Phi) ** 2, axis=0))
            sc[sc == 0] = 1.0
            P = Phi / sc
            A = P.conj().T @ P
            b = P.conj().T @ d[s_fit:s_fit + n_fit]
            lm = lam * np.trace(A).real / A.shape[0]
            try:
                w = np.linalg.solve(A + lm * np.eye(A.shape[0]), b) / sc
            except np.linalg.LinAlgError:
                continue
            dv = d[s_val:s_val + n_fit]
            r = dv - m._basis_seg(u, s_val, n_fit) @ w
            nm = 10 * np.log10(np.mean(np.abs(r) ** 2) / np.mean(np.abs(dv) ** 2))
            out.append((float(nm), Phi.shape[1], int(ns), int(M), 0))
    out.sort()
    return out

def pick_models(res, n=3, tol=0.3):
    """검증 NMSE 가 가장 좋은 것과 tol dB 안이면 계수가 적은 쪽을 먼저 → 서로 다른 후보 n 개"""
    if not res:
        return []
    best = res[0][0]
    near = sorted([r for r in res if r[0] <= best + tol], key=lambda r: (r[1], r[0]))
    pick = [near[0]]
    for r in [res[0]] + res[1:]:
        if len(pick) >= n:
            break
        if all((r[2], r[3], r[4]) != (q[2], q[3], q[4]) for q in pick):
            pick.append(r)
    return pick[:n]


# ---------------------------------------------------------------- 성능
def welch_psd(x, fs, nfft=8192):
    """Welch PSD (Hann, 50% 겹침) → (f [Hz, -fs/2..fs/2], psd 선형)"""
    x = np.asarray(x)
    nfft = min(nfft, len(x))
    win = np.hanning(nfft)
    step = nfft // 2
    acc = np.zeros(nfft)
    cnt = 0
    for s in range(0, len(x) - nfft + 1, step):
        seg = x[s:s + nfft] * win
        acc += np.abs(np.fft.fft(seg)) ** 2
        cnt += 1
    psd = np.fft.fftshift(acc / max(cnt, 1) / (fs * np.sum(win ** 2)))
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / fs))
    return f, psd


def band_power(f, psd, fc, bw):
    m = (f >= fc - bw / 2) & (f <= fc + bw / 2)
    return float(np.sum(psd[m])) if np.any(m) else float("nan"), bool(np.any(m)) and (fc - bw / 2 >= f[0]) and (fc + bw / 2 <= f[-1])


def aclr(y, fs, bw=NR["meas_bw"], spacing=NR["bw"]):
    """ACLR (dBc): 아래 / 위 인접 채널 (±spacing, 측정 대역 bw). 샘플레이트가 모자라면 full=False"""
    f, p = welch_psd(y, fs)
    pc, _ = band_power(f, p, 0.0, bw)
    pl, okl = band_power(f, p, -spacing, bw)
    pu, oku = band_power(f, p, spacing, bw)
    return (10 * np.log10(pl / pc), 10 * np.log10(pu / pc), okl and oku)


def down_k(y, k):
    """K 배 빠르게 받은 I/Q → 샘플레이트 1/K (FFT 로 가운데 대역만 남김). 블록 끝 몇 샘플은 울림 → 정렬이 가운데를 씀"""
    k = int(k)
    if k <= 1:
        return y
    n = (len(y) // (2 * k)) * (2 * k)
    Y = np.fft.fft(np.asarray(y[:n]))
    m = n // k
    h = m // 2
    return np.fft.ifft(np.concatenate([Y[:h], Y[n - h:]])) / k


NR_BWS = (5, 10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 90, 100, 200, 400)


def find_carriers(x, fs, thr_db=-25.0, min_bw=3e6):
    """보내는 신호 스펙트럼에서 반송파 찾기 → [(중심 Hz, 차지한 폭 Hz)] (낮은 주파수부터)
    PSD 를 조금 펴서 최고보다 thr_db 위인 구간 · 짧은 틈 (DC 빈 칸 등) 은 이어 붙임"""
    f, p = welch_psd(x, fs)
    df = f[1] - f[0]
    k = max(1, int(round(0.5e6 / df)))
    ps = np.convolve(p, np.ones(k) / k, mode="same")
    pdb = 10 * np.log10(np.maximum(ps, 1e-30))
    on = pdb > np.max(pdb) + thr_db
    runs, i, n = [], 0, len(on)
    while i < n:
        if on[i]:
            j = i
            while j + 1 < n and on[j + 1]:
                j += 1
            runs.append([f[i], f[j]])
            i = j + 1
        else:
            i += 1
    merged = []
    for a, b in runs:
        if merged and a - merged[-1][1] < max(3e6, 0.02 * (merged[-1][1] - merged[-1][0])):
            merged[-1][1] = b
        else:
            merged.append([a, b])
    out = [(0.5 * (a + b), b - a + df) for a, b in merged if b - a + df >= min_bw]
    return out


def nominal_bw(occ):
    """차지한 폭 → NR 공칭 채널 폭 (Hz) 추정"""
    for b in NR_BWS:
        if b * 1e6 >= occ * 0.98:
            return b * 1e6
    return occ


def aclr_carriers(f, p, cars, bw, adj):
    """반송파마다 아래 / 위 인접 채널 ACLR (그 반송파 전력 기준 dBc)
    → [dict(car, side 'L'/'U', f0, dbc, ok)] · 인접 채널이 다른 반송파와 겹치면 뺌"""
    out = []
    for i, (fc, _w) in enumerate(cars):
        pc, _ = band_power(f, p, fc, bw)
        for side, s in (("L", -1), ("U", 1)):
            f0 = fc + s * adj
            if any(j != i and abs(f0 - fj) < 0.5 * (wj + bw) * 0.9 for j, (fj, wj) in enumerate(cars)):
                continue
            pa, ok = band_power(f, p, f0, bw)
            v = 10 * np.log10(pa / pc) if (pa == pa and pc and pc == pc and pa > 0) else float("nan")
            out.append(dict(car=i, side=side, f0=f0, dbc=float(v), ok=bool(ok)))
    return out


def aclr_worst(r):
    """측정 한 줄의 가장 나쁜 ACLR (dBc) - 반송파 여러 개면 안쪽 인접 채널도 포함"""
    v = [r.get("aclr_l"), r.get("aclr_u")] + list(r.get("aclr_in") or [])
    v = [float(t) for t in v if t is not None and t == t]
    return max(v) if v else float("nan")


def evm(sig, y_al):
    """EVM (%) : 정렬된 받은 신호를 심볼별 FFT → 서브캐리어별 채널 추정으로 등화 → 보낸 QAM 과 비교"""
    n_sym, L = sig.n_sym, sig.sym_len
    Y = y_al[:n_sym * L].reshape(n_sym, L)[:, sig.cp:]
    F = np.fft.fft(Y, axis=1) / math.sqrt(sig.nfft)
    Yd = F[:, sig.bins]
    X = sig.X
    H = np.sum(Yd * np.conj(X), axis=0) / np.sum(np.abs(X) ** 2, axis=0)
    E = Yd / H - X
    return float(100 * math.sqrt(np.sum(np.abs(E) ** 2) / np.sum(np.abs(X) ** 2)))


def am_curves(x, y_al, g, n_pts=3000, rng=None):
    """AM-AM (dB 이득) · AM-PM (도) 산점도용 - 입력 크기 (최고값 기준 dB)"""
    rng = rng or np.random.default_rng(1)
    idx = rng.choice(len(x), min(n_pts, len(x)), replace=False)
    xi, yi = x[idx], y_al[idx] / g
    pk = np.max(np.abs(x))
    a = np.abs(xi)
    m = a > pk * 1e-3
    xin = 20 * np.log10(a[m] / pk)
    gain = 20 * np.log10(np.abs(yi[m]) / a[m])
    ph = np.degrees(np.angle(yi[m] / xi[m]))
    return xin, gain, ph


def iq_power_dbm(y, offset_db=0.0):
    """FSW I/Q (V) → 평균 전력 dBm (50 Ω, |iq| = 첨두 전압 기준) + 보정값"""
    p = np.mean(np.abs(y) ** 2) / (2 * 50.0)
    return float(10 * np.log10(max(p, 1e-30) * 1e3) + offset_db)


def spectrum_db(y, fs, n_pts=1200):
    """그래프용 스펙트럼 (dB, 채널 최고 0 dB 기준) - 점 수를 줄여서"""
    f, p = welch_psd(y, fs)
    pdb = 10 * np.log10(np.maximum(p, 1e-30))
    pdb -= np.max(pdb)
    if len(f) > n_pts:
        k = len(f) // n_pts
        f = f[:k * n_pts].reshape(n_pts, k).mean(1)
        pdb = pdb[:k * n_pts].reshape(n_pts, k).max(1)
    return f, pdb
