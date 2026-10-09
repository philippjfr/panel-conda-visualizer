from pathlib import Path

from conda_visualizer import theme

SRC = Path(theme.__file__).parent

RETIRED = ["#383838", "#3DAE2B", "#FFBA06", "#068C35", "#C9CDD5", "#7C8089", "#E6FAED", "#E6E8EC"]


def test_no_retired_brand_colors():
    for name in ("theme.py", "graph.js", "graph.css"):
        text = (SRC / name).read_text().upper()
        assert [hex_ for hex_ in RETIRED if hex_.upper() in text] == [], name


def test_contrast_text_follows_brand_rules():
    for mode in theme.THEME_CONFIG.values():
        palette = mode["palette"]
        # White on Green 500 fails contrast.
        assert palette["secondary"]["main"] == theme.GREEN[500]
        assert palette["secondary"]["contrastText"] == theme.NEUTRAL[950]


def test_assets_exist():
    assert theme.LOGO.is_file()
    assert theme.FAVICON.is_file()


def test_graph_palettes_have_no_yellow():
    text = (SRC / "graph.js").read_text().upper()
    assert "#FFDD00" not in text
