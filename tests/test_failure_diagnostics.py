"""Failed validation must have readable, escaped diagnostics—not a broken detail view."""
import pytest
from bs4 import BeautifulSoup
from conftest import admin_client, get_csrf, wait_for


@pytest.mark.parametrize('path,status,reason', [
    ('/docs/missing.pdf','INVALID','404'),
    ('/docs/forbidden.pdf','INVALID','403'),
    ('/docs/fake.pdf','PDF_INVALID','signature'),
])
def test_failed_resource_details_render_actual_reason(client, pdf_server_url, path, status, reason, monkeypatch):
    admin_client(client)
    submitted = client.post('/api/index/validate', json={'urls':[pdf_server_url + path + '?diagnostics=1']},
                            headers={'X-CSRF-Token': get_csrf(client)})
    assert submitted.status_code == 202
    source = pdf_server_url + path + '?diagnostics=1'
    record = wait_for(client, '/api/index/status?url=' + source,
                      lambda row: row.get('submissionStatus') == 'VALIDATION_FAILED')
    assert reason in record['error'].lower()
    assert record['referencePage'] is None
    assert record['externalIndexStatus'] == record['referenceIndexStatus'] == 'UNKNOWN'
    response = client.get(f"/pdfs/{record['id']}")
    assert response.status_code == 200
    doc = BeautifulSoup(response.text, 'html.parser')
    assert 'Last error' in doc.get_text() and record['error'] in doc.get_text()
    assert status.replace('_',' ') in doc.get_text()

    # Remote-derived failure messages must never become executable HTML.
    get = client.app.state.repos.pdfs.get
    message = 'Fetch failed: <script id="diagnostic-probe">alert(1)</script> & retry'
    async def injected_error(record_id):
        row = await get(record_id)
        return {**row, 'error':message} if row and record_id==record['id'] else row
    monkeypatch.setattr(client.app.state.repos.pdfs, 'get', injected_error)
    result = client.get(f"/pdfs/{record['id']}")
    doc = BeautifulSoup(result.text, 'html.parser')
    assert result.status_code == 200
    assert message in doc.get_text()
    assert doc.find('script', id='diagnostic-probe') is None


def test_submit_page_does_not_offer_unverified_external_url_as_sample(client):
    admin_client(client)
    page = client.get('/submit')
    assert page.status_code == 200
    assert 'not a working test URL' in page.text
    assert 'nainaxcvv-renew-2.pdf' not in page.text
