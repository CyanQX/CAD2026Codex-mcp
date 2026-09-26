"""Fail-closed validators for the raw AutoCAD command / AutoLISP fallback.

The v0.1 guard was a pair of regular expressions.  Testing showed that almost every
equivalent spelling slipped through (``_.NETLOAD``, ``( startapp ...)``, ``(command
"_.NETLOAD" ...)``, ``vlax-create-object "WScript.Shell"`` ...) while innocent drawing
text such as ``EXIT`` was blocked.  This module replaces it with structural checks:

* **commands** - the *first* command must be on an explicit allow-list, and *every* token
  (after stripping AutoCAD's ``_ . ' -`` prefixes) is scanned against a list of commands
  that load code, start programs, write files or quit AutoCAD.
* **AutoLISP** - the expression is tokenised (strings and comments are respected), the
  *function position* of every form must be on an allow-list, and a deny-list of
  dangerous symbols is rejected wherever it appears (quoted, passed to ``mapcar`` ...).
  Indirect-call primitives (``eval``, ``read``, ``apply``, ``vl-catch-all-apply``) are
  denied, so a call cannot be assembled from strings.

This is still a *seat belt, not a sandbox*: the raw channel is disabled by default and
gated by ``confirm=true``.  Enabling it means letting the model run AutoCAD commands.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .errors import RawBlocked

MAX_COMMAND_CHARS = 2000
MAX_COMMAND_LINES = 40
MAX_LISP_CHARS = 4000
MAX_LISP_TOKENS = 3000
MAX_LISP_DEPTH = 60

# ----------------------------------------------------------------------- commands
RAW_COMMAND_ALLOW = frozenset(
    """
    LINE PLINE POLYLINE CIRCLE ARC ELLIPSE RECTANG RECTANGLE POLYGON DONUT SPLINE XLINE RAY POINT
    TEXT DTEXT MTEXT
    MOVE COPY ROTATE SCALE MIRROR OFFSET TRIM EXTEND FILLET CHAMFER BREAK JOIN EXPLODE STRETCH
    LENGTHEN ARRAY ARRAYRECT ARRAYPOLAR ERASE
    HATCH BHATCH
    DIMLINEAR DIMALIGNED DIMANGULAR DIMRADIUS DIMDIAMETER DIMCONTINUE DIMBASELINE DIMSTYLE
    LEADER MLEADER QLEADER TABLE
    LAYER LAYON LAYOFF LAYFRZ LAYTHW LAYLCK LAYULK LAYISO LAYUNISO LAYMCUR
    ZOOM PAN REGEN REGENALL REDRAW
    UNDO U REDO OOPS PURGE AUDIT OVERKILL STYLE
    LIST ID DIST AREA MEASURE DIVIDE SELECT QSELECT CHPROP CHANGE MATCHPROP
    BLOCK INSERT ATTEDIT UNITS LIMITS LTSCALE
    """.split()
)

# Commands that load code, start programs, write files/close documents or quit AutoCAD.
RAW_COMMAND_DENY = frozenset(
    """
    NETLOAD APPLOAD ARXLOAD ARXUNLOAD SHELL SH CMD START EXPLORER NOTEPAD REINIT
    SCRIPT RSCRIPT RESUME VBASTMT VBARUN VBALOAD VBAUNLOAD VBAIDE VBAMAN VLISP VLIDE
    CUILOAD CUIUNLOAD CUI MENULOAD MENUUNLOAD MENU LOAD BROWSER INSERTOBJ OLELINKS OLEOPEN
    SAVEAS SAVE QSAVE WBLOCK EXPORT EXPORTPDF DXFOUT PLOT PUBLISH ETRANSMIT ARCHIVE
    OPEN NEW QNEW CLOSE CLOSEALL QUIT EXIT SETVAR
    """.split()
)

_CMD_SEP = re.compile(r"[\s;]+")
_CMD_PREFIX = "'_.*+^"
_LEADING_RUN = re.compile(r"[A-Z][A-Z0-9_]*")


def _norm_cmd(token: str) -> str:
    return token.strip().lstrip(_CMD_PREFIX).lstrip("-").strip('"').upper()


def validate_raw_command(command: str, extra_allowed: Iterable[str] = ()) -> str:
    """Return the (normalised) first command verb, or raise :class:`RawBlocked`."""

    if not isinstance(command, str) or not command.strip():
        raise RawBlocked("command is empty")
    if "\x00" in command:
        raise RawBlocked("command contains a NUL character")
    if len(command) > MAX_COMMAND_CHARS:
        raise RawBlocked(f"command too long (>{MAX_COMMAND_CHARS} characters)")
    text = command.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    lines = re.split(r"[\r\n]+", text)
    if len(lines) > MAX_COMMAND_LINES:
        raise RawBlocked(f"too many command lines (>{MAX_COMMAND_LINES}); split into several calls or use the structured tools")
    allow = RAW_COMMAND_ALLOW | {str(x).upper() for x in extra_allowed}
    first: str | None = None
    for line in lines:
        for token in _CMD_SEP.split(line):
            if not token:
                continue
            norm = _norm_cmd(token)
            lead = _LEADING_RUN.match(norm)
            for candidate in {norm, lead.group(0) if lead else ""}:
                if candidate in RAW_COMMAND_DENY:
                    raise RawBlocked(
                        f"command {candidate} can load code / start programs / write files / close drawings or quit AutoCAD; blocked"
                        " (if this word appears in drawing text, use cad_draw draw_text instead)"
                    )
            if first is None:
                first = norm
                if norm not in allow:
                    raise RawBlocked(f"command {norm} is not on the raw allow-list")
    if first is None:
        raise RawBlocked("command is empty")
    return first


# ------------------------------------------------------------------------ AutoLISP
LISP_ALLOW = frozenset(
    """
    + - * / 1+ 1- = /= < <= > >= ~
    abs atan cos sin sqrt exp expt log fix float min max rem gcd boole logand logior lsh
    and or not null atom listp numberp stringp minusp zerop eq equal type boundp
    if cond progn while repeat foreach setq quote lambda
    list cons car cdr caar cadr cdar cddr caddr cadddr cdddr last member nth append reverse length assoc subst
    mapcar vl-remove vl-remove-if vl-remove-if-not vl-position vl-sort vl-sort-i vl-list-length vl-every vl-some
    acad_strlsort strcat strlen substr strcase itoa atoi atof rtos angtos distof angtof
    vl-string-search vl-string-subst vl-string-trim vl-string-left-trim vl-string-right-trim
    vl-string-position vl-string-mismatch vl-string->list vl-list->string wcmatch
    distance angle polar inters trans
    entget entnext entlast handent entmod entmake entmakex entdel entupd
    ssget ssadd ssdel sslength ssname ssmemb
    tblsearch tblnext tblobjname namedobjdict dictsearch dictnext
    getvar setvar princ prin1 print terpri prompt
    """.split()
)

LISP_DENY = frozenset(
    """
    startapp command command-s vl-cmdf eval read apply funcall vl-catch-all-apply
    load autoload arxload arxunload vl-load-all vl-load-com vl-vbaload vl-vbarun
    open close read-line read-char write-line write-char findfile getfiled
    setenv getenv set vl-mkdir vl-directory-files defun defun-q exit quit
    """.split()
)
_LISP_DENY_PREFIXES = ("vla-", "vlax-", "vlr-", "dos_", "vl-file", "vl-registry", "vl-vba", "vl-load")

# System variables the model may set from LISP; anything security relevant is excluded.
SAFE_SETVARS = frozenset(
    "CLAYER OSMODE ORTHOMODE PDMODE PDSIZE LTSCALE DIMSCALE TEXTSIZE CMDECHO AUNITS AUPREC LUNITS LUPREC "
    "CECOLOR CELTYPE CELWEIGHT GRIDMODE SNAPMODE FILLMODE".split()
)

_NUMBER = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def _lex(expr: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    i, n = 0, len(expr)
    while i < n:
        c = expr[i]
        if c.isspace():
            i += 1
        elif c == ";":
            if expr.startswith(";|", i):
                j = expr.find("|;", i + 2)
                if j < 0:
                    raise RawBlocked("LISP block comment is not closed")
                i = j + 2
            else:
                j = expr.find("\n", i)
                i = n if j < 0 else j + 1
        elif c in "()":
            tokens.append(("paren", c))
            i += 1
        elif c == "'":
            tokens.append(("quote", c))
            i += 1
        elif c == '"':
            j = i + 1
            while j < n and expr[j] != '"':
                j += 2 if expr[j] == "\\" else 1
            if j >= n:
                raise RawBlocked("LISP string is not closed")
            tokens.append(("str", expr[i + 1 : j]))
            i = j + 1
        else:
            j = i
            while j < n and not expr[j].isspace() and expr[j] not in "()\";'":
                j += 1
            tokens.append(("atom", expr[i:j]))
            i = j
        if len(tokens) > MAX_LISP_TOKENS:
            raise RawBlocked(f"LISP expression too long (>{MAX_LISP_TOKENS} tokens)")
    return tokens


def validate_raw_lisp(expr: str) -> None:
    """Raise :class:`RawBlocked` unless ``expr`` only uses allow-listed AutoLISP."""

    if not isinstance(expr, str) or not expr.strip():
        raise RawBlocked("LISP expression is empty")
    if len(expr) > MAX_LISP_CHARS:
        raise RawBlocked(f"LISP expression too long (>{MAX_LISP_CHARS} characters)")
    tokens = _lex(expr)
    stack: list[dict] = []
    pending_quote = False

    def new_frame(parent: dict | None) -> dict:
        quoted = pending_quote or (parent is not None and (parent["quoted"] or parent["children_quoted"]))
        skip_head = False
        if parent is not None:
            if parent["cond"]:
                skip_head = True
            elif parent["params_pending"]:
                skip_head = True
                parent["params_pending"] = False
            parent["seen"] += 1
        return {
            "quoted": quoted, "children_quoted": False, "seen": 0, "skip_head": skip_head,
            "cond": False, "params_pending": False, "setvar": False,
        }

    for kind, value in tokens:
        if kind == "quote":
            pending_quote = True
            continue
        if kind == "paren" and value == "(":
            stack.append(new_frame(stack[-1] if stack else None))
            if len(stack) > MAX_LISP_DEPTH:
                raise RawBlocked(f"LISP nesting too deep (>{MAX_LISP_DEPTH})")
            pending_quote = False
            continue
        if kind == "paren":
            if not stack:
                raise RawBlocked("LISP parentheses do not match (extra ')')")
            stack.pop()
            pending_quote = False
            continue
        frame = stack[-1] if stack else None
        if kind == "str":
            if frame is not None:
                if frame["setvar"]:
                    if value.upper() not in SAFE_SETVARS:
                        raise RawBlocked(f"setvar may not change the system variable {value!r}")
                    frame["setvar"] = False
                frame["seen"] += 1
            pending_quote = False
            continue
        # atom
        symbol = value.lower()
        if not (_NUMBER.match(value) or value == "."):
            if symbol in LISP_DENY or symbol.startswith(_LISP_DENY_PREFIXES):
                raise RawBlocked(f"LISP symbol {value} can run commands / touch files / call COM or evaluate indirectly; blocked")
            if frame is not None:
                if frame["setvar"]:
                    raise RawBlocked("setvar requires a string literal as variable name")
                if frame["seen"] == 0 and not frame["quoted"] and not frame["skip_head"]:
                    if symbol not in LISP_ALLOW:
                        raise RawBlocked(f"LISP function {value} is not on the allow-list")
                    if symbol == "quote":
                        frame["children_quoted"] = True
                    elif symbol == "cond":
                        frame["cond"] = True
                    elif symbol == "lambda":
                        frame["params_pending"] = True
                    elif symbol == "setvar":
                        frame["setvar"] = True
                frame["seen"] += 1
        elif frame is not None:
            if frame["setvar"]:
                raise RawBlocked("setvar requires a string literal as variable name")
            frame["seen"] += 1
        pending_quote = False
    if stack:
        raise RawBlocked("LISP parentheses do not match (missing ')')")
