# app.py  (Single-file MVP: "하루 한 장면")
# pip install streamlit pydantic openai
# run: streamlit run app.py

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import streamlit as st
from pydantic import BaseModel, Field, field_validator, model_validator


# =========================
# Utils
# =========================

def today_str() -> str:
    return date.today().isoformat()

def clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

def make_mission_id(date_yyyy_mm_dd: str, seed: str) -> str:
    yyyymmdd = date_yyyy_mm_dd.replace("-", "")
    h = hashlib.sha1(f"{date_yyyy_mm_dd}:{seed}".encode("utf-8")).hexdigest()[:8]
    return f"{yyyymmdd}-{h}"

def _confetti_svg_bytes() -> bytes:
    # 간단한 축하용 SVG (로컬/오프라인 가능)
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" width="900" height="360" viewBox="0 0 900 360">
      <rect width="900" height="360" fill="#ffffff"/>
      <text x="50%" y="42%" text-anchor="middle" font-size="44" font-family="sans-serif">🎉</text>
      <text x="50%" y="60%" text-anchor="middle" font-size="30" font-family="sans-serif">오늘의 장면 저장 완료!</text>
      <g opacity="0.85">
        <circle cx="90" cy="70" r="8" fill="#ff6b6b"/>
        <circle cx="160" cy="120" r="6" fill="#ffd93d"/>
        <circle cx="240" cy="80" r="7" fill="#6bcB77"/>
        <circle cx="320" cy="140" r="5" fill="#4d96ff"/>
        <circle cx="420" cy="90" r="9" fill="#c77dff"/>
        <circle cx="520" cy="140" r="6" fill="#ff6b6b"/>
        <circle cx="620" cy="85" r="7" fill="#ffd93d"/>
        <circle cx="720" cy="125" r="6" fill="#6bcB77"/>
        <circle cx="820" cy="95" r="8" fill="#4d96ff"/>
      </g>
    </svg>
    """.strip()
    return svg.encode("utf-8")


# =========================
# Schema (Pydantic)
# =========================

TwistType = Literal[
    "perspective",
    "constraint",
    "roleplay",
    "sensory",
    "social",
    "artifact",
    "micro_adventure",
]
IndoorOutdoor = Literal["indoor", "outdoor", "either"]
CostLevel = Literal["free", "low", "any"]


class Option(BaseModel):
    label: Literal["Option A", "Option B"]
    variation: str = Field(min_length=2, max_length=60)
    delta: str = Field(min_length=2, max_length=120)  # 옵션 창의성 위해 조금 넉넉히


class Twist(BaseModel):
    type: TwistType
    surprise_point: str = Field(min_length=3, max_length=120)
    rule: str = Field(min_length=3, max_length=140)


class Mission(BaseModel):
    id: str = Field(min_length=8, max_length=40)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    title: str = Field(min_length=2, max_length=20)
    one_liner: str = Field(min_length=5, max_length=120)
    why_this_is_you: str = Field(min_length=8, max_length=220)

    time_minutes: int = Field(ge=10, le=40)
    difficulty: int = Field(ge=1, le=5)

    indoor_outdoor: IndoorOutdoor
    cost_level: CostLevel

    materials: List[str] = Field(default_factory=list, max_length=3)
    steps: List[str] = Field(min_length=3, max_length=5)

    twist: Twist
    options: List[Option] = Field(min_length=2, max_length=2)

    safety_notes: List[str] = Field(min_length=1, max_length=3)
    tags: List[str] = Field(min_length=3, max_length=5)

    @field_validator("materials")
    @classmethod
    def materials_len(cls, v: List[str]) -> List[str]:
        if len(v) > 3:
            raise ValueError("materials must be 0~3")
        return [x.strip() for x in v if x and x.strip()]

    @field_validator("steps")
    @classmethod
    def steps_clean(cls, v: List[str]) -> List[str]:
        vv = [s.strip() for s in v if s and s.strip()]
        if not (3 <= len(vv) <= 5):
            raise ValueError("steps must be 3~5")
        return vv

    @field_validator("tags")
    @classmethod
    def tags_clean(cls, v: List[str]) -> List[str]:
        vv = [t.strip().lstrip("#") for t in v if t and t.strip()]
        if not (3 <= len(vv) <= 5):
            raise ValueError("tags must be 3~5")
        for t in vv:
            if len(t) > 18:
                raise ValueError("tag too long")
        return vv

    @model_validator(mode="after")
    def options_must_be_ab(self) -> "Mission":
        labels = [o.label for o in self.options]
        if labels != ["Option A", "Option B"]:
            raise ValueError("options must be exactly [Option A, Option B] in order")
        if self.options[0].variation.strip() == self.options[1].variation.strip():
            raise ValueError("options variations must differ")
        if self.options[0].delta.strip() == self.options[1].delta.strip():
            raise ValueError("options delta must differ")
        return self


class UserProfile(BaseModel):
    time_available: int = Field(default=20, ge=5, le=240)
    energy: Literal["low", "medium", "high"] = "medium"
    preference_place: Literal["indoor", "outdoor", "any"] = "any"

    companion: Optional[Literal["solo", "with"]] = None
    budget: Optional[Literal["free", "low", "any"]] = None
    risk_appetite: int = Field(default=2, ge=0, le=5)
    dislike: Optional[str] = Field(default=None, max_length=40)


class RecentMissionLog(BaseModel):
    date: str
    title: str
    tags: List[str] = Field(default_factory=list)
    completed: bool = False
    satisfaction: Optional[int] = Field(default=None, ge=1, le=5)
    chosen_option: Optional[Literal["Option A", "Option B"]] = None
    keyword_notes: List[str] = Field(default_factory=list)


# =========================
# Safety
# =========================

BANNED_REASON = "contains risky/illegal/self-harm/harassment/driving/alcohol/privacy keywords"
BANNED_KEYWORDS = [
    "자해", "자살", "죽고", "죽자", "상처", "피", "목매", "약을", "과다복용",
    "칼로", "베어", "불로", "화재", "폭발", "폭죽",
    "불법", "도박", "훔치", "협박", "스토킹", "몰카", "해킹", "사기",
    "남을 괴롭", "민폐", "복수",
    "주민등록", "계좌번호", "비밀번호", "신용카드", "전화번호 수집",
    "운전 중", "주행 중", "음주", "술 마시", "취한", "폭음",
]

def contains_banned_content(m: Mission) -> Optional[str]:
    parts: List[str] = []
    parts += [m.title, m.one_liner, m.why_this_is_you]
    parts += m.materials + m.steps
    parts += [m.twist.surprise_point, m.twist.rule]
    for o in m.options:
        parts += [o.variation, o.delta]
    parts += m.safety_notes + m.tags

    text = "\n".join([p for p in parts if p]).lower()
    for kw in BANNED_KEYWORDS:
        if kw.lower() in text:
            return kw
    return None


# =========================
# Novelty (avoid repeats)
# =========================

def _norm_tag(t: str) -> str:
    return t.strip().lstrip("#").lower()

def _tokenize_title(s: str) -> set[str]:
    s = re.sub(r"[^\w\s가-힣]", " ", s.lower())
    return {t for t in s.split() if len(t) >= 2}

def novelty_check(mission: Mission, recent_logs: List[RecentMissionLog]) -> Tuple[bool, str]:
    last7 = recent_logs[:7]
    if not last7:
        return True, "no recent logs"

    m_tags = {_norm_tag(t) for t in mission.tags}
    m_title_toks = _tokenize_title(mission.title)

    for r in last7:
        r_tags = {_norm_tag(t) for t in r.tags}
        if r_tags:
            overlap = len(m_tags & r_tags) / max(1, len(m_tags))
            if overlap >= 0.60:
                return False, f"tag overlap too high with {r.date}"

        r_title_toks = _tokenize_title(r.title)
        if r_title_toks and m_title_toks:
            jacc = len(m_title_toks & r_title_toks) / max(1, len(m_title_toks | r_title_toks))
            if jacc >= 0.60:
                return False, f"title similarity too high with {r.date}"

    return True, "ok"


# =========================
# Validator (specialness rules)
# =========================

VAGUE_PATTERNS = [
    r"^명상(하기|해보기)?$",
    r"^운동(하기|해보기)?$",
    r"^산책(하기|해보기)?$",
    r"^독서(하기|해보기)?$",
    r"^정리(하기|해보기)?$",
    r"^기록(하기|해보기)?$",
]

CONCRETE_HINT = re.compile(
    r"(\d+\s*(분|초|번|개))|"
    r"(메모|사진|걷|적|그려|듣|냄새|촉감|타이머|알림|지도|동선|창문|책상|컵|물)",
    re.UNICODE,
)

@dataclass
class ValidationResult:
    ok: bool
    errors: List[str]

def _step_is_too_vague(step: str) -> bool:
    s = step.strip()
    if len(s) < 8:
        return True
    for pat in VAGUE_PATTERNS:
        if re.match(pat, s):
            return True
    if not CONCRETE_HINT.search(s):
        return True
    return False

def _twist_linked(m: Mission) -> bool:
    rule = (m.twist.rule or "").strip()
    if not rule:
        return False
    tokens = [t.strip() for t in re.split(r"[\s,./]+", rule) if len(t.strip()) >= 2]
    if not tokens:
        return False
    for stp in m.steps:
        if any(tok in stp for tok in tokens[:6]):
            return True
    return False

def _personalization_present(m: Mission, profile: UserProfile, recent_logs: List[RecentMissionLog]) -> bool:
    txt = m.why_this_is_you
    signals = [
        str(profile.time_available),
        profile.energy,
        profile.preference_place,
        "시간", "에너지", "실내", "실외", "최근", "지난",
    ]
    if any(sig in txt for sig in signals):
        return True

    recent_tags = set()
    for r in recent_logs[:7]:
        for t in r.tags:
            recent_tags.add(t.strip().lstrip("#"))
    if any(t in txt for t in list(recent_tags)[:8]):
        return True
    return False

def validate_mission(mission: Mission, profile: UserProfile, recent_logs: Optional[List[RecentMissionLog]] = None) -> ValidationResult:
    recent_logs = recent_logs or []
    errors: List[str] = []

    # (1) 실행가능성
    if mission.time_minutes > profile.time_available:
        errors.append("time_minutes exceeds time_available")
    if not (3 <= len(mission.steps) <= 5):
        errors.append("steps count must be 3~5")
    if len(mission.materials) > 3:
        errors.append("materials must be <= 3")
    for i, stp in enumerate(mission.steps, start=1):
        if _step_is_too_vague(stp):
            errors.append(f"step {i} too vague")

    # (2) twist 강제 + steps 연결 + novelty
    if not mission.twist.surprise_point.strip():
        errors.append("twist.surprise_point required")
    if not mission.twist.rule.strip():
        errors.append("twist.rule required")
    if not _twist_linked(mission):
        errors.append("twist.rule not linked to steps")

    nov_ok, nov_reason = novelty_check(mission, recent_logs)
    if not nov_ok:
        errors.append(f"novelty failed: {nov_reason}")

    # (3) 안전
    banned = contains_banned_content(mission)
    if banned:
        errors.append(f"safety failed: {BANNED_REASON} ({banned})")

    # (4) 개인화 체감
    if not _personalization_present(mission, profile, recent_logs):
        errors.append("why_this_is_you lacks grounding in inputs/logs")

    # 옵션 차이
    a, b = mission.options[0], mission.options[1]
    if a.variation.strip() == b.variation.strip():
        errors.append("options A/B variation identical")
    if a.delta.strip() == b.delta.strip():
        errors.append("options A/B delta identical")

    return ValidationResult(ok=(len(errors) == 0), errors=errors)


# =========================
# Fallback templates (5)
# =========================

@dataclass
class Template:
    key: str
    base_title: str
    base_one_liner: str
    twist_type: TwistType
    indoor_outdoor: IndoorOutdoor
    cost_level: CostLevel
    base_tags: List[str]

TEMPLATES: List[Template] = [
    Template("sensory_window", "창문 감각 스캔", "바깥을 ‘처음 보는 듯’ 다시 읽어보자.", "sensory", "indoor", "free",
             ["감각", "관찰", "짧은기록", "새로움"]),
    Template("reverse_route", "반대 동선 10분", "익숙한 길을 뒤집으면 하루가 살짝 흔들린다.", "micro_adventure", "outdoor", "free",
             ["산책", "동선", "미니모험", "새로움"]),
    Template("roleplay_curator", "나만의 큐레이터", "오늘의 나를 전시한다면, 뭐를 붙일까?", "roleplay", "either", "free",
             ["큐레이션", "메모", "자기이해", "아티팩트"]),
    Template("constraint_one_hand", "한 손 규칙 15분", "제약 하나가 평범한 일을 ‘게임’으로 바꾼다.", "constraint", "either", "free",
             ["제약", "게임화", "집중", "실행"]),
    Template("artifact_postcard", "하루 엽서 한 장", "오늘을 ‘한 장’으로 남기면 내일이 달라진다.", "artifact", "either", "low",
             ["기록", "엽서", "손글씨", "작은선물"]),
]

def _pick_template(profile: UserProfile) -> Template:
    if profile.preference_place == "indoor":
        return TEMPLATES[0] if profile.energy == "low" else TEMPLATES[2]
    if profile.preference_place == "outdoor":
        return TEMPLATES[1]
    if profile.energy == "high" and profile.time_available >= 25:
        return TEMPLATES[1]
    if profile.energy == "low":
        return TEMPLATES[0]
    return TEMPLATES[3]

def _difficulty(profile: UserProfile) -> int:
    base = {"low": 1, "medium": 3, "high": 4}[profile.energy]
    base += 1 if profile.risk_appetite >= 4 else 0
    return clamp(base, 1, 5)

def build_fallback_mission(profile: UserProfile, recent_logs: List[RecentMissionLog]) -> Mission:
    d = today_str()
    tpl = _pick_template(profile)

    time_minutes = clamp(profile.time_available, 10, 40)
    if profile.energy == "low":
        time_minutes = min(time_minutes, 20)

    place: IndoorOutdoor = tpl.indoor_outdoor
    if profile.preference_place in ("indoor", "outdoor"):
        place = profile.preference_place  # type: ignore

    cost_level: CostLevel = profile.budget if profile.budget in ("free", "low", "any") else tpl.cost_level  # type: ignore

    dislike_note = f" (피하고 싶은 것: {profile.dislike})" if profile.dislike else ""

    if tpl.key == "sensory_window":
        twist_rule = "창문/문턱을 ‘첫 방문’처럼 5분 관찰하고, 보이는 것 7개를 서로 다른 감각 단어로 적기"
        steps = [
            "타이머 10~20분을 켜고 창문(또는 현관문 앞)으로 간다.",
            "5분 동안 ‘처음 보는 장소’처럼 천천히 훑으며 눈에 띄는 것 7개를 찾는다.",
            "각 항목을 서로 다른 감각 단어(빛/소리/온도/질감/냄새/거리감 등)로 1줄씩 적는다.",
            "마지막 2분: 그중 1개를 골라 ‘왜 오늘 이게 보였을까?’를 한 문장으로 덧붙인다.",
        ]
        materials = ["메모 앱 또는 종이"]
    elif tpl.key == "reverse_route":
        twist_rule = "평소 동선의 ‘정반대 방향’으로 10분 걷고, 돌아올 때는 ‘가장 밝은 길’만 선택하기"
        steps = [
            "밖으로 나가(또는 건물 안 복도라도) 평소 가는 방향의 ‘반대’로 걷기 시작한다.",
            "10분 동안 ‘처음 보는 표지/가게/나무/소리’ 3개를 발견하면 메모한다.",
            "돌아오는 길은 규칙대로 ‘가장 밝아 보이는 길’만 고른다(안전 최우선).",
            "도착 후 2분: 발견 3개 중 1개를 제목처럼 6~10자로 적는다.",
        ]
        materials = ["메모 앱"]
    elif tpl.key == "roleplay_curator":
        twist_rule = "‘오늘의 나’ 전시 큐레이터가 되어, 사소한 물건 3개에 라벨(제목+설명)을 붙이기"
        steps = [
            "주변에서 사소한 물건 3개(컵/펜/영수증 등)를 골라 한 곳에 모은다.",
            "각 물건에 전시 라벨을 만든다: 제목(8~12자) + 설명 1문장.",
            "설명 문장에는 ‘오늘의 기분/에너지’ 단서를 1개 넣는다.",
            "마지막 3분: 3개 중 ‘오늘을 대표하는 1개’를 고르고 이유를 한 문장으로 적는다.",
        ]
        materials = ["종이/메모 앱"]
    elif tpl.key == "constraint_one_hand":
        twist_rule = "15분 동안 ‘한 손만’ 사용해서 평소 작업 1개를 수행하기(안전/무리 금지)"
        steps = [
            "평소 하는 작은 작업 1개를 고른다(정리/설거지/메일정리/책상정돈 등).",
            "타이머 15분을 켜고 ‘한 손 규칙’을 적용해 수행한다(무리/위험 작업 금지).",
            "중간에 1번 멈춰서: ‘불편함이 만든 새 방법’이 있었는지 메모한다.",
            "끝나면 2분: 오늘 배운 요령을 1줄 ‘사용설명서’로 적는다.",
        ]
        materials = ["타이머"]
    else:
        twist_rule = "오늘을 ‘엽서 한 장’처럼: 앞면(한 문장) + 뒷면(3줄)로 남기기"
        steps = [
            "종이(또는 메모 앱)를 엽서처럼 ‘앞/뒤’로 나눌 준비를 한다.",
            "앞면: 오늘을 특별하게 만든 단서 하나를 1문장으로 쓴다.",
            "뒷면: (1) 내가 한 것 (2) 느낀 것 (3) 내일의 작은 힌트 — 3줄로 적는다.",
            "가능하면 사진 1장(또는 스케치 10초)로 ‘우표’처럼 붙인다.",
        ]
        materials = ["종이/메모 앱"]

    why = (
        f"오늘은 {profile.time_available}분 / 에너지 {profile.energy} / 장소 {profile.preference_place} 기준으로 "
        f"준비물 최소 + 바로 실행 가능한 장면을 골랐어.{dislike_note}"
    )

    # ✅ 옵션 창의성 강화: A는 “영화 예고편 컷”, B는 “감독판”
    # (클릭하지 않아도 내용이 보이도록 UI에서 항상 표시)
    option_a = Option(
        label="Option A",
        variation="예고편 컷(가볍게)",
        delta="핵심만 ‘3-2-1’로: 관찰/기록 대상을 3개로, 문장은 2줄로, 마지막 1줄만 남기고 종료",
    )
    option_b = Option(
        label="Option B",
        variation="감독판(조금 더 특별하게)",
        delta="규칙에 ‘의외의 렌즈’ 1개 추가: 예) 반대 감정 단어로 묘사하거나, 가장 사소한 1개를 주인공처럼 20초 스토리로 써보기",
    )

    m = Mission(
        id=make_mission_id(d, tpl.base_title),
        date=d,
        title=tpl.base_title,
        one_liner=tpl.base_one_liner,
        why_this_is_you=why,
        time_minutes=time_minutes,
        difficulty=_difficulty(profile),
        indoor_outdoor=place,
        cost_level=cost_level,
        materials=materials[:3],
        steps=steps[:5],
        twist=Twist(
            type=tpl.twist_type,
            surprise_point="평범한 환경/행동에 ‘규칙 하나’를 끼워 넣어, 같은 하루를 다른 장면처럼 느끼게 만든다.",
            rule=twist_rule,
        ),
        options=[option_a, option_b],
        safety_notes=[
            "무리한 신체 행동/위험한 장소/운전 중 수행은 금지",
            "불편함이 통증으로 느껴지면 즉시 중단",
        ][:3],
        tags=tpl.base_tags[:5],
    )
    return m


# =========================
# LLM (optional) - sidebar key input
# =========================

class LLMError(Exception):
    pass

@dataclass
class LLMResponse:
    raw_text: str
    data: Dict[str, Any]

class OpenAIChatClient:
    def __init__(self, api_key: str, model: str = "gpt-4.1-mini"):
        if not api_key or not api_key.strip():
            raise LLMError("api_key is empty")
        try:
            from openai import OpenAI  # type: ignore
        except Exception as e:
            raise LLMError("openai package not installed") from e
        self._OpenAI = OpenAI
        self.api_key = api_key.strip()
        self.model = model

    def generate_mission_json(self, system_prompt: str, user_prompt: str) -> LLMResponse:
        client = self._OpenAI(api_key=self.api_key)
        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.8,
                response_format={"type": "json_object"},
            )
            text = resp.choices[0].message.content or ""
        except Exception as e:
            raise LLMError(f"OpenAI call failed: {e}") from e

        try:
            data = json.loads(text)
        except Exception as e:
            raise LLMError("Model did not return valid JSON") from e

        return LLMResponse(raw_text=text, data=data)


def summarize_recent_logs(recent_logs: List[RecentMissionLog]) -> str:
    last7 = recent_logs[:7]
    if not last7:
        return "최근 7일 기록 없음."
    completed = sum(1 for r in last7 if r.completed)
    sat = [r.satisfaction for r in last7 if r.satisfaction is not None]
    avg_sat = round(sum(sat) / len(sat), 2) if sat else None

    tag_counts: Dict[str, int] = {}
    for r in last7:
        for t in r.tags:
            tt = t.strip().lstrip("#")
            tag_counts[tt] = tag_counts.get(tt, 0) + 1
    top_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:8]
    top_tags_str = ", ".join([f"{t}({c})" for t, c in top_tags]) if top_tags else "없음"

    avoided: List[str] = []
    for r in last7:
        avoided += r.keyword_notes
    avoided = [a.strip() for a in avoided if a and a.strip()][:6]

    return (
        f"- 완료율: {completed}/7\n"
        f"- 평균 만족도: {avg_sat if avg_sat is not None else 'N/A'}\n"
        f"- 자주 등장한 태그: {top_tags_str}\n"
        f"- 메모/피한 키워드(있다면): {', '.join(avoided) if avoided else '없음'}\n"
    )

def build_user_prompt(profile: UserProfile, recent_logs: List[RecentMissionLog]) -> str:
    recent_summary = summarize_recent_logs(recent_logs)
    dislike = profile.dislike or "없음"
    return f"""
