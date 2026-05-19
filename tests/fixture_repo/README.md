# fixture_repo

Small Python project used as a test fixture for bsagent E2E tests.

## Known issues (intentional)

- `math_utils.py` — `factorial()` has an off-by-one bug (`range(1, n)` should be `range(1, n + 1)`)
- `math_utils.py` — `divide()` has no zero-division guard
- `string_utils.py` — all functions are missing docstrings
- `test_math.py` — `test_factorial` and `test_divide_by_zero` fail against the current code
