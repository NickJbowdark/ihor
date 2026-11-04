from gim.pipeline.classify import classify_title


def test_basic_categories():
    cats = classify_title("Ukraine and Russia resume ceasefire talks in Istanbul")
    assert "conflict" in cats and "diplomatic" in cats


def test_no_false_positive_on_word_prefix():
    assert "conflict" not in classify_title("Film wins award at festival")
    assert "conflict" not in classify_title("Weather warning issued for coast")
    assert "humanitarian" not in classify_title("Senior aide resigns")


def test_war_and_aid_full_words():
    assert "conflict" in classify_title("The war enters its fourth year")
    assert "humanitarian" in classify_title("Aid convoy reaches Gaza")
