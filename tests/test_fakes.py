import pytest

from tests.fakes import DeterministicFakeProvider


@pytest.mark.asyncio
async def test_successful_fake_provider_is_deterministic():
    provider = DeterministicFakeProvider.successful()

    first, first_usage = await provider.generate("hello")
    second, second_usage = await provider.generate("hello")

    assert first == second == "deterministic successful answer"
    assert first_usage == second_usage == {"prompt_tokens": 3, "completion_tokens": 2}
    assert provider.prompts == ["hello", "hello"]


@pytest.mark.asyncio
async def test_fake_provider_can_return_valid_judge_responses():
    judge, judge_usage = await DeterministicFakeProvider.valid_judge().generate("grade this")
    pairwise, pairwise_usage = await DeterministicFakeProvider.valid_pairwise_judge().generate("compare these")

    assert judge == '{"score": 8, "reason": "deterministic judge response"}'
    assert judge_usage == {"prompt_tokens": 11, "completion_tokens": 7}
    assert pairwise == ('{"winner": "A", "score_a": 9, "score_b": 4, '
                        '"reason": "deterministic pairwise judge response"}')
    assert pairwise_usage == {"prompt_tokens": 13, "completion_tokens": 9}

    tie, tie_usage = await DeterministicFakeProvider.valid_pairwise_tie().generate("compare these")
    assert tie == ('{"winner": "tie", "score_a": 7, "score_b": 7, '
                   '"reason": "deterministic pairwise tie"}')
    assert tie_usage == {"prompt_tokens": 13, "completion_tokens": 9}


@pytest.mark.asyncio
async def test_fake_provider_can_return_malformed_json_or_raise():
    malformed, usage = await DeterministicFakeProvider.malformed_json().generate("grade this")

    assert malformed == "this is deliberately not JSON"
    assert usage == {"prompt_tokens": 5, "completion_tokens": 3}
    with pytest.raises(RuntimeError, match="deterministic fake provider failure"):
        await DeterministicFakeProvider.failing().generate("fail")
