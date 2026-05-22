import re
import html as html_module
import sys
import time
import urllib.parse

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import (
    ExtractorError,
    int_or_none,
    float_or_none,
    traverse_obj,
    url_or_none,
)


class _Anime365Base(InfoExtractor):
    _DOMAINS = (
        r'(?:smotret-anime\.(?:org|app|net|com|online)'
        r'|anime-365\.ru'
        r'|hentai365\.ru'
        r'|anime365\.ru'
        r'|smotretanime\.ru)'
    )
    _API_BASE = 'https://smotret-anime.org'

    # ------------------------------------------------------------------ #
    #  API helpers                                                         #
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    #  Embed page                                                          #
    # ------------------------------------------------------------------ #

    def _apply_subtitle_defaults(self):
        """Enable subtitle download by default unless user explicitly opted out."""
        # yt-dlp always sets writesubtitles=False in params even when not specified,
        # so setdefault() won't work — we need direct assignment.
        # Check sys.argv to honour explicit --no-write-subs / --no-subs.
        user_opted_out = any(a in sys.argv for a in ('--no-write-subs', '--no-subs'))
        if user_opted_out:
            return
        params = self._downloader.params
        if not params.get('writesubtitles') and not params.get('writeautomaticsub'):
            params['writesubtitles'] = True
            if not params.get('subtitleslangs'):
                params['subtitleslangs'] = ['ru']

    def _fetch_embed_page(self, translation_id, page_url):
        """
        Fetch embed page, check auth status, return raw HTML.

        Raises login_required if isPremiumUser is False (not logged-in / no premium).
        """
        self._apply_subtitle_defaults()

        # Warm-up: visit the episode page first so the server validates the session
        # (without this the server may set a new unauthenticated session on the first
        # embed request, causing isPremiumUser=false even when the user is logged in)
        self._download_webpage(
            page_url,
            translation_id,
            headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            },
            note='Warming up session',
            fatal=False,
        )

        embed_url = f'{self._API_BASE}/translations/embed/{translation_id}'
        webpage = self._download_webpage(
            embed_url,
            translation_id,
            headers={
                'Referer': page_url,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
            },
            note=f'Downloading embed page {translation_id}',
            fatal=False,
        )
        if not webpage:
            return None

        # In simulate mode (-s only) — enumerate CDN options and pick fastest
        # Exclude --list-subs, --list-formats, etc. which also set simulate=True
        if (self.get_param('simulate')
                and not self.get_param('listsubtitles')
                and not self.get_param('listformats')):
            self._probe_and_set_best_cdn(translation_id, page_url)

        # Parse site config: {'isPremiumUser': true/false, 'csrf': '...', ...}
        cfg_str = self._search_regex(
            r"var\s+site\s*=\s*(\{[^;]+\})\s*;",
            webpage, 'site config', default='{}',
        )
        is_premium = bool(re.search(r"isPremiumUser['\"]?\s*:\s*true", cfg_str))

        if not is_premium:
            self.raise_login_required(
                'Требуется Premium-аккаунт на smotret-anime.org. '
                'Зарегистрируйтесь, оформите подписку и войдите через браузер (VK), '
                'затем запустите yt-dlp с --cookies-from-browser.',
                method='cookies',
            )

        return webpage

    def _extract_formats_from_embed(self, webpage, translation_id):
        """
        Parse the <video data-sources="..."> attribute from the embed page.

        Returns (formats_list, subtitles_dict).
        The CDN URLs inside data-sources are directly accessible.
        """
        if not webpage:
            return [], {}

        # ---- video attributes ---------------------------------------- #
        video_tag = self._search_regex(
            r'(<video\b[^>]+id=["\']main-video["\'][^>]*>)',
            webpage, 'video tag', default=None,
        )
        if not video_tag:
            return [], {}

        def _attr(name):
            m = re.search(name + r'=["\']([^"\']*)["\']', video_tag)
            return html_module.unescape(m.group(1)) if m else ''

        def _abs_url(path):
            path = path.strip()
            if not path:
                return None
            if path.startswith('/'):
                path = self._API_BASE + path
            return url_or_none(path)

        sources_raw = _attr('data-sources')
        vtt_url = _abs_url(_attr('data-vtt'))
        sub_url = _abs_url(_attr('data-subtitles'))
        framerate = float_or_none(_attr('data-framerate')) or None

        # ---- parse data-sources JSON ---------------------------------- #
        formats = []
        if sources_raw:
            try:
                sources = self._parse_json(sources_raw, translation_id, fatal=False) or []
            except Exception:
                sources = []

            for src in sources:
                height = int_or_none(src.get('height'))
                label = f'{height}p' if height else 'unknown'
                cdn_urls = [url_or_none(u) for u in (src.get('urls') or [])]
                cdn_urls = [u for u in cdn_urls if u]
                if not cdn_urls:
                    continue
                best_url = self._select_best_cdn(cdn_urls, translation_id, label)
                formats.append({
                    'url': best_url,
                    'ext': 'mp4',
                    'height': height,
                    'format_id': f'http-{height}p' if height else 'http',
                    'fps': framerate,
                })

        # ---- subtitles ----------------------------------------------- #
        subtitles = {}
        if vtt_url:
            subtitles.setdefault('ru', []).append({'url': vtt_url, 'ext': 'vtt'})
        if sub_url and sub_url != vtt_url:
            sub_path = urllib.parse.urlparse(sub_url).path
            ext = 'ass' if '/ass/' in sub_path or sub_path.endswith('.ass') else \
                  'srt' if sub_path.endswith('.srt') else 'vtt'
            subtitles.setdefault('ru', []).append({'url': sub_url, 'ext': ext})

        return formats, subtitles

    # ------------------------------------------------------------------ #
    #  Common metadata                                                     #
    # ------------------------------------------------------------------ #

    def _build_episode_info(self, episode_data, series_data, video_id):
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

    # ------------------------------------------------------------------ #
    #  CDN speed probing via profile settings                             #
    # ------------------------------------------------------------------ #

    _CDN_CACHE_SECTION = 'anime365'
    _CDN_CACHE_KEY = 'best_cdn_value'
    _PROFILE_URL = f'{_API_BASE}/users/profile'

    def _probe_cdn_speed(self, url, video_id):
        """Fetch first 128 KiB from url, return speed in bytes/sec (0 on error)."""
        try:
            t0 = time.monotonic()
            resp = self._request_webpage(
                url, video_id,
                headers={'Range': 'bytes=0-131071'},
                note=False, errnote=False, fatal=False,
            )
            if not resp:
                return 0.0
            data = resp.read(131072)
            elapsed = time.monotonic() - t0
            return len(data) / elapsed if elapsed > 0 else 0.0
        except Exception:
            return 0.0

    def _fetch_profile_form(self):
        """
        Fetch /users/profile, return (form_data_dict, cdn_options, current_cdn_value).
        form_data_dict contains all non-password inputs suitable for re-POST.
        cdn_options is list of (value, label) tuples.
        """
        html = self._download_webpage(
            self._PROFILE_URL, 'cdn-probe',
            note='Fetching profile for CDN probe',
            fatal=False,
        )
        if not html:
            return {}, [], None

        # Collect all form fields except password
        form_data = {'yt0': ''}  # submit button name
        for m in re.finditer(r'<input\b([^>]*)>', html, re.IGNORECASE):
            attrs = m.group(1)
            name_m = re.search(r'\bname="([^"]+)"', attrs)
            val_m = re.search(r'\bvalue="([^"]*)"', attrs)
            type_m = re.search(r'\btype="([^"]+)"', attrs, re.IGNORECASE)
            if not name_m:
                continue
            name = name_m.group(1)
            val = val_m.group(1) if val_m else ''
            type_ = (type_m.group(1) if type_m else 'text').lower()
            if type_ in ('hidden', 'text', 'email'):
                form_data[name] = val
            elif type_ == 'checkbox' and 'checked' in attrs:
                form_data[name] = val

        # Parse CDN select
        sel_m = re.search(
            r'<select\b[^>]+name="Users\[useOtherServers\]"[^>]*>(.*?)</select>',
            html, re.DOTALL | re.IGNORECASE,
        )
        cdn_options, current_val = [], None
        if sel_m:
            for opt in re.finditer(r'<option\b([^>]*)>([^<]+)</option>', sel_m.group(1)):
                attrs, label = opt.group(1), html_module.unescape(opt.group(2)).strip()
                val_m = re.search(r'\bvalue="(\d+)"', attrs)
                if not val_m:
                    continue
                val = val_m.group(1)
                cdn_options.append((val, label))
                if 'selected' in attrs:
                    current_val = val

        return form_data, cdn_options, current_val

    def _set_profile_cdn(self, form_data, cdn_value, label):
        """POST /users/profile to change CDN setting."""
        data = dict(form_data)
        data['Users[useOtherServers]'] = cdn_value
        self._download_webpage(
            self._PROFILE_URL, 'cdn-probe',
            data=urllib.parse.urlencode(data).encode(),
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Referer': self._PROFILE_URL,
            },
            note=f'Setting CDN → {label}',
            fatal=False,
        )

    def _get_embed_top_url(self, translation_id, page_url):
        """Fetch embed page and return the first CDN URL (highest quality)."""
        html = self._download_webpage(
            f'{self._API_BASE}/translations/embed/{translation_id}',
            'cdn-probe',
            headers={'Referer': page_url},
            note=False,
            fatal=False,
        )
        if not html:
            return None
        video_tag = self._search_regex(
            r'(<video\b[^>]+id=["\']main-video["\'][^>]*>)',
            html, 'video tag', default=None,
        )
        if not video_tag:
            return None
        src_m = re.search(r'data-sources=["\']([^"\']*)["\']', video_tag)
        if not src_m:
            return None
        try:
            sources = self._parse_json(
                html_module.unescape(src_m.group(1)), 'cdn-probe', fatal=False,
            ) or []
        except Exception:
            return None
        for src in sorted(sources, key=lambda x: int_or_none(x.get('height')) or 0, reverse=True):
            for u in (src.get('urls') or []):
                u = url_or_none(u)
                if u:
                    return u
        return None

    def _probe_and_set_best_cdn(self, translation_id, page_url):
        """
        Enumerate all CDN options from the user profile, measure download speed
        for each, then permanently set the fastest one in the profile.
        Called only in probe mode (-s / -F / --skip-download) and at most once
        per session.
        """
        if getattr(self, '_cdn_probe_done', False):
            return
        self._cdn_probe_done = True

        form_data, cdn_options, original_val = self._fetch_profile_form()
        if not cdn_options:
            self.report_warning('CDN probe: не удалось получить список каналов из профиля')
            return

        speeds = {}
        for cdn_val, cdn_label in cdn_options:
            self._set_profile_cdn(form_data, cdn_val, cdn_label)
            cdn_url = self._get_embed_top_url(translation_id, page_url)
            if not cdn_url:
                self.report_warning(f'CDN probe: {cdn_label} — URL не получен, пропуск')
                continue
            host = urllib.parse.urlparse(cdn_url).netloc
            speed = self._probe_cdn_speed(cdn_url, 'cdn-probe')
            speeds[cdn_val] = (speed, host, cdn_label)
            self.report_warning(
                f'CDN [{cdn_val}] {cdn_label}: {speed / 1024:.0f} KiB/s  ({host})'
            )

        if not speeds:
            # Restore original if nothing worked
            if original_val:
                orig_label = next((l for v, l in cdn_options if v == original_val), original_val)
                self._set_profile_cdn(form_data, original_val, orig_label)
            return

        best_val = max(speeds, key=lambda v: speeds[v][0])
        best_speed, best_host, best_label = speeds[best_val]
        self.report_warning(
            f'CDN → лучший: [{best_val}] {best_label}  ({best_host}, {best_speed / 1024:.0f} KiB/s) — сохранено в профиль'
        )
        self._set_profile_cdn(form_data, best_val, best_label)
        self._downloader.cache.store(self._CDN_CACHE_SECTION, self._CDN_CACHE_KEY, best_val)

    def _select_best_cdn(self, urls, video_id, label):
        """
        Fallback selector used when data-sources contains multiple URLs for one quality.
        In normal usage the site returns one URL, so this mostly shows an info message.
        """
        host = urllib.parse.urlparse(urls[0]).netloc
        self.to_screen(f'{video_id}: CDN [{label}]: {host}')
        return urls[0]

    # ------------------------------------------------------------------ #
    #  Best translation                                                    #
    # ------------------------------------------------------------------ #

    def _best_translation(self, translations):
        """Return the translation with the highest priority value."""
        active = [t for t in translations if t.get('isActive')]
        if not active:
            return None
        return max(active, key=lambda t: int_or_none(t.get('priority')) or 0)


