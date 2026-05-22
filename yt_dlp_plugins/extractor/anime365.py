import re

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import (
    ExtractorError,
    int_or_none,
    traverse_obj,
    url_or_none,
    urljoin,
)


class Anime365BaseIE(InfoExtractor):
    _DOMAINS = (
        r'(?:smotret-anime\.(?:org|app|net|com|online)'
        r'|anime-365\.ru'
        r'|hentai365\.ru'
        r'|anime365\.ru'
        r'|smotretanime\.ru)'
    )
    _API_BASE = 'https://smotret-anime.org'

    def _call_api(self, path, video_id, note=None, query=None, fatal=True):
        resp = self._download_json(
            f'{self._API_BASE}/api/{path}',
            video_id,
            note=note or f'Downloading {path}',
            query=query or {},
            headers={'Referer': f'{self._API_BASE}/'},
            fatal=fatal,
        )
        if resp is None:
            return None
        if 'error' in resp:
            msg = traverse_obj(resp, ('error', 'message')) or 'API error'
            if fatal:
                raise ExtractorError(msg, expected=True)
            return None
        return resp.get('data')

    def _get_series_title(self, titles):
        return (
            titles.get('ru')
            or titles.get('romaji')
            or titles.get('en')
            or titles.get('ja')
            or ''
        )

    def _build_formats(self, translations):
        """Convert translation objects into yt-dlp format dicts."""
        formats = []
        seen_ids = set()
        for t in translations:
            if not t.get('isActive'):
                continue
            tid = t.get('id')
            if not tid or tid in seen_ids:
                continue
            seen_ids.add(tid)

            type_label = t.get('type', 'unknown')   # subRu, voiceRu, raw …
            quality = t.get('qualityType', '')        # bd, tv
            authors = t.get('authorsSummary', '')

            format_id_parts = [type_label]
            if quality:
                format_id_parts.append(quality)
            if authors:
                # Sanitize for use as an identifier
                authors_safe = re.sub(r'[^\w\-]', '_', authors)[:40]
                format_id_parts.append(authors_safe)
            format_id = '-'.join(format_id_parts)

            embed_url = t.get('embedUrl') or f'{self._API_BASE}/translations/embed/{tid}'

            formats.append({
                'url': f'{self._API_BASE}/translations/mp4Stream/{tid}',
                'format_id': format_id,
                'format_note': f'{type_label} [{authors}]' if authors else type_label,
                'width': int_or_none(t.get('width')),
                'height': int_or_none(t.get('height')),
                'duration': float(t['duration']) if t.get('duration') else None,
                'ext': 'mp4',
                'http_headers': {
                    'Referer': embed_url,
                },
                # Metadata carried per-format so yt-dlp can use it
                '_translation_id': tid,
                '_type_kind': t.get('typeKind', ''),  # sub / voice / raw
                '_type_lang': t.get('typeLang', ''),  # ru / en / ja …
            })
        return formats

    def _extract_subtitles_from_embed(self, translation_id, embed_url):
        """
        Fetch the embed page and look for subtitle track URLs (VTT/ASS/SRT).
        Returns a subtitles dict compatible with yt-dlp info_dict.
        """
        webpage = self._download_webpage(
            embed_url, translation_id,
            note='Downloading embed page (subtitle search)',
            fatal=False,
        )
        if not webpage:
            return {}

        subtitles = {}
        # Video.js track configurations and plain URL references
        patterns = [
            (r'["\']([^"\']+\.vtt(?:\?[^"\']*)?)["\']', 'vtt'),
            (r'["\']([^"\']+\.ass(?:\?[^"\']*)?)["\']', 'ass'),
            (r'["\']([^"\']+\.srt(?:\?[^"\']*)?)["\']', 'srt'),
        ]
        for pattern, ext in patterns:
            for raw_url in re.findall(pattern, webpage):
                full_url = url_or_none(urljoin(self._API_BASE, raw_url))
                if not full_url:
                    continue
                # Try to find the language from nearby context (srclang attribute)
                lang_match = re.search(
                    r'srclang["\s:=]+["\']?(\w{2,5})["\']?',
                    webpage[:webpage.find(raw_url) + 200],
                )
                lang = lang_match.group(1) if lang_match else 'ru'
                subtitles.setdefault(lang, []).append({'url': full_url, 'ext': ext})

        return subtitles

    def _build_episode_info(self, episode_data, series_data, video_id):
        """Build common metadata fields from episode + series API data."""
        titles = series_data.get('titles') or {}
        series_title = self._get_series_title(titles)
        episode_full = episode_data.get('episodeFull') or ''
        episode_title = episode_data.get('episodeTitle') or ''

        title_parts = [p for p in [series_title, episode_full, episode_title] if p]
        title = ' - '.join(title_parts) if title_parts else video_id

        descriptions = series_data.get('descriptions') or []
        description = next((d.get('value') for d in descriptions if d.get('value')), None)

        return {
            'title': title,
            'series': series_title,
            'episode': episode_full,
            'episode_number': int_or_none(episode_data.get('episodeInt')),
            'thumbnail': url_or_none(series_data.get('posterUrl')),
            'description': description,
            'year': int_or_none(series_data.get('year')),
            'age_limit': 18 if series_data.get('isHentai') else None,
        }


