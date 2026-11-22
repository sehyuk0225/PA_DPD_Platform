# -*- coding: utf-8 -*-
"""선택 기능: PyTorch 신경망 DPD (torch 가 없으면 이 모델만 못 쓰고 나머지는 그대로)

torch 폴더 만들기 (프로그램을 빌드한 것과 같은 파이썬 버전으로, 한 번만):
    python -m pip install --target D:\\torchlib torch --index-url https://download.pytorch.org/whl/cpu
→ 프로그램 [설정 ▾] - [torch 폴더 지정…] 에서 D:\\torchlib 선택

모델: 위상 정규화 TDNN (지금 샘플 위상 기준으로 돌린 탭 · |x| 들 → 은닉층 2개 → 보정량, 바로 통과 + 보정)
numpy 신경망보다 크게 (은닉 64 × 2층) · 학습을 많이 (미니배치 Adam) 해도 빠름
"""
import os
import sys
import time

import numpy as np

if os.environ.get("_PADPD_NEVER_SET_"):
    # 실행되지 않음: exe 빌드 (PyInstaller) 때 torch · sympy · networkx 가 쓰는 표준 모듈을 exe 안에 같이 넣으려고만 적어 둠
    import unittest, unittest.mock, pdb, cProfile, pstats, timeit, difflib, modulefinder, pickletools, tracemalloc  # noqa
    import doctest, mmap, filecmp, sqlite3, code, codeop, runpy, uuid, statistics, fractions, decimal, cmath, csv  # noqa
    import plistlib, webbrowser, multiprocessing, multiprocessing.pool, concurrent.futures, xml.dom.minidom  # noqa
    import xml.etree.ElementTree, email.mime.text, http.client, urllib.request, tarfile, lzma, bz2, gzip, zipfile  # noqa
    import tempfile, sysconfig, dataclasses, typing, inspect, dis, ast, tokenize, textwrap, ctypes.util  # noqa
    import logging.handlers, contextvars, asyncio, selectors, socket, ssl, hashlib, hmac, secrets, shelve  # noqa
    import heapq, bisect, array, struct, queue, graphlib, zoneinfo, importlib.metadata, importlib.resources  # noqa
    import importlib.util, importlib.machinery, pkgutil, site, traceback, linecache, getpass, shlex, platform  # noqa
    import locale, gettext, argparse, optparse, signal, subprocess, numbers, weakref, copyreg, operator, keyword  # noqa
    import colorsys, opcode, symtable, glob, fnmatch, shutil, pathlib, pprint, reprlib, atexit, base64, quopri  # noqa
    import pickle, copy, functools, itertools, collections.abc, contextlib, enum, string, random, datetime  # noqa

_torch = None
_err = ""
_path = None


DEFAULT_DIRS = (r"D:\torchlib",)      # 안내한 설치 경로 (pip install --target D:\torchlib …) - 폴더를 따로 안 정해도 여기서 읽음


def default_dir():
    """안내한 torch 설치 폴더 중 있는 것 (없으면 None)"""
    for d in DEFAULT_DIRS:
        try:
            if os.path.isdir(os.path.join(d, "torch")) or os.path.isdir(os.path.join(d, "Lib", "site-packages", "torch")):
                return d
        except OSError:
            pass
    return None


def load(path=None):
    """torch 를 읽음 (path = pip --target 폴더 · 없으면 안내한 기본 경로 D:\torchlib). → (성공, 설명)"""
    global _torch, _err, _path
    if _torch is not None:
        return True, "torch %s (%s)" % (_torch.__version__, _path or "기본 위치")
    if not path or not os.path.isdir(path):
        path = default_dir() or path
    if path:
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            _err = "폴더가 없음: %s" % path
            return False, _err
        for p in (path, os.path.join(path, "site-packages"), os.path.join(path, "Lib", "site-packages")):
            if os.path.isdir(os.path.join(p, "torch")) and p not in sys.path:
                sys.path.insert(0, p)
                lib = os.path.join(p, "torch", "lib")
                if hasattr(os, "add_dll_directory") and os.path.isdir(lib):
                    try:
                        os.add_dll_directory(lib)            # Windows: torch DLL 위치
                    except OSError:
                        pass
                    os.environ["PATH"] = lib + os.pathsep + os.environ.get("PATH", "")
                break
    t0 = time.time()
    try:
        import torch
    except Exception as e:                                   # 버전 안 맞음 · 빠진 표준 모듈 등
        _err = "%s: %s" % (type(e).__name__, e)
        return False, _err
    _torch, _path = torch, path
    try:
        torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))
    except Exception:
        pass
    return True, "torch %s · %.1f초 (%s)" % (torch.__version__, time.time() - t0, path or "기본 위치")


