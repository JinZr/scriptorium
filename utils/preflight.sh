#!/usr/bin/env bash
# Check a workstation for the external tools Scriptorium needs before and
# after `pip install`. This script inspects the host environment only. It does
# not read a manuscript repository; run `scriptorium doctor` inside the
# committed manuscript repository for the project-level check.
#
# Usage: bash /path/to/scriptorium/utils/preflight.sh [--no-compile]
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
    # require_tool NAME HINT [PROBE_COMMAND...]
    # A probe command must exit 0; its first output line becomes the detail.
    local name="$1" hint="$2" path detail
    shift 2
    path="$(command -v "$name" 2>/dev/null || true)"
    if [ -z "$path" ]; then
        fail "$name" "not found in PATH${hint:+; $hint}"
        return 1
    fi
    if [ "$#" -eq 0 ]; then
        pass "$name" "$path"
        return 0
    fi
    if detail="$("$@" 2>&1)"; then
        pass "$name" "${detail%%$'\n'*}"
        return 0
    fi
    fail "$name" "$path is present but '$*' failed: ${detail%%$'\n'*}"
    return 1
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
latexmk_ok=0
require_tool latexmk "install a TeX distribution such as TeX Live or MacTeX" latexmk --version && latexmk_ok=1

# Scriptorium resolves TeX installation roots with these two expansions before
# every build; probe them the same way so a broken configuration fails here.
if require_tool kpsewhich "part of TeX Live; needed to identify TeX installation inputs"; then
    for expression in '{$TEXMFDIST,$TEXMFMAIN}' '{$TEXMF,$TEXMFCNF,$TEXMFCACHE}'; do
        if ! roots="$(kpsewhich "--expand-path=$expression" 2>/dev/null)"; then
            fail "texmf_roots" "kpsewhich --expand-path='$expression' exited with an error"
            continue
        fi
        # Same acceptance as the build: every entry absolute, none the filesystem root.
        roots="${roots//!!/}"
        bad_entry=""
        entry_count=0
        IFS=: read -r -a root_entries <<<"$roots"
        for entry in "${root_entries[@]+"${root_entries[@]}"}"; do
            [ -z "$entry" ] && continue
            entry_count=$((entry_count + 1))
            trimmed="$(printf '%s' "$entry" | sed 's#/*$##')"
            if [ "${entry#/}" = "$entry" ] || [ -z "$trimmed" ]; then
                bad_entry="$entry"
            fi
        done
        if [ "$entry_count" -eq 0 ]; then
            fail "texmf_roots" "kpsewhich --expand-path='$expression' returned no TeX roots"
        elif [ -n "$bad_entry" ]; then
            fail "texmf_roots" "kpsewhich --expand-path='$expression' contains an invalid root '$bad_entry'"
        else
            pass "texmf_roots" "$expression -> ${roots%%:*}"
        fi
    done
fi

available_engines=""
for engine in pdflatex xelatex lualatex; do
    engine_path="$(command -v "$engine" 2>/dev/null || true)"
    if [ -n "$engine_path" ]; then
        info "engine" "$engine available at $engine_path"
        available_engines="$available_engines $engine"
    else
        info "engine" "$engine not found (optional if another engine is present)"
    fi
done
if [ -n "$available_engines" ]; then
    pass "latex_engine" "available:$available_engines"
else
    fail "latex_engine" "none of pdflatex, xelatex, lualatex found"
fi

# --- Compile smoke test -----------------------------------------------------
# The document uses only the base article class so a minimal TeX installation
# is not failed for missing packages; package coverage is reported separately.
# Uses the same latexmk flags as Scriptorium's build: -norc ignores user rc
# files and -recorder must produce the .fls dependency evidence it reads.
smoke_compile() {
    # smoke_compile ENGINE WORKDIR -> 0 on success; log at WORKDIR/ENGINE.log
    local engine="$1" workdir="$2" flag
    case "$engine" in
        pdflatex) flag="-pdf" ;;
        xelatex) flag="-xelatex" ;;
        lualatex) flag="-lualatex" ;;
    esac
    rm -f "$workdir"/smoke.pdf "$workdir"/smoke.fls "$workdir"/smoke.fdb_latexmk "$workdir"/smoke.xdv
    (cd "$workdir" && latexmk -norc "$flag" -g -recorder -interaction=nonstopmode -halt-on-error smoke.tex \
        >"$workdir/$engine.log" 2>&1) || return 1
    [ -s "$workdir/smoke.pdf" ] && [ -s "$workdir/smoke.fls" ] && [ -s "$workdir/smoke.fdb_latexmk" ]
}

if [ "$RUN_COMPILE" = 0 ]; then
    info "compile" "skipped (--no-compile)"
