# knps-eco-watcher

국립공원 생태탐방원 잔여석을 10분마다 자동 조회해서, **0에서 1개 이상으로 바뀌면 텔레그램 알림**을 보냅니다. **GitHub Actions**에서 무료로 24시간 돌아갑니다. 서버 필요 없음.

자동 예약은 하지 않습니다 (사이트에 캡차/본인인증 존재, 약관 문제). 알림 받고 사람이 직접 예약.

---

## 준비물

- GitHub 계정
- 텔레그램 봇 토큰 ([@BotFather](https://t.me/BotFather) 에서 `/newbot`)
- 본인의 텔레그램 chat_id (아래 참고)

### 내 chat_id 알아내기

1. 방금 만든 봇과 텔레그램에서 아무 메시지("hi")나 한 번 보내기
2. 웹 브라우저에서 접속:
   `https://api.telegram.org/bot<본인_토큰>/getUpdates`
3. 응답 JSON 안에서 `"chat":{"id":123456789,...}` 의 숫자 = chat_id

---

## 설치 (한 번만)

1. 이 저장소를 본인 GitHub 계정에 **public** 저장소로 생성 (예: `jay/knps-eco-watcher`)
   - 무료 Actions 시간이 무제한이려면 public 필요
   - 코드는 공개돼도 토큰은 Secrets 에 있어 노출되지 않음
2. 저장소에 이 폴더 내용을 push
3. **Settings → Secrets and variables → Actions → New repository secret** 에서 두 개 등록:
   - `TELEGRAM_BOT_TOKEN` = BotFather 토큰
   - `TELEGRAM_CHAT_ID` = 위에서 알아낸 숫자
4. **Actions 탭** 진입 → workflow 활성화 확인 → 우측 **Run workflow** 버튼으로 한 번 수동 실행 (첫 실행은 baseline 저장만 하고 알림 X)

## 감시 공원 변경

GitHub 웹에서 `config.json` 열어서 편집 → 커밋. 다음 실행부터 반영됩니다.

```json
{
  "parks": ["가야산", "북한산", "설악산"],
  "check_next_month": true,
  "quiet_hours": [0, 6]
}
```

**감시 가능한 공원 (10곳)**:
북한산, 지리산, 소백산, 계룡산, 설악산, 한려해상, 무등산, 가야산, 내장산, 변산반도

`quiet_hours`: 이 시간대(KST) 에는 알림을 안 보내고 상태만 갱신. `[0, 6]` = 자정 ~ 새벽 6시.

---

## 로컬 테스트

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# .env 만들기
cp .env.example .env
# .env 에 토큰/chat_id 입력

# 조회만 (알림/상태 저장 없음)
.venv/bin/python watcher.py --dry-run

# 테스트 알림 1건 (파싱과 무관)
.venv/bin/python watcher.py --test-notify

# 실제 실행 (state.json 갱신)
.venv/bin/python watcher.py
```

---

## 동작 원리

- `config.json` 의 공원마다 이번 달(+옵션: 다음 달) 조회
- 각 공원의 (날짜 → 잔여수) 를 `state.json` 과 비교
- **이전값이 0인 날짜가 이번에 1+ 로 바뀌면** 텔레그램 메시지 1건 발송 (여러 공원/날짜는 하나로 묶음)
- 새 상태를 `state.json` 에 저장, GitHub Actions 가 자동 커밋

### 알림 예시

```
🏕 가야산 생태탐방원 빈자리!

  • 8/15(금) 생활관 잔여 2개
  • 8/16(토) 생활관 잔여 1개

예약 → https://reservation.knps.or.kr/eco/searchEcoReservation.do
```

---

## 알아둘 제약

- **GitHub Actions cron 은 실제로 몇 분 지연될 수 있음.** 초 단위 취소표 잡기는 무리, "10~20분 안에 알게 됨" 수준
- **60일간 저장소 활동이 없으면** Actions 스케줄이 자동 비활성화 (state 커밋이 계속 쌓이므로 대체로 유지됨). 안내 메일 오면 Actions 탭에서 다시 활성화
- **첫 실행은 알림 없이 baseline 만 저장** — 폭탄 방지
- **KNPS 사이트에 NetFunnel 대기열이 있음** — 공원 간 요청 사이 2.5초 간격

---

## 파일 구조

```
knps-eco-watcher/
├── .github/workflows/watch.yml   ← 10분마다 실행하는 Actions
├── watcher.py                    ← 조회/파싱/알림/상태
├── config.json                   ← 감시 공원 목록
├── state.json                    ← 이전 잔여 상태 (자동 커밋됨)
├── requirements.txt              ← requests, beautifulsoup4
├── .env.example                  ← 로컬 테스트용 템플릿
└── .gitignore
```
