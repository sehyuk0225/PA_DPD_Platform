# PA_DPD_Platform

PA Bench Monitor Site2 기반 PAM 자동 측정 · DPD 플랫폼.

- 원본: [jaybaek-git/Works](https://github.com/jaybaek-git/Works) (rto-current-meter 브랜치) - 지금 기준 v1.4.52 (commit 29c7e9c)
- `PA-Bench-Monitor-Site2/PA-Bench-Monitor-Site2_v1.4.15_AnalyzerLink.zip` : 원본 zip 그대로
- `PA-Bench-Monitor-Site2/PA-Bench-Monitor-Site2_v1.4.52.2_AnalyzerLink_JSH.zip` : **v1.4.52.2_JSH** (최신) (v1.4.52.1_JSH + 5 카드 ILC 보정 대역 제한 · 1966 MSps ILC 발산 막기)
- `PA-Bench-Monitor-Site2/PA-Bench-Monitor-Site2_v1.4.52.1_AnalyzerLink_JSH.zip` : v1.4.52.1_JSH (v1.4.52_JSH + 4 카드 모델 Multi-Kernel GMP 3D · 4D · 5D)
- `PA-Bench-Monitor-Site2/PA-Bench-Monitor-Site2_v1.4.52_AnalyzerLink_JSH.zip` : v1.4.52_JSH (원본 v1.4.52 + 녹색 테마 + ILC 신호 · 평균 I/Q `.bin` 저장 → 프로그램 폴더 `Signals\`)
- `PA-Bench-Monitor-Site2/PA-Bench-Monitor-Site2_v1.4.42.4_AnalyzerLink_JSH.zip` : v1.4.42.4_JSH (원본 v1.4.42 + 예전 JSH 기능 전부) (로그 자동 저장 · 중요한 줄 【코드】 표시 · 끝에 요약)
- `PA-Bench-Monitor-Site2/PA-Bench-Monitor-Site2_v1.4.42.3_AnalyzerLink_JSH.zip` : v1.4.42.3_JSH (v1.4.42.2_JSH + 관측 대역 스위치 세 개를 5 카드 한 묶음으로 · 로그 확인)
- `PA-Bench-Monitor-Site2/PA-Bench-Monitor-Site2_v1.4.42.2_AnalyzerLink_JSH.zip` : v1.4.42.2_JSH (원본 v1.4.42 + JSH 수정 · FSW 관측 대역 밖 안 고침)
- 예전 JSH 버전 zip 도 같은 폴더에 그대로 (`…v1.4.24_…_JSH.zip` · `…v1.4.24.1_…_JSH.zip` · `…v1.4.24.2_…_JSH.zip` · `…v1.4.24.3_…_JSH.zip` = 원본 v1.4.24 기준 · `…v1.4.29.1_…_JSH.zip` = 원본 v1.4.29 기준 · `…v1.4.34.1_…_JSH.zip` = 원본 v1.4.34 기준 · `…v1.4.36_…_JSH.zip` · `…v1.4.36.1_…_JSH.zip` = 원본 v1.4.36 기준 · `…v1.4.42_…_JSH.zip` · `…v1.4.42.1_…_JSH.zip` = 원본 v1.4.42 기준) - 새 버전은 지우지 않고 번호만 올려 추가
- `PA-Bench-Monitor-Site2/source/` : 지금 소스 = v1.4.52.2_JSH (수정은 여기서)

## JSH 수정 내역 — v1.4.52.2_JSH (원본 v1.4.52 + 아래)
- (v1.4.52.2) 5 카드 **ILC 보정 대역** (빈칸 = 자동 ±(바깥 인접 끝 + 인접 간격/4), 0 = 전 대역): 넓은 샘플레이트에서 대역 끝 위상 틀어짐으로 ILC 가 발산하던 것 막음 (`dpd_engine.ilc_band` · `ilc_band_mask`)
- (v1.4.52.1) 4 카드 모델 **Multi-Kernel GMP**: 2D GMP + 3D · 4D · 5D 커널 (nD 항 = x(n−m) · 서로 다른 지연 envelope n−1 개의 곱), 커널마다 `K Q M O` 칸 (`dpd_core.MKGMPModel`)
- 위쪽 패널 녹색 테마 #00704A · EXE 이름 끝 `_JSH` (`pa_bench_monitor_site2.py` 에서만 칠함)
- ILC 할 때 반복마다 **보낸 ILC 신호** · **받아 주기마다 평균한 I/Q** 를 `.bin` (I, Q 번갈아 float32 LE, 헤더 없음) 으로
  프로그램 (EXE) 폴더 안 `Signals\ILC_날짜_시각_SG○○dBm\` 에 자동 저장 (`00_원신호` · `00_DPD_OFF_평균IQ` · `ILC01_ILC신호` · `ILC01_평균IQ` … · `최고_ILC신호` · `최고_평균IQ` · `_형식.txt`), 못 쓰면 `%APPDATA%\PADPDTester\Signals\` (`dpd_engine.py` 의 `signals_dir` · `sig_bin_*`)
- 아래는 예전 JSH 판 (v1.4.42.4_JSH 까지) 내역 - v1.4.52_JSH 에는 없음

## (예전) JSH 수정 내역 — v1.4.42.4_JSH (원본 v1.4.42 + JSH 수정)
- DPD 4 카드: GMP **Lagging 항 (l+)** · **Leading 항 (l−)** 수를 따로 설정 (빈칸 = L 과 같음 → 원본과 똑같이 동작)
  - 'K · M · L 자동' · 'ILC→모델 K·M·L 넓힘' 이 고른 모델은 L 하나로 앞뒤 같게

## 실행
- 빌드 없이: `source/run_without_build_site2.bat`  (처음 한 번 `python -m pip install openpyxl PySide6 matplotlib numpy pyvisa`)
- EXE 빌드: `source/build_exe_site2.bat`
- EXE 이름 끝에 `_JSH` (`PA-Bench-Monitor-Site2_v1.4.42.4_JSH.exe`) · 위쪽 패널 바탕 (바탕 위 글씨 포함) 녹색 #00704A · 큰 숫자 칸은 원래 검정 (원본과 구분, `pa_bench_monitor_site2.py` 에서만 칠함)
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
- (v1.4.42.4) 로그를 실행마다 `%APPDATA%\PADPDTester\logs\` 에 자동 저장 · 중요한 줄 【A1】~【F1】 코드 표시 (노란 굵은 글씨) · 실행 끝에 요약
- (v1.4.42.3) 관측 대역 스위치 세 개를 5 카드 [★ JSH · FSW 관측 대역 안만] 한 묶음으로 · DPD OFF 때 로그에 켜짐/꺼짐
- (v1.4.42.2) **FSW 관측 대역 밖은 고치지도 · 맞추지도 않음** (5 카드 세 칸, 기본 켬): ILC · 대역 제한 학습 (원본 범위 버그 고침) · ILA/DLA/ILA+보정 학습 - 400 MHz · 983 MSps 에서 GMP 가 −32 dBc 에 멈추던 원인 (ILC 가 FSW 밖 대역에 쌓은 성분)

## 도구
- `tools/ilc_diag.py` : 저장한 ILC 결과 (`.npz`) 진단 - 모델이 ILC 입력을 못 따라가는 이유 (켤레 · I/Q 이미지 · 긴 메모리 · 스퍼 · 크기별 · 시간 변화) 를 **숫자만** 출력 (파형 데이터 안 나옴)
  - 실행: `python tools/ilc_diag.py` (가장 최근 `%APPDATA%\PADPDTester\ilc\*.npz`) 또는 `python tools/ilc_diag.py 파일.npz [--adj 200]` · Python 3 + numpy