# ====================================================================== #
#  Extractors                                                             #
# ====================================================================== #

class Anime365IE(_Anime365Base):
    """
    Extractor for a specific translation (sub/dub) of an anime episode.

    Fetches embed page → parses data-sources → CDN URLs (no mp4Stream).

    URL examples:
      https://smotret-anime.org/catalog/sousou-no-frieren-30414/1-seriya-291395/russkie-subtitry-5526683
    """
    IE_NAME = 'anime365'
    IE_DESC = 'Anime365 / Смотреть Аниме — перевод'

    _VALID_URL = (
        r'https?://(?:www\.)?' + _Anime365Base._DOMAINS
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
        'skip': 'Requires Premium account cookies',
    }]

    def _real_extract(self, url):
        mobj = self._match_valid_url(url)
        translation_id = mobj.group('id')
        episode_id = mobj.group('episode_id')
        series_id = mobj.group('series_id')

        # Fetch embed page → validates auth + gets CDN sources
        webpage = self._fetch_embed_page(translation_id, url)

        formats, subtitles = self._extract_formats_from_embed(webpage, translation_id)
        if not formats:
            raise ExtractorError(
                'Не удалось извлечь источники видео. '
                'Убедитесь что вы залогинены на smotret-anime.org с Premium-аккаунтом.',
                expected=True,
            )

        # Metadata from API
        episode_data = self._call_api(
            f'episodes/{episode_id}/',
            translation_id,
            note='Downloading episode data',
            query={'fields': 'id,episodeFull,episodeInt,episodeTitle,episodeType,seriesId'},
        )
        series_data = self._call_api(
            f'series/{series_id}/',
            translation_id,
            note='Downloading series data',
            query={'fields': 'id,titles,posterUrl,descriptions,year,season,type,isHentai'},
        )

        info = self._build_episode_info(episode_data, series_data, translation_id)
        info.update({
            'id': translation_id,
            'formats': formats,
            'subtitles': subtitles,
        })
        return info


