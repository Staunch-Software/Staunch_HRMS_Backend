"""Match payroll-sheet rows to employees.

Payroll sheets identify people inconsistently: IDs on some rows only, ID
columns holding job titles, names written as "A.NATARAJAN" for master
first "Natarajan" / last "A", role suffixes ("SUDHAKAR.V - DIRECTOR"),
dotted initials and stray spaces. Rules:
  1. an ID-shaped value whose normalized code exists      -> MATCHED_BY_ID
  2. otherwise token-set name similarity against everyone:
     one candidate >= MATCH_THRESHOLD and >= MATCH_MARGIN ahead -> MATCHED_BY_NAME
     candidates >= AMBIGUOUS_THRESHOLD                        -> AMBIGUOUS
     nothing                                                  -> UNMATCHED
Inactive employees are ranked below Active ones (INACTIVE_PENALTY). A row is
never auto-assigned when ambiguous, and one employee is never matched to two
rows of the same import: the later row becomes AMBIGUOUS ("duplicate").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.services.employee_code import normalize_employee_code

MATCHED_BY_ID = "MATCHED_BY_ID"
MATCHED_BY_NAME = "MATCHED_BY_NAME"
AMBIGUOUS = "AMBIGUOUS"
UNMATCHED = "UNMATCHED"
MATCH_STATUSES = (MATCHED_BY_ID, MATCHED_BY_NAME, AMBIGUOUS, UNMATCHED)

MATCH_THRESHOLD = 0.85
MATCH_MARGIN = 0.10
AMBIGUOUS_THRESHOLD = 0.60
CANDIDATE_FLOOR = 0.45
ID_NAME_CONFLICT = 0.5
INACTIVE_PENALTY = 0.10
FUZZY_TOKEN_MIN = 0.80
INITIAL_TOKEN_WEIGHT = 0.5
INITIAL_MATCH_FACTOR = 0.95
OMITTED_INITIAL_DISCOUNT = 0.25

# Letters, optional separator, digits: "STEMP0005", "ST-EMP-05", "E 12". A
# value without digits (job titles in an ID column) is never an ID.
_ID_SHAPE = re.compile(r"^[A-Za-z]{1,8}(?:[\s\-_/.]*[A-Za-z]{1,8})*[\s\-_/.]*\d{1,8}$|^\d{3,8}$")
_ROLE_SUFFIX = re.compile(r"\s+[-–—]+\s+.*$")
_HONORIFICS = {"MR", "MRS", "MS", "MISS", "DR", "SHRI", "SMT", "KUM"}


def looks_like_id(value) -> bool:
    return bool(value) and bool(_ID_SHAPE.match(str(value).strip()))


def name_tokens(value) -> list[str]:
    """'SUDHAKAR.V - DIRECTOR' -> ['SUDHAKAR', 'V']; 'A.NATARAJAN' -> ['A', 'NATARAJAN']."""
    if not value:
        return []
    text = _ROLE_SUFFIX.sub("", str(value).strip())
    text = re.sub(r"[^A-Za-z]+", " ", text).upper()
    return [t for t in text.split() if t not in _HONORIFICS]


def employee_tokens(employee: Employee) -> list[str]:
    return name_tokens(" ".join(p for p in (employee.first_name, employee.middle_name, employee.last_name) if p))


def employee_display_name(employee: Employee) -> str:
    return " ".join(p for p in (employee.first_name, employee.middle_name, employee.last_name) if p)


def _weight(token: str) -> float:
    return INITIAL_TOKEN_WEIGHT if len(token) == 1 else 1.0


def _token_similarity(a: list[str], b: list[str]) -> float:
    """Weighted token-set alignment (Dice-style: matched weight / total
    weight of both sides). Full tokens match exactly (1.0) or fuzzily (ratio
    >= FUZZY_TOKEN_MIN); a single-letter token is an initial (half weight)
    and matches an equal initial or the first letter of a full token
    (discounted by INITIAL_MATCH_FACTOR)."""
    if not a or not b:
        return 0.0
    total = sum(map(_weight, a)) + sum(map(_weight, b))
    matched = 0.0
    used_a: set[int] = set()
    used_b: set[int] = set()

    pairs = []
    for i, ta in enumerate(a):
        if len(ta) == 1:
            continue
        for j, tb in enumerate(b):
            if len(tb) > 1:
                ratio = 1.0 if ta == tb else SequenceMatcher(None, ta, tb).ratio()
                if ratio >= FUZZY_TOKEN_MIN:
                    pairs.append((ratio, i, j))
    for ratio, i, j in sorted(pairs, reverse=True):
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        matched += 2 * ratio

    def pair_initials(x: list[str], used_x: set[int], y: list[str], used_y: set[int]) -> float:
        gained = 0.0
        for i, tx in enumerate(x):
            if len(tx) != 1 or i in used_x:
                continue
            # An equal initial first, then a full token starting with the letter.
            for want_initial in (True, False):
                hit = next((j for j, ty in enumerate(y) if j not in used_y and ty[0] == tx
                            and (len(ty) == 1) == want_initial), None)
                if hit is not None:
                    used_x.add(i)
                    used_y.add(hit)
                    gained += (_weight(tx) + _weight(y[hit])) * (1.0 if want_initial else INITIAL_MATCH_FACTOR)
                    break
        return gained

    matched += pair_initials(a, used_a, b, used_b)
    matched += pair_initials(b, used_b, a, used_a)
    # An initial left over on one side only (the sheet simply omits it) costs
    # half as much as one that conflicts with a different initial on the other.
    left_a = [t for i, t in enumerate(a) if i not in used_a and len(t) == 1]
    left_b = [t for i, t in enumerate(b) if i not in used_b and len(t) == 1]
    if bool(left_a) != bool(left_b):
        total -= OMITTED_INITIAL_DISCOUNT * (len(left_a) + len(left_b))
    return matched / total


def name_similarity(row_name, emp_tokens: list[str]) -> float:
    a = name_tokens(row_name)
    b = emp_tokens
    if not a or not b:
        return 0.0
    best = _token_similarity(a, b)
    # Names run together or split differently ("RAMESHKUMAR" vs "Ramesh Kumar").
    a_full = "".join(t for t in a if len(t) > 1)
    b_full = "".join(t for t in b if len(t) > 1)
    if a_full and b_full and (len(a) != len(b) or a_full != b_full):
        joined = SequenceMatcher(None, a_full, b_full).ratio()
        if joined >= 0.92 and sorted(t for t in a if len(t) == 1) == sorted(t for t in b if len(t) == 1):
            best = max(best, joined * 0.95)
    return round(min(best, 1.0), 4)


@dataclass
class Candidate:
    employee_id: int
    employee_code: str
    name: str
    score: float
    status: str

    def as_dict(self) -> dict:
        return {"employee_id": self.employee_id, "employee_code": self.employee_code, "name": self.name,
                "score": self.score, "status": self.status}


@dataclass
class MatchResult:
    match_status: str
    employee_id: Optional[int] = None
    employee_code: Optional[str] = None
    employee_name: Optional[str] = None
    score: Optional[float] = None
    employee_status: Optional[str] = None
    candidates: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class EmployeeMatcher:
    """Holds the employee master in memory for one import (a few hundred rows)."""

    def __init__(self, employees: list[Employee]):
        self.employees = employees
        self.by_code = {normalize_employee_code(e.employee_code): e for e in employees}
        self.tokens = {e.id: employee_tokens(e) for e in employees}
        self.by_id = {e.id: e for e in employees}

    @classmethod
    def from_db(cls, db: Session) -> "EmployeeMatcher":
        return cls(db.query(Employee).order_by(Employee.id).all())

    def _candidate(self, employee: Employee, score: float) -> Candidate:
        return Candidate(employee.id, employee.employee_code, employee_display_name(employee), score, employee.status)

    def ranked(self, excel_name) -> list[tuple[float, Candidate]]:
        """(rank score, candidate) best first; rank score carries the inactive penalty."""
        out = []
        for employee in self.employees:
            score = name_similarity(excel_name, self.tokens[employee.id])
            if score < CANDIDATE_FLOOR:
                continue
            rank = score - (INACTIVE_PENALTY if employee.status != "Active" else 0.0)
            out.append((round(rank, 4), self._candidate(employee, score)))
        out.sort(key=lambda item: (-item[0], item[1].employee_code))
        return out

    def match(self, excel_id, excel_name) -> MatchResult:
        notes: list[str] = []
        if looks_like_id(excel_id):
            employee = self.by_code.get(normalize_employee_code(excel_id))
            if employee is not None:
                result = MatchResult(MATCHED_BY_ID, employee.id, employee.employee_code,
                                     employee_display_name(employee), 1.0, employee.status)
                if excel_name:
                    similarity = name_similarity(excel_name, self.tokens[employee.id])
                    if similarity < ID_NAME_CONFLICT:
                        # The ID points at someone else than the name does: never auto-assign.
                        others = [c for _, c in self.ranked(excel_name) if c.employee_id != employee.id]
                        if others and others[0].score >= MATCH_THRESHOLD:
                            by_id = self._candidate(employee, similarity)
                            return MatchResult(AMBIGUOUS, candidates=[by_id, *others[:2]], notes=[
                                f"ID {employee.employee_code} belongs to a differently named employee; "
                                f"the name matches {others[0].employee_code}"])
                        result.notes.append("name on the sheet differs from the employee master - please check")
                if employee.status != "Active":
                    result.notes.append(f"employee is {employee.status} in the master - payroll runs leave them out until reactivated")
                return result
            notes.append(f"ID '{excel_id}' not found in the employee master; matched on name")

        ranked = self.ranked(excel_name)
        top3 = [c for _, c in ranked[:3]]
        if not ranked or ranked[0][0] < AMBIGUOUS_THRESHOLD:
            if not excel_name:
                notes.append("no name on the row")
            return MatchResult(UNMATCHED, candidates=top3, notes=notes)
        best_rank, best = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
        if best.score >= MATCH_THRESHOLD and best_rank - runner_up >= MATCH_MARGIN - 1e-9:
            if best.status != "Active":
                notes.append(f"employee is {best.status} in the master - payroll runs leave them out until reactivated")
            return MatchResult(MATCHED_BY_NAME, best.employee_id, best.employee_code, best.name, best.score,
                               best.status, candidates=top3, notes=notes)
        notes.append("several employees have similar names" if best.score >= MATCH_THRESHOLD
                     else "no confident name match")
        return MatchResult(AMBIGUOUS, candidates=[c for r, c in ranked[:3] if r >= CANDIDATE_FLOOR], notes=notes)


def mark_duplicates(results: list[tuple[str, int, MatchResult]]) -> None:
    """results = [(sheet, row_number, MatchResult)] in sheet/row order. An
    employee already claimed by an earlier row (ID matches claim first) turns
    the later row AMBIGUOUS with a duplicate note; the claimed employee stays
    among its candidates so HR can still pick it deliberately."""
    claimed: dict[int, tuple[str, int]] = {}
    ordered = sorted(results, key=lambda item: item[2].match_status != MATCHED_BY_ID)
    for sheet, row_number, result in ordered:
        if result.match_status not in (MATCHED_BY_ID, MATCHED_BY_NAME):
            continue
        owner = claimed.get(result.employee_id)
        if owner is None:
            claimed[result.employee_id] = (sheet, row_number)
            continue
        where = f"row {owner[1]}" if owner[0] == sheet else f"sheet {owner[0]} row {owner[1]}"
        kept = Candidate(result.employee_id, result.employee_code, result.employee_name, result.score or 0.0,
                         result.employee_status or "")
        others = [c for c in result.candidates if c.employee_id != result.employee_id]
        result.candidates = [kept, *others][:3]
        result.notes.append(f"duplicate: {result.employee_code} is already matched to {where}")
        result.match_status = AMBIGUOUS
        result.employee_id = result.employee_code = result.employee_name = result.employee_status = None
        result.score = None