class Anime365IE(Anime365BaseIE):
    """
    Extractor for a specific translation (sub/dub variant) of an anime episode.

    URL example:
      https://smotret-anime.org/catalog/sousou-no-frieren-30414/1-seriya-291395/russkie-subtitry-5526683
    """
    IE_NAME = 'anime365'
    IE_DESC = 'Anime365 / Смотреть Аниме — перевод (эпизод с конкретной озвучкой/субтитрами)'

    _VALID_URL = (
        r'https?://(?:www\.)?' + Anime365BaseIE._DOMAINS
        + r'/catalog/[^/?#]+-(?P<series_id>\d+)'
        + r'/[^/?#]+-(?P<episode_id>\d+)'
        + r'/[^/?#]+-(?P<id>\d+)/?(?:[?#].*)?$'
    )

    _TESTS = [{
        'url': 'https://smotret-anime.org/catalog/sousou-no-frieren-30414/1-seriya-291395/russkie-subtitry-5526683',
        'info_dict': {
            'id': '5526683',
            'ext': 'mp4',
            'title': str,
            'series': 'Провожающая в последний путь Фрирен',
        },
        'skip': 'Requires valid session cookies',
    }]

    def _real_extract(self, url):
        mobj = self._match_valid_url(url)
        translation_id = mobj.group('id')
        episode_id = mobj.group('episode_id')
        series_id = mobj.group('series_id')

        # Fetch episode (contains all translations for format selection)
        episode_data = self._call_api(
            f'episodes/{episode_id}/',
            translation_id,
            note='Downloading episode data',
            query={'fields': 'id,episodeFull,episodeInt,episodeTitle,episodeType,seriesId,translations'},
        )
        translations = episode_data.get('translations') or []

        # Fetch series for title, thumbnail, etc.
        series_data = self._call_api(
            f'series/{series_id}/',
            translation_id,
            note='Downloading series data',
            query={'fields': 'id,titles,posterUrl,descriptions,year,season,type,isHentai'},
        )

        formats = self._build_formats(translations)
        if not formats:
            raise ExtractorError('No active translations found for this episode', expected=True)

        # Find the requested translation for subtitle extraction
        target = next(
            (t for t in translations if str(t.get('id')) == translation_id),
            {},
        )
        embed_url = target.get('embedUrl') or f'{self._API_BASE}/translations/embed/{translation_id}'
        subtitles = self._extract_subtitles_from_embed(translation_id, embed_url)

        info = self._build_episode_info(episode_data, series_data, translation_id)
        info.update({
            'id': translation_id,
            'formats': formats,
            'subtitles': subtitles,
        })
        return info


