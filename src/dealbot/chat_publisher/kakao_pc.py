"""카카오톡 오픈채팅방 게시 - Windows PC 카카오톡 UI 자동화 (비공식).

⚠️ 카카오는 기존 단체방/오픈채팅방에 메시지를 보내는 공식 API를 제공하지 않는다.
   이 모듈은 사용자의 PC에서 로그인된 카카오톡의 '채팅방 창'에 Win32 메시지로 텍스트를 넣는다.
   - 카카오 운영정책상 비정상적 자동화로 간주되어 계정 이용이 제한될 수 있다.
   - 카카오톡 UI 변경(창 클래스명 등) 시 동작하지 않을 수 있다.
   - 전송 성공 여부를 프로그램이 확인할 수 없다.

모드
  assist (기본): 입력창에 메시지를 채우고 창을 앞으로 가져온다. 사람이 Enter 로 최종 전송.
  auto         : Enter 까지 자동 전송. kakao.auto_send_accept_risk: true 일 때만 허용.

사전 조건: PC 카카오톡 로그인, 대상 오픈채팅방 입장, 해당 채팅방 창을 별도 창으로 열어둘 것.
          config 의 publish.kakao.room_name 은 채팅방 창 제목(=방 이름)과 정확히 같아야 한다.
로그인 정보는 저장·사용하지 않는다.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .base import PublishError, PublishResult

EDIT_CLASS = "RICHEDIT50W"


class WinApi(Protocol):
    def find_window(self, title: str) -> int: ...
    def find_edit(self, hwnd: int) -> int: ...
    def get_text_length(self, edit: int) -> int: ...
    def set_text(self, edit: int, text: str) -> None: ...
    def press_enter(self, edit: int) -> None: ...
    def focus(self, hwnd: int) -> None: ...


class Win32Api:  # pragma: no cover - Windows 전용
    def __init__(self):
        if sys.platform != "win32":
            raise PublishError("카카오톡 PC 게시는 Windows 에서만 동작합니다", retryable=False)
        try:
            import win32api
            import win32con
            import win32gui
        except ImportError as e:
            raise PublishError("pywin32 미설치: pip install -e .[windows]", retryable=False) from e
        self.api, self.con, self.gui = win32api, win32con, win32gui

    def find_window(self, title: str) -> int:
        return self.gui.FindWindow(None, title)

    def find_edit(self, hwnd: int) -> int:
        return self.gui.FindWindowEx(hwnd, None, EDIT_CLASS, None)

    def get_text_length(self, edit: int) -> int:
        return self.api.SendMessage(edit, self.con.WM_GETTEXTLENGTH, 0, 0)

    def set_text(self, edit: int, text: str) -> None:
        self.api.SendMessage(edit, self.con.WM_SETTEXT, 0, text.replace("\n", "\r\n"))

    def press_enter(self, edit: int) -> None:
        self.api.PostMessage(edit, self.con.WM_KEYDOWN, self.con.VK_RETURN, 0)
        time.sleep(0.05)
        self.api.PostMessage(edit, self.con.WM_KEYUP, self.con.VK_RETURN, 0)

    def focus(self, hwnd: int) -> None:
        try:
            self.gui.ShowWindow(hwnd, self.con.SW_RESTORE)
            self.gui.SetForegroundWindow(hwnd)
        except Exception:  # 포커스 강탈 제한으로 실패할 수 있음 - 치명적이지 않음
            pass


class KakaoPcPublisher:
    channel = "kakao_pc"

    def __init__(self, room_name: str, send_mode: str = "assist", accept_risk: bool = False,
                 min_seconds_between_posts: float = 30, max_posts_per_hour: int = 10,
                 recent_post_count: Callable[[datetime], int] | None = None,
                 api: WinApi | None = None, sleep: Callable[[float], None] = time.sleep):
        if not room_name:
            raise PublishError("publish.kakao.room_name 미설정 (채팅방 창 제목)", retryable=False)
        if send_mode not in {"assist", "auto"}:
            raise PublishError(f"알 수 없는 kakao.send_mode: {send_mode}", retryable=False)
        if send_mode == "auto" and not accept_risk:
            raise PublishError("auto 전송은 계정 제한 위험이 있어 kakao.auto_send_accept_risk: true 가 "
                               "필요합니다", retryable=False)
        self.room_name = room_name
        self.send_mode = send_mode
        self.min_gap = min_seconds_between_posts
        self.max_per_hour = max_posts_per_hour
        self.recent_post_count = recent_post_count
        self._api = api
        self._sleep = sleep
        self._last_post: float | None = None

    @property
    def api(self) -> WinApi:
        if self._api is None:
            self._api = Win32Api()
        return self._api

    def publish(self, message: str) -> PublishResult:
        if self.recent_post_count is not None:
            since = datetime.now(timezone.utc) - timedelta(hours=1)
            if self.recent_post_count(since) >= self.max_per_hour:
                raise PublishError("카카오 시간당 게시 상한 도달", retryable=False)
        if self._last_post is not None:
            wait = self.min_gap - (time.monotonic() - self._last_post)
            if wait > 0:
                self._sleep(wait)

        hwnd = self.api.find_window(self.room_name)
        if not hwnd:
            raise PublishError(f"채팅방 창을 찾을 수 없습니다: '{self.room_name}' (카카오톡에서 방을 열어두세요)",
                               retryable=False)
        edit = self.api.find_edit(hwnd)
        if not edit:
            raise PublishError("채팅 입력창을 찾을 수 없습니다 (카카오톡 UI 변경 가능성)", retryable=False)
        if self.api.get_text_length(edit) > 0:
            # assist 모드에서 이전 메시지를 아직 사람이 보내지 않은 경우 덮어쓰지 않는다
            raise PublishError("입력창에 전송되지 않은 텍스트가 있어 건너뜁니다", retryable=False)

        self.api.set_text(edit, message)
        self._last_post = time.monotonic()
        if self.send_mode == "assist":
            self.api.focus(hwnd)
            return PublishResult("handed_off", "입력창에 메시지 입력 완료 - 사람이 Enter 로 전송")
        self.api.press_enter(edit)
        self._sleep(0.5)
        if self.api.get_text_length(edit) > 0:
            raise PublishError("Enter 후에도 입력창에 텍스트가 남아 있음 - 전송 실패 의심", retryable=False)
        return PublishResult("sent", "자동 전송 (전달 여부는 카카오톡에서 확인 불가)")
