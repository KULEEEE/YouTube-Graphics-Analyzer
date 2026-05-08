# ytga

**Yo**uTube **G**raphics **A**nalyzer — 게임플레이 영상 URL을 던지면 핵심 프레임을 뽑고 Claude가 그래픽스 기법을 분석해주는 Windows 데스크톱 앱.

<!-- TODO: hero screenshot -->

```
URL  →  프레임 추출 (yt-dlp + ffmpeg)  →  Claude 분석  →  Markdown / PDF 리포트
```

---

## ⚡ Quick start

1. **[Releases](../../releases)** 에서 최신 `ytga.exe` 다운로드
2. **[Claude Code CLI](https://docs.claude.com/en/docs/claude-code)** 설치 + 로그인 (`claude --version` 으로 확인)
3. `ytga.exe` 실행 → URL 붙여넣기 → **프레임 추출** → 결과 OK면 **AI 분석 실행**

> ytga는 Anthropic API 키를 따로 받지 않습니다. 사용자의 기존 Claude Code 인증(Pro/Max/Team 구독 또는 API 청구)을 그대로 사용합니다. `yt-dlp` 와 `ffmpeg` 는 exe에 번들되어 있어 별도 설치 불필요.

---

## 사용법

<!-- TODO: main UI screenshot -->

### 1. 프레임 추출

URL과 옵션 입력 후 **프레임 추출** 클릭. AI 호출 없이 다운로드 + 샘플링 + 중복 제거만 진행됩니다.

| 옵션 | 기본 | 설명 |
|---|---|---|
| **프로젝트 이름** | (영상 제목 자동) | 출력 폴더명. 같은 이름이면 덮어쓰기, 다르면 새 폴더 |
| **시작 시각 (초)** | 0 | 인트로/타이틀 스킵용. 60, 90 같은 값 |
| **최대 길이 (초)** | 600 | 시작 이후 분석할 길이. 0 = 끝까지 |
| **최대 프레임** | 30 | 최종 추출할 최대 장 수 |
| **샘플 간격 (초)** | 1.0 | N초마다 1프레임. 작을수록 조밀 |
| **pHash 임계값** | 12 | 시각적 중복 통합 강도. 0=비활성 / 6=약함 / 20=매우 적극적 |
| **품질 필터** | ON | 거의 검은/흰 화면 (플래시·페이드) 자동 제외 |

추출이 끝나면 썸네일 그리드 표시.

<!-- TODO: thumbnail grid screenshot -->

각 썸네일에서:
- **본문 클릭** — 원본 이미지 열기 (탐색기/뷰어)
- **우상단 X** — 그 프레임만 삭제 (파일 + manifest 동기화)

마음에 안 들면 옵션 바꾸고 **다시 추출**으로 새로 시도. 같은 옵션이면 결과가 항상 같으므로, 다른 결과를 원할 땐 옵션 중 하나를 바꿔야 합니다.

### 2. AI 분석

프레임이 마음에 들면 **AI 분석 실행** 클릭. headless `claude -p` 가 모든 프레임을 multimodal로 읽고 한국어 리포트 생성:

```
### 1. 두드러지는 기법
- **Lumen GI** [확신도: 추정]
  - 근거: 프레임 7, 12에서 실내↔실외 전환 시 …
  - 왜 주목할 만한가: …

### 2. 파이프라인 총평
…
```

확신도(확실/추정/가능)와 근거 프레임 인용을 강제하는 프롬프트. "이 게임에만 두드러지는 것" 위주로 정리하도록 가이드되어 있어, "텍스처를 사용한다" 같은 당연한 항목은 빠집니다.

### 3. 후속 질문

분석 후 화면 하단에 채팅 영역이 활성화됨. `claude --resume`으로 같은 세션이 유지되어 모델이 이미 본 프레임을 그대로 들고 답합니다.

<!-- TODO: chat screenshot -->

> "프레임 5의 그 반사 SSR vs RT 어느 쪽 가능성 높아?"
> "Lumen이라고 했는데 SDFGI일 수도 있지 않아?"

대화는 자동으로 `chat.json` + `report.md` 에 누적 저장.

### 4. 리포트 저장

| 버튼 | 결과 |
|---|---|
| **리포트 저장** | `report.md` — 분석 + Q&A 합쳐진 마크다운 |
| **리포트 복사** | 위 내용 클립보드로 복사 |
| **PDF 저장** | 표지 + 분석 + 프레임 갤러리 + Q&A 가 든 PDF |
| **폴더 열기** | 프로젝트 폴더 탐색기에서 열기 |

기본 저장 경로: `%USERPROFILE%\ytga-output\<프로젝트명>\`

폴더 안 구성:
```
<프로젝트>/
├── project.json      메타데이터 (URL, session_id, 시각)
├── manifest.json     프레임 목록 + 절대 timestamp
├── chat.json         Q&A 히스토리
├── report.md         분석 + Q&A
├── report.pdf        (PDF 저장 누른 경우)
├── frame_001.jpg
└── …
```

### 5. 프로젝트 다시 열기

헤더 **프로젝트 열기** → 폴더 선택 → 프레임/리포트/채팅 전부 복원됩니다. session_id가 아직 유효하면(같은 머신, 며칠 이내) 후속 질문도 그대로 이어집니다.

---

## 결과 튜닝 가이드

| 증상 | 처방 |
|---|---|
| 같은 장면이 반복 | pHash 임계값 ↑ (12 → 16, 20) |
| 다른 장면이 묶임 | pHash 임계값 ↓ (12 → 8, 6) 또는 0으로 비활성 |
| 인트로/시네마틱만 잡힘 | 시작 시각 ↑ (60, 90, 120) |
| 너무 적은 프레임 | 샘플 간격 ↓ (1.0 → 0.5, 0.3) |
| 너무 많아서 느림 | 샘플 간격 ↑ (1.0 → 2.0, 3.0) |
| 분석 결과가 일반적 | `src/ytga/prompts.py` 의 `SYSTEM_GUIDE` 직접 수정 |

---

## CLI

소스에서 실행하거나 `ytga.exe` 도 `--cli` 인자로 CLI 모드:

```cmd
ytga.exe --cli "https://www.youtube.com/watch?v=VIDEO_ID" ^
  --project-name "Lost Ark Test" ^
  --start-seconds 60 ^
  --max-duration 300 ^
  --max-frames 25 ^
  --sample-interval 1.0 ^
  --phash-threshold 12 ^
  --export-pdf
```

자주 쓰는 옵션:

```
--project-name TEXT      프로젝트 폴더명 (생략 시 영상 제목)
--out PATH               출력 루트 (기본 %USERPROFILE%\ytga-output)
--start-seconds N        앞쪽 N초 스킵
--max-duration N         시작 이후 분석할 길이. 0 = 끝까지
--max-height N           다운로드 해상도 상한 (기본 1080)
--min-frames N           최소 프레임 수 (기본 10)
--max-frames N           최대 프레임 수 (기본 30)
--sample-interval N      샘플 간격(초) 기본 1.0
--phash-threshold N      시각적 중복 통합 강도 (기본 12, 0=비활성)
--no-quality-filter      품질 필터 끄기
--format jpg|png         출력 이미지 포맷
--keep-video             다운로드 mp4 보관
--no-analyze             프레임 추출만, Claude 호출 X
--export-pdf             report.pdf 자동 생성
```

전체 목록은 `--help`.

---

## 소스에서 빌드

### PowerShell (권장)

```powershell
git clone https://github.com/KULEEEE/YouTube-Graphics-Analyzer
cd YouTube-Graphics-Analyzer

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 개발 모드 실행
python -m ytga

# 단일 .exe 빌드
pyinstaller build.spec
# 결과: dist\ytga.exe
```

### Windows cmd

cmd에서는 두 군데가 다릅니다 — `cd /d` 로 드라이브 이동, `.bat` 로 venv 활성화.

```cmd
git clone https://github.com/KULEEEE/YouTube-Graphics-Analyzer
cd /d YouTube-Graphics-Analyzer

python -m venv .venv
.venv\Scripts\activate.bat
pip install -e ".[dev]"

REM 개발 모드 실행
python -m ytga

REM 단일 .exe 빌드
pyinstaller build.spec
REM 결과: dist\ytga.exe
```

> 활성화 잘 됐으면 프롬프트 앞에 `(.venv)` 가 붙습니다.

### GitHub Actions 자동 빌드

태그 푸시로 트리거 (`windows-latest` 러너에서 PyInstaller 실행, Release에 `ytga.exe` 자동 첨부):

```cmd
git tag v0.1.1
git push --tags
```

---

## 라이선스

MIT — © 2026 KULEEEE.

의존성: yt-dlp (Unlicense), FFmpeg (LGPL via imageio-ffmpeg), Flet (Apache-2.0), Pillow (HPND), ImageHash (BSD), reportlab (BSD).

---

## Disclaimer / 사용 시 주의

이 도구는 **개인적인 학습·분석·연구 목적**으로 만들어졌습니다. 사용자는 다음을 본인 책임으로 준수해야 합니다.

- **YouTube ToS** — 비공식 다운로드를 일반적으로 금지. 계정/IP 차단 가능성.
- **저작권** — 한국 저작권법 §30(사적 복제)·§35-3(공정 이용) 범위 안에서 개인 사용. 재배포·상업적 이용·가공 후 게시는 불법.
- **DRM** — 본 도구는 DRM 우회를 시도하지 않습니다. DRM 적용 영상(멤버십 전용 등) 다운로드 금지.
- **권리 보유 콘텐츠** — 가능하면 본인 영상이나 명시적 라이선스(CC 등)가 부여된 영상으로 분석하세요.

이 소프트웨어는 MIT 라이선스로 배포되며, 사용으로 인한 어떤 결과(법적 책임 포함)에 대해서도 작성자/기여자는 책임지지 않습니다 (LICENSE 파일 참조).
