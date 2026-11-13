"""Tests for src/creativity.py — all synthetic, no network."""

import pytest

from src.creativity import (
    HASHTAGS,
    REPO_URL,
    NameAgent,
    PostAgent,
    StoryAgent,
    VisualAgent,
    creative_check,
    format_money,
    main,
)


# --- NameAgent ---

def test_names_deterministic_same_inputs():
    agent = NameAgent()
    a = agent.nicknames(name="Gato", ticker="GATO", idea="a cat", seed=7)
    b = agent.nicknames(name="Gato", ticker="GATO", idea="a cat", seed=7)
    assert a == b
    assert len(a) == 3


def test_names_distinct_from_each_other_and_input():
    names = NameAgent().nicknames(name="Gato", ticker="GATO", idea="cat coin", seed=1)
    assert len(set(n.lower() for n in names)) == 3
    assert all("gato" not in n.lower() for n in names)


def test_names_different_seed_gives_different_names():
    a = NameAgent().nicknames(name="Gato", seed=1)
    b = NameAgent().nicknames(name="Gato", seed=99)
    assert a != b


def test_names_never_contain_profanity():
    names = NameAgent().nicknames(name="shit coin", ticker="SHIT", idea="damn cat", seed=3)
    joined = " ".join(names).lower()
    for bad in ("shit", "damn", "fuck"):
        assert bad not in joined


def test_names_invalid_count_raises():
    with pytest.raises(ValueError):
        NameAgent().nicknames(name="X", count=0)


# --- StoryAgent ---

def test_story_flagged_names_the_flag():
    story = StoryAgent().story(
        {"verdict": "flagged", "symbol": "XYZ", "market_cap_usd": 1_000_000,
         "change_24h_pct": -80.0, "liquidity_usd": 500, "rugged_mint_count": 3}
    )
    assert "flagged" in story
    assert "serial rugger" in story
    assert "XYZ" in story


def test_story_not_flagged_never_claims_rug():
    for status in ("not_flagged", "unknown", "under_review"):
        story = StoryAgent().story(
            {"verdict": status, "symbol": "XYZ", "market_cap_usd": 2_000_000,
             "change_24h_pct": -91.5, "liquidity_usd": 2_752}
        )
        assert "not proven a rug" in story
        # "not proven a serial rugger" is a negation, not a claim.
        assert "This dev is flagged" not in story
        assert "flags this dev" not in story


def test_story_uses_only_given_numbers():
    story = StoryAgent().story(
        {"verdict": "unknown", "symbol": "NIHAO", "market_cap_usd": 2_903,
         "change_24h_pct": -91.5, "liquidity_usd": 2_752}
    )
    assert "91.5%" in story
    assert "$2.9K" in story


def test_story_missing_data_says_so():
    story = StoryAgent().story({"verdict": "unknown", "symbol": "ABC"})
    assert "n/a" in story


# --- VisualAgent ---

def test_badge_colours():
    agent = VisualAgent()
    assert agent.badge("collapsed")["colour"] == "red"
    assert agent.badge("thin_liquidity")["colour"] == "amber"
    assert agent.badge("active")["colour"] == "green"
    assert agent.badge("flagged")["colour"] == "red"
    assert agent.badge("under_review")["colour"] == "amber"
    assert agent.badge("unknown")["colour"] == "grey"


def test_badge_summary_has_symbol_and_change():
    badge = VisualAgent().badge("collapsed", symbol="XYZ", change=-80.0)
    assert "XYZ" in badge["summary"]
    assert "-80.0%" in badge["summary"]
    assert badge["emoji"]


def test_sparkline_rises_and_falls():
    spark = VisualAgent().sparkline([-5.0, 0.0, 5.0, 10.0])
    assert spark == "▁▃▆█"
    assert len(spark) == 4


def test_sparkline_empty_and_flat():
    assert VisualAgent().sparkline([]) == "(no data)"
    assert VisualAgent().sparkline([3.0, 3.0, 3.0]) == "▄▄▄"


# --- PostAgent ---

def test_post_flagged_length_hashtags_and_link():
    post = PostAgent().draft("flagged", symbol="XYZ")
    assert len(post) <= 280
    assert REPO_URL in post
    for tag in HASHTAGS.split():
        assert tag in post
    assert "flagged" in post


def test_post_watch_only_says_not_proven_rug():
    post = PostAgent().draft("collapsed", symbol="NIHAO", change_24h_pct=-91.5)
    assert "not proven a rug" in post
    assert len(post) <= 280


def test_post_active_uses_given_numbers_only():
    post = PostAgent().draft("active", symbol="GATO", market_cap_usd=5_009_361,
                             change_24h_pct=-16.7)
    assert "$5.0M" in post
    assert "-16.7%" in post
    assert len(post) <= 280


def test_post_never_invents_price_or_mint():
    post = PostAgent().draft("unknown", symbol="ABC")
    assert "$" not in post.split(REPO_URL)[0].replace("$ABC", "")
    assert "pump" not in post.lower().replace("#pumpfun", "")


def test_format_money():
    assert format_money(5_009_361) == "$5.0M"
    assert format_money(2_903) == "$2.9K"
    assert format_money(None) == "n/a"


# --- creative_check pipeline ---

def test_creative_check_end_to_end():
    result = creative_check(
        {"verdict": "flagged", "symbol": "XYZ", "market_cap_usd": 1_000_000,
         "change_24h_pct": -80.0, "liquidity_usd": 500, "rugged_mint_count": 2,
         "evidence_links": ["https://example.com/a"]},
        name="Xyz Coin", ticker="XYZ", seed=2, changes=[-10.0, -50.0, -80.0],
    )
    assert len(result["nicknames"]) == 3
    assert "flagged" in result["story"]
    assert result["badge"]["colour"] == "red"
    assert result["sparkline"] == "█▄▁"
    assert len(result["post"]) <= 280


# --- CLI ---

def test_cli_all_modes_success(capsys):
    assert main(["--verdict", "flagged", "--symbol", "XYZ", "--mc", "1000000",
                 "--change", "-80"]) == 0
    out = capsys.readouterr().out
    assert "Nicknames" in out and "Story" in out and "Badge" in out and "Post" in out


def test_cli_post_only(capsys):
    assert main(["--verdict", "active", "--symbol", "GATO", "--mc", "5009361",
                 "--change", "-16.7", "--post"]) == 0
    out = capsys.readouterr().out
    assert "#memes" in out and "#rug" in out and "#pumpfun" in out
    assert len(out.strip()) <= 280


def test_cli_story_and_badge_modes(capsys):
    assert main(["--verdict", "unknown", "--symbol", "XYZ", "--story"]) == 0
    assert "not proven a rug" in capsys.readouterr().out
    assert main(["--verdict", "flagged", "--symbol", "XYZ", "--badge",
                 "--changes", "-5,0,5,10"]) == 0
    out = capsys.readouterr().out
    assert "▁▃▆█" in out


def test_cli_invalid_verdict_symbol_and_mc():
    assert main(["--verdict", "rugged"]) == 2
    assert main(["--verdict", "flagged", "--symbol", "TOOLONGSYMBOL"]) == 2
    assert main(["--verdict", "flagged", "--symbol", "XYZ", "--mc", "-5"]) == 2
    assert main(["--verdict", "flagged", "--changes", "a,b"]) == 2
