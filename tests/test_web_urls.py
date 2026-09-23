"""Normal public web pages are resources, not failed PDF downloads."""
import pytest
from bs4 import BeautifulSoup

from app.pdf.html import extract_html, is_html_response
from conftest import admin_client, get_csrf, wait_for
from test_discovery_fallback import guest, submit


@pytest.mark.parametrize('path', ['/', '/pages/about.php', '/blog/article', '/blog/heading-only',
                                 '/blog/generic', '/blog/plain', '/blog/xhtml', '/go/about'])
def test_normal_webpage_publishes_with_correct_type_and_source_link(client, pdf_server_url, path):
    url = pdf_server_url + path + '?web-url=1'
    row = submit(client, url)
    assert row['submissionStatus'] == 'DISCOVERY_SUBMITTED'
    assert row['type'] == 'HTML' and row['classification'] == 'HTML'
    assert row['pages'] is None
    assert row['htmlMetadata']['title'] == 'Browser safety and document references'
    assert row['textLength'] >= 120 and row['sha256']
    assert row['referencePath'] == '/jobs/' + str(row['jobId'])
    assert row['referenceSubmissionStatus'] == 'UNSUPPORTED'
    assert row['referenceIndexStatus'] == row['externalIndexStatus'] == 'UNKNOWN'
    assert not row['referenceSubmissionResult']['googleRequestMade']
    if path == '/go/about':
        assert row['finalUrl'].endswith('/pages/about.php')
        assert row['redirectChain'] == [url]
    public = guest(client)
    response = public.get(row['referencePath'])
    assert response.status_code == public.head(row['referencePath']).status_code == 200
    page = BeautifulSoup(response.text, 'html.parser')
    anchor = page.find('a', href=url)
    assert anchor and anchor.get_text(strip=True) == 'Open Original Web Page'
    assert 'noopener' in anchor['rel']
    assert 'Generated content · not a live vacancy' in page.get_text()
    assert 'JobPosting' not in response.text
    assert '%PDF-' not in response.text
    assert row['referencePage'] in public.get('/sitemap.xml').text
    assert row['referencePage'] in public.get('/rss.xml').text
    # Operator detail must not describe valid HTML as an unvalidated PDF.
    detail = client.get(f"/pdfs/{row['id']}")
    assert detail.status_code == 200
    text = BeautifulSoup(detail.text, 'html.parser').get_text(' ', strip=True)
    assert 'Webpage analysis' in text and 'Content validated Yes' in text
    assert 'PDF analysis' not in text and 'Valid PDF' not in text
    assert 'Source canonical' in text and 'Source robots' in text


def test_extensionless_pdf_is_detected_from_bytes(client, pdf_server_url):
    row = submit(client, pdf_server_url + '/download?web-url=1')
    assert row['type'] == 'PDF' and row['pages'] == 1
    assert row['referencePath'].startswith('/jobs/')
    assert 'View Original PDF' in guest(client).get(row['referencePath']).text


@pytest.mark.parametrize('path,classification,reason', [
    ('/api/document', 'UNSUPPORTED_CONTENT', 'Unsupported source content type'),
    ('/docs/fake.pdf', 'INVALID_PDF', 'signature'),
    ('/blog/js-only', 'HTML_REJECTED', 'extractable page text'),
    ('/blog/noindex', 'HTML_REJECTED', 'noindex'),
])
def test_unsupported_and_blocked_sources_do_not_become_fake_pages(client, pdf_server_url, path, classification, reason):
    row = submit(client, pdf_server_url + path + '?web-url=blocked')
    assert row['submissionStatus'] == 'VALIDATION_FAILED'
    assert row['classification'] == classification
    assert reason in row['error']
    assert row['referencePage'] is None


def test_mixed_pdf_and_webpage_batch_and_duplicate_handling(client, pdf_server_url):
    admin_client(client)
    urls = [pdf_server_url + '/blog/article?batch=web', pdf_server_url + '/docs/report.pdf?batch=web']
    response = client.post('/api/index/validate', json={'urls': urls + [urls[0] + '#section']},
                           headers={'X-CSRF-Token': get_csrf(client)})
    assert response.status_code == 202
    assert len(response.json()['accepted']) == 2 and len(response.json()['duplicates']) == 1
    for url, kind in zip(urls, ('HTML', 'PDF')):
        row = wait_for(client, '/api/index/status?url=' + url,
                       lambda r: r.get('referenceSubmissionStatus') == 'UNSUPPORTED')
        assert row['type'] == kind and row['referencePage']


@pytest.mark.parametrize('mime', [None, '', 'text/plain', 'application/octet-stream', 'text/html; charset=UTF-8', 'application/xhtml+xml'])
def test_bounded_html_detection_handles_normal_and_generic_mime(mime):
    body = b'\xef\xbb\xbf <!-- comment --> <!DOCTYPE HTML><html><head><title>Source</title></head></html>'
    assert is_html_response(body, mime)


@pytest.mark.parametrize('body,mime', [
    (b'{"value": "<html>not a page</html>"}', 'application/json'),
    (b'<html><body>Do not reinterpret explicit image content</body></html>', 'image/png'),
    (b'\x89PNG\x00binary', 'application/octet-stream'),
    (b'Plain text, not HTML markup.', 'text/plain'),
    (b' ' * 4096 + b'<html>outside the sniffing limit</html>', None),
    (b'<!-- comment -->' * 200 + b'not html', None),
])
def test_html_sniffer_does_not_accept_arbitrary_downloads(body, mime):
    assert not is_html_response(body, mime)


def test_html_heading_fallback_is_extracted_not_invented():
    body = b'<html><head></head><body><main><h1>Actual source heading</h1><p>' + b'Useful information for readers. ' * 10 + b'</p></main></body></html>'
    metadata = extract_html(body, 'https://public-site.invalid/about')
    assert metadata['title'] == 'Actual source heading' and metadata['publishable']
    assert not extract_html(body.replace(b'<h1>Actual source heading</h1>', b''), 'https://public-site.invalid/about')['publishable']


def test_submission_ui_explicitly_accepts_webpages(client):
    admin_client(client)
    for path in ('/submit', '/urls'):
        page = client.get(path)
        assert page.status_code == 200
        assert 'webpage' in page.text.lower()
        assert 'https://example.com/document.pdf' in page.text
    assert 'A .pdf extension is not required' in client.get('/submit').text


@pytest.mark.parametrize('entry', ['single', 'bulk', 'file'])
def test_all_existing_intake_methods_accept_webpages(client, pdf_server_url, entry):
    admin_client(client)
    url = pdf_server_url + '/pages/about.php?entry=' + entry
    headers = {'X-CSRF-Token': get_csrf(client)}
    if entry == 'file':
        response = client.post('/api/pdfs/file', files={'file': ('urls.txt', url.encode(), 'text/plain')}, headers=headers)
    elif entry == 'bulk':
        response = client.post('/api/pdfs/bulk', json={'urls': [url]}, headers=headers)
    else:
        response = client.post('/api/pdfs', json={'url': url}, headers=headers)
    assert response.status_code == 200 and len(response.json()['accepted']) == 1
    row = wait_for(client, '/api/index/status?url=' + url,
                   lambda r: r.get('referenceSubmissionStatus') == 'UNSUPPORTED')
    assert row['type'] == 'HTML' and row['referencePage']
