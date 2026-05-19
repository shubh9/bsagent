def reverse_string(s):
    return s[::-1]


def count_vowels(s):
    return sum(1 for c in s.lower() if c in "aeiou")


def is_palindrome(s):
    cleaned = "".join(c.lower() for c in s if c.isalnum())
    return cleaned == cleaned[::-1]


def truncate(s, max_len, suffix="..."):
    if len(s) <= max_len:
        return s
    return s[: max_len - len(suffix)] + suffix


def title_case(s):
    return " ".join(word.capitalize() for word in s.split())
