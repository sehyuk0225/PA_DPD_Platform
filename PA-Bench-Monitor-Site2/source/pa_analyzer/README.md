# RF PA Analyzer — v2.14.4

**PA Measurement Analyzer**(PAE / Gain vs Pout)와 **S-Parameter Viewer**를 하나로 묶은 Windows 프로그램입니다.
창 위쪽 탭으로 전환합니다. S-Parameter 사용법은 [docs/SPARAM_GUIDE.md](docs/SPARAM_GUIDE.md)를 보세요.

---

## PA 탭 (PA Measurement Analyzer)

RF Power Amplifier **Measurement(Excel)** 결과를 프로그램에 저장된 **Simulation** 결과와 자동으로 비교하는
Windows용 **실행 프로그램(.exe)**입니다. Python 설치 없이 압축을 풀고 더블클릭하면 실행됩니다.

![PAE vs Pout](assets/example_PAE_vs_Pout_3p6GHz.png)

> **UI Screenshot**: 첫 실행 후 캡처하여 `assets/ui_screenshot.png`로 추가해 주세요.
> (위 이미지는 프로그램의 **Copy Plot / Save PNG** 결과물과 동일한 Renderer로 생성한 것입니다.)

---

## 1. 주요 기능 (MVP)

| 구분 | 기능 |
|---|---|
| Excel | 모든 Sheet 자동 인식, Sheet별 Measurement Dataset, Header 행 자동 탐색, Column Mapping |
| FREQ | Sheet 이름(`3.6GHz`, `3600MHz`, `FREQ_3P6G`, `FREQ_3600`) / 셀 / Column에서 자동 인식, 수동 지정 |
| Condition | **FREQ, VGS,CA, VGS,PK, VGS,DRV** 항상 표시 (없으면 N/A) + VDD, Temp, Mode 등 자동 추가 |
| Simulation | 폴더 기반 Set 관리 (Typical / Corner …), FREQ 기준 자동 매칭 (±1 MHz) |
| Graph | PAE vs Pout, Gain vs Pout, Simulation(Line) + Measurement(Marker+Line) Overlay |
| 조작 | X/Y Min·Max, Auto Scale, Reset, 휠 Zoom, Pan, Box Zoom, Hover Tooltip |
| Raw data | DUT_Pout (dBm) / Gain / PAE(%) 자동 선택 (SG Pout, mW 열 제외) |
| Plot 창 | 1~4개, 모두 같은 크기, Plot별 PAE/Gain·축·Marker, Copy All Plots |
| 겹쳐 보기 | Datasets 체크박스 목록, 붙여넣을 때마다 이름·FREQ 입력 |
| Marker | V/H 키, 드래그·방향키 (Pout 0.1 dBm / Gain 0.1 dB / PAE 0.5 %), 값 상자 메뉴 |
| Back-off | Psat(최대 Pout) − BO 지점의 Gain / PAE 표 |
| 퀵메뉴 | Datasets 우클릭: 색상·선 종류·굵기·점 모양 |
| 붙여넣기 | Excel에서 복사(Ctrl+C) → 프로그램에 Ctrl+V → 즉시 그래프 (DRM 파일 대응), 조건/Header 자동 인식 |
| 프로젝트 | `.pam` 저장 / 열기: 붙여넣은 데이터 + FREQ 지정 + Column Mapping + Graph 설정 |
| 내보내기 | **Copy Plot** (Clipboard PNG → PowerPoint `Ctrl+V`), **Save PNG** (파일명에 FREQ 자동 포함), 조건 정보 포함 |
| 안정성 | 빈 Sheet, Column 누락, 텍스트/`#DIV/0!`/NaN 셀, FREQ 인식 실패, Simulation mismatch → 종료 없이 안내 메시지 |

## 2. 사용자: 설치 및 실행

1. 배포된 `PA-Measurement-Analyzer-win64.zip`을 받습니다.
   (GitHub Repository의 **Actions → 최신 Build Windows EXE → Artifacts**, 또는 **Releases**)
2. 압축을 풀고 `PA-Measurement-Analyzer.exe`를 실행합니다.
3. 상세 사용법은 [docs/USER_GUIDE.md](docs/USER_GUIDE.md)를 참고하세요. 같은 내용이 zip 안에 `사용법.md`로 들어 있습니다.

## 3. 개발자: EXE 빌드

### 방법 A — GitHub에서 자동 빌드 (로컬 Python 불필요)
`main`에 Push하면 `.github/workflows/build-exe.yml`이 Windows Runner에서 다음을 수행합니다.
보안 검사 → Unit/GUI 테스트 → PyInstaller 빌드 → **빌드된 exe 자체 Smoke Test** → zip Artifact 업로드.
`v0.1.0`처럼 Tag를 Push하면 Release에도 첨부됩니다.

### 방법 B — 내 PC에서 빌드
Python 3.10 이상을 설치한 뒤 다음을 실행합니다.
```bat
build_exe.bat              REM 폴더형 (빠른 시작, 권장)
build_exe.bat --onefile    REM exe 파일 1개 (시작 시 5~10초 압축 해제)
```
결과는 `dist\PA-Measurement-Analyzer-win64.zip`입니다.

### 소스에서 바로 실행 (개발용)
```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python src\main.py
```

### 필요한 Python Package
`PySide6`(GUI), `matplotlib`(Plot), `numpy`, `openpyxl`(Excel)이 필요하며, 빌드에는 `pyinstaller`가 추가로 필요합니다.

