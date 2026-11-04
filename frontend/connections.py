"""On-demand connection checks and a shared login dialog."""

import gradio as gr

from backend.p0.ai import connection_status
from frontend.data_hub import error_message

PROVIDERS = {"moodle": "Moodle", "sis": "校務系統", "ntpu-ai": "NTPU AI"}
MASK_PASSWORD_JS = """(...args) => {
  const input = document.querySelector('#login-password input');
  if (input) input.type = 'password';
  document.querySelector('#password-eye')?.setAttribute('aria-pressed', 'false');
  return args;
}"""


def check_connections(service):
    states, messages = {}, []
    try:
        if service.user:
            service.verify_identity()
        else:
            service.adopt_vault()
        states["moodle"] = True
        messages.append("Moodle：已連線")
    except Exception as exc:
        states["moodle"] = False
        messages.append("Moodle：需要登入或重新連線。" + error_message(exc))
    states["sis"] = False
    if states["moodle"] and service.hub.sis_user == service.user and service.hub.sis.state == "connected":
        try:
            snapshots = service.hub.sync_sis()
            states["sis"] = service.hub.sis.state == "connected" and not any(
                s.get("error") in {"SIS_LOGIN_REQUIRED", "SIS_ACCOUNT_CHANGED"}
                for s in snapshots.values()
            )
        except Exception:
            pass
    messages.append("校務系統：" + ("已連線" if states["sis"] else "尚未登入或連線已失效"))
    try:
        result = connection_status(service.ai)
        states["ntpu-ai"] = bool(result.get("ready"))
        ai_message = ai_connection_message(result)
    except Exception:
        states["ntpu-ai"] = False
        ai_message = "NTPU AI：連線檢查未完成，請稍後重新檢查。"
    messages.append(ai_message)
    return states, "\n\n".join(messages)


def ai_connection_message(status):
    if status.get("ready"):
        return "NTPU AI：已連線，可直接續跑或重新產生草稿。"
    if status.get("authentication", "required") == "required":
        return "NTPU AI：需要登入，請開啟登入視窗；完成後按「檢查／恢復 AI 連線」。"
    if status.get("authentication") == "authenticated":
        return "NTPU AI：登入已完成，正在交接背景連線。請關閉登入視窗後再次檢查。"
    return "NTPU AI：背景連線尚未就緒，請稍後重新檢查；目前無法判定登入是否失效。"


def build_connections(service, trigger, page):
    def fields(provider, states):
        needed = not states.get(provider, False)
        return (gr.update(visible=provider != "ntpu-ai" and needed),
                gr.update(visible=provider == "ntpu-ai"), "",
                gr.update(value="", type="password"))

    def check(current_page):
        states, message = check_connections(service)
        provider = "sis" if current_page == "hub" else "ntpu-ai"
        if not states["moodle"]:
            provider = "moodle"
        return states, message, provider, *fields(provider, states)

    def login(provider, username, password, states):
        try:
            if provider == "moodle":
                service.login(username.strip(), password)
                states = {**states, "sis": False}
                message = "Moodle 登入成功，課程已同步。可關閉視窗繼續使用。"
            elif provider == "sis":
                result = service.hub.connect_sis(username.strip(), password)
                message = "校務登入成功。" + ("部分資料尚未讀取，請在資料中心查看同步狀態。" if result.get("error") else "課表已更新。")
            else:
                raise ValueError("Unsupported credential provider")
            states = {**states, provider: True}
            return states, message, gr.update(visible=False), "", gr.update(value="", type="password"), True
        except Exception as exc:
            return ({**states, provider: False}, "登入未完成，請確認帳號與密碼後重試。\n\n" + error_message(exc),
                    gr.update(visible=True), username, gr.update(value="", type="password"), False)

    def logout(provider, states):
        try:
            service.logout(provider)
            states = {**states, provider: False}
            if provider == "moodle":
                states["sis"] = False
            return states, "已登出 " + PROVIDERS[provider] + "。", *fields(provider, states), True
        except Exception as exc:
            return states, error_message(exc), *fields(provider, states), False

    def open_ai():
        try:
            service.ai.rpc("open_login")
            return "已開啟專用瀏覽器。完成本人登入後，按「重新檢查連線」。"
        except Exception as exc:
            return error_message(exc)

    with gr.Column(visible=False, elem_id="connection-modal") as modal:
        with gr.Column(elem_id="connection-panel"):
            with gr.Row():
                gr.Markdown("## 連線管理", scale=3)
                close = gr.Button("關閉 ✕", scale=1, min_width=80, elem_id="close-connections")
            gr.Markdown("先檢查連線，需要時再登入。校務請使用與 Moodle 相同的本人帳號。")
            status = gr.Markdown("正在檢查連線…")
            states = gr.State({})
            auth_changed = gr.State(False)
            provider = gr.Radio([(v, k) for k, v in PROVIDERS.items()], value="moodle", label="連線服務")
            with gr.Column(visible=False) as credentials:
                username = gr.Textbox(label="帳號", type="text", max_lines=1)
                eye = gr.Button("👁 顯示／隱藏密碼", size="sm", render=False, elem_id="password-eye")
                password = gr.Textbox(label="密碼", type="password",
                                      elem_id="login-password", buttons=[eye])
                submit = gr.Button("登入所選服務", variant="primary")
            with gr.Column(visible=False) as ai:
                gr.Markdown("NTPU AI 使用專用瀏覽器完成學校登入。")
                ai_open = gr.Button("開啟 NTPU AI 登入", variant="primary")
            with gr.Row():
                recheck = gr.Button("重新檢查連線")
                signout = gr.Button("登出所選服務")

    eye.click(None, js="""() => {
      const input = document.querySelector('#login-password input');
      if (input) {
        input.type = input.type === 'password' ? 'text' : 'password';
        document.querySelector('#password-eye')?.setAttribute('aria-pressed', String(input.type === 'text'));
      }
    }""", api_name=False)
    fields_outputs = [credentials, ai, username, password]
    check_outputs = [states, status, provider, *fields_outputs]
    open_event = trigger.click(lambda: (gr.update(visible=True), "正在檢查連線…", gr.update(visible=False),
                           "", gr.update(value="", type="password")),
                  outputs=[modal, status, credentials, username, password], api_name=False).then(
                      check, page, check_outputs, api_name=False)
    recheck_event = recheck.click(check, page, check_outputs, js=MASK_PASSWORD_JS, api_name=False)
    provider.input(fields, [provider, states], fields_outputs, js=MASK_PASSWORD_JS, api_name=False)
    close.click(lambda: (gr.update(visible=False), "", gr.update(value="", type="password")),
                outputs=[modal, username, password], js=MASK_PASSWORD_JS, api_name=False)
    login_event = submit.click(login, [provider, username, password, states],
                              [states, status, credentials, username, password, auth_changed],
                              js=MASK_PASSWORD_JS, api_name=False)
    enter_event = password.submit(login, [provider, username, password, states],
                                 [states, status, credentials, username, password, auth_changed],
                                 js=MASK_PASSWORD_JS, api_name=False)
    logout_event = signout.click(logout, [provider, states], [states, status, *fields_outputs, auth_changed],
                                js=MASK_PASSWORD_JS, api_name=False)
    ai_open.click(open_ai, outputs=status, api_name=False)
    # Only credential changes reload drafts; checking a connection must not
    # overwrite unsaved edits in the homework wizard.
    return (login_event, logout_event, enter_event), (open_event, recheck_event), auth_changed
