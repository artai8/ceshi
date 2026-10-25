# nb/web_ui/pages/1_🔑_Telegram_Login.py —— 修复自动填充

import streamlit as st
import os  # 👈 新增导入

from nb.config import CONFIG, read_config, write_config
from nb.web_ui.password import check_password
from nb.web_ui.utils import hide_st, switch_theme

CONFIG = read_config()

st.set_page_config(
    page_title="TG 登录",
    page_icon="🔑",
)
hide_st(st)
switch_theme(st, CONFIG)

if check_password(st):

    # ✅ 自动从环境变量读取，若无则使用配置文件中的值
    env_api_id = os.getenv("API_ID", "")
    env_api_hash = os.getenv("API_HASH", "")
    env_session_string = os.getenv("SESSION_STRING", "")
    env_bot_token = os.getenv("BOT_TOKEN", "")

    # 优先使用环境变量，其次用配置中保存的值
    default_api_id = env_api_id or str(CONFIG.login.API_ID)
    default_api_hash = env_api_hash or CONFIG.login.API_HASH
    default_session_string = env_session_string or CONFIG.login.SESSION_STRING
    default_bot_token = env_bot_token or CONFIG.login.BOT_TOKEN

    # 输入框使用默认值（来自 env 或 config）
    input_api_id = st.text_input("API ID", value=default_api_id, type="password")
    input_api_hash = st.text_input("API HASH", value=default_api_hash, type="password")

    st.write("您可以从 https://my.telegram.org 获取 API ID 和 API Hash。")

    user_type = st.radio(
        "选择账户类型", ["机器人 (Bot)", "用户 (User)"], index=CONFIG.login.user_type
    )

    if "机器人" in user_type:
        CONFIG.login.user_type = 0
        CONFIG.login.BOT_TOKEN = st.text_input(
            "输入 Bot Token", value=default_bot_token, type="password"
        )
    else:
        CONFIG.login.user_type = 1
        CONFIG.login.SESSION_STRING = st.text_input(
            "输入 Session String", value=default_session_string, type="password"
        )
        with st.expander("如何获取 Session String？"):
            st.markdown(
                """
            您可以使用 `tg-login` 命令行程序来生成 Session String。

            _输入 API ID、API Hash 和手机号以生成 Session String。_

            **开发者提示：**

            由于某些问题，此 Web 界面不支持直接使用手机号登录用户账户。

            您可以通过 `pip install tg-login` 安装并运行 tg-login 来生成 Session String。tg-login 是开源的。

            什么是 Session String？
            Session String 是 Telethon 会话的字符串表示形式，可以用来免密码登录。
            """
            )

    # 保存时写入的是用户输入的值（可能覆盖了 env）
    if st.button("保存"):
        try:
            CONFIG.login.API_ID = int(input_api_id)
        except ValueError:
            st.error("API ID 必须是整数")
            st.stop()
        CONFIG.login.API_HASH = input_api_hash
        write_config(CONFIG)
        st.success("配置保存成功！")

