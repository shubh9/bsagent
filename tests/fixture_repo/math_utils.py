"""Basic math utilities."""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def multiply(a, b):
    return a * b


def divide(a, b):
    # BUG: no zero-division guard
    return a / b


def factorial(n):
    """Return n! for non-negative integers."""
    if n < 0:
        raise ValueError("factorial undefined for negative numbers")
    result = 1
    # BUG: off-by-one — should be range(1, n + 1)
    for i in range(1, n):
        result *= i
    return result
