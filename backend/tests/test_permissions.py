from sqlalchemy import select
from backend.app.models import AuditEvent


def test_requires_auth(scenario):
    client, _, _ = scenario
    assert client.get('/api/documents').status_code == 401
    assert client.post('/api/ask', json={'question': 'Atlas contract'}).status_code == 401


def test_tenant_and_group_isolation(scenario):
    client, token, _ = scenario
    hr = client.get('/api/documents', headers=token('hr@northstar.demo'))
    assert [doc['id'] for doc in hr.json()] == ['d_hr']
    finance = client.get('/api/documents', headers=token('finance@northstar.demo'))
    assert [doc['id'] for doc in finance.json()] == ['d_fin']
    otherco = client.get('/api/documents', headers=token('finance@otherco.demo'))
    assert [doc['id'] for doc in otherco.json()] == ['d_other']
    admin = client.get('/api/documents', headers=token('admin@northstar.demo'))
    assert {doc['id'] for doc in admin.json()} == {'d_hr', 'd_fin'}


def test_direct_file_routes_do_not_bypass_acl(scenario):
    client, token, _ = scenario
    headers = token('hr@northstar.demo')
    assert client.get('/api/documents/d_fin', headers=headers).status_code == 404
    assert client.get('/api/documents/d_fin/source', headers=headers).status_code == 404
    assert client.get('/api/documents/d_other/source', headers=headers).status_code == 404


def test_retrieval_cannot_return_cross_tenant_secret(scenario):
    client, token, _ = scenario
    headers = token('finance@northstar.demo')
    answer = client.post('/api/ask', headers=headers,
                         json={'question': 'Tell me the SILVER-IBIS-73 budget and secret'}).json()
    assert all(source['document_id'] != 'd_other' for source in answer['sources'])
    assert 'SILVER-IBIS-73' not in answer['answer']
    assert answer['mode'] == 'no_evidence'


def test_different_role_cannot_retrieve_salary(scenario):
    client, token, _ = scenario
    answer = client.post('/api/ask', headers=token('finance@northstar.demo'),
                         json={'question': 'What is the salary increase budget?'}).json()
    assert all(source['document_id'] != 'd_hr' for source in answer['sources'])
    assert '310,000' not in answer['answer']


def test_cannot_upload_into_unauthorized_group(scenario):
    client, token, _ = scenario
    response = client.post('/api/documents', headers=token('hr@northstar.demo'),
                           files={'file': ('fake.txt', b'This is a contract.')},
                           data={'groups': 'finance'})
    assert response.status_code == 403


def test_audit_endpoint_is_admin_only_and_tenant_scoped(scenario):
    client, token, factory = scenario
    hr = token('hr@northstar.demo')
    assert client.get('/api/audit', headers=hr).status_code == 403
    assert client.get('/api/documents/d_hr', headers=hr).status_code == 200
    events = client.get('/api/audit', headers=token('admin@northstar.demo')).json()
    assert any(event['action'] == 'document_view' for event in events)
    with factory() as db:
        assert db.scalar(select(AuditEvent).where(AuditEvent.tenant_id == 'northstar'))


def test_upload_to_authorized_group_and_inspect_extraction(scenario):
    client, token, _ = scenario
    hr = token('hr@northstar.demo')
    upload = client.post(
        '/api/documents', headers=hr,
        files={'file': ('employment_policy.txt', b'Counterparty: Example HR Ltd\nEnd date: 2027-02-01\nAnnual value: EUR 10,000')},
        data={'groups': 'hr'},
    )
    assert upload.status_code == 201, upload.text
    doc_id = upload.json()['id']
    detail = client.get(f'/api/documents/{doc_id}', headers=hr)
    assert detail.status_code == 200
    assert detail.json()['fields']['expiration_date']['value'] == '2027-02-01'
    assert client.get(f'/api/documents/{doc_id}',
                      headers=token('finance@northstar.demo')).status_code == 404
    assert client.get(f'/api/documents/{doc_id}',
                      headers=token('finance@otherco.demo')).status_code == 404
