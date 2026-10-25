"""Tenant-scoped Streamlit dashboard backed only by the control API."""

from __future__ import annotations

import json
import os

import requests
import streamlit as st


API_URL = os.getenv("CONTROL_API_URL", "http://api:8000").rstrip("/")
COOKIE_NAME = "nb_session"


def _token() -> str:
    context = getattr(st, "context", None)
    if context is None:
        st.error("当前 Streamlit 版本不支持安全 Cookie 认证，请升级到 1.37 或更高版本。")
        st.stop()
    return context.cookies.get(COOKIE_NAME, "")


def api(method: str, path: str, **kwargs):
    token = _token()
    if not token:
        st.warning("登录已失效，请返回 Bot 使用 /web 获取新的登录链接。")
        st.stop()
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.request(method, f"{API_URL}{path}", headers=headers, timeout=30, **kwargs)
    except requests.RequestException as exc:
        st.error(f"控制面不可用：{exc}")
        st.stop()
    if response.status_code == 409:
        st.error("任务已在另一页面更新，请刷新后重试。")
        st.stop()
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise RuntimeError(detail)
    return None if response.status_code == 204 else response.json()


def task_payload(task: dict) -> dict:
    ignored = {
        "id", "tenant_id", "status", "revision", "pending_action",
        "resolved_sources", "resolved_destinations", "offsets", "last_error",
        "created_at", "updated_at",
    }
    return {key: value for key, value in task.items() if key not in ignored}


def task_editor(task: dict | None = None) -> None:
    value = task_payload(task) if task else {
        "name": "新转发任务",
        "mode": "live",
        "sources": [],
        "destinations": [],
        "show_forwarded_from": False,
        "comments": {},
        "bot_media": {},
        "live": {},
        "past": {},
        "schedule": {},
        "plugins": {},
        "watermark_asset_id": None,
    }
    key = task["id"] if task else "new"
    with st.form(f"task-form-{key}"):
        name = st.text_input("任务名称", value=value.get("name", ""), key=f"name-{key}")
        mode = st.selectbox(
            "运行模式", ["live", "past", "schedule"],
            index=["live", "past", "schedule"].index(value.get("mode", "live")),
            key=f"mode-{key}",
        )
        sources = st.text_area("来源（每行一个）", value="\n".join(value.get("sources", [])), key=f"src-{key}")
        destinations = st.text_area("目标（每行一个）", value="\n".join(value.get("destinations", [])), key=f"dst-{key}")
        advanced = dict(value)
        for field in ["name", "mode", "sources", "destinations"]:
            advanced.pop(field, None)
        raw_advanced = st.text_area(
            "全部插件与高级配置（JSON）",
            value=json.dumps(advanced, ensure_ascii=False, indent=2),
            height=360,
            key=f"advanced-{key}",
        )
        submitted = st.form_submit_button("保存", type="primary")
        if submitted:
            try:
                payload = json.loads(raw_advanced or "{}")
                payload.update(
                    {
                        "name": name,
                        "mode": mode,
                        "sources": [line.strip() for line in sources.splitlines() if line.strip()],
                        "destinations": [line.strip() for line in destinations.splitlines() if line.strip()],
                    }
                )
                if task:
                    api("PUT", f"/api/tasks/{task['id']}", json={"revision": task["revision"], "task": payload})
                else:
                    api("POST", "/api/tasks", json=payload)
                st.success("任务已保存")
                st.rerun()
            except (ValueError, RuntimeError) as exc:
                st.error(str(exc))


st.set_page_config(page_title="NB 多租户转发", page_icon="✈️", layout="wide")
st.title("NB 多租户转发")

try:
    me = api("GET", "/api/me")
except RuntimeError as exc:
    me = None
    st.error(f"认证失败：{exc}")

try:
    account = api("GET", "/api/account")
except RuntimeError as exc:
    account = None
    st.warning(f"尚未绑定 Telegram 用户账号：{exc}")

if me:
    st.caption(f"租户 ID：{me['tenant_id']}" + (" · 管理员" if me.get("is_admin") else ""))
if account:
    st.info(f"账号：{account.get('display_name') or account.get('username') or account['tenant_id']} · {account['status']}")

new_tab, tasks_tab, account_tab = st.tabs(["新建任务", "任务列表", "账号"])
with new_tab:
    if account:
        task_editor()
    else:
        st.write("请先返回 Bot 使用 /login 绑定账号。")

with tasks_tab:
    tasks = api("GET", "/api/tasks") if me else []
    if not tasks:
        st.info("暂无任务")
    for task in tasks:
        with st.expander(f"{task['name']} · {task['status']} · {task['mode']}"):
            if task.get("last_error"):
                st.error(task["last_error"])
            task_editor(task)
            cols = st.columns(5)
            for column, action, label in zip(
                cols,
                ["validate", "run", "pause", "watermark", "delete"],
                ["校验", "运行", "暂停", "上传水印", "删除"],
            ):
                with column:
                    if action == "watermark":
                        image = st.file_uploader(label, type=["png"], key=f"wm-{task['id']}")
                        if image and st.button("确认上传", key=f"wm-submit-{task['id']}"):
                            try:
                                api(
                                    "POST",
                                    f"/api/tasks/{task['id']}/watermark",
                                    files={"file": (image.name, image.getvalue(), "image/png")},
                                )
                                st.success("水印已上传")
                                st.rerun()
                            except RuntimeError as exc:
                                st.error(str(exc))
                    elif st.button(label, key=f"{action}-{task['id']}"):
                        try:
                            if action == "delete":
                                api("DELETE", f"/api/tasks/{task['id']}")
                            else:
                                api("POST", f"/api/tasks/{task['id']}/{action}")
                            st.rerun()
                        except RuntimeError as exc:
                            st.error(str(exc))

with account_tab:
    if account and st.button("解绑并删除账号", type="primary"):
        try:
            api("DELETE", "/api/account")
            st.success("账号正在安全退出并清理数据。")
        except RuntimeError as exc:
            st.error(str(exc))

if me and me.get("is_admin"):
    st.divider()
    st.subheader("平台管理")
    tenants_tab, workers_tab, errors_tab = st.tabs(["租户", "Worker", "系统错误"])
    with tenants_tab:
        st.dataframe(api("GET", "/api/admin/tenants"), use_container_width=True)
    with workers_tab:
        st.dataframe(api("GET", "/api/admin/workers"), use_container_width=True)
    with errors_tab:
        st.dataframe(api("GET", "/api/admin/errors"), use_container_width=True)
