"""Kakao AlimTalk adapter boundary. Supports approved BizMessage relay when configured."""
import json
import urllib.request
from market_brief.core import Briefing, format_briefing


class KakaoAlimtalkChannel:
    def __init__(self, *, api_url: str | None = None, api_key: str | None = None,
                 sender_key: str | None = None, template_code: str | None = None, opener=None):
        self.api_url = api_url
        self.api_key = api_key
        self.sender_key = sender_key
        self.template_code = template_code
        self._opener = opener or urllib.request.urlopen

    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_key and self.sender_key and self.template_code)

    def send(self, recipient: str, briefing: Briefing) -> str:
        if not self.is_configured():
            raise NotImplementedError("Requires approved templates and contracted BizMessage provider")
        payload = json.dumps({
            "senderKey": self.sender_key,
            "templateCode": self.template_code,
            "recipientNo": recipient,
            "content": format_briefing(briefing),
            "buttons": [
                {
                    "name": "상세 시황 리포트 확인",
                    "type": "WL",
                    "url_mobile": briefing.report_url,
                    "url_pc": briefing.report_url,
                }
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            self.api_url,
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with self._opener(req, timeout=10) as resp:
            data = json.load(resp)
        if not data.get("success", True):
            raise RuntimeError("Kakao BizMessage API rejected message")
        return str(data.get("messageId", "kakao-sent"))
