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
                    source_labels = [f"{year} {name}" for year, name in receivable_sources.items()]
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
                            st.caption(f"추가 확인 예시: {point['next_step']}")
                else:
                    st.success(
                        "현재 설정한 기준에 해당하는 추가 검토 포인트가 없습니다. "
                        "이는 회계처리의 적정성을 의미하지 않습니다."
                    )

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
