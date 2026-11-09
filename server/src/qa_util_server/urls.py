import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit


def canonical_url(url):
    parts = urlsplit(url)
    route, _, query = parts.fragment.partition("?")
    params = sorted((key, value) for key, value in parse_qsl(query) if key not in {"VNK", "aid"})
    return urlunsplit(parts._replace(fragment=route + ("?" + urlencode(params) if params else "")))


def question_url(url, forum_url):
    candidate, forum = urlsplit(url), urlsplit(forum_url)
    course = re.search(r"/web/(?:home|myJoin)/([^/?]+)", forum.fragment)
    detail = re.fullmatch(r"/web/questionDetail/([^/?]+)/([^/?]+)(?:\?.*)?", candidate.fragment)
    if (candidate.scheme, candidate.netloc, candidate.path) != (forum.scheme, forum.netloc, forum.path) or not detail:
        raise ValueError("题目链接不属于当前论坛")
    if course and detail[1] != course[1]:
        raise ValueError("题目链接的课程不符")
    recruit = parse_qs(forum.fragment.partition("?")[2]).get("recruitId")
    if recruit and parse_qs(candidate.fragment.partition("?")[2]).get("recruitId") != recruit:
        raise ValueError("题目链接的教学班不符")
    return canonical_url(url)