class Anime365EpisodeIE(_Anime365Base):
    """
    Extractor for an anime episode page.

    Picks the highest-priority translation, fetches its embed → CDN URLs.

    URL example:
      https://smotret-anime.org/catalog/sousou-no-frieren-30414/1-seriya-291395/
    """
    IE_NAME = 'anime365:episode'
    IE_DESC = 'Anime365 / Смотреть Аниме — эпизод (лучший перевод)'

    _VALID_URL = (
        r'https?://(?:www\.)?' + _Anime365Base._DOMAINS
        + r'/catalog/[^/?#]+-(?P<series_id>\d+)'
        + r'/[^/?#]+-(?P<id>\d+)/?(?:[?#].*)?$'
    )

    _TESTS = [{
        'url': 'https://smotret-anime.org/catalog/sousou-no-frieren-30414/1-seriya-291395/',
        'info_dict': {
            'id': '291395',
            'title': str,
        },
        'skip': 'Requires Premium account cookies',
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
        series_data = self._call_api(
            f'series/{series_id}/',
            episode_id,
            note='Downloading series data',
            query={'fields': 'id,titles,posterUrl,descriptions,year,season,type,isHentai'},
        )

        translations = episode_data.get('translations') or []
        best = self._best_translation(translations)
        if not best:
            raise ExtractorError('No active translations found for this episode', expected=True)

        translation_id = str(best['id'])
        webpage = self._fetch_embed_page(translation_id, url)
        formats, subtitles = self._extract_formats_from_embed(webpage, translation_id)

        if not formats:
            raise ExtractorError(
                'Не удалось извлечь источники видео. '
                'Убедитесь что вы залогинены на smotret-anime.org с Premium-аккаунтом.',
                expected=True,
            )

        info = self._build_episode_info(episode_data, series_data, episode_id)
        info.update({
            'id': episode_id,
            'formats': formats,
            'subtitles': subtitles,
        })
        return info


class Anime365SeriesIE(_Anime365Base):
    """
    Extractor for an anime series — yields a playlist of all episodes.

    Each episode entry uses the best (highest-priority) translation.

    URL example:
      https://smotret-anime.org/catalog/sousou-no-frieren-30414/
    """
    IE_NAME = 'anime365:series'
    IE_DESC = 'Anime365 / Смотреть Аниме — сериал (все эпизоды)'

    _VALID_URL = (
        r'https?://(?:www\.)?' + _Anime365Base._DOMAINS
        + r'/catalog/(?P<id>[^/?#]+-\d+)/?(?:[?#].*)?$'
    )

    _TESTS = [{
        'url': 'https://smotret-anime.org/catalog/sousou-no-frieren-30414/',
        'info_dict': {
            'id': 'sousou-no-frieren-30414',
            'title': str,
        },
        'playlist_mincount': 1,
        'skip': 'Requires Premium account cookies',
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
            best = self._best_translation(translations)
            if not best:
                continue

            translation_id = str(best['id'])
            webpage = self._fetch_embed_page(translation_id, url)
            formats, subtitles = self._extract_formats_from_embed(webpage, translation_id)
            if not formats:
                continue

            ep_full = ep_data.get('episodeFull') or str(ep_id)
            ep_title_part = ep_data.get('episodeTitle') or ''
            title_parts = [p for p in [series_title, ep_full, ep_title_part] if p]

            entries.append({
                'id': str(ep_id),
                'title': ' - '.join(title_parts),
                'formats': formats,
                'subtitles': subtitles,
                'thumbnail': thumbnail,
                'series': series_title,
                'episode': ep_full,
                'episode_number': int_or_none(ep_data.get('episodeInt')),
                'year': int_or_none(series_data.get('year')),
                'age_limit': 18 if series_data.get('isHentai') else None,
            })

        return self.playlist_result(entries, series_slug, series_title)
