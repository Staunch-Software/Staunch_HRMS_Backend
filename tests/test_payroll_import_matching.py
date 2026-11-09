"""Employee matching for the payroll workbook import (synthetic names only)."""
from app.models.employee import Employee
from app.services.payroll_import.matching import (
    AMBIGUOUS,
    MATCHED_BY_ID,
    MATCHED_BY_NAME,
    UNMATCHED,
    EmployeeMatcher,
    looks_like_id,
    mark_duplicates,
    name_similarity,
    name_tokens,
)


def _emp(id_, code, first, last=None, middle=None, status="Active"):
    return Employee(id=id_, employee_code=code, first_name=first, middle_name=middle, last_name=last, status=status)


def _matcher(*employees):
    return EmployeeMatcher(list(employees))


def test_name_tokens_normalize_punctuation_initials_and_role_suffix():
    assert name_tokens("A.ZORVATH") == ["A", "ZORVATH"]
    assert name_tokens("  QUELLAN.V - DIRECTOR ") == ["QUELLAN", "V"]
    assert name_tokens("Mr. P.K.  Ostrander") == ["P", "K", "OSTRANDER"]
    assert name_tokens(None) == []


def test_looks_like_id():
    assert looks_like_id("ST-EMP-05")
    assert looks_like_id("STEMP0005")
    assert not looks_like_id("Technician")
    assert not looks_like_id("Sr Supervisor")
    assert not looks_like_id(None)


def test_initials_match_first_letter_of_full_token():
    assert name_similarity("A.ZORVATH", name_tokens("Zorvath A")) == 1.0
    assert name_similarity("QUELLAN.V - DIRECTOR", name_tokens("Quellan Varnish")) > 0.95
    assert name_similarity("A.ZORVATH", name_tokens("Zorvath K")) < 0.7


def test_matched_by_id_uses_normalized_code():
    m = _matcher(_emp(1, "STEMP0005", "Zorvath", "A"), _emp(2, "STEMP0006", "Brelik"))
    result = m.match("ST-EMP-05", "A.ZORVATH")
    assert result.match_status == MATCHED_BY_ID
    assert result.employee_id == 1
    assert result.notes == []


def test_id_not_found_falls_back_to_name():
    m = _matcher(_emp(1, "STEMP0005", "Zorvath", "A"))
    result = m.match("ST-EMP-99", "A. ZORVATH")
    assert result.match_status == MATCHED_BY_NAME
    assert result.employee_id == 1
    assert any("not found" in n for n in result.notes)


def test_job_title_in_id_column_is_ignored():
    m = _matcher(_emp(1, "STEMP0005", "Quellan", "V"))
    result = m.match("Technician", "QUELLAN.V - DIRECTOR")
    assert result.match_status == MATCHED_BY_NAME
    assert result.employee_id == 1
    assert result.score >= 0.85


def test_dotted_initials_and_extra_spaces():
    m = _matcher(_emp(1, "STEMP0001", "Ostrander", "K", middle="P"), _emp(2, "STEMP0002", "Ostrander", "M"))
    result = m.match(None, "P.K.   OSTRANDER")
    assert result.match_status == MATCHED_BY_NAME
    assert result.employee_id == 1


def test_omitted_initial_still_matches_a_unique_name():
    m = _matcher(_emp(1, "STEMP0001", "Brelik", "T"), _emp(2, "STEMP0002", "Cazimir", "R"))
    result = m.match(None, "BRELIK")
    assert result.match_status == MATCHED_BY_NAME
    assert result.employee_id == 1


def test_ambiguous_when_two_close_candidates():
    m = _matcher(_emp(1, "STEMP0001", "Brelik", "T"), _emp(2, "STEMP0002", "Brelik", "S"))
    result = m.match(None, "BRELIK")
    assert result.match_status == AMBIGUOUS
    assert result.employee_id is None
    assert {c.employee_id for c in result.candidates} == {1, 2}
    assert len(result.candidates) <= 3


def test_unmatched_when_nobody_is_close():
    m = _matcher(_emp(1, "STEMP0001", "Brelik", "T"))
    result = m.match(None, "XANTHOPOULOS")
    assert result.match_status == UNMATCHED
    assert result.employee_id is None


def test_inactive_employee_is_deprioritized():
    m = _matcher(_emp(1, "STEMP0001", "Brelik", "T", status="Inactive"), _emp(2, "STEMP0002", "Brelik", "T"))
    result = m.match(None, "T.BRELIK")
    assert result.match_status == MATCHED_BY_NAME
    assert result.employee_id == 2


def test_inactive_only_candidate_matches_with_note():
    m = _matcher(_emp(1, "STEMP0001", "Brelik", "T", status="Inactive"))
    result = m.match(None, "T.BRELIK")
    assert result.match_status == MATCHED_BY_NAME
    assert any("Inactive" in n for n in result.notes)


def test_id_pointing_at_someone_else_by_name_is_ambiguous():
    m = _matcher(_emp(1, "STEMP0001", "Brelik", "T"), _emp(2, "STEMP0002", "Cazimir", "R"))
    result = m.match("STEMP0001", "CAZIMIR R")
    assert result.match_status == AMBIGUOUS
    assert [c.employee_id for c in result.candidates][:2] == [1, 2]


def test_duplicate_row_for_same_employee_becomes_ambiguous():
    m = _matcher(_emp(1, "STEMP0001", "Brelik", "T"), _emp(2, "STEMP0002", "Cazimir", "R"))
    results = [
        ("S1", 3, m.match(None, "T BRELIK")),
        ("S1", 4, m.match("STEMP0001", "BRELIK T")),
        ("S1", 5, m.match(None, "CAZIMIR")),
    ]
    mark_duplicates(results)
    # The ID match claims the employee first; the earlier name-matched row is the duplicate.
    assert results[1][2].match_status == MATCHED_BY_ID
    dup = results[0][2]
    assert dup.match_status == AMBIGUOUS
    assert any("duplicate" in n for n in dup.notes)
    assert dup.candidates[0].employee_id == 1
    assert results[2][2].match_status == MATCHED_BY_NAME