elif [ -z "$available_engines" ] || [ "$latexmk_ok" = 0 ]; then
    info "compile" "skipped because latexmk or a LaTeX engine is missing"
else
    workdir="$(mktemp -d 2>/dev/null || mktemp -d -t scriptorium-preflight 2>/dev/null || true)"
    if [ -z "$workdir" ] || [ ! -d "$workdir" ]; then
        fail "compile" "cannot create a temporary directory for the smoke test (check TMPDIR and free space)"
        workdir=""
    fi
fi
if [ -n "${workdir:-}" ]; then
    cat > "$workdir/smoke.tex" <<'TEX'
\documentclass{article}
\begin{document}
\section{Preflight}
Scriptorium preflight smoke test: $E = mc^2$.
\end{document}
TEX
    compiled_with=""
    failed_engines=""
    for engine in $available_engines; do
        if smoke_compile "$engine" "$workdir"; then
            compiled_with="$compiled_with $engine"
        else
            failed_engines="$failed_engines $engine"
        fi
    done
    if [ -n "$compiled_with" ]; then
        pass "compile" "latexmk -norc -recorder compiled a base article document with:$compiled_with"
        [ -n "$failed_engines" ] && warn "compile" "failed with:$failed_engines (choose a working engine in scriptorium.toml)"
    else
        fail "compile" "latexmk -norc -recorder failed with every engine:$failed_engines; last log lines follow"
        for engine in $failed_engines; do
            echo "      [$engine]"
            tail -n 10 "$workdir/$engine.log" 2>/dev/null | sed 's/^/      /'
        done
    fi
    rm -rf "$workdir"
fi

# Common manuscript packages: informational, because the manuscript decides
# what it needs and `scriptorium doctor` compiles the real sources.
if command -v kpsewhich >/dev/null 2>&1; then
    missing_packages=""
    for package in amsmath graphicx hyperref natbib biblatex booktabs; do
        kpsewhich "$package.sty" >/dev/null 2>&1 || missing_packages="$missing_packages $package"
    done
    if [ -z "$missing_packages" ]; then
        info "packages" "amsmath, graphicx, hyperref, natbib, biblatex, booktabs are installed"
    else
        warn "packages" "not installed:$missing_packages (needed only if the manuscript uses them)"
    fi
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
        # The guide and the shared skill invoke the bare command, so the PATH
        # entry point must run and must belong to the checked interpreter's
        # installation (its base or user scripts directory).
        entry_owner="$("$PYTHON" - "$scriptorium_path" <<'PY' 2>/dev/null
import os, sys, sysconfig
target_dir = os.path.dirname(os.path.realpath(sys.argv[1]))
dirs = [sysconfig.get_path("scripts")]
try:
    dirs.append(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))
except (AttributeError, KeyError):
    pass
print("selected" if any(d and os.path.realpath(d) == target_dir for d in dirs) else "other")
PY
)"
        if [ "$entry_owner" != "selected" ]; then
            fail "scriptorium_cli" "$scriptorium_path is not the entry point installed by $PYTHON; put that interpreter's scripts directory first in PATH or remove the other copy"
        elif "$scriptorium_path" --help >/dev/null 2>&1; then
            pass "scriptorium_cli" "$scriptorium_path"
        else
            fail "scriptorium_cli" "$scriptorium_path is in PATH but cannot run; reinstall with $PYTHON -m pip install --force-reinstall"
        fi
    else
        # Report the scripts directory of the installation scheme that holds the
        # entry point: the interpreter's own scheme, or the user scheme for
        # `pip install --user`.
        scripts_dir="$("$PYTHON" - <<'PY' 2>/dev/null
import os, sysconfig
candidates = [sysconfig.get_path("scripts")]
try:
    candidates.append(sysconfig.get_path("scripts", sysconfig.get_preferred_scheme("user")))
except (AttributeError, KeyError):
    pass
for path in candidates:
    if path and os.path.exists(os.path.join(path, "scriptorium")):
        print(path)
        break
else:
    print(candidates[0] or "")
PY
)"
        warn "scriptorium_cli" "'scriptorium' is not in PATH; add ${scripts_dir:-its scripts directory} to PATH or activate that environment in the model client"
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
for host in codex claude agy; do
    host_path="$(command -v "$host" 2>/dev/null || true)"
    if [ -n "$host_path" ]; then
        info "host_cli" "$host at $host_path"
        host_found=1
    fi
done
if [ "$host_found" = 0 ]; then
    warn "host_cli" "no codex, claude, or agy CLI in PATH; Scriptorium needs one of Codex, Claude Code, or Antigravity (agy) to run reviews"
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
