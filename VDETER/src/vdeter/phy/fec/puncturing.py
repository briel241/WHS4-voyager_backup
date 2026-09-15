"""M.2092-2 data pattern 6, tail pattern 6a and LLR rate recovery."""

from __future__ import annotations

DATA_PATTERN_6 = (
    (1, 1, 0, 0, 0, 0),
    (1, 0, 0, 0, 1, 0),
)
TAIL_PATTERN_6A = (
    (1, 1, 0, 0, 0, 0),
    (1, 1, 0, 0, 0, 0),
    (1, 0, 0, 0, 0, 0),
    (0, 0, 0, 1, 1, 0),
    (0, 0, 0, 1, 1, 0),
    (0, 0, 0, 1, 0, 0),
)
DATA_CLOCKS = 432
DATA_CHANNEL_BITS = 864
TAIL_CHANNEL_BITS = 10
CHANNEL_BITS = 874


def _select(rows, patterns) -> tuple:
    output = []
    for row_index, row in enumerate(rows):
        pattern = patterns[row_index % len(patterns)]
        if len(row) != 6:
            raise ValueError("Turbo mother-stream row must contain six bits")
        output.extend(value for value, keep in zip(row, pattern) if keep)
    return tuple(output)


def puncture(data_rows, tail_rows) -> tuple[int, ...]:
    data = tuple(data_rows)
    tail = tuple(tail_rows)
    if len(data) != DATA_CLOCKS or len(tail) != 6:
        raise ValueError("Link ID 11 puncturing requires 432 data and 6 tail rows")
    output = _select(data, DATA_PATTERN_6) + _select(tail, TAIL_PATTERN_6A)
    if len(output) != CHANNEL_BITS:
        raise AssertionError("Link ID 11 puncturing count invariant failed")
    return output


def puncture_general(data_rows, tail_rows, data_patterns, tail_patterns) -> tuple[int, ...]:
    """공통 §5 general puncturing: 임의 clock 수와 pattern 주기를 지원한다."""

    data = tuple(data_rows)
    tail = tuple(tail_rows)
    if len(tail) != len(tail_patterns):
        raise ValueError("general puncturing requires one tail pattern row per tail clock")
    if len(data) % len(data_patterns):
        raise ValueError("general puncturing requires whole data pattern periods")
    return _select(data, tuple(data_patterns)) + _select(tail, tuple(tail_patterns))


def rate_recover_general(
    channel_llrs, data_clocks: int, data_patterns, tail_patterns
) -> tuple[tuple[tuple[float, ...], ...], tuple[tuple[float, ...], ...]]:
    """general LLR rate recovery: punctured 위치에 exact `+0.0`을 둔다."""

    values = tuple(float(value) for value in channel_llrs)
    patterns = tuple(data_patterns)
    tails = tuple(tail_patterns)
    expected = sum(
        sum(patterns[clock % len(patterns)]) for clock in range(data_clocks)
    ) + sum(sum(pattern) for pattern in tails)
    if len(values) != expected:
        raise ValueError("general rate recovery LLR count mismatch")
    position = 0
    data_rows = []
    for clock in range(data_clocks):
        row = [0.0] * 6
        for stream, keep in enumerate(patterns[clock % len(patterns)]):
            if keep:
                row[stream] = values[position]
                position += 1
        data_rows.append(tuple(row))
    tail_rows = []
    for pattern in tails:
        row = [0.0] * 6
        for stream, keep in enumerate(pattern):
            if keep:
                row[stream] = values[position]
                position += 1
        tail_rows.append(tuple(row))
    if position != expected:
        raise AssertionError("general rate recovery count invariant failed")
    return tuple(data_rows), tuple(tail_rows)


def rate_recover(channel_llrs) -> tuple[tuple[float, ...], tuple[tuple[float, ...], ...]]:
    values = tuple(float(value) for value in channel_llrs)
    if len(values) != CHANNEL_BITS:
        raise ValueError("Link ID 11 rate recovery requires 874 LLRs")
    position = 0
    data_rows = []
    for clock in range(DATA_CLOCKS):
        row = [0.0] * 6
        for stream, keep in enumerate(DATA_PATTERN_6[clock % 2]):
            if keep:
                row[stream] = values[position]
                position += 1
        data_rows.append(tuple(row))
    tail_rows = []
    for pattern in TAIL_PATTERN_6A:
        row = [0.0] * 6
        for stream, keep in enumerate(pattern):
            if keep:
                row[stream] = values[position]
                position += 1
        tail_rows.append(tuple(row))
    if position != CHANNEL_BITS:
        raise AssertionError("Link ID 11 rate recovery count invariant failed")
    return tuple(data_rows), tuple(tail_rows)
