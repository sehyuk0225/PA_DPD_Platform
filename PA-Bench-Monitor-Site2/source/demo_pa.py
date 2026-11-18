# -*- coding: utf-8 -*-
"""데모용 가상 PA (Doherty 비슷한 AM-AM / AM-PM + 메모리) - 장비 없이 DPD 루프 확인"""
import numpy as np


class DemoPA:
    def __init__(self, gain_db=40.0, psat_dbm=50.0, seed=3):
        self.g = 10 ** (gain_db / 20)
        self.vsat = np.sqrt(2 * 50 * 10 ** ((psat_dbm - 30) / 10))   # 출력 포화 첨두 전압
        self.rng = np.random.default_rng(seed)
        self.h_in = np.array([1.0, 0.18 - 0.08j, 0.05 + 0.02j])        # 입력 쪽 메모리 (정합 · 바이어스 회로)
        self.h_out = np.array([1.0, -0.06 + 0.04j])

    def __call__(self, v_in):
        """v_in: PA 입력 첨두 전압 (복소 기저대역) → 출력 첨두 전압"""
        u = np.convolve(v_in, self.h_in)[:len(v_in)] / np.sum(np.abs(self.h_in))
        a = np.abs(u) * self.g / self.vsat
        p = 1.6
        am = a * (1 + 0.12 * a ** 2 * np.exp(-3 * a ** 2)) / (1 + a ** (2 * p)) ** (1 / (2 * p))   # 도허티 비슷: 약간 확장 후 압축
        pm = np.radians(18.0) * a ** 2 / (1 + 0.8 * a ** 2) - np.radians(4.0) * a
        y = self.vsat * am * np.exp(1j * (np.angle(u) + pm))
        y = np.convolve(y, self.h_out)[:len(y)] / np.sum(np.abs(self.h_out))
        y = y + 0.025 * np.roll(y, 1) * (np.abs(np.roll(y, 1)) / self.vsat) ** 2      # 비선형 메모리
        nf = self.vsat * 10 ** (-62 / 20)
        y = y + nf * (self.rng.standard_normal(len(y)) + 1j * self.rng.standard_normal(len(y))) / np.sqrt(2)
        return y
