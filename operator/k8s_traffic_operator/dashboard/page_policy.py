"""앱 공통 페이지 권한 계약. UI 표시와 별개로 서버가 이 규칙을 사용한다."""
from __future__ import annotations

PAGES = {
    'bali': {'notes':'메모', 'settings':'개인 설정', 'users':'사용자 관리'},
    'canggu': {'overview':'개요', 'resources':'리소스', 'operations':'운영', 'activity':'활동', 'backup':'백업·복구', 'access':'접근 발급', 'settings':'개인 설정', 'users':'사용자 관리'},
    'gimpo': {'overview':'개요', 'health':'상태 점검', 'backups':'백업', 'restores':'복구', 'schedules':'스케줄', 'storage':'스토리지', 'repositories':'리포지토리', 'settings':'개인 설정', 'users':'사용자 관리'},
    'samsung': {'flows':'트래픽 흐름', 'policies':'정책', 'settings':'개인 설정', 'users':'사용자 관리'},
    'seongnam': {'dashboard':'개요', 'nodes':'노드', 'events':'이벤트', 'logs':'로그', 'predictions':'예측', 'reports':'리포트', 'settings':'개인 설정', 'users':'사용자 관리'},
    'seoul': {'overview':'개요', 'health':'헬스', 'network':'네트워크', 'security':'보안', 'storage_backup':'스토리지·백업', 'observability':'관측성', 'performance':'성능', 'load_test':'부하 테스트', 'chaos':'제안된 카오스', 'settings':'개인 설정', 'users':'사용자 관리'},
    'tashkent': {'products':'제품', 'new':'새 제품', 'presets':'프리셋', 'debug':'진단', 'settings':'개인 설정', 'users':'사용자 관리'},
}


def normalize(project: str, role: str, pages: list[str] | None) -> list[str] | None:
    """알 수 없는 앱/역할/페이지는 거절한다. None은 기존 계정 호환용이다."""
    if project not in PAGES or role not in ('admin', 'viewer'):
        raise ValueError('앱 또는 역할이 올바르지 않습니다')
    if pages is None:
        return None
    if not isinstance(pages, list) or any(not isinstance(x, str) or x not in PAGES[project] for x in pages):
        raise ValueError('허용되지 않은 페이지입니다')
    if role != 'admin' and 'users' in pages:
        raise ValueError('사용자 관리는 관리자만 허용됩니다')
    return sorted(set(pages))


def allowed(project: str, role: str, pages: list[str] | None, page: str) -> bool:
    if project not in PAGES or page not in PAGES[project] or role not in ('admin','viewer'):
        return False
    if role == 'admin':
        return True
    if page == 'users':
        return False
    if page == 'settings':
        return True  # 자기 비밀번호/개인 설정은 항상 접근할 수 있다.
    return pages is None or page in pages


def effective(project: str, role: str, pages: list[str] | None) -> list[str]:
    return [page for page in PAGES[project] if allowed(project, role, pages, page)]


def normalize_username(value: str) -> str:
    """라우트에 안전한 계정 이름. 기존 계정 이름을 재작성하지는 않는다."""
    import unicodedata
    if not isinstance(value,str):
        raise ValueError('아이디를 입력하세요')
    value=value.strip()
    if not 1 <= len(value) <= 128 or any(c in '/\\%' or c.isspace() or unicodedata.category(c).startswith('C') for c in value):
        raise ValueError('아이디는 공백·경로 문자가 없는 1~128자로 입력하세요')
    return value
