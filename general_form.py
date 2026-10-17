"""Official KORAIL /ticket/search/general form navigator.

Based on user-provided rendered HTML dated 2026-10-17. Station and calendar
popovers are React-rendered *after* click, so any unsupported popup must fail
closed with useful diagnostics; do not guess values or bypass restrictions.
"""
from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup
from playwright.async_api import Page, TimeoutError as PWTimeout

TARGET_DATE = "2026-10-24"
TARGET_TIME = "00:00"
GENERAL_FIELD = {
    "origin": 'input[name="txtGoStart"]',
    "destination": 'input[name="txtGoEnd"]',
    "date": "#startDate",
    "passengers": '.tab-tck.type_by a[title="인원"]',
}
POPUP_ROOTS = (
    '.type_station_lookup-pop:visible', '.type_tranin-station-pop_wrap:visible',
    '.layerPopup:visible', '.layerWrap:visible', '[role="dialog"]:visible',
    '.popup:visible', '.popUp:visible'
)
CALENDAR_ROOTS = (
    '[role="dialog"]:visible', '.layerPopup:visible', '.layerWrap:visible',
    '.calendar:visible', '[class*="calendar"]:visible',
    '.datepicker:visible', '[class*="datePicker"]:visible',
    '[class*="date-picker"]:visible', '.popUp:visible'
)


class SetupUIError(RuntimeError):
    pass


def general_form_from_saved_html(html: str) -> dict[str, Any]:
    """Offline snapshot of general-form structure; never makes a live request."""
    soup = BeautifulSoup(html, "lxml")
    for n in soup(["script", "style"]):
        n.decompose()
    def val(selector: str) -> str:
        el = soup.select_one(selector)
        return str(el.get("value", "") if el else "")
    def selected_radio(name: str) -> str:
        el = soup.select_one(f'input[name="{name}"][checked]')
        return str(el.get("value") if el else "")
    return {
        "origin": val(GENERAL_FIELD["origin"]),
        "destination": val(GENERAL_FIELD["destination"]),
        "date": val(GENERAL_FIELD["date"]),
        "passengers": soup.select_one(GENERAL_FIELD["passengers"]).get_text(" ", strip=True)
            if soup.select_one(GENERAL_FIELD["passengers"]) else "",
        "nearby": selected_radio("adjStnScdlOfrFlg"),
        "combined": selected_radio("adjStnScdlOfrFlg2"),
        "station_openers": len(soup.select('.tab-tck.type_by .ticketSrchWrap .btn_pop-open')),
        "date_opener": bool(soup.select_one('a[title="출발일"]')),
        "lookup": bool(soup.select_one('.tab-tck.type_by button.btn_lookup')),
    }