## 4. Excel Input Format

**한 Sheet = 한 Measurement Dataset**입니다. 레이아웃은 자유롭게 구성할 수 있으며, 아래 형태를 모두 자동으로 인식합니다.

```
(A) 조건 블록 + 아래쪽 Table            (B) 조건을 Table Column으로
   A        B        C                      FREQ(GHz)  VGS,CA  VGS,PK  VGS,DRV  Pout   Gain  PAE
1  FREQ     3.6 GHz                         3.6        -1.20   -1.35   -1.10    30.1   14.0  16.5
2  VGS,CA   -1.20    V                      3.6        -1.20   -1.35   -1.10    31.0   14.0  17.8
3  VGS,PK   -1.35    V
4  VGS,DRV  -1.10    V                   (C) 한 셀에  "VGS,CA = -1.20 V"
6  Pout(dBm) Gain(dB) PAE(%)             (D) Header 아래 단위 행(dBm, %, …)은 자동으로 건너뜀
7  30.1      14.0     16.5
```

* **Header 이름(대소문자·괄호·단위 무관):** `Pout`, `Po`, `Output Power` / `PAE` / `Gain`, `Gt`, `Gp` / `Pin`을 인식합니다.
* **조건 이름:** `VGS,CA`, `Vgs_CA`, `VGS_MAIN` / `VGS,PK`, `VGS_PEAK` / `VGS,DRV`, `VGS_DRIVER` / `FREQ`, `Frequency`를 인식합니다.
* **단위 처리:** PAE가 0~1 분수이면 %로 자동 변환합니다. Pout 단위가 W이면 dBm으로 변환합니다.
* **FREQ 우선순위:** 사용자 지정 → Sheet 내부 값 → Sheet 이름 → 파일 이름 순입니다. Sheet 이름과 내부 값이 다르면 경고를 표시합니다.
* 예제 파일: `data/example/Measurement_Example.xlsx`에는 Sheet마다 다른 레이아웃과 의도적인 오류 셀이 들어 있습니다.

## 5. Simulation Data Format

```
local_data/simulation/          ← 실제 데이터 (Git 제외, exe 옆에 두면 자동 사용)
data/example/simulation/        ← 저장소에 포함된 합성 예제
├── typical/            → Set "Typical"
│   ├── 3.4GHz.csv      → FREQ는 파일명 또는 "# FREQ = 3.4 GHz" 줄에서 인식
│   └── …
└── corner_ss/          → Set "Corner SS"
    └── corner_ss_sweep.csv   → FREQ column이 있으면 주파수별 Dataset으로 자동 분리
```
CSV 또는 xlsx를 사용할 수 있고, 최소한 `Pout`, `PAE`, `Gain` Column이 있어야 합니다. Measurement와 Pout Sampling이 달라도 됩니다(Interpolation하지 않음).

## 6. Measurement Condition

| 항목 | 표시 | 비고 |
|---|---|---|
| FREQ | 항상 | GHz, 인식 출처(Sheet 내부 / Sheet 이름 / 사용자 지정) 함께 표시 |
| VGS,CA / VGS,PK / VGS,DRV | 항상 | Carrier(Main) / Peaking / Driver Gate Bias, 없으면 N/A |
| VDD, VCC, VGG, Temp, Mode, Bias, Load, DUT, Board, Lot, Date | 값이 있을 때 | 자동 인식, Plot 이미지에도 포함 |

## 7. 테스트

```bat
python -m unittest discover -s tests -v       REM 33개 (Parsing, 매칭, Plot, Export, UI wiring)
python src\main.py --smoke-test               REM 실제 GUI 15단계 자체 점검
python scripts\security_check.py              REM Push 전 실제 데이터/비밀정보 검사
```
`git config core.hooksPath .githooks`를 설정하면 Push할 때마다 보안 검사가 자동으로 실행됩니다.

## 8. 데이터 보안

* `.gitignore`는 모든 `xlsx/csv/sNp` 등 데이터 파일을 차단하고, `data/example/`(합성 예제)만 허용합니다.
* 실제 측정·Simulation 데이터는 `local_data/`에만 보관하세요.
* Repository는 **Private**로 생성하세요.

## 9. 구조 및 확장

구조와 확장 방법은 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)를 참고하세요.

**향후 개발 예정:** Multi-Frequency / Multi-Simulation / Multi-Measurement Overlay, Pout vs Pin, Drain Eff., Idd, ACLR, EVM, AM-AM/AM-PM (Graph 정의는 이미 등록됨), Psat / P1dB / PAE Peak / Gain Compression 자동 추출, Bias·VDD·Temperature Sweep Sheet 분리, S-parameter(Touchstone).


변경 이력은 [CHANGELOG.md](CHANGELOG.md)를 참고하세요.

### 빌드 디스크 공간
* **필요 공간:** 처음 빌드할 때 약 3 GB, 이후 약 1.5 GB입니다.
* **공용 빌드 환경:** Python 패키지는 모든 버전이 함께 쓰는 `<드라이브>\PA-Analyzer-buildenv`에 한 번만 설치합니다. 다른 위치를 쓰려면 실행 전에 `set PA_BUILDENV=E:\경로`로 지정하세요.
* **예전 버전 정리:** 예전 버전 폴더에서 `clean_build_files.bat`을 실행하면 .venv / build / dist를 지워 공간을 확보합니다.