class Anime365EpisodeIE(Anime365BaseIE):
    """
    Extractor for an anime episode page (all available translations as formats).

    URL example:
      https://smotret-anime.org/catalog/sousou-no-frieren-30414/1-seriya-291395/
    """
    IE_NAME = 'anime365:episode'
    IE_DESC = 'Anime365 / Смотреть Аниме — эпизод (все доступные переводы)'

    _VALID_URL = (
        r'https?://(?:www\.)?' + Anime365BaseIE._DOMAINS
        + r'/catalog/[^/?#]+-(?P<series_id>\d+)'
        + r'/[^/?#]+-(?P<id>\d+)/?(?:[?#].*)?$'
    )

    _TESTS = [{
        'url': 'https://smotret-anime.org/catalog/sousou-no-frieren-30414/1-seriya-291395/',
        'info_dict': {
            'id': '291395',
            'title': str,
        },
        'skip': 'Requires valid session cookies',
    }]

    def _real_extract(self, url):
        mobj = self._match_valid_url(url)
        episode_id = mobj.group('id')
        series_id = mobj.group('series_id')

        episode_data = self._call_api(
            f'episodes/{episode_id}/',
            episode_id,
            note='Downloading episode data',
            query={'fields': 'id,episodeFull,episodeInt,episodeTitle,episodeType,seriesId,translations'},
        )
        translations = episode_data.get('translations') or []

        series_data = self._call_api(
            f'series/{series_id}/',
            episode_id,
            note='Downloading series data',
            query={'fields': 'id,titles,posterUrl,descriptions,year,season,type,isHentai'},
        )

        formats = self._build_formats(translations)
        if not formats:
            raise ExtractorError('No active translations found for this episode', expected=True)

        # Use the first sub translation for subtitle extraction (best-effort)
        sub_translation = next(
            (t for t in translations if t.get('isActive') and t.get('typeKind') == 'sub'),
            None,
        )
        subtitles = {}
        if sub_translation:
            embed_url = sub_translation.get('embedUrl') or \
                f'{self._API_BASE}/translations/embed/{sub_translation["id"]}'
            subtitles = self._extract_subtitles_from_embed(str(sub_translation['id']), embed_url)

        info = self._build_episode_info(episode_data, series_data, episode_id)
        info.update({
            'id': episode_id,
            'formats': formats,
            'subtitles': subtitles,
        })
        return info


class Anime365SeriesIE(Anime365BaseIE):
    """
    Extractor for an anime series page — yields a playlist of all episodes.

    URL example:
      https://smotret-anime.org/catalog/sousou-no-frieren-30414/
    """
    IE_NAME = 'anime365:series'
    IE_DESC = 'Anime365 / Смотреть Аниме — сериал (все эпизоды)'

    _VALID_URL = (
        r'https?://(?:www\.)?' + Anime365BaseIE._DOMAINS
        + r'/catalog/(?P<id>[^/?#]+-\d+)/?(?:[?#].*)?$'
    )

    _TESTS = [{
        'url': 'https://smotret-anime.org/catalog/sousou-no-frieren-30414/',
        'info_dict': {
            'id': 'sousou-no-frieren-30414',
            'title': str,
        },
        'playlist_mincount': 1,
        'skip': 'Requires valid session cookies',
    }]

    def _real_extract(self, url):
        mobj = self._match_valid_url(url)
        series_slug = mobj.group('id')
        series_id = series_slug.rsplit('-', 1)[-1]

        series_data = self._call_api(
            f'series/{series_id}/',
            series_slug,
            note='Downloading series data',
            query={'fields': 'id,titles,posterUrl,descriptions,year,season,type,isHentai,episodes'},
        )
        episodes = series_data.get('episodes') or []

        titles = series_data.get('titles') or {}
        series_title = self._get_series_title(titles)
        thumbnail = url_or_none(series_data.get('posterUrl'))

        entries = []
        for ep in episodes:
            if not ep.get('isActive'):
                continue
            ep_id = ep.get('id')
            if not ep_id:
                continue

            # Fetch full episode data (with translations) lazily per episode
            ep_data = self._call_api(
                f'episodes/{ep_id}/',
                series_slug,
                note=f'Downloading episode {ep_id} data',
                query={'fields': 'id,episodeFull,episodeInt,episodeTitle,episodeType,seriesId,translations'},
                fatal=False,
            )
            if not ep_data:
                continue

            translations = ep_data.get('translations') or []
            formats = self._build_formats(translations)
            if not formats:
                continue

            ep_full = ep_data.get('episodeFull') or str(ep_id)
            ep_title_part = ep_data.get('episodeTitle') or ''
            title_parts = [p for p in [series_title, ep_full, ep_title_part] if p]
            ep_title = ' - '.join(title_parts)

            entries.append({
                'id': str(ep_id),
                'title': ep_title,
                'formats': formats,
                'thumbnail': thumbnail,
                'series': series_title,
                'episode': ep_full,
                'episode_number': int_or_none(ep_data.get('episodeInt')),
                'year': int_or_none(series_data.get('year')),
                'age_limit': 18 if series_data.get('isHentai') else None,
            })

        return self.playlist_result(entries, series_slug, series_title)
