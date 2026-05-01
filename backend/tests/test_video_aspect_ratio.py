"""Regression test for video aspect-ratio parsing.

The user reported that "make me a 9:16 video..." was still producing a 16:9
video because the backend hard-coded "16:9" on the auto-detected video
pipeline. We introduced `parse_video_aspect_ratio` in server.py and wired it
into the two `_kick_off_video` call sites (send_message + smart_start). This
unit test locks in the parser behaviour.
"""

import pytest

from server import parse_video_aspect_ratio


@pytest.mark.parametrize(
    "text,expected",
    [
        # Explicit ratio literals
        ("make me a 9:16 video of a dog", "9:16"),
        ("render it in 9/16", "9:16"),
        ("16:9 cinematic drone shot", "16:9"),
        ("9x16 portrait clip", "9:16"),
        # 1:1 folds to 9:16 (Kie.ai does not support square today)
        ("1:1 square clip for IG grid", "9:16"),
        # Natural-language vertical signals
        ("tiktok video of cats playing", "9:16"),
        ("vertical reel for the launch", "9:16"),
        ("portrait mode video for stories", "9:16"),
        ("youtube short about our product", "9:16"),
        # Natural-language horizontal signals
        ("youtube video explaining the feature", "16:9"),
        ("landscape commercial with wide shots", "16:9"),
        ("widescreen cinematic bumper", "16:9"),
        # Unspecified → safe default
        ("just make a 5-second video", "16:9"),
        ("", "16:9"),
        (None, "16:9"),
    ],
)
def test_parse_video_aspect_ratio(text, expected):
    assert parse_video_aspect_ratio(text) == expected


def test_vertical_wins_when_ambiguous_portrait_youtube():
    # Portrait signal outweighs the bare word "youtube" only because we
    # explicitly match "youtube short". Generic "youtube" stays horizontal.
    assert parse_video_aspect_ratio("youtube shorts clip") == "9:16"
    assert parse_video_aspect_ratio("youtube video") == "16:9"
