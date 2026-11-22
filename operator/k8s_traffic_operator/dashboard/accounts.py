"""기존 단일 계정 파일/서명 쿠키를 보존하는 부가 웹 사용자 계약.

기존 필드는 변경하지 않고 추가 계정과 역할 메타데이터만 확장한다.
파일은 기존 CredentialStore의 원자적 교체와 잠금을 그대로 사용한다.
"""
import base64
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import hmac
import secrets
import time
from . import auth,page_policy

PROJECT='samsung'

class AccountError(ValueError):
    def __init__(self,message,status=400):super().__init__(message);self.status=status

class AccountStore(auth.CredentialStore):
    def _extra(self):
        value=self._data.get('web_users',{})
        return value if isinstance(value,dict) else {}
    def _record(self,username):
        if username==self._data['username']:return self._data
        value=self._extra().get(username)
        return value if isinstance(value,dict) else None
    def _access(self,username):
        record=self._record(username)
        if record is None:raise AccountError('계정이 없습니다',404)
        primary=username==self._data['username']
        role=record.get('web_role','admin') if primary else record.get('role','viewer')
        pages=record.get('web_pages') if primary else record.get('pages',[])
        try:pages=page_policy.normalize(PROJECT,role,pages)
        except (ValueError,TypeError):role='viewer';pages=[]
        return {'username':username,'role':role,'pages':pages}
    def access(self,username):
        with self._lock:return self._access(username)
    def summary(self,username):
        with self._lock:
            value=self._access(username)
            return {**value,'pages':page_policy.effective(PROJECT,value['role'],value['pages']),'availablePages':page_policy.PAGES[PROJECT],'canManageUsers':value['role']=='admin','passwordMinLength':auth.MIN_PASSWORD_LEN,'supportsUsernameChange':True}
    def list_users(self):
        with self._lock:
            names={self._data['username'],*self._extra().keys()}
            return [self._access(name) for name in sorted(names)]
    def verify(self,username,password):
        if not isinstance(username,str) or not isinstance(password,str):return False
        with self._lock:
            record=self._record(username)
            try:
                salt=bytes.fromhex(record['salt']) if record else b'\0'*16
                expected=bytes.fromhex(record['hash']) if record else b'\0'*32
            except (ValueError,TypeError,KeyError):return False
            actual=auth._hash_password(password,salt)
            return bool(record) and hmac.compare_digest(actual,expected)
    @contextmanager
    def _change(self,strict=True):
        with self._lock:
            before=deepcopy(self._data);was_persistent=self._persistent
            try:
                yield
                self._data['updated_at']=time.time()
                self._persistent=self._try_save()
                if strict and self._path and not self._persistent:raise AccountError('변경 사항을 저장하지 못했습니다. 다시 시도하세요.',503)
            except Exception:
                self._data=before;self._persistent=was_persistent
                raise
    def _validate(self,username,role,pages):
        try:return page_policy.normalize_username(username),page_policy.normalize(PROJECT,role,pages)
        except ValueError as error:raise AccountError(str(error)) from error
    def _password(self,password):
        if not isinstance(password,str) or not auth.MIN_PASSWORD_LEN<=len(password)<=1024:raise AccountError(f'비밀번호는 {auth.MIN_PASSWORD_LEN}~1024자로 입력하세요')
    def _admin_guard(self,username,new_role):
        if self._access(username)['role']=='admin' and new_role!='admin' and sum(u['role']=='admin' for u in self.list_users())<=1:raise AccountError('마지막 관리자는 강등할 수 없습니다',409)
    def create_user(self,username,password,role='viewer',pages=None):
        username,pages=self._validate(username,role,[] if pages is None else pages);self._password(password)
        with self._change():
            if self._record(username) is not None:raise AccountError('이미 존재하는 사용자입니다',409)
            if 'web_users' in self._data and not isinstance(self._data['web_users'],dict):raise AccountError('계정 저장 상태를 확인할 수 없습니다',503)
            salt=secrets.token_bytes(16)
            self._data.setdefault('web_users',{})[username]={'salt':salt.hex(),'hash':auth._hash_password(password,salt).hex(),'session_secret':secrets.token_hex(32),'role':role,'pages':pages}
        return {'ok':True}
    def edit_user(self,username,changes):
        with self._change():
            current=self._access(username);role=changes.get('role',current['role']);pages=changes.get('pages',current['pages'])
            _,pages=self._validate(username,role,pages);self._admin_guard(username,role)
            record=self._record(username)
            if username==self._data['username']:record['web_role']=role;record['web_pages']=pages
            else:record['role']=role;record['pages']=pages
            if 'password' in changes:
                self._password(changes['password']);salt=secrets.token_bytes(16)
                record['salt']=salt.hex();record['hash']=auth._hash_password(changes['password'],salt).hex();record['session_secret']=secrets.token_hex(32)
        return {'ok':True}
    def update_credentials(self,*,new_username=None,new_password=None,username=None,strict=False):
        with self._change(strict=strict):
            username=username or self._data['username'];record=self._record(username)
            if record is None:raise AccountError('계정이 없습니다',404)
            target=new_username or username
            if target!=username:
                target,_=self._validate(target,self._access(username)['role'],self._access(username)['pages'])
                if self._record(target) is not None:raise AccountError('이미 존재하는 사용자입니다',409)
            if new_password:
                salt=secrets.token_bytes(16);record['salt']=salt.hex();record['hash']=auth._hash_password(new_password,salt).hex()
            record['session_secret']=secrets.token_hex(32)
            if username==self._data['username']:self._data['username']=target
            elif target!=username:self._data['web_users'][target]=self._data['web_users'].pop(username)
    def change_credentials(self,username,current_password,*,new_username=None,new_password=None,confirm_password=None,strict=True):
        with self._lock:
            if not self.verify(username,current_password):raise AccountError('현재 비밀번호가 올바르지 않습니다',403)
            new_username=(new_username or '').strip()
            if new_password or confirm_password:
                if new_password!=confirm_password:raise AccountError('새 비밀번호가 일치하지 않습니다')
                self._password(new_password)
            if (not new_username or new_username==username) and not new_password:raise AccountError('변경할 내용이 없습니다')
            self.update_credentials(username=username,new_username=new_username or None,new_password=new_password or None,strict=strict)
            return new_username or username
    def authenticate_session(self,username,password,ttl=auth.SESSION_TTL_SECONDS):
        # 검증과 서명을 같은 잠금 안에서 처리하여 이전 비밀번호가 새 키를 얻지 못한다.
        with self._lock:
            return self.issue_session(username,ttl) if self.verify(username,password) else None
    def issue_session(self,username,ttl=auth.SESSION_TTL_SECONDS):
        with self._lock:
            record=self._record(username)
            if record is None:raise AccountError('계정이 없습니다',404)
            secret=record['session_secret']
            payload=f'{username}:{int(time.time())+ttl}'
            encoded=base64.urlsafe_b64encode(payload.encode()).decode('ascii').rstrip('=')
            signature=hmac.new(secret.encode('ascii'),encoded.encode('ascii'),hashlib.sha256).hexdigest()
            return encoded+'.'+signature
    def session_user(self,token):
        if not isinstance(token,str) or len(token)>8192 or '.' not in token:return None
        encoded,_,signature=token.partition('.')
        try:
            decoded=base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)).decode('utf-8')
            username,sep,expires=decoded.rpartition(':')
            if not sep or int(expires)<time.time():return None
            with self._lock:
                record=self._record(username)
                if record is None:return None
                expected=hmac.new(record['session_secret'].encode('ascii'),encoded.encode('ascii'),hashlib.sha256).hexdigest()
                return username if hmac.compare_digest(signature,expected) else None
        except (ValueError,UnicodeError,TypeError,KeyError):return None
