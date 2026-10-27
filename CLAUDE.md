# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A lightweight Streamlit shopping-list app (add / edit / delete / check items), to be deployed to Streamlit Community Cloud. UI text and docs are in Korean.

- `PRD.md` — source of truth for features, data model, acceptance criteria (section 10), milestones (M1–M4).
- `PROMPTS.md` — the 4-stage build plan (skeleton → features → UI → Playwright review). All four stages are done.
- `REVIEW.md` — stage-4 review: bug list (BUG-1…8) with repro steps, and section 6 recording how each was fixed. Check it before changing related behavior so fixed bugs don't come back.

Status: M1–M2 done and reviewed; **M3 (GitHub + Streamlit Cloud deploy) is next** — the folder is not a git repo yet. Requires Python 3.10+ (`X | None` syntax). Theme (single green accent, separate light/dark palettes) lives in `.streamlit/config.toml` and needs a server restart to take effect.

Folders: `screenshots/` is review evidence referenced by `REVIEW.md` (keep). `.playwright-mcp/` is Playwright scratch output (gitignored, safe to delete).

## Commands

```bash
pip install -r requirements.txt
python -m streamlit run app.py                                            # local dev (`streamlit` is not on PATH here)
python -m streamlit run app.py --server.headless true --server.port 8501  # for Playwright testing
```

There is no test suite or linter. Verify in two ways:
- Headless: `streamlit.testing.v1.AppTest.from_file(<absolute path to app.py>).run()` (relative paths resolve against the calling file; set `PYTHONIOENCODING=utf-8` when printing — the console is cp949). Before simulating rapid row clicks, reset `at.session_state["_last_row_click"] = 0.0` (and `_last_add`), or the click cooldown will swallow them.
- Browser: Playwright MCP against http://localhost:8501. Buttons → `.st-key-<widget key> button:visible` (Streamlit renders a hidden duplicate when `help=` is set); checkboxes → click `.st-key-check_<id> [data-testid="stCheckbox"]`, not the hidden `<input>`. Wait ~1 s after each action for the rerun. To regression-test BUG-1/2, use `page.mouse.click(x, y)` twice at the same coordinates 250–400 ms apart — Playwright's `dblclick()` is faster than a rerun and does not reproduce the bug.

Tests write `data.json` in the project folder — back it up first and restore it afterwards.

## Architecture

Single file `app.py`, dependency `streamlit` only. Sections: storage → state → callbacks → rendering → `main()`.

- **`data.json` is the source of truth, not `session_state`.** Several tabs/devices (and all Cloud visitors) share the file. `init_state()` reloads it on every script run, and every change goes through `mutate(change)`: take the `st.cache_resource` lock → `load_items()` fresh → apply the change by id → `save_items()`. Never save the session's copy of the list directly — that overwrites other tabs' changes (BUG-5).
- Checkbox display state is re-synced from the file before each render (`st.session_state[f"check_{id}"] = item["checked"]`), so checkboxes are created **without** `value=`.
- Row clicks (delete/toggle) share a 0.5 s cooldown (`in_cooldown("row_click")`) because rows reflow under the cursor after each rerun; deletes are kept in `session_state["undo"]` for the "되돌리기" button.
- Messages set inside callbacks go into `session_state["flash"]` (shown under the add form) or `edit_error` (shown in the edit box).
- Times use fixed KST (UTC+9) via `now_kst()` — Cloud servers run in UTC.
- A corrupt `data.json` is moved aside to `data.json.corrupt-<timestamp>`, never silently overwritten.

## Streamlit rules that matter here

- Mutate state **only in `on_click` / `on_change` callbacks**; no `st.rerun()`.
- Access state with brackets — `st.session_state["items"]`, never `.items` (resolves to the dict method).
- Widget keys contain the item id (`check_`, `edit_`, `del_`, `edit_name_`, `edit_qty_`, `save_`, `cancel_`, `row_`, `editbox_`); names can repeat, so never key by name or index. A widget key must not reuse a plain session_state data key (e.g. the undo button is `undo_btn`, the data is `undo`).
- Validate in `validate()`, not with widget limits: don't add `min_value`/`max_value` to `number_input` or `max_chars` to `text_input` — out-of-range input then never reaches the server and is silently replaced (BUG-3, BUG-7). Rules: name 1–50 chars after strip, quantity integer 1–99.
- The add form does not use `clear_on_submit`; `add_item` clears `new_name`/`new_qty` itself only on success, so invalid input stays editable. Initialize widget keys with `setdefault` in `init_state()` instead of passing `value=`.
- Display order: unchecked first, then checked, each by `created_at` (stable sort keeps insertion order within the same second).
- Custom CSS lives in the single `CSS` block and targets `data-testid` or `.st-key-*` classes only, never hashed `.st-emotion-cache-*` classes. Layout uses `st.container(horizontal=True)` rows (not `st.columns`, which stack on mobile) with `flex-wrap: nowrap`.
