from app.ingest.cookies import filter_domain, load_cookies

URL = "https://fultonschools.instructure.com"


def test_raw_cookie_header():
    cookies = load_cookies(
        "Cookie: _ga=GA1.2.x; canvas_session=abc123; _csrf_token=tok", URL
    )
    assert [c["name"] for c in cookies] == ["_ga", "canvas_session", "_csrf_token"]
    assert all(c["url"] == URL for c in cookies)
    assert cookies[1]["value"] == "abc123"


def test_header_without_prefix_and_value_with_equals():
    cookies = load_cookies("canvas_session=a=b=c; x=1", URL)
    assert cookies[0] == {"name": "canvas_session", "value": "a=b=c", "url": URL}


def test_curl_command():
    text = (
        "curl 'https://fultonschools.instructure.com/api/v1/users/self' "
        "-H 'accept: application/json' "
        "-H 'cookie: canvas_session=xyz; _csrf_token=t' --compressed"
    )
    cookies = load_cookies(text, URL)
    assert [c["name"] for c in cookies] == ["canvas_session", "_csrf_token"]


def test_json_extension_export():
    text = (
        '[{"name":"canvas_session","value":"v","domain":".instructure.com",'
        '"path":"/","secure":true,"sameSite":"no_restriction",'
        '"expirationDate":1893456000.5}]'
    )
    (cookie,) = load_cookies(text, URL)
    assert cookie["name"] == "canvas_session"
    assert cookie["domain"] == ".instructure.com"
    assert cookie["sameSite"] == "None"
    assert cookie["expires"] == 1893456000


def test_json_samesite_none_downgraded_when_insecure():
    text = '[{"name":"a","value":"b","domain":"x.instructure.com","secure":false,"sameSite":"no_restriction"}]'
    (cookie,) = load_cookies(text, URL)
    assert cookie["sameSite"] == "Lax"


def test_storage_state_shape():
    text = '{"cookies":[{"name":"a","value":"b","domain":"x.instructure.com","path":"/"}],"origins":[]}'
    (cookie,) = load_cookies(text, URL)
    assert cookie["name"] == "a"


def test_filter_domain_matches_url_or_domain():
    mixed = [
        {"name": "a", "value": "1", "url": URL},
        {"name": "b", "value": "2", "domain": ".instructure.com"},
        {"name": "c", "value": "3", "domain": "example.com"},
    ]
    kept = filter_domain(mixed, "instructure.com")
    assert [c["name"] for c in kept] == ["a", "b"]