너는 '하루를 사용자가 예상치 못한 방식으로 조금 특별하게 만드는' 미션 생성기다.
반드시 아래 Mission JSON 단일 객체만 출력해. 다른 텍스트는 절대 출력하지 마.

[사용자 입력(필수 3개)]
- time_available: {profile.time_available} (분)
- energy: {profile.energy} (low/medium/high)
- preference_place: {profile.preference_place} (indoor/outdoor/any)

[선택 입력]
- companion: {profile.companion or '미지정'}
- budget: {profile.budget or '미지정'}
- risk_appetite(0~5): {profile.risk_appetite}
- dislike(싫어하는 것 1개): {dislike}

[최근 7일 로그 요약]
{recent_summary}

[강제 규칙]
1) 출력은 Mission JSON 스키마를 정확히 따른다.
2) time_minutes는 10~40이며, 반드시 time_available을 넘지 않게(옵션A 포함) 설계한다.
3) steps는 3~5단계. 각 단계는 구체적 행동(시간/횟수/대상 포함)을 넣는다.
4) twist를 반드시 포함:
   - twist.type은 다음 중 하나: perspective|constraint|roleplay|sensory|social|artifact|micro_adventure
   - twist.rule, twist.surprise_point는 비어있으면 안 됨
   - steps 중 최소 1개는 twist.rule을 직접 실행하도록 연결
