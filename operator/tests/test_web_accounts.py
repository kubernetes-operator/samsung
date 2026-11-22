"""기존 자격증명 파일은 임시 경로만 사용하며 실제 클러스터에는 접근하지 않는다."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier,Event
from unittest.mock import Mock
import pytest
from fastapi.testclient import TestClient
from k8s_traffic_operator.dashboard import app as dashboard,auth
from k8s_traffic_operator.dashboard.accounts import AccountStore,AccountError
from k8s_traffic_operator.dashboard.hubble_flows import FlowSummary

USER='primary-fixture';PASSWORD='primary-fixture-password'

@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    monkeypatch.setenv('DASHBOARD_CREDENTIALS_PATH',str(tmp_path/'fixture.json'))
    monkeypatch.setenv('DASHBOARD_USERNAME',USER);monkeypatch.setenv('DASHBOARD_PASSWORD',PASSWORD)
    monkeypatch.delenv('DASHBOARD_URL_PREFIX',raising=False)
    monkeypatch.setattr(dashboard.data,'fetch_policies',lambda:[])
    monkeypatch.setattr(dashboard.hubble_flows,'fetch_summary',lambda **kwargs:FlowSummary(total=0,shown=0,scope='app'))
    auth.reset_store_cache()
    yield
    auth.reset_store_cache()

def login(username=USER,password=PASSWORD):
    client=TestClient(dashboard.app)
    response=client.post('/login',data={'username':username,'password':password},follow_redirects=False)
    assert response.status_code==302,response.text
    return client,response

def basic(username,password):return {'Authorization':'Basic '+base64.b64encode((username+':'+password).encode()).decode()}

def test_existing_file_and_signed_cookie_survive_extension(tmp_path):
    path=tmp_path/'original.json';native=auth.CredentialStore(str(path));token=native.issue_session(USER)
    before=path.read_bytes();store=AccountStore(str(path))
    assert path.read_bytes()==before
    assert store.session_user(token)==USER
    assert store.verify(USER,PASSWORD)
    store.create_user('reader','reader-fixture-password','viewer',['policies'])
    after=json.loads(path.read_text());old=json.loads(before)
    for key in ('username','hash','salt','session_secret'):assert after[key]==old[key]
    assert 'reader-fixture-password' not in path.read_text()
    reloaded=AccountStore(str(path))
    assert reloaded.verify('reader','reader-fixture-password')
    assert reloaded.access('reader')['pages']==['policies']
    assert reloaded.session_user(store.issue_session('reader'))=='reader'
    assert reloaded.session_user(token)==USER

def test_http_user_creation_grants_redirect_basic_and_immediate_revocation(monkeypatch):
    admin,_=login()
    response=admin.post('/api/auth/users',json={'username':'reader','password':'reader-fixture-password','pages':['policies']})
    assert response.status_code==200,response.text
    reader,response=login('reader','reader-fixture-password')
    assert response.headers['location']=='/policies'
    assert reader.get('/policies').status_code==200
    assert reader.get('/api/policies').status_code==200
    flows=Mock(side_effect=AssertionError('forbidden flow fetch must never execute'))
    monkeypatch.setattr(dashboard.hubble_flows,'fetch_summary',flows)
    assert reader.get('/api/flows').status_code==403
    assert reader.get('/',follow_redirects=False).status_code==403
    flows.assert_not_called()
    assert reader.get('/api/auth/users').status_code==403
    assert reader.post('/api/auth/users',json={'username':'forged','password':'test-password','role':'admin'}).status_code==403
    assert TestClient(dashboard.app).get('/api/policies',headers=basic('reader','reader-fixture-password')).status_code==200
    assert admin.patch('/api/auth/users/reader',json={'pages':[]}).status_code==200
    assert reader.get('/api/policies').status_code==403
    assert reader.get('/api/auth/ui/session').json()['pages']==['settings']

def test_confirmation_and_current_password_invalidate_own_sessions_only():
    admin,_=login();admin.post('/api/auth/users',json={'username':'reader','password':'reader-fixture-password','pages':['policies']})
    reader,_=login('reader','reader-fixture-password');other,_=login('reader','reader-fixture-password')
    body={'current_password':'reader-fixture-password','new_password':'changed-fixture-password','confirm_password':'different-password'}
    assert reader.post('/api/auth/ui/password',json=body).status_code==400
    body['confirm_password']=body['new_password'];body['current_password']='incorrect'
    assert reader.post('/api/auth/ui/password',json=body).status_code==403
    body['current_password']='reader-fixture-password'
    response=reader.post('/api/auth/ui/password',json=body)
    assert response.status_code==200 and response.json()['reauthenticate']
    assert reader.get('/api/auth/ui/session').status_code==401
    assert other.get('/api/auth/ui/session').status_code==401
    assert admin.get('/api/auth/ui/session').status_code==200
    assert auth.get_store().verify(USER,PASSWORD)
    assert login('reader','changed-fixture-password')[0].get('/api/policies').status_code==200

def test_secondary_legacy_settings_change_does_not_modify_primary():
    admin,_=login();admin.post('/api/auth/users',json={'username':'reader','password':'reader-fixture-password','pages':[]})
    reader,_=login('reader','reader-fixture-password')
    response=reader.post('/settings',data={'current_password':'reader-fixture-password','new_password':'reader-new-password','confirm_password':'reader-new-password'})
    assert response.status_code==200,response.text
    assert auth.get_store().username==USER and auth.get_store().verify(USER,PASSWORD)
    assert auth.get_store().verify('reader','reader-new-password')

def test_primary_and_secondary_username_change_preserves_roles():
    admin,_=login();admin.post('/api/auth/users',json={'username':'reader','password':'reader-fixture-password','pages':['policies']})
    reader,_=login('reader','reader-fixture-password')
    response=reader.post('/api/auth/ui/username',json={'current_password':'reader-fixture-password','new_username':'새사용자'})
    assert response.status_code==200,response.text
    assert reader.get('/api/auth/ui/session').status_code==401
    renamed,_=login('새사용자','reader-fixture-password')
    assert renamed.get('/api/policies').status_code==200
    assert renamed.get('/api/auth/users').status_code==403
    response=admin.post('/api/auth/ui/username',json={'current_password':PASSWORD,'new_username':'new-primary'})
    assert response.status_code==200,response.text
    assert auth.get_store().username=='new-primary'
    assert login('new-primary',PASSWORD)[0].get('/api/auth/users').status_code==200

def test_last_admin_concurrent_demotion_and_restart(tmp_path):
    store=auth.get_store();store.create_user('second-admin','fixture-password','admin',[])
    barrier=Barrier(2)
    def demote(username):
        barrier.wait(timeout=5)
        try:return store.edit_user(username,{'role':'viewer','pages':[]})
        except AccountError as error:return error.status
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(demote,[USER,'second-admin']))
    assert results.count(409)==1
    assert sum(u['role']=='admin' for u in store.list_users())==1
    auth.reset_store_cache()
    assert sum(u['role']=='admin' for u in auth.get_store().list_users())==1

def test_new_account_persistence_failure_rolls_back(monkeypatch):
    store=auth.get_store();token=store.issue_session(USER)
    monkeypatch.setattr(store,'_try_save',lambda:False)
    with pytest.raises(AccountError) as error:store.create_user('reader','fixture-password','viewer',[])
    assert error.value.status==503
    assert not store.verify('reader','fixture-password')
    assert store.session_user(token)==USER

def test_invalid_grants_and_duplicate_account_are_atomic():
    client,_=login()
    for body in ({'username':'bad/name','pages':[]},{'username':'bad','pages':['unknown']},{'username':'bad','pages':['users']}):
        assert client.post('/api/auth/users',json={'password':'fixture-password',**body}).status_code==400
    assert client.patch('/api/auth/users/'+USER,json={'role':'viewer','pages':[]}).status_code==409
    assert client.post('/api/auth/users',json={'username':USER,'password':'fixture-password'}).status_code==409
    assert len(client.get('/api/auth/users').json())==1
    assert all(set(user)=={'username','role','pages'} for user in client.get('/api/auth/users').json())

def test_login_and_password_change_are_serialized(monkeypatch):
    store=auth.get_store();entered=Event();release=Event();mutation=Event();original=auth._hash_password;first=True
    def hashing(password,salt):
        nonlocal first
        if first:
            first=False;entered.set();assert release.wait(timeout=5)
        return original(password,salt)
    monkeypatch.setattr(auth,'_hash_password',hashing)
    def change():
        mutation.set();store.change_credentials(USER,PASSWORD,new_password='new-fixture-password',confirm_password='new-fixture-password')
    with ThreadPoolExecutor(max_workers=2) as pool:
        signing=pool.submit(store.authenticate_session,USER,PASSWORD)
        assert entered.wait(timeout=5)
        editing=pool.submit(change);assert mutation.wait(timeout=5);assert not editing.done()
        release.set();token=signing.result(timeout=5);editing.result(timeout=5)
    assert store.session_user(token) is None
    assert store.verify(USER,'new-fixture-password')

def test_origin_check_and_settings_without_refresh():
    client,_=login()
    assert client.post('/api/auth/users',headers={'Origin':'https://untrusted.example'},json={'username':'bad','password':'fixture-password'}).status_code==403
    assert 'http-equiv="refresh"' not in client.get('/settings').text
