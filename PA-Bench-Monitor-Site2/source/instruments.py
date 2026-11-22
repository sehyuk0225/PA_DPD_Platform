# -*- coding: utf-8 -*-
"""PA DPD Tester - 장비 제어 (SCPI raw socket 5025, VISA 설치 필요 없음)

SMW200A : .wv 파일을 장비로 보내 ARB 로 재생 (MMEM:DATA → BB:ARB:WAV:SEL → BB:ARB:STAT ON)
FSW     : I/Q Analyzer 로 IQ 받기 (TRAC:IQ:SRAT / RLEN → INIT → TRAC:IQ:DATA:MEM?)
주소에 demo 를 넣으면 가상 장비 + 가상 PA 로 동작
"""
import re
import socket
import time

import numpy as np

import dpd_core as C


class ScpiSocket:
    def __init__(self, host, port=5025, timeout=20.0, name="장비"):
        self.name = name
        self.last = ""
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        self.timeout = timeout
        self.buf = b""
        self.dead = False
        self.host, self.port = host, port

    def abort(self):
        """중지: 기다리던 송수신을 바로 끊음 (다른 스레드에서 불러도 됨)"""
        self.dead = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    def _alive(self):
        if self.dead:
            raise ConnectionAbortedError("중지됨")

    def set_timeout(self, t):
        self.timeout = t
        self.sock.settimeout(t)

    def _recv(self, n):
        self._alive()
        try:
            chunk = self.sock.recv(n)
        except socket.timeout:
            raise TimeoutError("%s 응답 없음 (%.0f 초) - 마지막 명령: %s" % (self.name, self.timeout, self.last))
        if not chunk:
            raise ConnectionError("%s 와의 연결이 끊어졌습니다 (마지막 명령: %s)" % (self.name, self.last))
        return chunk

    def write(self, cmd):
        self._alive()
        self.last = cmd
        self.sock.sendall((cmd + "\n").encode("ascii"))

    def write_block(self, head, data):
        """head 뒤에 IEEE488.2 binary block (#<n><len><data>) 을 붙여 보냄 (예: MMEM:DATA 'path',)"""
        self._alive()
        ln = str(len(data))
        self.last = head + "#<%d bytes>" % len(data)
        self.sock.sendall(head.encode("ascii") + ("#%d%s" % (len(ln), ln)).encode("ascii") + data + b"\n")

    def query(self, cmd):
        self.write(cmd)
        while b"\n" not in self.buf:
            self.buf += self._recv(65536)
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("ascii", "replace").strip()

    def _read_exact(self, n):
        parts, have = [self.buf], len(self.buf)
        while have < n:
            chunk = self._recv(1 << 20)
            parts.append(chunk)
            have += len(chunk)
        b = b"".join(parts)
        out, self.buf = b[:n], b[n:]
        return out

    def query_block(self, cmd):
        self.write(cmd)
        while self._read_exact(1) != b"#":
            pass
        nd = int(self._read_exact(1))
        ln = int(self._read_exact(nd))
        data = self._read_exact(ln)
        if self.buf[:1] == b"\n":
            self.buf = self.buf[1:]
        return data

    def opc(self):
        return self.query("*OPC?")

    def err(self):
        try:
            return self.query("SYST:ERR?")
        except Exception as e:
            return str(e)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------- SMW200A
class SMW:
    PATH = "/var/user/pa_dpd_tx_%d.wv"

    def __init__(self, host, port=5025):
        self.io = ScpiSocket(host, port, timeout=60.0, name="SMW")
        self.idn = self.io.query("*IDN?")
        self.n = 0
        self.log = lambda s: None

    def load_wave(self, wv_bytes, freq, level, rf_on=True):
        io = self.io
        self.n += 1
        path = self.PATH % (self.n % 2)                  # 파일 이름을 번갈아 써서 ARB 가 꼭 새로 읽게
        io.write("*CLS")
        io.set_timeout(60.0 + len(wv_bytes) / 2e6)            # 큰 파일은 더 기다림
        io.write_block("MMEM:DATA '%s'," % path, wv_bytes)
        io.opc()
        e = io.err()
        if not e.startswith("0"):
            raise RuntimeError("SMW 파일 저장 오류: %s" % e)
        io.write("SOUR1:FREQ %.0f" % freq)
        io.write("SOUR1:POW %.2f" % level)
        io.write("SOUR1:BB:ARB:WAV:SEL '%s'" % path)
        io.write("SOUR1:BB:ARB:STAT ON")
        if rf_on:
            io.write("OUTP1 ON")
        io.opc()
        e = io.err()
        io.set_timeout(60.0)
        if not e.startswith("0"):
            raise RuntimeError("SMW 오류: %s" % e)
        try:
            clk = float(io.query("SOUR1:BB:ARB:CLOC?"))
            self.log("    SMW ARB 클럭 %.2f MHz" % (clk / 1e6))
        except Exception:
            pass

    def set_level(self, level):
        self.io.write("SOUR1:POW %.2f" % level)
        self.io.opc()

    def rf(self, on):
        self.io.write("OUTP1 %s" % ("ON" if on else "OFF"))
        self.io.opc()

    def close(self):
        self.io.close()


# ---------------------------------------------------------------- FSW
def fsw_screenshot(host, port=5025, timeout=20.0):
    """FSW 지금 화면 → PNG bytes. 따로 연결 (측정 중 연결과 안 섞임 · Bench 처럼 여러 연결 가능)
    HCOP 로 FSW 안에 PNG 저장 → MMEM:DATA? 로 받아 옴 → FSW 안 파일 지움. demo 면 작은 그림"""
    if str(host).strip().lower() in ("demo", ""):
        return _demo_png()
    io = ScpiSocket(host, port, timeout=timeout, name="FSW 화면")
    path = r"C:\R_S\Instr\user\pa_dpd_screen.png"
    try:
        io.write("*CLS")
        io.write("HCOP:DEST1 'MMEM'")
        io.write("HCOP:DEV:LANG1 PNG")
        io.write("MMEM:NAME '%s'" % path)
        io.write("HCOP:IMM1")
        io.opc()
        data = io.query_block("MMEM:DATA? '%s'" % path)
        try:
            io.write("MMEM:DEL '%s'" % path)
            io.opc()
        except Exception:
            pass
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            raise IOError("FSW 가 PNG 를 안 보냄 (%d bytes · %s)" % (len(data), io.err()))
        return data
    finally:
        io.close()


FSW_STATE = r"C:\R_S\Instr\user\pa_dpd_state.dfl"


def fsw_state_save(host, port=5025, timeout=30.0):
    """FSW 지금 설정 (모든 채널 · 화면 · 마커) → .dfl bytes (FSW 안에 저장 → 받아 옴 → FSW 안 파일 지움)"""
    if str(host).strip().lower() in ("demo", ""):
        return b"DEMO FSW STATE"
    io = ScpiSocket(host, port, timeout=timeout, name="FSW 설정")
    try:
        io.write("*CLS")
        io.write("MMEM:STOR1:STAT 1,'%s'" % FSW_STATE)
        io.opc()
        data = io.query_block("MMEM:DATA? '%s'" % FSW_STATE)
        try:
            io.write("MMEM:DEL '%s'" % FSW_STATE)
            io.opc()
            io.write("&GTL")
        except Exception:
            pass
        if len(data) < 100:
            raise IOError("FSW 설정 파일이 비어 있음 (%d bytes · %s)" % (len(data), io.err()))
        return data
    finally:
        io.close()


