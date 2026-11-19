# -*- coding: utf-8 -*-
"""PA DPD Tester - 측정 · DPD 반복 (화면 없이 동작, 화면은 콜백으로 결과를 받음)

순서: 신호 만들기 → [DPD OFF 측정] → 반복 { 받은 IQ 로 postdistorter 학습 (ILA) → 예왜곡 신호 보내기 → 측정 }
"""
import math
import os
import re
import time

import numpy as np

import bench_link as BL
import dpd_core as C
import instruments as I


class Stop(Exception):
    pass


DEFAULTS = dict(
    smw_host="demo", fsw_host="demo", file_max_msps=0.0, auto_model=True,
    model="gmp", method="ila", dvr_seg=8, torch_dir="", aclr_src="fsw", iq_avg=2, focus_mhz="", focus_w=4.0, ref_margin_db=3.0, fsw_park=True, nn_hidden=24, nn_epochs=30, ilc_iters=15, ilc_gain=0.8,
    freq_ghz=3.6, sg_dbm=-10.0, sg_max_dbm=0.0, in_loss_db=0.5, out_loss_db=40.0, cal_db=0.0,
    fsw_ref_dbm="auto", fsw_att_db="auto", fsw_rbw_khz=0.0,
    os=4, n_sym=28, mod="256QAM", cfr_on=True, papr_db=8.0,
    sig_src="nr", wv_path="", ch_bw_mhz=98.28, adj_mhz=100.0,
    rec=None, auto_cal=False,
    ec_iters=6, ec_mu=0.5, ila_guard_db=1.0, retry_off=2,
    K=7, M=4, L=1, odd_only=False, lam=1e-6, iters=4, mu=0.7, peak_lim_db=3.0, n_fit=40000, target_aclr=None,
)


MODEL_NAMES = {"gmp": "GMP", "sgmp": "구간 GMP", "dvr": "DVR", "nn": "신경망", "pnn": "위상정규 신경망", "tnn": "torch 신경망"}


def migrate_method(p):
    """예전 설정 (method = ila / dla / nn) → 모델 (model) + 학습 방식 (method = ila / dla)"""
    if p.get("method") == "nn":
        p["model"], p["method"] = "nn", "ila"
    p.setdefault("model", "gmp")
    if p.get("method") not in ("ila", "dla", "ilcm", "ilaec"):
        p["method"] = "ila"
    return p


def is_nn(p):
    return p.get("model") in ("nn", "pnn", "tnn")


def method_label(p):
    m = p.get("model", "gmp")
    if p.get("method") == "ilcm":
        return "%s-ILC→모델" % MODEL_NAMES.get(m, m)
    if is_nn(p):
        return MODEL_NAMES[m]
    return "%s-%s" % (MODEL_NAMES.get(m, m), {"dla": "DLA", "ilaec": "ILA+보정"}.get(p.get("method"), "ILA"))


