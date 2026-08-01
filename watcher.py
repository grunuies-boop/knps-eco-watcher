"""국립공원 생태탐방원 잔여석 감시 (GitHub Actions 스케줄용).

- config.json 의 감시 공원 목록을 순회
- 이번 달(+옵션: 다음 달) 페이지 조회 → 날짜별 잔여 파싱
- state.json 과 비교하여 0 → 1+ 전환 감지 → 텔레그램 알림
- 새 상태를 state.json 에 기록 (GitHub Actions 가 커밋)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
ENV_PATH = ROOT / ".env"

URL = "https://reservation.knps.or.kr/eco/searchEcoMonthReservation.do"
REFERER = "https://reservation.knps.or.kr/eco/searchEcoReservation.do"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

KST = timezone(timedelta(hours=9))

# 공원 한글명 → deptId
PARK_CODES: dict[str, str] = {
    "북한산": "B971002",
    "지리산": "B014003",
    "소백산": "B123002",
    "계룡산": "B163001",
    "설악산": "B301002",
    "한려해상": "B024002",
    "무등산": "B231002",
    "가야산": "B133002",
    "내장산": "B331001",
    "변산반도": "B183001",
}

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

CONSECUTIVE_FAILURE_ALERT_THRESHOLD = 6  # 10분 × 6 = 1시간
INTER_PARK_DELAY_SECONDS = 2.5           # 공원 간 요청 간격 (NetFunnel 회피)
FETCH_TIMEOUT_SECONDS = 15
FETCH_RETRIES = 2
FETCH_RETRY_DELAY_SECONDS = 5

WEEKDAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]

log = logging.getLogger("watcher")


# ────────────────────────────────────────────────────────────────
# Env loading (minimal .env parser to avoid python-dotenv dep)
# ────────────────────────────────────────────────────────────────

def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


# ────────────────────────────────────────────────────────────────
# HTTP + parsing
# ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DailyAvailability:
    date: str        # ISO YYYY-MM-DD
    remaining: int


def _parse_html(html: str) -> list[DailyAvailability]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[DailyAvailability] = []
    for cell in soup.select("div.calendar-cell[data-usedt]"):
        d = cell.get("data-usedt")
        if not d:
            continue
        em = cell.select_one("ul.contents em")
        try:
            remaining = int(em.get_text(strip=True)) if em else 0
        except ValueError:
            remaining = 0
        out.append(DailyAvailability(date=d, remaining=remaining))
    return out


def _fetch_with_retries(*, method: str, url: str, **kwargs) -> requests.Response | None:
    kwargs.setdefault("timeout", FETCH_TIMEOUT_SECONDS)
    kwargs.setdefault("headers", {}).setdefault("User-Agent", USER_AGENT)
    for attempt in range(FETCH_RETRIES + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as e:
            if attempt < FETCH_RETRIES:
                log.warning("fetch %s failed (attempt %d): %s — retrying", url, attempt + 1, e)
                time.sleep(FETCH_RETRY_DELAY_SECONDS)
            else:
                log.error("fetch %s failed after %d attempts: %s", url, attempt + 1, e)
    return None


def fetch_current_month(park_code: str) -> list[DailyAvailability] | None:
    """이번 달: GET 방식으로 조회 (설계서 확인)."""
    r = _fetch_with_retries(
        method="GET", url=URL, params={"deptId": park_code}
    )
    return _parse_html(r.text) if r is not None else None


def fetch_next_month(park_code: str) -> list[DailyAvailability] | None:
    """다음 달: POST 폼 전송. 실패해도 전체 실행이 죽지 않도록 별도 함수."""
    now_kst = datetime.now(KST)
    y, m = now_kst.year, now_kst.month
    m += 1
    if m > 12:
        m = 1
        y += 1
    r = _fetch_with_retries(
        method="POST",
        url=URL,
        data={"deptId": park_code, "year": str(y), "month": f"{m:02d}", "ctgType": "01"},
        headers={"User-Agent": USER_AGENT, "Referer": REFERER},
    )
    return _parse_html(r.text) if r is not None else None


# ────────────────────────────────────────────────────────────────
# State
# ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text() or "{}")
    except json.JSONDecodeError:
        log.warning("state.json 파싱 실패 — 빈 상태로 시작")
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def prune_past_dates(park_map: dict[str, int], today_iso: str) -> dict[str, int]:
    return {d: r for d, r in park_map.items() if d >= today_iso}


# ────────────────────────────────────────────────────────────────
# Notification
# ────────────────────────────────────────────────────────────────

def _md_format_date(iso: str) -> str:
    y, m, d = iso.split("-")
    dt = date(int(y), int(m), int(d))
    return f"{int(m)}/{int(d)}({WEEKDAYS_KO[dt.weekday()]})"


def format_alert(new_openings: dict[str, list[tuple[str, int]]]) -> str:
    """공원별 신규 잔여를 하나의 메시지로 묶는다."""
    lines: list[str] = []
    for park, items in new_openings.items():
        lines.append(f"🏕 *{park} 생태탐방원 빈자리!*")
        lines.append("")
        for iso, remaining in items:
            lines.append(f"  • {_md_format_date(iso)} 생활관 잔여 {remaining}개")
        lines.append("")
    lines.append("예약 → https://reservation.knps.or.kr/eco/searchEcoReservation.do")
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    try:
        r = requests.post(
            TELEGRAM_API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=FETCH_TIMEOUT_SECONDS,
        )
        r.raise_for_status()
        return True
    except Exception:
        log.exception("텔레그램 전송 실패")
        return False


# ────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────

@dataclass
class Config:
    parks: list[str]
    check_next_month: bool
    quiet_hours: tuple[int, int]


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        log.error("config.json 이 없습니다.")
        sys.exit(2)
    raw = json.loads(CONFIG_PATH.read_text())
    parks = raw.get("parks", [])
    if not isinstance(parks, list) or not parks:
        log.error("config.json 의 parks 가 비어있습니다.")
        sys.exit(2)
    unknown = [p for p in parks if p not in PARK_CODES]
    if unknown:
        log.error("알 수 없는 공원: %s (사용 가능: %s)", unknown, list(PARK_CODES))
        sys.exit(2)
    qh = raw.get("quiet_hours", [0, 6])
    return Config(
        parks=parks,
        check_next_month=bool(raw.get("check_next_month", True)),
        quiet_hours=(int(qh[0]), int(qh[1])),
    )


def is_quiet_now(quiet_hours: tuple[int, int]) -> bool:
    start, end = quiet_hours
    hour = datetime.now(KST).hour
    if start <= end:
        return start <= hour < end
    # 자정 넘어가는 구간 (예: 22, 6)
    return hour >= start or hour < end


# ────────────────────────────────────────────────────────────────
# Main pipeline
# ────────────────────────────────────────────────────────────────

def collect_transitions(
    park: str, days: list[DailyAvailability], prev: dict[str, int]
) -> list[tuple[str, int]]:
    """0 → 1+ 로 전환된 (date, remaining) 목록."""
    transitions: list[tuple[str, int]] = []
    for d in days:
        old = prev.get(d.date)
        if old is not None and old == 0 and d.remaining > 0:
            transitions.append((d.date, d.remaining))
    return transitions


def merge_availability(
    park: str, days: list[DailyAvailability], state: dict, today_iso: str
) -> dict[str, int]:
    """이전 상태에 이번 조회 결과를 병합. 지난 날짜는 정리."""
    prev = state.get(park, {})
    updated = {d: r for d, r in prev.items() if d >= today_iso}
    for d in days:
        if d.date >= today_iso:
            updated[d.date] = d.remaining
    return updated


def run(dry_run: bool = False) -> int:
    load_env_file(ENV_PATH)
    cfg = load_config()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not dry_run and (not token or not chat_id):
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 설정되지 않았습니다.")
        return 2

    state = load_state()
    is_first_run = not any(k for k in state.keys() if not k.startswith("_"))
    quiet = is_quiet_now(cfg.quiet_hours)
    today_iso = datetime.now(KST).date().isoformat()

    log.info("run start (dry_run=%s first_run=%s quiet=%s today=%s parks=%s)",
             dry_run, is_first_run, quiet, today_iso, cfg.parks)

    all_transitions: dict[str, list[tuple[str, int]]] = {}
    any_fetch_failed = False

    for i, park in enumerate(cfg.parks):
        if i > 0:
            time.sleep(INTER_PARK_DELAY_SECONDS)

        code = PARK_CODES[park]
        prev = {d: r for d, r in state.get(park, {}).items() if d >= today_iso}

        days_cur = fetch_current_month(code)
        if days_cur is None:
            any_fetch_failed = True
            log.warning("[%s] 이번 달 조회 실패 — 이 공원 스킵", park)
            continue

        days_all = list(days_cur)
        if cfg.check_next_month:
            days_nxt = fetch_next_month(code)
            if days_nxt is None:
                log.warning("[%s] 다음 달 조회 실패 (계속 진행)", park)
            else:
                days_all.extend(days_nxt)

        transitions = collect_transitions(park, days_all, prev) if not is_first_run else []
        state[park] = merge_availability(park, days_all, state, today_iso)

        avail_days = [d for d in days_all if d.remaining > 0]
        log.info("[%s] fetched=%d avail>0=%d transitions=%d",
                 park, len(days_all), len(avail_days), len(transitions))
        if transitions:
            all_transitions[park] = transitions

    # 연속 실패 카운터
    fail_count = int(state.get("_consecutive_failures", 0))
    if any_fetch_failed:
        fail_count += 1
    else:
        fail_count = 0
    state["_consecutive_failures"] = fail_count
    state["_consecutive_failures_notified"] = state.get("_consecutive_failures_notified", False)
    state["_last_run"] = datetime.now(KST).isoformat(timespec="seconds")

    # 알림
    if dry_run:
        if all_transitions:
            print("=== DRY RUN: 알림 예정 메시지 ===")
            print(format_alert(all_transitions))
        else:
            print("=== DRY RUN: 전환 없음 ===")
        for park in cfg.parks:
            for d, r in sorted(state.get(park, {}).items()):
                print(f"  {park} {d} = {r}")
    else:
        if is_first_run:
            log.info("첫 실행 — baseline 만 저장, 알림 스킵")
        elif quiet and all_transitions:
            log.info("quiet_hours 중 — 알림 스킵, 상태만 갱신")
        elif all_transitions:
            msg = format_alert(all_transitions)
            if send_telegram(token, chat_id, msg):
                log.info("알림 전송 완료 (parks=%s)", list(all_transitions.keys()))
            else:
                log.error("알림 전송 실패 (상태는 갱신됨)")

        # 연속 실패 알림 (1회만)
        if (
            fail_count >= CONSECUTIVE_FAILURE_ALERT_THRESHOLD
            and not state["_consecutive_failures_notified"]
        ):
            send_telegram(
                token,
                chat_id,
                "⚠️ *조회가 계속 실패하고 있습니다.*\n"
                "사이트 구조가 바뀌었을 수 있어요.",
            )
            state["_consecutive_failures_notified"] = True
        elif fail_count == 0:
            state["_consecutive_failures_notified"] = False

    if not dry_run:
        save_state(state)
    return 0


# ────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(description="KNPS 생태탐방원 잔여 감시")
    parser.add_argument("--dry-run", action="store_true",
                        help="조회만 출력, 알림/상태 저장 없음")
    parser.add_argument("--test-notify", action="store_true",
                        help="파싱과 무관하게 텔레그램 테스트 메시지 1건만 전송")
    args = parser.parse_args()

    if args.test_notify:
        load_env_file(ENV_PATH)
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 필요")
            return 2
        ok = send_telegram(token, chat_id, "✅ knps-eco-watcher 테스트 알림입니다.")
        return 0 if ok else 1

    try:
        return run(dry_run=args.dry_run)
    except SystemExit:
        raise
    except Exception:
        log.exception("예상치 못한 오류 — 정상 종료")
        return 0  # cron 실패 알림 방지


if __name__ == "__main__":
    sys.exit(main())
