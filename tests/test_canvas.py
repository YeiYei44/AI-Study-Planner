from app.ingest.canvas import parse_next_link


def test_parse_next_link_present():
    header = (
        '<https://x.instructure.com/api/v1/courses?page=1&per_page=100>; rel="current",'
        '<https://x.instructure.com/api/v1/courses?page=2&per_page=100>; rel="next",'
        '<https://x.instructure.com/api/v1/courses?page=9&per_page=100>; rel="last"'
    )
    assert (
        parse_next_link(header)
        == "https://x.instructure.com/api/v1/courses?page=2&per_page=100"
    )


def test_parse_next_link_absent():
    assert parse_next_link('<https://x/api/v1/courses?page=1>; rel="last"') is None
    assert parse_next_link("") is None
    assert parse_next_link(None) is None
