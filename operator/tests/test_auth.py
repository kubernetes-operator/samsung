"""dashboard.auth 모듈 테스트 — 파일 기반 자격증명 저장소 + 서명 세션 토큰.

실제 파일(tmp_path)에 저장하되 클러스터/외부 의존성은 없다(순수 stdlib).
"""

from __future__ import annotations

import json
import time

import pytest

from k8s_traffic_operator.dashboard import auth


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("DASHBOARD_CREDENTIALS_PATH", raising=False)
    auth.reset_store_cache()
    yield
    auth.reset_store_cache()


def _store(tmp_path, name="creds.json"):
    return auth.CredentialStore(str(tmp_path / name))


# --- 기본값/검증 -----------------------------------------------------------
def test_default_credentials_admin_password(tmp_path):
    store = _store(tmp_path)
    assert store.username == "admin"
    assert store.verify("admin", "password")
    assert not store.verify("admin", "wrong")
    assert not store.verify("root", "password")


def test_password_never_stored_in_plaintext(tmp_path):
    path = tmp_path / "creds.json"
    _store(tmp_path)
    raw = path.read_text(encoding="utf-8")
    assert "password" not in raw          # 평문 비밀번호가 파일에 없어야 한다
    data = json.loads(raw)
    assert set(data) >= {"username", "salt", "hash", "session_secret"}


def test_env_bootstrap_seeds_initial_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERNAME", "seed-user")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "seed-pass")
    store = _store(tmp_path)
    assert store.username == "seed-user"
    assert store.verify("seed-user", "seed-pass")
    assert not store.verify("admin", "password")


# --- 변경/영속 -------------------------------------------------------------
def test_change_password_persists_across_reload(tmp_path):
    store = _store(tmp_path)
    store.update_credentials(new_password="brand-new-pw")
    assert store.verify("admin", "brand-new-pw")
    assert not store.verify("admin", "password")
    # 같은 경로로 새 저장소를 열면 바뀐 비밀번호가 로드돼야 한다(파일 영속).
    reloaded = _store(tmp_path)
    assert reloaded.verify("admin", "brand-new-pw")
    assert not reloaded.verify("admin", "password")


def test_change_username(tmp_path):
    store = _store(tmp_path)
    store.update_credentials(new_username="operator", new_password="pw12345")
    assert store.username == "operator"
    assert store.verify("operator", "pw12345")


def test_in_memory_store_when_no_path():
    store = auth.CredentialStore(None)
    assert store.persistent is False
    assert store.verify("admin", "password")   # 메모리에서도 동작
    store.update_credentials(new_password="x1234")
    assert store.verify("admin", "x1234")


def test_persistent_flag_true_with_writable_path(tmp_path):
    assert _store(tmp_path).persistent is True


# --- 세션 토큰 -------------------------------------------------------------
def test_session_roundtrip(tmp_path):
    store = _store(tmp_path)
    token = store.issue_session("admin")
    assert store.session_user(token) == "admin"


def test_session_rejects_tampered_signature(tmp_path):
    store = _store(tmp_path)
    token = store.issue_session("admin")
    body, _, _sig = token.partition(".")
    assert store.session_user(body + ".deadbeef") is None
    assert store.session_user("garbage") is None
    assert store.session_user(None) is None


def test_session_rejects_expired(tmp_path):
    store = _store(tmp_path)
    token = store.issue_session("admin", ttl=-1)   # 이미 만료
    assert store.session_user(token) is None


def test_session_invalid_after_password_change(tmp_path):
    """비밀번호 변경 시 session_secret 회전 → 기존 토큰 무효."""
    store = _store(tmp_path)
    token = store.issue_session("admin")
    assert store.session_user(token) == "admin"
    store.update_credentials(new_password="rotated-pw")
    assert store.session_user(token) is None


def test_session_invalid_after_username_change(tmp_path):
    store = _store(tmp_path)
    token = store.issue_session("admin")
    store.update_credentials(new_username="someone-else")
    assert store.session_user(token) is None


# --- 손상 파일 복구 --------------------------------------------------------
def test_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "creds.json"
    path.write_text("not-json{", encoding="utf-8")
    store = auth.CredentialStore(str(path))
    assert store.verify("admin", "password")     # 손상 시 기본값으로 복구


def test_get_store_caches_by_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_CREDENTIALS_PATH", str(tmp_path / "c.json"))
    auth.reset_store_cache()
    assert auth.get_store() is auth.get_store()   # 같은 경로면 같은 인스턴스
