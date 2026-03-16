#!/usr/bin/env python3
"""
CRL Linter — static checker for Concurrent Janus (CJanus) programs.

Usage:
    python3 tools/linter.py <file.crl>          # check one file
    python3 tools/linter.py <dir/>*.crl         # check multiple files (glob)
    python3 tools/linter.py --all               # check all .crl in ../CJanus-IPSJPRO25-3/progs/
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Regular expressions (mirrors runtime/runtime.py load_crl)
# ---------------------------------------------------------------------------

_RE_EMPTY   = re.compile(r'^\s*$')
_RE_BEGIN   = re.compile(r'^begin\s+([\w\d$.:]+)$')
_RE_END     = re.compile(r'^end\s+([\w\d$.:]+)$')
_RE_GOTO    = re.compile(
    r'^[\w\d$.:\[\]+\-*/!<=>&|%]*\s*->\s*([\w\d$.:]+)\s*(?:;\s*([\w\d$.:]+)\s*)?')
_RE_COMEFROM = re.compile(
    r'^\s*([\w\d$.:]+)\s*(?:;\s*([\w\d$.:]+)\s*)?<-[\w\d$.:\[\]+\-*/!<=>&|%]*')
_RE_CALL    = re.compile(r'^call\s+([\w\d$.:,\s]+)$')
_RE_LOCK    = re.compile(r'^lock\s+(.*)')
_RE_UNLOCK  = re.compile(r'^unlock\s+(.*)')
_RE_SET     = re.compile(r'^set\s+(\$[\w\d$.]+@\d+)\s+(\S+)')
_RE_UNSET   = re.compile(r'^unset\s+(\$[\w\d$.]+@\d+)\s+(\S+)')
_RE_PUSH    = re.compile(r'^push\s*$')
_RE_POP     = re.compile(r'^pop\s*$')


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class GotoRef:
    """A -> <label> or <cond> -> <label1>;<label2> instruction."""
    lineno: int        # 1-based original line number
    labels: list[str]  # labels referenced in the goto


@dataclass
class ComeFromRef:
    """A <label> <- or <label1>;<label2> <- <cond> instruction."""
    lineno: int
    labels: list[str]  # labels defined by this come-from


@dataclass
class ProcInfo:
    name: str
    begin_line: int
    end_line: Optional[int] = None

    # goto refs inside this proc
    gotos: list[GotoRef]    = field(default_factory=list)
    comefroms: list[ComeFromRef] = field(default_factory=list)

    # call targets
    calls: list[tuple[int, list[str]]] = field(default_factory=list)  # (lineno, [names])

    # push/pop counts
    push_count: int = 0
    pop_count: int  = 0
    push_lines: list[int] = field(default_factory=list)
    pop_lines:  list[int] = field(default_factory=list)

    # lock/unlock: var -> list of line numbers
    lock_lines:   dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    unlock_lines: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))

    # set/unset: (var@depth, src) -> list of line numbers
    set_lines:   dict[tuple[str,str], list[int]] = field(default_factory=lambda: defaultdict(list))
    unset_lines: dict[tuple[str,str], list[int]] = field(default_factory=lambda: defaultdict(list))

    # raw instruction count (non-empty, non-begin/end)
    instr_count: int = 0


@dataclass
class LintError:
    level: str   # "ERROR" or "WARNING"
    location: str
    message: str

    def __str__(self) -> str:
        return f"{self.level} [{self.location}]: {self.message}"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_lock_vars(args: str) -> list[str]:
    """Split 'a, b, c' into ['a', 'b', 'c']."""
    return [v.strip() for v in args.split(',') if v.strip()]


def parse_crl(lines: list[str]) -> tuple[list[ProcInfo], list[LintError]]:
    """
    Parse the CRL source lines.
    Returns (list[ProcInfo], parse_errors).
    Line numbers are 1-based.
    """
    errors: list[LintError] = []
    procs: list[ProcInfo] = []
    current: Optional[ProcInfo] = None

    # Track all goto labels (label -> lineno) and come-from labels (label -> lineno)
    # across the whole file (for goto/comefrom pairing)
    all_gotos:     dict[str, int] = {}   # label -> lineno
    all_comefroms: dict[str, int] = {}   # label -> lineno

    for raw_lineno, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if _RE_EMPTY.match(line):
            continue

        bgm = _RE_BEGIN.match(line)
        enm = _RE_END.match(line)
        gom = _RE_GOTO.match(line)
        com = _RE_COMEFROM.match(line)

        if bgm:
            proc_name = bgm.group(1)
            if current is not None:
                errors.append(LintError(
                    "ERROR",
                    f"line {raw_lineno}",
                    f"begin '{proc_name}' found inside procedure '{current.name}' "
                    f"(missing 'end {current.name}'?)"
                ))
            current = ProcInfo(name=proc_name, begin_line=raw_lineno)
            procs.append(current)
            continue

        if enm:
            proc_name = enm.group(1)
            if current is None:
                errors.append(LintError(
                    "ERROR",
                    f"line {raw_lineno}",
                    f"end '{proc_name}' without matching begin"
                ))
            elif current.name != proc_name:
                errors.append(LintError(
                    "ERROR",
                    f"line {raw_lineno}",
                    f"end '{proc_name}' does not match open begin '{current.name}'"
                ))
                current.end_line = raw_lineno
                current = None
            else:
                current.end_line = raw_lineno
                current = None
            continue

        if current is None:
            # instruction outside any procedure
            errors.append(LintError(
                "ERROR",
                f"line {raw_lineno}",
                f"instruction outside any procedure: {line!r}"
            ))
            continue

        current.instr_count += 1

        # goto
        if gom:
            refs = [gom.group(i) for i in (1, 2) if gom.group(i)]
            current.gotos.append(GotoRef(lineno=raw_lineno, labels=refs))
            for lbl in refs:
                if lbl in all_gotos:
                    errors.append(LintError(
                        "ERROR",
                        f"line {raw_lineno}",
                        f"goto label '{lbl}' defined more than once "
                        f"(first at line {all_gotos[lbl]})"
                    ))
                else:
                    all_gotos[lbl] = raw_lineno
            continue

        # come-from
        if com:
            refs = [com.group(i) for i in (1, 2) if com.group(i)]
            current.comefroms.append(ComeFromRef(lineno=raw_lineno, labels=refs))
            for lbl in refs:
                if lbl in all_comefroms:
                    errors.append(LintError(
                        "ERROR",
                        f"line {raw_lineno}",
                        f"come-from label '{lbl}' defined more than once "
                        f"(first at line {all_comefroms[lbl]})"
                    ))
                else:
                    all_comefroms[lbl] = raw_lineno
            continue

        # call
        callm = _RE_CALL.match(line)
        if callm:
            names = [n.strip() for n in callm.group(1).split(',') if n.strip()]
            current.calls.append((raw_lineno, names))
            continue

        # lock
        lockm = _RE_LOCK.match(line)
        if lockm:
            for var in _parse_lock_vars(lockm.group(1)):
                current.lock_lines[var].append(raw_lineno)
            continue

        # unlock
        unlockm = _RE_UNLOCK.match(line)
        if unlockm:
            for var in _parse_lock_vars(unlockm.group(1)):
                current.unlock_lines[var].append(raw_lineno)
            continue

        # set
        setm = _RE_SET.match(line)
        if setm:
            key = (setm.group(1), setm.group(2))
            current.set_lines[key].append(raw_lineno)
            continue

        # unset
        unsetm = _RE_UNSET.match(line)
        if unsetm:
            key = (unsetm.group(1), unsetm.group(2))
            current.unset_lines[key].append(raw_lineno)
            continue

        # push / pop
        if _RE_PUSH.match(line):
            current.push_count += 1
            current.push_lines.append(raw_lineno)
            continue

        if _RE_POP.match(line):
            current.pop_count += 1
            current.pop_lines.append(raw_lineno)
            continue

    # Unclosed procedure at EOF
    if current is not None:
        errors.append(LintError(
            "ERROR",
            f"line {current.begin_line}",
            f"procedure '{current.name}' has no matching 'end {current.name}'"
        ))

    return procs, errors, all_gotos, all_comefroms


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_begin_end(procs: list[ProcInfo]) -> list[LintError]:
    """All begin/end pairs are validated during parsing; this just collects
    procs with missing ends (already caught by parser but summarised here)."""
    errors = []
    for p in procs:
        if p.end_line is None:
            errors.append(LintError(
                "ERROR",
                f"proc {p.name}",
                f"begin at line {p.begin_line} has no matching end"
            ))
    return errors


def check_goto_comefrom(
    all_gotos: dict[str, int],
    all_comefroms: dict[str, int],
    procs: list[ProcInfo],
) -> list[LintError]:
    """
    Every label referenced in a goto must have a come-from, and vice versa.
    Auto-generated labels (_0, _1, …) are excluded.
    """
    errors = []

    # Build set of all goto-referenced labels and all come-from-defined labels
    goto_labels: dict[str, int] = {}     # label -> line of goto instruction
    cf_labels: dict[str, int] = {}       # label -> line of comefrom instruction

    for p in procs:
        for g in p.gotos:
            for lbl in g.labels:
                if not lbl.startswith('_'):
                    goto_labels[lbl] = g.lineno
        for c in p.comefroms:
            for lbl in c.labels:
                if not lbl.startswith('_'):
                    cf_labels[lbl] = c.lineno

    for lbl, lineno in sorted(goto_labels.items(), key=lambda x: x[1]):
        if lbl not in cf_labels:
            errors.append(LintError(
                "ERROR",
                f"line {lineno}",
                f"goto -> {lbl}: label '{lbl}' has no matching come-from"
            ))

    for lbl, lineno in sorted(cf_labels.items(), key=lambda x: x[1]):
        if lbl not in goto_labels:
            errors.append(LintError(
                "ERROR",
                f"line {lineno}",
                f"come-from {lbl} <-: label '{lbl}' has no matching goto"
            ))

    return errors


def check_call_targets(procs: list[ProcInfo]) -> list[LintError]:
    """All call targets must resolve to a defined procedure."""
    defined = {p.name for p in procs}
    errors = []
    for p in procs:
        for lineno, names in p.calls:
            for name in names:
                if name not in defined:
                    errors.append(LintError(
                        "ERROR",
                        f"line {lineno}",
                        f"call '{name}': procedure not defined"
                    ))
    return errors


def check_push_pop(procs: list[ProcInfo]) -> list[LintError]:
    """push and pop counts must match within each procedure."""
    errors = []
    for p in procs:
        if p.push_count != p.pop_count:
            errors.append(LintError(
                "ERROR",
                f"proc {p.name}",
                f"push/pop imbalance: {p.push_count} push, {p.pop_count} pop"
            ))
    return errors


def check_lock_unlock(procs: list[ProcInfo]) -> list[LintError]:
    """lock/unlock counts per variable must match within each procedure."""
    errors = []
    for p in procs:
        all_vars = set(p.lock_lines.keys()) | set(p.unlock_lines.keys())
        for var in sorted(all_vars):
            lcount = len(p.lock_lines.get(var, []))
            ucount = len(p.unlock_lines.get(var, []))
            if lcount != ucount:
                level = "ERROR" if abs(lcount - ucount) > 0 else "WARNING"
                errors.append(LintError(
                    "WARNING",
                    f"proc {p.name}",
                    f"lock/unlock imbalance for variable '{var}': "
                    f"{lcount} lock, {ucount} unlock"
                ))
    return errors


def check_set_unset(procs: list[ProcInfo]) -> list[LintError]:
    """set/unset counts per (var@depth, src) must match within each procedure."""
    errors = []
    for p in procs:
        all_keys = set(p.set_lines.keys()) | set(p.unset_lines.keys())
        for key in sorted(all_keys):
            scount = len(p.set_lines.get(key, []))
            ucount = len(p.unset_lines.get(key, []))
            var, src = key
            if scount != ucount:
                errors.append(LintError(
                    "ERROR",
                    f"proc {p.name}",
                    f"set/unset imbalance for '{var} {src}': "
                    f"{scount} set, {ucount} unset"
                ))
    return errors


# ---------------------------------------------------------------------------
# Block structure information (informational only)
# ---------------------------------------------------------------------------

def check_block_structure(procs: list[ProcInfo]) -> list[LintError]:
    """
    Informational only: report procedures whose instruction count is not a
    multiple of 3 (load_crl pads automatically, but it's useful to know).
    """
    warnings = []
    for p in procs:
        if p.instr_count % 3 != 0:
            warnings.append(LintError(
                "WARNING",
                f"proc {p.name}",
                f"instruction count {p.instr_count} is not a multiple of 3 "
                f"(load_crl will pad automatically)"
            ))
    return warnings


# ---------------------------------------------------------------------------
# Main lint driver
# ---------------------------------------------------------------------------

def lint_file(path: str) -> tuple[list[LintError], int, list[str]]:
    """
    Lint a single .crl file.
    Returns (errors, non_empty_line_count, proc_names).
    """
    try:
        with open(path, encoding='utf-8') as f:
            raw_lines = f.readlines()
    except OSError as exc:
        return [LintError("ERROR", "file", str(exc))], 0, []

    # Strip trailing newlines for display; keep original for line counting
    lines = [l.rstrip('\n').rstrip('\r') for l in raw_lines]
    non_empty = sum(1 for l in lines if not _RE_EMPTY.match(l))

    procs, parse_errors, all_gotos, all_comefroms = parse_crl(lines)

    errors: list[LintError] = list(parse_errors)

    # Run all checks (parse errors already include begin/end problems,
    # but we re-check incomplete procs for clarity)
    errors += check_begin_end(procs)
    errors += check_goto_comefrom(all_gotos, all_comefroms, procs)
    errors += check_call_targets(procs)
    errors += check_push_pop(procs)
    errors += check_lock_unlock(procs)
    errors += check_set_unset(procs)
    # block structure is informational only
    errors += check_block_structure(procs)

    proc_names = [p.name for p in procs]
    return errors, non_empty, proc_names


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

_CHECK_NAMES = [
    ("begin/end pairs",   "begin/end pairs"),
    ("goto/comefrom",     "goto/comefrom pairs"),
    ("call targets",      "call targets"),
    ("push/pop",          "push/pop balance"),
    ("lock/unlock",       "lock/unlock balance"),
    ("set/unset",         "set/unset balance"),
]

_CHECK_KEYWORDS: dict[str, list[str]] = {
    "begin/end pairs":   ["has no matching end", "without matching begin",
                          "does not match open begin", "missing 'end"],
    "goto/comefrom pairs": ["goto ->", "come-from", "matching come-from", "matching goto"],
    "call targets":      ["call '"],
    "push/pop balance":  ["push/pop imbalance"],
    "lock/unlock balance": ["lock/unlock imbalance"],
    "set/unset balance": ["set/unset imbalance"],
}

_BLOCK_STRUCTURE_KW = "instruction count"


def _errors_for_check(errors: list[LintError], keywords: list[str]) -> list[LintError]:
    return [e for e in errors
            if any(kw in e.message for kw in keywords)]


def report(path: str, errors: list[LintError], non_empty: int, proc_names: list[str]) -> bool:
    """Print a human-readable report. Returns True if there are hard errors."""
    print(f"\nCRL Linter: {path}")
    print(f"Lines: {non_empty} (after stripping empty lines)")
    if proc_names:
        print(f"Procedures: {', '.join(proc_names)}")
    else:
        print("Procedures: (none)")
    print()

    hard_errors = [e for e in errors if e.level == "ERROR"]
    warnings    = [e for e in errors if e.level == "WARNING"
                   and _BLOCK_STRUCTURE_KW not in e.message]

    check_defs = [
        ("begin/end pairs",    _CHECK_KEYWORDS["begin/end pairs"]),
        ("goto/comefrom pairs",_CHECK_KEYWORDS["goto/comefrom pairs"]),
        ("call targets",       _CHECK_KEYWORDS["call targets"]),
        ("push/pop balance",   _CHECK_KEYWORDS["push/pop balance"]),
        ("lock/unlock balance",_CHECK_KEYWORDS["lock/unlock balance"]),
        ("set/unset balance",  _CHECK_KEYWORDS["set/unset balance"]),
    ]

    for check_name, keywords in check_defs:
        relevant = _errors_for_check(errors, keywords)
        hard = [e for e in relevant if e.level == "ERROR"]
        warn = [e for e in relevant if e.level == "WARNING"
                and _BLOCK_STRUCTURE_KW not in e.message]
        if hard:
            print(f"✗ {check_name}: FAIL")
        elif warn:
            print(f"~ {check_name}: WARNING")
        else:
            print(f"✓ {check_name}: OK")

    print()

    # Print all ERROR and WARNING messages (excluding block structure warnings)
    displayed: list[LintError] = []
    for e in errors:
        if e.level == "ERROR":
            displayed.append(e)
        elif e.level == "WARNING" and _BLOCK_STRUCTURE_KW not in e.message:
            displayed.append(e)

    # Block structure warnings at the end if any
    block_warnings = [e for e in errors
                      if e.level == "WARNING" and _BLOCK_STRUCTURE_KW in e.message]

    if displayed:
        for e in displayed:
            print(str(e))
        print()

    if block_warnings:
        print("Block structure info (load_crl pads automatically):")
        for e in block_warnings:
            print(f"  INFO [{e.location}]: {e.message}")
        print()

    if hard_errors or warnings:
        print(f"{len(hard_errors)} error(s), {len(warnings)} warning(s) found.")
        return True
    else:
        print("No errors found.")
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _find_progs_dir() -> Optional[Path]:
    """Locate ../CJanus-IPSJPRO25-3/progs/ relative to this script."""
    script_dir = Path(__file__).resolve().parent
    candidate = script_dir.parent.parent / "CJanus-IPSJPRO25-3" / "progs"
    if candidate.is_dir():
        return candidate
    # Also try relative to cwd
    cwd_candidate = Path.cwd() / "CJanus-IPSJPRO25-3" / "progs"
    if cwd_candidate.is_dir():
        return cwd_candidate
    return None


def main() -> int:
    args = sys.argv[1:]

    if not args:
        print(__doc__, file=sys.stderr)
        return 2

    files: list[str] = []

    if args == ["--all"]:
        progs_dir = _find_progs_dir()
        if progs_dir is None:
            print("ERROR: could not locate CJanus-IPSJPRO25-3/progs/", file=sys.stderr)
            return 2
        files = sorted(str(p) for p in progs_dir.glob("*.crl"))
        if not files:
            print(f"No .crl files found in {progs_dir}", file=sys.stderr)
            return 2
    else:
        files = args

    overall_error = False
    for path in files:
        errors, non_empty, proc_names = lint_file(path)
        had_error = report(path, errors, non_empty, proc_names)
        if had_error:
            overall_error = True

    return 1 if overall_error else 0


if __name__ == "__main__":
    sys.exit(main())
