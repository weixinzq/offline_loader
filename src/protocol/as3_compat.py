"""Protocol helpers for ActionScript 3 integer semantics.

AS3 bitwise operators coerce their operands to signed 32-bit integers, and
its remainder operator truncates division toward zero.  Python integers are
unbounded and Python's ``%`` uses floor division, so protocol code must make
both conversions explicit.
"""
from __future__ import annotations


UINT32_MASK = 0xFFFFFFFF
INT32_SIGN_BIT = 0x80000000
INT32_MODULUS = 0x100000000


def to_as3_int32(value: int) -> int:
    """Return the signed 32-bit value produced by an AS3 bitwise operation."""
    value &= UINT32_MASK
    if value & INT32_SIGN_BIT:
        return value - INT32_MODULUS
    return value


def as3_remainder(dividend: int, divisor: int) -> int:
    """Return AS3/JavaScript remainder (division truncated toward zero)."""
    if divisor == 0:
        raise ZeroDivisionError("integer modulo by zero")
    quotient = abs(dividend) // abs(divisor)
    if (dividend < 0) != (divisor < 0):
        quotient = -quotient
    return dividend - quotient * divisor
