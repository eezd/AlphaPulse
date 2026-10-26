from datetime import date
from pathlib import Path

import pytest

from alphapulse.calendar.europe_archives import destatis_links


@pytest.mark.parametrize("start,expected,has_next", [
    (date(2025, 1, 1), [], False),
    (date(2024, 1, 1), ["EN/Press/2024/09/PE24_340_611.html", "EN/Press/2024/08/PE24_329_611.html"], True),
])
def test_destatis_archive_only_uses_filtered_results_for_links_and_date_boundary(start, expected, has_next):
    body = (Path(__file__).parent / "fixtures/europe/de_archive_before_start.html").read_text()
    links, following = destatis_links(body, start, date(2026, 10, 26))
    assert links == ["https://www.destatis.de/" + path for path in expected]
    assert (following is not None) is has_next


def test_destatis_archive_rejects_a_page_without_the_filtered_result_block():
    with pytest.raises(ValueError, match="筛选结果"):
        destatis_links("<html><body>Access denied</body></html>", date(2025, 1, 1), date(2026, 10, 26))
