#!/usr/bin/env bash
# Check a workstation for the external tools Scriptorium needs before and
# after `pip install`. This script inspects the host environment only. It does
# not read a manuscript repository; run `scriptorium doctor` inside the
# committed manuscript repository for the project-level check.
#
# Usage: bash utils/preflight.sh [--no-compile]
#   PYTHON=/path/to/python  interpreter to check (default: python3, then python)
#
# Exit status: 0 when every required check passes, 1 otherwise.
set -u

MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=10
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
RUN_COMPILE=1

for argument in "$@"; do
    case "$argument" in
        --no-compile) RUN_COMPILE=0 ;;
        -h|--help)
            sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown option: $argument" >&2
            exit 2
            ;;
    esac
done

required_failures=0

report() {
    # report STATUS NAME MESSAGE
    printf '%-5s %-18s %s\n' "$1" "$2" "$3"
}
pass() { report "ok" "$1" "$2"; }
info() { report "info" "$1" "$2"; }
warn() { report "warn" "$1" "$2"; }
fail() {
    report "FAIL" "$1" "$2"
    required_failures=$((required_failures + 1))
}

require_tool() {
    # require_tool NAME HINT [VERSION_COMMAND...]
    local name="$1" hint="$2" path detail
    shift 2
    path="$(command -v "$name" 2>/dev/null || true)"
    if [ -z "$path" ]; then
        fail "$name" "not found in PATH${hint:+; $hint}"
        return 1
    fi
    detail=""
    if [ "$#" -gt 0 ]; then
        detail="$("$@" 2>/dev/null | head -n 1 || true)"
    fi
    pass "$name" "${detail:-$path}"
}

echo "Scriptorium preflight"
echo "repository: $REPO_ROOT"
echo

# --- Python -----------------------------------------------------------------
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON=python3
    elif command -v python >/dev/null 2>&1; then
        PYTHON=python
    fi
fi

python_ok=0
if [ -z "$PYTHON" ] || ! command -v "$PYTHON" >/dev/null 2>&1; then
    fail "python" "no python3 or python in PATH; set PYTHON=/path/to/python"
else
    python_path="$(command -v "$PYTHON")"
    python_version="$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || true)"
    if [ -z "$python_version" ]; then
        fail "python" "$python_path does not run"
    elif "$PYTHON" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_PYTHON_MAJOR, $MIN_PYTHON_MINOR) else 1)"; then
        pass "python" "$python_version at $python_path"
        python_ok=1
    else
        fail "python" "$python_version at $python_path; Scriptorium needs $MIN_PYTHON_MAJOR.$MIN_PYTHON_MINOR or newer"
    fi
fi

if [ "$python_ok" = 1 ]; then
    if "$PYTHON" -m pip --version >/dev/null 2>&1; then
        pass "pip" "$("$PYTHON" -m pip --version 2>/dev/null | cut -d' ' -f1-2)"
    else
        fail "pip" "python -m pip is unavailable for $PYTHON"
    fi
fi

# --- Git and TeX tools ------------------------------------------------------
require_tool git "" git --version
require_tool latexmk "install a TeX distribution such as TeX Live or MacTeX" latexmk --version
require_tool kpsewhich "part of TeX Live; needed to identify TeX installation inputs"

first_engine=""
for engine in pdflatex xelatex lualatex; do
    engine_path="$(command -v "$engine" 2>/dev/null || true)"
    if [ -n "$engine_path" ]; then
        info "engine" "$engine available at $engine_path"
        [ -z "$first_engine" ] && first_engine="$engine"
    else
        info "engine" "$engine not found (optional if another engine is present)"
    fi
done
if [ -n "$first_engine" ]; then
    pass "latex_engine" "at least one supported engine present"
else
    fail "latex_engine" "none of pdflatex, xelatex, lualatex found"
fi

# --- Compile smoke test -----------------------------------------------------
if [ "$RUN_COMPILE" = 0 ]; then
    info "compile" "skipped (--no-compile)"
