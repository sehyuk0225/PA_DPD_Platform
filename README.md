# PA_DPD_Platform

PA Bench Monitor Site2 기반 PAM 자동 측정 · DPD 플랫폼.

- 원본: [jaybaek-git/Works](https://github.com/jaybaek-git/Works) (rto-current-meter 브랜치, commit a3bd213)
- `PA-Bench-Monitor-Site2/PA-Bench-Monitor-Site2_v1.4.15_AnalyzerLink.zip` : 원본 zip 그대로
- `PA-Bench-Monitor-Site2/source/` : 위 zip 을 푼 소스 (수정은 여기서)

## 실행
- 빌드 없이: `source/run_without_build_site2.bat`  (처음 한 번 `python -m pip install openpyxl PySide6 matplotlib numpy pyvisa`)
- EXE 빌드: `source/build_exe_site2.bat`
