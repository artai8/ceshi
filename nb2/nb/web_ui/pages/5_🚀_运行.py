# nb/web_ui/pages/5_🏃_Run.py

import os
import signal
import subprocess
import sys
import time
import html
import logging

import streamlit as st
import streamlit.components.v1 as components
try:
    from streamlit_autorefresh import st_autorefresh
except Exception:
    def st_autorefresh(*args, **kwargs):
        return None
from nb.config import CONFIG, read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import switch_theme

CONFIG = read_config()

PID_FILE = os.path.join(os.getcwd(), "nb.pid")
LOG_FILE = os.path.join(os.getcwd(), "logs.txt")
OLD_LOG_FILE = os.path.join(os.getcwd(), "old_logs.txt")
LOG_MAX_BYTES = 1024 * 1024

# --- Process Utils ---
def rerun():
    if hasattr(st, 'rerun'): st.rerun()
    elif hasattr(st, 'experimental_rerun'): st.experimental_rerun()
    else: st.warning("Refresh needed")

def _read_pid_file() -> int:
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r") as f:
                s = f.read().strip()
                if s: return int(s)
    except (OSError, ValueError):
        return 0
    return 0

def _write_pid_file(pid: int):
    with open(PID_FILE, "w") as f: f.write(str(pid))

def _remove_pid_file():
    if os.path.exists(PID_FILE):
        try:
            os.remove(PID_FILE)
        except OSError:
            pass

def is_process_alive(pid: int) -> bool:
    if pid <= 0: return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError: return False
    except PermissionError: return True
    except OSError: return False

def get_running_pid() -> int:
    f_pid = _read_pid_file()
    c_pid = CONFIG.pid
    if f_pid > 0 and is_process_alive(f_pid):
        if c_pid != f_pid:
            CONFIG.pid = f_pid
            write_config(CONFIG)
        return f_pid
    if c_pid > 0 and is_process_alive(c_pid):
        _write_pid_file(c_pid)
        return c_pid
    if f_pid > 0 or c_pid > 0:
        _remove_pid_file()
        if c_pid > 0:
            CONFIG.pid = 0
            write_config(CONFIG)
    return 0

def _trim_log_file(path: str) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            with open(path, "rb") as f:
                f.seek(-LOG_MAX_BYTES, os.SEEK_END)
                data = f.read()
            with open(path, "wb") as f:
                f.write(data)
    except Exception:
        pass

def _read_log_tail(path: str, max_lines: int = 100) -> str:
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                lines = f.readlines()
                return "".join(lines[-max_lines:]) if lines else ""
    except OSError:
        pass
    return ""

