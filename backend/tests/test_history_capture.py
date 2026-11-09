from datetime import UTC, datetime
import pytest



def test_timeline_preserves_actual_publication_and_provider_identity(app_env):
    from alphapulse.crawler.history_capture import timeline_articles
    result = timeline_articles({'hasMore': False, 'articles': [{'_id': 'original', 'pathname': '/markets/story',
        'title': 'Official title', 'description': 'Public summary',
        'articleDates': {'displayDate': '2024-12-31T17:00:00Z'}}]})
    assert result[0].guid == 'original'
    assert result[0].published_at == datetime(2024, 12, 31, 17, tzinfo=UTC)
    assert result[0].summary == 'Public summary'


def test_unknown_timeline_shape_is_not_empty_success(app_env):
    from alphapulse.crawler.history_capture import timeline_articles
    with pytest.raises(ValueError):
        timeline_articles({'articles': []})


def test_ecb_index_keeps_existing_news_scope(app_env):
    from alphapulse.crawler.history_capture import ecb_articles
    row = [1, 1735732800, 2025, 0, 1, None, None, None, None,
           ['/press/key/date/2025/html/example.en.html', '/stats/example.html'], {'Title': 'Speech'}, [], []]
    result = ecb_articles(row)
    assert len(result) == 1
    assert '/press/key/' in str(result[0].url)
    assert result[0].published_at == datetime.fromtimestamp(1735732800, UTC)
    with pytest.raises(ValueError):
        ecb_articles(row[:-1])
