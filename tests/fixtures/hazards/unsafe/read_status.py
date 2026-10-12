def step_failed(record: dict) -> bool:
    return int(record.get("exit_code") or 0) != 0