class Engine:
    def __init__(self, p, log=None, on_result=None, on_progress=None, dc_reader=None):
        self.p = dict(DEFAULTS)
        self.p.update(p or {})
        migrate_method(self.p)
        self.log = log or (lambda s: None)
        self.on_result = on_result or (lambda r: None)
        self.on_progress = on_progress or (lambda s: None)
        self.dc_reader = dc_reader or (lambda: None)      # () → (Vdd V, Id A, 측정 시각, 출처) 또는 None
        self._cal_done = False
        self._calm_done = False
        self._calm_bad = False
        self.stop = False
        self.smw = self.fsw = None
        self.sig = None
        self.model = None
        self.G = None
        self.results = []
        self.tag = None            # 방식 비교 때 결과에 붙일 방식 이름
        self.compare = []

    # ------------------------------------------------------------ 준비
    def check(self):
        if self.stop:
            raise Stop()

    GUARD = r"C:\R_S\Instr\user\pa_dpd_guard.dfl"

    def fsw_guard_save(self):
        """[시작] 할 때 FSW 지금 설정 (사용자가 맞춰 둔 화면 · 채널) 을 FSW 안에 저장 → 측정 중 화면을 놓치면 되돌림"""
        self._guard = None
        if not self.p.get("fsw_guard", True) or self.fsw is None:
            return
        io = getattr(self.fsw, "io", None)
        if io is None:
            self._guard = "demo"
            return
        try:
            io.write("*CLS")
            io.write("MMEM:STOR1:STAT 1,'%s'" % self.GUARD)
            io.opc()
            self._guard = self.GUARD
            self.log("  FSW 지금 설정 저장 (측정 중 화면을 놓치면 이 설정으로 되돌리고 다시 잼)")
        except Exception as e:
            self.log("  ⚠ FSW 설정 저장 못 함 (%s) → 화면 놓침 자동 복구 없이 진행" % e)

    def fsw_guard_restore(self, why=""):
        """저장해 둔 FSW 설정으로 되돌림 (연결이 끊겼으면 다시 연결) → 프로그램이 만든 I/Q 채널 기억은 버리고 다음 측정 때 새로"""
        g = getattr(self, "_guard", None)
        if not g:
            return False
        self.log("  ↺ FSW 화면을 놓침%s → [시작] 때 FSW 설정으로 되돌리고 이 측정만 다시" % ((" (%s)" % why) if why else ""))
        f = self.fsw
        if g != "demo":
            ok = False
            for k in range(2):
                try:
                    if k == 1:                                  # 연결이 끊긴 것 같음 → 새로 연결
                        try:
                            f.close()
                        except Exception:
                            pass
                        fh, fp = I.clean_host(self.p["fsw_host"], int(self.p.get("fsw_port") or 5025))
                        nf = I.FSW(fh, fp)
                        nf.log = self.log
                        nf.prev = getattr(f, "prev", None)
                        for a_ in ("park", "acp_cfg", "span_fix"):
                            if hasattr(f, a_):
                                setattr(nf, a_, getattr(f, a_))
                        self.fsw = f = nf
                    io = f.io
                    try:
                        io.write("ABOR")
                    except Exception:
                        pass
                    io.write("*CLS")
                    io.write("MMEM:LOAD1:STAT 1,'%s'" % g)
                    io.opc()
                    io.write("SYST:DISP:UPD ON")
                    ok = True
                    break
                except Exception as e:
                    self.log("    FSW 되돌리기 %d 번째 실패: %s" % (k + 1, e))
            if not ok:
                return False
        for a_, v_ in (("via", None), ("_parked", False), ("tidied", False), ("made", False), ("fs", None)):
            try:
                setattr(f, a_, v_)
            except Exception:
                pass
        self._ref_mem = None                                    # FSW Ref 기억도 버림 → 자동 레벨부터
        self._guard_n = getattr(self, "_guard_n", 0) + 1
        time.sleep(0.5)
        return True

    def connect(self):
        self._guard_n = 0
        self._connect0()
        self.fsw_guard_save()

    def _connect0(self):
        if self.smw is None:
            self.log("장비 연결: SMW %s · FSW %s" % (self.p["smw_host"], self.p["fsw_host"]))
            self.smw, self.fsw = I.open_pair(self.p["smw_host"], self.p["fsw_host"],
                                             int(self.p.get("smw_port") or 5025), int(self.p.get("fsw_port") or 5025))
            self.smw.log = self.fsw.log = self.log
            self.log("  SMW: %s" % self.smw.idn)
            self.log("  FSW: %s" % self.fsw.idn)

    def make_signal(self):
        p = self.p
        t0 = time.time()
        if p.get("sig_src") == "file":
            self.sig = C.FileSignal(p["wv_path"], max_fs=float(p.get("file_max_msps") or 0) * 1e6 or None,
                                    up=int(p.get("file_up") or 1))
            if getattr(self.sig, "up", 1) > 1:
                self.log("신호 파일 %.2f MSps → ×%d = %.2f MSps 로 올려서 보냄 · 받음 (FSW I/Q 대역 ≈ ±%.0f MHz)"
                         % (self.sig.fs_file / 1e6, self.sig.up, self.sig.fs / 1e6, 0.4 * self.sig.fs / 1e6))
            if self.sig.drop_db is not None:
                self.log("신호 파일 %.2f MSps → %.2f MSps 로 줄여서 사용 (FSW 캡처 · 계산용, 잘린 대역 밖 성분 %.1f dB)"
                         % (self.sig.fs_file / 1e6, self.sig.fs / 1e6, self.sig.drop_db))
            self.log("신호: %s · %.2f MSps · %d 샘플 (%.3f ms) · PAPR %.2f dB - EVM 은 파형 비교"
                     % (p["wv_path"], self.sig.fs / 1e6, len(self.sig.x), len(self.sig.x) / self.sig.fs * 1e3, C.papr(self.sig.x)))
            if len(self.sig.x) > 2000000:
                self.log("  ⚠ 샘플이 많아 (%d) 한 번 측정에 시간이 걸립니다" % len(self.sig.x))
        else:
            self.sig = C.NRSignal(os=p["os"], n_sym=p["n_sym"], mod=p["mod"])
            pk0 = C.papr(self.sig.x)
            if p["cfr_on"]:
                pk = self.sig.cfr(p["papr_db"])
            else:
                pk = pk0
            self.log("신호: NR 100 MHz 1C · %s · %d 심볼 · %.2f MSps · %d 샘플 · PAPR %.2f → %.2f dB (%.1f초)"
                     % (p["mod"], p["n_sym"], self.sig.fs / 1e6, len(self.sig.x), pk0, pk, time.time() - t0))
        self.model = self.make_model()
        self.G = None
        self._plan = None
        self._wlog = False
        self._plog = False
        self._pk_note = False
        self._acp_warn = False
        self._bwlog = False
        self._calm_warn = False
        try:
            cars = C.find_carriers(self.sig.x, self.sig.fs)
            edge = max(abs(fc) + w / 2 for fc, w in cars) if cars else 0
            K = max(1, int(p.get("cap_up") or 1))
            if K > 1:
                self.log("  FSW 는 ×%d (%.2f MSps) 로 받음 → I/Q 대역 ≈ ±%.0f MHz (SMW 는 %.2f MSps 그대로 · 학습은 %.2f MSps)"
                         % (K, self.sig.fs * K / 1e6, 0.4 * self.sig.fs * K / 1e6, self.sig.fs / 1e6, self.sig.fs / 1e6))
            if edge > 0.4 * self.sig.fs * K:
                self.log("  ⚠ 신호 끝 (±%.0f MHz) 이 FSW I/Q 쓸 수 있는 대역 (≈ ±%.0f MHz = 0.8 × 샘플레이트) 보다 넓음 → "
                         "반송파 끝이 잘려 DPD · ACLR 이 틀림. 1 신호 샘플레이트 (400 MHz: '1000 MSps 넘으면 절반으로') 를 확인하세요" % (edge / 1e6, 0.4 * self.sig.fs * K / 1e6))
            if len(cars) > 1:
                bwm, adj = self.aclr_plan()[1:]
                self.log("  반송파 %d 개: %s → ACLR 은 반송파마다 양옆 (채널 %.2f MHz · 간격 %.1f MHz, 그 반송파 기준 dBc)"
                         % (len(cars), " · ".join("%+.1f MHz (폭 %.0f)" % (fc / 1e6, w / 1e6) for fc, w in cars),
                            bwm / 1e6, adj / 1e6))
        except Exception as ex:
            self.log("  (반송파 찾기 실패: %s)" % ex)
        return self.sig

    def aclr_plan(self):
        """ACLR · 집중 구간 기준 → (반송파 [(fc, 폭)], 채널 폭 Hz, 인접 간격 Hz)
        반송파 1 개 = 지금처럼 신호 가운데 (0 Hz) 기준 · 여러 개 = 반송파마다 그 중심 기준"""
        p = self.p
        bwc, adj = p.get("ch_bw_mhz", 98.28) * 1e6, p.get("adj_mhz", 100.0) * 1e6
        key = (id(self.sig), bwc, adj)
        if getattr(self, "_plan", None) and self._plan[0] == key:
            return self._plan[1]
        cars = C.find_carriers(self.sig.x, self.sig.fs) if self.sig is not None else []
        if len(cars) <= 1:
            plan = ([(0.0, bwc)], bwc, adj)
        else:
            occ = min(w for _f, w in cars)
            if bwc > occ * 1.05:                       # 채널 폭 칸이 반송파보다 넓음 (예: 400 MHz 칸에 100 MHz 반송파) → 반송파에 맞춤
                nom = C.nominal_bw(occ)
                plan = (cars, min(occ, nom * 0.9828), nom)
            else:
                plan = (cars, bwc, adj)
        self._plan = (key, plan)
        return plan

    def aclr_multi(self, f_hz, psd):
        """반송파 여러 개: → (아래 바깥, 위 바깥, 안쪽 [..], 이름 [..], 다 잼?) · 1 개면 None"""
        cars, bwm, adj = self.aclr_plan()
        if len(cars) <= 1:
            return None
        lst = C.aclr_carriers(f_hz, psd, cars, bwm, adj)
        if not lst:
            return None
        lo, hi = lst[0], lst[-1]
        inner = [d for d in lst if d is not lo and d is not hi]
        nm = lambda d: "C%d %s" % (d["car"] + 1, "아래" if d["side"] == "L" else "위")
        return (lo["dbc"], hi["dbc"], [d["dbc"] for d in inner], [nm(d) for d in inner],
                all(d["ok"] for d in lst), [(nm(d), d["f0"] / 1e6, d["dbc"]) for d in lst])

    def make_model(self, K=None, M=None, L=None):
        p = self.p
        K = p["K"] if K is None else K
        M = p["M"] if M is None else M
        lag = lead = None                          # GMP Lagging · Leading 따로 (4 카드, 빈칸 = L 과 같음)
        if L is None and not (p.get("auto_model") and p.get("model", "gmp") == "gmp"):
            lag, lead = p.get("L_lag"), p.get("L_lead")      # K · M · L 자동일 땐 자동이 고른 L 하나로 앞뒤 같게 (예전 그대로)
        L = p["L"] if L is None else L
        mdl = p.get("model", "gmp")
        if mdl == "tnn":
            import dpd_torch
            ok, msg = dpd_torch.load(p.get("torch_dir") or None)
            if ok:
                m = dpd_torch.TorchNNModel(M, int(p.get("nn_hidden", 24)), int(p.get("nn_epochs", 30)))
            else:
                # torch 가 없으면 멈추지 않고 numpy 위상정규 신경망으로 대신 (같은 구조 · 작고 느림)
                if not getattr(self, "_torch_warned", False):
                    self.log("  ⚠ torch 를 못 읽음 (%s) → numpy 위상정규 신경망으로 대신 돌림" % msg)
                    self._torch_warned = True
                m = C.NNModel(M, int(p.get("nn_hidden", 24)), int(p.get("nn_epochs", 30)), pn=True)
                m.fallback = True
        elif is_nn(p):
            m = C.NNModel(M, int(p.get("nn_hidden", 24)), int(p.get("nn_epochs", 30)), pn=(mdl == "pnn"))
        elif mdl == "dvr":
            m = C.DVRModel(int(p.get("dvr_seg", 8)), M, p["lam"])
        elif mdl == "sgmp":
            m = C.SegGMPModel(K, M, L, p["odd_only"], p["lam"], lag, lead)
        else:
            m = C.DPDModel(K, M, L, p["odd_only"], p["lam"], lag, lead)
        m.w = None
        return m

    # ------------------------------------------------------------ 한 번 측정
    def acp_cfg(self):
        """반송파 여러 개 (같은 간격 · 가운데 대칭) 면 FSW Mod. 화면 ACLR 설정 → dict, 아니면 None"""
        if not self.p.get("fsw_acp_auto", True):
            return None
        cars, bwm, adj = self.aclr_plan()
        if len(cars) < 2:
            # 반송파 1 개: Tx 1 개 (채널 폭) · 인접 (간격 = ACLR 간격) · Span = 바깥 인접 끝 × 1.75 → 화면 가운데에 신호 · 양옆 인접
            return dict(n=1, bw=round(bwm, -3), spac=0.0, abw=round(bwm, -3), asp=round(adj, -5),
                        span=math.ceil((adj + bwm / 2) * 2 * 1.75 / 10e6) * 10e6)
        fcs = [fc for fc, _w in cars]
        d = np.diff(fcs)
        if np.max(np.abs(d - d[0])) > 2e6 or abs(np.mean(fcs)) > 2e6:
            if not getattr(self, "_acp_warn", False):
                self._acp_warn = True
                self.log("  ⚠ 반송파 간격이 같지 않거나 가운데 대칭이 아님 → FSW Mod. 화면 ACLR 은 손대지 않음 (직접 맞춰 주세요)")
            return None
        edge = max(abs(f_) for f_ in fcs) + adj + bwm / 2
        span = math.ceil(edge * 2 * 1.2 / 50e6) * 50e6
        return dict(n=len(cars), bw=round(bwm, -3), spac=round(float(d[0]), -5), abw=round(bwm, -3), asp=round(adj, -5), span=span)

    def auto_summary(self):
        """실행 끝: auto (자동) 로 둔 설정이 실제로 어떤 값이 됐는지 로그에 모아서 → 다음에 직접 넣을 때 참고"""
        p, rs = self.p, [r for r in (self.results or []) if r.get("kind") in ("off", "dpd", "ilc")]
        if not rs:
            return
        last = rs[-1]
        is_auto = lambda k: str(p.get(k, "auto")).strip().lower() in ("auto", "")
        uniq = lambda vals, fmt: " / ".join(sorted({fmt % v for v in vals}, key=lambda t: float(t)))
        L = []
        refs = [r["fsw_ref"] for r in rs if isinstance(r.get("fsw_ref"), (int, float)) and r["fsw_ref"] == r["fsw_ref"]]
        if refs:
            L.append("FSW Ref Level%s: 처음 %.1f · 마지막 %.1f dBm" % (" (auto)" if is_auto("fsw_ref_dbm") else "", refs[0], refs[-1]))
        atts = [r["fsw_att"] for r in rs if isinstance(r.get("fsw_att"), (int, float))]
        if atts:
            L.append("FSW 감쇠 (I/Q)%s: %s dB" % (" (auto)" if is_auto("fsw_att_db") else "", uniq(atts, "%.0f")))
        matts = [r["mod_att"] for r in rs if isinstance(r.get("mod_att"), (int, float))]
        if matts:
            L.append("FSW 감쇠 (Mod. 화면): %s dB" % uniq(matts, "%.0f"))
        if getattr(self, "_mod_rbw", None):
            L.append("FSW RBW (Mod. 화면)%s: %.0f kHz" % (" (화면 그대로)" if not p.get("fsw_rbw_khz") else "", self._mod_rbw / 1e3))
        bws = [r["fsw_bw"] for r in rs if isinstance(r.get("fsw_bw"), (int, float)) and r["fsw_bw"] == r["fsw_bw"]]
        if bws:
            L.append("FSW I/Q 분석 대역 (샘플레이트 × 0.8): %.0f MHz · 신호 샘플레이트 %.2f MSps%s" % (
                bws[-1] / 1e6, self.sig.fs / 1e6,
                (" (파일 %.2f MSps 를 줄임)" % (self.sig.fs_file / 1e6)) if getattr(self.sig, "fs_file", self.sig.fs) != self.sig.fs else ""))
        try:
            cars, bwm, adj = self.aclr_plan()
            L.append("ACLR 채널 (자동): 반송파 %d 개%s · 채널 %.2f MHz · 간격 %.1f MHz" % (
                len(cars), (" (" + ", ".join("%+.0f" % (fc / 1e6) for fc, _w in cars) + " MHz)") if len(cars) > 1 else "", bwm / 1e6, adj / 1e6))
            if is_auto("pout_band"):
                L.append("Pout 대역 (auto): %s MHz" % ", ".join("%.1f~%.1f" % (a / 1e6, b / 1e6) for a, b in (self.pout_bands() or [])))
        except Exception:
            pass
        if p.get("cal_mod"):
            L.append("Pout 보정 (Mod. 화면에 맞춤%s): %.2f dB" % (" · 매 측정마다" if p.get("cal_every") else "", p.get("cal_db", 0.0)))
        if p.get("pout_lock") is not None:
            L.append("SG (목표 Pout %.2f dBm 에 맞춤): 기준 %.2f dBm · 마지막 측정 실제 %.2f dBm" % (
                float(p["pout_lock"]), p["sg_dbm"], last.get("sg", float("nan"))))
        if getattr(self, "chosen", None):
            L.append("DPD 모델 (자동): K%d M%d L%d" % tuple(self.chosen))
        rz = getattr(self, "_resume", None)
        if rz and rz.get("kind") == "ilc":
            L.append("ILC 이득: 처음 %.2f → 마지막 %.2f (나빠질 때 자동으로 반씩)" % (float(p.get("ilc_gain", 0.8)), rz.get("gk", 0)))
        self._auto_lines = list(L)                       # 설정 엑셀 저장 때 같이 (화면 · 측정과는 무관)
        if not L:
            return
        self.log("■ 자동으로 정해진 값 (다음에 직접 넣을 때 참고)")
        for l_ in L:
            self.log("   · " + l_)

    def _pout_read(self, pd, label, step, tgt, tol):
        """Pout 만 빠르게 재기 (기록 안 함). 목표 가까이 (3 × 허용 오차 안) 면 한 번 더 재서 평균 → 측정 흔들림에 덜 속음"""
        _y, r = self.measure(pd, label, -1, step=step, kind="lock")
        po = r["pout"]
        if po == po and abs(tgt - po) <= 3 * tol:
            self.check()
            _y2, r2 = self.measure(pd, label + " (한 번 더)", -1, step=step, kind="lock")
            if r2["pout"] == r2["pout"]:
                self.log("    Pout 두 번 평균: %.3f · %.3f → %.3f dBm" % (po, r2["pout"], (po + r2["pout"]) / 2))
                po = (po + r2["pout"]) / 2
        return po

    def gain_ok(self):
        """목표 Pout 을 맞췄을 때의 Gain 기억 (다음에 SG 최대인데 Pout 이 안 나오면 비교용 · 프로그램 켜 있는 동안)"""
        r = getattr(self, "_last_r", None)
        if r and r.get("gain") == r.get("gain"):
            self._g_ok = (float(r["gain"]), float(self.p.get("freq_ghz", 0)), time.strftime("%H:%M"))

    def gain_note(self, tgt, po):
        """SG 최대인데 원하는 Pout 이 안 나옴 → 지금 Gain 을 로그에 (SMA 커넥터 · 케이블 문제 확인용)"""
        r = getattr(self, "_last_r", None)
        if not r or r.get("gain") != r.get("gain"):
            return
        if time.time() - getattr(self, "_gn_t", 0) < 20:
            return
        self._gn_t = time.time()
        g = float(r["gain"])
        po = po if po == po else r.get("pout", float("nan"))
        self.log("  ⚠ Gain %.2f dB  (SG 최대 %.2f dBm · Pin %.2f dBm → Pout %.2f dBm, 목표 %.2f 보다 %.2f dB 모자람)"
                 % (g, float(self.p.get("sg_max_dbm", 0)), r.get("pin", float("nan")), po, tgt, tgt - po))
        ok = getattr(self, "_g_ok", None)
        if ok and abs(ok[1] - float(self.p.get("freq_ghz", 0))) < 1e-6 and ok[0] - g > 0.5:
            self.log("    목표 맞췄을 때 (%s) Gain %.2f dB 보다 %.1f dB 낮음 → SMA 커넥터 · 케이블 · 감쇠기 · 손실 칸 · 바이어스 확인"
                     % (ok[2], ok[0], ok[0] - g))
        else:
            self.log("    평소 Gain 보다 낮으면 SMA 커넥터 · 케이블 · 손실 칸 · 바이어스 확인 · 아니면 2 카드 SG 최대 ↑")

    def hold_pout(self, pd, label):
        """목표 Pout 딱 맞추기: 기록할 측정 전에 이 입력으로 Pout 만 빠르게 재서 (I/Q, 기록 안 함)
        허용 오차를 벗어나면 SG 를 고쳐 다시 - 맞으면 그 SG 로 기록 측정"""
        p = self.p
        try:
            tgt, tol = float(p.get("pout_lock")), float(p.get("pout_tol") or 0.1)
        except (TypeError, ValueError):
            return
        sg_max = float(p.get("sg_max_dbm", 0.0))
        prev = None
        po0 = None
        for k in range(7):                                  # 마지막으로 고친 SG 도 꼭 다시 재서 확인
            self.check()
            po = self._pout_read(pd, label + " · Pout 확인", "SG %.2f dBm" % p["sg_dbm"], tgt, tol)
            if k == 6 and abs(tgt - po) > tol:
                self.log("    ⚠ Pout 딱 맞추기: 6 번 고쳐도 ±%.2f dB 밖 (%.2f dBm) → 이 SG 로 기록 (허용 오차를 조금 넓히거나 기다림 ↑)" % (tol, po))
                return
            if po0 is None:
                po0 = po
            d = tgt - po
            if abs(d) <= tol:
                self.gain_ok()
                if k:
                    self.log("    Pout 딱 맞춤: %.2f dBm (목표 %.2f ±%.2f) · SG 기준 %.2f dBm" % (po, tgt, tol, p["sg_dbm"]))
                    if self.G is not None:             # DPD · ILC 의 목표 이득도 같이 → 다음 반복이 다시 벗어나지 않게
                        self.G = self.G * 10 ** ((po - po0) / 20)
                return
            slope = 1.0
            if prev is not None and abs(p["sg_dbm"] - prev[0]) > 0.005:
                slope = min(max((po - prev[1]) / (p["sg_dbm"] - prev[0]), 0.3), 1.5)
            prev = (p["sg_dbm"], po)
            new = round(p["sg_dbm"] + min(max(d / slope, -2.0), 2.0), 3)
            if new > sg_max:
                new = sg_max
            if abs(new - p["sg_dbm"]) < 0.001:
                self.log("    ⚠ Pout 딱 맞추기: SG 최대에 걸림 (%.2f dBm)" % sg_max)
                self.gain_note(tgt, po)
                return
            self.log("    Pout %.2f dBm (목표와 %+.2f) → SG 기준 %.2f → %.2f dBm" % (po, -d, p["sg_dbm"], new))
            p["sg_dbm"] = new

    def measure(self, pd, label, it=0, nmse=None, step="", kind=None):
        """측정 하나. FSW 응답 · 데이터 오류로 실패하면 [시작] 때 FSW 설정으로 되돌리고 한 번 다시 (실행마다 최대 5 번)"""
        if getattr(self, "_guard_busy", False):
            return self._measure1(pd, label, it, nmse, step, kind)
        try:
            return self._measure1(pd, label, it, nmse, step, kind)
        except Stop:
            raise
        except Exception as e:
            if self.stop or getattr(self, "_guard_n", 0) >= 5 or not getattr(self, "_guard", None):
                raise
            self.log("  ⚠ 측정 실패: %s: %s" % (type(e).__name__, e))
            if not self.fsw_guard_restore(type(e).__name__):
                raise
            self._guard_busy = True
            try:
                return self._measure1(pd, label, it, nmse, step, kind)
            finally:
                self._guard_busy = False

    def _measure1(self, pd, label, it=0, nmse=None, step="", kind=None):
        p, sig, x = self.p, self.sig, self.sig.x
        t_meas = time.time()
        if kind is None:                               # DPD 반복은 kind 없이 불림 → 여기서 정함 (예전: 그래서 GMP 는 Pout 유지 · 딱 맞춤이 안 걸렸음, ILC 만 됨)
            kind = "off" if it == 0 else "dpd"
        if kind in ("dpd", "ilc") and p.get("pout_strict") and p.get("pout_lock") is not None:
            self.hold_pout(pd, label)                  # 기록 전에 목표 Pout 허용 오차 안으로
        if self.fsw is not None:
            try:
                self.fsw.acp_cfg = self.acp_cfg()
                cars_s, bwm_s, adj_s = self.aclr_plan()       # Zero span 일 때 쓸 Span (바깥 인접 채널 끝 × 1.75)
                self.fsw.span_fix = math.ceil((max(abs(c_) for c_, _w in cars_s) + adj_s + bwm_s / 2) * 2 * 1.75 / 10e6) * 10e6
            except Exception:
                pass
        self.cur = dict(label=label, step=step, t=t_meas)
        et = self.eta_text()
        self.on_progress("%s측정 중 · %s%s" % ((et + " · ") if et else "", label, (" · " + step) if step else ""))
        lvl = p["sg_dbm"] + 20 * math.log10(C.rms(pd) / C.rms(x))      # 원래 신호와 같은 크기 비율로 (SMW 는 RMS 를 레벨에 맞춤)
        clipped = False
        if lvl > p["sg_max_dbm"]:
            self.log("  ⚠ SG 레벨 %.2f dBm > 최대 %.2f dBm → 최대로 제한" % (lvl, p["sg_max_dbm"]))
            lvl, clipped = p["sg_max_dbm"], True
        self.check()
        wv = C.write_wv(None, pd, sig.fs, comment="PA DPD Tester %s" % label)
        self.log("  %s: SG %.2f dBm 로 신호 보내는 중 (%.1f MB)…" % (label, lvl, len(wv) / 1e6))
        self.smw.load_wave(wv, p["freq_ghz"] * 1e9, lvl, rf_on=True)
        self._sent_pd = pd                                 # JSH: 지금 SG 가 내보내는 신호 (끝에 가장 좋았던 것으로 돌아갔는지 확인용)
        self.check()
        w_set = float(p.get("settle_s", 0) or 0)          # SG 신호 · 레벨 바꾼 뒤 PA · 전류가 자리 잡을 때까지 (2 카드 '읽기 전 기다림')
        if w_set > 0:
            t_end = time.time() + min(w_set, 30.0)
            while time.time() < t_end:
                self.check()
                time.sleep(min(0.1, max(0.0, t_end - time.time())))
        att = None if str(p["fsw_att_db"]).strip().lower() in ("auto", "") else float(p["fsw_att_db"])
        ref_auto = str(p.get("fsw_ref_dbm", "auto")).strip().lower() in ("auto", "")
        ref = self._next_ref(pd, lvl) if ref_auto else float(p["fsw_ref_dbm"])
        self.fsw.park = bool(p.get("fsw_park", True))      # 받은 뒤 FSW 화면을 Mod. 화면에 둘지 (화면 정리 때도 씀)
        rbw = float(p.get("fsw_rbw_khz") or 0) * 1e3 or None
        K = max(1, int(p.get("cap_up") or 1))          # FSW 만 K 배 빠르게 받음 (SMW 는 신호 샘플레이트 그대로) → I/Q 대역 넓게
        bw = self.fsw.setup(p["freq_ghz"] * 1e9, sig.fs * K, ref if ref == ref else 10.0, att, rbw)
        if ref_auto and ref != ref:
            # 처음 (이 실행 · 이 레벨에서 아직 모름): FSW 자동 레벨로 Ref Level · 감쇠를 신호에 맞춤 → ADC 를 꽉 채워 잡음 바닥 ↓
            al_ = self.fsw.auto_level()
            if al_:
                ref = al_[0]
                self.log("  FSW 자동 레벨: Ref %.1f dBm · 감쇠 %.0f dB" % al_)
            else:
                ref = 10.0
                self.log("  ⚠ FSW 자동 레벨 안 됨 → Ref 10 dBm 로 한 번 받아 보고 맞춤")
        k_avg = max(1, int(p.get("iq_avg", 2)))
        L = len(pd)
        CAP = 4000000                                  # 한 번에 받는 최대 샘플 (FSW 메모리 · 전송 시간) - 넘으면 여러 번 받아 평균
        per_cap = max(1, min(k_avg, CAP // max(L * K, 1)))
        n = per_cap * L + 2000 if per_cap > 1 else L + 20000
        y = None
        m_ref = float(p.get("ref_margin_db", 3.0) or 3.0)      # Ref 여유 (최고값 위로)
        extra = getattr(self, "_ref_extra", 0.0)                 # 과입력 (IF OVLD) 으로 더 올린 만큼 (이 실행 동안 기억)
        for attempt in range(4):
            time.sleep(0.2)
            t_cap = time.time()
            att_now, att_auto = (self.fsw.get_att() if hasattr(self.fsw, "get_att") else (None, None))
            self.log("  FSW I/Q 받는 중: %d 샘플 · %.2f MSps (%.2f ms, %.1f MB) · Ref %.1f dBm%s…"
                     % (n * K, sig.fs * K / 1e6, n / sig.fs * 1e3, n * K * 8 / 1e6, ref,
                        (" · 감쇠 %.0f dB%s" % (att_now, " (auto)" if att_auto else "")) if att_now is not None else ""))
            self._att_now = att_now
            self.fsw.overload(clear_only=True)
            y = self.fsw.capture(n * K, (L + 20000) * K)
            y_wide = y
            if K > 1:
                y = C.down_k(y, K)                     # 학습 · 정렬은 신호 샘플레이트로 (넓게 받은 것은 스펙트럼 · ACLR 에)
            if len(y) < n:
                self.log("    FSW 한 번에 %d 샘플만 받음 (한 주기 + 여유로 계산)" % len(y))
            self.check()
            pk = 10 * np.log10(max(float(np.max(np.abs(y)) ** 2) / (2 * 50.0), 1e-30) * 1e3)   # FSW 입력 최고 전력 dBm
            self._ref_mem = (pk, C.papr(pd), lvl)
            ov = self.fsw.overload()
            if ov:
                # FSW 과입력 → 그 I/Q 는 FSW 자체 왜곡이 섞임 (DPD 가 그걸 고치려다 막힘) → Ref ↑ (IF) · 감쇠 ↑ (RF) 하고 다시
                if ref_auto and attempt < 3:
                    extra += 3.0
                    self._ref_extra = extra
                    ref += 3.0
                    if "RF" in ov and att is None:
                        self.log("  ⚠ FSW %s → Ref +3 dB (%.1f dBm) 로 다시 받음 (감쇠 auto 라 같이 올라감)" % (ov, ref))
                    else:
                        self.log("  ⚠ FSW %s → Ref +3 dB (%.1f dBm) 로 다시 받음" % (ov, ref))
                    self.fsw.set_ref(ref)
                    if att is not None:
                        self.fsw.io.write("INP:ATT %.0f" % att)
                    continue
                self.log("  ⚠ FSW %s 인 채로 받음 - I/Q 에 FSW 왜곡이 섞임 (Ref Level · 감쇠 ↑)" % ov)
                break
            if not ref_auto or attempt >= 1:
                if pk > ref - 0.2:                     # FSW 과입력 (IF · RF OVLD) 은 위에서 이미 확인 → 여기는 없을 때
                    if not getattr(self, "_pk_note", False):
                        self._pk_note = True
                        self.log("  (참고) FSW 입력 최고값 %.1f dBm 이 Ref %.1f dBm 보다 %.1f dB 위 · FSW 과입력 (OVLD) 없음 → 괜찮음"
                                 % (pk, ref, pk - ref))
                break
            if pk > ref - 0.3 or pk < ref - 6.0 - extra:
                new = pk + (m_ref if pk <= ref - 0.3 else m_ref + 1.5) + extra
                self.log("  FSW 입력 최고 %.1f dBm · Ref %.1f → %.1f dBm 로 맞춰 다시 받음" % (pk, ref, new))
                ref = new
                self.fsw.set_ref(ref)
                continue
            break
        self.fsw.park = bool(p.get("fsw_park", True))
        k_avg_need = k_avg > per_cap                      # 여러 번 받아야 하면 다 받은 뒤에 화면을 돌림
        if not k_avg_need:
            self.fsw.park_spec(p["freq_ghz"] * 1e9)        # FSW 화면을 Mod. 화면 (ACLR 스펙트럼) 으로
        # Vdd 전원 전류: 이 신호를 보낸 뒤에 읽은 값 (최대 1.5초 기다림)
        dc = None
        t_end = time.time() + 1.5
        while True:
            try:
                dc = self.dc_reader()
            except Exception:
                dc = None
            if dc is None or dc[2] >= t_cap or time.time() > t_end or self.stop:
                break
            time.sleep(0.1)
        y_al, g, d = C.align(pd, y)
        n_per = 1
        acc = y_al.copy()
        n_cap = 1
        while True:
            # 받은 I/Q 안의 주기마다 맞춰 평균 → 측정 잡음 10·log(주기 수) dB 줄어듦 (ILC · DLA 의 바닥이 낮아짐)
            k0 = 1 if n_cap == 1 else 0
            for kk in range(k0, per_cap + 1):
                if n_per >= k_avg or len(y) < (kk + 1) * L + 1000 and not (kk == 0 and len(y) >= L):
                    break
                try:
                    y2, _, _ = C.align(pd, y[kk * L:])
                    acc += y2 * np.exp(1j * np.angle(np.vdot(y2, y_al)))
                    n_per += 1
                except Exception:
                    break
            if n_per >= k_avg or n_cap >= 2 * k_avg:
                break
            self.check()                                # 모자라면 한 번 더 받음 (SMW 는 같은 신호를 계속 내보내는 중)
            y = self.fsw.capture(n * K, (L + 20000) * K)
            if K > 1:
                y = C.down_k(y, K)
            n_cap += 1
        if n_cap > 1:
            self.log("    I/Q 평균 %d 주기 (%d 번 받음)" % (n_per, n_cap))
        if k_avg_need:
            self.fsw.park_spec(p["freq_ghz"] * 1e9)
        y_al = acc / n_per
        if self.G is None:
            self.G = C.small_signal_gain(x, y_al)
        else:
            # 측정마다 SMW · FSW 사이 위상이 달라짐 → 작은 신호 구간 위상을 처음 측정 (DPD OFF) 에 맞춤
            c = C.small_signal_gain(pd, y_al)
            y_al = y_al * np.exp(1j * (np.angle(self.G) - np.angle(c)))
        pout = C.iq_power_dbm(y_al, p["out_loss_db"] + p["cal_db"])
        # Pout = 채널 안 (반송파 대역) 전력만: 받은 대역 전체 전력 × (대역 안 PSD / 전체 PSD)
        try:
            bands = self.pout_bands()
            if bands:
                y_pb, fs_pb = (y_wide, sig.fs * K) if K > 1 else (y_al, sig.fs)
                f_pb, psd_pb = C.welch_psd(y_pb, fs_pb)
                m_pb = np.zeros(len(f_pb), bool)
                for lo_, hi_ in bands:
                    m_pb |= (f_pb >= lo_) & (f_pb <= hi_)
                frac = float(np.sum(psd_pb[m_pb]) / np.sum(psd_pb))
                if frac > 0:
                    d_pb = 10 * np.log10(frac)
                    if not getattr(self, "_plog", False):
                        self._plog = True
                        self.log("  Pout = 채널 안 전력만: %s MHz (받은 대역 전체보다 %.2f dB)"
                                 % (", ".join("%.1f~%.1f" % (a_ / 1e6, b_ / 1e6) for a_, b_ in bands), d_pb))
                    pout += d_pb
        except Exception as ex:
            self.log("  (Pout 대역 계산 실패 → 전체 전력: %s)" % ex)
        rec = p.get("rec")
        rec_p = BL.pout_at_sg(rec, lvl) if rec else float("nan")
        if rec and p.get("auto_cal") and not p.get("cal_mod") and it in (0, -1) and not self._cal_done and math.isfinite(rec_p):
            self._cal_done = True
            # Bench 기록 (같은 SG 레벨의 DUT Pout) 에 맞춰 Pout 보정값을 정함 → 이후 측정도 같은 보정
            d = rec_p - pout
            p["cal_db"] = p["cal_db"] + d
            pout = rec_p
            self.log("  Pout 보정 = %.2f dB (Bench 기록: SG %.2f dBm → DUT Pout %.2f dBm 에 맞춤)" % (p["cal_db"], lvl, rec_p))
        bwc, adj = p.get("ch_bw_mhz", 98.28) * 1e6, p.get("adj_mhz", 100.0) * 1e6
        if not getattr(self, "_bwlog", False) and bw == bw and bw > 0:
            self._bwlog = True
            try:
                cars_b, bwm_b, _a = self.aclr_plan()
                edge_b = max(abs(fc) for fc, _w in cars_b) + bwm_b / 2
            except Exception:
                edge_b = float("nan")
            self.log("  FSW I/Q 분석 대역 %.0f MHz (±%.0f) · 신호 끝 ±%.0f MHz%s" % (
                bw / 1e6, bw / 2e6, edge_b / 1e6,
                "  ⚠ 신호가 잘림 → Pout 낮게 · DPD 틀림 (FSW 대역 옵션 / 샘플레이트 확인)" if edge_b == edge_b and bw / 2 < edge_b else ""))
        y_sp, fs_sp = (y_wide, sig.fs * K) if K > 1 else (y_al, sig.fs)   # 스펙트럼 · I/Q ACLR = 넓게 받은 그대로 (정렬 필요 없음)
        al, au, full = C.aclr(y_sp, fs_sp, bwc, adj)
        why = ""
        if bw == bw and bw > 0 and bw / 2 < adj + bwc / 2:
            full = False                           # FSW 분석 대역폭이 인접 채널 끝 (±149.14 MHz) 보다 좁음
            why = "FSW I/Q 대역 ±%.0f < 인접 채널 끝 ±%.0f MHz" % (bw / 2e6, (adj + bwc / 2) / 1e6)
        a_in = a_nm = a_pos = None
        mc = self.aclr_multi(*C.welch_psd(y_sp, fs_sp))
        if mc is not None:                         # 반송파 여러 개: 반송파마다 양옆
            al, au, a_in, a_nm, full, a_pos = mc
            cars_, bwm_, adj_ = self.aclr_plan()
            edge = max(abs(fc) for fc, _w in cars_) + adj_ + bwm_ / 2
            why = "" if full else "샘플레이트 ±%.0f < 바깥 인접 채널 끝 ±%.0f MHz" % (fs_sp / 2e6, edge / 1e6)
            if bw == bw and bw > 0 and bw / 2 < edge:
                full = False
                why = "FSW I/Q 대역 ±%.0f < 바깥 인접 채널 끝 ±%.0f MHz (I/Q 로 계산한 값)" % (bw / 2e6, edge / 1e6)
        a_in_iq = list(a_in) if a_in is not None else None
        ev = C.evm_any(sig, y_al)
        f, sdb = C.spectrum_db(y_sp, fs_sp)
        f = f / 1e6
        iq_f, iq_db = np.asarray(f), np.asarray(sdb)
        mod_f = mod_db = None
        al_iq, au_iq, src_used = float(al), float(au), "iq"
        src_set = p.get("aclr_src", "fsw")
        use_mod = (src_set == "fsw" or (src_set == "fsw_end" and (it == 0 or getattr(self, "_force_mod", False)))) and kind != "lock"
        need_calm = bool(p.get("cal_mod")) and src_set != "iq" and not getattr(self, "_calm_bad", False) and \
            (not getattr(self, "_calm_done", False) or bool(p.get("cal_every")))   # 매번: 측정마다 스펙트럼을 읽어 보정 다시
        pout_spec = float("nan")                       # FSW 스펙트럼 (Mod. 화면) Tx 합 + 출력 손실 - 읽은 측정만
        mod_att_ = None
        aclr_mod = use_mod                             # ACLR 을 Mod. 화면 값으로 쓸 측정 ('ACLR 읽기' 설정대로)
        iq_keep = (al, au, full, a_in, a_nm, a_pos)    # Pout 맞춤 때문에만 Mod. 화면을 읽으면 ACLR 은 I/Q 값 그대로
        if need_calm:
            use_mod = True                             # Pout 을 FSW Mod. 화면 채널 전력에 맞추는 첫 측정 (Pout 맞추기 첫 번째 포함)
        if p.get("mod_every") and src_set != "iq" and kind != "lock":
            use_mod = True                             # 그래프를 ACLR (Mod. 화면) 스펙트럼으로 → 반복마다 Mod. 화면 한 번 (약 1 초)
        if use_mod and not getattr(self, "_mod_fail", False):
            # ACLR 은 FSW Mod. 화면 (사용자가 맞춘 스펙트럼 ACLR 측정, 설정 그대로) 에서 → I/Q 잡음 바닥에 안 막힘
            try:
                self.check()
                m = self.fsw.mod_aclr(p["freq_ghz"] * 1e9, rbw, att)
                al, au, full, src_used = m["aclr_l"], m["aclr_u"], True, "fsw"
                mod_att_ = m.get("att")
                if m.get("rbw"):
                    self._mod_rbw = m["rbw"]
                if mod_att_ is not None and mod_att_ != getattr(self, "_mod_att_logged", None):
                    self._mod_att_logged = mod_att_
                    self.log("    FSW Mod. 화면 감쇠 %.0f dB" % mod_att_)
                pm_ = self.mod_pout(m)
                if pm_ is not None:
                    pm_fsw = pm_                                # FSW 입력 기준 Tx (합)
                    pm_ += p["out_loss_db"]                     # DUT Pout (Pout 보정은 안 더함 - 더하면 실행마다 보정이 쌓임)
                    tl_ = m.get("tx_list") or []
                    txt_tx = ("Tx %d 개 합 (%s dBm → 합 %.2f dBm)" % (len(tl_), " + ".join("%.2f" % t for t in tl_), pm_fsw)
                              if len(tl_) > 1 else "Tx %.2f dBm" % pm_fsw)
                    txt_tx += " + 출력 손실 %.2f dB" % p["out_loss_db"]
                    if need_calm and abs(pm_ - pout) > 6.0:
                        self._calm_done = True
                        self._calm_bad = True
                        self.log("  ⚠ FSW Mod. 화면 %s = %.2f dBm 이 I/Q Pout %.2f dBm 과 %.1f dB 차이 → 너무 커서 맞추지 않음"
                                 " (FSW Ref Level Offset · Tx 채널 설정 확인)" % (txt_tx, pm_, pout, pm_ - pout))
                    elif need_calm and getattr(self, "_calm_done", False):
                        d_ = pm_ - pout                         # 매번 보정: 이번 측정 차이만큼 다시
                        p["cal_db"] = round(p["cal_db"] + d_, 3)
                        c0_ = p["cal_db"] - d_
                        self.log("    Pout 보정 → %.2f dB = I/Q 원래 %.2f → %s = %.2f dBm 차이 (지난 보정 %.2f 에서 이번 %+.2f dB)"
                                 % (p["cal_db"], pout - c0_, txt_tx, pm_, c0_, d_))
                        pout = pm_
                    elif need_calm:
                        d_ = pm_ - pout                         # pout 은 지금 보정 포함 → 새 보정 = 지금 보정 + 차이 (절대값, 안 쌓임)
                        p["cal_db"] = round(p["cal_db"] + d_, 3)
                        self._calm_done = True
                        c0_ = p["cal_db"] - d_
                        self.log("  Pout 보정 = %.2f dB (I/Q 원래 %.2f dBm → FSW Mod. 화면 %s = %.2f dBm 에 맞춤, 이후 측정도 같은 보정)"
                                 % (p["cal_db"], pout - c0_, txt_tx, pm_))
                        pout = pm_
                    else:
                        self.log("    Pout 비교: I/Q 원래 %.2f + 보정 %.2f = %.2f · FSW Mod. 화면 %s = %.2f dBm (차이 %+.2f dB)"
                                 % (pout - p["cal_db"], p["cal_db"], pout, txt_tx, pm_, pm_ - pout))
                    pout_spec = pm_
                elif need_calm and not getattr(self, "_calm_warn", False):
                    self._calm_warn = True
                    self.log("  ⚠ FSW Mod. 화면 Tx 채널 전력을 못 읽음 → Pout 맞춤 안 함")
                if m.get("f"):
                    f, sdb = np.asarray(m["f"]), np.asarray(m["db"])
                    mod_f, mod_db = f, sdb
                if mc is not None:
                    # 반송파 여러 개: FSW 결과 순서가 반송파 수에 따라 달라짐 → FSW 트레이스를 반송파마다 직접 적분
                    if not getattr(self, "_mc_raw_logged", False):
                        self._mc_raw_logged = True
                        self.log("    FSW ACP 결과 원래 값: %s (참고 · 반송파 여러 개는 트레이스로 계산)" % m.get("raw"))
                    if m.get("f"):
                        lin = 10 ** (np.asarray(m["db"]) / 10)
                        mf = self.aclr_multi(np.asarray(m["f"]) * 1e6, lin)
                        if mf is not None:
                            al, au, a_in, a_nm, full, a_pos = mf
                            fr_ = np.asarray(m["f"])
                            why = "" if full else "FSW Mod. 화면 Span %.0f~%.0f MHz < 바깥 인접 채널 끝 ±%.0f MHz (Span 넓히기)" % (
                                fr_[0], fr_[-1], (max(abs(fc) for fc, _w in self.aclr_plan()[0]) + self.aclr_plan()[2] + self.aclr_plan()[1] / 2) / 1e6)
                        else:
                            al, au, src_used = al_iq, au_iq, "iq"
                    else:
                        al, au, src_used = al_iq, au_iq, "iq"
                        self.log("  ⚠ FSW 트레이스를 못 읽어 반송파 ACLR 은 I/Q 계산 값")
            except Stop:
                raise
            except Exception as ex:
                self._mod_fail = True
                self.log("  ⚠ FSW Mod. 화면 ACLR 못 읽음 → 이번 실행은 I/Q 계산 ACLR 로 (%s)" % ex)
                self.fsw._parked = getattr(self.fsw, "_parked", False)
                self.fsw.select_iq(sig.fs)
        mod_lu = (float(al), float(au)) if src_used == "fsw" else (float("nan"), float("nan"))   # 이번에 읽은 장비 (Mod. 화면) ACLR - 표에 같이
        if not aclr_mod and src_used == "fsw":
            al, au, full, a_in, a_nm, a_pos = iq_keep  # 이번 측정 ACLR 은 I/Q (스펙트럼은 Pout 맞춤 · 그래프용으로만 읽음)
            src_used = "iq"
        xin, gam, pm = C.am_curves(x, y_al, self.G)
        pin = lvl - p["in_loss_db"]
        pout_iq = pout                                 # I/Q + Pout 보정
        pout_used = "iq"
        if p.get("pout_src") == "spec" and pout_spec == pout_spec:
            pout, pout_used = pout_spec, "spec"        # 스펙트럼을 읽은 측정은 Pout = FSW Tx 합 + 출력 손실 그대로
        vdd = idd = pdc = de = pae = float("nan")
        src = ""
        if dc is None and isinstance(self.smw, I.DemoSMW):
            # 데모: 가상 전원 (Vdd 48 V, Doherty 비슷한 효율 곡선)
            po_w = 10 ** ((pout - 30) / 10)
            eff = 0.18 + 0.40 * min(1.0, (po_w / 8.0) ** 0.45)
            dc = (48.0, po_w / eff / 48.0 + 0.25, time.time(), "demo")
        if dc is not None and dc[0] is not None and dc[1] is not None:
            vdd, idd, _t, src = dc
            pdc = vdd * idd
            if pdc > 0:
                po_w = 10 ** ((pout - 30) / 10)
                de = 100 * po_w / pdc
                pae = 100 * (po_w - 10 ** ((pin - 30) / 10)) / pdc
        de_of = lambda po: (100 * 10 ** ((po - 30) / 10) / pdc) if (pdc == pdc and pdc > 0 and po == po) else float("nan")
        r = dict(label=label, it=it, sg=lvl, pin=pin, pout=pout, gain=pout - pin, aclr_l=float(al), aclr_u=float(au),
                 aclr_full=bool(full), evm=ev, rec_pout=rec_p, cal_db=p["cal_db"], ch_bw=bwc / 1e6, adj=adj / 1e6,
                 evm_kind="OFDM" if getattr(sig, "X", None) is not None else "파형", papr=C.papr(pd), nmse=nmse, delay=d, fsw_bw=bw, clipped=clipped,
                 spec_f=np.asarray(f).tolist(), spec_db=np.asarray(sdb).tolist(), aclr_src=src_used,
                 iq_f=iq_f.tolist(), iq_db=iq_db.tolist(),
                 mod_f=None if mod_f is None else np.asarray(mod_f).tolist(), mod_db=None if mod_db is None else np.asarray(mod_db).tolist(),
                 aclr_iq_l=al_iq, aclr_iq_u=au_iq, aclr_mod_l=mod_lu[0], aclr_mod_u=mod_lu[1], aclr_in=a_in, aclr_in_nm=a_nm, aclr_iq_in=a_in_iq, aclr_pos=a_pos, n_avg=n_per, fsw_ref=ref, am_x=xin.tolist(), am_g=gam.tolist(), am_p=pm.tolist(),
                 t=time.strftime("%H:%M:%S"), kind=kind or ("off" if it == 0 else "dpd"), step=step, base_sg=p["sg_dbm"],
                 vdd=vdd, id=idd, pdc=pdc, de=de, pae=pae, dc_src=src, method=self.tag,
                 pout_iq=pout_iq, pout_spec=pout_spec, pout_used=pout_used, de_iq=de_of(pout_iq), de_spec=de_of(pout_spec),
                 fsw_att=getattr(self, "_att_now", None), mod_att=mod_att_)
        self._last_r = r
        if kind == "off":                           # JSH: 'DPD OFF 보다 나쁘면 다시' 기준 (같은 SG 일 때만 씀)
            self._off_ref = (C.aclr_worst(r), y_al, p["sg_dbm"])
        # 목표 Pout 고정: DPD · ILC 는 '작은 신호 이득' 직선으로 맞추려 해서 반복할수록 Pout 이 올라감
        # → 목표와의 차이만큼 목표 이득 (G) 을 줄이거나 키워 다음 반복이 목표 Pout 으로 가게 (SG 는 그대로 고정)
        tgt_ = p.get("pout_lock")
        if tgt_ is not None and tgt_ == tgt_ and kind in ("dpd", "ilc") and self.G is not None and pout == pout:
            d_ = float(tgt_) - pout
            if abs(d_) > 0.05:
                k_ = 0.8 * d_
                self.G = self.G * 10 ** (k_ / 20)
                self.log("    Pout 유지: 목표 %.2f · 지금 %.2f dBm → 목표 이득 %+.2f dB (다음 반복에 반영)" % (tgt_, pout, k_))
        if a_pos:                                   # 반송파 여러 개: 그래프용 반송파 위치 · 실제 쓴 채널 폭 · 간격
            cars_, bwm_, adj_ = self.aclr_plan()
            r["cars"], r["ch_bw"], r["adj"] = [fc / 1e6 for fc, _w in cars_], bwm_ / 1e6, adj_ / 1e6
        fd = self.focus_dbc(y_al) if self.focus_band() else None
        if fd is not None:
            r["focus_l"], r["focus_u"] = float(fd[0]), float(fd[1])
        if kind == "lock":                          # Pout 맞추기: 결과 표 · 기록에는 안 넣음
            return y_al, r
        # 이상 측정: 앞 측정보다 Gain 이 갑자기 크게 떨어지면 (PA 가 순간 이상 · 바이어스 · 연결) 버리고 한 번 다시 → 또 그러면 멈춤
        lim_g = float(p.get("bad_gain_db", 3.0) or 0)
        prev_r = next((q for q in reversed(self.results) if q.get("kind") in ("off", "dpd", "ilc")), None)
        if lim_g > 0 and kind in ("dpd", "ilc") and prev_r is not None and r["gain"] == r["gain"] \
                and prev_r.get("gain") == prev_r.get("gain") and prev_r["gain"] - r["gain"] > lim_g:
            msg = "Gain %.2f → %.2f dB (−%.1f) · Pout %.2f → %.2f dBm" % (prev_r["gain"], r["gain"], prev_r["gain"] - r["gain"],
                                                                     prev_r["pout"], r["pout"])
            if not getattr(self, "_bad_retry", False):
                self.log("  ⚠ 이상 측정 (%s) → 이 데이터는 버리고 같은 입력으로 다시 잼" % msg)
                if getattr(self, "_guard_n", 0) < 5:
                    self.fsw_guard_restore("Gain 급감")       # 화면 (채널 · 레벨) 을 놓쳤을 수 있음 → 시작 때 설정으로
                self._bad_retry = True
                try:
                    return self.measure(pd, label, it, nmse, step, kind)
                finally:
                    self._bad_retry = False
            self.log("  ✖ 다시 재도 이상 (%s) → 측정 멈춤 · SG RF OFF (바이어스는 그대로 둠)" % msg)
            try:
                self.rf_off()
            except Exception:
                pass
            try:
                time.sleep(1.0)
                dc_ = self.dc_reader()
                if dc_ and dc_[0] is not None and dc_[1] is not None:
                    self.log("    지금 바이어스: Vdd %.2f V · Id %.3f A (RF OFF 상태 - 처음 대기 전류와 비교해 보세요)" % (dc_[0], dc_[1]))
            except Exception:
                pass
            raise Stop("이상 측정이 계속됨 (%s)" % msg)
        # 이 측정의 입력 신호 (SMW 로 보낸 것) 보관 → 나중에 골라서 .wv 저장 · 다시 내보내기 (중지해도)
        if not hasattr(self, "pd_store"):
            self.pd_store, self._pd_n = {}, 0
        self._pd_n += 1
        r["pd_id"] = self._pd_n
        self.pd_store[self._pd_n] = (np.asarray(pd, dtype=np.complex64), float(p["sg_dbm"]), float(p["freq_ghz"]),
                                     np.asarray(y_al / self.G, dtype=np.complex64))      # (입력, SG, GHz, PA 출력 / 이득)
        if r["kind"] != "off":
            sc = C.aclr_worst(r)
            if getattr(self, "_best_id", None) is None or sc < self._best_sc:
                self._best_id, self._best_sc = self._pd_n, sc
        while len(self.pd_store) > 30:                     # 오래된 것부터 버림 (메모리) - 가장 좋은 것은 남김
            old = [k for k in sorted(self.pd_store) if k != getattr(self, "_best_id", None)]
            self.pd_store.pop(old[0])
        self.results.append(r)
        self._y_last = y_al
        self._eta_tick(t_meas)
        if a_in:
            self.log("    반송파 ACLR: " + " · ".join("%s %.1f" % (n_, v_) for n_, _f0, v_ in (a_pos or [])) + " dBc")
        if pout_spec == pout_spec:
            self.log("    Pout 두 가지: I/Q+보정 %.2f dBm (DE %.1f %%) · 스펙트럼 %.2f dBm (DE %.1f %%) → %s 사용"
                     % (pout_iq, de_of(pout_iq), pout_spec, de_of(pout_spec), "스펙트럼" if pout_used == "spec" else "I/Q+보정"))
        self.log("  %s: Pout %.2f dBm%s · ACLR %.1f / %.1f%s dBc%s · EVM %.2f %% · PAPR %.2f dB%s%s"
                 % (label, pout, " (반송파 합)" if a_in else "", al, au, (" · 안쪽 " + " / ".join("%.1f" % v_ for v_ in a_in)) if a_in else "", (" (Mod. 화면 · I/Q 계산 %.1f / %.1f)" % (al_iq, au_iq)) if src_used == "fsw" else "", ev, r["papr"], (" · DE %.1f %% (Id %.3f A)" % (de, idd)) if de == de else "", "" if full else "  (⚠ 인접 채널 일부만 잼 - %s · ACLR 이 실제보다 좋게 나올 수 있음)" % (why or "대역 모자람")))
        self.on_result(r)
        return y_al, r

    # ------------------------------------------------------------ DPD
    def dpd_signal(self, w):
        x = self.sig.x
        pd = self.model.apply(x, w)
        lim = np.max(np.abs(x)) * 10 ** (self.p["peak_lim_db"] / 20)
        a = np.abs(pd)
        n_clip = int(np.sum(a > lim))
        pd = np.where(a > lim, pd * lim / np.maximum(a, 1e-12), pd)
        return pd, n_clip

    # ------------------------------------------------------------ 남은 시간 (측정 횟수 × 한 번 걸리는 시간)
    def _next_ref(self, pd, lvl):
        """다음 측정의 FSW Ref Level: 앞 측정 최고값 + 레벨 변화 + PAPR 변화 + 1.5 dB 여유 (모르면 nan → 자동 레벨)"""
        m = getattr(self, "_ref_mem", None)
        if not m:
            return float("nan")
        pk, papr0, lvl0 = m
        return pk + (lvl - lvl0) + (C.papr(pd) - papr0) + float(self.p.get("ref_margin_db", 3.0) or 3.0) + getattr(self, "_ref_extra", 0.0)

    def eta_reset(self, total):
        self._ref_mem = None              # 실행마다 처음엔 FSW 자동 레벨
        self._ref_extra = 0.0
        self._best_id = None              # 이번 실행에서 가장 좋은 측정 (보관 신호 지울 때 남김)
        self._mod_fail = False
        self.eta = dict(total=int(total), done=0, per=None, t_last=None, lvl=None)

    def eta_level(self, planned):
        """한 레벨 시작: 이 레벨에서 할 측정 수 (계획). 앞 레벨이 일찍 끝났으면 그만큼 전체에서 뺌"""
        e = getattr(self, "eta", None)
        if e is None:
            return
        self.eta_level_end()
        e["lvl"] = (planned, e["done"])

    def eta_level_end(self):
        e = getattr(self, "eta", None)
        if e is None or e.get("lvl") is None:
            return
        planned, d0 = e["lvl"]
        used = e["done"] - d0
        e["total"] += used - planned                       # 목표 ACLR 에 일찍 닿음 · 반복이 더 됨 → 바로잡기
        e["lvl"] = None

    def eta_left(self):
        """남은 초 (지금 측정에서 이미 지난 시간을 빼서 1초씩 줄어듦) · None = 모름"""
        e = getattr(self, "eta", None)
        if not e:
            return None
        left = max(e["total"] - e["done"], 0)
        per = e["per"] or (5.0 if is_nn(self.p) else 4.0)
        since = time.time() - (e["t_last"] or getattr(self, "cur", {}).get("t", time.time()))
        return max(left * per - min(since, per * 0.95), 0.0)

    def eta_text(self):
        e = getattr(self, "eta", None)
        if not e:
            return ""
        left = max(e["total"] - e["done"], 0)
        per = e["per"] or (5.0 if is_nn(self.p) else 4.0)
        sec = left * per
        if left == 0:
            return "곧 끝"
        t = ("%d분 %02d초" % (sec // 60, sec % 60)) if sec >= 60 else "%d초" % max(sec, 1)
        return "남은 약 %s (%d/%d)" % (t, e["done"], e["total"])

    def _eta_tick(self, t_start):
        e = getattr(self, "eta", None)
        if e is None:
            return
        now = time.time()
        dt = now - (e["t_last"] if e["t_last"] is not None else t_start)   # 학습 · 계산 시간도 포함
        e["per"] = dt if e["per"] is None else 0.6 * e["per"] + 0.4 * dt
        e["t_last"] = now
        e["done"] += 1

    def ec_n(self):
        """JSH: 'ILA+보정' 오차 보정 측정 수 (남은 시간 계산용)"""
        return int(self.p.get("ec_iters", 0) or 0) if (self.p.get("method") == "ilaec" and not is_nn(self.p)) else 0

    def auto_n(self):
        """K · M · L 자동 실측 후보 수: 기본 3 · 넓게 5"""
        return 5 if self.p.get("auto_wide") else 3

    def auto_model(self, y_off, label_pre, step):
        """DPD 모델 (K · M · L) 자동: ① 계산으로 36 조합의 검증 NMSE → 후보 3개 ② 후보마다 실제로 한 번 DPD 측정
        → ACLR 가장 좋은 것. 반환 (y_al, pd, last, w) - 이어서 그 모델로 반복"""
        p = self.p
        x = self.sig.x
        t0 = time.time()
        self.log("▶ %sDPD 모델 자동 찾기 ① 계산 (K · M · L 조합)…" % label_pre)
        self.on_progress(self.eta_text() + " · DPD 모델 찾는 중 (계산)")
        # 메모리 깊이 · 교차 지연은 "샘플 수" → 샘플레이트가 491.52 MSps 의 몇 배인지만큼 후보도 늘림 (같은 시간을 보게)
        ratio = max(1, int(round(self.sig.fs / 491.52e6)))
        wide = bool(p.get("auto_wide"))
        Ks = (3, 5, 7, 9, 11) if wide else (5, 7, 9)
        Ms = tuple(sorted({max(1, int(round(m * ratio))) for m in ((1, 2, 3, 4, 6, 8) if wide else (2, 3, 4, 6))}))
        l0 = (0, 1, 2, 3) if wide else (0, 1, 2)
        Ls = tuple(sorted({int(round(l * ratio)) for l in l0})) if ratio <= 2 else tuple(l * ratio for l in l0)
        if ratio > 1:
            self.log("  샘플레이트 %.2f MSps (491.52 의 %d배) → M 후보 %s · L 후보 %s" % (self.sig.fs / 1e6, ratio, Ms, Ls))
        res = C.model_search(y_off / self.G, x, p["odd_only"], p["lam"], n_fit=min(int(p["n_fit"]), 30000),
                             Ks=Ks, Ms=Ms, Ls=Ls, cancelled=lambda: self.stop)
        self.check()
        cands = C.pick_models(res, self.auto_n())
        self.log("  %d 조합 %.1f초 · 후보: %s" % (len(res), time.time() - t0, " · ".join(
            "K%d M%d L%d (%.1f dB, %d 계수)" % (K, M, L, nm, nc) for nm, nc, K, M, L in cands)))
        best = None
        G0, sg0 = self.G, p["sg_dbm"]                     # 후보마다 같은 출발점 (목표 이득 · SG) 에서 → 앞 후보의 Pout 맞춤이 다음 후보에 쌓이지 않게
        for i, (nm, nc, K, M, L) in enumerate(cands):
            self.check()
            self.G, p["sg_dbm"] = G0, sg0
            name = "K%d M%d L%d" % (K, M, L)
            self.log("▶ %sDPD 모델 자동 찾기 ② 실측 %d/%d: %s" % (label_pre, i + 1, len(cands), name))
            self.model = self.make_model(K, M, L)
            w, nmse = self.model.fit(y_off / self.G, x, n_fit=p["n_fit"])
            self.model.w = w
            pd, _ = self.dpd_signal(w)
            y_al, last = self.measure(pd, "%s후보 %s" % (label_pre, name), 1, nmse, step)
            sc = C.aclr_worst(last)
            if best is None or sc < best[0]:
                best = (sc, (K, M, L), y_al, pd, last, w, self.G, p["sg_dbm"])
        if best is None:
            raise RuntimeError("K · M · L 자동: 후보를 하나도 못 잼 (계산 결과 없음 - 신호 길이 · 학습 샘플 수 확인)")
        sc, (K, M, L), y_al, pd, last, w, self.G, p["sg_dbm"] = best      # 고른 후보를 잰 그때 상태로 이어서
        self.model = self.make_model(K, M, L)
        self.model.w = w
        p["K"], p["M"], p["L"] = K, M, L
        self.chosen = (K, M, L)
        self.log("  ✔ 고른 모델: K%d M%d L%d (ACLR %.1f dBc) → 이 모델로 반복" % (K, M, L, sc))
        return y_al, pd, last, w

    def w_mode(self):
        """보정 기준: 'time' = 시간 영역 (전 대역 똑같이) · 'focus' = 주파수 영역 집중 구간 · 'aclr' = 주파수 영역 ACLR 인접 채널 자동"""
        m = self.p.get("w_mode")
        if m in ("time", "focus", "aclr"):
            return m
        return "focus" if self.p.get("focus_on", True) else "time"     # 예전 설정

    def weight_bands(self):
        """주파수 가중을 줄 구간 [(f_lo, f_hi) Hz] 또는 None (시간 영역)"""
        mode = self.w_mode()
        cars, bwm, adj = self.aclr_plan()
        if mode == "focus":
            fb = self.focus_band()
            if fb is None:
                return None
            return [b_ for fc, _w in cars for b_ in ((fc - fb[1], fc - fb[0]), (fc + fb[0], fc + fb[1]))]
        if mode == "aclr":
            out = []
            for i, (fc, _w) in enumerate(cars):
                for s_ in (-1, 1):
                    f0 = fc + s_ * adj
                    if any(j != i and abs(f0 - fj) < 0.5 * (wj + bwm) * 0.9 for j, (fj, wj) in enumerate(cars)):
                        continue
                    out.append((f0 - bwm / 2, f0 + bwm / 2))
            return out or None
        return None

    def mod_pout(self, m):
        """FSW Mod. 화면의 Tx 채널 전력 (dBm, FSW 입력) - Bench Mod. 측정과 같은 값.
        Tx 채널이 여러 개 (반송파마다 사각형) 면 전부 더함. 없으면 None"""
        v = m.get("tx_sum")
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v == v and abs(v) < 200 else None

    def pout_bands(self):
        """Pout 으로 셀 대역 [(lo, hi) Hz] · None = 받은 대역 전체. 'auto' = 반송파 채널 안 (aclr_plan 의 채널 폭)"""
        t = str(self.p.get("pout_band", "auto") or "auto").strip().lower()
        if t in ("all", "전체", "full"):
            return None
        if t in ("", "auto", "자동"):
            cars, bwm, _adj = self.aclr_plan()
            return [(fc - bwm / 2, fc + bwm / 2) for fc, _w in cars]
        out = []
        for part in re.split(r"[,;/]", t):
            if "~" in part:
                nums = [v_.strip() for v_ in part.split("~")]
                nums = [v_ for v_ in nums if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", v_)]
            else:
                nums = re.findall(r"[-+]?\d+(?:\.\d+)?", part)
            if len(nums) >= 2:
                a_, b_ = sorted((float(nums[0]), float(nums[1])))
                if b_ > a_:
                    out.append((a_ * 1e6, b_ * 1e6))
        if not out:
            raise ValueError("Pout 대역 '%s' 를 못 읽음 (예: -50~50 또는 -200~-100, 100~200 · auto · all)" % t)
        return out

    def focus_band(self):
        """집중 구간 (반송파 중심에서 떨어진 MHz, 예 '50-70') → (f1, f2) Hz 또는 None (보정 기준이 '집중 구간' 일 때만)"""
        if self.w_mode() != "focus":
            return None
        t = str(self.p.get("focus_mhz") or "").replace("~", "-").replace(",", "-").replace(" ", "")
        try:
            a, b = [float(v) for v in t.split("-") if v][:2]
        except (ValueError, TypeError):
            return None
        a, b = sorted((abs(a), abs(b)))
        return (a * 1e6, b * 1e6) if b > a else None

    def focus_weight(self, e):
        """오차 e 에 주파수 가중: 집중 구간 (양쪽) 밖을 1/가중 으로 줄임 → ILC · DLA 가 그 구간을 먼저 고침"""
        bands = self.weight_bands()
        if not getattr(self, "_wlog", False):
            self._wlog = True
            nm = {"time": "시간 영역 (전 대역 똑같이)", "focus": "주파수 영역 · 집중 구간", "aclr": "주파수 영역 · ACLR 인접 채널 자동"}[self.w_mode()]
            self.log("  보정 기준: %s%s" % (nm, (" · 가중 %s 배 · " % (self.p.get("focus_w") or 4) +
                                                 ", ".join("%.0f~%.0f" % (a_ / 1e6, b_ / 1e6) for a_, b_ in bands) + " MHz") if bands else
                                         (" (집중 구간 비어 있음 → 시간 영역)" if self.w_mode() == "focus" else "")))
        if not bands:
            return e                                   # 시간 영역: 전 대역 똑같이
        wgt = max(1.0, float(self.p.get("focus_w") or 4.0))
        f = np.fft.fftfreq(len(e), 1.0 / self.sig.fs)
        m = np.zeros(len(f), bool)
        for lo_, hi_ in bands:                         # 집중 구간 (반송파마다 ±) 또는 ACLR 인접 채널
            m |= (f >= lo_) & (f <= hi_)
        W = np.where(m, 1.0, 1.0 / wgt)               # 나머지를 줄임 (집중 구간 이득은 그대로 → 넘치지 않음)
        return np.fft.ifft(np.fft.fft(e) * W)

    def focus_dbc(self, y_al):
        """집중 구간 전력 (채널 대비 dBc, 아래 / 위) - I/Q 로 계산"""
        fb = self.focus_band()
        if fb is None:
            return None
        f, psd = C.welch_psd(y_al, self.sig.fs)
        cars, bwm, _adj = self.aclr_plan()
        mid, bw = 0.5 * (fb[0] + fb[1]), fb[1] - fb[0]
        lo, hi = [], []
        for fc, _w in cars:                            # 반송파마다 그 중심 기준, 그 반송파 전력 대비
            pc, _ = C.band_power(f, psd, fc, bwm)
            for s_, acc in ((-1, lo), (1, hi)):
                f0 = fc + s_ * mid
                if any(abs(f0 - fj) < 0.5 * (wj + bw) * 0.9 for fj, wj in cars if fj != fc and len(cars) > 1):
                    continue                           # 다른 반송파 안이면 뺌
                pa, _ = C.band_power(f, psd, f0, bw)
                acc.append(10 * np.log10(pa / pc))
        return (max(lo) if lo else float("nan")), (max(hi) if hi else float("nan"))

    def _final_mod(self, pd, label, step, kind=None):
        """ACLR 읽기 = 'OFF · 최종만' 일 때: 그 레벨 마지막 신호를 한 번 더 보내 FSW Mod. 화면 ACLR 로 잼 (그동안 반복은 I/Q 값)"""
        if self.p.get("aclr_src") != "fsw_end" or getattr(self, "_mod_fail", False) or self.p.get("mod_every"):
            return None                                  # 반복마다 Mod. 화면으로 쟀으면 다시 잴 필요 없음
        self.check()
        self._force_mod = True
        try:
            self.log("▶ %s최종 - FSW Mod. 화면 ACLR" % label)
            _y, r = self.measure(pd, "%s최종 (Mod. 화면)" % label, 99, None, step, kind=kind)
        finally:
            self._force_mod = False
        return r

    def log_model_params(self, title, w=None, nm=None, top=12):
        """JSH: 찾은 DPD 모델 (GMP 등) 파라미터를 로그로 - 모델 · K · M · L (Lagging / Leading) · 계수 수 · 학습 오차 · 큰 계수"""
        m = self.model
        if m is None:
            return
        w = getattr(m, "w", None) if w is None else w
        p = self.p
        mdl = p.get("model", "gmp")
        try:
            name = MODEL_NAMES.get(mdl, mdl)
            nmt = (" · 학습 오차 %.1f dB" % nm) if nm is not None and nm == nm else ""
            if hasattr(m, "H"):                                     # 신경망
                self.log("  ▣ %s 모델 파라미터: %s · M%d · 은닉 %d · 가중치 %d 개%s" % (title, name, m.M, m.H, m.n_coef(), nmt))
                return
            if hasattr(m, "nseg"):                                  # DVR
                self.log("  ▣ %s 모델 파라미터: %s · 구간 %d · M%d · λ %g · 계수 %d 개%s" % (title, name, m.nseg, m.M, m.lam, m.n_coef(), nmt))
            else:
                ko = m.orders()
                kx = len([k for k in ko if k > 1])
                n_mp, n_lag, n_lead = m.M * len(ko), m.M * getattr(m, "lag", m.L) * kx, m.M * getattr(m, "lead", m.L) * kx
                seg = 2 if hasattr(m, "t") and hasattr(m, "_gate") else 1        # 구간 GMP = 두 벌
                self.log("  ▣ %s 모델 파라미터: %s · K%d (%s) · M%d · L%d · Lagging %d · Leading %d · λ %g · 계수 %d 개%s"
                         % (title, name, m.K, "홀수만 " + ",".join(map(str, ko)) if m.odd else "1~%d" % m.K, m.M, m.L,
                            getattr(m, "lag", m.L), getattr(m, "lead", m.L), m.lam, m.n_coef(), nmt))
                extra = ""
                if hasattr(m, "lin_delays") and getattr(m, "nlin", 0) > 0:
                    extra += " · 긴 선형 등화 %d (±%d 탭)" % (len(m.lin_delays()), m.nlin)
                if hasattr(m, "taus_us"):
                    extra += " · 긴 메모리 (트래핑) %d (%s µs)" % (2 * len(m.taus_us), "·".join("%g" % t for t in m.taus_us))
                self.log("    항 수: 메모리 다항식 %d · Lagging 교차 %d · Leading 교차 %d%s%s"
                         % (n_mp, n_lag, n_lead, extra, (" (× 2 벌 = 피크 전 / 후, 경계 %.0f %% of max)" % (100 * getattr(m, "t_frac", float("nan"))))
                            if seg == 2 else ""))
            if w is None or not hasattr(m, "term_names"):
                return
            names = m.term_names()
            w = np.asarray(w)
            if len(names) != len(w):
                return
            ref = np.max(np.abs(w)) or 1.0
            order = np.argsort(-np.abs(w))[:int(top)]
            self.log("    큰 계수 %d 개 (|c| · 최대 대비 dB · 위상) - 전체는 [계수 CSV…]:" % len(order))
            for i in order:
                c = w[i]
                nm_ = names[i].replace("n--", "n+").replace("(n-0)", "(n)")        # Leading 항: x(n+1) 처럼
                self.log("      %-30s |c| %9.5f  %6.1f dB  ∠%7.1f°" % (nm_, abs(c), 20 * np.log10(abs(c) / ref + 1e-30),
                                                                     np.degrees(np.angle(c))))
        except Exception as e:
            self.log("  (모델 파라미터 로그 못 씀: %s)" % e)

    def _dpd_level(self, y_al, w, label_pre, step, target=None, start=None):
        if self.p.get("method") == "ilcm" and start is None:
            # ILC → 모델: 먼저 ILC 로 이 PA 에 맞는 입력 u 를 직접 찾고 → 모델이 '원래 신호 x → u' 를 배우게 (측정 잡음 덜 탐)
            self.log("▶ %sILC → 모델: ILC %d 번으로 정답 입력 찾기" % (label_pre, int(self.p.get("ilc_iters", 10))))
            self._ilc_level(y_al, label_pre + "ILC · ", step)
            u = self._ilc_u
            x = self.sig.x
            self.check()
            t0 = time.time()
            if is_nn(self.p):
                w, nm = self.model.fit(x, u, n_fit=self.p["n_fit"], init=None, cancelled=lambda: self.stop)
            else:
                w, nm = self.model.fit(x, u, n_fit=self.p["n_fit"])
            self.model.w = w
            self.log("  모델 학습 (x → ILC 입력) %.1f초 · 오차 %.1f dB" % (time.time() - t0, nm))
            w, nm = self._ilcm_widen(x, u, w, nm)
            w, nm = self._ilcm_lin(x, u, w, nm)
            w, nm = self._ilcm_trap(x, u, w, nm)
            w, nm = self._ilcm_focus(x, u, w, nm)
            self._ilcm_fit = nm
            self.log_model_params("ILC→모델 학습", w, nm)        # JSH
            pd, n_clip = self.dpd_signal(w)
            y_al, last = self.measure(pd, "%sILC→모델" % label_pre, 1, nm, step)
            start = (pd, last)
            ilcm_here = True
        else:
            ilcm_here = False
        last, w, pd = self._dpd_level_core(y_al, w, label_pre, step, target, start)
        if not ilcm_here:
            last, w, pd = self._retry_if_worse(last, w, pd, label_pre, step, target)     # JSH
        if ilcm_here:
            self.log_model_params("ILC→모델 최종 (ILA 다듬은 뒤)", w)   # JSH
        r = self._final_mod(pd, label_pre, step)
        if r is None and pd is not None and getattr(self, "_sent_pd", None) is not pd:
            # JSH: 되돌림 · 다시 로 '가장 좋았던' 계수로 끝남 → SG 는 아직 마지막에 해 본 (나쁜) 신호 → 가장 좋았던 신호를 다시 보내 확인
            self.check()
            self.log("▶ %s가장 좋았던 계수 신호를 다시 보냄 (SG 에 남기고 확인)" % label_pre)
            _yb, r = self.measure(pd, "%s최고 계수 (다시 보냄)" % label_pre, 99, None, step)
        self.last_pd, self.last_sg = pd, self.p["sg_dbm"]
        self._resume = dict(kind="dpd", sg=self.p["sg_dbm"], w=w, pd=pd, last=(r or last), y_al=getattr(self, "_y_last", y_al))
        return (r or last), w, pd

    def _retry_if_worse(self, last, w, pd, label_pre, step, target=None):
        """JSH: DPD 결과가 (같은 SG 의) DPD OFF 보다 나쁘면 처음부터 다시 - 계수 버림 · DPD OFF 다시 재서 목표 이득 다시 · μ ½.
        2 번째 다시부터는 목표 이득 크기 = 전체 평균 이득 (작은 신호 이득이 너무 커서 피크가 포화에 걸리는 경우).
        가장 좋았던 시도로 끝냄"""
        p = self.p
        n_try = int(p.get("retry_off", 2) or 0)
        off = getattr(self, "_off_ref", None)
        if n_try <= 0 or off is None or abs(off[2] - p["sg_dbm"]) > 1e-6 or last is None:
            return last, w, pd
        sc = C.aclr_worst(last)
        if not (sc == sc and sc > off[0]):
            return last, w, pd
        x = self.sig.x
        best = (sc, last, w, pd, self.G, getattr(self, "_y_last", None), self.model)
        mu0 = p["mu"]
        try:
            for att in range(1, n_try + 1):
                self.check()
                p["mu"] = max(float(mu0) * 0.5 ** att, 0.1)
                avg = att >= 2
                self.log("⚠ %sDPD %.1f dBc 가 DPD OFF %.1f dBc 보다 나쁨 → 처음부터 다시 (%d / %d): 계수 버림 · DPD OFF 다시 재서 "
                         "목표 이득 다시%s · μ %.2f" % (label_pre, best[0], off[0], att, n_try,
                                                    " (크기 = 전체 평균 이득)" if avg else "", p["mu"]))
                self.G = None
                y_off, r_off = self.measure(x, "%sDPD OFF (다시 %d)" % (label_pre, att), 0, step=step)
                off = self._off_ref
                if avg:
                    g_all = np.vdot(x, y_off) / np.vdot(x, x)
                    self.log("  목표 이득: 작은 신호 %.2f dB → 전체 평균 %.2f dB (위상은 작은 신호 그대로)"
                             % (20 * np.log10(abs(self.G)), 20 * np.log10(abs(g_all))))
                    self.G = abs(g_all) * np.exp(1j * np.angle(self.G))
                if is_nn(p):
                    self.model = self.make_model()
                last_n, w_n, pd_n = self._dpd_level_core(y_off, None, "%s다시%d · " % (label_pre, att), step, target)
                sc_n = C.aclr_worst(last_n) if last_n is not None else float("nan")
                if sc_n == sc_n and sc_n < best[0]:
                    best = (sc_n, last_n, w_n, pd_n, self.G, getattr(self, "_y_last", None), self.model)
                if sc_n == sc_n and sc_n <= off[0]:
                    self.log("  ✔ 다시 %d: %.1f dBc (DPD OFF %.1f 보다 좋음) → 끝" % (att, sc_n, off[0]))
                    break
                self.log("  다시 %d: %.1f dBc (DPD OFF %.1f 보다 아직 나쁨)" % (att, sc_n, off[0]))
            else:
                self.log("  ⚠ %d 번 다시 해도 DPD OFF 보다 나쁨 → 가장 좋았던 %.1f dBc 로 · PA 여유 (SG ↓) · 최고값 제한 · 측정 바닥 확인"
                         % (n_try, best[0]))
        finally:
            p["mu"] = mu0
        _s, last, w, pd, G_b, y_b, m_b = best
        self.G = G_b
        self.model = m_b
        self.model.w = w
        if y_b is not None:
            self._y_last = y_b
        return last, w, pd

    def _dpd_level_core(self, y_al, w, label_pre, step, target=None, start=None):
        """한 SG 레벨에서 DPD 반복 (w = 시작 계수, 앞 레벨 것) → (마지막 결과, 계수)
        start = (pd, last): 이미 그 계수로 측정한 결과 (자동 모델 찾기 뒤) → 첫 반복을 이어서"""
        x = self.sig.x
        pd = x
        last = None
        n = int(self.p["iters"])
        guard = float(self.p.get("ila_guard_db", 1.0) or 0.0)      # JSH: 되돌림 기준 (0 = 끔)
        mu_eff = float(self.p["mu"])
        best = None
        if start is not None:
            pd, last = start
            n = max(n - 1, 1)
            best = (C.aclr_worst(last), w, pd, last, y_al)
            if target is not None and C.aclr_worst(last) <= target:
                self.log("  목표 ACLR %.1f dBc 이내 → 반복 끝" % target)
                return last, w, pd
        for it in range(1, n + 1):
            self.check()
            self.log("▶ %sDPD 반복 %d / %d" % (label_pre, it, n))
            if w is not None and it == 1 and last is None:
                # 앞 레벨 계수로 먼저 한 번 보내 보고 (학습 없이) 그 결과로 이어서 학습 → 반복이 줄어듦
                pd, _ = self.dpd_signal(w)
                self.model.w = w
                y_al, last = self.measure(pd, "%sDPD 0 (이전 계수)" % label_pre, it, None, step)
                best = (C.aclr_worst(last), w, pd, last, y_al)
                if target is not None and C.aclr_worst(last) <= target:
                    self.log("  목표 ACLR %.1f dBc 이내 → 다음 레벨" % target)
                    return last, w, pd
            meth = self.p.get("method", "ila")
            mu = mu_eff
            if is_nn(self.p):
                # 신경망: 이전 가중치에서 이어서 학습 (ILA 방식, 섞지 않음)
                self.on_progress("%s · 신경망 학습 중 · %d 번째" % (self.eta_text(), it))
                t0 = time.time()
                w, nmse = self.model.fit(y_al / self.G, pd, n_fit=self.p["n_fit"], init=w, cancelled=lambda: self.stop)
                self.check()
                self.log("  신경망 학습 %.1f초 (가중치 %d 개)" % (time.time() - t0, self.model.n_coef()))
            elif meth == "dla" and w is not None:
                # 직접 학습: PA 출력 오차 (원래 신호 - 출력/G) 를 원래 신호 basis 로 맞춰 계수에 더함
                e = self.focus_weight(x - y_al / self.G)
                dw, _ = self.model.fit(x, e, n_fit=self.p["n_fit"])
                w = w + mu * dw
                nmse = 10 * np.log10(np.mean(np.abs(e) ** 2) / np.mean(np.abs(x) ** 2))
            else:
                wn, nmse = self.model.fit(y_al / self.G, pd, n_fit=self.p["n_fit"])
                w = wn if w is None else (1 - mu) * w + mu * wn
            self.model.w = w
            pd, n_clip = self.dpd_signal(w)
            if n_clip:
                self.log("  최고값 제한 (+%.1f dB) 에 걸린 샘플 %d 개" % (self.p["peak_lim_db"], n_clip))
            y_al, last = self.measure(pd, "%sDPD %d" % (label_pre, it), it, nmse, step)
            kb = getattr(self, "_keep_best", None)
            if kb is None and guard > 0:
                sc = C.aclr_worst(last)
                if sc == sc and (best is None or sc < best[0]):
                    best = (sc, w, pd, last, y_al)
                elif sc == sc and sc > best[0] + guard:
                    # JSH 되돌림: 가장 좋았던 것보다 기준 넘게 나빠짐 → 그 계수 · 측정으로 돌아가 μ ½ 로 이어서
                    mu_eff = max(mu_eff * 0.5, 0.1)
                    self.log("  ↩ 되돌림: %.1f dBc (최고 %.1f 보다 %.1f dB 나쁨 > %.1f) → 가장 좋았던 계수로 · μ %.2f"
                             % (sc, best[0], sc - best[0], guard, mu_eff))
                    _s, w, pd, last, y_al = best
                    self.model.w = w
            if kb is not None:
                sc = C.aclr_worst(last)
                if sc == sc and sc < kb[0]:
                    self._keep_best = kb = (sc, w, pd, last, y_al)
                elif sc == sc and sc > kb[0] + 1.0:
                    # 다듬기가 오히려 나빠짐 → 멈추고 가장 좋았던 계수 · 신호로 (ILC 결과로 모델 만들기)
                    self.log("  다듬기가 나빠짐 (%.1f > 최고 %.1f dBc) → 반복 멈추고 가장 좋았던 것으로" % (sc, kb[0]))
                    _s, w, pd, last, y_al = kb
                    self.model.w = w
                    break
            if target is not None and C.aclr_worst(last) <= target:
                self.log("  목표 ACLR %.1f dBc 이내 → 반복 끝" % target)
                break
        if guard > 0 and getattr(self, "_keep_best", None) is None and best is not None and last is not None \
                and C.aclr_worst(last) > best[0] + 1e-9:
            self.log("  ↩ 반복 끝: 마지막 %.1f 보다 좋았던 %.1f dBc 계수로" % (C.aclr_worst(last), best[0]))
            _s, w, pd, last, y_al = best
            self.model.w = w
            self._y_last = y_al
        if self.p.get("method") == "ilaec" and not is_nn(self.p) and w is not None and last is not None:
            last, w, pd = self._ec_refine(w, pd, last, y_al, label_pre, step, target)     # JSH: ILA 뒤 오차 보정 반복
        return last, w, pd

    # ------------------------------------------------------------ JSH: ILA + 오차 보정 (측정 반복하며 출력 오차를 직접 줄임)
    @staticmethod
    def _pa_curve(pd, y, nb=48):
        """측정한 (입력 pd, 출력 y = y_al/G) 로 PA AM-AM F(r) · AM-PM φ(r) 곡선 (다항식) → (F, F', φ') 함수"""
        r = np.abs(pd)
        ok = r > 1e-9
        r, yy, pp = r[ok], y[ok], pd[ok]
        rmax = np.percentile(r, 99.9)
        edges = np.linspace(0, rmax, nb + 1)
        idx = np.clip(np.digitize(r, edges) - 1, 0, nb - 1)
        rc, Fc, Pc, wc = [], [], [], []
        ph = np.angle(yy * np.conj(pp))
        for b in range(nb):
            sel = idx == b
            if np.count_nonzero(sel) < 20:
                continue
            rc.append(np.mean(r[sel]))
            Fc.append(np.mean(np.abs(yy[sel])))
            Pc.append(np.angle(np.mean(np.exp(1j * ph[sel]))))
            wc.append(np.sqrt(np.count_nonzero(sel)))
        rc, Fc, Pc, wc = map(np.asarray, (rc, Fc, Pc, wc))
        # F(r) = r · (a0 + a1 r + … ) (원점 지남) · φ(r) = b0 + b1 r + …
        A = np.stack([rc ** (k + 1) for k in range(6)], axis=1)
        a = np.linalg.lstsq(A * wc[:, None], Fc * wc, rcond=None)[0]
        B = np.stack([rc ** k for k in range(5)], axis=1)
        bph = np.linalg.lstsq(B * wc[:, None], np.unwrap(Pc) * wc, rcond=None)[0]
        F = lambda x: sum(a[k] * x ** (k + 1) for k in range(6))
        dF = lambda x: sum((k + 1) * a[k] * x ** k for k in range(6))
        dphi = lambda x: sum(k * bph[k] * x ** (k - 1) for k in range(1, 5))
        return F, dF, dphi, rmax

    def _ec_step(self, x, pd, y):
        """출력 오차 e = x − y 를 PA 기울기로 입력 쪽 보정량 δpd 로 (지름 · 접선 방향 따로, AM-PM 섞임 반영)"""
        e = x - y
        F, dF, dphi, rmax = self._pa_curve(pd, y)
        r = np.minimum(np.abs(pd), rmax)
        g0 = max(float(dF(1e-3 * rmax)), 1e-6)                    # 작은 신호 기울기
        Fp = np.maximum(dF(r), 0.25 * g0)                          # 포화 근처에서 기울기가 0 으로 가면 너무 크게 고치지 않게
        Fr = np.maximum(F(r), 1e-9)
        Fr_r = np.where(r > 1e-3 * rmax, Fr / np.maximum(r, 1e-12), g0)
        uo = np.exp(1j * np.angle(y))                              # 출력 방향
        ui = np.exp(1j * np.angle(pd))                             # 입력 방향
        eo = e * np.conj(uo)
        d_r = eo.real / Fp
        d_t = (eo.imag - Fr * dphi(r) * d_r) / Fr_r
        return (d_r + 1j * d_t) * ui, e

    def _ec_refine(self, w, pd, last, y_al, label_pre, step, target=None):
        """ILA 로 맞춘 계수에서 시작해 '측정 → 출력 오차 → PA 기울기로 입력 보정량 → 모델 계수에 μ 만큼 더함' 반복.
        나빠지면 가장 좋았던 계수로 돌아가 μ 를 반으로. 반환 (가장 좋은 결과, 계수, 신호)"""
        p, x = self.p, self.sig.x
        n = int(p.get("ec_iters", 6) or 0)
        mu = float(p.get("ec_mu", 0.5) or 0.5)
        if n <= 0:
            return last, w, pd
        sc0 = C.aclr_worst(last)
        best = (sc0, w, pd, last, y_al)
        self.log("▶ %s오차 보정 %d 번 (μ %.2f) - ILA 결과 (%.1f dBc) 에서 측정 출력 오차를 PA 기울기로 직접 줄임"
                 % (label_pre, n, mu, sc0))
        for it in range(1, n + 1):
            self.check()
            yb = best[4] / self.G
            dpd_in, e = self._ec_step(x, best[2], yb)
            dw, _ = self.model.fit(x, self.focus_weight(dpd_in), n_fit=p["n_fit"])
            nmse = 10 * np.log10(np.mean(np.abs(e) ** 2) / np.mean(np.abs(x) ** 2))
            w_new = best[1] + mu * dw
            self.model.w = w_new
            pd_new, n_clip = self.dpd_signal(w_new)
            if n_clip:
                self.log("  최고값 제한 (+%.1f dB) 에 걸린 샘플 %d 개" % (p["peak_lim_db"], n_clip))
            y_new, r = self.measure(pd_new, "%s오차 보정 %d" % (label_pre, it), it, nmse, step)
            sc = C.aclr_worst(r)
            if sc == sc and sc < best[0]:
                self.log("  오차 보정 %d: %.1f dBc (좋아짐 %+.1f dB · 출력 오차 %.1f dB · μ %.2f)" % (it, sc, sc - best[0], nmse, mu))
                best = (sc, w_new, pd_new, r, y_new)
            else:
                mu = max(mu * 0.5, 0.05)
                self.log("  오차 보정 %d: %.1f dBc (최고 %.1f 보다 안 좋음) → 최고 계수로 돌아가 μ %.2f" % (it, sc, best[0], mu))
            if target is not None and best[0] <= target:
                self.log("  목표 ACLR %.1f dBc 이내 → 오차 보정 끝" % target)
                break
        self.model.w = best[1]
        self.log("  ✔ 오차 보정 끝: %.1f → %.1f dBc (%+.1f dB)" % (sc0, best[0], best[0] - sc0))
        self._y_last = best[4]
        return best[3], best[1], best[2]

    def lock_sg(self):
        """목표 Pout (p['pout_lock']) 이 있으면: DPD OFF 신호를 보내며 실제 Pout 을 재서 SG 를 맞춤 → 그 SG 로 고정
        (기울기는 잰 두 점으로 · 압축되면 더 크게 움직임 · SG 최대에서 멈춤)"""
        tgt = self.p.get("pout_lock")
        try:
            tgt = float(tgt)
        except (TypeError, ValueError):
            return None
        if tgt != tgt:
            return None
        x = self.sig.x
        sg = float(self.p["sg_dbm"])
        sg_max = float(self.p.get("sg_max_dbm", 0.0))
        tol = float(self.p.get("pout_tol") or self.p.get("pout_lock_tol") or 0.1)   # 3 카드 허용 오차 (예전: 늘 0.1 dB 로 맞춰 0.05 가 안 먹음)
        hist = []
        self._ref_mem = None              # 앞 실행 (멈춘 실행 포함) 의 FSW Ref 기억은 버림 → 첫 측정은 FSW 자동 레벨부터
        self._ref_extra = 0.0             #   (예전: 멈췄다 다시 시작하면 남은 Ref 로 Pout 을 잘못 읽어 맞추기가 오래 걸림)
        self.log("▶ Pout 맞추기: 목표 %.2f dBm (SG %.2f dBm 부터, ±%.2f dB 안)" % (tgt, sg, tol))
        po = float("nan")
        for k in range(10):
            self.check()
            sg = min(sg, sg_max)
            self.p["sg_dbm"] = sg
            self.G = None
            po = self._pout_read(x, "Pout 맞추기 %d" % (k + 1), "SG %.2f dBm" % sg, tgt, tol)
            hist.append((sg, po))
            err = tgt - po
            self.log("  %d) SG %.2f dBm → Pout %.2f dBm (목표와 %+.2f dB)" % (k + 1, sg, po, -err))
            if abs(err) <= tol:
                self.gain_ok()
                break
            slope = 1.0
            if len(hist) >= 2:
                (s1, p1), (s2, p2) = hist[-2], hist[-1]
                if abs(s2 - s1) > 0.005:
                    slope = min(max((p2 - p1) / (s2 - s1), 0.2), 1.5)
            new = round(sg + min(max(err / slope, -6.0), 6.0), 2)
            if new > sg_max:
                if sg >= sg_max - 1e-6:
                    self.log("  ⚠ SG 최대 (%.2f dBm) 에서도 목표 Pout 에 못 미침 → SG 최대로 진행 (2 카드 SG 최대 확인)" % sg_max)
                    self.gain_note(tgt, po)
                    break
                new = sg_max
            sg = new
        else:
            self.log("  ⚠ 10 번 안에 목표 ±%.2f dB 에 못 맞춤 → 마지막 SG 로 진행" % tol)
        self.p["sg_dbm"] = sg
        self.G = None
        self.log("  ✔ SG %.2f dBm 로 고정 (Pout %.2f dBm) → DPD 시작" % (sg, po))
        cb = getattr(self, "on_lock", None)
        if cb:
            try:
                cb(sg)
            except Exception:
                pass
        return sg

    def run(self, do_dpd=True):
        """DPD OFF 측정 → (do_dpd) DPD 반복. 결과 목록 반환"""
        self._wlog = False
        self._plog = False
        self._pk_note = False
        self._acp_warn = False
        self._bwlog = False
        self._calm_warn = False
        self.stop = False
        self.results = []
        self._cal_done = False
        self._calm_done = False
        self._calm_bad = False
        self.connect()
        if self.sig is None:
            self.make_signal()
        if self.model is None:                            # 신호를 이어 쓴 경우 (중지 뒤 다시 실행) 에도 모델은 꼭
            self.model = self.make_model()
        self.lock_sg()
        x = self.sig.x
        self.G = None
        auto = self.p.get("auto_model") and self.p.get("model", "gmp") == "gmp"
        n_it = int(self.p["iters"])
        self.eta_reset(1 if not do_dpd else 1 + (self.auto_n() + max(n_it - 1, 1) if auto else n_it) + self.ec_n())
        self.eta_level(self.eta["total"])
        self.log("▶ DPD OFF 측정")
        y_al, r0 = self.measure(x, "DPD OFF", 0, step="SG %.2f dBm" % self.p["sg_dbm"])
        if not do_dpd:
            return self.results
        tgt = self.p.get("target_aclr")
        stp = "SG %.2f dBm" % self.p["sg_dbm"]
        if self.p.get("auto_model") and self.p.get("model", "gmp") == "gmp":
            y_al, pd, last, w = self.auto_model(y_al, "", stp)
            last, w, pd = self._dpd_level(y_al, w, "", stp, tgt, start=(pd, last))
        else:
            last, w, pd = self._dpd_level(y_al, None, "", stp, tgt)
        self.last_pd = pd
        return self.results

    def run_compare(self, methods, with_ilc=False):
        """방식 비교: 같은 SG 레벨 · 같은 DPD OFF 측정에서 출발해 방식마다 DPD 반복 → 방식별 최고 결과 요약
        methods = [(model, learn), ...]  예: [("gmp","ila"), ("gmp","dla"), ("dvr","ila"), ("nn","ila")]"""
        self._wlog = False
        self._plog = False
        self._pk_note = False
        self._acp_warn = False
        self._bwlog = False
        self._calm_warn = False
        self.stop = False
        self.results = []
        self.compare = []
        self._cmp_best = None
        self._cal_done = False
        self._calm_done = False
        self._calm_bad = False
        self.connect()
        if self.sig is None:
            self.make_signal()
        if self.model is None:                            # 신호를 이어 쓴 경우 (중지 뒤 다시 실행) 에도 모델은 꼭
            self.model = self.make_model()
        self.lock_sg()
        x = self.sig.x
        p = self.p
        keep = {k: p.get(k) for k in ("model", "method", "K", "M", "L", "L_lag", "L_lead")}
        n_it = int(p["iters"])
        tot = 1
        for mdl, lrn in methods:
            tot += (self.auto_n() + max(n_it - 1, 1)) if (mdl == "gmp" and p.get("auto_model")) else n_it
        if with_ilc:
            tot += int(p.get("ilc_iters", 10))
        self.eta_reset(tot)
        self.eta_level(tot)
        stp = "SG %.2f dBm" % p["sg_dbm"]
        self.G = None
        self.tag = "OFF"
        self.log("━━ 방식 비교 · %d 가지%s · %s" % (len(methods), " + ILC 한계" if with_ilc else "", stp))
        y_off, r_off = self.measure(x, "DPD OFF", 0, step=stp)
        G0, sg0 = self.G, p["sg_dbm"]                     # 방식마다 같은 출발점 (앞 방식의 Pout 맞춤이 쌓이지 않게)
        try:
            for i, (mdl, lrn) in enumerate(methods):
                self.check()
                p.update(keep)
                self.G, p["sg_dbm"] = G0, sg0
                p["model"], p["method"] = mdl, lrn
                name = method_label(p)
                self.model = self.make_model()
                if getattr(self.model, "fallback", False):
                    name += " (numpy 대신)"
                self.tag = name
                self.log("━━ 방식 %d / %d: %s" % (i + 1, len(methods), name))
                t0 = time.time()
                n0 = len(self.results)
                try:
                    if mdl == "gmp" and p.get("auto_model"):
                        y_al, pd, last, w = self.auto_model(y_off, name + " · ", stp)
                        last, w, pd = self._dpd_level(y_al, w, name + " · ", stp, p.get("target_aclr"), start=(pd, last))
                    else:
                        last, w, pd = self._dpd_level(y_off, None, name + " · ", stp, p.get("target_aclr"))
                    err = ""
                except Stop:
                    raise
                except Exception as e:                     # 한 방식이 실패해도 다음 방식은 계속
                    import traceback as _tb
                    fr_ = [f for f in _tb.extract_tb(e.__traceback__) if str(f.filename).endswith(".py")][-2:]
                    self.log("  ⚠ %s 실패: %s: %s%s" % (name, type(e).__name__, e, ("  (위치: %s)" % " ← ".join(
                        "%s %d줄 %s" % (os.path.basename(f.filename), f.lineno, f.name) for f in reversed(fr_))) if fr_ else ""))
                    err = str(e)
                pd_m = getattr(self, "last_pd", None)
                rs = [r for r in self.results[n0:] if r.get("kind") == "dpd"]
                if any(r.get("aclr_src") == "fsw" for r in rs):          # Mod. 화면 값이 있으면 그것끼리 (I/Q 값과 섞지 않음)
                    rs = [r for r in rs if r.get("aclr_src") == "fsw"]
                best = min(rs, key=lambda r: C.aclr_worst(r)) if rs else None
                self.compare.append(dict(name=name, model=mdl, learn=lrn, best=best, t=time.time() - t0,
                                         n_coef=self.model.n_coef() if self.model is not None else 0,
                                         kml=("M%d H%d" % (p["M"], p.get("nn_hidden", 24)) if mdl in ("nn", "pnn", "tnn") else
                                              "구간 %d · M%d" % (p.get("dvr_seg", 8), p["M"]) if mdl == "dvr" else
                                              "K%d M%d %s" % (p["K"], p["M"], self.model.lag_text() if hasattr(self.model, "lag_text")
                                                               else "L%d" % p["L"])), err=err))
                if best is not None:
                    sc_ = C.aclr_worst(best)
                    if getattr(self, "_cmp_best", None) is None or sc_ < self._cmp_best[0]:
                        self._cmp_best = (sc_, pd_m, p["sg_dbm"])
            if with_ilc:
                self.check()
                self.tag = "ILC 한계"
                t0 = time.time()
                b = self._ilc_level(y_off, "ILC · ", stp)
                self.compare.append(dict(name="ILC 한계", model="ilc", learn="", best=b, t=time.time() - t0, n_coef=0, kml="-", err=""))
        finally:
            p.update(keep)
            self.model = self.make_model()
            self.tag = None
        self.log("━━ 방식 비교 결과 (DPD OFF %.1f / %.1f dBc)" % (r_off["aclr_l"], r_off["aclr_u"]))
        for c in self.compare:
            b = c["best"]
            if b:
                self.log("  %-14s ACLR %.1f / %.1f dBc · EVM %.2f %% · Pout %.2f dBm · %.0f초 · 계수 %d"
                         % (c["name"], b["aclr_l"], b["aclr_u"], b["evm"], b["pout"], c["t"], c["n_coef"]))
            else:
                self.log("  %-14s 결과 없음 %s" % (c["name"], c["err"]))
        self.compare_off = r_off
        if self._cmp_best and self._cmp_best[1] is not None:
            self.last_pd, self.last_sg = self._cmp_best[1], self._cmp_best[2]     # 최종 신호 = 가장 좋은 방식
        return self.results

    def _ilc_level(self, y_al, label_pre, step, resume=None):
        """ILC: 모델 없이 측정을 되풀이하며 'PA 출력 = 원래 신호' 가 되는 입력을 샘플마다 직접 찾음 → 이 PA 의 한계 ACLR"""
        p, x = self.p, self.sig.x
        n = int(p.get("ilc_iters", 10))
        g = float(p.get("ilc_gain", 0.8))
        lim = np.max(np.abs(x)) * 10 ** (p["peak_lim_db"] / 20)
        u = x.copy()
        best = None            # (점수, 결과, 그때 입력 u, 그때 y_al)
        gk = g
        k0 = 0
        if resume:                                         # 이어서: 앞 ILC 의 가장 좋았던 입력 · 이득에서 계속
            u, y_al, gk, best, k0 = resume["u"].copy(), resume["y_al"], resume["gk"], resume["best"], resume["k"]
            n = int(resume["n"])
        try:
            best, u, y_al, gk, _k = self._ilc_loop(x, u, y_al, best, gk, k0, n, lim, label_pre, step)
        except Stop:
            best = getattr(self, "_ilc_best", None) or best
            if best is not None:                           # JSH: 중지해도 그때까지 가장 좋았던 입력을 결과로 남김 → 파일 저장 · ILC→모델 가능
                self._ilc_u = best[2]
                sg_b = best[1].get("base_sg") if isinstance(best[1], dict) else None
                sg_b = float(sg_b) if sg_b is not None and sg_b == sg_b else self.p["sg_dbm"]
                self._resume = dict(kind="ilc", sg=sg_b, sg_end=self.p["sg_dbm"], u=best[2], y_al=best[3], gk=gk, best=best,
                                    k=self._ilc_k, label_pre=label_pre, step=step, partial=True)
                self.log("  ■ ILC 중지 - 지금까지 %d 번 중 최고 %.1f dBc 를 결과로 남김 (ILC 결과로 모델 만들기 가능)"
                         % (self._ilc_k - k0, best[0]))
                self._ilc_done(partial=True)
            raise
        self._ilc_u = best[2]
        sg_b = best[1].get("base_sg") if isinstance(best[1], dict) else None
        sg_b = float(sg_b) if sg_b is not None and sg_b == sg_b else self.p["sg_dbm"]      # ILC 가장 좋았던 순간의 SG
        self._resume = dict(kind="ilc", sg=sg_b, sg_end=self.p["sg_dbm"], u=best[2], y_al=best[3], gk=gk, best=best, k=k0 + n,
                            label_pre=label_pre, step=step)
        self._ilc_done(partial=False)                      # JSH: ILC 가 끝나면 (어느 실행에서든) 파일로 저장
        r_fin = self._final_mod(best[2], label_pre, step, kind="ilc")
        self.last_pd, self.last_sg = best[2], self.p["sg_dbm"]       # 최종 신호 = ILC 가장 좋았던 입력
        best = (best[0], r_fin or best[1])
        self.log("  ✔ 한계 ACLR (ILC 최고): %.1f / %.1f dBc · Pout %.2f dBm" % (best[1]["aclr_l"], best[1]["aclr_u"], best[1]["pout"]))
        return best[1]

    def _ilc_done(self, partial):
        """JSH: ILC 결과가 생기면 화면 쪽 (on_ilc) 에 알림 → .npz 저장"""
        cb = getattr(self, "on_ilc", None)
        if cb is None:
            return
        try:
            cb(self, partial)
        except Exception as e:
            self.log("  ⚠ ILC 결과 저장 못 함: %s" % e)

    def _ilc_loop(self, x, u, y_al, best, gk, k0, n, lim, label_pre, step):
        """ILC 반복 (원래 _ilc_level 안의 반복 그대로) → (best, u, y_al, gk, 마지막 k)"""
        p = self.p
        self._ilc_k, self._ilc_best = k0, best
        for k in range(k0 + 1, k0 + n + 1):
            self.check()
            self.log("▶ %sILC %d / %d (한계 ACLR 찾기 · 이득 %.2f)" % (label_pre, k, k0 + n, gk))
            e = x - y_al / self.G
            u = u + gk * self.focus_weight(e)
            a = np.abs(u)
            n_clip = int(np.sum(a > lim))
            u = np.where(a > lim, u * lim / np.maximum(a, 1e-12), u)
            if n_clip:
                self.log("  최고값 제한 (+%.1f dB) 에 걸린 샘플 %d 개 (많으면 ILC 가 못 내려감 → 4 카드 최고값 제한 ↑)" % (p["peak_lim_db"], n_clip))
            nm = 10 * np.log10(np.mean(np.abs(e) ** 2) / np.mean(np.abs(x) ** 2))
            y_al, r = self.measure(u, "%sILC %d" % (label_pre, k), k, nm, step, kind="ilc")
            sc = C.aclr_worst(r)
            fd = self.focus_dbc(y_al)
            if fd is not None:                         # 집중 구간을 정했으면 그 구간 전력으로 좋고 나쁨을 판단
                sc = max([v_ for v_ in fd if v_ == v_] or [sc])
                self.log("  집중 구간 %s MHz: %.1f / %.1f dBc" % (self.p.get("focus_mhz"), fd[0], fd[1]))
            if best is None or sc < best[0]:
                best = (sc, r, u.copy(), y_al)
            elif sc > best[0] + 0.5:
                # 나빠지면 가장 좋았던 입력으로 돌아가서 이득을 반으로 (측정 잡음 · 과보정) → 천천히 더 내려감
                # (정한 반복 횟수는 끝까지 돌림 · 이득은 0.1 아래로 안 내림)
                gk = max(gk * 0.5, 0.1)
                u, y_al = best[2].copy(), best[3]
                self.log("  ILC 나빠짐 (%.1f > 최고 %.1f dBc%s) → 최고 때로 돌아가 이득 %.2f"
                         % (sc, best[0], " · 집중 구간" if fd is not None else "", gk))
            if r.get("clipped"):
                self.log("  ⚠ SG 레벨이 SG 최대에 걸림 → ILC 가 입력을 더 못 키움 (2 카드 SG 최대 확인)")
            self._ilc_best, self._ilc_k = best, k               # JSH: 중지돼도 여기까지는 남김
        return best, u, y_al, gk, self._ilc_k

    def run_ilc(self, levels=None):
        """ILC 로 한계 ACLR: levels 없으면 지금 SG 레벨 한 번, 있으면 레벨마다 (DPD OFF → ILC)"""
        self._wlog = False
        self._plog = False
        self._pk_note = False
        self._acp_warn = False
        self._bwlog = False
        self._calm_warn = False
        self.stop = False
        self.results = []
        self._cal_done = False
        self._calm_done = False
        self._calm_bad = False
        self.connect()
        if self.sig is None:
            self.make_signal()
        if self.model is None:                            # 신호를 이어 쓴 경우 (중지 뒤 다시 실행) 에도 모델은 꼭
            self.model = self.make_model()
        x = self.sig.x
        if not levels:
            self.lock_sg()                             # 한 레벨 ILC: 목표 Pout 에 SG 맞춘 뒤
        base = self.p["sg_dbm"]
        levels = levels or [base]
        per_lv = 1 + int(self.p.get("ilc_iters", 10))
        self.eta_reset(per_lv * len([l for l in levels if l <= self.p["sg_max_dbm"] + 1e-9]))
        try:
            for i, lv in enumerate(levels):
                self.check()
                if lv > self.p["sg_max_dbm"] + 1e-9:
                    break
                self.eta_level(per_lv)
                self.p["sg_dbm"] = lv
                step = "레벨 %d/%d · SG %.2f dBm" % (i + 1, len(levels), lv) if len(levels) > 1 else "SG %.2f dBm" % lv
                self.G = None
                self.log("━━ %s · DPD OFF" % step)
                y_al, r_off = self.measure(x, "OFF", 0, step=step)
                self._ilc_level(y_al, "", step)
        finally:
            self.p["sg_dbm"] = base
        return self.results

    def run_sweep(self, levels, pout_stop=None):
        """파워 스윕: SG 레벨마다 DPD OFF 측정 → DPD 학습 (앞 레벨 계수로 시작) → DPD ON 측정"""
        self.stop = False
        self.results = []
        self._cal_done = False
        self._calm_done = False
        self._calm_bad = False
        self.connect()
        if self.sig is None:
            self.make_signal()
        if self.model is None:                            # 신호를 이어 쓴 경우 (중지 뒤 다시 실행) 에도 모델은 꼭
            self.model = self.make_model()
        x = self.sig.x
        w = None
        base = self.p["sg_dbm"]
        tgt = self.p.get("target_aclr")
        auto = self.p.get("auto_model") and self.p.get("model", "gmp") == "gmp"
        n_it = int(self.p["iters"])
        nl = len([l for l in levels if l <= self.p["sg_max_dbm"] + 1e-9])
        plan = lambda first: 1 + ((self.auto_n() + max(n_it - 1, 1)) if (first and auto) else (n_it + (0 if first else 1))) + self.ec_n()
        self.eta_reset(sum(plan(i == 0) for i in range(nl)))
        try:
            for i, lv in enumerate(levels):
                self.check()
                if lv > self.p["sg_max_dbm"] + 1e-9:
                    self.log("SG %.2f dBm > SG 최대 → 스윕 끝" % lv)
                    break
                self.eta_level(plan(w is None))
                self.p["sg_dbm"] = lv
                step = "레벨 %d/%d · SG %.2f dBm" % (i + 1, len(levels), lv)
                self.log("━━ %s" % step)
                self.G = None
                y_al, r_off = self.measure(x, "OFF", 0, step=step)
                if self.p.get("auto_model") and self.p.get("model", "gmp") == "gmp" and w is None:          # 첫 레벨에서만 모델을 찾고 다음 레벨은 그 모델로
                    y_al, pd, last, w = self.auto_model(y_al, "", step)
                    last, w, pd = self._dpd_level(y_al, w, "", step, tgt, start=(pd, last))
                else:
                    last, w, pd = self._dpd_level(y_al, w, "", step, tgt)
                self.last_pd = pd
                if pout_stop is not None and r_off["pout"] >= pout_stop:
                    self.log("Pout %.2f dBm ≥ 멈춤 %.2f dBm → 스윕 끝" % (r_off["pout"], pout_stop))
                    break
                self.eta_level_end()
        finally:
            self.p["sg_dbm"] = base
        return self.results

    def ilc_save(self, path, meta):
        """ILC 결과 (가장 좋았던 입력 u · SG · 원래 신호 x) 를 파일로 → 나중에 ILC 다시 안 돌리고 바로 모델 학습"""
        import json
        st = getattr(self, "_resume", None)
        if not st or st.get("kind") != "ilc" or self.sig is None:
            return None
        np.savez_compressed(path, u=np.asarray(st["u"], dtype=np.complex64), x=np.asarray(self.sig.x, dtype=np.complex64),
                            sg=float(st["sg"]), best=float(st["best"][0]), fs=float(self.sig.fs), meta=json.dumps(meta, ensure_ascii=False))
        return path

    @staticmethod
    def ilc_load(path):
        """저장한 ILC 결과 → _resume 모양 (x_ref = 저장 때 신호, 지금 신호와 같은지 확인용)"""
        import json
        d = np.load(path, allow_pickle=False)
        meta = json.loads(str(d["meta"])) if "meta" in d.files else {}
        u = d["u"].astype(complex)
        best = float(d["best"])
        sg = float(d["sg"])
        return dict(kind="ilc", sg=sg, sg_end=sg, u=u, y_al=None, gk=None, best=(best, dict(meta.get("r") or {}), u, None),
                    k=0, label_pre="", step="", x_ref=d["x"].astype(complex), fs_ref=float(d["fs"]), meta=meta, path=path)

    def run_ilc_model(self):
        """ILC 결과로 모델: 마지막 ILC 의 가장 좋았던 입력 u 를 정답으로 지금 모델 (GMP 등) 이 x → u 를 배움
        → 측정 → (반복 횟수 − 1) 번 ILA 등으로 다듬음. ILC 는 다시 안 돌림"""
        for a_ in ("_wlog", "_plog", "_pk_note", "_acp_warn", "_bwlog", "_calm_warn"):
            setattr(self, a_, False)
        st = getattr(self, "_resume", None)
        if not st or st.get("kind") != "ilc":
            raise RuntimeError("ILC 결과가 없음 - [한계 ACLR (ILC)] 를 먼저 실행")
        self.stop = False
        self.results = []
        self.connect()
        if self.sig is None:
            raise RuntimeError("신호가 없음")
        p = self.p
        xr = st.get("x_ref")
        if xr is not None:                                   # 저장한 ILC 를 불러온 경우: 지금 신호가 그때 신호와 같아야 함
            x0 = self.sig.x
            same = len(xr) == len(x0) and abs(float(st.get("fs_ref") or self.sig.fs) - self.sig.fs) < 1.0
            if same:
                d = 10 * np.log10(np.mean(np.abs(x0 - xr) ** 2) / np.mean(np.abs(x0) ** 2) + 1e-30)
                same = d < -40
            if not same:
                raise RuntimeError("불러온 ILC 결과의 신호와 지금 신호가 다름 (샘플 %d vs %d · %.2f vs %.2f MSps) → "
                                   "1 카드 신호 (파일 · 대역 · CFR · 샘플레이트) 를 ILC 때와 같게"
                                   % (len(xr), len(x0), float(st.get("fs_ref") or 0) / 1e6, self.sig.fs / 1e6))
            m_ = st.get("meta") or {}
            self.log("  불러온 ILC: %s · %s · ILC 최고 %.1f dBc · SG %.2f dBm%s" % (
                os.path.basename(st.get("path") or ""), m_.get("when", ""), st["best"][0], st["sg"],
                (" · %s GHz" % m_.get("freq")) if m_.get("freq") is not None else ""))
            if m_.get("w_mode") and m_.get("w_mode") != "time" and not self.weight_bands():
                # ILC 때 집중 구간 / ACLR 가중을 썼는데 지금은 꺼져 있음 → 그때 설정으로 가중 학습
                for k_ in ("w_mode", "focus_mhz", "focus_w"):
                    if m_.get(k_) is not None:
                        p[k_] = m_[k_]
                self.log("  ILC 때 보정 기준 (%s · %s MHz · 가중 %s 배) 으로 학습 · 반복" % (m_.get("w_mode"), m_.get("focus_mhz"), m_.get("focus_w")))
            try:
                if m_.get("freq") is not None and abs(float(m_["freq"]) - float(p["freq_ghz"])) > 1e-6:
                    self.log("  ⚠ 주파수가 다름: 저장 %.4f GHz · 지금 %.4f GHz → ILC 결과가 안 맞을 수 있음" % (float(m_["freq"]), float(p["freq_ghz"])))
            except Exception:
                pass
        base = p["sg_dbm"]
        p["sg_dbm"] = st["sg"]
        x, u = self.sig.x, st["u"]
        n_it = int(p["iters"])
        self.eta_reset(n_it)
        self.eta_level(n_it)
        stp = "SG %.2f dBm · ILC 결과로" % st["sg"]
        try:
            self.model = self.make_model()
            self.tag = method_label(p) + " ← ILC"
            self.log("━━ ILC 결과로 모델 만들기: %s (ILC 최고 %.1f dBc · ILC 다시 안 돌림)" % (method_label(p), st["best"][0]))
            t0 = time.time()
            if is_nn(p):
                w, nm = self.model.fit(x, u, n_fit=p["n_fit"], init=None, cancelled=lambda: self.stop)
            else:
                w, nm = self.model.fit(x, u, n_fit=p["n_fit"])
            self.check()
            self.model.w = w
            self.log("  모델 학습 (x → ILC 입력) %.1f초 · 오차 %.1f dB" % (time.time() - t0, nm))
            w, nm = self._ilcm_widen(x, u, w, nm)
            w, nm = self._ilcm_lin(x, u, w, nm)
            w, nm = self._ilcm_trap(x, u, w, nm)
            w, nm = self._ilcm_focus(x, u, w, nm)
            self.log_model_params("ILC→모델 학습", w, nm)        # JSH
            pd, n_clip = self.dpd_signal(w)
            self.log("  최고값 제한 (+%.1f dB) 에 걸린 샘플 %d 개%s" % (p["peak_lim_db"], n_clip,
                     "  ⚠ 많음 → 모델 출력 피크가 잘림 (4 카드 최고값 제한 ↑)" if n_clip > 0.0005 * len(pd) else ""))
            self.ilcm_gap(x, u, pd)
            if abs(st.get("sg_end", st["sg"]) - st["sg"]) > 0.005:
                self.log("  SG: ILC 가장 좋았던 순간 %.2f dBm 로 (ILC 끝 %.2f dBm)" % (st["sg"], st["sg_end"]))
            y_al, last = self.measure(pd, "ILC→모델", 1, nm, stp)
            meth0 = p.get("method")
            if meth0 == "ilcm":
                p["method"] = "ila"                        # 다듬기는 ILA (ILC 는 이미 했음)
            self._keep_best = (C.aclr_worst(last), w, pd, last, y_al)
            try:
                _r, w_fin, _pd = self._dpd_level(y_al, w, "ILC→모델 · ", stp, p.get("target_aclr"), start=(pd, last))
                self.log_model_params("ILC→모델 최종 (ILA 다듬은 뒤)", w_fin)   # JSH
            finally:
                p["method"] = meth0
                self._keep_best = None
            self.ilcm_verdict(st["best"][0], nm)
        finally:
            p["sg_dbm"] = base
        return self.results

    def _fit_focus(self, m, x, u):
        """ILC 결과로 모델: ILC 와 같은 주파수 가중으로 학습 (집중 구간 오차를 그대로, 나머지는 1/가중)
        → ILC 가 집중 구간에 몰아서 만든 보정을 모델이 먼저 따라감. (w, 가중 오차 dB, 그냥 오차 dB) · 못 하면 None"""
        bands = self.weight_bands()
        if not bands or not hasattr(m, "_basis_seg") or not self.p.get("ilcm_focus_fit", True):
            return None
        n = len(x)
        nf = min(int(self.p.get("n_fit") or 40000), n)
        s0 = int(np.random.default_rng(0).integers(0, n - nf + 1))
        Phi = m._basis_seg(x, s0, nf)
        d = u[s0:s0 + nf]
        wgt = max(1.0, float(self.p.get("focus_w") or 4.0))
        f = np.fft.fftfreq(nf, 1.0 / self.sig.fs)
        mk = np.zeros(nf, bool)
        for lo_, hi_ in bands:
            mk |= (f >= lo_) & (f <= hi_)
        W = np.where(mk, 1.0, 1.0 / wgt)[:, None]
        Pf = np.fft.ifft(np.fft.fft(Phi, axis=0) * W, axis=0)
        df = np.fft.ifft(np.fft.fft(d) * W[:, 0])
        sc = np.sqrt(np.mean(np.abs(Pf) ** 2, axis=0))
        sc[sc == 0] = 1.0
        P = Pf / sc
        A = P.conj().T @ P
        lam = m.lam * np.trace(A).real / A.shape[0]
        w = np.linalg.solve(A + lam * np.eye(A.shape[0]), P.conj().T @ df) / sc
        nm_w = 10 * np.log10(np.mean(np.abs(df - Pf @ w) ** 2) / np.mean(np.abs(df) ** 2))
        nm = 10 * np.log10(np.mean(np.abs(d - Phi @ w) ** 2) / np.mean(np.abs(d) ** 2))
        return w, float(nm_w), float(nm)

    def _ilcm_lin(self, x, u, w, nm):
        """(넓은 대역) GMP 에 긴 선형 등화 (±N 탭) 를 붙여 다시 학습 → 좋아지면 바꿈. 먼저 '선형만' 으로 채널 안 오차 진단"""
        p = self.p
        if not p.get("ilcm_lin", True) or p.get("model", "gmp") != "gmp" or type(self.model) is not C.DPDModel:
            return w, nm
        nl = int(p.get("lin_taps") or 64)
        try:
            t0 = time.time()
            nf = min(int(p["n_fit"]), 30000)
            ml = C.LinGMPModel(1, 1, 0, False, p["lam"], nl)          # 선형만 (긴 FIR)
            _wl, nml = ml.fit(x, u, n_fit=nf)
            m0 = C.DPDModel(1, 1, 0, False, p["lam"])                 # 선형 1 탭 (그냥 크기 · 위상만)
            _w0, nm0 = m0.fit(x, u, n_fit=nf)
            self.log("  선형 진단: 크기 · 위상만 %.1f dB → 긴 선형 등화 ±%d 탭 %.1f dB = %.1f dB 좋아짐 (크면 대역 안 리플 · 지연이 원인)"
                     % (nm0, nl, nml, nm0 - nml))
            m2 = C.LinGMPModel(self.model.K, self.model.M, self.model.L, self.model.odd, p["lam"], nl,
                               getattr(self.model, "lag", None), getattr(self.model, "lead", None))      # JSH: Lagging · Leading 그대로
            w2, nm2 = m2.fit(x, u, n_fit=p["n_fit"])
            self.check()
            if nm2 < nm - 0.3:
                self.model, w = m2, w2
                self.model.w = w
                self.log("  ✔ GMP + 긴 선형 등화 ±%d 탭: 학습 오차 %.1f → %.1f dB (%.1f초)" % (nl, nm, nm2, time.time() - t0))
                nm = nm2
                if isinstance(getattr(self, "tag", None), str):
                    self.tag = self.tag.replace("GMP", "GMP+FIR", 1)
            else:
                self.log("  긴 선형 등화 붙여도 거의 같음 (%.1f dB) → GMP 그대로" % nm2)
        except Exception as e:
            self.log("  (긴 선형 등화 실패: %s)" % e)
        return w, nm

    def _ilcm_trap(self, x, u, w, nm):
        """긴 메모리 (GaN 트래핑 · 열) 진단: 느린 평균 전력 항을 더해 다시 학습 → 좋아지면 바꿈"""
        p = self.p
        if not p.get("ilcm_trap", True) or p.get("model", "gmp") != "gmp" or type(self.model) not in (C.DPDModel, C.LinGMPModel):
            return w, nm
        try:
            taus = tuple(float(t) for t in str(p.get("trap_us") or "0.1,1,10").replace(" ", "").split(",") if t)
        except ValueError:
            taus = (0.1, 1.0, 10.0)
        m0 = self.model
        try:
            t0 = time.time()
            m2 = C.TrapGMPModel(m0.K, m0.M, m0.L, m0.odd, p["lam"], getattr(m0, "nlin", 0), self.sig.fs, taus,
                                getattr(m0, "lag", None), getattr(m0, "lead", None))                     # JSH: Lagging · Leading 그대로
            w2, nm2 = m2.fit(x, u, n_fit=p["n_fit"])
            self.check()
            gain = nm - nm2
            self.log("  긴 메모리 진단 (GaN 트래핑 · 열, 느린 평균 전력 %s µs): 학습 오차 %.1f → %.1f dB = %.1f dB 좋아짐 (%.1f초)%s"
                     % ("·".join("%g" % t for t in taus), nm, nm2, gain, time.time() - t0,
                        "  → 긴 메모리가 원인 (크게 좋아짐)" if gain >= 3 else "  → 긴 메모리는 주원인 아님" if gain < 1 else ""))
            if gain > 0.3:
                self.model, w, nm = m2, w2, nm2
                self.model.w = w
                self.log("  ✔ 긴 메모리 항을 붙인 모델로 바꿈")
                if isinstance(getattr(self, "tag", None), str) and "+TRAP" not in self.tag:
                    self.tag = self.tag.replace("GMP", "GMP+TRAP", 1)
        except Exception as e:
            self.log("  (긴 메모리 진단 실패: %s)" % e)
        return w, nm

    def _ilcm_focus(self, x, u, w, nm):
        """집중 구간을 썼으면 같은 가중으로 다시 학습해서 바꿈 → (w, nm)"""
        try:
            r_ = self._fit_focus(self.model, x, u)
        except Exception as e:
            self.log("  (집중 구간 가중 학습 실패: %s → 그냥 학습 그대로)" % e)
            return w, nm
        if r_ is None:
            return w, nm
        w2, nmw, nm2 = r_
        self.model.w = w2
        self.log("  집중 구간 가중 학습 (ILC 와 같은 가중 %s 배): 가중 오차 %.1f dB (그냥 오차 %.1f dB · 그냥 학습 때 %.1f dB)"
                 % (self.p.get("focus_w") or 4, nmw, nm2, nm))
        self.log("    → ILC 가 집중 구간에 몰아 만든 보정을 먼저 따라가게 배움 (그냥 오차는 커져도 정상 · 판단은 '가중 오차' 와 아래 '집중 구간 차이')")
        self._ilcm_fitw = nmw
        return w2, nmw

    def _ilcm_widen(self, x, u, w, nm):
        """(나) ILC → 모델 학습 오차가 크면 K · M · L 을 넓혀 계산으로 다시 찾음 (측정 없음) → (w, nm)"""
        p = self.p
        nm0, kml0 = nm, (p["K"], p["M"], p["L"])
        th = float(p.get("ilcm_fit_db", -40.0))
        if p.get("ilcm_widen", True) and nm > th and p.get("model", "gmp") == "gmp" and not is_nn(p):
            # (나) 학습 오차가 크면 = 지금 K · M · L 로는 ILC 입력을 다 못 흉내 냄 → 넓은 조합에서 계산으로 다시 (측정 없음)
            self.log("  학습 오차 %.1f dB > %.0f dB → K · M · L 넓혀 다시 찾기 (계산만, 측정 없음)…" % (nm, th))
            self.on_progress(self.eta_text() + " · ILC→모델 K·M·L 넓혀 찾는 중")
            ratio = max(1, int(round(self.sig.fs / 491.52e6)))
            Ms = tuple(sorted({max(1, int(round(m * ratio))) for m in (1, 2, 3, 4, 6, 8)}))
            Ls = tuple(sorted({int(round(l * ratio)) for l in (0, 1, 2, 3)}))
            t1 = time.time()
            res = C.model_search(x, u, p["odd_only"], p["lam"], n_fit=min(int(p["n_fit"]), 30000),
                                 Ks=(5, 7, 9, 11), Ms=Ms, Ls=Ls, cancelled=lambda: self.stop)
            self.check()
            pk = C.pick_models(res, 1)
            if pk:
                nmv, nc, K, M, L = pk[0]
                self.log("  %d 조합 %.1f초 · 가장 좋은: K%d M%d L%d (검증 오차 %.1f dB, %d 계수)"
                         % (len(res), time.time() - t1, K, M, L, nmv, nc))
                if (K, M, L) != kml0:
                    m2 = self.make_model(K, M, L)
                    w2, nm2 = m2.fit(x, u, n_fit=p["n_fit"])
                    self.check()
                    if nm2 < nm - 0.5:
                        self.model, w, nm = m2, w2, nm2
                        self.model.w = w
                        p["K"], p["M"], p["L"] = K, M, L
                        if p.get("L_lag") is not None or p.get("L_lead") is not None:     # 넓혀 찾은 모델은 L 하나로 앞뒤 같음
                            p["L_lag"] = p["L_lead"] = None
                            self.log("  (Lagging · Leading 따로 정한 값 대신 찾은 L%d 로 앞뒤 같게)" % L)
                        self.tag = method_label(p) + (" ← ILC" if str(getattr(self, "tag", "")).endswith("← ILC") else "")
                        self.log("  ✔ K%d M%d L%d 로 바꿈: 학습 오차 %.1f → %.1f dB" % (K, M, L, nm0, nm))
                        cb = getattr(self, "on_kml", None)
                        if cb:
                            try:
                                cb(K, M, L)
                            except Exception:
                                pass
                    else:
                        self.log("  넓혀도 학습 오차가 거의 안 줄어듦 (%.1f dB) → K%d M%d L%d 그대로" % (nm2, kml0[0], kml0[1], kml0[2]))
        return w, nm

    def ilcm_gap(self, x, u, pd):
        """(측정 없이) 모델 출력 pd 가 ILC 입력 u 와 얼마나 다른지: 차이 신호의 인접 채널 전력 / 원래 신호 채널 전력
        → 이 값이 ILC ACLR 보다 높으면 모델 ACLR 도 대략 그 근처에서 막힘"""
        try:
            fs = self.sig.fs
            bwc, adj = self.p.get("ch_bw_mhz", 98.28) * 1e6, self.p.get("adj_mhz", 100.0) * 1e6
            f, P = C.welch_psd(u, fs)
            pc, _ = C.band_power(f, P, 0.0, bwc)
            f, Pe = C.welch_psd(pd - u, fs)
            pl, _ = C.band_power(f, Pe, -adj, bwc)
            pu, _ = C.band_power(f, Pe, adj, bwc)
            ec, _ = C.band_power(f, Pe, 0.0, bwc)
            g = 10 * np.log10(max(pl, pu) / pc)
            bands = self.weight_bands()
            fz = ""
            if bands:                                   # 집중 구간 (ILC 가 몰아서 고친 곳) 의 차이
                pf_ = max(C.band_power(f, Pe, 0.5 * (a_ + b_), b_ - a_)[0] for a_, b_ in bands)
                gf = 10 * np.log10(pf_ / pc)
                fz = " · 집중 구간 %.1f dBc" % gf
                g = gf
            self.log("  모델 출력 vs ILC 입력 차이 (계산): 인접 채널 %.1f dBc%s · 채널 안 %.1f dB  → 모델 ACLR 이 대략 이 근처에서 막힘%s"
                     % (10 * np.log10(max(pl, pu) / pc), fz, 10 * np.log10(ec / pc),
                        "  ⚠ 모델이 ILC 를 못 따라감 (K·M·L ↑ · 신경망)" if g > -45 else ""))
            self._ilcm_gapdb = g
            return g
        except Exception as e:
            self.log("  (모델 차이 계산 실패: %s)" % e)
            return None

    def ilcm_verdict(self, ilc_best, fit_db):
        """(가) ILC 결과로 모델 만들기 끝 → 모델 최고 ACLR 을 ILC 최고와 비교 · 학습 오차로 어디가 한계인지 판정"""
        vals = [C.aclr_worst(r) for r in self.results if r.get("kind") in ("dpd", "ilc") and r.get("aclr_l") is not None]
        vals = [v for v in vals if v == v]
        if not vals:
            return None
        mb = min(vals)
        gap = mb - ilc_best
        th = float(self.p.get("ilcm_fit_db", -40.0))
        if gap <= 1.0:
            why = ("모델이 ILC 만큼 나옴 → 모델은 한계 아님. 더 내리려면 ILC 자체가 막힌 곳 (측정 바닥 · PA) 을 봐야 함: "
                   "FSW 감쇠 줄이기 · Ref 를 피크 바로 위로 · 노이즈 캔슬 · 스윕 시간 늘리기, "
                   "PA 없이 SG → FSW 로 같은 Pout ACLR 을 재 비교 (비슷하면 장비 바닥)")
            kind = "측정 · ILC 한계"
        elif fit_db > th:
            why = ("학습 오차가 큼 (%.1f dB) → 모델이 ILC 입력을 다 못 흉내 냄 = 모델 한계. "
                   "K · M · L 키우기 · 4 카드 자동 [넓게] · 다른 모델 (신경망) 시도" % fit_db)
            kind = "모델 한계"
        else:
            why = ("학습은 잘 됐는데 (%.1f dB) 측정에서 못 따라감 → 반복 사이 PA 상태 변화 (온도 · 메모리) · "
                   "이득 차이일 수 있음. 반복 횟수 늘리기 · Pout 딱 맞춤 확인 · [이어서 더]" % fit_db)
            kind = "모델 학습 OK · 측정에서 차이"
        self.log("━━ 판정: ILC 최고 %.1f dBc · 모델 최고 %.1f dBc (ILC 보다 %+.1f dB) · 학습 오차 %.1f dB → %s"
                 % (ilc_best, mb, gap, fit_db, kind))
        self.log("   " + why)
        v = dict(ilc=ilc_best, model=mb, gap=gap, fit=fit_db, kind=kind, why=why)
        cb = getattr(self, "on_verdict", None)
        if cb:
            try:
                cb(v)
            except Exception:
                pass
        return v

    def run_more(self, n):
        """끝난 (또는 중지한) DPD · ILC 를 그 결과에서 이어서 n 번 더 (같은 SG 레벨 · 같은 계수 / 입력)"""
        self._wlog = False
        self._plog = False
        self._pk_note = False
        self._acp_warn = False
        self._bwlog = False
        self._calm_warn = False
        st = getattr(self, "_resume", None)
        if not st:
            raise RuntimeError("이어서 할 결과가 없음 - DPD 또는 ILC 를 먼저 실행")
        self.stop = False
        self.results = []
        self.connect()
        if self.sig is None:
            raise RuntimeError("신호가 없음")
        if self.model is None:
            self.model = self.make_model()
        p = self.p
        base, base_it = p["sg_dbm"], p["iters"]
        p["sg_dbm"] = st["sg"]
        self.eta_reset(int(n) + 1)
        self.eta_level(int(n) + 1)
        stp = "SG %.2f dBm · 이어서" % st["sg"]
        try:
            if st["kind"] == "ilc":
                self.log("━━ ILC 이어서 %d 번 (지금까지 %d 번 · 최고 %.1f dBc)" % (n, st["k"], st["best"][0]))
                rs = dict(st)
                rs["n"] = n
                self._ilc_level(st["y_al"], st.get("label_pre", ""), stp, resume=rs)
            else:
                self.log("━━ DPD 이어서 %d 번 (%s)" % (n, method_label(p)))
                self.model.w = st["w"]
                p["iters"] = int(n) + 1                      # start 를 주면 반복 = iters - 1
                tgt = p.get("target_aclr")
                self._dpd_level(st["y_al"], st["w"], "이어서 · ", stp, tgt, start=(st["pd"], st["last"]))
        finally:
            p["sg_dbm"], p["iters"] = base, base_it
        return self.results

    def send_final(self, pd=None, sg=None):
        """최종 DPD 신호 (마지막 실행의 마지막 · 가장 좋은 입력) 를 SMW 로 보내고 RF 를 켜 둠 (계속 내보냄)"""
        pd = getattr(self, "last_pd", None) if pd is None else pd
        if pd is None:
            raise RuntimeError("보낼 최종 DPD 신호가 없음 - DPD (또는 ILC) 를 먼저 실행")
        self.stop = False
        self.connect()
        p, x = self.p, self.sig.x
        sg = getattr(self, "last_sg", p["sg_dbm"]) if sg is None else sg
        lvl = sg + 20 * math.log10(C.rms(pd) / C.rms(x))
        if lvl > p["sg_max_dbm"]:
            self.log("  ⚠ SG 레벨 %.2f dBm > 최대 %.2f dBm → 최대로 제한" % (lvl, p["sg_max_dbm"]))
            lvl = p["sg_max_dbm"]
        wv = C.write_wv(None, pd, self.sig.fs, comment="PA DPD Tester final DPD")
        self.log("▶ 최종 DPD 신호 내보내기: SG %.2f dBm · %.2f GHz · PAPR %.2f dB (%.1f MB) - RF 켜 둠"
                 % (lvl, p["freq_ghz"], C.papr(pd), len(wv) / 1e6))
        self.smw.load_wave(wv, p["freq_ghz"] * 1e9, lvl, rf_on=True)
        self.log("  ✔ SMW 가 최종 DPD 신호를 계속 내보내는 중 (끄려면 [SG RF OFF] 또는 Bench)")
        return lvl

    def fsw_like(self, r, ghz=None):
        """고른 측정 (r) 때와 같은 FSW 조건으로: Mod. 화면 감쇠 · RBW (그 측정에서 쓴 값) → 계속 스윕
        (내보낸 신호를 FSW 로 볼 때 기록값과 같은 조건 · 예전: 마지막 측정 뒤 원래 감쇠로 돌아가 있어 ACLR 이 더 나빠 보임)"""
        f = self.fsw
        if f is None or not hasattr(f, "show_like"):
            return
        att = r.get("mod_att") if isinstance(r, dict) else None
        if att is None:
            try:
                att = float(self.p.get("fsw_att_db"))
            except (TypeError, ValueError):
                att = None
        rbw = float(self.p.get("fsw_rbw_khz") or 0) * 1e3 or None
        try:
            f.acp_cfg = self.acp_cfg()
        except Exception:
            pass
        ch = f.show_like((ghz or self.p["freq_ghz"]) * 1e9, att, rbw)
        self.log("  FSW 를 기록 때 조건으로: %s%s%s → 계속 스윕" % (
            ("'%s' 화면" % ch) if ch else "Mod. 화면",
            (" · 감쇠 %.0f dB" % att) if att is not None else " · 감쇠 그대로 (기록에 없음)",
            (" · RBW %.0f kHz" % (rbw / 1e3)) if rbw else ""))

    def send_wv_file(self, raw, ghz, lvl, name=""):
        """저장해 둔 .wv (파일 그대로) 를 SMW 로 보내고 RF 를 켜 둠 - DPD 를 다시 안 돌리고 그 입력으로 바로 PA 를 돌릴 때"""
        self.stop = False
        self.connect()
        if lvl > self.p["sg_max_dbm"]:
            self.log("  ⚠ SG 레벨 %.2f dBm > 최대 %.2f dBm → 최대로 제한" % (lvl, self.p["sg_max_dbm"]))
            lvl = self.p["sg_max_dbm"]
        self.log("▶ 저장한 .wv 내보내기: %s · SG %.2f dBm · %.4g GHz (%.1f MB) - RF 켜 둠" % (name, lvl, ghz, len(raw) / 1e6))
        self.smw.load_wave(raw, ghz * 1e9, lvl, rf_on=True)
        self.log("  ✔ SMW 가 이 신호를 계속 내보내는 중 (끄려면 [SG RF OFF] 또는 Bench)")
        return lvl

    def abort(self, rf_off=True):
        """즉시 중지: 장비와 주고받던 것을 바로 끊고, 새로 연결해서 SG RF OFF · FSW 원래 모드로 (다른 스레드에서 부름)"""
        self.stop = True
        self._ref_mem = None                             # 끊긴 측정의 FSW Ref 기억 버림 (다음 시작은 자동 레벨부터)
        self._ref_extra = 0.0
        self.on_result = lambda r: None                  # 끊긴 실행의 결과는 화면에 안 올림
        self.on_progress = lambda s: None
        smw, fsw = self.smw, self.fsw
        self.smw = self.fsw = None
        for d in (smw, fsw):
            io = getattr(d, "io", None)
            if io is not None:
                io.abort()
        if smw is not None and rf_off:
            try:
                if getattr(smw, "io", None) is None:
                    smw.rf(False)                                  # 가상 장비
                else:
                    io = I.ScpiSocket(smw.io.host, smw.io.port, timeout=5.0, name="SMW")
                    io.write("OUTP1 OFF")
                    io.opc()
                    try:
                        io.write("&GTL")                       # 앞 패널 바로 쓰게 Local
                    except Exception:
                        pass
                    io.close()
                self.log("SG RF OFF")
            except Exception as e:
                self.log("⚠ SG RF OFF 실패: %s - SMW 에서 RF 를 직접 꺼 주세요" % e)
        if fsw is not None and getattr(fsw, "io", None) is not None and fsw.prev:
            try:
                fsw.io = I.ScpiSocket(fsw.io.host, fsw.io.port, timeout=10.0, name="FSW")
                fsw.restore(keep_view=True)
                try:
                    fsw.io.write("SYST:DISP:UPD ON")
                    fsw.io.write("&GTL")
                except Exception:
                    pass
                fsw.io.close()
            except Exception as e:
                self.log("⚠ FSW 원래 모드로 못 되돌림: %s" % e)

    def go_local(self, close=False):
        """끝나면 SMW · FSW 앞 패널을 바로 쓰게 Local (&GTL) · FSW 화면 켜기. close = 연결도 끊음 (연결 해제)
        다음 측정 때 명령을 보내면 장비가 알아서 다시 Remote"""
        for d in (self.smw, self.fsw):
            io = getattr(d, "io", None)
            if io is None:
                continue
            try:
                if d is self.fsw:
                    io.write("SYST:DISP:UPD ON")
                io.write("&GTL")
            except Exception:
                pass
        if close:
            for d in (self.smw, self.fsw):
                try:
                    d.close()
                except Exception:
                    pass
            self.smw = self.fsw = None

    def fsw_restore(self, keep_view=False):
        f = self.fsw
        if f is not None and hasattr(f, "restore"):
            if keep_view:
                f.restore(keep_view=True)
            else:
                f.restore()

    def rf_off(self):
        try:
            if self.smw is not None:
                self.smw.rf(False)
                self.log("SG RF OFF")
        except Exception as e:
            self.log("RF OFF 실패: %s" % e)

    def close(self):
        try:
            self.fsw_restore()
        except Exception:
            pass
        for d in (self.smw, self.fsw):
            try:
                d.close()
            except Exception:
                pass
        self.smw = self.fsw = None
