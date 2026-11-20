# PA_DPD_Platform

PA Bench Monitor Site2 기반 PAM 자동 측정 · DPD 플랫폼.

- 원본: [jaybaek-git/Works](https://github.com/jaybaek-git/Works) (rto-current-meter 브랜치) - 지금 기준 v1.4.42 (commit 23adde1)
- `PA-Bench-Monitor-Site2/PA-Bench-Monitor-Site2_v1.4.15_AnalyzerLink.zip` : 원본 zip 그대로
- `PA-Bench-Monitor-Site2/PA-Bench-Monitor-Site2_v1.4.42.1_AnalyzerLink_JSH.zip` : **v1.4.42.1_JSH** (최신) (원본 v1.4.42 + JSH 수정 · DVR 찾기는 원본 것만)
- 예전 JSH 버전 zip 도 같은 폴더에 그대로 (`…v1.4.24_…_JSH.zip` · `…v1.4.24.1_…_JSH.zip` · `…v1.4.24.2_…_JSH.zip` · `…v1.4.24.3_…_JSH.zip` = 원본 v1.4.24 기준 · `…v1.4.29.1_…_JSH.zip` = 원본 v1.4.29 기준 · `…v1.4.34.1_…_JSH.zip` = 원본 v1.4.34 기준 · `…v1.4.36_…_JSH.zip` · `…v1.4.36.1_…_JSH.zip` = 원본 v1.4.36 기준 · `…v1.4.42_…_JSH.zip` = 원본 v1.4.42 기준) - 새 버전은 지우지 않고 번호만 올려 추가
- `PA-Bench-Monitor-Site2/source/` : 지금 소스 = v1.4.42.1_JSH (수정은 여기서)

## JSH 수정 내역 — v1.4.42.1_JSH (원본 v1.4.42 + JSH 수정)
- DPD 4 카드: GMP **Lagging 항 (l+)** · **Leading 항 (l−)** 수를 따로 설정 (빈칸 = L 과 같음 → 원본과 똑같이 동작)
  - 'K · M · L 자동' · 'ILC→모델 K·M·L 넓힘' 이 고른 모델은 L 하나로 앞뒤 같게

## 실행
- 빌드 없이: `source/run_without_build_site2.bat`  (처음 한 번 `python -m pip install openpyxl PySide6 matplotlib numpy pyvisa`)
- EXE 빌드: `source/build_exe_site2.bat`
- EXE 이름 끝에 `_JSH` (`PA-Bench-Monitor-Site2_v1.4.42.1_JSH.exe`) · 위쪽 패널 바탕 (바탕 위 글씨 포함) 녹색 #00704A · 큰 숫자 칸은 원래 검정 (원본과 구분, `pa_bench_monitor_site2.py` 에서만 칠함)
- ILC 결과 `.npz` 저장: 한계 ACLR (ILC) 뿐 아니라 ILC→모델 · 방식 비교 · 이어서 안의 ILC 도, 중지하면 그때까지 최고 결과를 `…_중지.npz` 로 (`%APPDATA%\PADPDTester\ilc`)
- ILC→모델: 찾은 모델 파라미터 (모델 · K · M · L · Lagging · Leading · 계수 수 · 학습 오차 · 큰 계수 12 개) 를 로그에 (학습 직후 · ILA 다듬은 뒤)
- (v1.4.24.2) DPD 4 카드 학습 **ILA+보정**: ILA 뒤 '오차 보정 반복' 만큼 측정하며 출력 오차를 PA 기울기로 보정해 GMP 계수를 더 고침 · **오차 보정 μ** 따로 설정
- (v1.4.24.3) DPD 반복 **되돌림** (가장 좋았던 것보다 '되돌림 기준' dB 넘게 나빠지면 그 계수로 · μ ½) · **DPD OFF 보다 나쁘면 처음부터 다시** (DPD OFF 다시 재서 목표 이득 다시 · 2 번째부터 전체 평균 이득)
- (v1.4.29.1) 원본 v1.4.29 로 올림 - 원본의 ILC→모델 새 모델 (GMP + 긴 선형 등화 · GMP + 긴 메모리 트래핑) 도 Lagging · Leading 그대로 · 모델 파라미터 로그에 그 항 수도
- (v1.4.34.1) 원본 v1.4.34 로 올림 - ILC 중지는 원본 방식 + JSH 자동 저장 · 원본 피크 안정화 (K ↓ · λ ↑) 도 Lagging · Leading 그대로
- (v1.4.36) 원본 v1.4.36 으로 올림 - 원본 대역 제한 학습 (ILC→모델) 도 Lagging · Leading 그대로 · 이름은 원본 이름 끝에 _JSH
- ~~(v1.4.36.1) ILC→모델 DVR 구간 수 · M 자동 찾기 (JSH)~~ → v1.4.42.1 에서 뺌 (원본 v1.4.42 DVR 찾기만 씀)
- (v1.4.42) 원본 v1.4.42 로 올림 - 4 카드 학습 ILA · DLA · ILA+보정 (ILC→모델 은 원본대로 5 카드 🧠)
- (v1.4.42.1) JSH DVR 찾기 뺌 → DVR 구간 수 · M 찾기는 원본 v1.4.42 기능만 (원본과 같은 결과)

## 도구
- `tools/ilc_diag.py` : 저장한 ILC 결과 (`.npz`) 진단 - 모델이 ILC 입력을 못 따라가는 이유 (켤레 · I/Q 이미지 · 긴 메모리 · 스퍼 · 크기별 · 시간 변화) 를 **숫자만** 출력 (파형 데이터 안 나옴)
  - 실행: `python tools/ilc_diag.py` (가장 최근 `%APPDATA%\PADPDTester\ilc\*.npz`) 또는 `python tools/ilc_diag.py 파일.npz [--adj 200]` · Python 3 + numpy
