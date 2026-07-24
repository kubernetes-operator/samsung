"""대시보드 로그인 자격증명 저장소 + 세션 토큰.

설계 원칙:
- **파일 기반 저장**(볼륨 마운트) — 대시보드는 클러스터 k8s API에 여전히 read-only다.
  자격증명 변경을 Secret에 쓰지 않고 자기 볼륨의 파일에만 쓴다(쓰기 RBAC 불필요).
- **기본 자격증명은 admin / password.** 앱 내 `/settings`에서 아이디·비밀번호를 바꿀 수
  있고, 변경은 저장 파일에 반영된다(PVC면 재기동에도 유지, 볼륨이 없거나 쓰기 불가면
  메모리에만 유지되어 재기동 시 기본값으로 되돌아간다).
- 비밀번호는 **PBKDF2-HMAC-SHA256(salt)** 로 해시 저장한다(평문 저장 안 함).
- 세션은 **HMAC 서명 쿠키**다. 서명 키(session_secret)는 저장 파일에 보관하며,
  비밀번호/아이디 변경 시 **회전**시켜 기존 세션을 전부 무효화한다(다른 기기 강제 로그아웃).

저장 경로는 env `DASHBOARD_CREDENTIALS_PATH`로 지정한다. 비어 있으면 파일 없이
메모리로만 동작한다(로컬 실행/테스트). 초기 자격증명은 env `DASHBOARD_USERNAME`/
`DASHBOARD_PASSWORD`가 있으면 그 값으로, 없으면 admin/password로 만든다(부트스트랩 전용 —
이후에는 저장 파일이 우선).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Optional

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "password"
MIN_PASSWORD_LEN = 4
SESSION_TTL_SECONDS = 12 * 60 * 60
COOKIE_NAME = "dash_session"

_PBKDF2_ITERATIONS = 200_000


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, _PBKDF2_ITERATIONS)


class CredentialStore:
    """로그인 자격증명 1건(username + 비밀번호 해시) + 세션 서명 키를 담는 파일 백엔드 저장소."""

    def __init__(self, path: Optional[str]):
        self._path = path
        self._lock = threading.RLock()
        self._persistent = False
        self._data: dict = {}
        self._load()

    # -- 초기화/로드/저장 ----------------------------------------------------
    def _default_data(self) -> dict:
        salt = secrets.token_bytes(16)
        user = os.getenv("DASHBOARD_USERNAME") or DEFAULT_USERNAME
        pw = os.getenv("DASHBOARD_PASSWORD") or DEFAULT_PASSWORD
        return {
            "version": 1,
            "username": user,
            "salt": salt.hex(),
            "hash": _hash_password(pw, salt).hex(),
            "session_secret": secrets.token_hex(32),
            "updated_at": None,
        }

    def _valid(self, data) -> bool:
        return isinstance(data, dict) and all(
            isinstance(data.get(k), str) for k in ("username", "salt", "hash", "session_secret")
        )

    def _load(self) -> None:
        with self._lock:
            data = None
            if self._path and os.path.exists(self._path):
                try:
                    with open(self._path, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if self._valid(loaded):
                        data = loaded
                except (OSError, ValueError):
                    data = None
            if data is None:
                self._data = self._default_data()
                self._persistent = self._try_save()  # 새로 만들면 파일로 남겨 본다
            else:
                self._data = data
                self._persistent = True

    def _try_save(self) -> bool:
        if not self._path:
            return False
        try:
            directory = os.path.dirname(self._path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp, self._path)  # 원자적 교체(부분 쓰기 방지)
            return True
        except OSError:
            return False

    # -- 조회 ---------------------------------------------------------------
    @property
    def username(self) -> str:
        with self._lock:
            return self._data["username"]

    @property
    def persistent(self) -> bool:
        """자격증명 변경이 재기동에도 유지되는지(파일 저장 성공 여부)."""
        with self._lock:
            return self._persistent

    def verify(self, username: str, password: str) -> bool:
        """(username, password)가 저장된 자격증명과 일치하는지 상수 시간 비교."""
        with self._lock:
            stored_user = self._data["username"]
            salt = bytes.fromhex(self._data["salt"])
            expected = bytes.fromhex(self._data["hash"])
        actual = _hash_password(password or "", salt)
        # 단축 평가로 새지 않도록 둘 다 계산.
        user_ok = hmac.compare_digest(username or "", stored_user)
        pass_ok = hmac.compare_digest(actual, expected)
        return user_ok and pass_ok

    # -- 변경 ---------------------------------------------------------------
    def update_credentials(self, *, new_username: Optional[str] = None,
                           new_password: Optional[str] = None) -> None:
        """아이디/비밀번호를 바꾸고 세션 키를 회전한다(기존 세션 전부 무효화)."""
        with self._lock:
            if new_username:
                self._data["username"] = new_username
            if new_password:
                salt = secrets.token_bytes(16)
                self._data["salt"] = salt.hex()
                self._data["hash"] = _hash_password(new_password, salt).hex()
            self._data["session_secret"] = secrets.token_hex(32)
            self._data["updated_at"] = time.time()
            self._persistent = self._try_save()

    # -- 세션(서명 쿠키) -----------------------------------------------------
    def issue_session(self, username: str, ttl: int = SESSION_TTL_SECONDS) -> str:
        with self._lock:
            secret = self._data["session_secret"]
        exp = int(time.time()) + ttl
        payload = f"{username}:{exp}"
        b = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
        sig = hmac.new(secret.encode("ascii"), b.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{b}.{sig}"

    def session_user(self, token: Optional[str]) -> Optional[str]:
        """유효한 세션 토큰이면 사용자명을, 아니면 None. 서명·만료·현재 사용자명을 모두 확인."""
        if not token or "." not in token:
            return None
        b, _, sig = token.partition(".")
        with self._lock:
            secret = self._data["session_secret"]
            current_user = self._data["username"]
        expected = hmac.new(secret.encode("ascii"), b.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            pad = "=" * (-len(b) % 4)
            payload = base64.urlsafe_b64decode(b + pad).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        user, sep, exp_s = payload.rpartition(":")
        if not sep:
            return None
        try:
            exp = int(exp_s)
        except ValueError:
            return None
        if exp < time.time():
            return None
        # 아이디가 바뀌면(회전과 별개로) 옛 토큰 무효.
        if not hmac.compare_digest(user, current_user):
            return None
        return user


# --------------------------------------------------------------------------- 저장소 캐시(경로별 싱글턴)
_stores: dict = {}
_stores_lock = threading.Lock()


def _credentials_path() -> Optional[str]:
    return (os.getenv("DASHBOARD_CREDENTIALS_PATH") or "").strip() or None


def get_store() -> CredentialStore:
    """현재 설정된 경로의 자격증명 저장소를 반환(경로별로 한 번만 로드해 재사용)."""
    path = _credentials_path()
    with _stores_lock:
        store = _stores.get(path)
        if store is None:
            store = CredentialStore(path)
            _stores[path] = store
        return store


def reset_store_cache() -> None:
    """테스트에서 경로/env 변경 후 저장소를 다시 로드하도록 캐시를 비운다."""
    with _stores_lock:
        _stores.clear()
