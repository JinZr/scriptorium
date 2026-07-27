from pathlib import Path
import subprocess


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _manuscript_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "paper"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "sections").mkdir()
    (repo / "figures").mkdir()
    (repo / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\input{sections/results}\n"
        "\\bibliography{refs}\n"
        "\\includegraphics{figures/result}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (repo / "sections" / "results.tex").write_text("The value is 1.\n", encoding="utf-8")
    (repo / "refs.bib").write_text("@article{x, title={X}}\n", encoding="utf-8")
    (repo / "figures" / "result.png").write_bytes(b"not-a-real-png")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "paper")
    return repo
