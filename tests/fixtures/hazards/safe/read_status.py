def step_failed(record: dict) -> bool:
    raw = record.get("exit_code", "")
    if not raw.isdigit():
        raise ValueError(f"no verdict: exit_code={raw!r}")
    return int(raw) != 0
