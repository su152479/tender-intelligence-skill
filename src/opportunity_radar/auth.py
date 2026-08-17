import logging, os
from pathlib import Path
from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

class LoginManager:
    def __init__(self, auth_dir: Path): self.auth_dir = auth_dir
    def state_path(self, source_id: str) -> Path: return self.auth_dir / f"{source_id}.json"
    def manual_login(self, source: dict) -> Path:
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        timeout = int(os.getenv("RADAR_LOGIN_TIMEOUT_SECONDS", "300")) * 1000
        channel = os.getenv("RADAR_BROWSER_CHANNEL", "msedge").strip() or None
        with sync_playwright() as p:
            # Use the locally installed, user-facing browser when available. This does
            # not bypass CAPTCHA; it only gives the user a normal browser for login.
            browser = p.chromium.launch(headless=False, channel=channel)
            context = browser.new_context()
            page = context.new_page()
            page.goto(source.get("login_url", source["url"]), wait_until="domcontentloaded")
            page.set_default_timeout(timeout)
            success_url = source.get("login_success_url_contains")
            if success_url:
                print("请在浏览器中人工完成登录（包括验证码）；登录成功后程序会自动保存状态。")
                # Some portals briefly show the return URL before their asynchronous
                # login check redirects. Let that check settle before declaring success.
                page.wait_for_timeout(15000)
                if "login" in page.url.lower():
                    page.wait_for_url(lambda url: success_url in url and "login" not in url.lower(), timeout=timeout)
                elif success_url not in page.url:
                    raise RuntimeError(f"未到达预期登录成功页面：{page.url}")
                page.wait_for_timeout(1500)
            else:
                print("请在浏览器中人工完成登录（包括验证码），完成后回到终端按 Enter。")
                input()
            context.storage_state(path=str(self.state_path(source["id"])))
            browser.close()
        return self.state_path(source["id"])
    def has_state(self, source_id: str) -> bool: return self.state_path(source_id).exists()
    def context_kwargs(self, source_id: str) -> dict:
        if not self.has_state(source_id): raise RuntimeError(f"登录状态不存在，请运行: radar login {source_id}")
        return {"storage_state": str(self.state_path(source_id))}
    def remind_expired(self, source_id: str):
        log.warning("%s 登录可能失效，请运行 radar login %s 重新人工登录", source_id, source_id)
