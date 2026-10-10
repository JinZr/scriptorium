import pytest

from scriptorium.config import ManuscriptConfig
from scriptorium.manuscript import ManuscriptManager


def _detect(tmp_path, main_text, supplement_text=None):
    (tmp_path / "main.tex").write_text(main_text, encoding="utf-8")
    supplements = ()
    if supplement_text is not None:
        (tmp_path / "supplement.tex").write_text(supplement_text, encoding="utf-8")
        supplements = ("supplement.tex",)
    config = ManuscriptConfig("main.tex", "pdflatex", supplements)
    return ManuscriptManager(tmp_path).detect_template(tmp_path, config)


@pytest.mark.parametrize(
    ("preamble", "template", "family"),
    [
        ("\\documentclass{article}\n\\usepackage[preprint]{neurips_2026}\n", "neurips_2026", "ml_conference"),
        ("\\documentclass{article}\n\\usepackage{iclr2026_conference,times}\n", "iclr2026_conference", "ml_conference"),
        ("\\documentclass[pdflatex,sn-mathphys-num]{sn-jnl}\n", "sn-jnl", "nature_family"),
        ("\\documentclass[conference]{IEEEtran}\n", "IEEEtran", "other"),
    ],
)
def test_known_templates_name_their_venue_family(tmp_path, preamble, template, family) -> None:
    detected = _detect(tmp_path, preamble + "\\begin{document}\nText.\n\\end{document}\n")

    assert detected == {"template": template, "venue_family": family}


def test_unknown_and_commented_templates_are_not_detected(tmp_path) -> None:
    detected = _detect(
        tmp_path,
        "\\documentclass{article}\n% \\usepackage{icml2026}\n\\usepackage{amsmath}\n"
        "\\begin{document}\n\\end{document}\n",
    )

    assert detected == {"template": None, "venue_family": "unknown"}


def test_a_supplement_declaration_is_used_when_the_main_document_has_none(tmp_path) -> None:
    detected = _detect(
        tmp_path,
        "\\documentclass{article}\n\\begin{document}\n\\end{document}\n",
        "\\documentclass{article}\n\\usepackage[accepted]{icml2026}\n\\begin{document}\n\\end{document}\n",
    )

    assert detected == {"template": "icml2026", "venue_family": "ml_conference"}
