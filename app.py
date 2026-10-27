"""쇼핑 리스트 앱 (Streamlit).

위젯 key 규칙: 아이템별 위젯은 반드시 아이템 id를 포함해 고유하게 짓는다.
    체크박스  f"check_{id}"
    수정 버튼 f"edit_{id}"
    삭제 버튼 f"del_{id}"
    편집 입력 f"edit_name_{id}", f"edit_qty_{id}" / 저장·취소 f"save_{id}", f"cancel_{id}"
같은 이름의 아이템이 여러 개일 수 있으므로 이름이나 리스트 인덱스는 key로 쓰지 않는다.

모든 상태 변경은 위젯의 on_click / on_change 콜백에서 처리한다.
콜백 안에서 만든 안내 메시지는 session_state에 담아 두었다가 렌더링 단계에서 표시한다.

data.json이 단일 원본이다. 여러 탭·기기가 같은 파일을 쓰므로, 변경은 항상
"잠금 → 최신 파일 읽기 → id 기준으로 변경 → 저장" 순서(mutate)로 하고,
매 실행마다 파일을 다시 읽어 화면에 반영한다.
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

MAX_NAME_LEN = 50
MIN_QTY = 1
MAX_QTY = 99
DATA_FILE = Path(__file__).parent / "data.json"
MD_SPECIAL = set("\\`*_{}[]()#+-.!|~<>$:")
KST = timezone(timedelta(hours=9), "KST")  # 한국은 서머타임이 없어 고정 오프셋으로 충분
# 행이 재정렬·삭제되면 같은 좌표에 다른 행이 들어오므로, 이 시간 안의 연속 클릭은 무시한다.
CLICK_COOLDOWN = 0.5
# 추가 직후 이 시간 안에 들어온 빈 제출(추가 버튼 연타)은 경고 없이 무시한다.
EMPTY_SUBMIT_GRACE = 1.0


def now_kst() -> datetime:
    return datetime.now(KST)


# ---------------------------------------------------------------------------
# 저장소 (data.json)
# ---------------------------------------------------------------------------

@st.cache_resource
def file_lock() -> threading.Lock:
    """모든 세션이 공유하는 파일 잠금 (스크립트가 재실행돼도 같은 객체)."""
    return threading.Lock()


def quarantine_corrupt_file(reason: str) -> None:
    """읽을 수 없는 data.json을 옆으로 옮겨 보존하고 사용자에게 알린다."""
    backup = DATA_FILE.with_name(f"data.json.corrupt-{now_kst():%Y%m%d-%H%M%S}")
    try:
        os.replace(DATA_FILE, backup)
        where = f" 원본은 {backup.name}에 보관했어요."
    except OSError:
        where = ""
    st.session_state["flash"] = (
        "error", f"저장된 데이터를 읽지 못해 빈 리스트로 시작해요 ({reason}).{where}"
    )


def load_items() -> list[dict]:
    """data.json을 읽는다. 파일이 없으면 빈 리스트, 손상되었으면 원본을 보존한 뒤 빈 리스트."""
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except OSError:
        return []
    except ValueError:
        quarantine_corrupt_file("JSON 형식 오류")
        return []
    if not isinstance(data, list):
        quarantine_corrupt_file("리스트 형식이 아님")
        return []
    items = []
    for raw in data:
        try:
            items.append({
                "id": str(raw["id"]),
                "name": str(raw["name"]),
                "quantity": int(raw["quantity"]),
                "checked": bool(raw["checked"]),
                "created_at": str(raw["created_at"]),
            })
        except (KeyError, TypeError, ValueError):
            continue  # 형식이 맞지 않는 항목은 건너뛴다
    return items


def save_items(items: list[dict]) -> None:
    """임시 파일에 쓴 뒤 교체해, 저장 도중 중단돼도 파일이 깨지지 않게 한다."""
    tmp = DATA_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, DATA_FILE)
    except OSError:
        st.session_state["flash"] = ("error", "저장에 실패했어요. 새로고침하면 변경 내용이 사라질 수 있어요.")


def mutate(change) -> list[dict]:
    """최신 파일을 읽어 change(items) -> items 를 적용하고 저장한다.

    다른 탭에서 저장한 내용을 덮어쓰지 않도록, 세션에 들고 있던 리스트가 아니라
    항상 파일의 최신 내용을 기준으로 변경한다.
    """
    with file_lock():
        items = change(load_items())
        save_items(items)
    st.session_state["items"] = items
    return items


# ---------------------------------------------------------------------------
# 상태
# ---------------------------------------------------------------------------

def init_state() -> None:
    """session_state 기본값을 설정하고, 매 실행마다 data.json의 최신 내용을 불러온다."""
    st.session_state["items"] = load_items()
    st.session_state.setdefault("editing_id", None)
    st.session_state.setdefault("confirm_clear", False)
    st.session_state.setdefault("flash", None)
    st.session_state.setdefault("edit_error", None)
    st.session_state.setdefault("undo", None)
    st.session_state.setdefault("new_name", "")
    st.session_state.setdefault("new_qty", 1)


def make_item(name: str, quantity: int = 1) -> dict:
    """PRD 5장 Item 구조를 만든다."""
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "quantity": quantity,
        "checked": False,
        "created_at": now_kst().isoformat(timespec="seconds"),
    }


def find_item(item_id: str, items: list[dict] | None = None) -> dict | None:
    if items is None:
        items = st.session_state["items"]
    return next((i for i in items if i["id"] == item_id), None)


def has_duplicate(items: list[dict], name: str, exclude_id: str | None = None) -> bool:
    """같은 이름의 미체크 아이템이 있는지 (PRD 4.1)."""
    return any(
        i["name"] == name and not i["checked"] and i["id"] != exclude_id for i in items
    )


def validate(name, quantity) -> tuple[str, int, str | None]:
    """(정리된 이름, 수량, 에러 메시지 또는 None)을 돌려준다."""
    name = (name or "").strip()
    if not name:
        return name, 0, "아이템명을 입력해 주세요."
    if len(name) > MAX_NAME_LEN:
        return name, 0, f"아이템명은 {MAX_NAME_LEN}자 이하로 입력해 주세요. (현재 {len(name)}자)"
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        return name, 0, "수량은 숫자로 입력해 주세요."
    if not MIN_QTY <= quantity <= MAX_QTY:
        return name, 0, f"수량은 {MIN_QTY}~{MAX_QTY} 사이로 입력해 주세요."
    return name, quantity, None


def in_cooldown(action: str, seconds: float = CLICK_COOLDOWN) -> bool:
    """직전 같은 종류의 동작 후 seconds가 지나지 않았으면 True. 아니면 시각을 기록한다."""
    key = f"_last_{action}"
    now = time.monotonic()
    if now - st.session_state.get(key, 0.0) < seconds:
        return True
    st.session_state[key] = now
    return False


def reset_transient() -> None:
    """다른 동작이 일어나면 열려 있던 '전체 삭제' 확인을 닫는다."""
    st.session_state["confirm_clear"] = False


# ---------------------------------------------------------------------------
# 상태 변경 함수 (콜백)
# 모두 id 기반으로 동작하며, 리스트를 순회하면서 원본을 수정하지 않는다.
# ---------------------------------------------------------------------------

def add_item() -> None:
    reset_transient()
    raw_name = st.session_state.get("new_name")
    if not (raw_name or "").strip() and (
        time.monotonic() - st.session_state.get("_last_add", 0.0) < EMPTY_SUBMIT_GRACE
    ):
        return  # 추가 버튼 연타로 들어온 빈 제출
    name, quantity, error = validate(raw_name, st.session_state.get("new_qty"))
    if error:
        st.session_state["flash"] = ("warning", error)
        return  # 입력값을 그대로 남겨 바로 고칠 수 있게 한다
    duplicate = False

    def change(items):
        nonlocal duplicate
        duplicate = has_duplicate(items, name)
        return items + [make_item(name, quantity)]

    mutate(change)
    st.session_state["_last_add"] = time.monotonic()
    st.session_state["undo"] = None
    st.session_state["new_name"] = ""
    st.session_state["new_qty"] = 1
    if duplicate:
        st.session_state["flash"] = ("info", f"'{name}'은(는) 이미 리스트에 있어요. 그래도 추가했어요.")


def start_edit(item_id: str) -> None:
    reset_transient()
    item = find_item(item_id, load_items())
    if item is None:
        return
    st.session_state["editing_id"] = item_id
    st.session_state["edit_error"] = None
    # 편집 입력창을 현재 값으로 채운다 (이전에 취소한 입력이 남지 않도록)
    st.session_state[f"edit_name_{item_id}"] = item["name"]
    st.session_state[f"edit_qty_{item_id}"] = item["quantity"]


def update_item(item_id: str) -> None:
    name, quantity, error = validate(
        st.session_state.get(f"edit_name_{item_id}"),
        st.session_state.get(f"edit_qty_{item_id}"),
    )
    if error:
        st.session_state["edit_error"] = error
        return
    found = duplicate = False

    def change(items):
        nonlocal found, duplicate
        item = find_item(item_id, items)
        if item is not None:
            found = True
            duplicate = has_duplicate(items, name, exclude_id=item_id)
            item["name"] = name
            item["quantity"] = quantity
        return items

    mutate(change)
    cancel_edit()
    st.session_state["undo"] = None
    if not found:
        st.session_state["flash"] = ("warning", "다른 곳에서 이미 삭제된 아이템이에요.")
    elif duplicate:
        st.session_state["flash"] = ("info", f"'{name}'은(는) 이미 리스트에 있어요. 그래도 저장했어요.")


def cancel_edit() -> None:
    st.session_state["editing_id"] = None
    st.session_state["edit_error"] = None


def remove_items(predicate, toast: str) -> None:
    """predicate에 맞는 아이템을 지우고, 되돌리기용으로 보관한다."""
    removed: list[dict] = []

    def change(items):
        removed.extend(i for i in items if predicate(i))
        return [i for i in items if not predicate(i)]

    items = mutate(change)
    reset_transient()
    if find_item(st.session_state["editing_id"] or "", items) is None:
        cancel_edit()
    if removed:
        st.session_state["undo"] = removed
        st.toast(toast, icon="🗑️")


def delete_item(item_id: str) -> None:
    if in_cooldown("row_click"):
        return  # 연속 클릭으로 아래 행이 지워지는 것을 막는다
    remove_items(lambda i: i["id"] == item_id, "삭제되었습니다")


def toggle_item(item_id: str) -> None:
    # 무시한 클릭은 다음 렌더링에서 체크박스가 파일 값으로 되돌려진다.
    if in_cooldown("row_click"):
        return
    reset_transient()
    checked = bool(st.session_state.get(f"check_{item_id}"))

    def change(items):
        item = find_item(item_id, items)
        if item is not None:
            item["checked"] = checked
        return items

    mutate(change)


def clear_checked() -> None:
    remove_items(lambda i: i["checked"], "체크된 항목을 삭제했어요")


def request_clear_all() -> None:
    st.session_state["confirm_clear"] = True


def cancel_clear_all() -> None:
    st.session_state["confirm_clear"] = False


def clear_all() -> None:
    remove_items(lambda i: True, "전체 삭제했어요")


def undo_delete() -> None:
    restored = st.session_state.get("undo") or []

    def change(items):
        existing = {i["id"] for i in items}
        return items + [i for i in restored if i["id"] not in existing]

    mutate(change)
    reset_transient()
    st.session_state["undo"] = None
    st.toast(f"{len(restored)}개를 되돌렸어요", icon="↩️")


# ---------------------------------------------------------------------------
# 화면
# ---------------------------------------------------------------------------

def escape_md(text: str) -> str:
    """사용자 입력이 마크다운/HTML로 해석되지 않도록 특수문자를 이스케이프한다."""
    return "".join("\\" + c if c in MD_SPECIAL else c for c in text)


WEEKDAYS = "월화수목금토일"
QTY_WIDTH = 96

# 커스텀 CSS는 여기 한 곳에만 둔다. Streamlit 해시 클래스(.st-emotion-cache-*, .css-*) 대신
# data-testid와 st.container(key=...)가 만드는 .st-key-* 클래스만 사용한다.
CSS = """
<style>
/* 입력 폼·아이템 행은 좁은 화면에서도 한 줄 유지 (이름 칸이 줄어들며 줄바꿈) */
.st-key-add_row, [class*="st-key-row_"] {
    flex-wrap: nowrap;
}
/* 아이템 행: 행 사이 얇은 구분선 */
[class*="st-key-row_"] {
    padding: 0.15rem 0;
    border-bottom: 1px solid rgba(128, 128, 128, 0.18);
}
/* 긴 단어가 행을 밀어내지 않도록 이름 영역 줄바꿈 */
[class*="st-key-row_"] [data-testid="stMarkdown"] {
    min-width: 0;
    overflow-wrap: anywhere;
}
/* 아이콘 버튼 터치 영역 */
[class*="st-key-row_"] [data-testid="stButton"] button {
    min-width: 2.6rem;
    min-height: 2.6rem;
    padding: 0 0.5rem;
}
/* 편집 중인 행 강조 */
[class*="st-key-editbox_"] {
    background: rgba(61, 190, 132, 0.08);
}
</style>
"""


def render_header() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
    today = now_kst()
    st.title("🛒 쇼핑 리스트", anchor=False)
    st.caption(f"{today.month}월 {today.day}일 ({WEEKDAYS[today.weekday()]}) · 오늘 살 것들")


def render_add_form() -> None:
    # 입력값 초기화는 add_item이 성공했을 때만 직접 한다 (실패 시 입력 유지).
    # 수량 범위는 위젯 min/max 대신 validate()로 검사한다. 위젯 min/max를 쓰면
    # 범위 밖 입력이 서버로 전달되지 않고 이전 값으로 조용히 저장된다.
    # 이름 길이도 max_chars 대신 validate()로 검사한다 (긴 붙여넣기가 통째로 무시되지 않도록).
    with st.form("add_form", clear_on_submit=False, border=False):
        with st.container(horizontal=True, vertical_alignment="bottom", gap="small",
                          key="add_row"):
            st.text_input(
                "아이템명", key="new_name",
                placeholder="살 물건을 입력하세요", label_visibility="collapsed",
                width="stretch",
            )
            st.number_input(
                "수량", key="new_qty", step=1, format="%d",
                label_visibility="collapsed", width=QTY_WIDTH,
            )
            st.form_submit_button(
                "추가", on_click=add_item, type="primary",
                icon=":material/add:", width="content",
            )

    flash = st.session_state["flash"]
    if flash:
        kind, message = flash
        getattr(st, kind)(message)
        st.session_state["flash"] = None

    undo = st.session_state["undo"]
    if undo:
        label = f"'{undo[0]['name']}'" if len(undo) == 1 else f"아이템 {len(undo)}개"
        with st.container(horizontal=True, vertical_alignment="center", gap="small",
                          key="undo_row"):
            st.caption(f"{escape_md(label)}를 삭제했어요.", width="stretch")
            st.button("되돌리기", key="undo_btn", on_click=undo_delete,
                      icon=":material/undo:", type="tertiary")


def render_progress() -> None:
    items = st.session_state["items"]
    if not items:
        return
    total = len(items)
    done = sum(i["checked"] for i in items)
    st.progress(done / total, text=f"**{done} / {total}** 완료")
    if done == total:
        st.success("**장보기 완료!** 모든 물건을 담았어요.", icon="🎉")


def render_item_row(item: dict) -> None:
    item_id = item["id"]
    if st.session_state["editing_id"] == item_id:
        with st.container(border=True, key=f"editbox_{item_id}"):
            with st.container(horizontal=True, vertical_alignment="center", gap="small"):
                st.text_input(
                    "아이템명", key=f"edit_name_{item_id}",
                    label_visibility="collapsed", width="stretch",
                )
                st.number_input(
                    "수량", key=f"edit_qty_{item_id}", step=1, format="%d",
                    label_visibility="collapsed", width=QTY_WIDTH,
                )
            with st.container(horizontal=True, horizontal_alignment="right", gap="small"):
                st.button("취소", key=f"cancel_{item_id}", on_click=cancel_edit)
                st.button("저장", key=f"save_{item_id}", on_click=update_item, args=(item_id,),
                          type="primary", icon=":material/check:")
            if st.session_state["edit_error"]:
                st.warning(st.session_state["edit_error"])
        return

    # 체크박스 표시값은 항상 파일 값을 따른다 (다른 탭의 변경, 무시된 연속 클릭 반영).
    st.session_state[f"check_{item_id}"] = item["checked"]
    name = escape_md(item["name"])
    with st.container(horizontal=True, vertical_alignment="center", gap="small",
                      key=f"row_{item_id}"):
        st.checkbox(
            item["name"], key=f"check_{item_id}",
            on_change=toggle_item, args=(item_id,), label_visibility="collapsed",
            width="content",
        )
        qty = f"x{item['quantity']}"
        if item["checked"]:
            st.markdown(f":gray[~~{name}~~]", width="stretch")
            st.markdown(f":gray[{qty}]", width="content")
        else:
            st.markdown(name, width="stretch")
            st.markdown(f"**{qty}**", width="content")
        st.button("✏️", key=f"edit_{item_id}", on_click=start_edit, args=(item_id,),
                  help=f"{name} 수정", type="tertiary")
        st.button("🗑️", key=f"del_{item_id}", on_click=delete_item, args=(item_id,),
                  help=f"{name} 삭제", type="tertiary")


def render_item_list() -> None:
    items = st.session_state["items"]
    if not items:
        with st.container(border=True):
            st.space("small")
            st.markdown("리스트가 비어 있어요. 살 물건을 추가해 보세요 🛒",
                        text_alignment="center")
            st.space("small")
        return
    # 미체크 → 체크 순. sorted는 안정 정렬이라 created_at이 같으면 추가 순서가 유지된다.
    for item in sorted(items, key=lambda i: (i["checked"], i["created_at"])):
        render_item_row(item)


def render_footer_actions() -> None:
    items = st.session_state["items"]
    if not items:
        st.session_state["confirm_clear"] = False
        return
    st.space("small")
    if st.session_state["confirm_clear"]:
        st.warning(f"아이템 {len(items)}개를 모두 삭제할까요?",
                   icon=":material/warning:")
        with st.container(horizontal=True, horizontal_alignment="right", gap="small"):
            st.button("취소", key="confirm_clear_no", on_click=cancel_clear_all)
            st.button(":red[삭제 확인]", key="confirm_clear_yes", on_click=clear_all,
                      icon=":material/delete_forever:")
        return
    with st.container(horizontal=True, horizontal_alignment="distribute", gap="small"):
        st.button(
            "체크된 항목 삭제", key="clear_checked", on_click=clear_checked,
            disabled=not any(i["checked"] for i in items), icon=":material/playlist_remove:",
        )
        st.button(":red[전체 삭제]", key="clear_all", on_click=request_clear_all,
                  icon=":material/delete_forever:", type="tertiary")


def main() -> None:
    st.set_page_config(page_title="쇼핑 리스트", page_icon="🛒", layout="centered")
    init_state()
    if st.session_state["editing_id"] and find_item(st.session_state["editing_id"]) is None:
        cancel_edit()  # 다른 탭에서 지워진 아이템을 편집 중이었으면 편집 종료
    render_header()
    render_add_form()
    render_progress()
    render_item_list()
    render_footer_actions()


main()
