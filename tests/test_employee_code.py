import pytest

from app.services.employee_code import normalize_employee_code


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("STEMP0005", "STEMP0005"),
        ("ST-EMP-05", "STEMP0005"),
        ("ST-EMP-112", "STEMP0112"),
        ("st emp 0192 ", "STEMP0192"),
        ("STINT0007", "STINT0007"),
        ("\nSTEMP0001", "STEMP0001"),
        ("STEMP12345", "STEMP12345"),
        ("1234", "1234"),
        ("ENGINEER", "ENGINEER"),
        (None, ""),
        ("", ""),
    ],
)
def test_normalize_employee_code(raw, expected):
    assert normalize_employee_code(raw) == expected
