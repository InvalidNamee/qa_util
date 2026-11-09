from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_url(url):
    parts = urlsplit(url)
    route, _, query = parts.fragment.partition("?")
    params = sorted((key, value) for key, value in parse_qsl(query) if key not in {"VNK", "aid"})
    return urlunsplit(parts._replace(fragment=route + ("?" + urlencode(params) if params else "")))
