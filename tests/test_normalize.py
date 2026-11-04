from gim.pipeline.normalize import canonicalize_url, clean_text, is_safe_url, title_fingerprint


def test_canonicalize_strips_tracking_and_www():
    a = canonicalize_url("http://WWW.Example.com/a/b/?utm_source=x&id=5&fbclid=abc#top")
    b = canonicalize_url("https://example.com/a/b?id=5")
    assert a == b == "https://example.com/a/b?id=5"


def test_is_safe_url():
    assert is_safe_url("https://example.com/x")
    assert not is_safe_url("javascript:alert(1)")
    assert not is_safe_url("data:text/html,hi")
    assert not is_safe_url("https://")


def test_clean_text_removes_html_and_control_chars():
    assert clean_text("<i>Hello</i>‮  &amp;\x00 world") == "Hello & world"
    assert len(clean_text("x" * 2000)) == 500


def test_fingerprint_ignores_outlet_suffix_case_and_order_words():
    a = title_fingerprint("Ukraine and Russia resume ceasefire talks - BBC News")
    b = title_fingerprint("UKRAINE AND RUSSIA RESUME CEASEFIRE TALKS")
    c = title_fingerprint("Sudan army retakes Khartoum airport")
    assert a == b
    assert a != c