5) 안전: 불법/위험/자해/타인 괴롭힘/운전 중 행동/음주 권장/개인정보 과도 수집은 금지.
   safety_notes에 금지/주의 1~3개를 포함한다.
6) 개인화 체감:
   - why_this_is_you는 반드시 사용자 입력(시간/에너지/장소) 또는 최근 기록 중 1개 이상을 근거로 1~2문장 작성
   - options는 Option A/B 2개.
     A는 부담 낮고 깔끔한 버전(예: '예고편 컷'), B는 더 창의적인 버전(예: '감독판').
     무엇이 달라지는지(delta) 구체적으로.

[중복 방지]
최근 7일의 title/tags와 60% 이상 겹치지 않도록 새로움을 확보한다.

이제 Mission JSON 단일 객체만 출력해.
""".strip()

SYSTEM_PROMPT = "너는 안전하고 실행 가능한 '하루를 특별하게 만드는' 미션 생성 엔진이다. 절대 JSON 이외의 텍스트를 출력하지 마."


@dataclass
class GenerationMeta:
    used_fallback: bool
    errors: List[str]

def _postprocess_mission_dict(d: dict, profile: UserProfile) -> dict:
    if "date" not in d or not d["date"]:
        d["date"] = today_str()
    if "id" not in d or not d["id"]:
        d["id"] = make_mission_id(d["date"], d.get("title", "mission"))

    tm = int(d.get("time_minutes", profile.time_available))
    tm = clamp(tm, 10, 40)
    tm = min(tm, profile.time_available)
    d["time_minutes"] = tm

    if "title" in d and isinstance(d["title"], str):
        d["title"] = d["title"].strip()[:20]

    mats = d.get("materials", [])
    if isinstance(mats, list):
        d["materials"] = mats[:3]

    steps = d.get("steps", [])
    if isinstance(steps, list):
        d["steps"] = steps[:5]

    safety = d.get("safety_notes", [])
    if isinstance(safety, list):
        d["safety_notes"] = safety[:3] if safety else ["무리/위험 행동 금지"]

    tags = d.get("tags", [])
    if isinstance(tags, list):
        d["tags"] = tags[:5]

    return d

def generate_daily_mission(profile: UserProfile, recent_logs: List[RecentMissionLog], llm_client: Optional[OpenAIChatClient]) -> Tuple[Mission, GenerationMeta]:
    errors: List[str] = []

    if llm_client is not None:
        try:
            user_prompt = build_user_prompt(profile, recent_logs)
            resp = llm_client.generate_mission_json(SYSTEM_PROMPT, user_prompt)
            mission_dict = _postprocess_mission_dict(resp.data, profile)
            mission = Mission.model_validate(mission_dict)

            vr = validate_mission(mission, profile, recent_logs)
            if vr.ok:
                return mission, GenerationMeta(used_fallback=False, errors=[])
            errors += vr.errors
        except Exception as e:
            errors.append(str(e))

    fb = build_fallback_mission(profile, recent_logs)
    vr2 = validate_mission(fb, profile, recent_logs)
    if vr2.ok:
        return fb, GenerationMeta(used_fallback=True, errors=errors)

    # last resort
    fb.time_minutes = min(fb.time_minutes, profile.time_available)
    fb.steps = fb.steps[:5]
    fb.materials = fb.materials[:3]
    fb.safety_notes = (fb.safety_notes or ["무리/위험 행동 금지"])[:3]
    return fb, GenerationMeta(used_fallback=True, errors=errors + vr2.errors)


# =========================
# SQLite (single file)
# =========================

DB_PATH = Path("app.db")

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def _ensure_column(conn: sqlite3.Connection, table: str, col: str, col_def: str) -> None:
    # SQLite: ALTER TABLE ADD COLUMN은 IF NOT EXISTS 없음(버전에 따라)
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = {row["name"] for row in cur.fetchall()}
    if col not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
        conn.commit()

def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS missions (
            date TEXT PRIMARY KEY,
            mission_json TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            completed INTEGER DEFAULT 0,
            satisfaction INTEGER,
            chosen_option TEXT,
            notes TEXT,
            tags TEXT,
            image_b64 TEXT,
            image_mime TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()

    # 기존 DB를 쓰는 경우를 대비해 컬럼 추가 보장
    _ensure_column(conn, "logs", "image_b64", "TEXT")
    _ensure_column(conn, "logs", "image_mime", "TEXT")

    conn.close()

def save_mission(m: Mission) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO missions(date, mission_json) VALUES(?, ?)",
        (m.date, m.model_dump_json(ensure_ascii=False)),
    )
    conn.commit()
    conn.close()

def load_mission(date_str: str) -> Optional[Mission]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT mission_json FROM missions WHERE date = ?", (date_str,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return Mission.model_validate_json(row["mission_json"])

def list_missions(limit: int = 30) -> List[Mission]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT mission_json FROM missions ORDER BY date DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [Mission.model_validate_json(r["mission_json"]) for r in rows]

def add_log(
    date_str: str,
    completed: bool,
    satisfaction: Optional[int],
    chosen_option: Optional[str],
    notes: Optional[str],
    tags: Optional[List[str]] = None,
    image_b64: Optional[str] = None,
    image_mime: Optional[str] = None,
) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO logs(date, completed, satisfaction, chosen_option, notes, tags, image_b64, image_mime)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date_str,
            1 if completed else 0,
            satisfaction,
            chosen_option,
            notes,
            json.dumps(tags or [], ensure_ascii=False),
            image_b64,
            image_mime,
        ),
    )
    conn.commit()
    conn.close()

def get_latest_log_for_date(date_str: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM logs
        WHERE date = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (date_str,),
    )
    row = cur.fetchone()
    conn.close()
    return row

def recent_logs(limit: int = 7) -> List[RecentMissionLog]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT m.date, m.mission_json,
               (SELECT completed FROM logs l WHERE l.date=m.date ORDER BY l.id DESC LIMIT 1) AS completed,
               (SELECT satisfaction FROM logs l WHERE l.date=m.date ORDER BY l.id DESC LIMIT 1) AS satisfaction,
               (SELECT chosen_option FROM logs l WHERE l.date=m.date ORDER BY l.id DESC LIMIT 1) AS chosen_option,
               (SELECT notes FROM logs l WHERE l.date=m.date ORDER BY l.id DESC LIMIT 1) AS notes,
               (SELECT tags FROM logs l WHERE l.date=m.date ORDER BY l.id DESC LIMIT 1) AS log_tags
        FROM missions m
        ORDER BY m.date DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()

    out: List[RecentMissionLog] = []
    for r in rows:
        mj = json.loads(r["mission_json"])
        tags = mj.get("tags", [])
        keyword_notes: List[str] = []
        if r["notes"]:
            keyword_notes = [str(r["notes"])[:30]]
        if r["log_tags"]:
            try:
                tags = json.loads(r["log_tags"])
            except Exception:
                pass

        out.append(
            RecentMissionLog(
                date=r["date"],
                title=mj.get("title", ""),
                tags=tags,
                completed=bool(r["completed"]) if r["completed"] is not None else False,
                satisfaction=r["satisfaction"],
                chosen_option=r["chosen_option"],
                keyword_notes=keyword_notes,
            )
        )
    return out


# =========================
# Streamlit UI
# =========================

st.set_page_config(page_title="하루 한 장면", page_icon="🎬", layout="centered")
init_db()

st.title("🎬 하루 한 장면")

with st.sidebar:
    st.header("오늘 상태")
    time_available = st.number_input("오늘 시간(분)", min_value=5, max_value=240, value=20, step=5)
    energy = st.selectbox("에너지", ["low", "medium", "high"], index=1)
    place = st.selectbox("장소", ["indoor", "outdoor", "any"], index=2)

    st.divider()
    st.subheader("선택 입력")
    companion = st.selectbox("동반 여부", ["(미지정)", "solo", "with"], index=0)
    budget = st.selectbox("예산", ["(미지정)", "free", "low", "any"], index=0)
    risk = st.slider("무모함(0~5)", 0, 5, 2)
    dislike = st.text_input("싫어하는 것(1개)", value="")

    st.divider()
    st.subheader("LLM(선택)")
    use_llm = st.checkbox("LLM 사용", value=False)

    api_key_input = ""
    model_input = "gpt-4.1-mini"

    if use_llm:
        api_key_input = st.text_input(
            "OpenAI API Key",
            type="password",
            value=st.session_state.get("openai_api_key", ""),
            help="키는 세션에만 저장되고 DB에는 저장하지 않습니다.",
        )
        model_input = st.text_input(
            "Model",
            value=st.session_state.get("openai_model", "gpt-4.1-mini"),
        )

        if api_key_input:
            st.session_state["openai_api_key"] = api_key_input
        if model_input:
            st.session_state["openai_model"] = model_input

profile = UserProfile(
    time_available=int(time_available),
    energy=energy,
    preference_place=place,
    companion=None if companion == "(미지정)" else companion,
    budget=None if budget == "(미지정)" else budget,
    risk_appetite=int(risk),
    dislike=(dislike.strip() or None),
)

today = today_str()

# 상태 변수
st.session_state.setdefault("chosen_option", None)
st.session_state.setdefault("show_completion", False)

tab1, tab2 = st.tabs(["오늘", "히스토리"])

# ---------- 오늘 ----------
with tab1:
    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button("🎲 오늘의 미션 생성", use_container_width=True):
            logs = recent_logs(7)

            llm_client = None
            if use_llm:
                try:
                    api_key = st.session_state.get("openai_api_key") or api_key_input
                    model = st.session_state.get("openai_model") or model_input
                    if not api_key:
                        st.warning("API Key를 입력하지 않아 fallback으로 생성합니다.")
                    else:
                        llm_client = OpenAIChatClient(api_key=api_key, model=model)
                except Exception as e:
                    st.warning(f"LLM 클라이언트 준비 실패: {e}")
                    llm_client = None

            mission, meta = generate_daily_mission(profile, logs, llm_client=llm_client)
            save_mission(mission)
            st.session_state["mission"] = mission
            st.session_state["meta"] = meta
            st.session_state["chosen_option"] = None
            st.session_state["show_completion"] = False

    with col2:
        if st.button("📌 오늘 미션 불러오기", use_container_width=True):
            m = load_mission(today)
            if m:
                st.session_state["mission"] = m
                st.session_state["show_completion"] = False
            else:
                st.info("저장된 오늘 미션이 없습니다. 생성해보세요.")

    mission: Optional[Mission] = st.session_state.get("mission")
    meta: Optional[GenerationMeta] = st.session_state.get("meta")

    if mission:
        st.subheader(mission.title)
        st.write(f"**{mission.one_liner}**")
        st.write(mission.why_this_is_you)

        st.write(" · ".join([f"`{t}`" for t in mission.tags]))
        st.write(
            f"⏱️ {mission.time_minutes}분  |  난이도 {mission.difficulty}/5  |  장소: {mission.indoor_outdoor}  |  비용: {mission.cost_level}"
        )

        st.markdown("### 준비물")
        st.write(" / ".join(mission.materials) if mission.materials else "없음")

        # (3) Twist 표시 삭제: 데이터에는 유지되지만 UI에는 보여주지 않음

        st.markdown("### Steps")
        for i, s in enumerate(mission.steps, start=1):
            st.write(f"{i}. {s}")

        # (4) 옵션: 클릭하지 않아도 항상 보이게 + 타이틀 변경
        st.markdown("### 옵션")
        a, b = mission.options[0], mission.options[1]

        boxA, boxB = st.columns(2)
        with boxA:
            st.markdown(f"#### {a.variation}")
            st.write(a.delta)
            if st.button("Option A 선택", use_container_width=True):
                st.session_state["chosen_option"] = "Option A"
        with boxB:
            st.markdown(f"#### {b.variation}")
            st.write(b.delta)
            if st.button("Option B 선택", use_container_width=True):
                st.session_state["chosen_option"] = "Option B"

        chosen = st.session_state.get("chosen_option")
        if chosen:
            opt = a if chosen == "Option A" else b
            st.info(f"선택됨: **{opt.variation}**")

        st.markdown("### 안전")
        for s in mission.safety_notes:
            st.write(f"- {s}")

        # 디버그(원하면 숨기고 싶으면 expander 자체를 제거해도 됨)
        if meta and (meta.used_fallback or meta.errors):
            with st.expander("생성 상태(디버그)"):
                st.write("fallback 사용:", meta.used_fallback if meta else None)
                for e in (meta.errors if meta else []):
                    st.write(f"- {e}")

        st.divider()

        # (5) 완료 기록: 버튼 눌러야 열리도록
        if st.button("✅ 미션 완료", use_container_width=True):
            st.session_state["show_completion"] = True

            # 축하 팝업: st.dialog 지원하면 modal, 아니면 balloons + info
            if hasattr(st, "dialog"):
                @st.dialog("🎉 축하해요!")
                def _celebrate_dialog():
                    st.image(_confetti_svg_bytes(), caption=None)
                    st.write("오늘의 장면을 남길 준비가 됐어요. 아래에 기록을 저장해보자!")
                    st.button("기록하러 가기", use_container_width=True)

                _celebrate_dialog()
            else:
                st.balloons()
                st.success("🎉 축하해요! 아래에서 오늘의 장면을 기록해보자.")

        if st.session_state.get("show_completion"):
            st.markdown("### 완료 기록")

            completed = True  # 버튼 눌러 들어왔으니 기본 완료 True
            satisfaction = st.slider("만족도(1~5)", 1, 5, 4)
            notes = st.text_area("메모(선택)", value="", height=90)

            # (6) 이미지 업로드/저장
            uploaded = st.file_uploader(
                "이미지 추가(선택)",
                type=["png", "jpg", "jpeg", "webp"],
                help="오늘의 장면을 사진으로 남겨보세요. DB에 base64로 저장됩니다.",
            )

            preview_bytes = None
            mime = None
            image_b64 = None

            if uploaded is not None:
                preview_bytes = uploaded.getvalue()
                mime = uploaded.type or "image/png"
                image_b64 = base64.b64encode(preview_bytes).decode("utf-8")
                st.image(preview_bytes, caption="업로드된 이미지 미리보기", use_container_width=True)

            if st.button("💾 오늘 기록 저장", use_container_width=True):
                add_log(
                    date_str=mission.date,
                    completed=completed,
                    satisfaction=satisfaction,
                    chosen_option=st.session_state.get("chosen_option"),
                    notes=notes.strip() or None,
                    tags=mission.tags,
                    image_b64=image_b64,
                    image_mime=mime,
                )
                st.success("저장했습니다!")

    else:
        st.info("사이드바에서 상태를 고르고 ‘오늘의 미션 생성’을 눌러보세요.")


# ---------- 히스토리 ----------
with tab2:
    st.subheader("📚 히스토리")
    missions = list_missions(30)
    if not missions:
        st.info("저장된 미션이 없습니다.")
    else:
        for m in missions:
            with st.expander(f"{m.date} — {m.title}"):
                st.write(f"**{m.one_liner}**")
                st.write(m.why_this_is_you)
                st.write(" / ".join(m.tags))
                st.write(f"⏱️ {m.time_minutes}분 · 난이도 {m.difficulty}/5 · {m.indoor_outdoor} · {m.cost_level}")

                # 최신 로그(이미지 포함) 표시
                row = get_latest_log_for_date(m.date)
                if row:
                    st.markdown("**기록**")
                    st.write(f"- 완료: {'예' if row['completed'] else '아니오'}")
                    if row["satisfaction"] is not None:
                        st.write(f"- 만족도: {row['satisfaction']}/5")
                    if row["chosen_option"]:
                        st.write(f"- 선택 옵션: {row['chosen_option']}")
                    if row["notes"]:
                        st.write(f"- 메모: {row['notes']}")

                    if row and row["image_b64"]:
    try:
        img_bytes = base64.b64decode(row["image_b64"])
        st.image(img_bytes, caption="저장된 이미지", use_container_width=True)
    except Exception:
        st.warning("저장된 이미지를 불러오지 못했어요.")

                else:
                    st.caption("이 날짜의 기록(완료/메모/이미지)이 아직 없습니다.")