elif [ -z "$first_engine" ] || ! command -v latexmk >/dev/null 2>&1; then
    info "compile" "skipped because latexmk or a LaTeX engine is missing"
else
    workdir="$(mktemp -d 2>/dev/null || mktemp -d -t scriptorium-preflight)"
    cat > "$workdir/smoke.tex" <<'TEX'
\documentclass{article}
\usepackage{amsmath}
\usepackage{graphicx}
\usepackage{hyperref}
\begin{document}
\section{Preflight}
Scriptorium preflight smoke test: $E = mc^2$.
\end{document}
TEX
    case "$first_engine" in
        pdflatex) engine_flag="-pdf" ;;
        xelatex) engine_flag="-xelatex" ;;
        lualatex) engine_flag="-lualatex" ;;
    esac
    if (cd "$workdir" && latexmk "$engine_flag" -interaction=nonstopmode -halt-on-error smoke.tex \
            >"$workdir/latexmk.log" 2>&1) && [ -s "$workdir/smoke.pdf" ]; then
        pass "compile" "latexmk $engine_flag compiled a test document with amsmath, graphicx, hyperref"
    else
        fail "compile" "latexmk $engine_flag failed; last log lines follow"
        tail -n 15 "$workdir/latexmk.log" 2>/dev/null | sed 's/^/      /'
    fi
    rm -rf "$workdir"
fi

# --- Scriptorium package ----------------------------------------------------
scriptorium_path="$(command -v scriptorium 2>/dev/null || true)"
installed_in_python=0
if [ "$python_ok" = 1 ] && "$PYTHON" -c 'import scriptorium' >/dev/null 2>&1; then
    installed_in_python=1
fi

if [ "$installed_in_python" = 1 ]; then
    if "$PYTHON" -m scriptorium --help >/dev/null 2>&1; then
        pass "scriptorium" "installed in $PYTHON"
    else
        fail "scriptorium" "importable from $PYTHON but '$PYTHON -m scriptorium --help' fails"
    fi
    if [ -n "$scriptorium_path" ]; then
        info "scriptorium_cli" "$scriptorium_path"
    else
        warn "scriptorium_cli" "'scriptorium' is not in PATH; add $(dirname "$(command -v "$PYTHON")") to PATH or activate that environment in the model client"
    fi
    if "$PYTHON" -c 'import pymupdf' >/dev/null 2>&1; then
        pass "pdf_rendering" "pymupdf imports in $PYTHON"
    else
        fail "pdf_rendering" "pymupdf is missing; reinstall: $PYTHON -m pip install --force-reinstall PyMuPDF"
    fi
elif [ -n "$scriptorium_path" ]; then
    warn "scriptorium" "$scriptorium_path exists but $PYTHON cannot import scriptorium; rerun with PYTHON set to the interpreter that installed it"
    info "pdf_rendering" "not checked; rerun with the matching PYTHON"
else
    info "scriptorium" "not installed; run: $PYTHON -m pip install '$REPO_ROOT'"
    info "pdf_rendering" "checked after installation (PyMuPDF is installed with the package)"
fi

# --- Host model clients -----------------------------------------------------
host_found=0
for host in codex claude; do
    host_path="$(command -v "$host" 2>/dev/null || true)"
    if [ -n "$host_path" ]; then
        info "host_cli" "$host at $host_path"
        host_found=1
    fi
done
if [ "$host_found" = 0 ]; then
    warn "host_cli" "no codex or claude CLI in PATH; Scriptorium needs one of Codex, Claude Code, or Antigravity to run reviews"
fi

skill_file="$REPO_ROOT/skills/scriptorium/SKILL.md"
if [ -f "$skill_file" ]; then
    info "skill" "load $skill_file in the model client"
fi

# --- Summary ----------------------------------------------------------------
echo
if [ "$required_failures" = 0 ]; then
    echo "All required checks passed. Next: pip install, then run 'scriptorium doctor' inside the manuscript repository."
    exit 0
fi
echo "$required_failures required check(s) failed. Fix them before installing or running Scriptorium."
exit 1
