"""widget.js has no JS test runner in this repo, so these tests work two
ways: (1) a pure-Python reimplementation of the exact WCAG contrast
formula widget.js uses, as a spec/correctness oracle for the real-world
colors this bug was about (manually cross-checked against the shipped
JS via node — see the commit this file was added in), and (2) checking
the served widget.js source actually wires the fix in (the CSS variable,
the function, every place that used to hardcode white text/icons against
the brand colour).
"""

import math
from pathlib import Path

WIDGET_JS_PATH = Path(__file__).resolve().parent.parent / "app" / "static" / "widget.js"


def _relative_luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast_with_white(hex_color: str) -> float:
    return 1.05 / (_relative_luminance(hex_color) + 0.05)


# ---------- WCAG contrast math (spec/correctness oracle) ----------


def test_riversidewoodcraft_pale_cream_fails_wcag_aa_against_white():
    """The exact colour from the bug report — confirmed unreadable live."""
    assert _contrast_with_white("#faf5f0") < 4.5


def test_default_navy_brand_passes_against_white():
    assert _contrast_with_white("#1a1a2e") >= 4.5


def test_wcag_boundary_colors():
    """767676/777777 are the textbook WCAG AA boundary greys — the ratio
    must straddle 4.5 between them, confirming the formula's precision,
    not just its rough direction."""
    assert _contrast_with_white("#767676") >= 4.5
    assert _contrast_with_white("#777777") < 4.5


def test_black_and_white_are_the_contrast_extremes():
    assert _contrast_with_white("#000000") == 21.0
    assert math.isclose(_contrast_with_white("#ffffff"), 1.0, rel_tol=1e-9)


# ---------- widget.js wiring ----------


def _widget_js_source() -> str:
    return WIDGET_JS_PATH.read_text(encoding="utf-8")


def test_widget_js_computes_contrast_and_picks_brand_text_color():
    source = _widget_js_source()
    assert "relativeLuminance" in source
    assert "contrastWithWhite" in source
    assert "pickBrandTextColor" in source
    assert "4.5" in source


def test_widget_js_defines_brand_text_custom_property():
    source = _widget_js_source()
    assert "--brand-text" in source
    assert "pickBrandTextColor(brandColor)" in source


def test_widget_js_header_message_bubble_and_send_button_use_brand_text():
    """The three symptoms named in the bug report (header text, user
    message bubbles, send button) must all use the contrast-aware colour,
    not a hardcoded white, now."""
    source = _widget_js_source()
    assert '".header{background:var(--brand);color:var(--brand-text)' in source
    assert '".msg.user{background:var(--brand);color:var(--brand-text)' in source
    assert '".input-row button{background:var(--brand);color:var(--brand-text)' in source


def test_widget_js_launcher_and_human_send_also_use_brand_text():
    source = _widget_js_source()
    assert '".launcher{' in source and "color:var(--brand-text)" in source
    assert '".human-form .human-send{background:var(--brand);color:var(--brand-text)' in source


def test_widget_js_no_longer_hardcodes_white_text_on_brand_background():
    """None of the brand-coloured-background rules should still say
    color:#fff literally — they should all defer to --brand-text now."""
    source = _widget_js_source()
    for rule in (
        '".launcher{',
        '".header{',
        '".msg.user{',
        '".input-row button{',
        '".human-form .human-send{',
    ):
        start = source.index(rule)
        end = source.index("}", start)
        assert "color:#fff" not in source[start:end], f"{rule} still hardcodes white text"
