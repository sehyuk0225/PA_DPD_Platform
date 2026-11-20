"""장비 연결이 끊겼을 때 자동으로 다시 연결 (프로그램을 껐다 켜지 않아도 되게)

psu_panel.py (바이어스 자동 순서) 는 고치지 않음: 장비를 여는 곳 (psu_panel.Visa.open) 만 바깥에서 감싸서
열린 장비마다 '대리 장비' 를 돌려줌. 대리 장비는 명령이 실패하면 세어 두었다가
2 번 이어서 실패하면 그 주소를 닫고 다시 열어 (*IDN? 로 확인) 같은 명령을 한 번 더 보냄.
다시 열기만 하므로 출력 · 전압 설정은 그대로 (*RST 등 안 보냄).
"""
import threading
import time

LOG = None          # LOG(msg, tag) - 프로그램이 붙여 줌 (파워서플라이 로그)
FAILS = 2           # 이어서 몇 번 실패하면 다시 연결
GAP_S = 5.0         # 다시 연결은 이 간격보다 자주 안 함


def _log(msg, tag="err"):
    try:
        if LOG:
            LOG(msg, tag)
    except Exception:
        pass


class RInst:
    """pyvisa 장비 대리: query · write · read 가 이어서 실패하면 다시 열고 한 번 더"""

    def __init__(self, inst, reopen, res):
        object.__setattr__(self, "_i", inst)
        object.__setattr__(self, "_reopen", reopen)
        object.__setattr__(self, "_res", res)
        object.__setattr__(self, "_fails", 0)
        object.__setattr__(self, "_last", 0.0)
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_closed", False)
        object.__setattr__(self, "n_reconnect", 0)

    def _reconnect(self):
        old = self._i
        try:
            old.close()
        except Exception:
            pass
        last_e = None
        for k in range(2):
            try:
                new = self._reopen()
                idn = new.query("*IDN?").strip()
                object.__setattr__(self, "_i", new)
                object.__setattr__(self, "n_reconnect", self.n_reconnect + 1)
                _log("↻ %s 연결이 끊겨 자동으로 다시 연결함 (%s) - 출력 · 설정은 그대로" % (self._res, idn[:40]), "ok")
                return True
            except Exception as e:
                last_e = e
                time.sleep(0.5)
        _log("✖ %s 다시 연결 실패: %s (케이블 · 전원 · 주소 확인 후 [연결] 다시)" % (self._res, last_e), "err")
        return False

    def _call(self, name, *a, **k):
        with self._lock:
            try:
                r = getattr(self._i, name)(*a, **k)
                object.__setattr__(self, "_fails", 0)
                return r
            except Exception:
                if self._closed:
                    raise
                object.__setattr__(self, "_fails", self._fails + 1)
                if self._fails < FAILS or time.time() - self._last < GAP_S:
                    raise
                object.__setattr__(self, "_last", time.time())
                object.__setattr__(self, "_fails", 0)
                if not self._reconnect():
                    raise
                return getattr(self._i, name)(*a, **k)

    def query(self, *a, **k):
        return self._call("query", *a, **k)

    def write(self, *a, **k):
        return self._call("write", *a, **k)

    def read(self, *a, **k):
        return self._call("read", *a, **k)

    def query_ascii_values(self, *a, **k):
        return self._call("query_ascii_values", *a, **k)

    def close(self):
        object.__setattr__(self, "_closed", True)
        try:
            self._i.close()
        except Exception:
            pass

    def __getattr__(self, n):
        return getattr(object.__getattribute__(self, "_i"), n)

    def __setattr__(self, n, v):
        setattr(self._i, n, v)


def install():
    """psu_panel.Visa.open 을 감쌈 (한 번만). Site2 는 site2.install() 보다 먼저 불러야 장비 한 대씩 감쌈"""
    import psu_panel as P
    if getattr(P.Visa, "_reconnect", False):
        return
    orig = P.Visa.open

    def open_r(self, res, timeout_ms=3000):
        inst = orig(self, res, timeout_ms)
        r = (res or "").strip().upper()
        if r.startswith("DEMO") or not hasattr(inst, "query") or isinstance(inst, RInst):
            return inst
        return RInst(inst, lambda: orig(self, res, timeout_ms), res.strip())

    P.Visa.open = open_r
    P.Visa._reconnect = True