def _kill_posix(pid: int, force: bool) -> bool:
    if not is_process_alive(pid):
        _remove_pid_file()
        return True
    try:
        if force:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.killpg(pid, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
        except Exception:
            pass
    time.sleep(2 if not force else 1)
    if not is_process_alive(pid):
        _remove_pid_file()
        return True
    if not force:
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        time.sleep(1)
    res = not is_process_alive(pid)
    if res:
        _remove_pid_file()
    return res

def _kill_windows(pid: int, force: bool) -> bool:
    if not is_process_alive(pid):
        _remove_pid_file()
        return True
    flag = "/F" if force else ""
    try:
        os.system(f"taskkill /PID {pid} /T {flag}")
    except Exception:
        pass
    time.sleep(1)
    res = not is_process_alive(pid)
    if res:
        _remove_pid_file()
    return res

def kill_process(pid: int, force: bool = False) -> bool:
    if not is_process_alive(pid):
        _remove_pid_file()
        return True
    if os.name == "nt":
        return _kill_windows(pid, force)
    return _kill_posix(pid, force)

def start_nb_process(mode: str) -> int:
    if os.path.exists(LOG_FILE):
        try: os.rename(LOG_FILE, OLD_LOG_FILE)
        except Exception: pass
    cwd = os.getcwd()
    python = sys.executable
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = cwd
    cmd = [python, "-u", "-m", "nb.cli", mode, "--loud"]
    fd = None
    try:
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        proc = subprocess.Popen(cmd, stdout=fd, stderr=fd, stdin=subprocess.DEVNULL, cwd=cwd, env=env, start_new_session=True)
        time.sleep(2)
        if proc.poll() is not None: return 0
        _write_pid_file(proc.pid)
        return proc.pid
    except Exception: return 0
    finally:
        if fd is not None:
            try: os.close(fd)
            except OSError: pass

def termination():
    _remove_pid_file()
    CONFIG.pid = 0
    write_config(CONFIG)

def _read_log_file(path: str) -> str:
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return f.read()
    except OSError:
        pass
    return ""

def get_all_logs_text() -> str:
    _trim_log_file(LOG_FILE)
    _trim_log_file(OLD_LOG_FILE)
    old_content = _read_log_file(OLD_LOG_FILE)
    new_content = _read_log_file(LOG_FILE)
    if old_content and new_content:
        return old_content + "\n" + new_content
    if old_content:
        return old_content
    if new_content:
        return new_content
    return "暂无日志。"

# --- UI Code ---

st.set_page_config(page_title="Run Dashboard", page_icon="🏃", layout="wide")
switch_theme(st, CONFIG)

if check_password(st):
    st.markdown("""
    <style>
    .terminal-wrapper {
        background: #1e293b;
        border-radius: 15px;
        box-shadow:  9px 9px 16px var(--shadow-dark),
                    -9px -9px 16px var(--shadow-light);
        overflow: hidden;
        border: 1px solid var(--glass-border);
    }
    .terminal-head {
        background: #0f172a;
        padding: 12px 20px;
        display: flex; gap: 8px; align-items: center;
        border-bottom: 1px solid #334155;
    }
    .terminal-body {
        padding: 20px;
        height: 400px;
        overflow-y: auto;
        color: #e2e8f0;
        font-family: 'Consolas', 'Monaco', monospace;
        font-size: 13px;
        white-space: pre-wrap;
        box-shadow: inset 0 0 20px rgba(0,0,0,0.2);
    }
    .dot { width: 12px; height: 12px; border-radius: 50%; }
    .red { background: #ef4444; box-shadow: 0 0 5px #ef4444; }
    .yellow { background: #f59e0b; box-shadow: 0 0 5px #f59e0b; }
    .green { background: #10b981; box-shadow: 0 0 5px #10b981; }
    </style>
    """, unsafe_allow_html=True)

    pid = get_running_pid()

    with st.container():
        c1, c2, c3 = st.columns(3)
        with c1:
            mode_options = ["实时的", "过去的", "定时的"]
            mode_index = CONFIG.mode if CONFIG.mode < len(mode_options) else 0
            mode = st.radio("模式", mode_options, index=mode_index, horizontal=True, label_visibility="collapsed")
        with c2:
            if mode == "过去的":
                CONFIG.mode = 1
                st.write("")
            elif mode == "定时的":
                CONFIG.mode = 2
                run_time_val = st.text_input(
                    "每日执行时间 (HH:MM)",
                    value=CONFIG.schedule.run_time,
                    key="schedule_run_time",
                    help="每天在此时间自动执行转发任务（服务器本地时间）",
                )
                CONFIG.schedule.run_time = run_time_val
            else:
                CONFIG.live.delete_sync = st.checkbox("同步删除", value=CONFIG.live.delete_sync)
                CONFIG.mode = 0
        with c3:
            if pid > 0:
                st.button(f"🟢 运行中 ({pid})", disabled=True, use_container_width=True, key="status_btn")
            else:
                st.button("🔴 已停止", disabled=True, use_container_width=True, key="status_btn")

    st.write("---")

    if pid == 0:
        c_btn, c_spacer = st.columns([1, 3])
        with c_btn:
            if st.button("▶️ 开始运行", type="primary", use_container_width=True):
                mode_map = {0: "live", 1: "past", 2: "schedule"}
                mode_arg = mode_map.get(CONFIG.mode, "live")
                new_pid = start_nb_process(mode_arg)
                if new_pid > 0:
                    CONFIG.pid = new_pid
                    write_config(CONFIG)
                    time.sleep(1)
                    rerun()
                else:
                    st.error("启动失败")
    else:
        c_btn, c_spacer = st.columns([1, 3])
        with c_btn:
            s1, s2, s3 = st.columns(3)
            with s1:
                if st.button("⏹️ 停止", type="primary", use_container_width=True):
                    if kill_process(pid):
                        termination()
                        time.sleep(1)
                        rerun()
            with s2:
                if st.button("🔴 强制终止", type="secondary", use_container_width=True):
                    os.system(f"kill -9 {pid}")
                    termination()
                    time.sleep(1)
                    rerun()
            with s3:
                st.download_button(
                    "📥 下载全部日志",
                    data=get_all_logs_text(),
                    file_name="logs_all.txt",
                    mime="text/plain",
                    use_container_width=True,
                )

    # --- 修复2: 只在进程运行时才自动刷新 ---
    st.write("")
    if pid > 0:
        st_autorefresh(interval=1000, key="log_autorefresh")

    # --- 读取日志 ---
    log_content = "暂无日志。"
    if os.path.exists(LOG_FILE):
        _trim_log_file(LOG_FILE)
        try:
            with open(LOG_FILE, "r") as f:
                lines = f.readlines()
                raw_content = "".join(lines[-100:]) if lines else ""
                if not raw_content:
                    _trim_log_file(OLD_LOG_FILE)
                    raw_content = _read_log_tail(OLD_LOG_FILE, 100)
                raw_content = raw_content or "等待输出..."
                log_content = html.escape(raw_content)
        except OSError as err:
            logging.warning(f"读取日志失败: {err}")

    # --- 修复3: 智能滚动，用户翻看时不强制拉到底部 ---
    st.components.v1.html(
        f"""
        <div id="log-container" style="
            height:400px;
            overflow-y:auto;
            padding:16px;
            background:#ffffff;
            color:#000000;
            font-family:Consolas, Monaco, monospace;
            font-size:13px;
            white-space:pre-wrap;
            border-radius:15px;
            border:1px solid #ccc;
        ">{log_content}</div>
        <script>
            (function() {{
                const box = document.getElementById('log-container');
                if (!box) return;
                const distanceFromBottom = box.scrollHeight - box.scrollTop - box.clientHeight;
                if (distanceFromBottom < 50) {{
                    box.scrollTop = box.scrollHeight;
                }}
            }})();
        </script>
        """,
        height=420,
        scrolling=False
    )
