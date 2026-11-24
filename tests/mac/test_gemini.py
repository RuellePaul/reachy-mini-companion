"""gemini.post against Gemini's answers: google.rpc error bodies, network failures, what a caller decides on."""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from mac_server import gemini
from mac_server.gemini import GeminiError

QUOTA = 'Le quota Gemini est atteint pour le moment.'


def _answer(status: int, body: Any = None, text: str | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = (text if text is not None else json.dumps(body)).encode()
    return response


def _quota(quota_id: str, delay: str | None) -> requests.Response:
    """A 429 as the API documents it: QuotaFailure names the quota spent, RetryInfo says when to come back."""
    details: list[dict[str, Any]] = [
        {
            '@type': 'type.googleapis.com/google.rpc.QuotaFailure',
            'violations': [{'quotaMetric': 'generativelanguage.googleapis.com/generate_requests', 'quotaId': quota_id}],
        },
        {'@type': 'type.googleapis.com/google.rpc.Help', 'links': [{'url': 'https://ai.google.dev/gemini-api'}]},
    ]
    if delay is not None:
        details.append({'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': delay})
    message = 'You exceeded your current quota, please check your plan and billing details.'
    return _answer(
        429, {'error': {'code': 429, 'message': message, 'status': 'RESOURCE_EXHAUSTED', 'details': details}}
    )


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """What reached requests.post; put the answer (a Response, or an exception to raise) at its end first."""
    calls: list[Any] = []

    def post(url: str, **kwargs: Any) -> requests.Response:
        answer = calls.pop()
        calls.append((url, kwargs))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(gemini.requests, 'post', post)
    return calls


def test_an_answer_is_its_json_object(sent: list[Any]) -> None:
    sent.append(_answer(200, {'steps': []}))
    assert gemini.post('key', 'interactions', {'model': 'm'}, 25) == {'steps': []}
    assert sent == [
        (f'{gemini.API}/interactions', {'json': {'model': 'm'}, 'headers': {'x-goog-api-key': 'key'}, 'timeout': 25})
    ]


@pytest.mark.parametrize('body', ['[1, 2]', '"ok"', '<html>proxy</html>'], ids=['list', 'string', 'html'])
def test_a_200_that_is_no_json_object_is_an_empty_answer_not_a_refusal(sent: list[Any], body: str) -> None:
    sent.append(_answer(200, text=body))
    assert gemini.post('key', 'interactions', {}, 25) == {}  # the caller finds no voice in it, and says so


def test_no_key_sends_nothing(sent: list[Any]) -> None:
    with pytest.raises(GeminiError, match='GEMINI_API_KEY manque') as error:
        gemini.post('', 'interactions', {}, 25)
    assert (error.value.status, sent) == (503, [])


@pytest.mark.parametrize(
    ('answer', 'per_day', 'after'),
    [
        (_quota('GenerateRequestsPerDayPerProjectPerModel', '41s'), True, 41.0),
        (_quota('GenerateRequestsPerMinutePerProjectPerModel', '7.5s'), False, 7.5),
        (_quota('GenerateRequestsPerMinutePerProjectPerModel', '1m30s'), False, None),
        (_quota('generate_requests_per_day_per_project', None), True, None),
        (_answer(429, {'error': {'code': 429, 'message': 'Quota exceeded: 100 requests per day.'}}), True, None),
        (
            _answer(429, {'error': {'message': 'Rate limit exceeded (limit: 10 requests per minute on Tier 1)'}}),
            False,
            None,
        ),
        (_answer(429, text='Too Many Requests'), False, None),
    ],
    ids=['per day', 'per minute', 'unreadable delay', 'quota id in snake case', 'plain message', 'tier 1', 'no json'],
)
def test_a_429_says_whether_the_day_is_spent_and_when_to_come_back(
    sent: list[Any], answer: requests.Response, per_day: bool, after: float | None
) -> None:
    """Per day: the scene voices move to the lite model until Pacific midnight; per minute: they wait it out."""
    sent.append(answer)
    with pytest.raises(GeminiError) as error:
        gemini.post('key', 'interactions', {}, 25)
    assert (str(error.value), error.value.status) == (QUOTA, 429)
    assert (error.value.per_day, error.value.retry_after_s) == (per_day, after)


@pytest.mark.parametrize(
    ('answer', 'status', 'message'),
    [
        (requests.Timeout('read timed out'), 504, "Gemini n'a pas répondu à temps."),
        (requests.ConnectionError('no route'), 502, 'Impossible de joindre Gemini\xa0: le Mac a-t-il du réseau\xa0?'),
        (_answer(503, text='<html>Service Unavailable</html>'), 503, 'Gemini a refusé la demande (erreur 503).'),
        (
            _answer(400, {'error': {'code': 400, 'message': 'Invalid voice', 'status': 'INVALID_ARGUMENT'}}),
            400,
            'Gemini a refusé la demande (erreur 400).',
        ),
    ],
    ids=['timeout', 'network', 'server error page', 'refused'],
)
def test_other_failures_carry_the_status_a_caller_retries_on(
    sent: list[Any], answer: Any, status: int, message: str
) -> None:
    """5xx and the network: retried; another 4xx: the line is refused, not asked again (scene_voice)."""
    sent.append(answer)
    with pytest.raises(GeminiError) as error:
        gemini.post('key', 'interactions', {}, 25)
    assert (str(error.value), error.value.status, error.value.per_day) == (message, status, False)