def available():
    return _torch is not None


def last_error():
    return _err


def py_tag():
    return "Python %d.%d" % sys.version_info[:2]


class TorchNNModel:
    """NNModel 과 같은 모양 (fit / apply / n_coef / w) 이라 엔진은 그대로 씀"""

    def __init__(self, M=4, hidden=64, epochs=60, lr=2e-3, seed=0):
        if _torch is None:
            raise RuntimeError("torch 를 못 읽음 - [설정 ▾] - [torch 폴더 지정…] (%s)" % (_err or "폴더 없음"))
        from dpd_core import NNModel
        self.M, self.H, self.epochs, self.lr = int(M), int(hidden), int(epochs), float(lr)
        self._f = NNModel(M, 8, 1, pn=True)                  # 입력 특징 만들기만 빌려 씀 (위상 정규화)
        self.seed = seed
        self.w = None
        self.scale = 1.0
        self.K, self.L = "NN", 0                      # 표시용 (NNModel 과 같게)

    def n_in(self):
        return self._f.n_in()

    def n_coef(self):
        n, H = self.n_in(), self.H
        return n * H + H + H * H + H + H * 2 + 2

    def _net(self):
        T = _torch
        _torch.manual_seed(self.seed)
        net = T.nn.Sequential(T.nn.Linear(self.n_in(), self.H), T.nn.Tanh(),
                              T.nn.Linear(self.H, self.H), T.nn.Tanh(),
                              T.nn.Linear(self.H, 2))
        with T.no_grad():
            net[-1].weight.mul_(0.01)
            net[-1].bias.zero_()
        return net.double()

    def _load(self, w):
        net = self._net()
        if w is not None:
            net.load_state_dict(w["sd"])
        return net

    def fit(self, u, d, n_fit=40000, rng=None, init=None, cancelled=None):
        T = _torch
        rng = rng or np.random.default_rng(0)
        n = len(u)
        n_fit = int(min(n_fit or n, n))
        s0 = int(rng.integers(0, max(n - n_fit, 1)))
        sc = init["scale"] if init else 1.0 / (np.sqrt(np.mean(np.abs(u) ** 2)) + 1e-30)
        F, uu = self._f._feat(u * sc, s0, n_fit)
        rot = self._f._rot
        t = (d[s0:s0 + n_fit] * sc - uu) * rot                 # 돌린 좌표에서 '바로 통과와의 차이'
        Ft = T.from_numpy(F)
        Yt = T.from_numpy(np.stack([t.real, t.imag], 1))
        net = self._load(init)
        opt = T.optim.Adam(net.parameters(), lr=self.lr)
        sched = T.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(self.epochs, 1))
        bs = 512
        g = T.Generator().manual_seed(int(rng.integers(0, 2 ** 31)))
        for ep in range(self.epochs):
            if cancelled is not None and cancelled():
                break
            perm = T.randperm(n_fit, generator=g)
            for i in range(0, n_fit, bs):
                idx = perm[i:i + bs]
                opt.zero_grad()
                loss = T.mean((net(Ft[idx]) - Yt[idx]) ** 2)
                loss.backward()
                opt.step()
            sched.step()
        with T.no_grad():
            o = net(Ft).numpy()
        r = (o[:, 0] + 1j * o[:, 1]) * np.conj(rot) + uu - d[s0:s0 + n_fit] * sc
        nmse = 10 * np.log10(np.mean(np.abs(r) ** 2) / (np.mean(np.abs(d[s0:s0 + n_fit] * sc) ** 2) + 1e-30))
        self.w = {"sd": {k: v.clone() for k, v in net.state_dict().items()}, "scale": sc}
        return self.w, float(nmse)

    def apply(self, x, w=None, chunk=65536):
        T = _torch
        w = self.w if w is None else w
        if w is None:
            return x.copy()
        net = self._load(w)
        sc = w["scale"]
        out = np.empty(len(x), complex)
        with T.no_grad():
            for s0 in range(0, len(x), chunk):
                n = min(chunk, len(x) - s0)
                F, uu = self._f._feat(x * sc, s0, n)
                o = net(T.from_numpy(F)).numpy()
                out[s0:s0 + n] = (uu + (o[:, 0] + 1j * o[:, 1]) * np.conj(self._f._rot)) / sc
        return out
