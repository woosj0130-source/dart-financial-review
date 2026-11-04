"""DART 최근 3개년 연결재무제표 핵심 계정 조회 Streamlit 앱."""

import io
import json
import os
import re
import zipfile
from datetime import date, datetime, timezone
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st


BASE_URL = "https://opendart.fss.or.kr/api"
ANNUAL_REPORT_CODE = "11011"  # 사업보고서
TRADE_RECEIVABLES = "매출채권"
COMBINED_RECEIVABLES = "매출채권 및 기타채권"
RECEIVABLE_ACCOUNTS = (TRADE_RECEIVABLES, COMBINED_RECEIVABLES)
REVIEW_STATUSES = ("미검토", "추가 자료 필요", "원인 확인")
REVIEW_FIELDS = ("corp_code", "account", "comparison", "status", "memo", "saved_at")
REVIEW_MEMO_LIMIT = 2000
COMBINED_RECEIVABLE_IDS = [
    "ifrs-full_TradeAndOtherCurrentReceivables",
    "ifrs_TradeAndOtherCurrentReceivables",
]

# 회사마다 계정 ID나 한글 계정명이 조금씩 달라서 ID와 이름을 함께 확인한다.
ACCOUNT_RULES = {
    "매출액": {
        "ids": ["ifrs-full_Revenue", "ifrs_Revenue", "dart_Revenue"],
        "names": ["매출액", "수익(매출액)", "영업수익", "수익"],
    },
    "영업이익": {
        "ids": ["dart_OperatingIncomeLoss", "ifrs-full_OperatingIncomeLoss"],
        "names": ["영업이익", "영업이익(손실)", "영업손익"],
    },
    "당기순이익": {
        "ids": ["ifrs-full_ProfitLoss", "ifrs_ProfitLoss"],
        "names": ["당기순이익", "당기순이익(손실)", "당기순손익"],
    },
    "자산총계": {
        "ids": ["ifrs-full_Assets", "ifrs_Assets"],
        "names": ["자산총계", "자산총액"],
    },
    "부채총계": {
        "ids": ["ifrs-full_Liabilities", "ifrs_Liabilities"],
        "names": ["부채총계", "부채총액"],
    },
    TRADE_RECEIVABLES: {
        "ids": [*COMBINED_RECEIVABLE_IDS, "dart_ShortTermTradeReceivable"],
        "names": ["매출채권", "매출채권 및 기타채권", "매출채권및기타채권"],
    },
    "재고자산": {
        "ids": ["ifrs-full_Inventories", "ifrs_Inventories"],
        "names": ["재고자산"],
    },
    "영업활동현금흐름": {
        "ids": [
            "ifrs-full_CashFlowsFromUsedInOperatingActivities",
            "ifrs_CashFlowsFromUsedInOperatingActivities",
        ],
        "names": ["영업활동현금흐름", "영업활동으로 인한 현금흐름"],
    },
}


class DartError(Exception):
    """사용자에게 보여줄 수 있는 DART 조회 오류."""


def get_api_key(typed_key: str) -> str:
    """화면 입력값을 우선하고, 없으면 환경변수 값을 사용한다."""
    return typed_key.strip() or os.getenv("DART_API_KEY", "").strip()


def request_json(endpoint: str, params: dict) -> dict:
    """DART JSON API를 호출하고 DART가 보낸 오류도 예외로 바꾼다."""
    try:
        response = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=20)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise DartError("DART 서버에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요.") from exc
    except ValueError as exc:
        raise DartError("DART 서버에서 예상하지 못한 응답을 받았습니다.") from exc

    if data.get("status") != "000":
        raise DartError(data.get("message", "DART 조회 중 알 수 없는 오류가 발생했습니다."))
    return data


