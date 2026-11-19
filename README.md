# PA_DPD_Platform

PA Bench Monitor Site2 기반 PAM 자동 측정 · DPD 플랫폼.

- 원본: [jaybaek-git/Works](https://github.com/jaybaek-git/Works) (rto-current-meter 브랜치, commit a3bd213)
- `PA-Bench-Monitor-Site2/PA-Bench-Monitor-Site2_v1.4.15_AnalyzerLink.zip` : 원본 zip 그대로
- `PA-Bench-Monitor-Site2/PA-Bench-Monitor-Site2_v1.4.24_AnalyzerLink_JSH.zip` : 원본 v1.4.24 + JSH 수정
- `PA-Bench-Monitor-Site2/source/` : 지금 소스 = v1.4.24_JSH (수정은 여기서)

## JSH 수정 내역 (v1.4.24 기준)
- DPD 4 카드: GMP **Lagging 항 (l+)** · **Leading 항 (l−)** 수를 따로 설정 (빈칸 = L 과 같음 → 원본과 똑같이 동작)
  - 'K · M · L 자동' · 'ILC→모델 K·M·L 넓힘' 이 고른 모델은 L 하나로 앞뒤 같게

## 실행
- 빌드 없이: `source/run_without_build_site2.bat`  (처음 한 번 `python -m pip install openpyxl PySide6 matplotlib numpy pyvisa`)
- EXE 빌드: `source/build_exe_site2.bat`
- EXE 이름 끝에 `_JSH` (`PA-Bench-Monitor-Site2_v1.4.24_JSH.exe`) · 위쪽 패널 바탕 (바탕 위 글씨 포함) 녹색 #00704A · 큰 숫자 칸은 원래 검정 (원본과 구분, `pa_bench_monitor_site2.py` 에서만 칠함)
- ILC 결과 `.npz` 저장: 한계 ACLR (ILC) 뿐 아니라 ILC→모델 · 방식 비교 · 이어서 안의 ILC 도, 중지하면 그때까지 최고 결과를 `…_중지.npz` 로 (`%APPDATA%\PADPDTester\ilc`)
- ILC→모델: 찾은 모델 파라미터 (모델 · K · M · L · Lagging · Leading · 계수 수 · 학습 오차 · 큰 계수 12 개) 를 로그에 (학습 직후 · ILA 다듬은 뒤)