class GeneralForm:
    def __init__(self, page: Page):
        self.page = page
        self.step = "초기"

    async def _value(self, selector: str) -> str:
        return (await self.page.locator(selector).first.input_value()).strip()

    async def _wait(self, milliseconds: int = 400) -> None:
        await self.page.wait_for_timeout(milliseconds)

    async def _visible_roots(self, selectors: tuple[str, ...]) -> list[Any]:
        result = []
        for sel in selectors:
            els = self.page.locator(sel)
            for i in range(min(await els.count(), 5)):
                el = els.nth(i)
                if await el.is_visible():
                    result.append(el)
        return result

    async def _station(self, *, origin: bool, station: str) -> None:
        field = GENERAL_FIELD["origin"] if origin else GENERAL_FIELD["destination"]
        self.step = f"{'출발역' if origin else '도착역'} {station} 선택"
        if await self._value(field) == station:
            return
        cls = "start" if origin else "end"
        opener = self.page.locator(f'.tab-tck.type_by .ticketSrchWrap .{cls} a.btn_pop-open').first
        if not await opener.count():
            raise SetupUIError(f"{self.step}: 열기 버튼 없음")
        await opener.click()
        await self._wait(500)

        # Choose only a visible, editable station-search field. Never input into
        # password, hidden, readonly or unrelated account fields.
        selectors = (
            '.type_station_lookup-pop input[type="text"]:visible:not([readonly])',
            '.layerPopup:visible input[type="text"]:visible:not([readonly])',
            '.layerWrap:visible input[type="text"]:visible:not([readonly])',
            'input[placeholder*="역명"]:visible:not([readonly])',
            'input[placeholder*="역"]:visible:not([readonly])',
            'input[placeholder*="검색"]:visible:not([readonly])',
            'input[id="schStr"]:visible:not([readonly])',
        )
        used = None
        for sel in selectors:
            loc = self.page.locator(sel)
            if await loc.count():
                used = loc.last
                break
        if used:
            await used.fill(station)
            await self._wait(650)

        # The list may be a button or a span inside a clickable list item.
        # Restrict to the newly opened popup before trying a document-wide
        # exact-text candidate; always verify the actual form value afterwards.
        roots = await self._visible_roots(POPUP_ROOTS)
        roots.append(self.page.locator("body"))
        for root in roots:
            matches = root.get_by_text(station, exact=True)
            for idx in range(min(await matches.count(), 25)):
                candidate = matches.nth(idx)
                try:
                    if not await candidate.is_visible():
                        continue
                    # Don't click text inside the unchanged search form itself.
                    is_form = await candidate.evaluate(
                        "e => !!e.closest('.tab-tck.type_by .ticketSrchWrap')")
                    if is_form:
                        continue
                    await candidate.click(timeout=1200)
                    await self._wait(450)
                    if await self._value(field) == station:
                        return
                except Exception:
                    continue
        raise SetupUIError(
            f"{self.step}: 팝업에서 역 선택 실패 (클릭 후 열린 역 선택창 HTML 필요)")

    async def _set_zero_time(self) -> bool:
        """Use UI controls if exposed; never mutate React state directly."""
        if TARGET_TIME in await self._value(GENERAL_FIELD["date"]):
            return True
        roots = await self._visible_roots(CALENDAR_ROOTS)
        for root in roots:
            # Native dropdowns with semantic time indicators.
            selects = root.locator("select")
            for i in range(min(await selects.count(), 12)):
                sel = selects.nth(i)
                if not await sel.is_visible():
                    continue
                desc = " ".join((await sel.get_attribute(x) or "") for x in
                                ("id", "name", "title", "aria-label", "class"))
                if not re.search(r"시각|시간|hour|time", desc, re.I):
                    continue
                try:
                    values = await sel.locator("option").evaluate_all(
                        "els => els.map(e => ({value:e.value, text:e.textContent.trim()}))")
                    choices = [v["value"] for v in values if v["value"] in ("0", "00", "00:00")]
                    if choices:
                        await sel.select_option(choices[0])
                        await self._wait(250)
                        if TARGET_TIME in await self._value(GENERAL_FIELD["date"]):
                            return True
                except Exception:
                    pass
            for txt in ("00:00", "0:00", "00시", "오전 12시"):
                locs = root.get_by_text(txt, exact=True)
                if await locs.count() != 1:
                    continue
                one = locs.first
                try:
                    if await one.is_visible():
                        await one.click(timeout=1500)
                        await self._wait(400)
                        if TARGET_TIME in await self._value(GENERAL_FIELD["date"]):
                            return True
                except Exception:
                    pass
        return TARGET_TIME in await self._value(GENERAL_FIELD["date"])

    async def _pick_date_day(self) -> bool:
        # Prefer explicit ISO date attributes. A bare '24' without date scope
        # is only used inside a visible calendar, never the entire page.
        for selector in (
            '[data-date="2026-10-24"]', '[data-value="2026-10-24"]',
            '[data-day="2026-10-24"]',
            '[title*="2026-10-24"]', '[aria-label*="2026-10-24"]',
            '[title*="2026년 10월 24일"]', '[aria-label*="2026년 10월 24일"]',
        ):
            locs = self.page.locator(selector)
            for i in range(min(await locs.count(), 8)):
                candidate = locs.nth(i)
                try:
                    if not await candidate.is_visible():
                        continue
                    await candidate.click(timeout=1500)
                    await self._wait(350)
                    if TARGET_DATE in await self._value(GENERAL_FIELD["date"]):
                        return True
                except Exception:
                    pass
        roots = await self._visible_roots(CALENDAR_ROOTS)
        for root in roots:
            matches = root.get_by_text("24", exact=True)
            visible = []
            for i in range(min(await matches.count(), 30)):
                candidate = matches.nth(i)
                try:
                    if await candidate.is_visible():
                        visible.append(candidate)
                except Exception:
                    pass
            if len(visible) == 1:
                try:
                    await visible[0].click(timeout=1500)
                    await self._wait(350)
                    if TARGET_DATE in await self._value(GENERAL_FIELD["date"]):
                        return True
                except Exception:
                    pass
        return TARGET_DATE in await self._value(GENERAL_FIELD["date"])

    async def _date(self) -> None:
        self.step = "출발일 10월 24일 00:00 선택"
        current = await self._value(GENERAL_FIELD["date"])
        if TARGET_DATE in current and TARGET_TIME in current:
            return
        opener = self.page.locator('.tab-tck.type_by a[title="출발일"]').first
        if not await opener.count():
            raise SetupUIError("출발일 달력 열기 버튼 없음")
        await opener.click()
        await self._wait(350)
        if TARGET_DATE not in await self._value(GENERAL_FIELD["date"]):
            if not await self._pick_date_day():
                raise SetupUIError("달력에서 2026-10-24 선택 실패 (열린 달력 HTML 필요)")
        if TARGET_TIME not in await self._value(GENERAL_FIELD["date"]):
            if not await self._visible_roots(CALENDAR_ROOTS):
                # Date-picker may have closed as soon as the day was clicked.
                await opener.click()
                await self._wait(300)
            if not await self._set_zero_time():
                raise SetupUIError("날짜 선택은 되었지만 출발시각 00:00 설정 실패 (달력의 시간 선택창 HTML 필요)")
        self.step = "출발일 검증"
        result = await self._value(GENERAL_FIELD["date"])
        if TARGET_DATE not in result or TARGET_TIME not in result:
            raise SetupUIError(f"출발일 검증 실패: {result!r}")
        # Optional confirm button, if the picker keeps a modal open.
        for root in await self._visible_roots(CALENDAR_ROOTS):
            for label in ("적용", "확인", "선택완료"):
                b = root.get_by_role("button", name=label, exact=True)
                if await b.count() == 1:
                    try:
                        await b.click(timeout=1000)
                        break
                    except Exception:
                        pass

    async def _radio(self, name: str) -> None:
        self.step = f"옵션 {name}=Y"
        button = self.page.locator('.tab-tck.type_by .optionWrap button.btnToggle').first
        panel = self.page.locator('.tab-tck.type_by .optionWrap .selectAreaWrap.inline').first
        if not await panel.count():
            raise SetupUIError("검색 옵션 패널 없음")
        if not await panel.is_visible():
            await button.click()
            await self._wait(200)
        loc = self.page.locator(f'input[name="{name}"][value="Y"]')
        if await loc.count() != 1:
            raise SetupUIError(f"검색 옵션 {name} 포함 라디오 없음")
        if not await loc.is_checked():
            label = self.page.locator(f'label[for="{await loc.get_attribute("id")}"]')
            if await label.count():
                await label.click()
            else:
                await loc.check()
        if not await loc.is_checked():
            raise SetupUIError(f"검색 옵션 {name} 적용 실패")

    async def configure(self) -> None:
        self.step = "첫 검색 화면 로딩"
        try:
            await self.page.locator(GENERAL_FIELD["origin"]).wait_for(
                state="visible", timeout=14000)
        except PWTimeout as exc:
            raise SetupUIError("일반 검색 화면의 txtGoStart 필드가 로딩되지 않음") from exc
        await self._station(origin=True, station="목포")
        await self._station(origin=False, station="서울")
        await self._date()
        for radio_name in ("adjStnScdlOfrFlg", "adjStnScdlOfrFlg2"):
            await self._radio(radio_name)

        self.step = "검색 조건 최종 검증"
        for field, expected in (("origin", "목포"), ("destination", "서울")):
            value = await self._value(GENERAL_FIELD[field])
            if value != expected:
                raise SetupUIError(f"{field} 불일치: {value}")
        date_value = await self._value(GENERAL_FIELD["date"])
        if TARGET_DATE not in date_value or TARGET_TIME not in date_value:
            raise SetupUIError(f"날짜/출발시각 불일치: {date_value}")
        people = await self.page.locator(GENERAL_FIELD["passengers"]).first.inner_text()
        if not re.fullmatch(r"\s*총\s*1\s*명\s*", people):
            raise SetupUIError(f"인원 1명 아님: {people[:70]}")
        for name in ("adjStnScdlOfrFlg", "adjStnScdlOfrFlg2"):
            if not await self.page.locator(f'input[name="{name}"][value="Y"]').is_checked():
                raise SetupUIError(f"검색 옵션 {name} 검증 실패")

        self.step = "열차 조회 버튼 누르기"
        lookup = self.page.locator('.tab-tck.type_by button.btn_lookup').first
        if await lookup.count() != 1:
            raise SetupUIError("최초 검색 화면의 열차 조회 버튼 없음")
        await lookup.click()
        try:
            await self.page.wait_for_url('**/ticket/search/list', timeout=20000)
        except PWTimeout as exc:
            raise SetupUIError(f"열차 조회 후 결과 페이지 이동 실패, 현재 URL={self.page.url}") from exc

    async def popup_diagnostics(self) -> str:
        """Only short visible UI labels and input metadata; never cookies or tokens."""
        try:
            return str(await self.page.evaluate("""() => {
              const vis = e => {const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                  return r.width>1&&r.height>1&&s.visibility!=='hidden'&&s.display!=='none';};
              const inputs = Array.from(document.querySelectorAll('input,select'))
                .filter(vis).map(e=>({tag:e.tagName,id:e.id,name:e.name,
                  placeholder:e.getAttribute('placeholder')||'',title:e.title||'',
                  value:e.type==='password'?'[hidden]':(e.value||'').slice(0,50)})).slice(-18);
              const overlays = Array.from(document.querySelectorAll('[role=dialog],.layerPopup,.layerWrap,[class*=calendar],[class*=station]'))
                .filter(vis).slice(-6).map(e=>({css:e.className?.toString().slice(0,70),
                  text:(e.innerText||'').trim().slice(0,190)}));
              return {inputs, overlays};
            }"""))[:1200]
        except Exception as exc:
            return f"팝업진단불가:{type(exc).__name__}"
