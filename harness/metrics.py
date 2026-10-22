"""Small run summary helpers."""

def score_table(rounds: list[dict]) -> str:
    lines = ["| Round | Decision | Total |", "|---:|---|---:|"]
    lines.extend(f"| {row['round']} | {row['decision']} | {row['total']:.2f} |" for row in rounds)
    return "\n".join(lines)