def fsw_state_load(host, data, port=5025, timeout=60.0):
    """PC 의 .dfl → FSW 로 보내고 불러오기 (모든 채널 · 화면이 저장할 때로) → Local"""
    if str(host).strip().lower() in ("demo", ""):
        return "demo"
    io = ScpiSocket(host, port, timeout=timeout, name="FSW 설정")
    try:
        io.write("*CLS")
        io.write_block("MMEM:DATA '%s'," % FSW_STATE, data)
        io.opc()
        io.write("MMEM:LOAD1:STAT 1,'%s'" % FSW_STATE)
        io.opc()
        e = io.err()
        try:
            io.write("MMEM:DEL '%s'" % FSW_STATE)
            io.write("SYST:DISP:UPD ON")
            io.write("&GTL")
        except Exception:
            pass
        return e
    finally:
        io.close()


def _demo_png(w=320, h=120):
    """demo: 회색 바탕 PNG (장비 없이 저장 흐름 확인용)"""
    import zlib
    import struct
    row = b"\x00" + bytes([40, 44, 52]) * w
    raw = row * h

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class FSW:
    CH = "DPD IQ"
    CHUNK = 131072                     # 한 번에 받는 샘플 수 (1 MB)

    def __init__(self, host, port=5025):
        self.host, self.port = host, port
        self.io = ScpiSocket(host, port, timeout=30.0, name="FSW")
        self.idn = self.io.query("*IDN?")
        self.made = False
        self.fs = None
        self.prev = None                   # DPD 측정 전 FSW 모드 (끝나면 되돌림)
        self.tidied = False                # 측정 화면 정리 했는지
        self.log = lambda s: None

    def setup(self, freq, fs, ref_dbm, att_db=None, rbw=None):
        io = self.io
        io.write("*CLS")
        self.att_fixed = att_db
        self.select_iq(fs)
        if not self.tidied and getattr(self, "via", None) != "TRAC:IQ:STAT ON":   # Spectrum 안 I/Q 방식이면 화면은 그대로
            self.tidy()
        io.write("FREQ:CENT %.0f" % freq)
        io.write("DISP:TRAC:Y:SCAL:RLEV %.2f" % ref_dbm)
        if att_db is None:
            io.write("INP:ATT:AUTO ON")
        else:
            io.write("INP:ATT %.0f" % att_db)
        io.write("TRAC:IQ:SRAT %.6f" % fs)
        if rbw:                                         # I/Q 화면 스펙트럼 창 RBW (보기용, I/Q 데이터와는 무관)
            io.write("IQ:BAND:RES %.0f" % rbw)
            if self.errors():
                io.write("BAND:RES %.0f" % rbw)
                self.errors()
        io.write("TRIG:SOUR IMM")
        io.write("INIT:CONT OFF")
        io.opc()
        e = self.errors()
        if e:
            self.log("    ⚠ FSW 설정 오류: %s" % " / ".join(e))
        try:
            got = float(io.query("TRAC:IQ:SRAT?"))
            self.fs = got
            if abs(got - fs) > 1e3:
                raise RuntimeError("FSW 샘플레이트가 %.2f MHz 로 바뀜 (요청 %.2f MHz) - FSW 대역 옵션 한계일 수 있음. "
                                   "1 신호의 샘플레이트를 낮춰 보세요 (400 MHz: 샘플레이트 ×2 끄기)." % (got / 1e6, fs / 1e6))
        except (ValueError, TimeoutError):
            pass
        try:
            return float(io.query("TRAC:IQ:BWID?"))
        except Exception:
            return float("nan")

    def errors(self):
        """FSW 오류 줄을 모두 꺼냄. 다른 프로그램 (Bench 등) 이 연결을 끊을 때 남는 '&GTL' 오류는 무시"""
        out = []
        for _ in range(20):
            e = self.io.err()
            if e.startswith("0") or e.startswith("+0"):
                break
            if "&GTL" in e.upper():
                continue
            out.append(e)
        return out

    def _inst(self):
        try:
            return self.io.query("INST?").strip().strip("'\"")
        except ConnectionAbortedError:
            raise
        except Exception as ex:
            return "? (%s)" % ex

    def _is_iq(self):
        return "IQ" in self._inst().upper()

    def _iq_works(self, fs):
        """정말 I/Q 명령을 받는지 시험: 샘플레이트 명령을 보내고 오류 (-113 Undefined header) 가 없으면 OK.
        (INST? 응답만으로는 틀릴 수 있음 - 모르는 질문에는 FSW 가 답을 안 해 멈추므로 질문 대신 쓰기 + 오류 확인)"""
        self.errors()
        self.io.write("TRAC:IQ:SRAT %.0f" % (fs or 100e6))
        try:
            self.io.opc()
        except TimeoutError:
            return False
        return not self.errors()

    def _iq_list(self):
        """INST:LIST? → I/Q 종류 채널 이름들 (예: 'IQ','DPD IQ')"""
        try:
            r = self.io.query("INST:LIST?")
        except Exception:
            return [], ""
        it = [t.strip().strip("'\"") for t in r.split(",")]
        names = [it[i + 1] for i in range(0, len(it) - 1, 2) if it[i].upper() == "IQ"]
        return names, r

    def select_iq(self, fs=None):
        """I/Q 받기가 되는 상태로. 펌웨어마다 되는 명령이 달라 차례로 시도하고, 실제로 I/Q 명령이 먹는지로 확인"""
        io = self.io
        self.errors()
        if getattr(self, "_parked", False) and getattr(self, "via", None):
            # 화면을 Mod. 화면에 세워 둔 상태 → 받기 전에 I/Q 로 (조용히)
            self._parked = False
            if self.via.startswith("TRAC:IQ"):
                io.write("TRAC:IQ:STAT ON")
                io.opc()
                self.errors()
                return
            nm = re.findall(r"'([^']*)'", self.via)
            want = (nm[-1] if nm else "IQ")
            io.write("INST:SEL '%s'" % want)
            io.opc()
            t_end = time.time() + 8.0
            while time.time() < t_end and self._inst().upper() != want.upper():
                self.io._alive()
                time.sleep(0.3)
            self.errors()
            return
        if getattr(self, "via", None):
            # 이번 실행에서 이미 I/Q 채널을 골라 둠 → 지금도 그 채널이면 그대로 (다시 만들지 않음 → FSW 화면이 왔다 갔다 하지 않음)
            now = self._inst().upper()
            nm = re.findall(r"'([^']*)'", self.via)
            want = (nm[-1] if nm else "IQ").upper()
            if now == want or (self.via.startswith("TRAC:IQ") and not now.startswith("?")):
                return
            self.log("    FSW 채널이 바뀌어 있음 (%s) → I/Q 채널 (%s) 로 다시" % (now, want))
            io.write("INST:SEL '%s'" % (nm[-1] if nm else "IQ"))
            io.opc()
            t_end = time.time() + 8.0
            while time.time() < t_end:
                if self._inst().upper() == want:
                    self.errors()
                    return
                self.io._alive()
                time.sleep(0.4)
        if self._iq_works(fs):
            return
        if self.prev is None:
            p = self._inst()
            self.prev = None if p.startswith("?") else p
        names, lst = self._iq_list()
        uniq = "DPD IQ %d" % (int(time.time()) % 1000)
        # 처음 버전에서 되던 방법 (I/Q 채널 새로 만들기 → 그 채널이 바로 선택됨) 을 먼저. 끝나면 그 채널을 지움 → 원래 채널로
        tries = [("I/Q 채널 새로 만들기", ["INST:DEL '%s'" % self.CH, "INST:CRE IQ, '%s'" % self.CH], ["INST:DEL '%s'" % self.CH]),
                 ("I/Q 채널 새로 만들기 (새 이름)", ["INST:CRE IQ, '%s'" % uniq], ["INST:DEL '%s'" % uniq]),
                 ("INST:SEL IQ", ["INST:SEL IQ"], None),
                 ("TRAC:IQ ON", ["TRAC:IQ:STAT ON"], ["TRAC:IQ:STAT OFF"])]
        why = []
        for name, cmds, back in tries:
            e = []
            for k, cmd in enumerate(cmds):
                io.write(cmd)
                try:
                    io.opc()
                except TimeoutError:
                    pass
                ek = self.errors()
                if not (k == 0 and len(cmds) > 1):       # 지울 채널이 없어서 나는 오류는 상관없음
                    e += ek
            ok = False
            t_end = time.time() + (12.0 if not e else 1.5)       # 화면 (앱) 전환이 끝날 때까지 기다림
            while time.time() < t_end:
                self.io._alive()
                if self._iq_works(fs):
                    ok = True
                    break
                time.sleep(0.7)
            if ok:
                self.via = cmds[-1]
                self.back = back
                self.log("    FSW I/Q 받기 준비 (%s · 지금 INST? = %s)" % (name, self._inst()))
                return
            why.append("%s: %s" % (name, " / ".join(e) or "I/Q 명령 안 먹음"))
        raise RuntimeError("FSW 를 I/Q 받기 상태로 바꾸지 못했습니다 (%s) [IDN = %s · INST? = %s · INST:LIST? = %s]"
                           " - FSW 에서 MODE → I/Q Analyzer 를 직접 열어 두면 그대로 씁니다."
                           % (" · ".join(why), self.idn, self._inst(), lst or "응답 없음"))

    # ------------------------------------------------------------ Ref Level 자동 · Mod. 화면 (스펙트럼) ACLR 읽기
    def auto_level(self):
        """FSW 자동 레벨 (신호를 보고 Ref Level · 감쇠를 맞춤) → (Ref dBm, 감쇠 dB). 안 되면 None"""
        io = self.io
        self.errors()
        old = io.timeout if hasattr(io, "timeout") else 30.0
        try:
            io.set_timeout(25.0)
            io.write("SENS:ADJ:LEV")
            io.opc()
        except TimeoutError:
            return None
        finally:
            try:
                io.set_timeout(old)
            except Exception:
                pass
        if self.errors():
            return None
        if getattr(self, "att_fixed", None) is not None:   # 감쇠를 숫자로 정했으면 자동 레벨 뒤에도 그 값으로
            io.write("INP:ATT %.0f" % self.att_fixed)
            io.opc()
            self.errors()
        try:
            ref = float(io.query("DISP:TRAC:Y:SCAL:RLEV?"))
        except Exception:
            ref = float("nan")
        try:
            att = float(io.query("INP:ATT?"))
        except Exception:
            att = float("nan")
        return ref, att

    def set_ref(self, ref_dbm):
        self.io.write("DISP:TRAC:Y:SCAL:RLEV %.2f" % ref_dbm)
        self.io.opc()

    def get_att(self):
        """지금 채널의 입력 감쇠 (dB) · auto 인지 → (dB, auto?) / 못 읽으면 (None, None)"""
        try:
            att = float(self.io.query("INP:ATT?"))
            try:
                au = self.io.query("INP:ATT:AUTO?").strip() in ("1", "ON")
            except Exception:
                au = None
            return att, au
        except Exception:
            return None, None

    def overload(self, clear_only=False):
        """FSW 과입력 표시 (STAT:QUES:POW: bit0 = RF OVLD, bit2 = IF OVLD) → 'IF OVLD' · 'RF OVLD' 글 (없으면 '')
        clear_only = 받기 전에 이전 기록 (EVENT) 을 지움"""
        io = self.io
        try:
            ev = int(float(io.query("STAT:QUES:POW?")))
            if clear_only:
                return ""
            try:
                cond = int(float(io.query("STAT:QUES:POW:COND?")))
            except Exception:
                cond = 0
        except Exception:
            self.errors()
            return ""
        v = ev | cond
        out = []
        if v & 1:
            out.append("RF OVLD")
        if v & 4:
            out.append("IF OVLD")
        return " · ".join(out)

    def spec_channel(self):
        """ACLR 을 읽을 스펙트럼 채널 (DPD 전 화면 = Bench Mod. 측정 화면). 없으면 INST:LIST? 의 첫 Spectrum 채널"""
        if self.prev and "IQ" not in self.prev.upper():
            return self.prev
        try:
            r = self.io.query("INST:LIST?")
            it = [t.strip().strip("'\"") for t in r.split(",")]
            for i in range(0, len(it) - 1, 2):
                if it[i].upper().startswith("SAN"):
                    return it[i + 1]
        except Exception:
            pass
        return None

    ACP_Q = (("txn", "SENS:POW:ACH:TXCH:COUN"), ("bw1", "SENS:POW:ACH:BWID:CHAN1"), ("bw2", "SENS:POW:ACH:BWID:CHAN2"),
             ("sp1", "SENS:POW:ACH:SPAC:CHAN1"), ("nadj", "SENS:POW:ACH:ACP"), ("abw", "SENS:POW:ACH:BWID:ACH"),
             ("asp", "SENS:POW:ACH:SPAC:ACH"), ("span", "FREQ:SPAN"))

    @staticmethod
    def _keep_path():
        import os
        d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "PADPDTester")
        return os.path.join(d, "fsw_acp_keep.json")

    def _save_keep(self, keep):
        import json, os
        try:
            os.makedirs(os.path.dirname(self._keep_path()), exist_ok=True)
            with open(self._keep_path(), "w", encoding="utf-8") as f:
                json.dump(keep, f)
        except Exception:
            pass

    def _load_keep(self):
        import json
        try:
            with open(self._keep_path(), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _drop_keep(self):
        import os
        try:
            os.remove(self._keep_path())
        except Exception:
            pass

    def _orig_cent(self, ch):
        """DPD 전 Mod. 화면 중심: 지난 DPD 가 화면을 그대로 두고 끝났으면 (파일) 그 원래 값, 아니면 지금 값"""
        k = self._load_keep() or {}
        if k.get("_cent"):
            return (k.get("_ch") or ch, float(k["_cent"]))
        return (ch, float(self.io.query("FREQ:CENT?")))

    def _acp_now(self):
        io = self.io
        try:
            return int(float(io.query("SENS:POW:ACH:TXCH:COUN?"))), float(io.query("FREQ:SPAN?"))
        except Exception:
            return None, None

    def apply_acp(self):
        """반송파 여러 개 신호: DPD 동안 Mod. 화면 ACLR 을 신호에 맞춤 (Tx 수 · 폭 · 간격 · 인접 · Span)
        처음 바꿀 때 원래 값을 기억 (파일에도 → 중간에 꺼져도 다음 실행에서 되돌림) → restore() 에서 되돌림.
        I/Q 를 받고 오면 FSW 가 ACLR 설정을 되돌릴 수 있어 매번 Tx 수 · Span 을 확인하고 다르면 다시 넣음.
        반송파 1 개 신호 (cfg 없음) 인데 예전에 바꾼 설정이 남아 있으면 되돌림"""
        cfg = getattr(self, "acp_cfg", None)
        io = self.io
        if not cfg:
            if getattr(self, "acp_keep", None) or self._load_keep():
                self.restore_acp()
            n_now, _sp = self._acp_now()
            if n_now is not None and n_now > 1:          # 반송파 1 개 신호인데 Tx 가 여러 개로 남아 있음 → ACLR 값이 Tx 전력으로 읽힘
                io.write("SENS:POW:ACH:TXCH:COUN 1")
                io.opc()
                self.errors()
                self.log("    FSW Mod. 화면 Tx 채널이 %d 개로 남아 있어 1 개로 바꿈 (반송파 1 개 신호)" % n_now)
            return
        if getattr(self, "acp_keep", None) is None:
            keep = self._load_keep()                    # 지난 실행이 못 되돌린 원래 값이 있으면 그것을 원래 값으로
            if keep:
                if keep.get("_cent") and getattr(self, "mod_f_old", None) is None:
                    self.mod_f_old = (keep.get("_ch"), keep["_cent"])   # 지난 DPD 가 남긴 원래 중심
                keep = {k: v for k, v in keep.items() if not str(k).startswith("_")}
            if not keep:
                keep = {}
                for k, q in self.ACP_Q:
                    try:
                        keep[k] = float(io.query(q + "?"))
                    except Exception:
                        pass
                self.errors()
                if keep.get("span", 0) < 1e6:           # I/Q 상태에서 읽어 0 이 나오면 Span 은 기억 안 함 (0 으로 되돌리면 일자 선)
                    keep.pop("span", None)
                keep = {k: v for k, v in keep.items() if v > 0}   # 못 읽은 값 (0) 은 되돌리지 않음
                if int(cfg["n"]) == 1 and int(keep.get("txn", 1)) > 1:
                    keep = {"txn": 1}                   # 반송파 1 개인데 지난 DPD 의 Tx 여러 개가 남아 있음 → 원래는 Tx 1 개로 봄
                if int(cfg["n"]) > 1 and int(keep.get("txn", 1)) == int(cfg["n"]) and abs(keep.get("sp1", 0) - cfg["spac"]) < 1e6:
                    keep = {"txn": 1}                   # 이미 우리 설정 (지난번에 못 되돌림) → 원래 값 모름: Tx 1 개로만 되돌림
                self._save_keep(keep)
            self.acp_keep = keep
        n_now, span_now = self._acp_now()
        if n_now == int(cfg["n"]) and span_now is not None and abs(span_now - cfg["span"]) < 1e6 \
                and getattr(self, "_acp_set", None) == cfg:
            return                                      # 이미 맞음
        n = int(cfg["n"])
        cmds = ["SENS:POW:ACH:TXCH:COUN %d" % n]
        for i in range(1, n + 1):
            cmds.append("SENS:POW:ACH:BWID:CHAN%d %.0f" % (i, cfg["bw"]))
        for i in range(1, n):
            cmds.append("SENS:POW:ACH:SPAC:CHAN%d %.0f" % (i, cfg["spac"]))
        if n == 1:
            cmds = ["SENS:POW:ACH:TXCH:COUN 1", "SENS:POW:ACH:BWID:CHAN1 %.0f" % cfg["bw"]]
        cmds += ["SENS:POW:ACH:ACP 1", "SENS:POW:ACH:BWID:ACH %.0f" % cfg["abw"],
                 "SENS:POW:ACH:SPAC:ACH %.0f" % cfg["asp"], "FREQ:SPAN %.0f" % cfg["span"]]
        for c in cmds:
            io.write(c)
        io.opc()
        e = self.errors()
        n2, sp2 = self._acp_now()
        first = getattr(self, "_acp_set", None) != cfg
        self._acp_set = dict(cfg)
        if first or e or n2 != n or (sp2 is not None and abs(sp2 - cfg["span"]) > 1e6):
            self.log("    FSW Mod. 화면 ACLR 을 신호에 맞춤: Tx %d 개 × %.2f MHz (간격 %.0f) · 인접 %.2f MHz (간격 %.0f) · Span %.0f MHz"
                     " → FSW 읽은 값: Tx %s 개 · Span %s MHz%s (DPD 끝나면 원래대로)"
                     % (n, cfg["bw"] / 1e6, cfg["spac"] / 1e6, cfg["abw"] / 1e6, cfg["asp"] / 1e6, cfg["span"] / 1e6,
                        n2, ("%.0f" % (sp2 / 1e6)) if sp2 is not None else "?", ("  ⚠ " + " / ".join(e)) if e else ""))

    def restore_acp(self):
        keep, self.acp_keep = getattr(self, "acp_keep", None), None
        self._acp_set = None
        filek = self._load_keep() or {}
        priv = {k: v for k, v in filek.items() if str(k).startswith("_")}   # 원래 중심 (_cent) 은 restore_view 까지 남김
        if not keep:
            keep = filek
        if keep:
            keep = {k: v for k, v in keep.items() if not str(k).startswith("_")}

        def _done():
            if priv:
                self._save_keep(priv)
            else:
                self._drop_keep()
        if not keep:
            _done()
            return
        io = self.io
        try:
            n = int(keep.get("txn", 1))
            cmds = ["SENS:POW:ACH:TXCH:COUN %d" % n]
            if "bw1" in keep:
                cmds.append("SENS:POW:ACH:BWID:CHAN1 %.0f" % keep["bw1"])
            if n > 1 and "bw2" in keep:
                cmds.append("SENS:POW:ACH:BWID:CHAN2 %.0f" % keep["bw2"])
            if n > 1 and "sp1" in keep:
                cmds.append("SENS:POW:ACH:SPAC:CHAN1 %.0f" % keep["sp1"])
            for k, q in (("nadj", "SENS:POW:ACH:ACP %d"), ("abw", "SENS:POW:ACH:BWID:ACH %.0f"),
                         ("asp", "SENS:POW:ACH:SPAC:ACH %.0f"), ("span", "FREQ:SPAN %.0f")):
                if k in keep and not (k == "span" and keep[k] < 1e6):
                    cmds.append(q % (int(keep[k]) if k == "nadj" else keep[k]))
            for c in cmds:
                io.write(c)
            io.opc()
            self.errors()
            _done()
            self.log("FSW Mod. 화면 ACLR 설정 원래대로 (Tx %d 개%s)" % (
                n, (" · Span %.0f MHz" % (keep["span"] / 1e6)) if keep.get("span", 0) >= 1e6 else ""))
        except Exception as ex:
            self.log("⚠ FSW Mod. 화면 ACLR 설정을 못 되돌림: %s" % ex)

    def park_spec(self, freq=None):
        """I/Q 를 받은 뒤 FSW 화면을 Mod. 화면 (Spectrum · ACLR) 으로 돌려 둠 → 장비 화면에 늘 ACLR 스펙트럼 (연속 스윕)"""
        if not getattr(self, "park", False) or getattr(self, "_parked", False) or not getattr(self, "via", None):
            return
        io = self.io
        try:
            if self.via.startswith("TRAC:IQ"):
                io.write("TRAC:IQ:STAT OFF")
            else:
                ch = self.spec_channel()
                if not ch:
                    return
                io.write("INST:SEL '%s'" % ch)
                io.opc()
                if freq:
                    if getattr(self, "mod_f_old", None) is None:
                        try:
                            self.mod_f_old = self._orig_cent(ch)
                        except Exception:
                            self.mod_f_old = None
                    io.write("FREQ:CENT %.0f" % freq)
                self.apply_acp()
                if freq:
                    io.write("FREQ:CENT %.0f" % freq)
            io.write("INIT:CONT ON")                       # 계속 스윕 → 지금 나가는 신호의 ACLR 이 화면에 보임
            io.opc()
            self.errors()
            self._parked = True
        except Exception:
            pass

    def show_like(self, freq=None, att=None, rbw=None, ref=None, span=None):
        """신호 내보내기 뒤: FSW 화면을 Mod. 화면 (스펙트럼 ACLR) 으로 · 그 측정 때의 감쇠 · RBW · Ref · Span 으로 맞춰 계속 스윕 (원래대로 안 돌림)"""
        io = self.io
        ch = self.spec_channel()
        if ch:
            io.write("INST:SEL '%s'" % ch)
            io.opc()
        elif getattr(self, "via", None) == "TRAC:IQ:STAT ON":
            io.write("TRAC:IQ:STAT OFF")
            io.opc()
        if freq:
            io.write("FREQ:CENT %.0f" % freq)
        try:
            io.write("CALC1:MARK:FUNC:POW:SEL ACP")
            self.apply_acp()
        except Exception:
            pass
        if freq:
            io.write("FREQ:CENT %.0f" % freq)
        if rbw:
            io.write("BAND:RES %.0f" % rbw)
        if span:
            io.write("FREQ:SPAN %.0f" % span)
        if att is not None:
            io.write("INP:ATT %.0f" % att)
        if ref is not None and ref == ref:
            io.write("DISP:TRAC:Y:SCAL:RLEV %.2f" % ref)
        io.write("INIT:CONT ON")
        io.opc()
        self.errors()
        return ch

    def mod_aclr(self, freq=None, rbw=None, att=None):
        """지금 SMW 가 내보내는 신호를 Mod. 화면 (사용자가 맞춘 스펙트럼 ACLR 측정, 설정 그대로) 에서 한 번 재서 읽음
        → dict(tx, aclr_l, aclr_u, f (MHz, 중심 기준), db (최고 0 dB), ch) / 안 되면 예외. 끝나면 I/Q 채널로 돌아옴"""
        io = self.io
        via = getattr(self, "via", None)
        ch = None
        self.errors()
        if getattr(self, "_parked", False):
            ch = self.spec_channel() if via != "TRAC:IQ:STAT ON" else None
        elif via == "TRAC:IQ:STAT ON":                        # Spectrum 안에서 I/Q 를 켠 방식: 끄면 바로 스펙트럼
            io.write("TRAC:IQ:STAT OFF")
            io.opc()
        else:
            ch = self.spec_channel()
            if not ch:
                raise RuntimeError("FSW 에 Spectrum (Mod. 화면) 채널이 없음 - Bench Mod. 측정 화면을 FSW 에 열어 두세요")
            io.write("INST:SEL '%s'" % ch)
            io.opc()
            t_end = time.time() + 10.0
            while self._inst().upper() != ch.upper() and time.time() < t_end:
                self.io._alive()
                time.sleep(0.3)
        f_old = None
        keep = {}                                           # 바꾼 RBW · 감쇠 → 끝나면 원래대로 (Bench Mod. 측정 화면 그대로 두기)
        try:
            try:
                cont = io.query("INIT:CONT?").strip() in ("1", "ON")
            except Exception:
                cont = True
            io.write("INIT:CONT OFF")
            if freq:
                # FSW 는 채널마다 중심 주파수가 따로 → Mod. 화면 채널도 DPD 주파수로
                # (MultiView 에 보이는 Mod. 화면이 DPD 주파수 그대로 보이게, DPD 가 다 끝나면 원래 주파수로)
                if getattr(self, "mod_f_old", None) is None:
                    try:
                        self.mod_f_old = self._orig_cent(ch)
                    except Exception:
                        self.mod_f_old = None
                io.write("FREQ:CENT %.0f" % freq)
            io.write("CALC1:MARK:FUNC:POW:SEL ACP")
            self.apply_acp()
            try:
                sp_ = float(io.query("FREQ:SPAN?"))
                if sp_ < 1e6:                                    # Zero span (일자 선 · ACLR ≈ 0) → 스펙트럼이 되게
                    new_ = float(getattr(self, "span_fix", 520e6))
                    io.write("FREQ:SPAN %.0f" % new_)
                    self.log("    ⚠ FSW Mod. 화면 Span 이 0 (zero span) 이라 %.0f MHz 로 바꿈 (Bench 화면 Span 도 확인)" % (new_ / 1e6))
            except Exception:
                pass
            if freq:
                io.write("FREQ:CENT %.0f" % freq)               # Tx 채널 수 · 간격을 바꾸면 FSW 가 중심을 옮길 수 있음 → 다시 DPD 주파수로
            if rbw:
                try:
                    keep["rbw_auto"] = io.query("BAND:AUTO?").strip() in ("1", "ON")
                    keep["rbw"] = float(io.query("BAND:RES?"))
                except Exception:
                    pass
                io.write("BAND:RES %.0f" % rbw)
            if att is not None:
                try:
                    keep["att_auto"] = io.query("INP:ATT:AUTO?").strip() in ("1", "ON")
                    keep["att"] = float(io.query("INP:ATT?"))
                except Exception:
                    pass
                io.write("INP:ATT %.0f" % att)
            if (rbw or att is not None) and not getattr(self, "_set_logged", False):
                self._set_logged = True
                self.log("    FSW Mod. 화면 측정에만: %s%s (끝나면 원래대로)" % (("RBW %.0f kHz " % (rbw / 1e3)) if rbw else "",
                                                                   ("감쇠 %.0f dB" % att) if att is not None else ""))
            if not getattr(self, "_acp_logged", False):
                self._acp_logged = True
                info = []
                for nm, q in (("채널 폭", "SENS:POW:ACH:BWID:CHAN1?"), ("인접 폭", "SENS:POW:ACH:BWID:ACH?"),
                              ("간격", "SENS:POW:ACH:SPAC:ACH?"), ("RBW", "BAND:RES?"), ("Span", "FREQ:SPAN?")):
                    try:
                        info.append("%s %.2f MHz" % (nm, float(io.query(q)) / 1e6) if nm != "RBW" else "%s %.0f kHz" % (nm, float(io.query(q)) / 1e3))
                    except Exception:
                        pass
                self.errors()
                self.log("    FSW Mod. 화면 (%s) ACLR 설정: %s" % (ch or "Spectrum", " · ".join(info) or "읽기 안 됨"))
            io.set_timeout(60.0)
            io.write("INIT:IMM")
            io.opc()                                          # 사용자 화면의 스윕 · 평균 그대로 한 번
            io.set_timeout(30.0)
            v = [float(t) for t in io.query("CALC1:MARK:FUNC:POW:RES? ACP").split(",") if t.strip()]
            if len(v) < 3:
                raise RuntimeError("ACP 결과가 이상함: %s - FSW Mod. 화면이 ACLR (ACP) 측정인지 확인" % v)
            # Tx 채널 수 (FSW 결과 순서: Tx1 … TxN, 인접 아래 · 위 …)
            try:
                n_tx = int(float(io.query("SENS:POW:ACH:TXCH:COUN?")))
            except Exception:
                n_tx = 1
            k_ = n_tx if (n_tx > 1 and len(v) >= n_tx + 2) else 1
            if k_ > 1 and v[k_] > -3.0 and len(v) >= k_ + 3:  # Tx 합 칸이 끼어 있으면 (값이 dBc 가 아니라 dBm) 한 칸 뒤
                k_ += 1
            out = dict(tx=v[0], aclr_l=v[k_], aclr_u=v[k_ + 1], ch=ch or "Spectrum", raw=["%.2f" % t for t in v[:12]])
            out["n_tx"] = n_tx
            txs = [v[0]]
            if n_tx > 1:
                try:
                    vm = [float(t) for t in io.query("CALC1:MARK:FUNC:POW:RES? MCAC").split(",") if t.strip()]
                    if len(vm) >= n_tx:
                        txs = vm[:n_tx]
                        out["raw_mcac"] = ["%.2f" % t for t in vm[:12]]
                except Exception:
                    pass
            try:
                out["att"] = float(io.query("INP:ATT?"))
            except Exception:
                pass
            for k_, q_ in (("ref", "DISP:TRAC:Y:SCAL:RLEV?"), ("span", "FREQ:SPAN?"), ("rbw_now", "BAND:RES?")):
                try:                                          # 그 측정 때 Mod. 화면 조건 (내보내기 때 그대로 되돌리려고)
                    out[k_] = float(io.query(q_))
                except Exception:
                    pass
            out["tx_list"] = txs
            out["tx_sum"] = float(10 * np.log10(np.sum(10 ** (np.asarray(txs) / 10.0))))
            try:                                              # 그래프용 트레이스
                f0, f1 = float(io.query("FREQ:STAR?")), float(io.query("FREQ:STOP?"))
                io.write("FORM REAL,32")
                tr = np.frombuffer(io.query_block("TRAC:DATA? TRACE1"), dtype="<f4").astype(float)
                io.write("FORM ASC")
                if len(tr) > 10:
                    fc = 0.5 * (f0 + f1)
                    if freq:
                        if abs(fc - freq) > 1e6 and not getattr(self, "_cent_warn", False):
                            self._cent_warn = True
                            self.log("    ⚠ FSW Mod. 화면 중심 %.4f GHz ≠ DPD 주파수 %.4f GHz → 그래프 · ACLR 은 DPD 주파수 기준으로 맞춰 계산"
                                     % (fc / 1e9, freq / 1e9))
                        fc = freq                                # 주파수 축은 늘 DPD 주파수 (신호 가운데) 기준
                    out["f"] = ((np.linspace(f0, f1, len(tr)) - fc) / 1e6).tolist()
                    out["db"] = (tr - np.max(tr)).tolist()
                    out["db_abs"] = tr.tolist()                # 절대값 (dBm / RBW) → 채널 전력 합 (Pout 비교 · 맞춤)
                    try:
                        out["rbw"] = float(io.query("BAND:RES?"))
                    except Exception:
                        pass
            except Exception:
                try:
                    io.write("FORM ASC")
                except Exception:
                    pass
            e = self.errors()
            if e:
                self.log("    (Mod. 화면 ACLR 읽기 중 FSW 메시지: %s)" % " / ".join(e))
            if "rbw" in keep:
                io.write("BAND:RES %.0f" % keep["rbw"])
                if keep.get("rbw_auto"):
                    io.write("BAND:AUTO ON")
            if "att" in keep:
                io.write("INP:ATT %.0f" % keep["att"])
                if keep.get("att_auto"):
                    io.write("INP:ATT:AUTO ON")
            if cont:
                io.write("INIT:CONT ON")
            return out
        finally:
            if getattr(self, "park", False):
                self._parked = True                         # Mod. 화면에 그대로 둠 (다음 I/Q 받기 때 돌아감)
                try:
                    io.write("INIT:CONT ON")
                except Exception:
                    pass
            elif via == "TRAC:IQ:STAT ON":
                io.write("TRAC:IQ:STAT ON")
                io.opc()
            else:
                nm = re.findall(r"'([^']*)'", via or "")
                io.write("INST:SEL '%s'" % (nm[-1] if (via and "CRE" in via and nm) else "IQ"))
                io.opc()
                t_end = time.time() + 10.0
                while time.time() < t_end and not self._iq_works(self.fs):
                    self.io._alive()
                    time.sleep(0.4)

    def tidy(self):
        """FSW 화면 정리 (보기용, 측정값과는 무관): 스펙트럼 창 하나만 · 마커 끄기 · 확대 끄기. 안 되는 명령은 넘어감"""
        io = self.io
        done = []
        try:                                               # 1 번 창을 스펙트럼으로 바꾸고 나머지 창 (Magnitude 등) 은 닫음
            io.write("LAY:REPL:WIND '1',FREQ")
            io.opc()
            if not self.errors():
                done.append("스펙트럼 보기")
            r = io.query("LAY:CAT?")
            it = [t.strip().strip("'\"") for t in r.split(",")]
            names = [it[i] for i in range(0, len(it) - 1, 2)]
            for nm in names:
                if nm != "1":
                    io.write("LAY:REM '%s'" % nm)
                    io.opc()
            self.errors()
            if len(names) > 1:
                done.append("다른 창 %d 개 닫음" % (len(names) - 1))
        except Exception:
            pass
        try:                                               # MultiView: Mod. 화면 (Spectrum) 과 I/Q 를 나란히 (끝나면 원래대로)
            if getattr(self, "form_prev", None) is None:
                self.form_prev = io.query("DISP:FORM?").strip().upper()
        except Exception:
            self.form_prev = None
        self.errors()
        form = ("한 화면 (Mod. 화면 위주)", "DISP:FORM SING") if getattr(self, "park", False) else ("두 화면 같이 (MultiView)", "DISP:FORM SPL")
        for name, cmd in (form,
                          ("마커 끄기", "CALC1:MARK:AOFF"),
                          ("확대 끄기", "DISP:WIND1:ZOOM:STAT OFF"),
                          ("세로축 자동", "DISP:WIND1:TRAC:Y:SCAL:AUTO ONCE"),
                          ("화면 켜기", "SYST:DISP:UPD ON")):
            try:
                io.write(cmd)
                io.opc()
                if not self.errors():
                    done.append(name)
            except Exception:
                pass
        self.tidied = True
        if done:
            self.log("    FSW 화면 정리: %s" % " · ".join(done))

    def keep_view(self, mf):
        """DPD 끝: Mod. 화면은 DPD 주파수 · 신호 설정 그대로 두고 (신호가 가운데 보이게), 원래 중심 · ACLR 설정은 파일에 기억
        → restore_view() (Pulse · Mod. 탭으로 가거나 Bench 측정 시작) 에서 되돌림"""
        keep = getattr(self, "acp_keep", None) or self._load_keep() or {}
        if mf:
            keep["_ch"], keep["_cent"] = mf[0], mf[1]
        if keep:
            self._save_keep(keep)
            self.acp_keep = None                         # 파일로 넘김 (다른 연결에서도 되돌릴 수 있게)
            self._acp_set = None
            self.log("FSW Mod. 화면은 DPD 주파수 · 신호에 맞춘 그대로 둠 (Pulse · Mod. 탭으로 가거나 Bench 측정을 시작하면 원래대로)")

    def restore_view(self):
        """keep_view 로 남겨 둔 Mod. 화면 (중심 주파수 · ACLR 설정) 을 DPD 전 값으로"""
        keep = getattr(self, "acp_keep", None) or self._load_keep()
        mf, self.mod_f_old = getattr(self, "mod_f_old", None), None
        ch, cent = (mf if mf else (None, None))
        if keep:
            ch = ch or keep.get("_ch")
            cent = cent if cent else keep.get("_cent")
        if not keep and not cent:
            return False
        io = self.io
        try:
            if ch:
                io.write("INST:SEL '%s'" % ch)
                io.opc()
            if cent:
                io.write("FREQ:CENT %.0f" % float(cent))
            if keep:
                self.acp_keep = {k: v for k, v in keep.items() if not k.startswith("_")}
                self.restore_acp()
            self._drop_keep()                            # 원래 중심까지 다 되돌림 → 파일 지움
            io.write("INIT:CONT ON")
            self.errors()
            self.log("FSW Mod. 화면을 DPD 전 값으로 (중심 %s)" % (("%.4f GHz" % (float(cent) / 1e9)) if cent else "그대로"))
            return True
        except Exception as ex:
            self.log("⚠ FSW Mod. 화면을 원래대로 못 돌림: %s" % ex)
            return False

    def restore(self, keep_view=False):
        """DPD 측정 전 모드 (예: Spectrum) 로 되돌림 → PA Bench Monitor 측정이 그대로 되게
        keep_view=True: 모드만 되돌리고 Mod. 화면 중심 · ACLR 설정은 DPD 그대로 (신호가 가운데 보이게) - 나중에 restore_view"""
        prev, self.prev = self.prev, None
        via, self.via = getattr(self, "via", None), None
        self.tidied = False
        self._parked = False
        if not prev:
            if keep_view:
                self.keep_view(getattr(self, "mod_f_old", None))
                self.mod_f_old = None
            elif getattr(self, "acp_keep", None):
                self.restore_acp()
            return
        io = self.io
        try:
            self.errors()
            back, self.back = getattr(self, "back", None), None
            for cmd in (back or ["INST:SEL %s" % prev]):
                io.write(cmd)
                io.opc()
            io.opc()
            e = self.errors()
            t_end = time.time() + 10.0                     # 화면 전환이 끝날 때까지
            now = self._inst()
            while now.upper() != prev.upper() and time.time() < t_end:
                self.io._alive()
                time.sleep(0.5)
                now = self._inst()
            mf, self.mod_f_old = getattr(self, "mod_f_old", None), None
            if keep_view and now.upper() == prev.upper():
                self.keep_view(mf)                     # 신호가 가운데 보이게 그대로 · 원래 값은 나중에
            else:
                if mf and now.upper() == prev.upper() and (mf[0] or "").upper() in (prev.upper(), "SPECTRUM"):
                    io.write("FREQ:CENT %.0f" % mf[1])     # Mod. 화면 주파수를 DPD 전 값으로
                if now.upper() == prev.upper():
                    self.restore_acp()                     # Mod. 화면 ACLR 설정 (반송파 여러 개로 바꿨으면) 원래대로
            fp, self.form_prev = getattr(self, "form_prev", None), None
            if fp and fp.startswith("SING"):
                io.write("DISP:FORM SING")               # 원래 한 화면이었으면 한 화면으로
            if now.upper() == prev.upper():
                io.write("INIT:CONT ON")               # 원래 화면처럼 연속 측정
                self.log("FSW 화면 원래 모드로 되돌림 (%s)" % prev)
            else:
                self.log("⚠ FSW 원래 모드 (%s) 로 못 되돌림%s - FSW 에서 MODE 로 직접 바꿔 주세요"
                         % (prev, (": " + " / ".join(e)) if e else ""))
        except Exception as ex:
            self.log("⚠ FSW 원래 모드 (%s) 로 못 되돌림: %s" % (prev, ex))

    def reopen(self):
        self.io = ScpiSocket(self.host, self.port, timeout=30.0, name="FSW")

    def _rlen(self):
        try:
            return int(float(self.io.query("TRAC:IQ:RLEN?")))
        except (ValueError, TimeoutError):
            return 0

    def _set_len(self, want):
        """캡처 길이 정하기: 펌웨어 · 옵션마다 받는 명령이 달라서 차례로 시도 → (실제 길이, 오류 설명)"""
        io, fs = self.io, self.fs
        tries = [("RLEN", lambda: io.write("TRAC:IQ:RLEN %d" % want))]
        tries.append(("I/Q 다시 선택 + RLEN", lambda: (self.select_iq(fs), io.write("TRAC:IQ:RLEN %d" % want))))
        if fs:
            tries.append(("TRAC:IQ:SET", lambda: io.write("TRAC:IQ:SET NORM,0,%.0f,IMM,POS,0,%d" % (fs, want))))
            tries.append(("측정 시간", lambda: io.write("SENS:SWE:TIME %.9f" % (want / fs))))
        why = []
        for name, f in tries:
            f()
            errs = self.errors()
            got = self._rlen()
            if got >= want and not errs:
                if name != "RLEN":
                    self.log("    FSW 캡처 길이: %s 방식으로 설정됨 (%d 샘플)" % (name, got))
                return got, ""
            why.append("%s: %s" % (name, " / ".join(errs) or "%d 샘플로 바뀜" % got))
            if got >= want and name == "측정 시간":
                return got, ""
        return 0, " · ".join(why)

    def capture(self, n, n_min=None):
        """I/Q n 샘플 받기. FSW 가 n 을 못 받으면 n_min (한 주기 + 여유) 까지 줄여서 받음"""
        io = self.io
        old = self.errors()
        if old:
            self.log("    (FSW 에 남아 있던 오류: %s)" % " / ".join(old))
        tried = [n] + ([n_min] if n_min and n_min < n else [])
        got, why = 0, ""
        for want in tried:
            got, why = self._set_len(want)
            if got >= want:
                break
            self.log("    ⚠ FSW 캡처 길이 %d 샘플 안 됨 (%s)%s" % (want, why, " → 줄여서 다시" if want != tried[-1] else ""))
            got = 0
        if got <= 0:
            raise RuntimeError("FSW 캡처 길이 설정 오류 (%d 샘플 · %.2f MSps): %s" % (tried[-1], (self.fs or 0) / 1e6, why))
        n = got
        io.set_timeout(30.0 + n / 491.52e6 * 20)
        try:
            io.write("INIT:IMM")
            io.opc()
            io.write("FORM REAL,32")
            io.write("TRAC:IQ:DATA:FORM IQP")
            e = self.errors()
            if e:
                self.log("    ⚠ FSW 캡처 후 오류: %s" % " / ".join(e))
            io.set_timeout(30.0)
            parts, k, t0 = [], 0, time.time()
            while k < n:                                       # 큰 블록 한 번 대신 나눠서 받음 (FSW 전송 한계 · 느린 LAN)
                m = min(self.CHUNK, n - k)
                try:
                    parts.append(io.query_block("TRAC:IQ:DATA:MEM? %d,%d" % (k, m)))
                except TimeoutError as ex:
                    try:
                        io.close()
                    except Exception:
                        pass
                    self.reopen()                              # 늦게 오는 데이터가 다음 명령 응답과 섞이지 않게 새로 연결
                    io = self.io
                    e = self.errors()
                    if m > 16384:                              # 더 작게 나눠서 한 번 더
                        self.log("    ⚠ FSW I/Q %d 샘플 받기 응답 없음%s → 16384 샘플씩 다시"
                                 % (m, (" (" + " / ".join(e) + ")") if e else ""))
                        self.CHUNK = 16384
                        io.write("FORM REAL,32")
                        io.write("TRAC:IQ:DATA:FORM IQP")
                        continue
                    raise TimeoutError("%s (%d / %d 샘플째, 한 번에 %d 샘플)%s"
                                       % (ex, k, n, m, (" · FSW 오류: " + " / ".join(e)) if e else ""))
                k += m
            raw = b"".join(parts)
            if n > self.CHUNK:
                self.log("    FSW I/Q %d 샘플을 %d 번에 나눠 받음 (%.1f 초)" % (n, len(parts), time.time() - t0))
        finally:
            io = self.io
            io.set_timeout(30.0)
        v = np.frombuffer(raw, "<f4").astype(float)
        return v[0::2] + 1j * v[1::2]

    def close(self):
        self.io.close()


# ---------------------------------------------------------------- 가상 장비 (demo)
class DemoBench:
    """SMW → (입력 손실) → 가상 PA → (출력 손실) → FSW"""

    def __init__(self):
        from demo_pa import DemoPA
        self.pa = DemoPA()
        self.wave = None
        self.level = -30.0
        self.rf_on = False
        self.freq = 3.6e9
        self.in_loss = 0.5
        self.out_loss = 40.0
        self.rng = np.random.default_rng(7)


class DemoSMW:
    def __init__(self, bench):
        self.b = bench
        self.idn = "Rohde&Schwarz,SMW200A,DEMO,5.30"

    def load_wave(self, wv_bytes, freq, level, rf_on=True):
        import tempfile, os
        fd, p = tempfile.mkstemp(suffix=".wv")
        os.write(fd, wv_bytes)
        os.close(fd)
        x, fs = C.read_wv(p)
        os.remove(p)
        self.b.wave = x
        self.b.wave_fs = fs
        self.b.freq = freq
        self.b.level = level
        self.b.rf_on = bool(rf_on)
        time.sleep(0.3)

    def set_level(self, level):
        self.b.level = level

    def rf(self, on):
        self.b.rf_on = bool(on)

    def close(self):
        pass


class DemoFSW:
    def __init__(self, bench):
        self.b = bench
        self.idn = "Rohde&Schwarz,FSW-43,DEMO,5.00"
        self.fs = 491.52e6
        self.last = None

    def auto_level(self):
        return 5.0, 20.0

    def set_ref(self, ref_dbm):
        pass

    def get_att(self):
        return None, None

    def overload(self, clear_only=False):
        return ""

    def park_spec(self, freq=None):
        pass

    def restore_view(self):
        return False

    def show_like(self, freq=None, att=None, rbw=None, ref=None, span=None):
        return "Spectrum (demo)"

    def mod_aclr(self, freq=None, rbw=None, att=None):
        """데모: 마지막 I/Q 를 잡음 없이 다시 계산 (진짜 FSW 는 스펙트럼 ACLR 측정을 읽음)"""
        if self.last is None:
            raise RuntimeError("데모: 측정 없음")
        al, au, _ = C.aclr(self.last, self.fs)
        f, db = C.spectrum_db(self.last[:min(len(self.last), 500000)], self.fs)
        fw, pw = C.welch_psd(self.last[:min(len(self.last), 500000)], self.fs)
        dfw = fw[1] - fw[0]
        dba = 10 * np.log10(np.maximum(pw * dfw / 100.0 * 1e3, 1e-30))            # dBm / 점 (50 Ω, 첨두)
        return dict(tx=float("nan"), aclr_l=float(al) - 3.0, aclr_u=float(au) - 3.0, ch="데모 Spectrum",
                    f=(f / 1e6).tolist(), db=(np.asarray(db) - 3.0 * (np.abs(f) > 49e6)).tolist(),
                    f_abs=(fw / 1e6).tolist(), db_abs=dba.tolist(), rbw=dfw / 1.065,
                    tx_sum=float(10 * np.log10(np.mean(np.abs(self.last) ** 2) / 100.0 * 1e3)), tx_list=[], n_tx=1)

    def setup(self, freq, fs, ref_dbm, att_db=None, rbw=None):
        self.fs = fs
        return 0.8 * fs

    def capture(self, n, n_min=None):
        b = self.b
        if b.wave is None or not b.rf_on:
            return 1e-6 * (b.rng.standard_normal(n) + 1j * b.rng.standard_normal(n))
        x = b.wave / C.rms(b.wave)
        vin = x * np.sqrt(2 * 50 * 10 ** ((b.level - b.in_loss - 30) / 10))     # SMW RMS = 설정 레벨
        k = int(round(self.fs / b.wave_fs)) if (getattr(b, "wave_fs", None) and self.fs) else 1
        if k > 1:                                       # FSW 만 K 배로 받기: 신호 샘플레이트로 만든 뒤 올림 (SMW 출력 대역 = 신호 샘플레이트)
            self.fs, fs_keep = b.wave_fs, self.fs
            try:
                y = self.capture(n // k + 64, None)
            finally:
                self.fs = fs_keep
            m = len(y)
            Y = np.fft.fft(y)
            h = m // 2
            y = np.fft.ifft(np.concatenate([Y[:h], np.zeros(m * (k - 1), complex), Y[h:]])) * k
            y = y + 1e-6 * (b.rng.standard_normal(len(y)) + 1j * b.rng.standard_normal(len(y))) * np.std(y) * 3e3 * 1e-3
            self.last = y[:n]
            return y[:n]
        reps = int(np.ceil((n + len(x)) / len(x))) + 1
        y = b.pa(np.tile(vin, reps))[len(x):]                                   # 앞 한 주기는 버림 (메모리 안정)
        y = C.frac_delay(y, b.rng.uniform(0, 1))                                # 실제처럼 소수 샘플 지연
        s0 = int(b.rng.integers(0, len(x)))
        y = y[s0:s0 + n] * 10 ** (-b.out_loss / 20) * np.exp(1j * b.rng.uniform(0, 2 * np.pi))
        time.sleep(0.2)
        self.last = y
        return y

    def close(self):
        pass


def clean_host(addr, port=5025):
    """주소 정리: 앞뒤 공백 · 'TCPIP0::192.168.0.11::inst0::INSTR' (VISA) · 'http://' · '192.168.0.11:5025' → (호스트, 포트)"""
    a = str(addr or "").strip().strip("'\"")
    if "::" in a:                                       # VISA 주소 → 두 번째 칸이 IP / 이름
        parts = [t for t in a.split("::") if t]
        a = parts[1] if len(parts) > 1 else parts[0]
        if parts and parts[-1].upper() == "SOCKET" and len(parts) >= 3:
            try:
                port = int(parts[2])
            except ValueError:
                pass
    a = re.sub(r"^[a-zA-Z]+://", "", a).split("/")[0].strip()
    if a.count(":") == 1:
        h, pt = a.split(":")
        if pt.strip().isdigit():
            a, port = h.strip(), int(pt)
    return a, int(port or 5025)


def open_pair(smw_host, fsw_host, smw_port=5025, fsw_port=5025):
    """(smw, fsw) 연결. 둘 중 하나라도 demo 면 둘 다 가상"""
    if str(smw_host).strip().lower() == "demo" or str(fsw_host).strip().lower() == "demo":
        b = DemoBench()
        return DemoSMW(b), DemoFSW(b)
    sh, sp = clean_host(smw_host, smw_port)
    fh, fp = clean_host(fsw_host, fsw_port)
    try:
        smw = SMW(sh, sp)
    except OSError as e:
        raise RuntimeError("SMW200A 주소 '%s' (포트 %d) 에 연결 못 함: %s - 장비 탭 SMW 주소 (IP) 확인" % (sh, sp, e))
    try:
        fsw = FSW(fh, fp)
    except OSError as e:
        smw.close() if hasattr(smw, "close") else None
        raise RuntimeError("FSW 주소 '%s' (포트 %d) 에 연결 못 함: %s - 장비 탭 FSW 주소 (IP) 확인" % (fh, fp, e))
    return smw, fsw
