"""Tests for User-Agent rotation."""

from mapear_rss.extraction.user_agents import USER_AGENTS, UserAgentRotator


class TestUserAgentRotator:
    def test_disabled_returns_default(self) -> None:
        rot = UserAgentRotator(enabled=False)
        ua1 = rot.next()
        ua2 = rot.next()
        assert ua1 == ua2

    def test_rotation_cycles(self) -> None:
        rot = UserAgentRotator(enabled=True)
        seen = {rot.next() for _ in range(len(USER_AGENTS) * 2)}
        assert seen == set(USER_AGENTS)

    def test_for_domain_stable(self) -> None:
        rot = UserAgentRotator(enabled=True)
        a1 = rot.for_domain("example.com")
        a2 = rot.for_domain("example.com")
        assert a1 == a2

    def test_seed_produces_deterministic_shuffle(self) -> None:
        rot_a = UserAgentRotator(enabled=True, seed=42)
        rot_b = UserAgentRotator(enabled=True, seed=42)
        assert [rot_a.next() for _ in range(5)] == [rot_b.next() for _ in range(5)]
