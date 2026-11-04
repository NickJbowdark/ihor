import pytest


@pytest.fixture
def gdelt_payload():
    """A realistic GDELT DOC ArtList response, including deliberately bad records."""
    return {
        "articles": [
            {
                "url": "https://www.example.com/world/ukraine-talks/?utm_source=twitter",
                "url_mobile": "",
                "title": "Ukraine and Russia resume ceasefire talks in Istanbul - Example News",
                "seendate": "20261104T101500Z",
                "socialimage": "",
                "domain": "example.com",
                "language": "English",
                "sourcecountry": "United Kingdom",
            },
            {   # syndicated copy of the same wire story on another outlet
                "url": "https://another-paper.org/ukraine-russia-ceasefire",
                "title": "Ukraine and Russia resume ceasefire talks in Istanbul",
                "seendate": "20261104T103000Z",
                "domain": "another-paper.org",
                "language": "English",
                "sourcecountry": "United States",
            },
            {   # malicious link scheme -> must be dropped
                "url": "javascript:alert(1)",
                "title": "Click me",
                "seendate": "20261104T103000Z",
            },
            {   # missing date -> must be dropped
                "url": "https://example.org/x",
                "title": "No date here",
                "seendate": "garbage",
            },
            {   # HTML / control characters in the title -> cleaned
                "url": "https://news.example.net/sudan",
                "title": "<b>Sudan</b>: UN warns of famine‮ as refugees flee &amp; aid stalls",
                "seendate": "20261103T080000Z",
                "domain": "news.example.net",
                "language": "English",
                "sourcecountry": "Kenya",
            },
        ]
    }
