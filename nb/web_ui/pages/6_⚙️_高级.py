import os
import json
import streamlit as st
from nb.const import FAILED_FORWARD_LOG_FILE_NAME
from nb.config import CONFIG_FILE_NAME, read_config
from nb.utils import platform_info
from nb.web_ui.password import check_password
from nb.web_ui.utils import switch_theme

CONFIG = read_config()


def _rerun():
    if hasattr(st, "rerun"):
        st.rerun()
    elif hasattr(st, "experimental_rerun"):
        st.experimental_rerun()


def _load_failed_records():
    records = []
    if not os.path.exists(FAILED_FORWARD_LOG_FILE_NAME):
        return records

    try:
        with open(FAILED_FORWARD_LOG_FILE_NAME, "r", encoding="utf8") as file:
            for line_no, line in enumerate(file, start=1):
                raw = line.strip()
                if not raw:
                    continue
                try:
                    records.append(json.loads(raw))
                except json.JSONDecodeError:
                    records.append(
                        {
                            "timestamp": "",
                            "mode": "invalid-record",
                            "source_chat_id": None,
                            "source_message_id": None,
                            "dest_chat_ids": [],
                            "grouped_message_ids": [],
                            "reason": f"第 {line_no} 行 JSON 解析失败",
                            "details": {"raw": raw},
                        }
                    )
    except Exception as e:
        st.error(f"读取失败记录失败: {e}")
    return records


def _clear_failed_records():
    try:
        if os.path.exists(FAILED_FORWARD_LOG_FILE_NAME):
            os.remove(FAILED_FORWARD_LOG_FILE_NAME)
    except Exception as e:
        st.error(f"清空失败记录失败: {e}")
        return
    st.success("失败记录已清空")
    _rerun()

st.set_page_config(page_title="高级设置", page_icon="🔬", layout="wide")
switch_theme(st, CONFIG)

if check_password(st):
    st.title("高级设置")
    
    st.markdown("""
    <div class="glass-card" style="border-left: 4px solid #f59e0b; margin-bottom: 20px;">
        <span style="font-size: 1.2rem; margin-right: 10px;">⚠️</span>
        <strong>警告：</strong> 此页面允许直接访问原始配置。请谨慎操作。
    </div>
    """, unsafe_allow_html=True)

    if st.checkbox("我了解风险"):
        
        with st.expander("系统信息"):
            st.code(platform_info())

        with st.expander("原始配置 (JSON)"):
            with open(CONFIG_FILE_NAME, "r") as file:
                # 兼容 Pydantic v2 dump 后的 JSON
                data = json.loads(file.read())
                dumped = json.dumps(data, indent=3)
            
            c1, c2 = st.columns([1, 3])
            with c1:
                st.download_button(
                    "📥 下载配置", 
                    data=dumped, 
                    file_name=CONFIG_FILE_NAME,
                    use_container_width=True
                )
            st.json(data)

        with st.expander("失败消息记录"):
            records = _load_failed_records()
            record_count = len(records)

            if not records:
                st.info("当前没有失败消息记录。")
            else:
                grouped_count = sum(1 for item in records if item.get("mode") == "past-grouped")
                single_count = sum(1 for item in records if item.get("mode") == "past-single")

                c1, c2, c3 = st.columns(3)
                c1.metric("失败记录总数", record_count)
                c2.metric("单条失败", single_count)
                c3.metric("媒体组失败", grouped_count)

                try:
                    with open(FAILED_FORWARD_LOG_FILE_NAME, "r", encoding="utf8") as file:
                        raw_dump = file.read()
                except Exception:
                    raw_dump = ""

                a1, a2, a3 = st.columns([1, 1, 2])
                with a1:
                    st.download_button(
                        "📥 下载失败记录",
                        data=raw_dump,
                        file_name=FAILED_FORWARD_LOG_FILE_NAME,
                        use_container_width=True,
                    )
                with a2:
                    if st.button("🗑️ 清空失败记录", use_container_width=True):
                        _clear_failed_records()
                with a3:
                    slider_max = min(200, record_count)
                    slider_step = 5 if slider_max >= 5 else 1
                    max_items = st.slider(
                        "显示最近记录数",
                        min_value=1,
                        max_value=slider_max,
                        value=min(20, record_count),
                        step=slider_step,
                    )

                latest_records = list(reversed(records))[:max_items]
                table_rows = []
                for item in latest_records:
                    dest_ids = item.get("dest_chat_ids") or []
                    grouped_ids = item.get("grouped_message_ids") or []
                    table_rows.append(
                        {
                            "时间": item.get("timestamp", ""),
                            "类型": item.get("mode", ""),
                            "源频道": item.get("source_chat_id", ""),
                            "源消息": item.get("source_message_id", ""),
                            "目标数": len(dest_ids),
                            "媒体组条数": len(grouped_ids),
                            "原因": item.get("reason", ""),
                        }
                    )

                st.dataframe(table_rows, use_container_width=True, hide_index=True)

                selected_index = st.selectbox(
                    "查看详细记录",
                    options=range(len(latest_records)),
                    format_func=lambda idx: (
                        f"{latest_records[idx].get('timestamp', '')} | "
                        f"{latest_records[idx].get('mode', '')} | "
                        f"msg={latest_records[idx].get('source_message_id', '')}"
                    ),
                )
                st.json(latest_records[selected_index])
