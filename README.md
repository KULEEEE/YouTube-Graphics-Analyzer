# ytga

**Yo**uTube **G**raphics **A**nalyzer — 게임 플레이 영상 유튜브 링크를 던지면, 영상에서 핵심 프레임을 자동으로 뽑은 뒤 Claude Code로 그래픽스 기법을 추론해주는 데스크톱 도구.

```
YouTube URL  ─►  yt-dlp 다운로드  ─►  ffmpeg scene-change 추출  ─►  Claude Code (headless) 분석  ─►  Markdown 리포트
```

## 무엇을 해주는가

- **프레임 추출**: yt-dlp로 영상을 받고, ffmpeg의 scene change 감지로 의미 있는 장면 전환 프레임만 10~30장 자동 선별. 부족하면 균등 샘플로 보강.
- **그래픽스 분석**: 추출된 프레임들을 headless `claude -p` 로 보내서, 이 게임에서 **두드러지는 렌더링 기법**을 근거(프레임 인용)와 확신도(확실/추정/가능) 라벨과 함께 한국어로 정리.
- **결과물**: `report.md` (분석 리포트) + `manifest.json` (프레임 메타데이터) + 추출된 이미지들.

리포트는 일반 ↔ 특수 기법을 구분합니다. "텍스처를 사용한다", "조명이 있다" 같은 당연한 항목은 적지 않고, **이 게임에만 두드러지는 것**(예: Lumen GI 흔적, RT 반사, Strand 기반 헤어, 특정 톤매핑 시그니처 등)에 집중합니다.

## 요구 사항