def read_dart_xml_error(content: bytes) -> str | None:
    """DART가 ZIP 대신 보낸 XML 오류에서 코드와 안내 문구를 읽는다."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None

    status = (root.findtext("status") or "").strip()
    message = (root.findtext("message") or "").strip()
    if not status and not message:
        return None

    suggestions = {
        "010": "인증키 관리 화면에서 현재 Key를 다시 복사해 주세요.",
        "011": "인증키가 일시 중지된 상태인지 확인해 주세요.",
        "012": "DART에 등록한 접근 IP 또는 웹 사용환경을 확인해 주세요.",
        "020": "요청 한도를 초과했습니다. 오픈API 이용현황을 확인해 주세요.",
        "800": "DART 시스템 점검이 끝난 뒤 다시 시도해 주세요.",
        "900": "일시적인 DART 오류일 수 있으므로 잠시 후 다시 시도해 주세요.",
        "901": "개인정보 보유기간이 만료된 Key입니다. DART 계정 상태를 확인해 주세요.",
    }
    code_text = f" {status}" if status else ""
    message_text = message or "상세 메시지가 없습니다."
    suggestion = suggestions.get(status, "API Key와 DART 서비스 상태를 확인해 주세요.")
    return f"DART 오류{code_text}: {message_text} {suggestion}"


@st.cache_data(ttl=24 * 60 * 60, show_spinner=False)
def load_corporations(api_key: str) -> list[dict]:
    """DART 전체 기업 고유번호 ZIP을 내려받아 회사 목록으로 만든다."""
    try:
        response = requests.get(
            f"{BASE_URL}/corpCode.xml",
            params={"crtfc_key": api_key},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise DartError(
            "DART 서버에 연결하지 못했습니다. 인터넷 연결과 DART 서비스 상태를 확인해 주세요."
        ) from exc

    try:
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            xml_bytes = archive.read("CORPCODE.xml")
    except zipfile.BadZipFile as exc:
        dart_error = read_dart_xml_error(response.content)
        if dart_error:
            raise DartError(dart_error) from exc
        raise DartError(
            "DART가 기업목록 ZIP이 아닌 예상하지 못한 응답을 보냈습니다. 잠시 후 다시 시도해 주세요."
        ) from exc
    except KeyError as exc:
        raise DartError(
            "DART 기업목록 파일 안에서 CORPCODE.xml을 찾지 못했습니다. 잠시 후 다시 시도해 주세요."
        ) from exc

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise DartError("DART 기업목록 XML을 읽지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
    companies = []
    for item in root.findall("list"):
        companies.append(
            {
                "corp_code": item.findtext("corp_code", "").strip(),
                "corp_name": item.findtext("corp_name", "").strip(),
                "stock_code": item.findtext("stock_code", "").strip(),
            }
        )
    return companies


def search_companies(companies: list[dict], query: str) -> list[dict]:
    """기업명이 같은 회사를 먼저, 이름에 검색어가 포함된 회사를 그다음에 둔다."""
    query = query.strip().lower()
    matches = [c for c in companies if query in c["corp_name"].lower()]
    return sorted(
        matches,
        key=lambda c: (
            c["corp_name"].lower() != query,
            not bool(c["stock_code"]),
            len(c["corp_name"]),
            c["corp_name"],
        ),
    )[:50]


def normalize_name(value: str) -> str:
    """띄어쓰기와 괄호 차이를 줄여 계정명을 비교하기 쉽게 만든다."""
    return re.sub(r"[\s()ㆍ·,_-]", "", value or "").lower()


def parse_amount(value: str):
    """DART의 쉼표 포함 금액 문자열을 정수로 바꾼다."""
    cleaned = (value or "").replace(",", "").strip()
    if cleaned in {"", "-"}:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def find_account(rows: list[dict], rule: dict):
    """계정 ID를 우선 사용하고, 찾지 못하면 한글 계정명으로 찾는다."""
    id_priority = {account_id: i for i, account_id in enumerate(rule["ids"])}
    name_priority = {normalize_name(name): i for i, name in enumerate(rule["names"])}

    candidates = []
    for row in rows:
        account_id = row.get("account_id", "")
        account_name = normalize_name(row.get("account_nm", ""))
        if account_id in id_priority:
            candidates.append((0, id_priority[account_id], row))
        elif account_name in name_priority:
            candidates.append((1, name_priority[account_name], row))

    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


@st.cache_data(ttl=60 * 60, show_spinner=False)
def fetch_year(api_key: str, corp_code: str, year: int) -> list[dict] | None:
    """한 사업연도의 사업보고서 연결재무제표 전체 계정을 조회한다."""
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": str(year),
        "reprt_code": ANNUAL_REPORT_CODE,
        "fs_div": "CFS",  # 연결재무제표
    }
    try:
        return request_json("fnlttSinglAcntAll.json", params).get("list", [])
    except DartError as exc:
        # 013은 '조회된 데이터가 없습니다'. 다른 오류는 숨기지 않는다.
        if "조회된 데이타가 없습니다" in str(exc) or "조회된 데이터가 없습니다" in str(exc):
            return None
        raise


def collect_recent_three_years(api_key: str, corp_code: str):
    """올해부터 과거로 조회해 데이터가 존재하는 최근 3개 사업연도를 모은다."""
    found = []
    checked_years = []
    for year in range(date.today().year, date.today().year - 8, -1):
        checked_years.append(year)
        rows = fetch_year(api_key, corp_code, year)
        if rows:
            found.append((year, rows))
        if len(found) == 3:
            break
    return sorted(found), checked_years


def make_result(found_years: list[tuple[int, list[dict]]]):
    """연도별 핵심 계정 금액과 선택한 채권 계정의 공시 정보를 반환한다."""
    values = {account: {} for account in ACCOUNT_RULES}
    values[COMBINED_RECEIVABLES] = {}
    missing = []
    receivable_sources = {}

    for year, rows in found_years:
        for account, rule in ACCOUNT_RULES.items():
            row = find_account(rows, rule)
            amount = parse_amount(row.get("thstrm_amount", "")) if row else None
            result_account = account
            if account == TRADE_RECEIVABLES and row:
                source_name = row.get("account_nm") or ""
                receivable_sources[str(year)] = {
                    "name": source_name or "계정명 없음",
                    "receipt": row.get("rcept_no") or "",
                }
                normalized_name = normalize_name(source_name)
                # 공시 계정명이 분명하면 계정 ID보다 이름을 우선한다.
                if "기타채권" in normalized_name or (
                    normalized_name not in {"매출채권", "단기매출채권", "유동매출채권"}
                    and row.get("account_id") in COMBINED_RECEIVABLE_IDS
                ):
                    result_account = COMBINED_RECEIVABLES
            values[result_account][year] = amount
            if amount is None:
                missing.append(f"{year}년 {result_account}: 연결재무제표에서 해당 계정의 금액을 확인하지 못했습니다.")

    if not values[COMBINED_RECEIVABLES]:
        del values[COMBINED_RECEIVABLES]
    if not values[TRADE_RECEIVABLES]:
        del values[TRADE_RECEIVABLES]
    frame = pd.DataFrame.from_dict(values, orient="index")
    frame.index.name = "핵심 계정"
    frame = frame.reindex(sorted(frame.columns), axis=1)
    frame.columns = [f"{year}년" for year in frame.columns]
    return frame, missing, receivable_sources


def receivable_filing_links(comparison: str, sources: dict) -> list[str]:
    """비교에 사용한 사업보고서의 접수번호로 DART 링크를 만든다."""
    links = []
    for year in comparison.split("→"):
        receipt = str(sources.get(year, {}).get("receipt", ""))
        if re.fullmatch(r"[0-9]{14}", receipt):
            links.append(
                f"[{year}년 사업보고서](https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt})"
            )
    return links


def format_won(value) -> str:
    """화면 표에서 원 단위 금액을 읽기 쉽게 표시한다."""
    if pd.isna(value):
        return "자료 없음"
    return f"{int(value):,}"


def calculate_percentage(numerator, denominator):
    """분자와 분모로 백분율을 계산한다. 계산할 수 없으면 None을 반환한다."""
    if pd.isna(numerator) or pd.isna(denominator) or denominator == 0:
        return None
    return numerator / denominator * 100


def calculate_growth_rates(amount_frame: pd.DataFrame) -> pd.DataFrame:
    """각 핵심 계정의 인접한 두 사업연도 사이 증감률을 계산한다."""
    growth_rates = {}
    year_columns = list(amount_frame.columns)

    for account in amount_frame.index:
        account_growth = {}
        for previous_column, current_column in zip(year_columns, year_columns[1:]):
            previous_amount = amount_frame.loc[account, previous_column]
            current_amount = amount_frame.loc[account, current_column]
            comparison_name = f"{previous_column.rstrip('년')}→{current_column.rstrip('년')}"

            if pd.isna(previous_amount) or pd.isna(current_amount) or previous_amount == 0:
                account_growth[comparison_name] = None
            else:
                account_growth[comparison_name] = (
                    (current_amount - previous_amount) / abs(previous_amount) * 100
                )
        growth_rates[account] = account_growth

    result = pd.DataFrame.from_dict(growth_rates, orient="index")
    result.index.name = "핵심 계정"
    return result


def calculate_financial_ratios(amount_frame: pd.DataFrame) -> pd.DataFrame:
    """핵심 계정 금액으로 연도별 주요 재무비율을 계산한다."""
    ratio_values = {
        "영업이익률": {},
        "순이익률": {},
        "부채비율": {},
        "재고자산비율": {},
        "영업현금흐름비율": {},
    }
    receivable_accounts = amount_frame.index.intersection(RECEIVABLE_ACCOUNTS)
    for account in receivable_accounts:
        ratio_values[f"{account}비율"] = {}

    for year_column in amount_frame.columns:
        revenue = amount_frame.loc["매출액", year_column]
        operating_income = amount_frame.loc["영업이익", year_column]
        net_income = amount_frame.loc["당기순이익", year_column]
        assets = amount_frame.loc["자산총계", year_column]
        liabilities = amount_frame.loc["부채총계", year_column]
        inventories = amount_frame.loc["재고자산", year_column]
        operating_cash_flow = amount_frame.loc["영업활동현금흐름", year_column]

        equity = None if pd.isna(assets) or pd.isna(liabilities) else assets - liabilities

        ratio_values["영업이익률"][year_column] = calculate_percentage(
            operating_income, revenue
        )
        ratio_values["순이익률"][year_column] = calculate_percentage(net_income, revenue)
        ratio_values["부채비율"][year_column] = calculate_percentage(liabilities, equity)
        for account in receivable_accounts:
            ratio_values[f"{account}비율"][year_column] = calculate_percentage(
                amount_frame.loc[account, year_column], revenue
            )
        ratio_values["재고자산비율"][year_column] = calculate_percentage(inventories, revenue)
        ratio_values["영업현금흐름비율"][year_column] = calculate_percentage(
            operating_cash_flow, net_income
        )

    result = pd.DataFrame.from_dict(ratio_values, orient="index")
    result.index.name = "재무비율"
    return result


def format_percent(value, show_plus: bool = False) -> str:
    """계산된 숫자를 화면용 백분율 문자열로 바꾼다."""
    if pd.isna(value):
        return "계산 불가"
    if show_plus:
        return f"{value:+.1f}%"
    return f"{value:.1f}%"


def find_review_points(
    amount_frame: pd.DataFrame,
    growth_frame: pd.DataFrame,
    ratio_frame: pd.DataFrame,
    account_growth_limit: float,
    margin_change_limit: float,
    relationship_gap_limit: float,
) -> list[dict]:
    """설정한 기준에 해당하는 항목을 추가 검토 후보로 정리한다."""
    review_points = []

    # 1. 핵심 계정의 전년 대비 증감률이 설정값 이상인지 확인한다.
    for account in growth_frame.index:
        for comparison in growth_frame.columns:
            growth = growth_frame.loc[account, comparison]
            if not pd.isna(growth) and abs(growth) >= account_growth_limit:
                review_points.append(
                    {
                        "title": f"{account}의 큰 전년 대비 변화",
                        "account": account,
                        "comparison": comparison,
                        "reason": (
                            f"{comparison} {account} 증감률이 {growth:+.1f}%로, "
                            f"설정 기준인 ±{account_growth_limit:.1f}% 이상입니다."
                        ),
                        "next_step": "증감 원인과 관련 주석을 추가로 확인할 수 있습니다.",
                    }
                )

    # 2. 영업이익률과 순이익률의 연도 간 차이를 퍼센트포인트(%p)로 확인한다.
    year_columns = list(ratio_frame.columns)
    for ratio_name in ["영업이익률", "순이익률"]:
        for previous_column, current_column in zip(year_columns, year_columns[1:]):
            previous_ratio = ratio_frame.loc[ratio_name, previous_column]
            current_ratio = ratio_frame.loc[ratio_name, current_column]
            if pd.isna(previous_ratio) or pd.isna(current_ratio):
                continue

            change = current_ratio - previous_ratio
            if abs(change) >= margin_change_limit:
                comparison = f"{previous_column.rstrip('년')}→{current_column.rstrip('년')}"
                review_points.append(
                    {
                        "title": f"{ratio_name}의 큰 변화",
                        "reason": (
                            f"{comparison} {ratio_name}이 {previous_ratio:.1f}%에서 "
                            f"{current_ratio:.1f}%로 {change:+.1f}%p 변했습니다. "
                            f"설정 기준은 ±{margin_change_limit:.1f}%p입니다."
                        ),
                        "next_step": "매출과 관련 비용의 변동 원인을 추가로 확인할 수 있습니다.",
                    }
                )

    # 3. 매출채권·재고자산 증가율이 매출 증가율보다 크게 높은지 확인한다.
    related_accounts = {
        TRADE_RECEIVABLES: "채권의 구성, 회수기간 및 대손 관련 주석을 추가로 확인할 수 있습니다.",
        COMBINED_RECEIVABLES: "주석에서 매출채권과 기타채권의 구성 및 각각의 변동을 확인할 수 있습니다.",
        "재고자산": "재고의 구성, 회전 추세 및 평가 관련 주석을 추가로 확인할 수 있습니다.",
    }
    for account, next_step in related_accounts.items():
        if account not in growth_frame.index:
            continue
        for comparison in growth_frame.columns:
            revenue_growth = growth_frame.loc["매출액", comparison]
            account_growth = growth_frame.loc[account, comparison]
            if pd.isna(revenue_growth) or pd.isna(account_growth):
                continue

            growth_gap = account_growth - revenue_growth
            if growth_gap >= relationship_gap_limit:
                review_points.append(
                    {
                        "title": f"{account}과 매출액 증가율의 차이",
                        "account": account,
                        "comparison": comparison,
                        "reason": (
                            f"{comparison} {account} 증가율({account_growth:+.1f}%)이 "
                            f"매출액 증가율({revenue_growth:+.1f}%)보다 "
                            f"{growth_gap:.1f}%p 높습니다. "
                            f"설정 기준은 {relationship_gap_limit:.1f}%p입니다."
                        ),
                        "next_step": next_step,
                    }
                )

    # 4. 순이익은 양수인데 영업활동현금흐름은 음수인 연도를 확인한다.
    for year_column in amount_frame.columns:
        net_income = amount_frame.loc["당기순이익", year_column]
        operating_cash_flow = amount_frame.loc["영업활동현금흐름", year_column]
        if pd.isna(net_income) or pd.isna(operating_cash_flow):
            continue
        if net_income > 0 and operating_cash_flow < 0:
            review_points.append(
                {
                    "title": "순이익과 영업활동현금흐름의 방향 차이",
                    "reason": (
                        f"{year_column} 당기순이익은 {net_income:,.0f}원으로 양수이지만, "
                        f"영업활동현금흐름은 {operating_cash_flow:,.0f}원으로 음수입니다."
                    ),
                    "next_step": "운전자본 변동과 영업활동현금흐름 주석을 추가로 확인할 수 있습니다.",
                }
            )

    return review_points


def read_review_backup(content: bytes) -> list[dict]:
    """이 앱에서 내보낸 검토 기록을 확인한 뒤 반환한다."""
    backup = json.loads(content)
    if not isinstance(backup, dict) or backup.get("version") != 1:
        raise ValueError("지원하지 않는 검토 기록 파일입니다.")

    records = backup.get("records")
    if not isinstance(records, list):
        raise ValueError("검토 기록 목록이 없습니다.")
    for record in records:
        if (
            not isinstance(record, dict)
            or record.keys() != set(REVIEW_FIELDS)
            or not all(isinstance(value, str) for value in record.values())
            or not re.fullmatch(r"[0-9]{8}", record["corp_code"])
            or not re.fullmatch(r"[0-9]{4}→[0-9]{4}", record["comparison"])
            or record["account"] not in RECEIVABLE_ACCOUNTS
            or record["status"] not in REVIEW_STATUSES
            or len(record["memo"]) > REVIEW_MEMO_LIMIT
        ):
            raise ValueError("검토 기록에 잘못된 항목이 있습니다.")
        saved_at = datetime.fromisoformat(record["saved_at"])
        if saved_at.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError("검토 기록의 저장 시각을 확인해 주세요.")
        record["saved_at"] = saved_at.isoformat()
    return records


@st.fragment
def show_receivable_reviews(company: dict, review_points: list[dict]):
    """채권 검토 상태와 메모를 추가하고 세션의 기록을 백업한다."""
    st.subheader("채권 검토 기록")
    st.caption(
        "기록은 현재 세션에 보관됩니다. 새로 접속할 때 이어서 검토하려면 "
        "JSON 파일로 내려받아 두세요."
    )
    records = st.session_state.setdefault("receivable_review_records", [])
    with st.expander("검토 기록 불러오기"):
        backup = st.file_uploader("이 앱에서 내려받은 JSON 파일", type="json")
        if st.button("기록 불러오기", disabled=backup is None):
            try:
                incoming = read_review_backup(backup.getvalue())
                # 기존 기록과 합치고 같은 파일을 다시 불러와도 중복되지 않게 한다.
                merged = {
                    tuple(record[field] for field in REVIEW_FIELDS): record
                    for record in records + incoming
                }
                added_count = len(merged) - len(records)
                records[:] = merged.values()
                st.success(f"새 검토 기록 {added_count}건을 불러왔습니다.")
            except (ValueError, RecursionError):
                st.error("검토 기록을 읽지 못했습니다. 이 앱에서 내려받은 JSON 파일을 선택해 주세요.")

    corp_code = company["corp_code"]
    company_records = [record for record in records if record["corp_code"] == corp_code]
    cases = {
        (point["comparison"], point["account"])
        for point in review_points
        if point.get("account") in RECEIVABLE_ACCOUNTS
    }
    cases.update((record["comparison"], record["account"]) for record in company_records)
    if cases:
        comparison, account = st.selectbox(
            "기록할 채권 검토 항목",
            sorted(cases, reverse=True),
            format_func=lambda case: f"{case[0]} · {case[1]}",
            key=f"review-case-{corp_code}",
        )
        history = sorted(
            (record for record in company_records
             if (record["comparison"], record["account"]) == (comparison, account)),
            key=lambda record: record["saved_at"],
            reverse=True,
        )
        last = history[0] if history else None
        current_status = last["status"] if last else REVIEW_STATUSES[0]
        form_key = f"review-{corp_code}-{comparison}-{account}"
        with st.form(form_key, clear_on_submit=True):
            status = st.selectbox("검토 상태", (None, *REVIEW_STATUSES),
                                  format_func=lambda value: value or "현재 상태 유지")
            memo = st.text_area("검토 메모", max_chars=REVIEW_MEMO_LIMIT)
            submitted = st.form_submit_button("검토 기록 저장")
        if submitted:
            status = status or current_status
            if status == current_status and not memo.strip():
                st.info("상태를 바꾸거나 메모를 입력하면 기록을 저장할 수 있습니다.")
            else:
                record = dict(
                    corp_code=corp_code, account=account, comparison=comparison,
                    status=status, memo=memo.strip(), saved_at=datetime.now(timezone.utc).isoformat(),
                )
                records.append(record)
                history.insert(0, record)
                st.success("검토 기록을 저장했습니다.")

        st.write(f"**현재 상태** · {history[0]['status'] if history else current_status}")
        if history:
            with st.expander(f"변경 이력 {len(history)}건"):
                for record in history:
                    saved_at = datetime.fromisoformat(record["saved_at"]).astimezone(ZoneInfo("Asia/Seoul"))
                    st.caption(f"{saved_at:%Y-%m-%d %H:%M:%S} KST · {record['status']}")
                    if record["memo"]:
                        st.text(record["memo"])
    else:
        st.info("현재 조회에서 기록할 채권 검토 항목이 없습니다.")

    if records:
        st.download_button(
            "전체 검토 기록 내려받기",
            data=json.dumps({"version": 1, "records": records}, ensure_ascii=False, indent=2),
            file_name="dart_review_history.json",
            mime="application/json",
            on_click="ignore",
        )


@st.fragment
def show_financial_charts(
    amount_frame: pd.DataFrame,
    growth_frame: pd.DataFrame,
    ratio_frame: pd.DataFrame,
):
    """조회 결과를 세 가지 간단한 그래프로 보여준다."""
    st.subheader("6. 재무 변화 시각화")

    st.markdown("**핵심 계정 금액 추세**")
    selected_account = st.selectbox(
        "그래프로 볼 계정",
        options=list(amount_frame.index),
        key="chart_account",
    )
    selected_amounts = amount_frame.loc[selected_account]
    if selected_amounts.notna().any():
        amount_chart = selected_amounts.rename("금액").to_frame()
        st.line_chart(amount_chart, width="stretch", height=300)
        st.caption(f"{selected_account}의 사업연도별 금액입니다. 단위: 원")
    else:
        st.info(f"{selected_account}은 그래프로 표시할 수 있는 금액 자료가 없습니다.")

    st.markdown("**수익성 비율 추세**")
    profitability_chart = ratio_frame.loc[["영업이익률", "순이익률"]].T
    if profitability_chart.notna().any().any():
        st.line_chart(profitability_chart, width="stretch", height=300)
        st.caption("영업이익률과 순이익률의 사업연도별 변화입니다. 단위: %")
    else:
        st.info("수익성 비율을 그래프로 표시하는 데 필요한 자료가 없습니다.")

    st.markdown("**매출 관련 증감률 비교**")
    comparison_accounts = ["매출액", TRADE_RECEIVABLES, COMBINED_RECEIVABLES, "재고자산"]
    growth_comparison_chart = growth_frame.loc[
        [account for account in comparison_accounts if account in growth_frame.index]
    ].T
    if growth_comparison_chart.notna().any().any():
        st.bar_chart(growth_comparison_chart, width="stretch", height=350)
        st.caption(
            "매출액·채권·재고자산의 전년 대비 증감률을 비교합니다. 단위: %"
        )
    else:
        st.info("매출 관련 증감률을 그래프로 표시하는 데 필요한 자료가 없습니다.")

    st.caption(
        "그래프는 공개 재무정보의 변화를 보기 쉽게 표현한 것이며, "
        "특정 회계처리의 적정성이나 부정·오류 여부를 판단하지 않습니다."
    )


st.set_page_config(page_title="DART 재무제표 조회", page_icon="📊", layout="wide")
st.title("DART 최근 3개년 연결재무제표 조회")
st.caption("공개 재무정보를 불러오는 1단계 도구이며, 부정이나 회계오류를 판단하지 않습니다.")

with st.sidebar:
    st.header("조회 설정")
    typed_key = st.text_input(
        "DART API Key",
        type="password",
        help="입력값은 화면에 가려집니다. DART_API_KEY 환경변수가 있으면 비워도 됩니다.",
    )
    company_query = st.text_input("기업명", placeholder="예: 삼성전자")
    with st.expander("추가 검토 기준 설정"):
        st.caption("아래 값은 보편적인 판정 기준이 아니라 조정 가능한 초기 스크리닝 조건입니다.")
        account_growth_limit = st.number_input(
            "핵심 계정 증감률 기준 (%)",
            min_value=0.0,
            value=30.0,
            step=5.0,
        )
        margin_change_limit = st.number_input(
            "이익률 변화 기준 (%p)",
            min_value=0.0,
            value=5.0,
            step=1.0,
        )
        relationship_gap_limit = st.number_input(
            "매출 대비 채권·재고 증가율 차이 기준 (%p)",
            min_value=0.0,
            value=20.0,
            step=5.0,
        )
    search_clicked = st.button("기업 검색", type="primary", width="stretch")

search_completed = False
if search_clicked:
    # 새 검색 결과가 이전 검색 결과와 섞이지 않게 먼저 비운다.
    st.session_state.pop("matches", None)
    api_key = get_api_key(typed_key)
    if not api_key:
        st.error("DART API Key를 입력하거나 DART_API_KEY 환경변수를 설정해 주세요.")
    elif not company_query.strip():
        st.warning("검색할 기업명을 입력해 주세요.")
    else:
        try:
            with st.spinner("DART 기업 목록을 확인하고 있습니다..."):
                companies = load_corporations(api_key)
                matches = search_companies(companies, company_query)
            st.session_state["api_key"] = api_key
            st.session_state["matches"] = matches
            search_completed = True
        except DartError as exc:
            st.error(str(exc))

matches = st.session_state.get("matches", [])
if search_clicked and search_completed and not matches:
    st.info("입력한 이름이 포함된 기업을 찾지 못했습니다. 정식 기업명 일부로 다시 검색해 주세요.")

if matches:
    st.subheader("1. 조회할 기업 선택")

    def company_label(company: dict) -> str:
        stock = company["stock_code"] or "비상장/종목코드 없음"
        return f'{company["corp_name"]} ({stock})'

    selected_index = st.selectbox(
        "검색 결과",
        options=range(len(matches)),
        format_func=lambda index: company_label(matches[index]),
    )
    selected = matches[selected_index]

    if st.button("최근 3개년 연결재무제표 불러오기", type="primary"):
        try:
            with st.spinner("최근 사업보고서를 찾고 핵심 계정을 정리하고 있습니다..."):
                found_years, checked_years = collect_recent_three_years(
                    st.session_state["api_key"], selected["corp_code"]
                )

            if not found_years:
                st.warning(
                    "최근 사업연도에서 연결재무제표를 찾지 못했습니다. "
                    "연결 대상 종속회사가 없는 기업이거나 사업보고서가 아직 제출되지 않았을 수 있습니다."
                )
            else:
                frame, missing, receivable_sources = make_result(found_years)
                years = [year for year, _ in found_years]
                st.subheader("2. 조회 결과")
                st.write(
                    f'**기업:** {selected["corp_name"]}  |  '
                    f'**종목코드:** {selected["stock_code"] or "없음"}  |  '
                    f'**사업연도:** {min(years)}~{max(years)}년'
                )
                st.caption("금액 단위: 원 / DART 사업보고서의 연결재무제표(CFS) 기준")
                st.dataframe(frame.style.format(format_won), width="stretch")
                if receivable_sources:
                    source_labels = [
                        f"{year}년 {source['name']}" for year, source in receivable_sources.items()
                    ]
                    st.caption("채권 공시 계정명 · " + " / ".join(source_labels))

                growth_frame = calculate_growth_rates(frame)
                st.subheader("3. 전년 대비 증감률")
                if (
                    TRADE_RECEIVABLES in frame.index
                    and COMBINED_RECEIVABLES in frame.index
                    and frame.loc[[TRADE_RECEIVABLES, COMBINED_RECEIVABLES]].notna().any(axis=1).all()
                ):
                    st.warning(
                        "사업연도별 채권 계정의 범위가 다릅니다. "
                        "매출채권과 매출채권 및 기타채권 사이의 증감률은 계산하지 않습니다."
                    )
                st.dataframe(
                    growth_frame.style.format(
                        lambda value: format_percent(value, show_plus=True)
                    ),
                    width="stretch",
                )

                ratio_frame = calculate_financial_ratios(frame)
                st.subheader("4. 주요 재무비율")
                st.dataframe(
                    ratio_frame.style.format(format_percent),
                    width="stretch",
                )

                review_points = find_review_points(
                    frame,
                    growth_frame,
                    ratio_frame,
                    account_growth_limit,
                    margin_change_limit,
                    relationship_gap_limit,
                )
                st.subheader("5. 추가 검토 포인트")
                st.caption(
                    "아래 항목은 설정한 숫자 기준에 해당한 항목을 정리한 것이며, "
                    "이상·위험·부정 또는 오류를 판정한 결과가 아닙니다."
                )

                if review_points:
                    for number, point in enumerate(review_points, start=1):
                        with st.container(border=True):
                            st.markdown(f"**{number}. {point['title']}**")
                            st.write(point["reason"])
                            if point.get("account") in RECEIVABLE_ACCOUNTS:
                                links = receivable_filing_links(
                                    point["comparison"], receivable_sources
                                )
                                if links:
                                    st.markdown("**공시 원문** · " + " / ".join(links))
                                if len(links) < 2:
                                    st.caption("일부 공시 링크가 없습니다. DART에서 해당 연도 사업보고서를 검색해 주세요.")
                                st.write(
                                    "**공개 공시에서 확인** · 연결재무제표 주석에서 채권 구성, "
                                    "손실충당금, 공시된 연령분석을 확인합니다."
                                )
                                if selected["stock_code"] == "005380" and point["comparison"].endswith("→2025"):
                                    st.caption("현대자동차 2025년 연결재무제표 주석 3번 ‘매출채권’에 채권 내역과 연령분석이 있습니다.")
                                st.write(
                                    "**회사에 요청할 자료** · 거래처별 채권 원장, 기말 이후 "
                                    "회수 내역과 입금 증빙, 주요 거래처의 결제 조건을 확인합니다."
                                )
                            else:
                                st.caption(f"추가 확인 예시: {point['next_step']}")
                else:
                    st.success(
                        "현재 설정한 기준에 해당하는 추가 검토 포인트가 없습니다. "
                        "이는 회계처리의 적정성을 의미하지 않습니다."
                    )

                show_receivable_reviews(selected, review_points)
                show_financial_charts(frame, growth_frame, ratio_frame)

                if len(found_years) < 3:
                    st.warning(
                        f"최근 {len(found_years)}개 사업연도만 찾았습니다. "
                        f"확인한 연도: {', '.join(map(str, checked_years))}"
                    )
                if missing:
                    with st.expander(f"찾지 못한 계정 {len(missing)}건과 이유"):
                        for reason in missing:
                            st.write(f"- {reason}")

                st.info(
                    "기업별 계정명과 계정 ID가 다를 수 있으므로 일부 항목은 '자료 없음'으로 표시될 수 있습니다. "
                    "최종 확인에는 DART 원문 사업보고서와 주석을 이용해 주세요."
                )
                st.caption(
                    "위 지표는 공개 재무정보를 이용한 단순 계산 결과이며, "
                    "특정 회계처리의 적정성이나 부정·오류 여부를 판단하지 않습니다."
                )
        except DartError as exc:
            st.error(f"재무제표를 불러오지 못했습니다: {exc}")
        except Exception as exc:
            st.error("예상하지 못한 오류가 발생했습니다. 입력값을 확인한 뒤 다시 시도해 주세요.")
            st.caption(f"개발용 오류 정보: {type(exc).__name__}: {exc}")
