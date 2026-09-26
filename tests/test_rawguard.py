"""Regression tests for the raw command / AutoLISP guards.

Every 'BLOCKED' case below is a spelling that slipped through the v0.1 regex guard
(measured: 10 of 11 command probes and 12 of 12 LISP probes bypassed it).
"""

import pytest

from cad_super_mcp.errors import RawBlocked
from cad_super_mcp.rawguard import validate_raw_command, validate_raw_lisp

BLOCKED_COMMANDS = [
    "_NETLOAD\nfoo.dll",
    "_.NETLOAD",
    ".NETLOAD",
    "_.APPLOAD",
    "_.SHELL calc",
    "'_.SHELL",
    "_.-VBARUN foo",
    "_.VBALOAD x.dvb",
    "_.CUILOAD x.cuix",
    "_.RSCRIPT",
    "SH",
    "START calc.exe",
    "_LINE 0,0 1,1\n_.NETLOAD",          # dangerous verb hidden on a later line
    '_NETLOAD"x.dll"',                    # glued argument
    "_.SAVEAS c:\\\\tmp\\\\x.dwg",        # would bypass cad_save's confirm gate
    "_.WBLOCK c:\\\\x y",
    "_QUIT",
    "_.CLOSE",
    "_LINE 0,0 1,1\\n_.NETLOAD",          # literal backslash-n sequence
    "",
    "   ",
]

ALLOWED_COMMANDS = [
    ("_CIRCLE\n0,0\n50\n", "CIRCLE"),
    ("_.LINE 0,0 100,0", "LINE"),
    ("_-LAYER\n_M\nNEWLAYER\n\n", "LAYER"),
    ("'ZOOM E", "ZOOM"),
    ("_.PLINE 0,0 100,0 100,60 C", "PLINE"),
]


@pytest.mark.parametrize("cmd", BLOCKED_COMMANDS)
def test_dangerous_or_empty_commands_are_blocked(cmd):
    with pytest.raises(RawBlocked):
        validate_raw_command(cmd)


@pytest.mark.parametrize(("cmd", "verb"), ALLOWED_COMMANDS)
def test_ordinary_drawing_commands_are_allowed(cmd, verb):
    assert validate_raw_command(cmd) == verb


def test_unknown_first_command_needs_explicit_opt_in():
    with pytest.raises(RawBlocked):
        validate_raw_command("_.SOLIDEDIT")
    assert validate_raw_command("_.SOLIDEDIT", extra_allowed=["solidedit"]) == "SOLIDEDIT"


def test_extra_allowed_never_overrides_the_deny_list():
    with pytest.raises(RawBlocked):
        validate_raw_command("_.NETLOAD", extra_allowed=["NETLOAD"])


def test_too_many_lines_is_rejected():
    with pytest.raises(RawBlocked):
        validate_raw_command("_LINE\n" + "0,0\n" * 100)


BLOCKED_LISP = [
    '(startapp "calc.exe")',
    '( startapp "calc.exe")',                                   # whitespace after '('
    "(apply 'startapp '(\"calc.exe\"))",                        # symbol not in head position
    "(vl-catch-all-apply 'vl-file-delete '(\"x\"))",
    '(command "_.NETLOAD" "x.dll")',                            # bypass of the command guard via LISP
    '(vl-cmdf "_.SHELL" "calc")',
    '(vlax-invoke (vlax-create-object "WScript.Shell") "Run" "calc.exe")',
    '(vlax-create-object "Scripting.FileSystemObject")',
    '(vl-load-all "x.vlx")',
    "(autoload \"x\" '(\"c:foo\"))",
    '(vl-registry-delete "HKEY_CURRENT_USER\\\\Software\\\\x")',
    '(open "C:/x.txt" "w")',
    '(eval (read (strcat "(star" "tapp \\"calc.exe\\")")))',    # string-assembled call
    "(mapcar 'startapp '(\"calc.exe\"))",
    '(vla-sendcommand (vla-get-activedocument (vlax-get-acad-object)) "_.NETLOAD ")',
    '(defun c:evil () (princ))',
    "(c:foo)",                                                  # unknown user command
    '(setvar "SECURELOAD" 0)',                                  # security-relevant variable
    '(setvar "FILEDIA" 0)',
    "(setvar 'clayer \"0\")",                                   # variable name must be a literal
    '(entsel "pick")',                                          # interactive: would hang
    '(alert "hi")',
    '(getenv "PATH")',
    "(princ \"unterminated",
    "(getvar \"DWGNAME\"",                                      # unbalanced
    "(getvar \"DWGNAME\"))",
    ";| unterminated comment (getvar \"x\")",
    "",
]

ALLOWED_LISP = [
    '(getvar "DWGNAME")',
    "(+ 1 2)",
    '(strcat "a" "b")',
    "(setq p (list 0.0 0.0 0.0))",
    "(mapcar '(lambda (x) (* x 2)) '(1 2 3))",
    "(cond ((> a 1) (princ \"big\")) (t (princ \"small\")))",
    '(entget (entlast))',
    "(foreach e lst (princ e))",
    '(setvar "CLAYER" "A-WALL")',
    "(quote (unquoted heads are just data here))",
    "'((0 . \"LINE\") (10 0.0 0.0 0.0))",
    '(entmake (list (cons 0 "LINE") (cons 10 (list 0.0 0.0 0.0)) (cons 11 (list 100.0 0.0 0.0))))',
    '(princ "(startapp is only text here)")',                  # dangerous words inside strings are data
    '(getvar "DWGNAME") ; (startapp "x") in a comment',
]


@pytest.mark.parametrize("expr", BLOCKED_LISP)
def test_dangerous_lisp_is_blocked(expr):
    with pytest.raises(RawBlocked):
        validate_raw_lisp(expr)


@pytest.mark.parametrize("expr", ALLOWED_LISP)
def test_ordinary_lisp_is_allowed(expr):
    validate_raw_lisp(expr)


def test_innocent_drawing_text_exit_is_not_a_false_positive_in_lisp():
    # v0.1 blocked the *command* text EXIT (fire-exit signs). In LISP strings it is just data.
    validate_raw_lisp('(entmake (list (cons 0 "TEXT") (cons 1 "EXIT") (cons 10 (list 0.0 0.0 0.0)) (cons 40 250.0)))')