- **Windows 10/11 (x64)** — 이 저장소의 릴리스는 Windows .exe만 제공
- **[Claude Code CLI](https://docs.claude.com/en/docs/claude-code)** 설치 + 인증 (`claude --version` 으로 확인)
  - ytga는 Anthropic API 키를 직접 받지 않습니다. 사용자의 기존 Claude Code 인증을 그대로 사용 (Pro/Max/Team 구독 또는 API 청구 — `claude` CLI가 동작하면 됨)

`yt-dlp` 와 `ffmpeg` 는 **별도 설치 불필요** — exe에 번들링되어 있습니다.

## 설치

### A. 이미 빌드된 .exe 받기 (권장)

[Releases](../../releases) 에서 최신 `ytga.exe` 다운로드 후 실행하면 끝.

### B. 소스에서 직접 실행 (개발/CLI 모드)

```powershell
git clone https://github.com/<your-account>/ytga
cd ytga

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# GUI
python -m ytga

# CLI
python -m ytga --cli "https://www.youtube.com/watch?v=..."
```

## 사용법

### GUI

1. `ytga.exe` 실행
2. YouTube URL 붙여넣기
3. (선택) 최대 길이/프레임 수 조정 — 기본값 600초 / 30장이면 충분
4. **분석 시작** 클릭
5. 진행 상태가 단계별로 표시됨:
   - 다운로드 중 N%
   - 프레임 추출 중 (scene detection)
   - 프레임 분석 중 X/N (Claude가 한 장씩 읽는 중)
   - 완료
6. 추출된 프레임 그리드 + 분석 리포트가 표시됨. **리포트 저장** / **폴더 열기** / **복사** 버튼으로 결과 활용.

기본 출력 위치: `%USERPROFILE%\ytga-output\<video_id>\`

### CLI

소스에서 실행하는 경우:

```powershell
python -m ytga --cli "https://www.youtube.com/watch?v=VIDEO_ID" `
  --out output `
  --max-duration 600 `
  --max-frames 30 `
  --format jpg
```

옵션:
- `--max-duration N`     — 다운로드할 최대 길이 (초). `0` 이면 전체 영상.
- `--max-height N`       — 다운로드 해상도 상한. 기본 1080.
- `--min-frames N`       — 부족하면 균등 샘플로 채울 최소 프레임 수.
- `--max-frames N`       — 너무 많으면 균등 decimate할 최대 프레임 수.
- `--scene-threshold X`  — ffmpeg scene 변화 감지 임계값 (0.0~1.0, 기본 0.3).
- `--format jpg|png`     — 출력 이미지 포맷.
- `--keep-video`         — 다운로드한 mp4도 함께 보관.
- `--no-analyze`         — 프레임 추출만 하고 Claude 분석은 건너뜀.

## 빌드 (Windows .exe)

PowerShell:

```powershell
pip install -e ".[dev]"
pyinstaller build.spec
# 결과물: dist\ytga.exe
```

또는 GitHub Actions로 자동 빌드 — 태그 푸시 시 (`git tag v0.1.0 && git push --tags`) `dist/ytga.exe` 가 GitHub Release에 자동 첨부됩니다.

## 동작 원리 (간단히)

1. **다운로드** — `yt-dlp` Python 모듈로 1080p mp4 (audio merged)을 임시 폴더에 다운로드. `--max-duration` 가 켜져 있으면 `download_ranges`로 앞쪽 N초만 받음.
2. **Scene 감지** — `imageio-ffmpeg` 가 번들한 ffmpeg를 직접 호출. `select='gt(scene,0.3)',showinfo` 로 장면 변화 프레임을 뽑고 stderr의 `pts_time` 을 파싱해서 타임스탬프 매핑.
3. **균형 조정** — scene 프레임이 `max_frames` 초과면 timeline 상 균등 decimate, `min_frames` 미만이면 균등 샘플로 보강.
4. **headless Claude** — `claude -p --output-format stream-json --input-format text --allowed-tools Read --permission-mode bypassPermissions` 를 subprocess로 띄움. 프롬프트는 stdin으로 전달. 모델이 Read 툴로 이미지를 multimodal로 보고 분석.
5. **리포트** — `result` 이벤트의 텍스트를 `report.md` 로 저장, GUI는 Markdown 위젯으로 렌더.

## 한계 / 알려진 이슈

- 프레임만 보고 분석하므로, **시간 축 신호** (TAA ghosting, frame generation 흔적 등)는 정지 화면에 약하게만 잡힘. 모션이 있는 프레임이 잘 잡히도록 scene threshold 조정 필요할 수 있음.
- Stream/라이브 영상은 `--max-duration` 으로만 자르며, 일부 라이브 URL은 yt-dlp가 거절할 수 있음.
- 매우 짧은 (<10초) 영상은 scene 감지가 거의 안 잡혀서 거의 전부 균등 샘플로 보강됩니다.
- DRM 보호된 / 멤버십 전용 / 비공개 영상은 다운로드 불가.

## 라이선스

MIT. 의존성 라이선스: yt-dlp (Unlicense), FFmpeg (LGPL/GPL — imageio-ffmpeg는 LGPL 빌드 사용), Flet (Apache-2.0), Pillow (HPND), ImageHash (BSD-2), reportlab (BSD).

## Disclaimer / 사용 시 주의

이 도구는 **개인적인 학습·분석·연구 목적**으로 만들어졌습니다. 사용자는 다음을 본인 책임으로 준수해야 합니다.

- **YouTube 이용약관** — YouTube ToS는 비공식 다운로드를 일반적으로 금지합니다. 계정/IP 차단 등 계약상 제재 가능성이 있습니다.
- **저작권** — 다운로드한 영상은 사적 이용 범위 안에서만 사용하세요. 한국 저작권법 §30(사적 복제)·§35-3(공정 이용)이 분석/학습용 개인 사용은 일반적으로 허용하지만, **재배포·상업적 이용·가공 후 게시는 불법**입니다.
- **DRM** — 본 도구는 DRM 우회를 시도하지 않습니다. DRM이 적용된 영상(멤버십 전용 등)은 다운로드하지 마세요.
- **본인 콘텐츠 / 권리 보유 콘텐츠** — 가능하면 본인 영상이나 명시적 라이선스(CC 등)가 부여된 영상으로 분석하세요.

이 소프트웨어는 MIT 라이선스로 배포되며, 사용으로 인한 어떤 결과(법적 책임 포함)에 대해서도 작성자/기여자는 책임지지 않습니다 (LICENSE 파일 참조).
