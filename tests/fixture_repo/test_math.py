"""Tests for math_utils — some intentionally failing."""
import pytest
from math_utils import add, subtract, multiply, divide, factorial


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0
    assert add(0, 0) == 0


def test_subtract():
    assert subtract(5, 3) == 2
    assert subtract(0, 5) == -5


def test_multiply():
    assert multiply(3, 4) == 12
    assert multiply(-2, 5) == -10
    assert multiply(0, 100) == 0


def test_divide():
    assert divide(10, 2) == 5.0
    assert divide(7, 2) == 3.5


def test_divide_by_zero():
    # Should raise ZeroDivisionError — currently no guard in divide()
    with pytest.raises(ZeroDivisionError):
        divide(5, 0)


def test_factorial():
    assert factorial(0) == 1
    assert factorial(1) == 1
    assert factorial(5) == 120   # FAILS due to off-by-one bug
    assert factorial(3) == 6
